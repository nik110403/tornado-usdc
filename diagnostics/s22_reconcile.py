#!/usr/bin/env python3
"""S22 reconciliation diagnostic (read-mostly; edits no producer file, reruns no gate).

Shows by SET EQUALITY against an independent acquisition that S22 g1's eligibility sets are complete
(tribunal 2026-10-26-s22-g1-floor, "Adopted plan" step 1):

  a1/a2  raw withdrawal-recipient set R and depositor set D, rebuilt from the S21 June census chunks
         (<= june_end) UNION a fresh single-provider fetch of [FETCH_LO, pre_period_end], equal to the S22
         eligibility_screen.csv arms (+ 0x0 when 0x0 is in the rebuilt set)
  a3     per-topic log counts of the fetch == the S22 receipts for the same span (receipts streamed)
  a4     S21 chunks and the fetch agree on the overlap [FETCH_LO, june_end]
  a5     full-span totals (S21 union fetch) == the S22 summary (W/D/N logs, matched deposits, rows)
  b      sum of nextIndex() over the designated pools at pre_period_end == S22 designated-pool Deposit logs
  c      g1b's 30 frame windows recomputed (g1b_frame.original_windows), Transfer logs fetched, compared with g1b
  d      g1b exclusion.csv is a superset of S22 screen U S21 all-instance sets U the fetched Tornado set

Locked rules honoured: every query pins toBlock from config/blocks.json (and is refused above pre_period_end),
chain id 1 asserted, topics computed by lib/keccak via the gates, stdlib only, window <= lib.rpc.shape_cap, the
whole span is always requested (never shrunk), ONE provider with fallback disabled (a failed range raises).
S21 chunks are read through lib.rpc.ChunkStore with a read-only subclass (no attempts.jsonl write).
Exit: 0 = no check failed (PENDING allowed), 1 = a check FAILED, 2 = BLOCKED (incomplete run / bad input).
"""
import argparse
import csv
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from collections import Counter

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from gates import g1_cohort as g1  # noqa: E402
from gates import g1b_frame  # noqa: E402
from lib import blocks, keccak, rpc  # noqa: E402

PASS, FAIL, PENDING = "PASS", "FAIL", "PENDING"
FETCH_LO = 15_000_000          # S22's last receipt range starts here (one range to the cutoff): counts compare 1:1
ZERO = "0x" + "0" * 40
W_TOPIC, D_TOPIC, N_TOPIC, T_TOPIC = (g1.WITHDRAWAL_TOPIC, g1.DEPOSIT_TOPIC, g1.ENCRYPTED_NOTE_TOPIC,
                                      g1.TRANSFER_TOPIC)
NEXT_INDEX_SELECTOR = keccak.selector("nextIndex()")  # computed, never copied from a document
USDC_USDT = [g1.USDC, g1.USDT]
COOLDOWNS = None               # None = lib.rpc.QUOTA_COOLDOWNS; tests set ()
FIRST_N = 20


# --------------------------------------------------------------------------- helpers

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def diff_sets(left, right, left_name, right_name):
    lo, ro = sorted(left - right), sorted(right - left)
    return {"equal": not lo and not ro, f"{left_name}_size": len(left), f"{right_name}_size": len(right),
            f"{left_name}_only": len(lo), f"{right_name}_only": len(ro),
            f"{left_name}_only_first": lo[:FIRST_N], f"{right_name}_only_first": ro[:FIRST_N]}


def stream_receipts(path, hasher=None):
    """Yield one parsed receipt per line. Never loads the file: the S22 receipts are ~900 MB."""
    with open(path, "rb") as f:
        for line in f:
            if hasher is not None:
                hasher.update(line)
            if line.strip():
                yield json.loads(line)


def scan_receipts(path, rules):
    """One streaming pass. rules: {topics_json: (label, endpoint, {addressCount,...}, gate)}.
    Returns {"ranges": {label: [(from, to, logCount)]}, "lines": n, "sha256": hex}."""
    h, ranges, n = hashlib.sha256(), {}, 0
    for rec in stream_receipts(path, h):
        n += 1
        rule = rules.get(json.dumps(rec.get("topics")))
        if rule is None:
            continue
        label, endpoint, counts, gate = rule
        if rec.get("endpoint") != endpoint or rec.get("addressCount") not in counts:
            continue
        if gate is not None and rec.get("gate") != gate:
            continue
        ranges.setdefault(label, []).append((rec["fromBlock"], rec["toBlock"], rec["logCount"]))
    return {"ranges": ranges, "lines": n, "sha256": h.hexdigest()}


def span_sum(ranges, lo, hi):
    """(sum of logCount, ok, msg) over receipts lying inside [lo, hi]; they must tile it exactly (a receipt
    straddling a bound is not counted and breaks the tiling)."""
    rs = sorted(r for r in ranges if r[0] >= lo and r[1] <= hi)
    if not rs:
        return None, False, f"no receipt overlaps [{lo},{hi}]"
    cursor = lo
    for a, b, _ in rs:
        if a != cursor:
            return None, False, f"receipts do not tile [{lo},{hi}] at {cursor} (next range starts {a})"
        cursor = b + 1
    if cursor - 1 != hi:
        return None, False, f"receipts end at {cursor - 1}, expected {hi}"
    return sum(r[2] for r in rs), True, "tiled"


def load_pools(g0_dir):
    """Designated pools exactly as g1 loads them (g1._load_designated_pools), pointed at the S22 g0 dir."""
    old = g1._G0_DIR
    g1._G0_DIR = g0_dir
    try:
        return g1._load_designated_pools()
    finally:
        g1._G0_DIR = old


def load_screen(path):
    arms = {}
    with open(path, newline="") as f:
        rd = csv.DictReader(f)
        if not rd.fieldnames or "address" not in rd.fieldnames or "arm" not in rd.fieldnames:
            raise ValueError(f"{path}: expected columns address,arm; got {rd.fieldnames}")
        for row in rd:
            arms[row["address"].lower()] = row["arm"]
    bad = set(arms.values()) - {"withdrawal", "deposit", "both"}
    if bad:
        raise ValueError(f"{path}: unknown arm values {sorted(bad)}")
    return arms


def parse_s22_summary(path):
    with open(path) as f:
        t = f.read()
    m = re.search(r"Withdrawal arm: (\d+) logs, (\d+) unique recipients", t)
    m2 = re.search(r"Deposit arm: (\d+) designated-pool Deposit logs, (\d+) EncryptedNote logs \(all instances\), "
                   r"(\d+) matched", t)
    if not m or not m2:
        raise ValueError(f"{path}: cannot parse the Withdrawal/Deposit arm lines")
    return {"withdrawal_logs": int(m.group(1)), "unique_recipients": int(m.group(2)),
            "deposit_logs": int(m2.group(1)), "note_logs": int(m2.group(2)), "matched_deposits": int(m2.group(3))}


def parse_g1b_summary(path):
    with open(path) as f:
        t = f.read()
    rows = {}
    for m in re.finditer(r"^\| (\d+) \| (\d+)-(\d+) \| (\d+) \| (\d+) \|\s*$", t, re.M):
        rows[(int(m.group(2)), int(m.group(3)))] = int(m.group(4))
    dx = re.search(r"in exclusion set: (\d+)", t)
    cd = re.search(r"has code at \d+: (\d+)", t)
    return {"window_unique": rows, "dropped_exclusion": int(dx.group(1)) if dx else None,
            "dropped_code": int(cd.group(1)) if cd else None}


def read_csv_rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def git_state():
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=_ROOT, text=True).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=_ROOT, text=True).strip())
        return {"commit": head, "dirty": dirty}
    except (subprocess.CalledProcessError, OSError):
        return {"commit": "unknown", "dirty": None}


# --------------------------------------------------------------------------- log events

def _event(l, who=None):
    """(block, logIndex, txhash, address, who) -- who is the decoded recipient/depositor/None."""
    li = l["logIndex"]
    return (rpc._log_block_num(l), int(li, 16) if isinstance(li, str) else int(li),
            l["transactionHash"].lower(), l["address"].lower(), who)


def w_event(l):
    return _event(l, g1._withdrawal_recipient(l))


def n_event(l):
    return _event(l, g1._note_depositor(l))


def d_event(l):
    return _event(l)


def ekey(ev):
    return (ev[2], ev[1])


# --------------------------------------------------------------------------- S21 chunk reading

class _ReadOnlyStore(rpc.ChunkStore):
    """ChunkStore that never writes attempts.jsonl into the S21 work dir."""

    def _note(self, rec):
        pass


def read_sweep(directory, expect_address, expect_topics, lo, hi, decode, inputs, prefix):
    """Validate the binding, prove coverage of [lo, hi], and reduce every chunk to event tuples
    (one chunk in memory at a time). Returns (events, proof, binding)."""
    bpath = os.path.join(directory, "binding.json")
    if not os.path.isfile(bpath):
        raise rpc.CheckpointError(f"{directory}: binding.json missing")
    with open(bpath) as f:
        binding = json.load(f)
    q = binding.get("query", {})
    want = {"endpoint": g1b_frame.WIDE, "address": expect_address, "topics": expect_topics,
            "shape": rpc.query_shape(expect_address, expect_topics)}
    if {k: q.get(k) for k in want} != want:
        raise rpc.CheckpointError(f"{directory}: binding query {q} != expected {want}")
    for name in sorted(os.listdir(directory)):
        if name == "binding.json" or (name.startswith("chunk-") and name.endswith(".json")):
            inputs[f"{prefix}/{name}"] = sha256_file(os.path.join(directory, name))
    store = _ReadOnlyStore(directory, binding)
    proof = store.coverage_proof(lo, hi)  # every chunk valid + bound, tiles [lo, hi], no duplicate event
    events = []
    for a, b in sorted(store._index):
        events.extend(decode(l) for l in store.read_chunk(a, b)["logs"])
    return events, proof, binding


# --------------------------------------------------------------------------- network seam

class Net:
    """The only code that talks to a provider. One endpoint per call; no fallback anywhere."""

    def chain_id(self, endpoint):
        return rpc.assert_chain_id(endpoint)

    def fetch(self, endpoint, lo, hi, address, topics):
        return rpc.fetch_logs_scoped(endpoint, lo, hi, address=address, topics=topics)

    def call(self, endpoint, to, data, block, max_retries=3, backoff_s=1.0):
        last = None
        for i in range(max_retries):
            try:
                return rpc.call(endpoint, to, data, block)
            except (rpc.RpcError, OSError, TimeoutError) as e:
                last = e
                time.sleep(backoff_s * (2 ** i))
        raise rpc.GapError(f"{endpoint} eth_call {to} @ {block} failed after {max_retries} attempts: {last}")


class Recorder:
    def __init__(self, path):
        self.fh = open(path, "a")

    def write(self, rec):
        self.fh.write(json.dumps({**rec, "t": time.time()}, sort_keys=True) + "\n")
        self.fh.flush()

    def close(self):
        self.fh.close()


def fetch_query(net, outdir, name, endpoint, lo, hi, address, topics, window, pre_end, identity, input_hashes,
                rec, check):
    """One checkpointed single-provider acquisition of [lo, hi]. Returns (logs, coverage_proof)."""
    if hi > pre_end:
        raise rpc.RpcError(f"refusing to read block {hi} > pre_period_end {pre_end}")
    binding = rpc.make_binding(identity, rpc.canonical_sha256({"check": check, "lo": lo, "hi": hi}),
                               input_hashes, endpoint, address, topics)
    store = rpc.ChunkStore(os.path.join(outdir, "chunks", name), binding)
    q = binding["query"]
    window = min(window, rpc.shape_cap(endpoint, q["shape"]))

    def on_chunk(c):
        rec.write({"check": check, "endpoint": c["endpoint"], "fromBlock": c["lo"], "toBlock": c["hi"],
                   "logCount": c["logCount"], "addressCount": len(q["address"] or ()), "topics": q["topics"],
                   "sha256": c["response_sha256"], "attempt_id": c["attempt_id"]})

    def guard(h):
        if h > pre_end:
            raise rpc.RpcError(f"block {h} > pre_period_end {pre_end}")

    logs = store.acquire(lo, hi, lambda a, b: net.fetch(endpoint, a, b, q["address"], q["topics"]), window,
                         guard=guard, on_chunk=on_chunk, cooldowns=COOLDOWNS)
    return logs, store.coverage_proof(lo, hi)


# --------------------------------------------------------------------------- plan

def plan(endpoint, pools, windows, lo, hi):
    cap_multi, cap_topic = rpc.shape_cap(endpoint, "multi_address"), rpc.shape_cap(endpoint, "topic_only")
    n_multi, n_topic = math.ceil((hi - lo + 1) / cap_multi), math.ceil((hi - lo + 1) / cap_topic)
    n_frame = sum(math.ceil((e - s + 1) / cap_multi) for s, e in windows)
    a_req = 2 * n_multi + n_topic
    return {"endpoint": endpoint, "fetch_span": [lo, hi], "pools": len(pools),
            "caps": {"multi_address": cap_multi, "topic_only": cap_topic, "result_cap": rpc.result_cap(endpoint)},
            "a_getLogs": {"withdrawal": n_multi, "deposit": n_multi, "encryptednote": n_topic, "total": a_req},
            "c_getLogs": {"windows": len(windows), "min_requests": n_frame,
                          "note": "a window at/over the result cap bisects (+1 request per split)"},
            "b_eth_call": len(pools), "chain_id_calls": 1,
            "getLogs_min_total": a_req + n_frame, "eth_call_total": len(pools)}


# --------------------------------------------------------------------------- the run

def _status(ok):
    return PASS if ok else FAIL


def run(args, net=None, pinned=None, fetch_lo=None):
    net = net or Net()
    pinned = pinned if pinned is not None else blocks.load_pinned()
    fetch_lo = FETCH_LO if fetch_lo is None else fetch_lo
    endpoint = args.endpoint
    if endpoint not in rpc.ENDPOINTS:
        raise ValueError(f"unknown endpoint {endpoint!r}")
    if endpoint == g1b_frame.WIDE:
        raise ValueError(f"--endpoint {endpoint!r} is the provider S22/g1b used; the acquisition must be independent")
    if os.path.lexists(args.outdir):
        raise FileExistsError(f"--outdir {args.outdir} already exists (must be new)")
    s22_out, s21_work, g1b_out = (os.path.abspath(x) for x in (args.s22_out, args.s21_work, args.g1b_out))
    outdir = os.path.abspath(args.outdir)
    for src in (s22_out, s21_work, g1b_out):
        if outdir == src or outdir.startswith(src + os.sep):
            raise ValueError(f"--outdir {outdir} lies inside the input dir {src}")
    census_start = pinned["census_start"]["block"]
    june_end = pinned["june_end"]["block"]
    pre_end = pinned["pre_period_end"]["block"]
    assert census_start < fetch_lo <= june_end < pre_end
    p = {k: os.path.join(s22_out, "g1", k) for k in ("eligibility_screen.csv", "summary.md", "receipts.jsonl")}
    p["manifest"] = os.path.join(s22_out, "g0", "manifest.csv")
    for path in p.values():
        if not os.path.isfile(path):
            raise FileNotFoundError(f"S22 input missing: {path}")
    pools = load_pools(os.path.join(s22_out, "g0"))
    windows = g1b_frame.original_windows(pinned)
    the_plan = plan(endpoint, pools, windows, fetch_lo, pre_end)
    if args.dry_run:
        print(json.dumps(the_plan, indent=2))
        return 0, the_plan

    os.makedirs(outdir)
    rec = Recorder(os.path.join(outdir, "receipts.jsonl"))
    inputs, checks = {}, []
    try:
        cid = net.chain_id(endpoint)
        if cid != 1:
            raise rpc.RpcError(f"chain id {cid} != 1")
        rec.write({"kind": "chain_id", "endpoint": endpoint, "chain_id": cid})
        inputs["s22/g0/manifest.csv"] = sha256_file(p["manifest"])
        inputs["s22/g1/eligibility_screen.csv"] = sha256_file(p["eligibility_screen.csv"])
        inputs["s22/g1/summary.md"] = sha256_file(p["summary.md"])
        pools_set = set(pools)
        identity = rpc.producer_identity([os.path.abspath(__file__)])
        bind_inputs = {"s22_manifest": inputs["s22/g0/manifest.csv"]}

        # ---- S22 side
        screen = load_screen(p["eligibility_screen.csv"])
        s22 = parse_s22_summary(p["summary.md"])
        rules = {json.dumps([[W_TOPIC]]): ("W", "tenderly", {len(pools)}, "g1"),
                 json.dumps([[D_TOPIC]]): ("D", "tenderly", {len(pools)}, "g1"),
                 json.dumps([[N_TOPIC]]): ("N", "tenderly", {0}, "g1")}
        scan = scan_receipts(p["receipts.jsonl"], rules)
        inputs["s22/g1/receipts.jsonl"] = scan["sha256"]

        # ---- S21 chunks (<= june_end), validated through ChunkStore, reduced chunk by chunk
        wt = W_TOPIC.lower()
        a_events, proof_a, bind_a = read_sweep(
            os.path.join(s21_work, "census_chunks", "sweep_a"), None, [[W_TOPIC, N_TOPIC]], census_start, june_end,
            lambda l: (("W", w_event(l)) if l["topics"][0].lower() == wt else ("N", n_event(l))),
            inputs, "s21/sweep_a")
        s21_d, proof_b, bind_b = read_sweep(
            os.path.join(s21_work, "census_chunks", "sweep_b"), sorted(pools), [[D_TOPIC]], census_start, june_end,
            d_event, inputs, "s21/sweep_b")
        s21_w_all = [e for k, e in a_events if k == "W"]
        s21_w = [e for e in s21_w_all if e[3] in pools_set]
        s21_n = [e for k, e in a_events if k == "N"]
        meta = os.path.join(s21_work, "meta.json")
        if os.path.isfile(meta):
            inputs["s21/meta.json"] = sha256_file(meta)

        # ---- fresh fetch [fetch_lo, pre_end], one provider, fallback off
        cap_multi = rpc.shape_cap(endpoint, "multi_address")
        f_w, proof_fw = fetch_query(net, outdir, "withdrawal", endpoint, fetch_lo, pre_end, sorted(pools),
                                    [[W_TOPIC]], cap_multi, pre_end, identity, bind_inputs, rec, "a")
        f_d, proof_fd = fetch_query(net, outdir, "deposit", endpoint, fetch_lo, pre_end, sorted(pools),
                                    [[D_TOPIC]], cap_multi, pre_end, identity, bind_inputs, rec, "a")
        f_n, proof_fn = fetch_query(net, outdir, "encryptednote", endpoint, fetch_lo, pre_end, None,
                                    [[N_TOPIC]], rpc.shape_cap(endpoint, "topic_only"), pre_end, identity,
                                    bind_inputs, rec, "a")
        fw, fd, fn = [w_event(l) for l in f_w], [d_event(l) for l in f_d], [n_event(l) for l in f_n]

        # ---- (a) raw sets: union by (tx, logIndex), then g1's matching rule (first EncryptedNote per (block, tx))
        W, D_ev, N_ev = {}, {}, {}
        for evs, tgt in ((s21_w, W), (fw, W), (s21_d, D_ev), (fd, D_ev), (s21_n, N_ev), (fn, N_ev)):
            for ev in evs:
                tgt.setdefault(ekey(ev), ev)
        note_by_tx = {}
        for ev in sorted(N_ev.values(), key=lambda e: (e[0], e[1])):
            note_by_tx.setdefault((ev[0], ev[2]), ev[4])
        R = {ev[4] for ev in W.values()}
        D, residual = set(), 0
        for ev in D_ev.values():
            who = note_by_tx.get((ev[0], ev[2]))
            if who:
                D.add(who)
            else:
                residual += 1
        matched = len(D_ev) - residual
        screen_r = {a for a, arm in screen.items() if arm in ("withdrawal", "both")}
        screen_d = {a for a, arm in screen.items() if arm in ("deposit", "both")}
        zero_in_screen = ZERO in screen
        d1 = diff_sets(R, screen_r | ({ZERO} if ZERO in R else set()), "rebuilt", "screen_plus_zero")
        d1.update(zero_in_rebuilt=ZERO in R, zero_in_screen=zero_in_screen,
                  s22_summary_unique_recipients=s22["unique_recipients"], rebuilt_unique_recipients=len(R))
        checks.append({"id": "a1_recipients_set_equality", "status": _status(
            d1["equal"] and not zero_in_screen and len(R) == s22["unique_recipients"]), **d1})
        d2 = diff_sets(D, screen_d | ({ZERO} if ZERO in D else set()), "rebuilt", "screen_plus_zero")
        d2.update(zero_in_rebuilt=ZERO in D, zero_in_screen=zero_in_screen)
        checks.append({"id": "a2_depositors_set_equality", "status": _status(d2["equal"] and not zero_in_screen), **d2})

        par = {}
        for label, got in (("W", len(fw)), ("D", len(fd)), ("N", len(fn))):
            tot, ok, msg = span_sum(scan["ranges"].get(label, []), fetch_lo, pre_end)
            par[label] = {"fetched": got, "s22_receipts": tot, "receipts_tiled": ok, "note": msg,
                          "equal": ok and tot == got}
        checks.append({"id": "a3_log_count_parity", "status": _status(all(v["equal"] for v in par.values())),
                       "span": [fetch_lo, pre_end], "per_topic": par})

        ov = {}
        for label, s21e, fe in (("W", s21_w, fw), ("D", s21_d, fd), ("N", s21_n, fn)):
            left = {ekey(e): e[4] for e in s21e if fetch_lo <= e[0] <= june_end}
            right = {ekey(e): e[4] for e in fe if e[0] <= june_end}
            dk = diff_sets(set(left), set(right), "s21", "fetch")
            dk["who_mismatch"] = sum(1 for k in left.keys() & right.keys() if left[k] != right[k])
            dk["equal"] = dk["equal"] and dk["who_mismatch"] == 0
            ov[label] = dk
        checks.append({"id": "a4_overlap_agreement", "status": _status(all(v["equal"] for v in ov.values())),
                       "span": [fetch_lo, june_end], "per_topic": ov})

        tot = {"W_logs": (len(W), s22["withdrawal_logs"]), "D_logs": (len(D_ev), s22["deposit_logs"]),
               "N_logs": (len(N_ev), s22["note_logs"]), "matched_deposits": (matched, s22["matched_deposits"]),
               "screen_rows": (len((R | D) - {ZERO}), len(screen))}
        checks.append({"id": "a5_full_span_totals", "status": _status(all(a == b for a, b in tot.values())),
                       "span": [census_start, pre_end],
                       "totals": {k: {"rebuilt": a, "s22": b} for k, (a, b) in tot.items()},
                       "s21_coverage": {"sweep_a": proof_a, "sweep_b": proof_b},
                       "fetch_coverage": {"withdrawal": proof_fw, "deposit": proof_fd, "encryptednote": proof_fn}})

        # ---- (b) nextIndex at pre_period_end
        per_pool, total = {}, 0
        for pool in pools:
            res = net.call(endpoint, pool, NEXT_INDEX_SELECTOR, pre_end)
            per_pool[pool] = int(res, 16)
            total += per_pool[pool]
            rec.write({"check": "b", "kind": "eth_call", "endpoint": endpoint, "to": pool, "block": pre_end,
                       "data": NEXT_INDEX_SELECTOR, "result": res})
        r_total, r_ok, r_msg = span_sum(scan["ranges"].get("D", []), census_start, pre_end)
        checks.append({"id": "b_next_index_sum",
                       "status": _status(total == s22["deposit_logs"] and r_ok and total == r_total),
                       "block": pre_end, "sum_nextIndex": total, "s22_summary_deposit_logs": s22["deposit_logs"],
                       "s22_receipts_deposit_logs": r_total, "receipts_note": r_msg,
                       "rebuilt_deposit_logs": len(D_ev), "per_pool": per_pool})

        # ---- (c) frame windows
        win = {}
        for s, e in windows:
            logs, _ = fetch_query(net, outdir, f"frame_{s}_{e}", endpoint, s, e, USDC_USDT, [[T_TOPIC]], e - s + 1,
                                  pre_end, identity, bind_inputs, rec, "c")
            addrs, legs = set(), Counter()
            for l in logs:
                fr, to = g1b_frame._transfer_addrs(l)
                addrs.update((fr, to))
                legs[fr] += 1
                legs[to] += 1
            win[(s, e)] = {"logs": len(logs), "addrs": addrs, "legs": legs}
        fetched_union = set().union(*(w["addrs"] for w in win.values())) if win else set()
        fetched_legs = Counter()
        for w in win.values():
            fetched_legs.update(w["legs"])
        frame_detail = {f"{s}-{e}": {"logs": win[(s, e)]["logs"], "unique_addrs": len(win[(s, e)]["addrs"]),
                                     "addrs_sha256": hashlib.sha256(
                                         "\n".join(sorted(win[(s, e)]["addrs"])).encode()).hexdigest()}
                        for s, e in windows}

        g1b_files = {k: os.path.join(g1b_out, k) for k in
                     ("exclusion.csv", "frame_survivors.csv", "summary.md", "receipts.jsonl")}
        missing = sorted(k for k, v in g1b_files.items() if not os.path.isfile(v))
        excl = None
        if os.path.isfile(g1b_files["exclusion.csv"]):
            inputs["g1b/exclusion.csv"] = sha256_file(g1b_files["exclusion.csv"])
            excl = {r["address"].lower() for r in read_csv_rows(g1b_files["exclusion.csv"])}
        if missing:
            checks.append({"id": "c_frame_windows", "status": PENDING, "windows": len(windows),
                           "reason": f"g1b output not present in {g1b_out}: missing {missing}",
                           "fetched_union_addresses": len(fetched_union), "per_window_fetched": frame_detail})
        else:
            inputs["g1b/frame_survivors.csv"] = sha256_file(g1b_files["frame_survivors.csv"])
            inputs["g1b/summary.md"] = sha256_file(g1b_files["summary.md"])
            gs = parse_g1b_summary(g1b_files["summary.md"])
            grec = scan_receipts(g1b_files["receipts.jsonl"], {json.dumps([[T_TOPIC]]): ("T", "tenderly", {2}, "g1b")})
            inputs["g1b/receipts.jsonl"] = grec["sha256"]
            per_window, problems = [], []
            for s, e in windows:
                tot_g, ok, msg = span_sum(grec["ranges"].get("T", []), s, e)
                w = win[(s, e)]
                row = {"window": [s, e], "fetched_logs": w["logs"], "g1b_receipt_logs": tot_g,
                       "fetched_unique_addrs": len(w["addrs"]),
                       "g1b_summary_unique_addrs": gs["window_unique"].get((s, e)), "receipts_note": msg}
                row["ok"] = ok and tot_g == w["logs"] and gs["window_unique"].get((s, e)) == len(w["addrs"])
                per_window.append(row)
                if not row["ok"]:
                    problems.append(row["window"])
            surv = {r["address"].lower(): r for r in read_csv_rows(g1b_files["frame_survivors.csv"])}
            win_sets = [w["addrs"] for w in win.values()]
            bad = {"not_in_fetched_union": [], "in_exclusion": [], "windows_seen": [], "transfer_count": []}
            for a, r in sorted(surv.items()):
                if a not in fetched_union:
                    bad["not_in_fetched_union"].append(a)
                if a in excl:
                    bad["in_exclusion"].append(a)
                if int(r["windows_seen"]) != sum(1 for ws in win_sets if a in ws):
                    bad["windows_seen"].append(a)
                if int(r["transfer_count"]) != fetched_legs[a]:
                    bad["transfer_count"].append(a)
            dropped_excl = len(fetched_union & excl)
            residue = fetched_union - excl - set(surv)  # g1b's "has code at pre_period_end" set (not re-queried)
            counts_ok = gs["dropped_exclusion"] == dropped_excl and gs["dropped_code"] == len(residue)
            checks.append({"id": "c_frame_windows",
                           "status": _status(not problems and not any(bad.values()) and counts_ok),
                           "windows": len(windows), "windows_mismatching": problems, "per_window": per_window,
                           "survivors": len(surv),
                           "survivor_defects": {k: {"n": len(v), "first": v[:FIRST_N]} for k, v in bad.items()},
                           "dropped_in_exclusion": {"fetched": dropped_excl, "g1b_summary": gs["dropped_exclusion"]},
                           "dropped_has_code_residue": {"fetched": len(residue), "g1b_summary": gs["dropped_code"],
                                                        "note": "union minus exclusion minus survivors; code not re-queried"},
                           "fetched_union_addresses": len(fetched_union)})

        # ---- (d) exclusion closure (g1b exclusion = all-instance recipients + EncryptedNote depositors)
        s21_all = {e[4] for e in s21_w_all} | {e[4] for e in s21_n}
        fetched_tornado = {e[4] for e in fw} | {e[4] for e in fn}  # matched depositors are a subset of note depositors
        need = {"s22_screen": set(screen), "s21_all_instance": s21_all, "fetched_window": fetched_tornado}
        if excl is None:
            checks.append({"id": "d_exclusion_closure", "status": PENDING,
                           "reason": f"{g1b_files['exclusion.csv']} not present",
                           "required_sizes": {k: len(v) for k, v in need.items()}})
        else:
            union_need = set().union(*need.values())
            checks.append({"id": "d_exclusion_closure", "status": _status(not (union_need - excl)),
                           "exclusion_size": len(excl), "required_union_size": len(union_need),
                           "shortfall_total": len(union_need - excl),
                           "per_source": {k: {"size": len(v), "missing_from_exclusion": len(v - excl),
                                              "first": sorted(v - excl)[:FIRST_N]} for k, v in need.items()}})

        # ---- output
        result = {"schema_version": 1, "endpoint": endpoint, "chain_id": cid,
                  "block_bounds": {"census_start": census_start, "june_end": june_end, "pre_period_end": pre_end,
                                   "fetch_lo": fetch_lo, "fetch_hi": pre_end},
                  "pools": len(pools), "code": git_state(), "producer_identity": identity,
                  "s21_bindings": {"sweep_a": bind_a, "sweep_b": bind_b},
                  "s22_summary": s22, "s22_receipts_lines": scan["lines"], "plan": the_plan,
                  "input_sha256": dict(sorted(inputs.items())),
                  "checks": checks, "any_fail": any(c["status"] == FAIL for c in checks)}
        with open(os.path.join(outdir, "reconcile.json"), "w") as f:
            json.dump(result, f, indent=1, sort_keys=True)
            f.write("\n")
        lines = ["# S22 reconciliation", "",
                 f"endpoint `{endpoint}` (fallback disabled), chain id {cid}, fetch [{fetch_lo}, {pre_end}], "
                 f"code `{result['code']['commit']}`{' (dirty)' if result['code']['dirty'] else ''}", "",
                 "| check | status |", "|---|---|"]
        lines += [f"| {c['id']} | {c['status']} |" for c in checks]
        lines += ["", "Details and the first <=20 differing addresses per check: `reconcile.json`."]
        with open(os.path.join(outdir, "summary.md"), "w") as f:
            f.write("\n".join(lines) + "\n")
        return (1 if result["any_fail"] else 0), result
    finally:
        rec.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--s22-out", required=True, help="S22 worktree out/ dir (holds g0/ and g1/)")
    ap.add_argument("--s21-work", required=True, help="S21 june .work dir (holds census_chunks/)")
    ap.add_argument("--g1b-out", required=True,
                    help="dir with g1b exclusion.csv, frame_survivors.csv, summary.md, receipts.jsonl")
    ap.add_argument("--outdir", required=True, help="new dir (refused if it exists)")
    ap.add_argument("--endpoint", default="mevblocker")
    ap.add_argument("--dry-run", action="store_true", help="print the request plan and exit without network")
    args = ap.parse_args(argv)
    try:
        code, _ = run(args)
    except (rpc.GapError, rpc.RpcError, rpc.CheckpointError, rpc.CoverageError, OSError, ValueError,
            AssertionError) as e:
        print(f"BLOCKED: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    return code


if __name__ == "__main__":
    sys.exit(main())
