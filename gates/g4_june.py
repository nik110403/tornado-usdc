"""Gate 4 (June placebo population) -- prespec section 5 item 2 / SPEC S10c part 2.

The placebo is "rebuilt on June-only information": exposure, Tornado Cash
exclusions, frame windows, contract and blacklist status and all covariates use
data up to 30 June 2022 only. This gate builds that population and writes it in
EXACTLY g4's file schema (covariates.csv, wallet_coin_day_pre.csv, ...) to
the explicit immutable `cfg['output_dir']` (run_all: out/recovery/june/<generation>/, must be new/empty),
plus `population_manifest.json` (written last; verified by R/pre.R before any CSV load). Work files live in
`<output_dir>.work`; the finished generation appears by one rename; no `current` pointer.
Inputs: `cfg['input_root']` (holds g0/) and `cfg['baseline_blacklist_dir']`.

  * `june_end` = last block with timestamp <= 2022-06-30 23:59:59 UTC, found by
    binary search (lib.blocks.block_at) inside [pre_period_start, pre_period_end],
    boundary-verified, pinned in config/blocks.json, receipted.
  * Exposed arm = EOAs (no code at june_end) that withdrew from / deposited to
    one of the designated pools (g0 manifest) at or before june_end. The Tornado
    logs were never persisted by g1 (only receipts), so they are re-read here,
    census_start -> june_end only (every read <= june_end <= pre_period_end).
  * Control frame = 30 random 400-block windows wholly inside 9-30 June
    (seed 20251015), g1b's candidate extraction, minus contracts (code at
    june_end), minus every Tornado withdrawal-recipient / EncryptedNote-depositor
    (all instances) up to june_end.
  * Panel = g4's: opening balanceOf at pre_period_start, Transfer walk through
    pre_period_end (NOT beyond), USDT DestroyedBlackFunds, integers. The walk,
    checkpointing, balance audit and cross-endpoint reverify ARE g4's functions
    (imported; g4's module globals are re-pointed at this gate's out dir for the
    duration of run()).
  * Floors on the 22 June days only: mean combined holdings >= $100 and
    >= 6 nonzero days. Covariates are as of june_end / over June (the three
    `*_at_pre_end` blacklist columns keep g4's names for schema identity; here
    they mean "at june_end"). `exchange_share` stays empty (no labels).

Recovery (SPEC S15): census reads are provider-scoped (no cross-provider fallback), checkpointed per
chunk under `census_chunks/` and bound to producer commit / config hash / g0 input hashes / query shape
(lib.rpc.ChunkStore): an interrupted run resumes without refetching completed chunks, quota (429) gets
a bounded cool-down then a hard failure, a result cap bisects, a corrupt or mismatched chunk fails the
run. One coverage proof per sweep spans chunks from every attempt (`census_coverage.json`). Blacklist
status comes ONLY from an accepted g0_baseline_blacklist generation (cfg['baseline_blacklist_dir']); the
lifetime out/g0 event files are not loaded for it.

HARD RULE (as g4): no block > pre_period_end is ever read. `g4._guard_block`
runs before every request and a final assertion walks every receipt.

`--smoke` (run_all.py): 2 frame windows + a seeded sample of 400 exposed
addresses, written to out/g4_june_smoke/ (never to out/g4_june/).
"""
import csv
import fcntl
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from gates import g0_baseline_blacklist as g0b
from gates import g1_cohort as g1
from gates import g1b_frame
from gates import g4_controls as g4
from lib import blocks, rpc

JUNE_END_TS = "2022-06-30T23:59:59Z"
WINDOW_SEED = 20251015
N_WINDOWS = 30
WINDOW_BLOCKS = 400
N_JUNE_DAYS = (date(2022, 6, 30) - g4.PRE_START_DATE).days + 1  # 22 (9..30 June)
JUNE_MIN_NONZERO_DAYS = 6
SMOKE_WINDOWS = 2
SMOKE_EXPOSED = 400
SWEEP_WINDOW = 250_000      # topic-only Withdrawal+EncryptedNote sweep (as g1b)
POOL_WINDOW = 2_000_000     # pool-filtered Deposit sweep (as g1)
CODE_BATCH = 1500

_HERE = os.path.dirname(os.path.abspath(__file__))


# --- pure helpers (unit-tested) -------------------------------------------

def _epoch(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def resolve_june_end(endpoint, pre_start, pre_end, pinned, pin_fn=blocks.pin):
    """june_end = highest block with timestamp <= JUNE_END_TS, by binary search over
    [pre_start, pre_end] (never above pre_end). A block already pinned as `june_end`
    is NOT re-resolved but its boundary is re-verified. Returns
    (block, timestamp_iso, next_block_timestamp_iso)."""
    existing = pinned.get("june_end")
    jb = existing["block"] if existing else blocks.block_at(endpoint, JUNE_END_TS, lo=pre_start, hi=pre_end)
    if not jb < pre_end:
        raise AssertionError(f"june_end {jb} must be < pre_period_end {pre_end}")
    ts, nxt = blocks.block_timestamp(endpoint, jb), blocks.block_timestamp(endpoint, jb + 1)
    target = _epoch(JUNE_END_TS)
    if not (_epoch(ts) <= target < _epoch(nxt)):
        raise AssertionError(f"june_end {jb} ({ts}) is not the last block <= {JUNE_END_TS} (next block {nxt})")
    if not existing:
        pin_fn("june_end", jb, ts)
    return jb, ts, nxt


def draw_windows(lo, june_end, seed=WINDOW_SEED, n=N_WINDOWS, width=WINDOW_BLOCKS):
    """n distinct window starts drawn uniformly (Python random.Random(seed).sample) so that
    every window [s, s+width-1] lies wholly inside [lo, june_end]. Sorted. Returns [(start, end)]."""
    starts = sorted(random.Random(seed).sample(range(lo, june_end - width + 2), n))
    wins = [(s, s + width - 1) for s in starts]
    assert all(lo <= s and e <= june_end for s, e in wins)
    return wins


def june_floor(usdc_s, usdt_s):
    """(mean_total floor-div over the 22 June days, nonzero_days, pass): mean >= $100 and
    >= 6 nonzero June days. Days after 30 June are ignored; no 60-day conditioning."""
    mean_total, nonzero, _ = g4.floor_stats(usdc_s[:N_JUNE_DAYS], usdt_s[:N_JUNE_DAYS])
    return mean_total, nonzero, (mean_total >= g4.FLOOR_MIN_UNITS and nonzero >= JUNE_MIN_NONZERO_DAYS)


def blacklisted_in_window(events, start_block, end_block):
    """events: (block, log_index, kind) for ONE address and ONE coin. True if the address is
    blacklisted at start_block (state from events <= start_block) or an 'add' occurs in
    (start_block, end_block]. Events after end_block are never consulted."""
    ev = [e for e in events if e[0] <= end_block]
    return g4.blacklist_state_at(ev, start_block) or any(k == "add" for b, _, k in ev if b > start_block)


def june_participants(universe, dlegs, rule):
    """Wallets that proceed to the June floors under `rule` (g1's select_participants semantics, applied to the JUNE
    placebo's own pre window, NOT main's (pre_period_start, pre_period_end], which runs past the placebo event):
    `baseline_transfer_required` keeps a wallet only if the held Transfer walk saw >=1 USDC/USDT Transfer leg for it on a
    day index < N_JUNE_DAYS, i.e. in (pre_period_start, june_end] -- ANY leg, zero-value and third-party included;
    legs after june_end never count. `none` keeps everyone. `dlegs` is g4._merge_walk(by_day=True)'s
    dlegs[coin][addr] = {day: [legs, mint_redeem_legs]}. No RPC: the walk already holds these counts."""
    rule = g1.resolve_participation_rule({"participation_rule": rule})
    participation = {a: {c for c in g4.COINS if any(v[0] for d, v in dlegs[c].get(a, {}).items() if 0 <= d < N_JUNE_DAYS)}
                     for a in universe}
    return g1.select_participants(sorted(universe), participation, rule)


def participation_window(pre_start, june_end):
    """Exact bounds of the participation window recorded in meta / manifest / summary: blocks (pre_start, june_end],
    day indexes [0, N_JUNE_DAYS) = 9..30 June."""
    return {"after_block_exclusive": pre_start, "to_block_inclusive": june_end,
            "first_date": g4.PRE_START_DATE.isoformat(), "last_date": JUNE_END_TS[:10], "day_index_lt": N_JUNE_DAYS}


def participation_note(rule):
    """Schema-notes sentence in main g1's form; R's cohort_schema_rule regex (`participation_rule=<rule>`, exactly one) reads it."""
    return (f"participation_rule={rule} (UNRESOLVED upstream, S14; recorded, not defaulted). Window (pre_period_start, june_end] "
            f"(the June placebo's own pre window, 9-30 June; legs after june_end never count): "
            + ("a wallet is tracked only with >=1 USDC/USDT Transfer leg in the window (zero-value and third-party legs count); "
               "others are dropped BEFORE the June floors." if rule == g1.RULE_BASELINE_TRANSFER
               else "no baseline-Transfer requirement; every candidate reaches the June floors."))


def exposure_arms(withdrawal_logs, note_logs, deposit_logs, pools, june_end):
    """Arm per address (withdrawal|deposit|both), zero address dropped. Withdrawals count only
    at `pools` (the sweep is all-instance); a deposit is attributed via the EncryptedNote in
    the same (block, tx), as g1. Every log must be at or before june_end."""
    pools = set(pools)
    for l in [*withdrawal_logs, *note_logs, *deposit_logs]:
        if g1._log_block(l) > june_end:
            raise g4.PostPeriodRead(f"Tornado log at block {g1._log_block(l)} > june_end {june_end}")
    wd = {g1._withdrawal_recipient(l) for l in withdrawal_logs if l["address"].lower() in pools}
    note_by_tx = {}
    for l in note_logs:
        note_by_tx.setdefault((g1._log_block(l), l["transactionHash"].lower()), g1._note_depositor(l))
    dep = set()
    for l in deposit_logs:
        d = note_by_tx.get((g1._log_block(l), l["transactionHash"].lower()))
        if d:
            dep.add(d)
    arms = {}
    for a in (wd | dep) - {g4.ZERO_ADDRESS}:
        arms[a] = "both" if (a in wd and a in dep) else ("withdrawal" if a in wd else "deposit")
    return arms


def exclusion_set(withdrawal_logs, note_logs):
    """Every Withdrawal recipient + EncryptedNote depositor (all instances), as g1b."""
    return ({g1b_frame._withdrawal_recipient(l) for l in withdrawal_logs}
            | {g1b_frame._encryptednote_depositor(l) for l in note_logs})


def frame_candidates(window_logs):
    """Unique from/to addresses of Transfer logs (g1b's extraction)."""
    out = set()
    for l in window_logs:
        out.update(g1b_frame._transfer_addrs(l))
    return out


# --- output schema: g4's columns (names + dtypes identical), June descriptions ---

_JUNE_DESC = {
    "wallet_id": "dense id by sorted address over this gate's universe (June controls + June exposed floor-passers)",
    "mean_total_units": "mean over the 22 June days (9-30 Jun 2022) of (USDC+USDT) base units, floor div",
    "mean_usdc_units": "mean USDC base units over the 22 June days, floor div",
    "mean_usdt_units": "mean USDT base units over the 22 June days, floor div",
    "nonzero_days": "June days (of 22) with nonzero total balance",
    "usdc_share_ppm": "ratio of June sums: sum(USDC) / (sum(USDC)+sum(USDT)) in parts per million; empty if no holdings",
    "transfer_count_total": "Transfer legs (from + to appearances), USDC + USDT, over 9-30 June",
    "transfer_count_usdc": "USDC Transfer legs over 9-30 June",
    "transfer_count_usdt": "USDT Transfer legs over 9-30 June",
    "eth_balance_wei_units": "eth_getBalance at june_end, wei",
    "first_send_block": "PROXY for wallet age: block of first outgoing tx <= june_end (bisection on eth_getTransactionCount); empty = never sent by june_end",
    "wallet_age_blocks": "june_end - first_send_block; empty if never sent",
    "mint_redeem_legs": "transfers to/from the zero address over 9-30 June (zero address only)",
    "issuer_mint_redeem_flag": "mint_redeem_legs > 0 (over June)",
    "blacklisted_usdc_at_pre_end": "COLUMN NAME KEPT FOR SCHEMA IDENTITY WITH g4: USDC blacklist membership at ANY time from 9 Jun to june_end (state at pre_period_start (full baseline history from deployment) or any baseline Blacklisted event in (pre_period_start, june_end]); flag, not a drop",
    "blacklisted_usdt_at_pre_end": "COLUMN NAME KEPT FOR SCHEMA IDENTITY WITH g4: USDT blacklist membership at ANY time from 9 Jun to june_end (state at pre_period_start (full baseline history from deployment) or any baseline AddedBlackList event in (pre_period_start, june_end]); flag, not a drop",
}
COV_COLS = [(n, t, _JUNE_DESC.get(n, d)) for n, t, d in g4.COV_COLS]
PANEL_COLS = list(g4.PANEL_COLS)  # unchanged: dense 60 days x 2 coins for June floor-passers


# --- plumbing -----------------------------------------------------------------

@contextmanager
def _retarget(gate, out_dir, in_root):
    """Point g4's module globals (checkpoint dir, receipts file, g0 inputs, receipt gate tag)
    at this gate for the duration of run(); restored afterwards."""
    saved = {k: getattr(g4, k) for k in ("_OUT_DIR", "_RECEIPTS_PATH", "_G0_DIR", "_receipt", "_WALL")}
    g1_g0 = g1._G0_DIR
    orig_receipt = g4._receipt
    g4._OUT_DIR = out_dir
    g4._RECEIPTS_PATH = os.path.join(out_dir, "receipts.jsonl")
    g4._G0_DIR = g1._G0_DIR = os.path.join(in_root, "g0")
    g4._receipt = lambda rec: orig_receipt({**rec, "gate": gate})
    g4._WALL = {}
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(g4, k, v)
        g1._G0_DIR = g1_g0


def _log(msg):
    print(f"[g4_june] {msg}", flush=True)


def _line_count(path):
    if not os.path.exists(path):
        return 0
    with open(path) as f:
        return sum(1 for _ in f)


# --- run lock, input preflight, durable-file helpers ---

class RunLocked(RuntimeError):
    pass


def acquire_run_lock(out_dir):
    """Exclusive non-blocking flock on out_dir/.lock, held for the life of the returned file object.
    A second invocation on the same out dir would rmtree the first run's staging generation and
    interleave checkpoint jsonl, so it must fail loudly instead."""
    try:
        fh = os.fdopen(os.open(os.path.join(out_dir, ".lock"), os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o666), "a+")   # a symlinked .lock is never followed
    except OSError as e:
        raise RunLocked(f"{out_dir}/.lock cannot be opened as a regular file (symlink?): {e}") from e
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        fh.close()
        raise RunLocked(f"{out_dir} is locked by another g4_june run (flock on .lock held); refusing to start") from e
    fh.seek(0)
    fh.truncate()
    fh.write(f"pid {os.getpid()}\n")
    fh.flush()
    return fh


# every g0 file this gate reads. circle_events.csv is NOT read: blacklist status comes from the baseline manifest.
INPUT_FILES = ("manifest.csv", "usdt_events.csv")
PRODUCER_FILES = tuple(os.path.join(_HERE, "..", p) for p in ("lib/rpc.py", "gates/g4_june.py", "gates/g4_controls.py"))


def baseline_dir(cfg):
    """Explicit baseline-blacklist generation dir (cfg['baseline_blacklist_dir'] only; no env var); no fallback to
    the lifetime g0 event files or to old flags."""
    path = cfg.get("baseline_blacklist_dir")
    if not path:
        raise RuntimeError("no baseline blacklist input: pass cfg['baseline_blacklist_dir'] "
                           "(an accepted out/recovery/blacklist_baseline/<generation>); "
                           "g4_june never falls back to out/g0 event files")
    return path


def preflight_inputs(g0_dir):
    """sha256 of every g0 input the gate will read; a missing/empty file raises (g1._load_usdt_destroyed
    would otherwise silently return [] for a missing usdt_events.csv)."""
    out = {}
    for name in INPUT_FILES:
        path = os.path.join(g0_dir, name)
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            raise RuntimeError(f"required g0 input {path} is missing or empty")
        out[name] = g4._sha256_file(path)
    return out


def check_meta(meta_path, meta, gate):
    """Refuse a resume whose frame/blocks/inputs differ; otherwise write meta.json atomically."""
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            if json.load(f) != meta:
                raise RuntimeError(f"{meta_path} differs from this run's frame/blocks/inputs -- stale checkpoints; "
                                   f"move out/{gate} aside to start fresh")
    else:
        tmp = meta_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(meta, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, meta_path)


def repair_jsonl_tail(path):
    """Drop a torn (unparseable) final record of an append-only jsonl, like g4._Ckpt; an unterminated
    but valid final record is terminated. Returns True if anything was changed."""
    if not os.path.exists(path):
        return False
    with open(path, "rb") as f:
        raw = f.read()
    if not raw:
        return False
    terminated = raw.endswith(b"\n")
    head, sep, tail = (raw[:-1] if terminated else raw).rpartition(b"\n")
    keep = len(head) + len(sep)  # bytes before the last record
    try:
        json.loads(tail)
        ok = True
    except ValueError:
        ok = False
    if ok:
        if terminated:
            return False
        with open(path, "ab") as f:
            f.write(b"\n")
        return True
    _log(f"{path}: dropping unparseable final line (torn write)")
    with open(path, "r+b") as f:
        f.truncate(keep)
    return True


def _guard(pre_end):
    return lambda hi: g4._guard_block(hi, pre_end)


def _on_chunk(gate, store):
    """Append a newly fetched chunk to the shared receipts.jsonl (g4's receipt writer; reused chunks
    were receipted by the attempt that fetched them and stay in the append-only file)."""
    def rec(c):
        q = store.binding["query"]
        g4._receipt({"endpoint": c["endpoint"], "fromBlock": c["lo"], "toBlock": c["hi"], "logCount": c["logCount"],
                     "addressCount": len(q["address"] or ()), "topics": q["topics"], "sha256": c["response_sha256"],
                     "attempt_id": c["attempt_id"], "t": c["t"]})
    return rec


def _sweep(out_dir, name, endpoint, lo, hi, address, topics, window, binding_ctx, pre_end, gate):
    """One provider-scoped, checkpointed census read of [lo, hi] -> (logs, coverage_proof). Chunks that an
    earlier attempt completed are validated and reused; nothing falls back to another provider."""
    commit, cfg_sha, inputs = binding_ctx
    store = rpc.ChunkStore(os.path.join(out_dir, "census_chunks", name),
                           rpc.make_binding(commit, cfg_sha, inputs, endpoint, address, topics))
    logs = rpc.acquire_logs(store, lo, hi, window, guard=_guard(pre_end), on_chunk=_on_chunk(gate, store))
    proof = store.coverage_proof(lo, hi)
    if (proof["lo"], proof["hi"]) != (lo, hi) or proof["binding_sha256"] != store.binding_sha256:
        raise rpc.CoverageError(f"{name}: coverage proof does not match the sweep span/binding")
    _log(f"  {name}: {proof['events']} events in {proof['chunks']} chunks over {len(proof['attempt_ids'])} attempt(s)")
    return logs, proof


def census_config_sha256(census_start, june_end, pre_end, windows):
    """Hash of everything that shapes the census queries; part of every chunk's binding."""
    cfg = rpc.ENDPOINTS[g1b_frame.WIDE]
    return rpc.canonical_sha256({
        "census_start": census_start, "june_end": june_end, "pre_end": pre_end, "windows": [list(w) for w in windows],
        "sweep_window": SWEEP_WINDOW, "pool_window": POOL_WINDOW, "wide": g1b_frame.WIDE,
        "caps": {k: cfg.get(k) for k in ("block_range_cap", "shape_caps", "unmeasured_shape_cap", "result_cap")}})


def _warn_legacy_caches(out_dir):
    for name in ("tornado_sets.json", "frame_candidates.json"):
        if os.path.exists(os.path.join(out_dir, name)):
            _log(f"IGNORING legacy cache {name}: it carries no payload/provenance/coverage proof and is never reused")


def _valid_code(r):
    import re
    if not (isinstance(r, str) and re.match(r"^0x[0-9a-fA-F]*$", r)):
        raise ValueError(f"malformed eth_getCode result {r!r}")
    return r


def _has_code_many(addrs, block, pre_end, ckpt):
    """Checkpointed, parallel batched eth_getCode at one pinned block -> {addr: bool}."""
    addrs = sorted(addrs)
    out, todo = {}, []
    for bi in range(0, len(addrs), CODE_BATCH):
        chunk = addrs[bi:bi + CODE_BATCH]
        key = f"{block}:{bi}"
        got = ckpt.data.get(key)
        if got is not None and set(got) == set(chunk):
            out.update(got)
        else:
            todo.append((key, chunk))

    def fetch(kc):
        key, chunk = kc
        g4._guard_block(block, pre_end)
        res = g4._batch_post(g4.BATCH, [("eth_getCode", [a, hex(block)]) for a in chunk], validate=_valid_code)
        g4._receipt({"kind": "eth_getCode", "endpoint": g4.BATCH, "fromBlock": block, "toBlock": block,
                     "addressCount": len(chunk),
                     "sha256": hashlib.sha256(json.dumps(res).encode()).hexdigest(), "t": time.time()})
        return key, {a: r not in ("0x", "0x0") for a, r in zip(chunk, res)}

    step = g4.WORKERS * 4
    for i in range(0, len(todo), step):
        for key, got in g4._pmap(fetch, todo[i:i + step]):
            ckpt.put(key, got)
            out.update(got)
        _log(f"  eth_getCode @ {block}: {min(i + step, len(todo))}/{len(todo)} batches")
    return out


def _tornado_sets(census_start, june_end, pre_end, pools, out_dir, gate, binding_ctx):
    """(arms, exclusion, stats, proofs). Both sweeps are provider-scoped and checkpointed chunk by chunk
    (resumable across attempts); the sets are a pure function of validated chunks <= june_end."""
    _log(f"Tornado sweep (a): Withdrawal+EncryptedNote, all instances, {census_start} -> {june_end} on {g1b_frame.WIDE}")
    a_logs, proof_a = _sweep(out_dir, "sweep_a", g1b_frame.WIDE, census_start, june_end, None,
                             [[g1b_frame.WITHDRAWAL_TOPIC, g1b_frame.ENCRYPTED_NOTE_TOPIC]], SWEEP_WINDOW,
                             binding_ctx, pre_end, gate)
    wd = [l for l in a_logs if l["topics"][0].lower() == g1b_frame.WITHDRAWAL_TOPIC.lower()]
    notes = [l for l in a_logs if l["topics"][0].lower() == g1b_frame.ENCRYPTED_NOTE_TOPIC.lower()]
    _log(f"  {len(wd)} Withdrawal, {len(notes)} EncryptedNote logs")
    _log(f"Tornado sweep (b): Deposit @ {len(pools)} designated pools, {census_start} -> {june_end}")
    dep, proof_b = _sweep(out_dir, "sweep_b", g1b_frame.WIDE, census_start, june_end, pools,
                          [[g1.DEPOSIT_TOPIC]], POOL_WINDOW, binding_ctx, pre_end, gate)
    arms = exposure_arms(wd, notes, dep, pools, june_end)
    excl = exclusion_set(wd, notes)
    stats = {"withdrawal_logs": len(wd), "pool_withdrawal_logs": sum(1 for l in wd if l["address"].lower() in set(pools)),
             "encryptednote_logs": len(notes), "pool_deposit_logs": len(dep), "exposed_candidates": len(arms),
             "exclusion": len(excl)}
    return arms, excl, stats, {"sweep_a": proof_a, "sweep_b": proof_b}


def _frame(windows, june_end, pre_end, out_dir, gate, binding_ctx):
    """Candidates from unfiltered USDC+USDT Transfer logs in the drawn windows (one checkpointed store each)."""
    cands, per, proofs = set(), [], {}
    for i, (s, e) in enumerate(windows):
        logs, proofs[f"frame_{s}_{e}"] = _sweep(out_dir, f"frame_{s}_{e}", g1b_frame.WIDE, s, e, [g1.USDC, g1.USDT],
                                                [[g1.TRANSFER_TOPIC]], WINDOW_BLOCKS, binding_ctx, pre_end, gate)
        a = frame_candidates(logs)
        cands |= a
        per.append([s, e, len(logs), len(a)])
        _log(f"  window {i + 1}/{len(windows)} [{s},{e}]: {len(logs)} Transfer logs, {len(a)} unique addrs")
    return cands, per, proofs


# --- run ------------------------------------------------------------------------

MANIFEST_NAME = "population_manifest.json"


def _stale_g4_env():
    """Every G4_* variable (SAMPLE_CONTROLS, BASELINE_BLACKLIST_DIR, WALK_ENDPOINT, AUDIT_MAX, OUT_DIR, ...) silently changes
    g4_controls' behaviour or routing; the routed populations (main, june) admit none of them."""
    return sorted(k for k, v in os.environ.items() if k.startswith("G4_") and v)


def _producer_dirty():
    """Producer source files that differ from HEAD (staged or unstaged); [] when clean."""
    out = subprocess.check_output(["git", "diff", "--name-only", "HEAD", "--", *[os.path.abspath(p) for p in PRODUCER_FILES]],
                                  cwd=_HERE, text=True)
    return out.split()


def _publish(final_dir, stage, work_dir, smoke, inputs, june_end, n_cov, n_panel, identity, *, participation_rule, participation_window):
    """Write population_manifest.json LAST (atomic, hashes of every other output file), then move the finished
    stage dir to its explicit final location with one rename. R/pre.R verifies this manifest before any CSV load.
    `identity` is rpc.producer_identity() captured ONCE at run start: publishing is refused if HEAD moved, the producer
    sources changed since, or they differ from HEAD (dirty tree)."""
    participation_rule = g1.resolve_participation_rule({"participation_rule": participation_rule})
    try:
        with open(os.path.join(stage, "covariates.schema.json")) as f:
            notes = json.load(f).get("notes")
    except (OSError, ValueError) as e:
        raise RuntimeError(f"covariates.schema.json unreadable ({e}): cannot bind participation_rule") from e
    if sorted(set(re.findall(r"\bparticipation_rule=[A-Za-z_-]+", notes or ""))) != [f"participation_rule={participation_rule}"]:
        raise RuntimeError(f"covariates.schema.json notes do not record exactly participation_rule={participation_rule}; refusing to publish")
    head, _, src_hash = identity.partition(":")
    if not (re.fullmatch(r"[0-9a-f]{40}", head) and re.fullmatch(r"[0-9a-f]{16}", src_hash)):
        raise RuntimeError(f"cannot record producer identity: {identity!r}")
    now = rpc.producer_identity(PRODUCER_FILES)
    if now != identity:
        raise RuntimeError(f"producer identity changed during the run ({identity} -> {now}); refusing to publish")
    dirty = _producer_dirty()
    if dirty:
        raise RuntimeError(f"producer sources differ from HEAD ({', '.join(dirty)}); refusing to publish an unreproducible generation")
    meta = os.path.join(work_dir, "meta.json")
    if not os.path.isfile(meta) or os.path.islink(meta):
        raise RuntimeError(f"{meta} missing: cannot bind the run's frame/inputs record")
    shutil.copyfile(meta, os.path.join(stage, "meta.json"))
    walk_ep, replay_ep = g4.assert_single_provider(work_dir)
    outputs = {n: g4._sha256_file(os.path.join(stage, n)) for n in sorted(os.listdir(stage))
               if n != MANIFEST_NAME and os.path.isfile(os.path.join(stage, n))}
    man = {"population_id": "june_placebo_smoke" if smoke else "june_placebo", "generation_id": os.path.basename(final_dir),
           "producing_commit": head, "producer_source_sha256": src_hash, "schema_version": g4.SCHEMA_VERSION,
           "baseline_start_date": g4.PRE_START_DATE.isoformat(), "baseline_end_date": JUNE_END_TS[:10], "excluded_dates": [],
           "walk_endpoint": walk_ep, "replay_endpoint": replay_ep,
           "cutoff_block": june_end, "participation_rule": participation_rule, "participation_window": participation_window, "input_file_sha256": dict(sorted(inputs.items())), "output_file_sha256": outputs,
           "row_counts": {"covariates.csv": n_cov, "wallet_coin_day_pre.csv": n_panel}}
    tmp = os.path.join(stage, MANIFEST_NAME + ".tmp")
    with open(tmp, "w") as f:
        json.dump(man, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, os.path.join(stage, MANIFEST_NAME))
    os.makedirs(os.path.dirname(final_dir), exist_ok=True)
    _check_target_free(final_dir)  # re-check right before the rename; rename(2) onto an empty dir is atomic, onto a filled one fails
    os.rename(stage, final_dir)


def _check_target_free(final_dir):
    if os.path.lexists(final_dir) and (os.path.islink(final_dir) or not os.path.isdir(final_dir) or os.listdir(final_dir)):
        raise RuntimeError(f"output dir {final_dir} exists and is not an empty directory; generations are immutable")


def run(cfg):
    smoke = bool(cfg.get("smoke"))
    gate = "g4_june_smoke" if smoke else "g4_june"
    final_dir = cfg.get("output_dir")
    if not final_dir:
        raise RuntimeError("no output dir: pass cfg['output_dir'] (explicit out/recovery/june/<generation>); "
                           "g4_june has no default or `current` output")
    if not cfg.get("input_root"):
        raise RuntimeError("no input root: pass cfg['input_root'] (directory holding g0/); there is no fallback")
    final_dir = os.path.abspath(final_dir)
    if final_dir.endswith(".work"):
        raise RuntimeError(f"output dir {final_dir} must not end in .work (reserved for the work directory)")
    _check_target_free(final_dir)
    stale = _stale_g4_env()
    if stale:
        raise RuntimeError(f"environment {', '.join(stale)} must be unset for the routed g4_june population (no env fallbacks)")
    cfg = {**cfg, "output_dir": final_dir}
    out_dir = final_dir + ".work"  # checkpoints / lock / receipts / staging; the final dir appears only by one atomic rename
    if os.path.islink(out_dir) or (os.path.lexists(out_dir) and not os.path.isdir(out_dir)):
        raise RuntimeError(f"{out_dir} is a symlink or not a directory; refusing to use it as the work directory")
    g1.resolve_participation_rule(cfg)  # required, never defaulted: before the work dir exists and before any RPC
    os.makedirs(out_dir, exist_ok=True)
    cfg["producer_identity"] = rpc.producer_identity(PRODUCER_FILES)  # captured ONCE; binds chunks and the published manifest
    with _retarget(gate, out_dir, cfg["input_root"]):
        return _run(cfg, smoke, gate, out_dir)


def _run(cfg, smoke, gate, out_dir):
    t_run = time.time()
    lock = acquire_run_lock(out_dir)  # held until the process exits / this frame is released
    try:
        return _run_locked(cfg, smoke, gate, out_dir, t_run)
    finally:
        lock.close()


def _run_locked(cfg, smoke, gate, out_dir, t_run):
    G = g4
    rule = g1.resolve_participation_rule(cfg)
    inputs = preflight_inputs(G._G0_DIR)  # before any RPC / any deletion
    baseline = g0b.load_baseline(baseline_dir(cfg), "june")  # verified before any RPC; raises BaselineError
    inputs = {**inputs, "baseline_blacklist_manifest": baseline.manifest_sha256}
    meta_path = os.path.join(out_dir, "meta.json")
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            if json.load(f).get("inputs") != inputs:
                raise RuntimeError(f"{meta_path}: g0 inputs differ from the recorded ones -- "
                                   f"stale checkpoints; move out/{gate} aside to start fresh")
    repair_jsonl_tail(G._RECEIPTS_PATH)
    gen_name = "gen-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + G._git_commit()[:8]
    for d in os.listdir(out_dir):  # crash leftovers: a generation dir that never got its summary
        dp = os.path.join(out_dir, d)
        if d.startswith("gen-") and os.path.isdir(dp) and not os.path.exists(os.path.join(dp, "summary.md")):
            shutil.rmtree(dp)
    stage = os.path.join(out_dir, gen_name)
    os.makedirs(stage)
    pinned = blocks.load_pinned()
    census_start = pinned["census_start"]["block"]
    pre_start = pinned["pre_period_start"]["block"]
    pre_end = pinned["pre_period_end"]["block"]
    checks = []

    # --- june_end ---
    for attempt in range(6):  # drpc: ~5.6 % transient errors, rpc._post does not retry
        try:
            jb, jts, jnext = resolve_june_end("drpc", pre_start, pre_end, pinned)
            break
        except (OSError, rpc.RpcError):
            if attempt == 5:
                raise
            time.sleep(2 ** attempt)
    G._receipt({"kind": "june_end", "block": jb, "timestamp": jts, "next_block_timestamp": jnext,
                "target": JUNE_END_TS, "t": time.time()})
    june_end = jb
    if baseline.cutoff != june_end:
        raise RuntimeError(f"baseline consumer cutoff {baseline.cutoff} != resolved june_end {june_end}")
    day_of_june_end = (datetime.fromisoformat(jts.replace("Z", "+00:00")).date() - G.PRE_START_DATE).days
    checks.append((f"june_end {june_end} ({jts}) is the last block <= {JUNE_END_TS}, pinned, and < pre_period_end {pre_end}",
                   june_end < pre_end and day_of_june_end == N_JUNE_DAYS - 1, f"next block {june_end + 1} @ {jnext}"))
    _log(f"june_end = block {june_end} @ {jts} (pre_period_end {pre_end})")

    # --- exposure + exclusion (provider-scoped, chunk-checkpointed, bound to commit/config/inputs) ---
    windows = draw_windows(pre_start + 1, june_end)
    binding_ctx = (cfg["producer_identity"], census_config_sha256(census_start, june_end, pre_end, windows), inputs)
    _warn_legacy_caches(out_dir)
    pools = g1._load_designated_pools()
    arms, excl, tstats, proofs = _tornado_sets(census_start, june_end, pre_end, pools, out_dir, gate, binding_ctx)
    exp_cand = sorted(arms)
    if smoke:
        exp_cand = sorted(random.Random(G.SEED).sample(exp_cand, min(SMOKE_EXPOSED, len(exp_cand))))
        _log(f"SMOKE: exposed sample {len(exp_cand)} of {len(arms)}")
    code_ck = G._Ckpt("code_checkpoint.jsonl")

    # --- frame ---
    with open(os.path.join(stage, "window_draw.json"), "w") as f:
        json.dump({"seed": WINDOW_SEED, "n": N_WINDOWS, "width": WINDOW_BLOCKS, "lo": pre_start + 1, "hi": june_end,
                   "windows": windows, "used": windows[:SMOKE_WINDOWS] if smoke else windows}, f, indent=1)
    used = windows[:SMOKE_WINDOWS] if smoke else windows
    cands, per_window, frame_proofs = _frame(used, june_end, pre_end, out_dir, gate, binding_ctx)
    proofs.update(frame_proofs)
    expected_spans = {"sweep_a": (census_start, june_end), "sweep_b": (census_start, june_end),
                      **{f"frame_{a}_{b}": (a, b) for a, b in used}}
    with open(os.path.join(stage, "census_coverage.json"), "w") as f:
        json.dump(proofs, f, sort_keys=True, indent=1)
    checks.append(("census coverage proof: every sweep/frame chunk (reused from earlier attempts + new) is bound to producer/config/inputs/query, "
                   "chunks tile their span exactly, (tx_hash, log_index) unique, none at/over the result cap",
                   set(proofs) == set(expected_spans)
                   and all((p["lo"], p["hi"]) == expected_spans[k] and p["unique_events"] == p["events"]
                           for k, p in proofs.items()),
                   f"{len(proofs)} stores, {sum(p['chunks'] for p in proofs.values())} chunks, {sum(p['events'] for p in proofs.values())} events "
                   f"(census_coverage.json)"))
    cands.discard(G.ZERO_ADDRESS)
    to_check = sorted((cands - excl) | set(exp_cand))
    _log(f"{len(cands)} frame candidates, {len(cands & excl)} in the Tornado exclusion set; "
         f"eth_getCode @ {june_end} for {len(to_check)} addresses (candidates minus exclusion, plus exposed)")
    has_code = _has_code_many(to_check, june_end, pre_end, code_ck)
    exposed = {a: arms[a] for a in exp_cand if not has_code[a]}
    controls = sorted(a for a in cands - excl if not has_code[a] and a not in exposed)
    arm_of = {a: "control" for a in controls}
    arm_of.update(exposed)
    universe = sorted(arm_of)
    universe_set = set(universe)
    wallet_id = {a: i for i, a in enumerate(universe)}
    frame_sha = hashlib.sha256("\n".join(universe).encode()).hexdigest()
    checks.append(("no control is in the Tornado exclusion set (<= june_end), is a contract at june_end, or is the zero address",
                   not (set(controls) & excl) and G.ZERO_ADDRESS not in universe_set
                   and not any(has_code[a] for a in universe), f"{len(controls)} controls"))
    _log(f"universe {len(universe)} = {len(controls)} controls + {len(exposed)} exposed EOAs; frame sha {frame_sha[:12]}")

    meta = {"frame_sha256": frame_sha, "pre_start": pre_start, "pre_end": pre_end, "june_end": june_end,
            "walk_array": G.WALK_ARRAY, "smoke": smoke, "inputs": inputs, "participation_rule": rule,
            "participation_window": participation_window(pre_start, june_end)}
    check_meta(meta_path, meta, gate)

    # --- opening balances + walk (g4's, by_day for June-restricted leg counts) ---
    bal_ck = G._Ckpt("balances_checkpoint.jsonl")
    with G._timed("opening balances"):
        _log(f"opening balanceOf @ {pre_start} for {len(universe)} wallets x 2 coins on {G.BATCH}")
        opening = {c: G._balance_of_many(c, universe, pre_start, pre_end, bal_ck) for c in G.COINS}
    walk_ck = G._Ckpt("walk_checkpoint.jsonl")
    with G._timed("transfer walk"):
        G._run_walk(universe, pre_start, pre_end, walk_ck, by_day=True)
    flows, legs, mr, dlegs = G._merge_walk(walk_ck, universe_set, by_day=True)

    destroyed = [r for r in G._load_destroyed(pre_start, pre_end) if r["address"] in universe_set]
    destroyed_applied = 0
    for r in destroyed:
        day = (date.fromisoformat(r["date"]) - G.PRE_START_DATE).days
        if 0 <= day < G.N_DAYS:
            fa = flows["USDT"][r["address"]]
            fa[day] = fa.get(day, 0) - r["amount"]
            destroyed_applied += 1

    # --- participation rule (same as main g1) from the legs the walk already holds, BEFORE the June floors ---
    tracked = june_participants(universe, dlegs, rule)
    tracked_set = set(tracked)
    in_window = set(june_participants(universe, dlegs, "baseline_transfer_required"))
    n_baseline = len(in_window)
    dropped_exposed = sum(1 for a in universe if a not in tracked_set and arm_of[a] != "control")
    _log(f"participation_rule={rule}: {n_baseline} of {len(universe)} wallets have a Transfer leg in (pre_start, june_end]; "
         f"{len(tracked)} reach the June floors ({dropped_exposed} exposed dropped)")

    # --- reconstruction + JUNE floors ---
    series, stats = {}, {}
    with G._timed("reconstruct"):
        for a in tracked:
            s_usdc = G.reconstruct_series(opening["USDC"][a], flows["USDC"].get(a, {}))
            s_usdt = G.reconstruct_series(opening["USDT"][a], flows["USDT"].get(a, {}))
            mean_total, nonzero, ok = june_floor(s_usdc, s_usdt)
            stats[a] = (mean_total, nonzero, sum(s_usdc[:N_JUNE_DAYS]), sum(s_usdt[:N_JUNE_DAYS]))
            if ok:
                series[a] = (s_usdc, s_usdt)
    passers = sorted(series)
    ctrl_pass = [a for a in passers if arm_of[a] == "control"]
    exp_pass = [a for a in passers if arm_of[a] != "control"]
    _log(f"June-floor-passing: {len(passers)} ({len(ctrl_pass)} control, {len(exp_pass)} exposed of {len(exposed)})")

    # --- ETH at june_end, wallet age (first send <= june_end) ---
    eth_ck = G._Ckpt("eth_checkpoint.jsonl")
    eth, todo = {}, []
    with G._timed("eth balance"):
        for bi in range(0, len(passers), G.ETH_BATCH):
            chunk = passers[bi:bi + G.ETH_BATCH]
            got = eth_ck.data.get(str(bi))
            if got is not None and set(got) == set(chunk):
                eth.update(got)
            else:
                todo.append((str(bi), chunk))

        def fetch_eth(kc):
            k, chunk = kc
            res = G._point_batch("eth_getBalance", "eth_getBalance", [([a, hex(june_end)], june_end) for a in chunk], pre_end)
            return k, {a: g1._hex_to_int(r) for a, r in zip(chunk, res)}

        for k, got in G._pmap(fetch_eth, todo):
            eth_ck.put(k, got)
            eth.update(got)

    age_ck = G._Ckpt("age_checkpoint.jsonl")
    first_send = {}

    def nonce_fn(pairs):
        parts = [pairs[i:i + G.NONCE_BATCH] for i in range(0, len(pairs), G.NONCE_BATCH)]
        res = G._pmap(lambda part: G._point_batch("eth_getTransactionCount", "eth_getTransactionCount",
                                                  [([a, hex(b)], b) for a, b in part], pre_end), parts)
        return [g1._hex_to_int(r) for part in res for r in part]

    with G._timed("wallet age"):
        for gi in range(0, len(passers), G.AGE_GROUP):
            group = passers[gi:gi + G.AGE_GROUP]
            got = age_ck.data.get(str(gi))
            if got is None or set(got) != set(group):
                got = G.bisect_first_send(group, nonce_fn, june_end)
                age_ck.put(str(gi), got)
            first_send.update(got)

    bl = baseline.events(universe_set)  # baseline manifest (deployment -> pre_period_end), cut at june_end

    # --- reconciliation + balance-identity audit (at pre_end: the walk runs through pre_end) ---
    rng = random.Random(G.SEED)
    ranked = sorted(ctrl_pass, key=lambda a: (stats[a][0], a))
    pick = []
    if ranked:
        step = max(1, len(ranked) // G.RECON_N)
        pick = sorted({ranked[min(i * step + rng.randrange(step), len(ranked) - 1)] for i in range(min(G.RECON_N, len(ranked)))})
    recon_ck = G._Ckpt("recon_checkpoint.jsonl")
    mism = []
    with G._timed("reconciliation"):
        for coin in G.COINS:
            fresh = G._balance_of_many(coin, pick, pre_end, pre_end, recon_ck)
            for a in pick:
                recon = series[a][0 if coin == "USDC" else 1][-1]
                if recon != fresh[a]:
                    mism.append(f"{a} {coin}: reconstructed={recon} balanceOf={fresh[a]}")
    checks.append((f"day-end reconstructed balance == balanceOf @ pre_period_end, {len(pick)} control wallets x 2 coins, tolerance 0",
                   bool(pick) and not mism, "; ".join(mism) or f"ok ({len(pick)} wallets, {2 * len(pick)} pairs)"))

    audit_set = G.pick_audit_set(passers, arm_of, G.AUDIT_MAX)
    audit_ck = G._Ckpt("audit_checkpoint.jsonl")
    audit_mism = []
    with G._timed("balance-identity audit"):
        for ci, coin in enumerate(G.COINS):
            fresh = G._balance_of_many(coin, audit_set, pre_end, pre_end, audit_ck)
            for a in audit_set:
                if series[a][ci][-1] != fresh[a]:
                    audit_mism.append((a, arm_of[a], coin, series[a][ci][-1], fresh[a]))
    if audit_mism:
        with open(os.path.join(stage, "audit_mismatches.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["address", "arm", "coin", "reconstructed", "balanceOf_pre_end"])
            w.writerows(audit_mism)
    checks.append((f"balance identity: opening balanceOf @ {pre_start} + sum of walked daily deltas == balanceOf @ pre_period_end, "
                   f"{len(audit_set)} of {len(passers)} floor-passers x 2 coins, tolerance 0",
                   bool(audit_set) and not audit_mism,
                   f"{len(audit_mism)} mismatches: " + "; ".join(f"{a} {c} recon={r} chain={f}" for a, _, c, r, f in audit_mism[:50])
                   if audit_mism else f"0 mismatches ({len(audit_set)} wallets, {2 * len(audit_set)} pairs)"))
    capped = [(k, r) for k in walk_ck.data for r in walk_ck.data[k]["ranges"]
              if r[2] >= G.RESULT_CAP.get(r[4], float("inf"))]
    checks.append(("no checkpointed walk range returned >= its endpoint result cap (possible truncation)", not capped,
                   f"{len(capped)} ranges at/over cap"))
    rv_ck = G._Ckpt("reverify_checkpoint.jsonl")
    with G._timed("reverify"):
        rv_cached, rv_fresh, rv_per = G._reverify(walk_ck, universe, pre_end, rv_ck)
    checks.append(("walk units re-verified on a second endpoint, stratified by walk endpoint (count + sha256); "
                   "cached entries must be bound to digest/count/endpoints/version", rv_cached + rv_fresh > 0,
                   f"{rv_cached + rv_fresh} ranges matched: {rv_cached} from bound cache, {rv_fresh} freshly compared; units per endpoint {rv_per}"))

    # --- covariates.csv + panel (g4's columns) ---
    def june_sum(coin, a, idx):
        return sum(v[idx] for d, v in dlegs[coin].get(a, {}).items() if d < N_JUNE_DAYS)

    cov_path = os.path.join(stage, "covariates.csv")
    with open(cov_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[c[0] for c in G.COV_COLS])
        w.writeheader()
        for a in passers:
            mean_total, nonzero, su, st = stats[a]
            fs = first_send[a]
            share = G.usdc_share_ppm(su, st)  # ratio of June sums
            lu, lt = june_sum("USDC", a, 0), june_sum("USDT", a, 0)
            ms = june_sum("USDC", a, 1) + june_sum("USDT", a, 1)
            w.writerow({
                "wallet_id": wallet_id[a], "address": a, "arm": arm_of[a], "mean_total_units": mean_total,
                "mean_usdc_units": su // N_JUNE_DAYS, "mean_usdt_units": st // N_JUNE_DAYS, "nonzero_days": nonzero,
                "usdc_share_ppm": "" if share is None else share,
                "transfer_count_total": lu + lt, "transfer_count_usdc": lu, "transfer_count_usdt": lt,
                "eth_balance_wei_units": eth[a],
                "first_send_block": "" if fs is None else fs,
                "wallet_age_blocks": "" if fs is None else june_end - fs,
                "mint_redeem_legs": ms, "issuer_mint_redeem_flag": ms > 0,
                "blacklisted_usdc_at_pre_end": blacklisted_in_window(bl["USDC"].get(a, ()), pre_start, june_end),
                "blacklisted_usdt_at_pre_end": blacklisted_in_window(bl["USDT"].get(a, ()), pre_start, june_end),
                "exchange_share": "",
            })
    cov_sha = G._sha256_file(cov_path)
    panel_path = os.path.join(stage, "wallet_coin_day_pre.csv")
    n_panel = 0
    with open(panel_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[c[0] for c in G.PANEL_COLS])
        w.writeheader()
        for a in passers:
            for ci, coin in enumerate(G.COINS):
                for d, bal in enumerate(series[a][ci]):
                    w.writerow({"wallet_id": wallet_id[a], "coin": coin,
                                "date": (G.PRE_START_DATE + timedelta(days=d)).isoformat(), "day_index": d,
                                "balance_units": bal, "arm": arm_of[a], "nonzero": bal > 0})
                    n_panel += 1
    panel_sha = G._sha256_file(panel_path)
    in_hash = [frame_sha, baseline.manifest_sha256]
    G._write_schema("covariates", COV_COLS, ("wallet_id",), len(passers), cov_sha, in_hash,
                    "June-only placebo population: wallets passing the JUNE floors (mean >= $100 over 9-30 Jun, >= 6 of 22 nonzero days) "
                    "of both arms. Covariates over 9-30 June / as of june_end; blacklisted_*_at_pre_end columns mean 'blacklisted at any time 9 Jun-june_end'. "
                    "Confidential (address-level): never committed. exchange_share is NEEDS_CONTEXT (empty). " + participation_note(rule), outdir=stage)
    G._write_schema("wallet_coin_day_pre", PANEL_COLS, ("wallet_id", "coin", "day_index"), n_panel, panel_sha, in_hash,
                    "June-floor-passing wallets only (both arms); dense over 60 days x 2 coins (balances through pre_period_end). " + participation_note(rule), outdir=stage)

    # --- hard-rule assertion + final checks ---
    n_receipts = G._assert_receipts_within(G._RECEIPTS_PATH, pre_end)
    G._assert_walk_within(walk_ck.data, pre_end)
    checks.append((f"no receipt beyond pre_period_end ({pre_end})", True, f"{n_receipts} receipts, max to_block <= {pre_end}"))
    unit_issues = (g1._validate_units_columns(cov_path, ["mean_total_units", "mean_usdc_units", "mean_usdt_units", "eth_balance_wei_units"])
                   + g1._validate_units_columns(panel_path, ["balance_units"]))
    checks.append(("every *_units column matches ^-?[0-9]+$", not unit_issues, "; ".join(unit_issues) or "ok"))
    checks.append(("schema.json sidecars present",
                   all(os.path.exists(os.path.join(stage, f"{n}.schema.json")) for n in ("covariates", "wallet_coin_day_pre")), "ok"))
    with open(cov_path, newline="") as f:
        hdr = next(csv.reader(f))
    checks.append(("covariates.csv header == g4's column list", hdr == [c[0] for c in G.COV_COLS], f"{len(hdr)} columns"))
    never_sent = sum(1 for a in passers if first_send[a] is None)
    mix = Counter(r[4] for k in walk_ck.data for r in walk_ck.data[k]["ranges"])
    mix_s = ", ".join(f"{ep} {n}" for ep, n in sorted(mix.items())) + f" (walk endpoint={G.WALK}, replay endpoint={G.REPLAY_ENDPOINT[G.WALK]})"

    passed = sum(1 for _, ok, _ in checks if ok)
    failed = [(n, d) for n, ok, d in checks if not ok]
    walls = "; ".join(f"{k} {v:.0f}s" for k, v in G._WALL.items())
    win_rows = [f"| {i} | {s}-{e} | {n} | {u} |" for i, (s, e, n, u) in enumerate(per_window)]
    lines = [
        "# Gate 4 (June placebo) -- June-only population" + (" -- SMOKE RUN (2 windows, sampled exposed; NOT the placebo population)" if smoke else ""),
        "",
        f"**june_end = block {june_end} @ {jts}** (last block <= {JUNE_END_TS}; next block {june_end + 1} @ {jnext}); pre_period_end = {pre_end}.",
        f"Tornado sweep (census_start {census_start} -> june_end): {tstats['withdrawal_logs']} Withdrawal logs ({tstats['pool_withdrawal_logs']} at the "
        f"{len(pools)} designated pools), {tstats['encryptednote_logs']} EncryptedNote, {tstats['pool_deposit_logs']} pool Deposit logs; "
        f"exposed candidates {tstats['exposed_candidates']}; exclusion set {tstats['exclusion']}.",
        f"Frame: {len(used)} of {N_WINDOWS} drawn windows x {WINDOW_BLOCKS} blocks, seed {WINDOW_SEED} (random.Random.sample over starts in "
        f"[{pre_start + 1}, {june_end - WINDOW_BLOCKS + 1}]); {len(cands)} unique candidates; full draw in `window_draw.json`.",
        f"Universe: {len(universe)} = {len(controls)} control + {len(exposed)} exposed EOAs (code screen at june_end).",
        f"Participation rule (recorded, not defaulted): `{rule}`. Wallets with a Transfer leg in the June placebo's own pre window (pre_period_start, june_end] ({pre_start}, {june_end}]: "
        f"{n_baseline}; reaching the June floors: {len(tracked)} ({dropped_exposed} exposed wallets dropped by the rule).",
        f"Walk: opening balanceOf @ {pre_start}; Transfer walk {pre_start + 1}..{pre_end}; endpoint mix (ranges): {mix_s}; "
        f"USDT DestroyedBlackFunds events applied: {destroyed_applied}.",
        f"**June-floor-passing: {len(passers)} ({len(ctrl_pass)} control, {len(exp_pass)} exposed of {len(exposed)}).** "
        f"Floors over the {N_JUNE_DAYS} June days: mean total >= {G.FLOOR_MIN_UNITS} base units ($100) and >= {JUNE_MIN_NONZERO_DAYS} nonzero days.",
        f"Wallet age (proxy: first outgoing tx block <= june_end): {len(passers) - never_sent} have one, {never_sent} never sent.",
        "Blacklist membership at any time from 9 Jun to june_end (added then removed counts) is flagged (columns `blacklisted_*_at_pre_end` keep g4's names), not dropped.",
        "",
        "## NEEDS_CONTEXT", "",
        "- `exchange_share`: column left EMPTY (no verified, citable CEX label list).",
        "- Issuer treasury addresses: flag covers the zero address only.",
        "", "## Frame windows", "", "| window | blocks | Transfer logs | unique addrs |", "|---|---|---|---|", *win_rows,
        "", "## Acceptance checks", "", "| check | result | detail |", "|---|---|---|",
    ]
    for name, ok, detail in checks:
        lines.append(f"| {name} | {'PASS' if ok else 'FAIL'} | {detail} |")
    lines += [
        "", f"{passed}/{len(checks)} checks passed.", "",
        f"Generation: `{os.path.basename(cfg['output_dir'])}` (`{cfg['output_dir']}`; immutable, no `current` pointer).", "",
        f"Wall clock this invocation: {time.time() - t_run:.0f}s ({walls}). Resumable: re-run the same command.",
        "", "## Outputs", "",
        f"- `covariates.csv` ({len(passers)} rows, sha256 `{cov_sha}`) -- address-level, confidential, untracked",
        f"- `wallet_coin_day_pre.csv` ({n_panel} rows, sha256 `{panel_sha}`) -- address-level, confidential, untracked",
    ]
    body = "\n".join(lines) + "\n"
    if failed:
        with open(os.path.join(stage, "summary.FAILED.md"), "w") as f:
            f.write(body)
        os.replace(stage, os.path.join(out_dir, "failed-" + gen_name[4:]))
        with open(os.path.join(out_dir, "summary.FAILED.md"), "w") as f:
            f.write(body)
    else:
        with open(os.path.join(stage, "summary.md"), "w") as f:
            f.write(body)
        _publish(cfg["output_dir"], stage, out_dir, smoke, inputs, june_end, len(passers), n_panel, cfg["producer_identity"],
                 participation_rule=rule, participation_window=participation_window(pre_start, june_end))
    _log(f"{passed}/{len(checks)} acceptance checks passed")
    for name, detail in failed:
        _log(f"FAIL: {name} -- {detail}")
    return 0 if not failed else 1
