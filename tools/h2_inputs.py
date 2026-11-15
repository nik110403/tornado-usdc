#!/usr/bin/env python3
"""H2 inputs producer (offline, NO RPC, never re-runs g1): the two signed post-stage inputs
  deposit_counts.csv   address,deposit_count        one row per MATCHED exposed wallet
  usdc_pool_flag.csv   address,usdc_pool_depositor  one row per ELIGIBLE exposed wallet (all arm=exposed rows)
plus h2_inputs_receipt.json / h2_inputs_summary.md (input hashes, counts, oracles, cut points).

Reads only persisted getLogs chunks (read-only ChunkStore, coverage proofs): Deposit logs from the S21 June census sweep_b
plus the S22 reconcile deposit chunks, EncryptedNote logs from sweep_a plus the S22 encryptednote chunks. The depositor of a
Deposit log is the EncryptedNote sender (topics[1]) joined on (block, tx hash); unmatched deposits stay unresolved (no tx.from
fallback). Everything is fail-closed: any violated assertion raises H2Error, exit 3, and nothing final is written.
Tribunal chg-e5801bb3 (plans/tornado-usdc-pipeline/tribunal/2026-10-28-h2-inputs/report.md).
"""
import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections import Counter, defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from diagnostics import s22_reconcile as rc  # noqa: E402  (ChunkStore reader pattern + event decoders)
from gates import g1_cohort as g1  # noqa: E402
from lib import blocks, rpc  # noqa: E402

CUTOFF = 15_298_282
COHORT_SHA256 = "63e00508adc5fad8281d4139cac036193661b71acc0aa96b1c8b63bcc7425efb"
USDC_POOLS = ("0xd96f2b1c14db8458374d9aca76e26c3d18364307", "0x4736dcf1b7a3d580672cce6e7c65cd5cc9cfba9d")
N_POOLS = 23
ORACLES = {"deposit_logs": 163_696, "matched": 138_772, "residual": 24_924,
           "n_matched_exposed": 4_487, "n_eligible_exposed": 4_501,
           "q1": 0.0, "q2": 1.0, "flagged": 109, "zero": 1_818,
           "unresolved_usdc": {USDC_POOLS[1]: 96, USDC_POOLS[0]: 36}}
# the 3 observed EncryptedNote emitters on matched deposit transactions (full addresses taken from the persisted data)
NOTE_EMITTERS = frozenset({"0x722122df12d4e14e13ac3b6895a86e84145b6967",
                           "0xd90e2f925da726b50c4ed8d0fb90ad053324f31b",
                           "0x905b63fff465b9ffbf41dea908ceb12478ec7601"})
# expected endpoint per store (S21 junef sweeps ran on tenderly, S22 reconcile chunks on mevblocker)
EXPECTED_ENDPOINT = {"junef_deposit": "tenderly", "junef_note": "tenderly", "s22_deposit": "mevblocker", "s22_note": "mevblocker"}


class H2Error(Exception):
    pass


def need(cond, msg):
    if not cond:
        raise H2Error(msg)


def need_regular(path, what):
    need(not os.path.islink(path), f"{what}: {path} is a symlink (refused)")
    need(os.path.isfile(path), f"{what}: {path} is not a regular file")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def quantile7(xs, p):
    """R quantile(type = 7)."""
    s = sorted(xs)
    h = (len(s) - 1) * p
    lo = int(h // 1)
    hi = min(lo + 1, len(s) - 1)
    return float(s[lo] + (h - lo) * (s[hi] - s[lo]))


# ------------------------------------------------------------------ chunk reading

def read_store(directory, lo, hi, topic0, cutoff, expect):
    """Validate every chunk (read-only ChunkStore), prove coverage of [lo, hi], and return
    (events, binding, proof) for logs with topics[0] == topic0, filtered to block <= cutoff.
    expect = (endpoint, address|None, topics): the store's own binding.json is NOT trusted, it must equal this
    (same comparison as diagnostics/s22_reconcile.py read_sweep). event = dict(block, idx, tx, address, topics, data)."""
    need(os.path.isdir(directory) and not os.path.islink(directory), f"{directory}: not a real directory (symlink or missing)")
    bpath = os.path.join(directory, "binding.json")
    need_regular(bpath, f"{directory}: binding.json")
    for name in os.listdir(directory):
        need(not os.path.islink(os.path.join(directory, name)), f"{directory}/{name}: symlink (refused)")
    with open(bpath) as f:
        binding = json.load(f)
    endpoint, address, topics = expect
    q = binding.get("query", {})
    want = {"endpoint": endpoint, "address": address, "topics": topics, "shape": rpc.query_shape(address, topics)}
    need({k: q.get(k) for k in want} == want, f"{directory}: binding query {q} != expected {want}")
    store = rc._ReadOnlyStore(directory, binding)
    try:
        proof = store.coverage_proof(lo, hi)
    except (rpc.CheckpointError, rpc.CoverageError) as e:
        raise H2Error(f"{directory}: {e}") from e
    t0 = topic0.lower()
    out, dropped = [], 0
    for a, b in sorted(store._index):
        for l in store.read_chunk(a, b)["logs"]:
            if l["topics"][0].lower() != t0:
                continue
            blk = rpc._log_block_num(l)
            if blk > cutoff:
                dropped += 1
                continue
            li = l["logIndex"]
            out.append({"block": blk, "idx": int(li, 16) if isinstance(li, str) else int(li),
                        "tx": l["transactionHash"].lower(), "address": l["address"].lower(),
                        "topics": tuple(t.lower() for t in l["topics"]), "data": l.get("data", "0x")})
    need(all(e["block"] <= cutoff for e in out), f"{directory}: event above cutoff {cutoff}")
    return out, binding, {**proof, "dropped_above_cutoff": dropped, "binding_endpoint": endpoint,
                          "binding_shape": want["shape"], "binding_sha256": store.binding_sha256}


def dedupe(*groups, what):
    """Union by (tx, logIndex); identical duplicates collapse, conflicting content is fatal."""
    seen, dup = {}, 0
    for g in groups:
        for e in g:
            k = (e["tx"], e["idx"])
            if k in seen:
                need(seen[k] == e, f"{what}: conflicting duplicate log {k}")
                dup += 1
            else:
                seen[k] = e
    return list(seen.values()), dup


# ------------------------------------------------------------------ csv inputs

def read_csv(path):
    need_regular(path, "input csv")
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def load_membership(path):
    rows = read_csv(path)
    need(rows and {"address", "arm", "matched"} <= set(rows[0]), f"{path}: needs address, arm, matched")
    eligible, matched = set(), set()
    for r in rows:
        a = r["address"]
        need(a == a.lower() and a.startswith("0x") and len(a) == 42, f"{path}: bad address {a!r}")
        need(r["arm"] in ("exposed", "control"), f"{path}: unknown arm {r['arm']!r}")
        need(r["matched"] in ("TRUE", "FALSE"), f"{path}: matched must be TRUE/FALSE")
        if r["arm"] == "exposed":
            need(a not in eligible, f"{path}: duplicate exposed address")
            eligible.add(a)
            if r["matched"] == "TRUE":
                matched.add(a)
    return eligible, matched


def load_cohort(path, sha):
    need_regular(path, "cohort")
    got = sha256_file(path)
    need(sha is None or got == sha, f"cohort sha256 {got} != {sha}")
    rows = read_csv(path)
    return {r["address"].lower(): r["arm"] for r in rows}, got


# ------------------------------------------------------------------ join

def join(deposits, notes, note_emitters):
    """-> (depositor of each resolved deposit as {(tx, idx): addr}, stats). Fail-closed on the §3 conditions."""
    dep_tx, note_tx = defaultdict(list), defaultdict(list)
    for d in deposits:
        dep_tx[(d["block"], d["tx"])].append(d)
    for n in notes:
        if (n["block"], n["tx"]) in dep_tx:
            note_tx[(n["block"], n["tx"])].append(n)
    who, residual = {}, Counter()
    matched = 0
    for k, ds in dep_tx.items():
        ns = note_tx.get(k)
        if not ns:
            for d in ds:
                residual[d["address"]] += 1
            continue
        senders = {"0x" + n["topics"][1][-40:] for n in ns}
        need(len(senders) == 1, f"tx {k[1]}: {len(senders)} distinct note senders")
        need(len(ns) == len(ds), f"tx {k[1]}: {len(ns)} notes != {len(ds)} Deposit logs")
        bad = {n["address"] for n in ns} - note_emitters
        need(not bad, f"tx {k[1]}: note emitter(s) {sorted(bad)} outside the pinned set")
        (s,) = senders
        for d in ds:
            who[(d["tx"], d["idx"])] = (s, d["address"])
        matched += len(ds)
    return who, {"deposit_logs": len(deposits), "matched": matched, "residual": sum(residual.values()),
                 "residual_by_pool": dict(residual), "note_logs": len(notes),
                 "note_logs_on_deposit_txs": sum(len(v) for v in note_tx.values())}


# ------------------------------------------------------------------ producer

def produce(a, pools, **kw):
    """a: dict of paths; pools: the expected designated-pool addresses (g1._load_designated_pools()).
    Returns {'deposit_counts': rows, 'usdc_pool_flag': rows, 'receipt': dict}. Writes nothing. Malformed inputs -> H2Error."""
    try:
        return _produce(a, pools, **kw)
    except (KeyError, IndexError, ValueError, TypeError, AttributeError, csv.Error, OSError) as e:
        raise H2Error(f"malformed input: {type(e).__name__}: {e}") from e


def _produce(a, pools, cutoff=CUTOFF, spans=None, oracles=ORACLES, cohort_sha=COHORT_SHA256, note_emitters=NOTE_EMITTERS,
             usdc_pools=USDC_POOLS, n_pools=N_POOLS):
    pinned = blocks.load_pinned()
    if spans is None:
        need(cutoff == pinned["pre_period_end"]["block"], "cutoff != pinned pre_period_end")
        spans = {"junef": (pinned["census_start"]["block"], pinned["june_end"]["block"]), "s22": (15_000_000, cutoff)}
    cohort, cohort_hash = load_cohort(a["cohort"], cohort_sha)
    eligible, matched_exp = load_membership(a["match_membership"])
    need(matched_exp <= eligible, "matched exposed not a subset of eligible exposed")
    if oracles:
        need(len(matched_exp) == oracles["n_matched_exposed"] and len(eligible) == oracles["n_eligible_exposed"],
             f"membership sizes {len(matched_exp)}/{len(eligible)}")
    if a.get("eligible_frame_coverage"):
        retained = {r["address"] for r in read_csv(a["eligible_frame_coverage"]) if r["screen_status"] == "retained"}
        need(eligible <= retained, "eligible exposed wallets missing from eligible_frame_coverage retained set")

    D, N = g1.DEPOSIT_TOPIC, g1.ENCRYPTED_NOTE_TOPIC
    pools = sorted(p.lower() for p in pools)
    need(len(pools) == n_pools and len(set(pools)) == n_pools, f"designated pools != {n_pools} distinct addresses")
    W = g1.WITHDRAWAL_TOPIC
    E = EXPECTED_ENDPOINT
    d1, _, p_d1 = read_store(a["junef_deposit"], *spans["junef"], D, cutoff, (E["junef_deposit"], pools, [[D]]))
    d2, _, p_d2 = read_store(a["s22_deposit"], *spans["s22"], D, cutoff, (E["s22_deposit"], pools, [[D]]))
    n1, _, p_n1 = read_store(a["junef_note"], *spans["junef"], N, cutoff, (E["junef_note"], None, [[W, N]]))
    n2, _, p_n2 = read_store(a["s22_note"], *spans["s22"], N, cutoff, (E["s22_note"], None, [[N]]))
    pools = set(pools)
    need(set(usdc_pools) <= pools, "USDC pools not among the designated pools")
    deposits, dup_d = dedupe(d1, d2, what="Deposit")
    notes, dup_n = dedupe(n1, n2, what="EncryptedNote")
    need(all(d["address"] in pools for d in deposits), "Deposit log from a non-designated pool")
    need(all(e["block"] <= cutoff for e in deposits + notes), "event above cutoff")

    who, st = join(deposits, notes, note_emitters)
    if oracles:
        for k, ok in (("deposit_logs", "deposit_logs"), ("matched", "matched"), ("residual", "residual")):
            need(st[k] == oracles[ok], f"oracle {k}: {st[k]} != {oracles[ok]}")
    depositors = {s for s, _ in who.values()}
    arms = {"deposit", "both"}
    got = {x for x in depositors if x in cohort}
    want = {x for x, arm in cohort.items() if arm in arms}
    need(got == want, f"depositor set ∩ cohort ({len(got)}) != cohort arm in {sorted(arms)} ({len(want)})")

    cnt, usdc = Counter(), set()
    for s, pool in who.values():
        cnt[s] += 1
        if pool in usdc_pools:
            usdc.add(s)
    rows_c = [(x, cnt.get(x, 0)) for x in sorted(matched_exp)]
    rows_f = [(x, "TRUE" if x in usdc else "FALSE") for x in sorted(eligible)]
    # coverage (fail-closed, on the rows actually written)
    need({r[0] for r in rows_c} == matched_exp and len(rows_c) == len(matched_exp), "deposit_counts coverage != matched exposed set")
    need(all(isinstance(r[1], int) and r[1] >= 0 for r in rows_c), "deposit_count not a non-negative integer")
    need({r[0] for r in rows_f} == eligible and len(rows_f) == len(eligible), "usdc_pool_flag coverage != eligible exposed set")
    need(all(r[1] in ("TRUE", "FALSE") for r in rows_f), "usdc_pool_depositor not TRUE/FALSE")

    vals = [r[1] for r in rows_c]
    q1, q2 = quantile7(vals, 1 / 3), quantile7(vals, 2 / 3)
    res = {"deposit_logs": st["deposit_logs"], "matched": st["matched"], "residual": st["residual"],
           "note_logs": st["note_logs"], "note_logs_on_deposit_txs": st["note_logs_on_deposit_txs"],
           "duplicates_collapsed": {"deposit": dup_d, "encryptednote": dup_n},
           "n_matched_exposed": len(matched_exp), "n_eligible_exposed": len(eligible),
           "q1": q1, "q2": q2, "flagged": sum(1 for r in rows_f if r[1] == "TRUE"),
           "zero": sum(1 for v in vals if v == 0),
           "unresolved_usdc": {p: st["residual_by_pool"].get(p, 0) for p in usdc_pools},
           "depositor_set_size": len(depositors), "depositors_in_cohort": len(got)}
    if oracles:
        for k in ("q1", "q2", "flagged", "zero", "unresolved_usdc"):
            need(res[k] == oracles[k], f"oracle {k}: {res[k]} != {oracles[k]}")
    receipt = {"cutoff_block": cutoff, "no_rpc": True, "oracles_enforced": bool(oracles),
               "pinned_oracles": oracles if oracles else None, "pinned_cohort_sha256": cohort_sha, "designated_pools": sorted(pools),
               "expected_endpoints": dict(EXPECTED_ENDPOINT), "cohort_sha256": cohort_hash,
               "input_sha256": {k: sha256_file(a[k]) for k in ("match_membership", "eligible_frame_coverage") if a.get(k)},
               "chunk_proofs": {"junef_deposit": p_d1, "s22_deposit": p_d2, "junef_note": p_n1, "s22_note": p_n2},
               "usdc_pools": list(usdc_pools), "note_emitters": sorted(note_emitters), "results": res,
               "quantile_rule": "type-7, one-third and two-thirds, over deposit_count of matched exposed wallets"}
    return {"deposit_counts": rows_c, "usdc_pool_flag": rows_f, "receipt": receipt}


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_fsync(path, text, **kw):
    with open(path, "w", **kw) as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def write_outputs(out, outdir):
    """os.mkdir(outdir) is the no-replace existence lock; files are written + fsynced in a temp sibling and renamed over the
    (still empty) locked directory; directory + parent fsynced. Any failure removes both. outdir must not exist."""
    outdir = os.path.abspath(outdir)
    parent = os.path.dirname(outdir)
    os.makedirs(parent, exist_ok=True)
    try:
        os.mkdir(outdir)
    except FileExistsError:
        raise H2Error(f"--outdir {outdir} already exists (must be new)") from None
    tmp = tempfile.mkdtemp(prefix=".h2-inputs-", dir=parent)
    try:
        for name, header, rows in (("deposit_counts.csv", ("address", "deposit_count"), out["deposit_counts"]),
                                   ("usdc_pool_flag.csv", ("address", "usdc_pool_depositor"), out["usdc_pool_flag"])):
            with open(os.path.join(tmp, name), "w", newline="") as f:
                w = csv.writer(f, lineterminator="\n")
                w.writerow(header)
                w.writerows(rows)
                f.flush()
                os.fsync(f.fileno())
        r = out["receipt"]
        r["output_sha256"] = {n: sha256_file(os.path.join(tmp, n)) for n in ("deposit_counts.csv", "usdc_pool_flag.csv")}
        _write_fsync(os.path.join(tmp, "h2_inputs_receipt.json"), json.dumps(r, indent=2, sort_keys=True) + "\n")
        s = r["results"]
        md = ["# H2 inputs receipt", "", f"Cut at block <= {r['cutoff_block']}; no RPC. Cohort sha256 `{r['cohort_sha256']}`. Oracles enforced: {r['oracles_enforced']}.", "",
              "| quantity | value |", "|---|---|"]
        md += [f"| {k} | {v} |" for k, v in s.items()]
        md += ["", "## Input sha256", ""] + [f"- {k}: `{v}`" for k, v in r["input_sha256"].items()]
        md += ["", "## Store bindings (expected endpoint / sha256)", ""] + [
            f"- {k}: {v['binding_endpoint']} `{v['binding_sha256']}`" for k, v in r["chunk_proofs"].items()]
        md += ["", "## Output sha256", ""] + [f"- {k}: `{v}`" for k, v in r["output_sha256"].items()]
        _write_fsync(os.path.join(tmp, "h2_inputs_summary.md"), "\n".join(md) + "\n")
        _fsync_dir(tmp)
        os.rename(tmp, outdir)   # atomically replaces the empty locked directory
        _fsync_dir(parent)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(outdir, ignore_errors=True)
        raise


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--outdir", required=True, help="NEW directory")
    p.add_argument("--cohort", required=True, help="g1 cohort.csv (sha256 pinned)")
    p.add_argument("--match-membership", required=True)
    p.add_argument("--eligible-frame-coverage", required=True)
    p.add_argument("--junef-deposit", required=True, help="census_chunks/sweep_b")
    p.add_argument("--junef-note", required=True, help="census_chunks/sweep_a")
    p.add_argument("--s22-deposit", required=True, help="s22_reconcile chunks/deposit")
    p.add_argument("--s22-note", required=True, help="s22_reconcile chunks/encryptednote")
    p.add_argument("--g0-dir", default=None, help="dir holding the designated-pool manifest.csv (default: g1's out/g0)")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    a = {k: getattr(args, k) for k in ("cohort", "match_membership", "eligible_frame_coverage", "junef_deposit",
                                       "junef_note", "s22_deposit", "s22_note")}
    try:
        need(not os.path.lexists(args.outdir), f"--outdir {args.outdir} already exists (must be new)")
        g0 = args.g0_dir
        old = g1._G0_DIR
        if g0:
            g1._G0_DIR = g0
        try:
            pools = g1._load_designated_pools()
        except Exception as e:   # missing/malformed manifest = refusal, not a traceback
            raise H2Error(f"designated pools unreadable: {e}") from e
        finally:
            g1._G0_DIR = old
        out = produce(a, pools)
        write_outputs(out, args.outdir)
    except H2Error as e:
        print(f"h2_inputs REFUSED (nothing written): {e}")
        return 3
    print(f"[h2_inputs] wrote {os.path.abspath(args.outdir)}")
    print(json.dumps(out["receipt"]["results"], sort_keys=True))
    print(json.dumps(out["receipt"]["output_sha256"], sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
