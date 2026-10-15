"""Gate 1 -- exposed cohort by arm x holding type, floor, kill rule.

(a) Withdrawal arm: recipients = first data word of every Withdrawal at S1's
    designated pools (the indexed `relayer` topic is never the recipient).
(b) Deposit arm: topic-only EncryptedNote sweep (all instances, tenderly),
    joined to designated-pool Deposit logs on (blockNumber, transactionHash);
    the unmatched residual is counted, not fetched further (deferred).
(c) Dedupe across arms; an address in both is flagged `both_arms`.
(d) Exclude any address with eth_getCode non-empty at the event block.
(e) Pre-period participation: filtered Transfer logs (USDC + USDT, both
    `from`/`to` topic positions) over the pinned pre-period blocks.
(f) Daily balances for every participating wallet: balanceOf at the
    pre-period-start boundary block (both coins) plus cumulative net
    Transfer flow per UTC day; USDT additionally applies S1's
    DestroyedBlackFunds at its block.
(g)/(h) Per-wallet floor/kill classification -> cohort.csv,
    wallet_coin_day_pre.csv, crosstab.md, deposit_residual.csv.

(d)/(f) fan out to O(10^5) addresses -- individually-throttled eth_getCode /
eth_call (as gate 0 does for its 97 emitters) would take many hours at
drpc's documented 2.5 req/s. Standard JSON-RPC batch requests (an array of
call objects in one HTTP POST) were probed live against all three endpoints
before writing this file: mevblocker accepts eth_getCode batches up to at
least 2,000 items and eth_call batches up to ~300-499 items (406 above that)
in well under a second; tenderly rate-limits aggressively on many small
requests (fits its documented "few large requests" use, not this). drpc's
"no JSON-RPC batching" note is taken as authoritative and never relied on.
_batch_post() below implements this locally (gate-scoped, since lib/rpc.py
is out of this item's touch scope) with retry + bisect-on-4xx, never a
silent drop. Range-based eth_getLogs fetches still go through
lib.rpc.get_logs (receipts + retry + tiling already built in there).
"""
import csv
import hashlib
import json
import os
import random
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from lib import keccak, rpc

_HERE = os.path.dirname(os.path.abspath(__file__))
_OUT_DIR = os.path.join(_HERE, "..", "out", "g1")
_G0_DIR = os.path.join(_HERE, "..", "out", "g0")
_RECEIPTS_PATH = os.path.join(_OUT_DIR, "receipts.jsonl")

USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
COINS = {"USDC": USDC, "USDT": USDT}
_ZERO_ADDRESS = "0x" + "0" * 40

WIDE = "tenderly"  # wide-range topic/address-filtered eth_getLogs (few large requests)
OR = "mevblocker"  # large OR-array filtered getLogs + batched point queries

# Pre-period window, per SPEC (fixed, not re-derived from the pinned boundary
# blocks -- those blocks are deliberately the last block *before*/*on* the
# boundary, see run() below).
PRE_START_DATE = date(2022, 6, 9)
PRE_END_DATE = date(2022, 8, 7)
N_DAYS = (PRE_END_DATE - PRE_START_DATE).days + 1  # 60

DEPOSIT_TOPIC = keccak.topic("Deposit(bytes32,uint32,uint256)")
WITHDRAWAL_TOPIC = keccak.topic("Withdrawal(address,bytes32,address,uint256)")
ENCRYPTED_NOTE_TOPIC = keccak.topic("EncryptedNote(address,bytes)")
TRANSFER_TOPIC = keccak.topic("Transfer(address,address,uint256)")
BALANCE_OF_SELECTOR = keccak.selector("balanceOf(address)")


def _cross_check_topics():
    assert ENCRYPTED_NOTE_TOPIC == "0xfa28df43db3553771f7209dcef046f3bdfea15870ab625dcda30ac58b82b4008", ENCRYPTED_NOTE_TOPIC
    assert TRANSFER_TOPIC == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef", TRANSFER_TOPIC
    assert BALANCE_OF_SELECTOR == "0x70a08231", BALANCE_OF_SELECTOR


_cross_check_topics()

_NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, OSError)
_UNITS_RE = re.compile(r"^-?[0-9]+$")

FLOOR_MIN_UNITS = 100 * 10 ** 6  # $100, 6-decimal stablecoins
FLOOR_MIN_NONZERO_DAYS = 14
KILL_THRESHOLD = 1500
TARGET_THRESHOLD = 3300


# --- receipts helpers (scoped tiling assertion; same pattern as g0 -- the
# shared receipts.jsonl holds several distinct, sometimes-overlapping spans
# in this gate, so whole-file tiling only makes sense per logical span). ---

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


def _get_logs_or_safe(endpoint, from_block, to_block, address, topics):
    """rpc.get_logs, but pre-splits any OR-array topics position down to
    `endpoint`'s own or_array_cap first, unioning (deduped) results across
    the sub-fetches. step (e) builds its wallet-address OR-arrays at 2,000
    items (safe for mevblocker's cap of >=2,400) -- reverify replays that
    same query on drpc or tenderly, whose caps are documented at ~1,000;
    without this, replay fails structurally (live run 2026-09-04: tenderly
    -32002 "not supported") regardless of whether the underlying data
    agrees. Not needed for the (a)/(b) WIDE-phase ranges reverify can also
    draw (small `address` list, no large topics array) -- those pass
    through the no-op branch unchanged.

    tenderly's documented or_array_cap (1,000) is itself wrong for this
    request shape (topics OR-array + an `address` filter): live probing
    2026-09-05 found exactly 999 items rejected outright with
    {-32002 "not supported"} while 998 succeeds -- not a size-dependent
    HTTP 400, a hard request-shape cutoff a hair under the documented
    figure. Rather than chase the exact boundary (which may not be stable
    across tokens/date), apply a local safety margin. Applied regardless
    of which endpoint we're chunking for (not just tenderly itself):
    lib.rpc.get_logs's OWN internal retry falls over to tenderly on a
    drpc failure (`_OTHER_ENDPOINT`, single-hop) with the SAME topics
    array unmodified, so a drpc-sized (1,000-item) sub-fetch could still
    land an oversized array on tenderly's doorstep on retry."""
    cap = min(rpc.ENDPOINTS[endpoint]["or_array_cap"], 900)
    if topics:
        for i, t in enumerate(topics):
            if isinstance(t, list) and len(t) > cap:
                seen, out = set(), []
                for j in range(0, len(t), cap):
                    sub_topics = list(topics)
                    sub_topics[i] = t[j:j + cap]
                    for l in rpc.get_logs(endpoint, from_block, to_block, address=address, topics=sub_topics, gate=None):
                        key = (l["blockHash"], l["transactionHash"], l["logIndex"])
                        if key not in seen:
                            seen.add(key)
                            out.append(l)
                return out
    return rpc.get_logs(endpoint, from_block, to_block, address=address, topics=topics, gate=None)


def _cross_endpoint_reverify(all_ranges, fraction=0.05, min_n=5):
    """Same rationale as g0._cross_endpoint_reverify: replay through
    lib.rpc.get_logs (re-splits per the replay endpoint's own cap, uses the
    real address/topics filter) rather than lib.rpc.reverify's raw
    single-shot replay.

    Sampled at random (seeded, deterministic), not `all_ranges[:n]` (g0's
    original -- fine there, since g0 never has more than a few dozen ranges
    total). g1's `all_ranges` mixes ~18 huge WIDE-endpoint ranges (a/b/note,
    up to 2,000,000 blocks each -- the *same* 6 boundaries 3x over, one set
    per phase) at the front with ~6,500 small step-(e) OR-array ranges
    (<=10,000 blocks) after. Live run (2026-09-04) found `[:n]` picked up
    all 18 huge ones -- each needing drpc-side auto-split into 100-200
    sequential sub-fetches to replay (its cap is 10,000) -- and over this
    session's connection that alone ran past two hours without finishing.
    Random sampling weights the replay cost by each range's true share
    (18/6530 =~ 0.3%), matching the reverify step's intent (a spot-check,
    not an exhaustive re-verification of the most expensive ranges) while
    still satisfying "5% of ranges, min 5" against the full population.
    """
    import math

    n = min(max(min_n, math.ceil(len(all_ranges) * fraction)), len(all_ranges))
    sample = random.Random(20260904).sample(all_ranges, n)
    other_endpoint = {"tenderly": "drpc", "drpc": "tenderly", "mevblocker": "drpc"}
    verified = []
    for i, r in enumerate(sample):
        other = other_endpoint.get(r["endpoint"])
        if other is None:
            continue
        orig = _get_logs_or_safe(r["endpoint"], r["fromBlock"], r["toBlock"], r["address"], r["topics"])
        alt = _get_logs_or_safe(other, r["fromBlock"], r["toBlock"], r["address"], r["topics"])
        if len(orig) != len(alt):
            raise AssertionError(
                f"reverify logCount mismatch [{r['fromBlock']},{r['toBlock']}]: {r['endpoint']}={len(orig)} {other}={len(alt)}"
            )
        if sorted(rpc._log_key(l) for l in orig) != sorted(rpc._log_key(l) for l in alt):
            raise AssertionError(
                f"reverify tuple mismatch [{r['fromBlock']},{r['toBlock']}] between {r['endpoint']} and {other}"
            )
        verified.append((r["fromBlock"], r["toBlock"]))
        if (i + 1) % 25 == 0 or i + 1 == len(sample):
            print(f"[g1]   reverify {i + 1}/{len(sample)}")
    return verified


def _fetch_window_bisecting(endpoint, start, end, address, topics, gate, min_window=2000):
    try:
        return rpc.get_logs(endpoint, start, end, address=address, topics=topics, gate=gate)
    except rpc.GapError:
        if end - start + 1 <= min_window:
            raise
        mid = (start + end) // 2
        print(f"[g1]   window [{start},{end}] failed (likely a result-count cap) -- bisecting at {mid}")
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


# --- batched JSON-RPC (point queries only -- getCode / eth_call; range
# fetches stay on lib.rpc.get_logs). Not put through the range-receipt /
# tiling machinery -- there is no block span to tile for a point query, same
# precedent as g0's _eth_call/_get_code (which also bypass receipts.jsonl). ---

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


def _balance_of_data(addr):
    return BALANCE_OF_SELECTOR + addr[2:].lower().rjust(64, "0")


def _hex_to_int(h):
    if h in (None, "0x", "0x0", ""):
        return 0
    return int(h, 16)


def _batch_get_code(endpoint, addrs, block, batch_size=1500):
    block_hex = hex(block)
    out = {}
    for i in range(0, len(addrs), batch_size):
        chunk = addrs[i:i + batch_size]
        results = _batch_post(endpoint, [("eth_getCode", [a, block_hex]) for a in chunk])
        out.update(zip(chunk, results))
    return out


def _batch_balance_of(endpoint, token, addrs, block, batch_size=250):
    block_hex = hex(block)
    out = {}
    for i in range(0, len(addrs), batch_size):
        chunk = addrs[i:i + batch_size]
        calls = [("eth_call", [{"to": token, "data": _balance_of_data(a)}, block_hex]) for a in chunk]
        results = _batch_post(endpoint, calls)
        for a, r in zip(chunk, results):
            out[a] = _hex_to_int(r)
    return out


# --- log decoding ---

def _log_block(log):
    return int(log["blockNumber"], 16)


def _withdrawal_recipient(log):
    """`to` is the first (non-indexed) data word; `relayer` is indexed
    (topics[1]) -- never the recipient (see module docstring / SPEC note)."""
    data_hex = log["data"][2:] if log["data"].startswith("0x") else log["data"]
    return "0x" + data_hex[:64][-40:].lower()


def _note_depositor(log):
    return "0x" + log["topics"][1][-40:].lower()


def _transfer_decode(log):
    from_addr = "0x" + log["topics"][1][-40:].lower()
    to_addr = "0x" + log["topics"][2][-40:].lower()
    value = _hex_to_int(log["data"])
    return from_addr, to_addr, value


def _log_date(log):
    ts_hex = log.get("blockTimestamp")
    if not ts_hex:
        raise RuntimeError(f"log missing blockTimestamp (needed for day bucketing): tx={log.get('transactionHash')}")
    return datetime.fromtimestamp(int(ts_hex, 16), tz=timezone.utc).date()


def _addr_topic(addr):
    return "0x" + addr[2:].lower().rjust(64, "0")


# --- S1 output readers ---

def _load_designated_pools():
    path = os.path.join(_G0_DIR, "manifest.csv")
    if not os.path.exists(path):
        raise RuntimeError("out/g0/manifest.csv not found -- gate g1 needs gate g0 to have run first")
    pools = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if row["class"] == "instance" and row["in_circle_38"] == "True":
                pools.append(row["address"].lower())
    if not pools:
        raise RuntimeError("designated-pool set from out/g0/manifest.csv is empty")
    return sorted(pools)


def _load_usdt_destroyed(pre_start_block, pre_end_block):
    path = os.path.join(_G0_DIR, "usdt_events.csv")
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if row["event"] != "DestroyedBlackFunds":
                continue
            blk = int(row["block"])
            if pre_start_block < blk <= pre_end_block:
                rows.append({
                    "address": row["address"].lower(), "block": blk,
                    "amount": int(row["amount_units"]), "date": row["date"],
                })
    return rows


# --- table writers (CSV + schema.json sidecar); a streaming variant for
# wallet_coin_day_pre.csv, which can run to O(10^6) rows -- holding every row
# in memory just to re-sort it (the small-table _write_table pattern from g0)
# is unnecessary since rows are already emitted in (wallet_id, coin,
# day_index) order by construction. ---

SCHEMA_VERSION = 1


def _git_commit():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=_HERE, text=True).strip()
    except Exception:
        return "unknown"


def _schema_dict(columns, primary_key, row_count, input_hashes, sha, notes=None):
    pk = primary_key if isinstance(primary_key, (list, tuple)) else (primary_key,)
    schema = {
        "schema_version": SCHEMA_VERSION,
        "columns": [{"name": c[0], "dtype": c[1], "units": c[2]} for c in columns],
        "primary_key": list(pk),
        "null_policy": "empty string = missing / not applicable",
        "row_count": row_count,
        "input_hashes": input_hashes,
        "producing_commit": _git_commit(),
        "sha256": sha,
    }
    if notes:
        schema["notes"] = notes
    return schema


def _write_schema(name, schema):
    with open(os.path.join(_OUT_DIR, f"{name}.schema.json"), "w") as f:
        json.dump(schema, f, indent=2, sort_keys=True)
        f.write("\n")


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
    _write_schema(name, _schema_dict(columns, primary_key, len(rows_sorted), input_hashes, sha, notes))
    return csv_path, sha, len(rows_sorted)


def _validate_units_columns(csv_path, unit_columns):
    bad = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            for col in unit_columns:
                if not _UNITS_RE.match(row[col]):
                    bad.append(f"{csv_path}:{col}={row[col]!r}")
                    if len(bad) > 5:
                        return bad
    return bad


def _holding_type(usdc_series, usdt_series):
    has_usdc = any(v > 0 for v in usdc_series)
    has_usdt = any(v > 0 for v in usdt_series)
    if any(u > 0 and t > 0 for u, t in zip(usdc_series, usdt_series)):
        return "both"
    if has_usdc:
        # non-overlapping nonzero periods for both coins (never simultaneous)
        # are rare and not "both" per the SPEC's simultaneous-day definition;
        # USDC-first is an arbitrary but deterministic tiebreak.
        return "USDC-only"
    if has_usdt:
        return "USDT-only"
    return "none"


def run(cfg):
    os.makedirs(_OUT_DIR, exist_ok=True)
    if os.path.exists(_RECEIPTS_PATH):
        os.remove(_RECEIPTS_PATH)

    pinned = cfg["blocks"]
    census_start = pinned["census_start"]["block"]
    event_block = pinned["event_block"]["block"]
    # pre_period_start/_end are boundary blocks: the pinned start block is
    # the *last block before* 2022-06-09T00:00:00Z (a snapshot reference,
    # its own date is 2022-06-08); the pinned end block is the last block
    # *on* 2022-08-07 (already inside the window). See module docstring.
    pre_start_block = pinned["pre_period_start"]["block"]
    pre_end_block = pinned["pre_period_end"]["block"]

    checks = []
    all_ranges = []

    designated = _load_designated_pools()
    print(f"[g1] {len(designated)} designated pools (from out/g0/manifest.csv)")

    # --- (a) withdrawal arm ---
    print(f"[g1] (a) withdrawal arm: Withdrawal @ designated pools, {census_start} -> {event_block} on {WIDE}")
    start_line = _line_count(_RECEIPTS_PATH)
    wd_logs = _get_logs_chunked(WIDE, census_start, event_block, address=designated, topics=[[WITHDRAWAL_TOPIC]], gate="g1", window=2_000_000)
    wd_recs = _new_receipts(start_line)
    _assert_component_tiled(wd_recs, census_start, event_block, "withdrawal arm")
    all_ranges.extend(_tag_ranges(wd_recs, designated, [[WITHDRAWAL_TOPIC]]))
    withdrawal_recipients = defaultdict(int)
    for l in wd_logs:
        withdrawal_recipients[_withdrawal_recipient(l)] += 1
    print(f"[g1]   {len(wd_logs)} Withdrawal logs, {len(withdrawal_recipients)} unique recipients")
    checks.append(("withdrawal-arm unique recipients >= 60,629", len(withdrawal_recipients) >= 60629, f"got {len(withdrawal_recipients)}"))

    # --- (b) deposit arm ---
    print(f"[g1] (b) deposit arm: Deposit @ designated pools + EncryptedNote sweep")
    start_line = _line_count(_RECEIPTS_PATH)
    dep_logs = _get_logs_chunked(WIDE, census_start, event_block, address=designated, topics=[[DEPOSIT_TOPIC]], gate="g1", window=2_000_000)
    dep_recs = _new_receipts(start_line)
    _assert_component_tiled(dep_recs, census_start, event_block, "deposit-arm Deposit logs")
    all_ranges.extend(_tag_ranges(dep_recs, designated, [[DEPOSIT_TOPIC]]))
    print(f"[g1]   {len(dep_logs)} designated-pool Deposit logs")

    start_line = _line_count(_RECEIPTS_PATH)
    note_logs = _get_logs_chunked(WIDE, census_start, event_block, address=None, topics=[[ENCRYPTED_NOTE_TOPIC]], gate="g1", window=2_000_000)
    note_recs = _new_receipts(start_line)
    _assert_component_tiled(note_recs, census_start, event_block, "EncryptedNote sweep")
    all_ranges.extend(_tag_ranges(note_recs, None, [[ENCRYPTED_NOTE_TOPIC]]))
    print(f"[g1]   {len(note_logs)} EncryptedNote logs (all instances, topic-only)")

    note_by_tx = {}
    for l in note_logs:
        note_by_tx.setdefault((_log_block(l), l["transactionHash"].lower()), _note_depositor(l))

    deposit_depositors = defaultdict(int)
    residual_rows = []
    for l in dep_logs:
        key = (_log_block(l), l["transactionHash"].lower())
        depositor = note_by_tx.get(key)
        if depositor:
            deposit_depositors[depositor] += 1
        else:
            residual_rows.append({
                "pool": l["address"].lower(), "block": _log_block(l),
                "tx_hash": l["transactionHash"].lower(), "log_index": int(l["logIndex"], 16),
            })
    matched = len(dep_logs) - len(residual_rows)
    resolve_rate = matched / len(dep_logs) if dep_logs else 0.0
    print(f"[g1]   {matched} matched to EncryptedNote ({resolve_rate:.1%}), {len(residual_rows)} unmatched residual (expected ~=45,282)")
    checks.append(("deposit arm resolves >= 70% of designated-pool deposits via EncryptedNote", resolve_rate >= 0.70, f"got {resolve_rate:.4f} ({matched}/{len(dep_logs)})"))

    # --- (c) dedupe / arm assignment ---
    # The zero address is never a real wallet -- USDC/USDT use it as the
    # universal mint/burn sentinel (every mint is Transfer(0x0, to, amt)), so
    # if it ever entered the cohort a pre-period participation query for it
    # would match every mint system-wide. Live run (2026-09-04) found 775/
    # 151,468 designated-pool Withdrawal logs decode `to` (the first,
    # non-indexed data word) as 0x0 -- the recipient really is address(0) on
    # chain (verified: topics[0] matches WITHDRAWAL_TOPIC exactly, data is
    # exactly 3 words as expected, nullifierHash/fee decode to plausible
    # values) -- likely zero-recipient withdrawals from a buggy/test client,
    # not a decode bug here. Excluded as a non-wallet sentinel, same as a
    # contract address is excluded in (d) below.
    zero_wd = sum(1 for a in withdrawal_recipients if a == _ZERO_ADDRESS)
    zero_dep = sum(1 for a in deposit_depositors if a == _ZERO_ADDRESS)
    if zero_wd or zero_dep:
        print(f"[g1]   excluding zero address as a non-wallet sentinel ({zero_wd} withdrawal-arm, {zero_dep} deposit-arm log(s))")
    all_addrs = (set(withdrawal_recipients) | set(deposit_depositors)) - {_ZERO_ADDRESS}
    arm_of = {}
    for a in all_addrs:
        in_wd, in_dep = a in withdrawal_recipients, a in deposit_depositors
        arm_of[a] = "both" if (in_wd and in_dep) else ("withdrawal" if in_wd else "deposit")
    both_arms_set = {a for a, arm in arm_of.items() if arm == "both"}
    print(f"[g1] (c) {len(all_addrs)} deduped addresses across arms ({len(both_arms_set)} in both)")

    # --- (d) contract exclusion ---
    print(f"[g1] (d) contract exclusion: eth_getCode @ {event_block} for {len(all_addrs)} addresses on {OR} (batched)")
    sorted_addrs = sorted(all_addrs)
    codes = _batch_get_code(OR, sorted_addrs, event_block)
    excluded = {a for a, c in codes.items() if c not in (None, "0x", "0x0")}
    cohort_addrs = sorted(all_addrs - excluded)
    print(f"[g1]   excluded {len(excluded)} contract addresses; {len(cohort_addrs)} EOA cohort members remain")
    cohort_set = set(cohort_addrs)
    wallet_id = {a: i for i, a in enumerate(cohort_addrs)}

    # --- (e) pre-period participation: filtered Transfer logs ---
    print(f"[g1] (e) pre-period participation: Transfer logs, {pre_start_block + 1} -> {pre_end_block} on {OR}")
    participation = defaultdict(set)  # addr -> {"USDC","USDT"}
    daily_flow = {"USDC": defaultdict(lambda: defaultdict(int)), "USDT": defaultdict(lambda: defaultdict(int))}
    OR_ARRAY_BATCH = 2000  # <= mevblocker's documented >=2,400 OR-array cap, with margin
    # config/endpoints.json declares mevblocker's block_range_cap as 50,000,
    # but a live probe during this item's build (2026-09-04) found it
    # currently rejects exactly-50,000-block eth_getLogs requests with
    # {-32602, "range 49999 exceeds limit of 10000"} -- the same failure
    # mode g0 hit with tenderly's undocumented result-count cap. Out of this
    # item's touch scope to fix config/endpoints.json, so routed around
    # locally: window at the empirically-safe 10,000 (matches drpc's
    # documented cap) via the same bisecting chunker used for (a)/(b),
    # instead of relying on lib.rpc.get_logs's single-shot auto-split.
    OR_WINDOW = 10_000
    for coin_name, token_addr in COINS.items():
        seen_keys = set()
        n_batches = -(-len(cohort_addrs) // OR_ARRAY_BATCH) or 1
        for position, topic_index in (("from", 1), ("to", 2)):
            for bi in range(0, len(cohort_addrs), OR_ARRAY_BATCH):
                chunk = cohort_addrs[bi:bi + OR_ARRAY_BATCH]
                topic_list = [_addr_topic(a) for a in chunk]
                topics = [TRANSFER_TOPIC, topic_list] if topic_index == 1 else [TRANSFER_TOPIC, None, topic_list]
                start_line = _line_count(_RECEIPTS_PATH)
                logs = _get_logs_chunked(OR, pre_start_block + 1, pre_end_block, address=token_addr, topics=topics, gate="g1", window=OR_WINDOW)
                recs = _new_receipts(start_line)
                _assert_component_tiled(recs, pre_start_block + 1, pre_end_block,
                                         f"pre-period participation {coin_name} {position} batch {bi // OR_ARRAY_BATCH + 1}")
                all_ranges.extend(_tag_ranges(recs, token_addr, topics))
                new = 0
                for l in logs:
                    key = (l["blockHash"], l["transactionHash"], l["logIndex"])
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    new += 1
                    from_addr, to_addr, value = _transfer_decode(l)
                    day = (_log_date(l) - PRE_START_DATE).days
                    if not (0 <= day < N_DAYS):
                        raise AssertionError(f"Transfer log outside pre-period window: {l['transactionHash']} date offset {day}")
                    if from_addr in cohort_set:
                        participation[from_addr].add(coin_name)
                        daily_flow[coin_name][from_addr][day] -= value
                    if to_addr in cohort_set:
                        participation[to_addr].add(coin_name)
                        daily_flow[coin_name][to_addr][day] += value
                print(f"[g1]   {coin_name} {position} batch {bi // OR_ARRAY_BATCH + 1}/{n_batches}: {len(logs)} logs, {new} new")

    participating = sorted(a for a in cohort_addrs if participation[a])
    print(f"[g1]   {len(participating)} participating wallets (>=1 Transfer in either coin during pre-period)")

    # USDT DestroyedBlackFunds (S1's usdt_events.csv) -- applies only where
    # the wallet is already being tracked for USDT via a real Transfer.
    destroyed = _load_usdt_destroyed(pre_start_block, pre_end_block)
    destroyed_applied = 0
    for row in destroyed:
        addr = row["address"]
        if addr not in cohort_set or "USDT" not in participation[addr]:
            continue
        day = (date.fromisoformat(row["date"]) - PRE_START_DATE).days
        if not (0 <= day < N_DAYS):
            continue
        daily_flow["USDT"][addr][day] -= row["amount"]
        destroyed_applied += 1
    print(f"[g1]   applied {destroyed_applied}/{len(destroyed)} in-window USDT DestroyedBlackFunds events")

    # --- (f) daily balances: balanceOf @ pre_period_start (both coins) ---
    print(f"[g1] (f) balanceOf @ {pre_start_block} for {len(participating)} wallets x 2 coins on {OR} (batched)")
    start_balances = {coin_name: _batch_balance_of(OR, token_addr, participating, pre_start_block)
                       for coin_name, token_addr in COINS.items()}

    # --- (g)/(h) per-wallet reconstruction, streamed to wallet_coin_day_pre.csv ---
    rng = random.Random(20260904)
    sample_pairs = set()
    if participating:
        universe = [(a, c) for a in participating for c in COINS]
        sample_pairs = set(rng.sample(universe, min(50, len(universe))))
    sample_end_balance = {}  # (addr, coin) -> reconstructed end-of-window balance
    both_daily = defaultdict(int)  # arm -> both-coin-simultaneously-positive wallet-days (holding_type == both)

    wcdp_columns = [
        ("wallet_id", "int", "dense id, sorted by address"), ("coin", "str", "USDC|USDT"),
        ("date", "str", "UTC YYYY-MM-DD"), ("day_index", "int", "days since pre-period start (2022-06-09), 0-based"),
        ("balance_units", "int", "base units (6 dec)"), ("arm", "str", "withdrawal|deposit|both"),
        ("both_arms", "bool", "address seen in both arms"), ("in_panel", "bool", "nonzero total balance on >=1 pre-period day"),
        ("nonzero", "bool", "balance_units > 0 this day (this coin)"),
    ]
    wcdp_path = os.path.join(_OUT_DIR, "wallet_coin_day_pre.csv")
    wcdp_f = open(wcdp_path, "w", newline="")
    wcdp_w = csv.DictWriter(wcdp_f, fieldnames=[c[0] for c in wcdp_columns])
    wcdp_w.writeheader()
    wcdp_rows_written = 0

    cohort_rows = []
    for addr in cohort_addrs:
        wid = wallet_id[addr]
        arm = arm_of[addr]
        both_arms_flag = addr in both_arms_set
        if addr not in participation or not participation[addr]:
            cohort_rows.append({
                "wallet_id": wid, "address": addr, "arm": arm, "both_arms": both_arms_flag,
                "holding_type": "none", "both_every_day": False, "pre_mean_total_units": 0,
                "nonzero_days": 0, "floor_pass": False, "in_panel": False,
            })
            continue

        series = {}
        for coin_name in COINS:
            bal = start_balances[coin_name].get(addr, 0)
            flows = daily_flow[coin_name].get(addr, {})
            s = []
            for d in range(N_DAYS):
                bal += flows.get(d, 0)
                if bal < 0:
                    raise AssertionError(f"reconstructed negative balance: {addr} {coin_name} day {d} = {bal}")
                s.append(bal)
            series[coin_name] = s
            if (addr, coin_name) in sample_pairs:
                sample_end_balance[(addr, coin_name)] = s[-1]

        usdc_s, usdt_s = series["USDC"], series["USDT"]
        nonzero_days = sum(1 for u, t in zip(usdc_s, usdt_s) if u > 0 or t > 0)
        in_panel = nonzero_days >= 1
        holding_type = _holding_type(usdc_s, usdt_s) if in_panel else "none"
        both_every_day = in_panel and all((u > 0 and t > 0) or (u == 0 and t == 0) for u, t in zip(usdc_s, usdt_s))
        pre_mean_total_units = sum(u + t for u, t in zip(usdc_s, usdt_s)) // N_DAYS
        floor_pass = pre_mean_total_units >= FLOOR_MIN_UNITS and nonzero_days >= FLOOR_MIN_NONZERO_DAYS
        if holding_type == "both":
            both_daily[arm] += sum(1 for u, t in zip(usdc_s, usdt_s) if u > 0 and t > 0)

        cohort_rows.append({
            "wallet_id": wid, "address": addr, "arm": arm, "both_arms": both_arms_flag,
            "holding_type": holding_type, "both_every_day": both_every_day,
            "pre_mean_total_units": pre_mean_total_units, "nonzero_days": nonzero_days,
            "floor_pass": floor_pass, "in_panel": in_panel,
        })

        for coin_name, s in series.items():
            for d, bal in enumerate(s):
                wcdp_w.writerow({
                    "wallet_id": wid, "coin": coin_name, "date": (PRE_START_DATE + timedelta(days=d)).isoformat(),
                    "day_index": d, "balance_units": bal, "arm": arm, "both_arms": both_arms_flag,
                    "in_panel": in_panel, "nonzero": bal > 0,
                })
                wcdp_rows_written += 1

    wcdp_f.close()
    with open(wcdp_path, "rb") as f:
        wcdp_sha = hashlib.sha256(f.read()).hexdigest()
    _write_schema("wallet_coin_day_pre", _schema_dict(
        wcdp_columns, ("wallet_id", "coin", "date"), wcdp_rows_written,
        input_hashes=[r["sha256"] for r in (wd_recs + dep_recs + note_recs)],
        sha=wcdp_sha,
        notes="one row per (wallet_id, coin, day) for every participating wallet, dense over all "
              f"{N_DAYS} pre-period days; non-participating cohort members have no rows here "
              "(see cohort.csv in_panel=False).",
    ))

    # --- sanity check: day-end reconstructed balance == fresh balanceOf @ pre_period_end ---
    print(f"[g1]   sanity check: {len(sample_pairs)} random (wallet,coin) pairs, day-end vs fresh balanceOf @ {pre_end_block}")
    by_coin = defaultdict(list)
    for a, c in sample_pairs:
        by_coin[c].append(a)
    fresh_end = {c: _batch_balance_of(OR, COINS[c], addrs, pre_end_block) for c, addrs in by_coin.items()}
    mismatches = []
    for a, c in sample_pairs:
        recon, fresh = sample_end_balance[(a, c)], fresh_end[c][a]
        if recon != fresh:
            mismatches.append(f"{a} {c}: reconstructed={recon} fresh={fresh}")
    checks.append(("day-end reconstructed balance == balanceOf @ pre_period_end, random 50, tolerance 0",
                    not mismatches, "; ".join(mismatches) or f"ok ({len(sample_pairs)} pairs)"))

    # --- cross-endpoint reverification (5%, min 5, of all range fetches this gate) ---
    print(f"[g1] cross-endpoint reverification (5%, min 5, of {len(all_ranges)} ranges)")
    reverified = _cross_endpoint_reverify(all_ranges)
    print(f"[g1]   reverified {len(reverified)} ranges")

    # --- write cohort.csv + deposit_residual.csv ---
    cohort_columns = [
        ("wallet_id", "int", "dense id, sorted by address"), ("address", "str", "0x, 42 chars, lowercase"),
        ("arm", "str", "withdrawal|deposit|both"), ("both_arms", "bool", "address seen in both arms"),
        ("holding_type", "str", "USDC-only|USDT-only|both|none"),
        ("both_every_day", "bool", "every nonzero pre-period day held both coins simultaneously"),
        ("pre_mean_total_units", "int", "mean over all pre-period days of (USDC+USDT) base units, floor div"),
        ("nonzero_days", "int", "pre-period days with nonzero total balance"),
        ("floor_pass", "bool", f"pre_mean_total_units >= {FLOOR_MIN_UNITS} and nonzero_days >= {FLOOR_MIN_NONZERO_DAYS}"),
        ("in_panel", "bool", "nonzero total balance on >=1 pre-period day"),
    ]
    cohort_path, cohort_sha, cohort_n = _write_table(
        "cohort", cohort_rows, cohort_columns, "wallet_id",
        input_hashes=[r["sha256"] for r in (wd_recs + dep_recs + note_recs)],
    )

    residual_columns = [
        ("pool", "str", "0x, 42 chars, lowercase, designated pool"), ("block", "int", "Deposit log block"),
        ("tx_hash", "str", "0x, 66 chars, lowercase"), ("log_index", "int", "within-tx log index"),
    ]
    residual_path, residual_sha, residual_n = _write_table(
        "deposit_residual", residual_rows, residual_columns, ("tx_hash", "log_index"),
        input_hashes=[r["sha256"] for r in dep_recs],
        notes="Designated-pool Deposit logs with no matching EncryptedNote in the same tx -- "
              "pre-EncryptedNote-feature deposits (~pre-Apr-2021). No depositor identity is "
              "recoverable from on-chain data alone; deferred, not fetched further (per SPEC).",
    )

    # --- validation checks ---
    unit_issues = _validate_units_columns(cohort_path, ["pre_mean_total_units"]) + _validate_units_columns(wcdp_path, ["balance_units"])
    checks.append(("every *_units column matches ^-?[0-9]+$", not unit_issues, "; ".join(unit_issues) or "ok"))
    sidecars_ok = all(os.path.exists(os.path.join(_OUT_DIR, f"{n}.schema.json")) for n in ("cohort", "wallet_coin_day_pre", "deposit_residual"))
    checks.append(("schema.json sidecars present", sidecars_ok, "ok" if sidecars_ok else "missing sidecar(s)"))

    # --- crosstab.md ---
    def _crosstab(rows):
        t = defaultdict(int)
        for r in rows:
            t[(r["arm"], r["holding_type"])] += 1
        return t

    in_panel_rows = [r for r in cohort_rows if r["in_panel"]]
    floor_rows = [r for r in cohort_rows if r["floor_pass"]]
    arms = ["withdrawal", "deposit", "both"]
    holding_types = ["USDC-only", "USDT-only", "both", "none"]

    def _table_lines(rows, title):
        t = _crosstab(rows)
        lines = [f"### {title}", "", "| arm | " + " | ".join(holding_types) + " | total |", "|---|" + "---|" * (len(holding_types) + 1)]
        for arm in arms:
            counts = [t.get((arm, ht), 0) for ht in holding_types]
            lines.append(f"| {arm} | " + " | ".join(str(c) for c in counts) + f" | {sum(counts)} |")
        lines.append(f"| **total** | " + " | ".join(str(sum(t.get((a, ht), 0) for a in arms)) for ht in holding_types)
                      + f" | **{len(rows)}** |")
        lines.append("")
        return lines

    floor_pooled = len(floor_rows)
    if floor_pooled < KILL_THRESHOLD:
        verdict = "KILL"
    elif floor_pooled < TARGET_THRESHOLD:
        verdict = "PASS"
    else:
        verdict = "TARGET"

    crosstab_lines = [
        "# Gate 1 -- exposed cohort crosstab",
        "",
        f"Cohort (post-exclusion): {len(cohort_addrs)}. Participating (>=1 pre-period Transfer): {len(participating)}.",
        f"In-panel (nonzero balance >=1 pre-period day): {len(in_panel_rows)}. Floor-passing: {len(floor_rows)}.",
        "",
        *_table_lines(in_panel_rows, "arm x holding_type -- all in-panel"),
        *_table_lines(floor_rows, "arm x holding_type -- floor-passing"),
        "### both-coin cell, daily resolution",
        "",
        "Wallet-days (per participating wallet, per day) where both USDC and USDT were simultaneously "
        "positive, summed across the pre-period, for holding_type=both wallets, by arm:",
        "",
        "| arm | both-coin wallet-days |", "|---|---|",
    ]
    for arm in arms:
        crosstab_lines.append(f"| {arm} | {both_daily.get(arm, 0)} |")
    crosstab_lines += [
        "",
        f"**Kill rule: floor-passing in-panel exposed wallets (both arms pooled) = {floor_pooled} "
        f"-- KILL if < {KILL_THRESHOLD}, PASS if >= {KILL_THRESHOLD}, TARGET if >= {TARGET_THRESHOLD}. "
        f"Result: {verdict}.**",
    ]
    with open(os.path.join(_OUT_DIR, "crosstab.md"), "w") as f:
        f.write("\n".join(crosstab_lines) + "\n")

    # --- summary.md ---
    passed = sum(1 for _, ok, _ in checks if ok)
    failed = [(n, d) for n, ok, d in checks if not ok]
    summary_lines = [
        "# Gate 1 -- exposed cohort by arm x holding type, floor, kill rule",
        "",
        f"Withdrawal arm: {len(wd_logs)} logs, {len(withdrawal_recipients)} unique recipients.",
        f"Deposit arm: {len(dep_logs)} designated-pool Deposit logs, {len(note_logs)} EncryptedNote logs (all instances), "
        f"{matched} matched ({resolve_rate:.1%}), {len(residual_rows)} unmatched residual.",
        f"Deduped cohort: {len(all_addrs)}; excluded {len(excluded)} contract addresses; {len(cohort_addrs)} EOA cohort members.",
        f"Participating (>=1 pre-period Transfer): {len(participating)}. USDT DestroyedBlackFunds applied: {destroyed_applied}.",
        f"In-panel: {len(in_panel_rows)}. Floor-passing (pooled): {floor_pooled} -- {verdict}.",
        f"Cross-endpoint reverification: {len(reverified)} ranges re-fetched and matched.",
        "",
        "## Acceptance checks", "", "| check | result | detail |", "|---|---|---|",
    ]
    for name, ok, detail in checks:
        summary_lines.append(f"| {name} | {'PASS' if ok else 'FAIL'} | {detail} |")
    summary_lines += [
        "", f"{passed}/{len(checks)} checks passed.", "",
        "## Outputs", "",
        f"- `cohort.csv` ({cohort_n} rows, sha256 `{cohort_sha}`)",
        f"- `wallet_coin_day_pre.csv` ({wcdp_rows_written} rows, sha256 `{wcdp_sha}`)",
        f"- `deposit_residual.csv` ({residual_n} rows, sha256 `{residual_sha}`)",
        "- `crosstab.md`",
    ]
    with open(os.path.join(_OUT_DIR, "summary.md"), "w") as f:
        f.write("\n".join(summary_lines) + "\n")

    print(f"[g1] {passed}/{len(checks)} acceptance checks passed")
    if failed:
        for name, detail in failed:
            print(f"[g1] FAIL: {name} -- {detail}")
        return 1
    return 0
