"""Gate g5_veto -- the separate §5.9 veto walk (S59). NOT part of g5's main panel run.

Why separate (tribunal 2026-10-26 findings H, I): g0's cross-provider reverify never covered the Circle / USDT freeze ranges, so the
list R derives from the g0 lifetime freeze files needs an independent check; and putting freeze units inside g5's main run would let one
freeze-range mismatch void the whole S11 panel (g5's reverify is all-or-nothing). This entrypoint walks ONLY Circle `Blacklisted` (USDC)
and Tether `AddedBlackList` (USDT) logs over (pre_end, post_end] -- ~60 requests -- with its own receipts, its own full re-verification on
the other endpoint, its own run lock and endpoint attestation.

It can only VETO: R (`R/later_freeze.R`) removes from the g0-derived §5.9 drop list the wallets this walk does not confirm; a wallet that
appears only here is reported as a discrepancy and is never added. R recomputes everything from the raw logs written here (it never trusts a
summary or an "agree" flag) and asserts the walk's ranges tile (pre_end, post_end] exactly.

Same pre-access discipline as g5: before ANY RPC the committed prespec/ signature, the signed executable config, the signed code manifest
(ALL_CODE_FILES: Python AND R halves, incl. this file and R/later_freeze.R), the R-half checks and every signed post input hash are verified
(`g5_post.verify_signature` / `verify_post_inputs`); a failure raises with `.pre_access = True` and the port is never touched. Nothing here
reads `latest`; every request is guarded to (pre_end, post_end]. This module must not be run before signing (the post window is post-cutoff).

Output (a NEW/empty dir, atomically renamed into place): veto_logs.csv (raw logs, one row per log), receipts.jsonl (one getLogs receipt per
fetched range: v2 digest for the cross-endpoint comparison + `rows_sha256`, the digest R can recompute), reverify.jsonl (every range
re-fetched on the other endpoint), veto_manifest.json (sha256 of the three files, signed-file hashes, producing commit, endpoint attestation).
"""
import csv
import hashlib
import json
import os
import shutil
import time

from gates import g0_manifest as g0
from gates import g1_cohort as g1
from gates import g4_controls as g4
from gates import g4_june
from gates import g5_post as g5
from lib import rpc

GATE = "g5_veto"
SCHEMA_VERSION = 1
LOGS_NAME, MANIFEST_NAME = "veto_logs.csv", "veto_manifest.json"
RECEIPTS_NAME, REVERIFY_NAME = g5.RECEIPTS_NAME, g5.REVERIFY_NAME
# MIRROR of R/later_freeze.R LF_UNITS (tests/test_g5_veto.py diffs them): unit -> (emitting contract, topic0)
UNITS = {"circle_blacklisted": (g1.USDC.lower(), g0.BLACKLISTED_TOPIC.lower()),
         "tether_added_blacklist": (g1.USDT.lower(), g0.ADDED_BLACKLIST_TOPIC.lower())}
LOG_COLS = ("unit", "block", "log_index", "tx_hash", "block_hash", "address", "topics", "data", "block_timestamp")
CHAIN_ID = 1


class VetoError(AssertionError):
    pass


# --- canonical row text: the digest R recomputes (R/later_freeze.R lf_rows_sha) ---------------------------------------------------

def log_row(unit, log):
    """One raw log -> the nine canonical text fields of LOG_COLS."""
    return (unit, str(int(log["blockNumber"], 16)), str(int(log["logIndex"], 16)), str(log["transactionHash"]).lower(),
            str(log["blockHash"]).lower(), str(log["address"]).lower(), "|".join(str(t).lower() for t in log["topics"]),
            str(log["data"]).lower(), str(int(log["blockTimestamp"], 16)))


def rows_sha256(rows):
    """sha256 over the rows sorted by (block, log_index), each '|'-joined, lines '\\n'-joined with a trailing '\\n' (empty set -> sha256 of
    one newline). R: ps_sha256_text(lines)."""
    lines = ["|".join(r) for r in sorted(rows, key=lambda r: (int(r[1]), int(r[2])))]
    return hashlib.sha256(("\n".join(lines) + "\n").encode()).hexdigest()


def check_logs(unit, logs, s, e):
    """The endpoint's logs for one unit must be this unit's event, from its contract, inside [s, e], decodable and unique."""
    token, topic = UNITS[unit]
    seen = set()
    g4.check_block_timestamps(logs)
    for l in logs:
        if str(l.get("address", "")).lower() != token:
            raise VetoError(f"{unit}: log from {l.get('address')}, expected {token}")
        topics = l.get("topics") or []
        if not topics or str(topics[0]).lower() != topic:
            raise VetoError(f"{unit}: unexpected topic0 in tx={l.get('transactionHash')}")
        blk = int(l["blockNumber"], 16)
        if not (s <= blk <= e):
            raise VetoError(f"{unit}: log block {blk} outside [{s}, {e}]")
        k = (l.get("blockHash"), l.get("logIndex"))
        if k[0] is None or k[1] is None or k in seen:
            raise VetoError(f"{unit}: missing or duplicate (blockHash, logIndex) {k}")
        seen.add(k)
        addr, _ = g0._decode_address_event(l, has_uint=False)
        if not g5._ADDR_RE.fullmatch(addr):
            raise VetoError(f"{unit}: cannot decode a wallet address from tx={l.get('transactionHash')}")


# --- guarded port (live implementation below; tests inject a fake with the same four methods) ------------------------------------

class GuardedVetoPort:
    """No request leaves (pre_end, post_end]; raises SpanError BEFORE delegating."""

    def __init__(self, port, pre_end, post_end):
        self.port, self.pre, self.post = port, pre_end, post_end

    def _range(self, s, e):
        if not (self.pre < s <= e <= self.post):
            raise g5.SpanError(f"request [{s},{e}] outside ({self.pre}, {self.post}]")

    def assert_chain(self):
        return self.port.assert_chain()

    def walk(self, unit, s, e):
        self._range(s, e)
        return self.port.walk(unit, s, e)

    def refetch(self, endpoint, unit, s, e):
        self._range(s, e)
        return self.port.refetch(endpoint, unit, s, e)


class LiveVetoRpc:
    def __init__(self, post_end):
        self.post_end = post_end

    def assert_chain(self):
        for ep in sorted({g4.WALK, g4.REPLAY_ENDPOINT[g4.WALK]}):
            rpc.assert_chain_id(ep)

    def walk(self, unit, s, e):
        token, topic = UNITS[unit]
        return g4._walk_fetch(token, [topic], s, e, self.post_end)

    def refetch(self, endpoint, unit, s, e):
        token, topic = UNITS[unit]
        return g4._replay(endpoint, token, [topic], s, e, self.post_end)


# --- run ----------------------------------------------------------------------------------------------------------------------------

def make_binding(verified):
    """Identity of a veto work dir: every producer source file (the g5 set, which includes this file), the signed config and signed file
    hashes, the pinned config and the effective endpoints. A g5 main-run work dir carries another binding and is refused."""
    b = g5.make_binding(verified)
    return {**b, "gate": GATE, "replay_endpoint": g4.REPLAY_ENDPOINT[g4.WALK]}


def _append(path, rec):
    with open(path, "a") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _check_outdir(outdir, work):
    out, wk = os.path.realpath(outdir), os.path.realpath(work)
    if os.path.islink(outdir.rstrip("/")):
        raise g5.PreAccessDenied(f"{outdir} is a symlink")
    if out == wk or out.startswith(wk + os.sep) or wk.startswith(out + os.sep):
        raise g5.PreAccessDenied("the veto output dir and its receipts/work dir (G4_OUT_DIR) must be disjoint")
    if os.path.exists(outdir) and (not os.path.isdir(outdir) or os.listdir(outdir)):
        raise g5.PreAccessDenied(f"{outdir} exists and is not empty (veto generations are immutable)")


def run(config_path, outdir, port=None, work_dir=None):
    """Verify signature -> config -> signed inputs (all RPC-free), then walk, re-verify, publish. port=None is the live run (LiveVetoRpc,
    work dir = G4_OUT_DIR); tests inject a fake port (+ work_dir). Failures before any RPC carry `.pre_access = True` (exit 3)."""
    live = port is None
    try:
        if live:
            stale = [k for k in g4_june._stale_g4_env() if k != "G4_OUT_DIR"]
            if stale:
                raise g5.PreAccessDenied(f"environment {', '.join(stale)} must be unset for a g5_veto run (no env overrides of the g4 helpers)")
        verified = g5.verify_signature(g5._read_bytes(config_path))
        cfg = verified.cfg
        g5.verify_post_inputs(verified)
        pre, post = cfg["pre_end_block"], cfg["post_end_block"]
        work = g4._OUT_DIR if live else work_dir
        if work is None:
            raise g5.PreAccessDenied("no work dir")
        if live:
            g5.check_receipts_dir(outdir)
        _check_outdir(outdir, work)
        binding = make_binding(verified)
    except (g5.SignatureError, g5.InputMismatch, g5.SpanError) as e:
        e.pre_access = True
        raise
    except g5.PreAccessDenied:
        raise
    except Exception as e:
        raise g5.PreAccessDenied(f"verification failed before any data access: {type(e).__name__}: {e}") from e
    lock = None
    try:
        os.makedirs(work, exist_ok=True)
        lock = g4_june.acquire_run_lock(work)        # before bind_work_dir: a second run on the same work dir is refused (exit 3)
        g5.bind_work_dir(work, binding)
        rec_path, rv_path = os.path.join(work, RECEIPTS_NAME), os.path.join(work, REVERIFY_NAME)
        if os.path.exists(rec_path) or os.path.exists(rv_path) or os.path.exists(os.path.join(work, g5.CHECKPOINT_NAME)):
            raise g5.PreAccessDenied(f"{work} holds an earlier run's receipts/checkpoint; the veto walk (~60 requests) has no resume -- move it aside")
    except Exception as e:
        if lock is not None:
            lock.close()
        if isinstance(e, g5.PreAccessDenied):
            raise
        raise g5.PreAccessDenied(f"work-dir setup failed before any RPC: {type(e).__name__}: {e}") from e
    try:
        g = GuardedVetoPort(LiveVetoRpc(post) if live else port, pre, post)
        g.assert_chain()                              # first RPC
        rows = []                                    # all raw log rows
        receipts = []
        for unit in UNITS:
            logs, ranges = g.walk(unit, pre + 1, post)
            g4._assert_tiles([(a, b) for a, b, *_ in ranges], pre + 1, post)
            check_logs(unit, logs, pre + 1, post)
            urows = [log_row(unit, l) for l in logs]
            for a, b, cnt, dig, ep in sorted(ranges):
                sub = [r for r in urows if a <= int(r[1]) <= b]
                if len(sub) != cnt:
                    raise VetoError(f"{unit} [{a},{b}]: receipt count {cnt} != {len(sub)} logs in the range")
                rec = {"gate": GATE, "kind": "getLogs", "unit": unit, "endpoint": ep, "fromBlock": a, "toBlock": b,
                       "logCount": cnt, "sha256": dig, "rows_sha256": rows_sha256(sub), "t": time.time()}
                _append(rec_path, rec)
                receipts.append(rec)
            if len(urows) != sum(r["logCount"] for r in receipts if r["unit"] == unit):
                raise VetoError(f"{unit}: logs outside every receipted range")
            rows.extend(urows)
        replay = g4.REPLAY_ENDPOINT[g4.WALK]
        for r in receipts:                            # FULL re-verification (small walk): every range on the OTHER endpoint
            other = g4.REPLAY_ENDPOINT.get(r["endpoint"])
            if other is None or other == r["endpoint"]:
                raise g5.ReverifyError(f"no independent second endpoint for {r['endpoint']!r}")
            if not str(r["sha256"]).startswith("v2:"):
                raise g5.ReverifyError(f"{r['unit']} [{r['fromBlock']},{r['toBlock']}] carries a legacy digest")
            alt = g.refetch(other, r["unit"], r["fromBlock"], r["toBlock"])
            check_logs(r["unit"], alt, r["fromBlock"], r["toBlock"])
            dig, arows = g4._digest(alt, with_block=True), [log_row(r["unit"], l) for l in alt]
            if len(alt) != r["logCount"] or dig != r["sha256"] or rows_sha256(arows) != r["rows_sha256"]:
                raise g5.ReverifyError(f"veto reverify mismatch {r['unit']} [{r['fromBlock']},{r['toBlock']}]: {r['endpoint']} count={r['logCount']} "
                                       f"{other} count={len(alt)} (digest equal: {dig == r['sha256']})")
            _append(rv_path, {"gate": GATE, "kind": "reverify", "unit": r["unit"], "endpoint": other, "source": r["endpoint"],
                              "fromBlock": r["fromBlock"], "toBlock": r["toBlock"], "logCount": len(alt), "sha256": dig,
                              "rows_sha256": rows_sha256(arows), "t": time.time()})
        walk_ep = g4.WALK
        eps = {r["endpoint"] for r in receipts}
        if eps != {walk_ep}:
            raise g5.ReverifyError(f"acquisition endpoints {sorted(eps)} != the single walk endpoint {walk_ep!r}; refusing to publish")
        return publish(outdir, rows, rec_path, rv_path, verified, pre, post, walk_ep, replay)
    finally:
        lock.close()


def publish(outdir, rows, rec_path, rv_path, verified, pre, post, walk_ep, replay_ep):
    outdir = os.path.normpath(outdir)
    tmp = f"{outdir}.tmp-{os.getpid()}"
    os.makedirs(tmp)
    with open(os.path.join(tmp, LOGS_NAME), "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(LOG_COLS)
        for r in sorted(rows, key=lambda r: (r[0], int(r[1]), int(r[2]))):
            w.writerow(r)
    shutil.copyfile(rec_path, os.path.join(tmp, RECEIPTS_NAME))
    shutil.copyfile(rv_path, os.path.join(tmp, REVERIFY_NAME))
    files = {n: g5._sha256_bytes(g5._read_bytes(os.path.join(tmp, n))) for n in (LOGS_NAME, RECEIPTS_NAME, REVERIFY_NAME)}
    with open(os.path.join(tmp, MANIFEST_NAME), "w") as f:
        json.dump({"schema_version": SCHEMA_VERSION, "gate": GATE, "pre_end_block": pre, "post_end_block": post,
                   "units": {u: {"address": a, "topic0": t} for u, (a, t) in UNITS.items()},
                   "files": files, "row_count": len(rows),
                   "signed_file_sha256": verified.signed_hashes, "producing_commit": g4._git_commit(),
                   "walk_endpoint": walk_ep, "replay_endpoint": replay_ep, "chain_id": CHAIN_ID,
                   "chain_asserted_endpoints": sorted({walk_ep, replay_ep})}, f, indent=2, sort_keys=True)
        f.write("\n")
    if os.path.isdir(outdir):
        os.rmdir(outdir)          # empty (checked before the walk)
    os.rename(tmp, outdir)
    return os.path.join(outdir, MANIFEST_NAME), len(rows), files[LOGS_NAME]
