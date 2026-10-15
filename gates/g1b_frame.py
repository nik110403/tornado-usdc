"""Gate 1b -- control sampling frame probe.

Samples 30 uniformly-drawn, seeded, 400-block windows strictly inside the
pinned pre-period; pulls unfiltered (no counterparty filter) USDC + USDT
`Transfer` logs per window; builds the candidate control-frame population
from unique `from`/`to` addresses; drops addresses with code (contracts) and
any address in the Tornado exclusion set; for survivors, computes the
pre-period transfer count (from the same sample pull) and an implied
inverse-probability weight; reports capture-recapture frame-size estimation,
recurrence, weight distribution, and top-1% leg concentration.

Exclusion set: "S1's full Deposit/Withdrawal/EncryptedNote address set
(depositors, recipients)" is pulled independently here, not read from
out/g0/*. gates/g0_manifest.py's census is topic-only and used only to
*count* logs and classify pools (out/g0/manifest.csv is pool-level, address
column is the pool address itself) -- it never decodes or persists a
per-log depositor/recipient address, so there is no S1 output file this gate
could read. S3 depends only on S1 in the SPEC's dependency graph (S2, which
does derive per-address withdrawal recipients and EncryptedNote depositors
for the cohort, runs in parallel with this item -- see
the project tracker), so this gate re-derives
the same address universe itself over S1's own census span
(census_start -> event_block). Deposit(bytes32,uint32,uint256) carries no
address field at all (commitment, leafIndex, timestamp only) -- it cannot
contribute a depositor or recipient, so it is computed (topic-list
completeness / keccak self-test parity with g0) but never queried; only
Withdrawal (recipient = first data word, per SPEC S2(a): "the indexed
relayer is never the recipient") and EncryptedNote (depositor = topics[1],
the indexed sender) are swept. See Deviation notes in the S3 implementation
report for this and the window-sample seed placement.
"""
import csv
import hashlib
import json
import os
import random
import re
import statistics
import subprocess
import time
import urllib.error
import urllib.request
from collections import Counter

from lib import keccak, rpc

_HERE = os.path.dirname(os.path.abspath(__file__))
_OUT_DIR = os.path.join(_HERE, "..", "out", "g1b")
_RECEIPTS_PATH = os.path.join(_OUT_DIR, "receipts.jsonl")

USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"

WIDE = "tenderly"   # wide-range topic-only eth_getLogs
NARROW = "drpc"     # eth_getCode

DEPOSIT_TOPIC = keccak.topic("Deposit(bytes32,uint32,uint256)")           # computed, not queried (see module docstring)
WITHDRAWAL_TOPIC = keccak.topic("Withdrawal(address,bytes32,address,uint256)")
ENCRYPTED_NOTE_TOPIC = keccak.topic("EncryptedNote(address,bytes)")
TRANSFER_TOPIC = keccak.topic("Transfer(address,address,uint256)")


def _cross_check_topics():
    """The two topics the spec hands us a known-good value for, plus the
    Transfer topic used throughout this gate -- catches a transposed ABI
    signature string before any topic is used to query (never copy a topic
    hash from a document)."""
    assert ENCRYPTED_NOTE_TOPIC == "0xfa28df43db3553771f7209dcef046f3bdfea15870ab625dcda30ac58b82b4008", ENCRYPTED_NOTE_TOPIC
    assert TRANSFER_TOPIC == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef", TRANSFER_TOPIC


_cross_check_topics()

# SPEC S3 says "starts drawn uniformly (seeded, seed in config)". Kept as a
# module constant here instead of a new config/*.json file: this item's
# Touches prefixes are gates/g1b_ and out/g1b/ only -- config/ is not listed,
# and the Contract says "Write ONLY inside this item's Touches: prefixes".
# See Deviation note in the S3 implementation report.
WINDOW_SEED = 20220609  # pre-period start date (2022-06-09), YYYYMMDD -- deterministic, documented
N_WINDOWS = 30
WINDOW_BLOCKS = 400

_NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, OSError)
_MIN_INTERVAL = {NARROW: 0.4}  # drpc: 2.5 req/s sustained
_last_call_t = {}


def _throttle(endpoint):
    interval = _MIN_INTERVAL.get(endpoint)
    if not interval:
        return
    wait = interval - (time.time() - _last_call_t.get(endpoint, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_call_t[endpoint] = time.time()


def _get_code(endpoint, addr, block, max_retries=4, backoff_s=0.5):
    last_err = None
    for attempt in range(max_retries):
        try:
            _throttle(endpoint)
            return rpc.get_code(endpoint, addr, block)
        except _NETWORK_ERRORS as e:
            last_err = e
            time.sleep(backoff_s * (2 ** attempt))
    raise RuntimeError(f"eth_getCode {addr} failed after {max_retries} network retries: {last_err}")


# --- batched JSON-RPC eth_getCode (mevblocker accepts eth_getCode batches up
# to at least 2,000 items per gates/g1_cohort.py's live probe -- see that
# module's docstring). _batch_post/_batch_get_code below are copied from
# gates/g1_cohort.py verbatim (same endpoint, same batch size) rather than
# imported: g1_cohort.py does not expose them as a lib, and lib/rpc.py is
# out of this item's Touches. _get_code above stays as the per-address
# fallback for any batch that still fails after _batch_post's own retries. ---

BATCH = "mevblocker"       # matches g1_cohort.py's OR endpoint for batched point queries
CODE_BATCH_SIZE = 1500     # matches g1_cohort.py's _batch_get_code default
_CODE_CHECKPOINT_PATH = os.path.join(_OUT_DIR, "code_check.jsonl")


class _BatchError(Exception):
    pass


def _batch_post(endpoint, calls, max_retries=4, backoff_s=0.5):
    """calls: list of (method, params). Returns list of raw `result` values
    in the same order as `calls`, matched by request id (not response
    order). Retries transient failures with backoff; on a 4xx that looks
    size-related, bisects the batch and recurses. Raises RuntimeError if a
    call still can't be resolved after that -- never silently dropped."""
    if not calls:
        return []
    cfg = rpc.ENDPOINTS[endpoint]
    payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)]
    req = urllib.request.Request(
        cfg["url"], data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": rpc.USER_AGENT},
        method="POST",
    )
    last_err = None
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = json.loads(resp.read())
            by_id = {}
            for item in raw:
                if "error" in item:
                    raise _BatchError(f"{endpoint} batch id={item.get('id')} -> {item['error']}")
                by_id[item["id"]] = item["result"]
            return [by_id[i] for i in range(len(calls))]
        except urllib.error.HTTPError as e:
            last_err = e
            if len(calls) > 1 and e.code in (400, 406, 413, 429):
                mid = len(calls) // 2
                return (_batch_post(endpoint, calls[:mid], max_retries, backoff_s)
                        + _batch_post(endpoint, calls[mid:], max_retries, backoff_s))
            time.sleep(backoff_s * (2 ** attempt))
        except (_NETWORK_ERRORS + (_BatchError,)) as e:
            last_err = e
            time.sleep(backoff_s * (2 ** attempt))
    raise RuntimeError(f"{endpoint} batch of {len(calls)} failed after {max_retries} retries: {last_err}")


def _batch_get_code(endpoint, addrs, block, batch_size=CODE_BATCH_SIZE):
    block_hex = hex(block)
    out = {}
    for i in range(0, len(addrs), batch_size):
        chunk = addrs[i:i + batch_size]
        results = _batch_post(endpoint, [("eth_getCode", [a, block_hex]) for a in chunk])
        out.update(zip(chunk, results))
    return out


def _load_code_checkpoint(block):
    """Resume support for the code-check step: out/g1b/code_check.jsonl is
    one JSON line per checked address -- {"address", "has_code", "block"} --
    appended after every batch. Deterministic to re-derive (same three
    fields, recomputed identically on a fresh run), so the survivor set is
    identical whether the run was interrupted or not. Only lines pinned to
    `block` are trusted -- a stale file from a different event_block is
    ignored rather than silently reused."""
    if not os.path.exists(_CODE_CHECKPOINT_PATH):
        return {}
    resumed = {}
    with open(_CODE_CHECKPOINT_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("block") == block:
                resumed[rec["address"]] = rec["has_code"]
    return resumed


def _append_code_checkpoint(fh, addr, has_code, block):
    fh.write(json.dumps({"address": addr, "has_code": has_code, "block": block}) + "\n")
    fh.flush()


# --- log decoding ---

def _data_words(log):
    data_hex = log["data"][2:] if log["data"].startswith("0x") else log["data"]
    data_bytes = bytes.fromhex(data_hex) if data_hex else b""
    return [data_bytes[i:i + 32] for i in range(0, len(data_bytes), 32)]


def _withdrawal_recipient(log):
    """Withdrawal(address to, bytes32 nullifierHash, address indexed relayer,
    uint256 fee) -- `to` is the first non-indexed field (SPEC S2(a): "the
    indexed relayer is never the recipient")."""
    words = _data_words(log)
    if not words:
        raise AssertionError(f"Withdrawal log missing recipient data word: {log}")
    return "0x" + words[0][-20:].hex().lower()


def _encryptednote_depositor(log):
    """EncryptedNote(address indexed sender, bytes encryptedNote)."""
    topics = log["topics"]
    if len(topics) < 2:
        raise AssertionError(f"EncryptedNote log missing indexed sender: {log}")
    return "0x" + topics[1][-40:].lower()


def _transfer_addrs(log):
    """Transfer(address indexed from, address indexed to, uint256 value)."""
    topics = log["topics"]
    return "0x" + topics[1][-40:].lower(), "0x" + topics[2][-40:].lower()


# --- chunked fetch w/ adaptive bisection for tenderly's undocumented ~50k
# result-count cap (see gates/g0_manifest.py's _get_logs_chunked /
# _fetch_window_bisecting -- same workaround, duplicated here rather than
# shared via lib/rpc.py or an import from gates.g0_manifest, since this
# item's Touches prefixes don't include lib/ or g0_manifest.py, and gates
# are otherwise self-contained in this codebase). ---

def _fetch_window_bisecting(endpoint, start, end, address, topics, gate, min_window=2000):
    try:
        return rpc.get_logs(endpoint, start, end, address=address, topics=topics, gate=gate)
    except rpc.GapError:
        if end - start + 1 <= min_window:
            raise
        mid = (start + end) // 2
        print(f"[g1b]   window [{start},{end}] failed (likely a result-count cap) -- bisecting at {mid}")
        return (
            _fetch_window_bisecting(endpoint, start, mid, address, topics, gate, min_window)
            + _fetch_window_bisecting(endpoint, mid + 1, end, address, topics, gate, min_window)
        )


def _get_logs_chunked(endpoint, from_block, to_block, address, topics, gate, window):
    all_logs = []
    start = from_block
    while start <= to_block:
        end = min(start + window - 1, to_block)
        all_logs.extend(_fetch_window_bisecting(endpoint, start, end, address, topics, gate))
        start = end + 1
    return all_logs


# --- receipts helpers (scoped tiling assertion over the shared per-gate file) ---

def _line_count(path):
    if not os.path.exists(path):
        return 0
    with open(path) as f:
        return sum(1 for _ in f)


def _new_receipts(start_line):
    with open(_RECEIPTS_PATH) as f:
        lines = f.readlines()
    return [json.loads(l) for l in lines[start_line:]]


def _assert_component_tiled(recs, from_block, to_block, label):
    tmp_path = _RECEIPTS_PATH + ".component_check"
    with open(tmp_path, "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    try:
        rpc.assert_tiling(tmp_path, from_block, to_block)
    except AssertionError as e:
        raise AssertionError(f"{label}: {e}") from e
    finally:
        os.remove(tmp_path)


def _tag_ranges(recs, address, topics):
    return [{"endpoint": r["endpoint"], "address": address, "topics": topics,
              "fromBlock": r["fromBlock"], "toBlock": r["toBlock"]} for r in recs]


def _cross_endpoint_reverify(all_ranges, fraction=0.05, min_n=5):
    """Reimplemented locally rather than via lib.rpc.reverify -- see
    gates/g0_manifest.py's _cross_endpoint_reverify for why (no re-splitting
    against the fallback endpoint's own cap; always replays address=None,
    under-constraining an address-filtered original query)."""
    import math

    n = min(max(min_n, math.ceil(len(all_ranges) * fraction)), len(all_ranges))
    sample = all_ranges[:n]  # deterministic: first n by fetch order
    other_endpoint = {"tenderly": "drpc", "drpc": "tenderly", "mevblocker": "drpc"}
    verified = []
    for r in sample:
        other = other_endpoint.get(r["endpoint"])
        if other is None:
            continue
        orig = rpc.get_logs(r["endpoint"], r["fromBlock"], r["toBlock"], address=r["address"], topics=r["topics"], gate=None)
        alt = rpc.get_logs(other, r["fromBlock"], r["toBlock"], address=r["address"], topics=r["topics"], gate=None)
        if len(orig) != len(alt):
            raise AssertionError(
                f"reverify logCount mismatch [{r['fromBlock']},{r['toBlock']}]: {r['endpoint']}={len(orig)} {other}={len(alt)}"
            )
        if sorted(rpc._log_key(l) for l in orig) != sorted(rpc._log_key(l) for l in alt):
            raise AssertionError(
                f"reverify tuple mismatch [{r['fromBlock']},{r['toBlock']}] between {r['endpoint']} and {other}"
            )
        verified.append((r["fromBlock"], r["toBlock"]))
    return verified


# --- table writer: CSV + schema.json sidecar (interchange schema) ---

SCHEMA_VERSION = 1


def _git_commit():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=_HERE, text=True).strip()
    except (subprocess.CalledProcessError, OSError, FileNotFoundError):
        return "unknown"


def _write_table(name, rows, columns, primary_key, input_hashes, notes=None):
    fieldnames = [c[0] for c in columns]
    pk = primary_key if isinstance(primary_key, (list, tuple)) else (primary_key,)
    rows_sorted = sorted(rows, key=lambda r: tuple(r[k] for k in pk))

    csv_path = os.path.join(_OUT_DIR, f"{name}.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows_sorted:
            w.writerow(r)
    with open(csv_path, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()

    schema = {
        "schema_version": SCHEMA_VERSION,
        "columns": [{"name": c[0], "dtype": c[1], "units": c[2]} for c in columns],
        "primary_key": list(pk),
        "null_policy": "empty string = missing / not applicable",
        "row_count": len(rows_sorted),
        "input_hashes": input_hashes,
        "producing_commit": _git_commit(),
        "sha256": sha,
    }
    if notes:
        schema["notes"] = notes
    with open(os.path.join(_OUT_DIR, f"{name}.schema.json"), "w") as f:
        json.dump(schema, f, indent=2, sort_keys=True)
        f.write("\n")
    return csv_path, sha, len(rows_sorted)


def _percentile(sorted_vals, p):
    """Nearest-rank percentile (0 <= p <= 100) on an already-sorted list."""
    if not sorted_vals:
        return None
    k = max(0, min(len(sorted_vals) - 1, round(p / 100 * (len(sorted_vals) - 1))))
    return sorted_vals[k]


def _chapman(n1, n2, m):
    """Chapman's bias-corrected Lincoln-Petersen capture-recapture
    estimator for the size of a population sampled twice (n1, n2 = counts
    in each sample, m = overlap)."""
    return (n1 + 1) * (n2 + 1) / (m + 1) - 1


def _read_s2_floor_count():
    """Best-effort read of S2's floor-passing pooled count from
    out/g1/crosstab.md. S3 depends only on S1 (S2 runs in parallel -- see
    the project tracker) so this degrades
    gracefully to None (not a gate failure) when out/g1/crosstab.md is
    absent or its kill-rule line doesn't parse."""
    path = os.path.join(_HERE, "..", "out", "g1", "crosstab.md")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        text = f.read()
    m = re.search(r"floor-passing[^0-9]*?([\d,]+)", text, re.IGNORECASE)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def run(cfg):
    os.makedirs(_OUT_DIR, exist_ok=True)
    if os.path.exists(_RECEIPTS_PATH):
        os.remove(_RECEIPTS_PATH)  # fresh ledger each run -- gitignored, regenerable

    pinned = cfg["blocks"]
    census_start = pinned["census_start"]["block"]
    event_block = pinned["event_block"]["block"]
    pre_start = pinned["pre_period_start"]["block"]
    pre_end = pinned["pre_period_end"]["block"]

    checks = []
    all_ranges = []  # every sub-range fetched this gate, tagged with its real
                      # (address, topics) -- for _cross_endpoint_reverify below.

    # --- exclusion set: Withdrawal recipients + EncryptedNote depositors,
    # topic-only, no address filter, over S1's own census span ---
    print(f"[g1b] exclusion sweep: Withdrawal+EncryptedNote {census_start} -> {event_block} on {WIDE}")
    start_line = _line_count(_RECEIPTS_PATH)
    excl_logs = _get_logs_chunked(
        WIDE, census_start, event_block,
        address=None, topics=[[WITHDRAWAL_TOPIC, ENCRYPTED_NOTE_TOPIC]], gate="g1b", window=250_000,
    )
    excl_recs = _new_receipts(start_line)
    _assert_component_tiled(excl_recs, census_start, event_block, "exclusion sweep")
    excl_hashes = [r["sha256"] for r in excl_recs]
    all_ranges.extend(_tag_ranges(excl_recs, None, [[WITHDRAWAL_TOPIC, ENCRYPTED_NOTE_TOPIC]]))

    withdrawal_logs = [l for l in excl_logs if l["topics"][0].lower() == WITHDRAWAL_TOPIC.lower()]
    encnote_logs = [l for l in excl_logs if l["topics"][0].lower() == ENCRYPTED_NOTE_TOPIC.lower()]
    print(f"[g1b]   {len(withdrawal_logs)} Withdrawal, {len(encnote_logs)} EncryptedNote logs")

    recipients = {_withdrawal_recipient(l) for l in withdrawal_logs}
    depositors = {_encryptednote_depositor(l) for l in encnote_logs}
    exclusion_source = {}
    for a in recipients:
        exclusion_source[a] = "withdrawal_recipient"
    for a in depositors:
        exclusion_source[a] = "both" if a in exclusion_source else "encryptednote_depositor"
    exclusion_set = set(exclusion_source)
    print(f"[g1b]   exclusion set: {len(exclusion_set)} addresses "
          f"({len(recipients)} recipients, {len(depositors)} depositors)")
    checks.append(("exclusion set size reported", len(exclusion_set) > 0, f"{len(exclusion_set)} addresses"))

    # --- 30 windows x 400 blocks, seeded, strictly inside the pre-period ---
    rng = random.Random(WINDOW_SEED)
    max_start = pre_end - WINDOW_BLOCKS + 1
    starts = sorted(rng.sample(range(pre_start, max_start + 1), N_WINDOWS))
    straddle = [s for s in starts if s < pre_start or s + WINDOW_BLOCKS - 1 > pre_end]
    checks.append(("no window straddles the pre-period bounds", not straddle,
                    "ok" if not straddle else f"{len(straddle)} offending starts: {straddle}"))

    window_addrs = []        # per-window unique address set (from union to)
    window_hashes = []
    leg_counter = Counter()  # address -> appearances (as from or to) across all windows
    for i, start in enumerate(starts):
        end = start + WINDOW_BLOCKS - 1
        start_line = _line_count(_RECEIPTS_PATH)
        logs = rpc.get_logs(WIDE, start, end, address=[USDC, USDT], topics=[[TRANSFER_TOPIC]], gate="g1b")
        recs = _new_receipts(start_line)
        _assert_component_tiled(recs, start, end, f"window {i} [{start},{end}]")
        window_hashes.extend(r["sha256"] for r in recs)
        all_ranges.extend(_tag_ranges(recs, [USDC, USDT], [[TRANSFER_TOPIC]]))
        addrs = set()
        for l in logs:
            frm, to = _transfer_addrs(l)
            addrs.add(frm)
            addrs.add(to)
            leg_counter[frm] += 1
            leg_counter[to] += 1
        window_addrs.append(addrs)
        print(f"[g1b]   window {i + 1}/{N_WINDOWS} [{start},{end}]: {len(logs)} Transfer logs, {len(addrs)} unique addrs")
    checks.append(("receipts tile each window", True, "ok (structurally enforced -- a tiling failure above would have raised)"))

    candidates = set().union(*window_addrs) if window_addrs else set()
    print(f"[g1b] {len(candidates)} unique candidate addresses across {N_WINDOWS} windows")

    # --- drop exclusion-set members first (no code check needed -- dropped either way) ---
    dropped_excl = candidates & exclusion_set
    to_check = candidates - exclusion_set

    # --- drop addresses with code at event_block (same anchor SPEC S2(d) uses),
    # batched (mevblocker, 1,500/POST -- same endpoint+size as g1_cohort.py's
    # eth_getCode step) with incremental checkpointing so an interrupted run
    # resumes instead of re-querying everything ---
    resumed = _load_code_checkpoint(event_block)
    resumed = {a: hc for a, hc in resumed.items() if a in to_check}
    remaining = sorted(to_check - set(resumed))
    if resumed:
        print(f"[g1b]   resumed {len(resumed)}/{len(to_check)} addresses from {_CODE_CHECKPOINT_PATH}")
    print(f"[g1b] eth_getCode check on {len(to_check)} candidates at block {event_block} "
          f"({BATCH}, batched, {CODE_BATCH_SIZE}/POST; {len(remaining)} remaining after resume)")

    has_code = dict(resumed)
    fallback_batches = 0
    with open(_CODE_CHECKPOINT_PATH, "a") as ckpt_fh:
        for i in range(0, len(remaining), CODE_BATCH_SIZE):
            chunk = remaining[i:i + CODE_BATCH_SIZE]
            try:
                results = _batch_get_code(BATCH, chunk, event_block, batch_size=CODE_BATCH_SIZE)
            except RuntimeError as e:
                fallback_batches += 1
                print(f"[g1b]   batch {i // CODE_BATCH_SIZE + 1} failed after retries ({e}); "
                      f"falling back to per-address eth_getCode ({NARROW}) for {len(chunk)} addresses")
                results = {addr: _get_code(NARROW, addr, event_block) for addr in chunk}
            for addr, code in results.items():
                hc = code not in (None, "0x", "0x0")
                has_code[addr] = hc
                _append_code_checkpoint(ckpt_fh, addr, hc, event_block)
            done = min(i + CODE_BATCH_SIZE, len(remaining))
            print(f"[g1b]   checked {done}/{len(remaining)} (+{len(resumed)} resumed)")

    contract_addrs = {a for a in to_check if has_code.get(a)}
    print(f"[g1b]   {len(contract_addrs)} candidates have code -- dropped; {len(dropped_excl)} in exclusion set -- dropped")
    if fallback_batches:
        print(f"[g1b]   {fallback_batches} batch(es) fell back to per-address eth_getCode")

    survivors = to_check - contract_addrs
    print(f"[g1b]   {len(survivors)} survivors")

    survivor_window_sets = [addrs & survivors for addrs in window_addrs]

    # --- capture-recapture across all window pairs (Chapman's estimator) ---
    pair_estimates = []
    for i in range(N_WINDOWS):
        for j in range(i + 1, N_WINDOWS):
            n1, n2 = len(survivor_window_sets[i]), len(survivor_window_sets[j])
            m = len(survivor_window_sets[i] & survivor_window_sets[j])
            pair_estimates.append(_chapman(n1, n2, m))
    implied_frame_size = statistics.median(pair_estimates) if pair_estimates else 0.0
    implied_frame_mean = statistics.mean(pair_estimates) if pair_estimates else 0.0

    # --- recurrence across windows ---
    recurrence = {a: sum(1 for s in survivor_window_sets if a in s) for a in survivors}
    recurrence_vals = sorted(recurrence.values())
    recurrence_hist = Counter(recurrence_vals)

    # --- per-survivor transfer count (legs) + implied inverse-probability
    # weight, normalized so weights sum to the implied frame size (each
    # survivor "represents" weight_i wallets of the underlying population --
    # Horvitz-Thompson-style; P(inclusion) is taken proportional to transfer
    # frequency per the brief, so weight ~ 1/frequency up to that norm) ---
    survivor_legs = {a: leg_counter[a] for a in survivors}
    total_inv = sum(1.0 / c for c in survivor_legs.values() if c > 0)
    weights = {}
    for a, c in survivor_legs.items():
        weights[a] = (implied_frame_size * (1.0 / c) / total_inv) if (c > 0 and total_inv > 0) else 0.0
    weight_vals = sorted(weights.values())
    w_p10 = _percentile(weight_vals, 10)
    w_p50 = _percentile(weight_vals, 50)
    w_p90 = _percentile(weight_vals, 90)
    w_max = weight_vals[-1] if weight_vals else None

    # --- top-1% share of legs ---
    legs_sorted = sorted(survivor_legs.values(), reverse=True)
    total_legs = sum(legs_sorted)
    top_k = max(1, round(0.01 * len(legs_sorted))) if legs_sorted else 0
    top1pct_share = (sum(legs_sorted[:top_k]) / total_legs) if total_legs > 0 else 0.0

    # --- compare to S2's floor-passing exposed count, if available (S2||S3 -- may not have run yet) ---
    s2_floor_count = _read_s2_floor_count()
    if s2_floor_count is None:
        comparison_line = "S2 not yet run (out/g1/crosstab.md not found or unparseable) -- 5x comparison pending"
    else:
        ratio_ok = implied_frame_size >= 5 * s2_floor_count
        comparison_line = (
            f"implied frame size {implied_frame_size:.0f} is "
            f"{'>=' if ratio_ok else '<'} 5x the S2 floor-passing exposed count "
            f"({s2_floor_count} -> threshold {5 * s2_floor_count})"
        )
    checks.append(("summary states implied frame size vs 5x S2 floor-passing count", True, comparison_line))

    # --- 5% cross-endpoint reverification over the whole gate's receipts ---
    print(f"[g1b] cross-endpoint reverification (5%, min 5, of {len(all_ranges)} ranges)")
    reverified = _cross_endpoint_reverify(all_ranges)
    print(f"[g1b]   reverified {len(reverified)} ranges")

    # --- write outputs ---
    wallet_ids = {a: i for i, a in enumerate(sorted(candidates | exclusion_set))}

    exclusion_rows = [
        {"wallet_id": wallet_ids[a], "address": a, "source": exclusion_source[a]}
        for a in exclusion_set
    ]
    exclusion_columns = [
        ("wallet_id", "int", "dense id, assigned by sorted address across this gate's address universe"),
        ("address", "str", "0x, 42 chars, lowercase"),
        ("source", "str", "withdrawal_recipient|encryptednote_depositor|both"),
    ]
    exclusion_path, exclusion_sha, exclusion_n = _write_table(
        "exclusion", exclusion_rows, exclusion_columns, "address", input_hashes=excl_hashes,
        notes="Deposit(bytes32,uint32,uint256) not swept -- no address field to decode; see module docstring.",
    )

    survivor_rows = [
        {
            "wallet_id": wallet_ids[a], "address": a,
            "windows_seen": recurrence[a], "transfer_count": survivor_legs[a],
            "weight_ipw": weights[a],
        }
        for a in survivors
    ]
    survivor_columns = [
        ("wallet_id", "int", "dense id, assigned by sorted address across this gate's address universe"),
        ("address", "str", "0x, 42 chars, lowercase"),
        ("windows_seen", "int", "number of the 30 sample windows this address appears in"),
        ("transfer_count", "int", "legs (from+to appearances) in the 30-window sample"),
        ("weight_ipw", "float", "implied inverse-probability weight; normalized so weights sum to the implied frame size"),
    ]
    survivor_path, survivor_sha, survivor_n = _write_table(
        "frame_survivors", survivor_rows, survivor_columns, "address", input_hashes=window_hashes,
    )

    passed = sum(1 for _, ok, _ in checks if ok)
    failed = [(n, d) for n, ok, d in checks if not ok]

    window_table = "\n".join(
        f"| {i} | {s}-{s + WINDOW_BLOCKS - 1} | {len(window_addrs[i])} | {len(survivor_window_sets[i])} |"
        for i, s in enumerate(starts)
    )
    recurrence_table = "\n".join(
        f"| {k} | {recurrence_hist.get(k, 0)} |" for k in range(1, N_WINDOWS + 1) if recurrence_hist.get(k, 0)
    )

    summary_lines = [
        "# Gate 1b -- control sampling frame probe",
        "",
        f"Exclusion sweep: {census_start} -> {event_block} (Withdrawal+EncryptedNote, topic-only, {WIDE}).",
        f"Withdrawal logs: {len(withdrawal_logs)}. EncryptedNote logs: {len(encnote_logs)}.",
        f"Exclusion set: {len(exclusion_set)} addresses ({len(recipients)} withdrawal recipients, "
        f"{len(depositors)} EncryptedNote depositors).",
        "",
        f"Sample: {N_WINDOWS} windows x {WINDOW_BLOCKS} blocks, seed {WINDOW_SEED}, drawn strictly inside "
        f"the pre-period [{pre_start}, {pre_end}] on {WIDE}.",
        f"Unique candidate addresses (from union to, all windows): {len(candidates)}.",
        f"Dropped -- in exclusion set: {len(dropped_excl)}. Dropped -- has code at {event_block}: {len(contract_addrs)}.",
        f"Survivors (control-frame population): {len(survivors)}.",
        "",
        "## Per-window detail",
        "",
        "| window | block range | unique addrs | survivor addrs |",
        "|---|---|---|---|",
        window_table,
        "",
        "## Recurrence across windows (survivors only)",
        "",
        f"Mean windows seen: {statistics.mean(recurrence_vals):.2f}. Max: {max(recurrence_vals) if recurrence_vals else 0}. "
        f"Seen in >= 2 windows: {sum(1 for v in recurrence_vals if v >= 2)}.",
        "",
        "| windows seen | # survivors |",
        "|---|---|",
        recurrence_table,
        "",
        "## Implied frame size (capture-recapture, Chapman's estimator, all "
        f"{len(pair_estimates)} window pairs)",
        "",
        f"Median: {implied_frame_size:.0f}. Mean: {implied_frame_mean:.0f}.",
        "",
        f"**{comparison_line}**",
        "",
        "## Weight distribution (implied inverse-probability weight)",
        "",
        f"p10={w_p10:.4f} p50={w_p50:.4f} p90={w_p90:.4f} max={w_max:.4f}" if weight_vals else "no survivors",
        "",
        f"Top-1% of survivors by legs hold {top1pct_share * 100:.2f}% of total legs "
        f"({top_k} addresses / {total_legs} total legs).",
        "",
        "## Acceptance checks",
        "",
        "| check | result | detail |",
        "|---|---|---|",
    ]
    for name, ok, detail in checks:
        summary_lines.append(f"| {name} | {'PASS' if ok else 'FAIL'} | {detail} |")
    summary_lines += [
        "",
        f"{passed}/{len(checks)} checks passed.",
        "",
        f"Cross-endpoint reverification: {len(reverified)} ranges re-fetched and matched.",
        "",
        "## Outputs",
        "",
        f"- `exclusion.csv` ({exclusion_n} rows, sha256 `{exclusion_sha}`)",
        f"- `frame_survivors.csv` ({survivor_n} rows, sha256 `{survivor_sha}`)",
        "",
        f"Exclusion path: `{os.path.relpath(exclusion_path, os.path.join(_HERE, '..'))}`",
        f"Survivors path: `{os.path.relpath(survivor_path, os.path.join(_HERE, '..'))}`",
    ]
    with open(os.path.join(_OUT_DIR, "summary.md"), "w") as f:
        f.write("\n".join(summary_lines) + "\n")

    print(f"[g1b] {passed}/{len(checks)} acceptance checks passed")
    if failed:
        for name, detail in failed:
            print(f"[g1b] FAIL: {name} -- {detail}")
        return 1
    return 0
