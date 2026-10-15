"""Gate 2 -- reconstruction integrity. Built by S4.

(i) Coin-level supply invariant: for each coin and each of 5 random 1,000-block
    windows in the pre-period plus the fixed window 15,365,400-15,365,600,
    Delta totalSupply (two eth_calls) vs net zero-address Transfer flow (mint
    minus burn, fetched narrowly via a from=0x0 topic filter and a to=0x0
    topic filter -- never a full unfiltered Transfer scan of the window).
    USDC must match to the unit in every window. USDT's older contract mints
    via issue() and burns via redeem()/destroyBlackFunds() WITHOUT emitting a
    Transfer log at all (a documented quirk of the real Tether contract, not
    a bug here) -- so the raw Transfer-only invariant is expected to miss
    those windows; adding Issue/Redeem/DestroyedBlackFunds from S1's
    out/g0/usdt_events.csv must then match exactly.
(ii) Freeze intersection: S2's out/g1/cohort.csv addresses intersected with
    the full lifetime Circle `Blacklisted` set (out/g0/circle_events.csv) and
    USDT's `AddedBlackList`/`DestroyedBlackFunds` addresses (out/g0/
    usdt_events.csv) -> out/g2/frozen_in_cohort.csv with per-event dates.
    Flagged, never dropped -- and never merged back into cohort.csv, which is
    out of this item's Touches scope (see the deviation note in the project spec).
(iii) Canary + stratified check: 10 cohort wallets, stratified by {coin,
    zero/nonzero, high/low activity}, reconstructed independently (this
    gate's own balanceOf + Transfer-log walk, not S2's persisted numbers) at
    3 shared pre-period blocks each (30 checks, tolerance 0); plus the
    canary 0x5cb182a51...425f5 at block 15,365,491, where a naive
    Transfer-only reconstruction must disagree with the on-chain balance (no
    Transfer log fires for a destroyBlackFunds() wipe -- verified directly by
    probing for one at that exact block -- so the naive reconstruction is
    exactly balanceOf(block-1)) and the corrected one (subtracting S1's
    DestroyedBlackFunds amount) must agree.
(iv) OR-array requests are capped and asserted against the endpoint's
    documented cap before sending (_assert_or_cap).

Every range fetch goes through lib.rpc.get_logs (receipts + retry + tiling
built in); point queries (eth_call/totalSupply/balanceOf) are gate-scoped
local retry helpers, same precedent as g0/g1 (lib/rpc.py is out of this
item's touch scope). Tiling is asserted per logical component (each window,
each mint/burn leg, each stratified from/to fetch) via
_assert_component_tiled -- the shared receipts.jsonl file holds many
non-contiguous, sometimes-overlapping-by-filter spans, so whole-file tiling
only makes sense scoped to one component at a time (same pattern as g0/g1).
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
from collections import defaultdict

from lib import keccak, rpc

_HERE = os.path.dirname(os.path.abspath(__file__))
_OUT_DIR = os.path.join(_HERE, "..", "out", "g2")
_G0_DIR = os.path.join(_HERE, "..", "out", "g0")
_G1_DIR = os.path.join(_HERE, "..", "out", "g1")
_RECEIPTS_PATH = os.path.join(_OUT_DIR, "receipts.jsonl")

USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
COINS = {"USDC": USDC, "USDT": USDT}
_ZERO_ADDRESS = "0x" + "0" * 40

WIDE = "tenderly"  # wide-range / OR-array topic-filtered eth_getLogs
NARROW = "drpc"    # eth_call archive state + small filtered getLogs

TRANSFER_TOPIC = keccak.topic("Transfer(address,address,uint256)")
TOTAL_SUPPLY_SELECTOR = keccak.selector("totalSupply()")
BALANCE_OF_SELECTOR = keccak.selector("balanceOf(address)")


def _addr_topic(addr):
    return "0x" + addr[2:].lower().rjust(64, "0")


ZERO_TOPIC = _addr_topic(_ZERO_ADDRESS)


def _cross_check_topics():
    assert TRANSFER_TOPIC == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef", TRANSFER_TOPIC


_cross_check_topics()

CANARY_ADDR = "0x5cb182a51c452937abecc9e81a9663efbeb425f5"
CANARY_BLOCK = 15365491
CANARY_EXPECTED_UNITS = 1199336943238  # 1,199,336.943238 USDT, per SPEC

_NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, OSError)
_UNITS_RE = re.compile(r"^-?[0-9]+$")
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


def _hex_to_int(h):
    if h in (None, "0x", "0x0", ""):
        return 0
    return int(h, 16)


def _eth_call_raw(endpoint, to, data, block, max_retries=4, backoff_s=0.5):
    last_err = None
    for attempt in range(max_retries):
        try:
            _throttle(endpoint)
            return rpc.call(endpoint, to, data, block)
        except rpc.RpcError as e:
            raise RuntimeError(f"eth_call {to} {data}@{block} -> {e}")
        except _NETWORK_ERRORS as e:
            last_err = e
            time.sleep(backoff_s * (2 ** attempt))
    raise RuntimeError(f"eth_call {to} {data}@{block} failed after {max_retries} retries: {last_err}")


def _total_supply(endpoint, token, block):
    return _hex_to_int(_eth_call_raw(endpoint, token, TOTAL_SUPPLY_SELECTOR, block))


def _balance_of_data(addr):
    return BALANCE_OF_SELECTOR + addr[2:].lower().rjust(64, "0")


def _balance_of(endpoint, token, addr, block):
    return _hex_to_int(_eth_call_raw(endpoint, token, _balance_of_data(addr), block))


def _assert_or_cap(endpoint, items):
    cap = rpc.ENDPOINTS[endpoint]["or_array_cap"]
    if len(items) > cap:
        raise AssertionError(f"OR-array of {len(items)} items exceeds {endpoint}'s cap of {cap}")


# --- receipts helpers (scoped tiling assertion; same pattern as g0/g1) ---

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
    """Same rationale as g0/g1: replay through lib.rpc.get_logs (re-splits
    per the replay endpoint's own cap, uses the real address/topics filter)
    rather than lib.rpc.reverify's raw single-shot replay."""
    import math

    n = min(max(min_n, math.ceil(len(all_ranges) * fraction)), len(all_ranges))
    sample = all_ranges[:n]  # deterministic: first n by fetch order (this gate's ranges are all small)
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


# --- log decoding ---

def _log_block(log):
    return int(log["blockNumber"], 16)


def _transfer_decode(log):
    from_addr = "0x" + log["topics"][1][-40:].lower()
    to_addr = "0x" + log["topics"][2][-40:].lower()
    value = _hex_to_int(log["data"])
    return from_addr, to_addr, value


# --- S0/S1/S2 output readers ---

def _load_cohort():
    path = os.path.join(_G1_DIR, "cohort.csv")
    if not os.path.exists(path):
        raise RuntimeError("out/g1/cohort.csv not found -- gate g2 needs gate g1 to have run first")
    rows = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            rows.append({
                "wallet_id": int(row["wallet_id"]), "address": row["address"].lower(),
                "arm": row["arm"], "holding_type": row["holding_type"],
                "pre_mean_total_units": int(row["pre_mean_total_units"]),
                "nonzero_days": int(row["nonzero_days"]),
                "floor_pass": row["floor_pass"] == "True", "in_panel": row["in_panel"] == "True",
            })
    return rows


def _load_usdt_events():
    path = os.path.join(_G0_DIR, "usdt_events.csv")
    if not os.path.exists(path):
        raise RuntimeError("out/g0/usdt_events.csv not found -- gate g2 needs gate g0 to have run first")
    rows = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            rows.append({
                "address": row["address"].lower() if row["address"] else "",
                "event": row["event"], "block": int(row["block"]), "date": row["date"],
                "tx_hash": row["tx_hash"], "log_index": int(row["log_index"]),
                "amount_units": int(row["amount_units"]) if row["amount_units"] else None,
            })
    return rows


def _load_circle_blacklisted():
    path = os.path.join(_G0_DIR, "circle_events.csv")
    if not os.path.exists(path):
        raise RuntimeError("out/g0/circle_events.csv not found -- gate g2 needs gate g0 to have run first")
    rows = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if row["event"] != "Blacklisted":
                continue
            rows.append({
                "address": row["address"].lower(), "block": int(row["block"]), "date": row["date"],
                "tx_hash": row["tx_hash"], "log_index": int(row["log_index"]),
            })
    return rows


# --- (i) coin-level supply invariant ---

def _pick_windows(pre_start_block, pre_end_block, seed=20260905, n=5, width=1000):
    rng = random.Random(seed)
    lo, hi = pre_start_block, pre_end_block - width + 1
    windows = []
    for i in range(n):
        start = rng.randint(lo, hi)
        windows.append((start, start + width - 1, f"random-{i + 1}"))
    windows.append((15365400, 15365600, "fixed-canary-window"))
    return windows


def _fetch_zero_leg(token, start, end, topics, gate, label):
    start_line = _line_count(_RECEIPTS_PATH)
    logs = rpc.get_logs(NARROW, start, end, address=token, topics=topics, gate=gate)
    recs = _new_receipts(start_line)
    _assert_component_tiled(recs, start, end, label)
    return logs, recs


def _net_mint_burn(token, start, end, gate, label):
    """Net zero-address Transfer flow over [start, end]: sum of mints
    (from=0x0) minus sum of burns (to=0x0). Fetched via two topic-filtered
    queries (never an unfiltered Transfer scan of the window -- USDC/USDT
    transfer volume in a 1,000-block window can be large; the zero-address
    leg is not)."""
    mint_logs, mint_recs = _fetch_zero_leg(token, start, end, [TRANSFER_TOPIC, ZERO_TOPIC, None], gate, f"{label} mint")
    burn_logs, burn_recs = _fetch_zero_leg(token, start, end, [TRANSFER_TOPIC, None, ZERO_TOPIC], gate, f"{label} burn")
    minted = sum(_hex_to_int(l["data"]) for l in mint_logs)
    burned = sum(_hex_to_int(l["data"]) for l in burn_logs)
    ranges = (_tag_ranges(mint_recs, token, [TRANSFER_TOPIC, ZERO_TOPIC, None])
              + _tag_ranges(burn_recs, token, [TRANSFER_TOPIC, None, ZERO_TOPIC]))
    return minted - burned, ranges


def _usdt_correction(usdt_events, start, end):
    """Sum of Issue - Redeem - DestroyedBlackFunds within [start, end] --
    USDT's issue()/redeem()/destroyBlackFunds() move totalSupply WITHOUT a
    Transfer log, so this is exactly what the raw Transfer-only invariant is
    missing."""
    total = 0
    for r in usdt_events:
        if not (start <= r["block"] <= end):
            continue
        if r["event"] == "Issue":
            total += r["amount_units"]
        elif r["event"] == "Redeem":
            total -= r["amount_units"]
        elif r["event"] == "DestroyedBlackFunds":
            total -= r["amount_units"]
    return total


# --- (iii) stratified reconciliation ---

def _select_stratified_wallets(cohort_rows, n=10, seed=20260905):
    """Stratify by {coin, zero/nonzero, high/low activity}: for each coin,
    2 wallets from the high-activity half of in-panel holders of that coin,
    2 from the low-activity half, and 1 non-participating (in_panel=False,
    zero throughout) wallet -- 5 x 2 coins = 10. "zero/nonzero" and
    "high/low activity" are read straight off cohort.csv's own in_panel /
    nonzero_days columns (not re-derived here); the reconciliation below
    still independently reconstructs from raw balanceOf + Transfer logs, so
    this selection choice does not weaken the check -- see run()."""
    rng = random.Random(seed)
    picks, seen = [], set()

    def add(row, coin):
        key = (row["wallet_id"], coin)
        if key in seen:
            return
        seen.add(key)
        picks.append((row, coin))

    for coin in ("USDC", "USDT"):
        nonzero = [r for r in cohort_rows if r["in_panel"] and r["holding_type"] in (f"{coin}-only", "both")]
        zero = [r for r in cohort_rows if not r["in_panel"]]
        if not nonzero:
            raise RuntimeError(f"no in-panel {coin} cohort wallets available to stratify on")
        days_sorted = sorted(r["nonzero_days"] for r in nonzero)
        median_days = days_sorted[len(days_sorted) // 2]
        high = [r for r in nonzero if r["nonzero_days"] >= median_days]
        low = [r for r in nonzero if r["nonzero_days"] < median_days] or high
        for bucket in (high, low):
            for r in rng.sample(bucket, min(2, len(bucket))):
                add(r, coin)
        if zero:
            add(rng.choice(zero), coin)

    if len(picks) < n:
        for r in sorted(cohort_rows, key=lambda r: r["wallet_id"]):
            if len(picks) >= n:
                break
            coin = "USDT" if r["holding_type"] == "USDT-only" else "USDC"
            add(r, coin)
    return picks[:n]


def _canary_check(usdt_events, all_ranges):
    """Verify no Transfer log exists for the canary address at CANARY_BLOCK
    itself -- only then is balanceOf(CANARY_BLOCK - 1) provably equal to what
    a pure Transfer-log-walk ("naive") reconstruction would show at
    CANARY_BLOCK (no event at that block to apply, so the walk carries the
    prior value forward unchanged)."""
    addr_topic = _addr_topic(CANARY_ADDR)
    for label, topics in (("canary from-check", [TRANSFER_TOPIC, addr_topic]),
                           ("canary to-check", [TRANSFER_TOPIC, None, addr_topic])):
        start_line = _line_count(_RECEIPTS_PATH)
        logs = rpc.get_logs(NARROW, CANARY_BLOCK, CANARY_BLOCK, address=USDT, topics=topics, gate="g2")
        recs = _new_receipts(start_line)
        _assert_component_tiled(recs, CANARY_BLOCK, CANARY_BLOCK, label)
        all_ranges.extend(_tag_ranges(recs, USDT, topics))
        if logs:
            raise RuntimeError(
                f"canary {CANARY_ADDR} has a Transfer log at block {CANARY_BLOCK} -- the "
                "balanceOf(block-1) shortcut for the naive reconstruction no longer holds"
            )

    naive = _balance_of(NARROW, USDT, CANARY_ADDR, CANARY_BLOCK - 1)
    actual = _balance_of(NARROW, USDT, CANARY_ADDR, CANARY_BLOCK)
    destroy_rows = [r for r in usdt_events if r["event"] == "DestroyedBlackFunds"
                    and r["address"] == CANARY_ADDR and r["block"] == CANARY_BLOCK]
    if not destroy_rows:
        raise RuntimeError(f"no DestroyedBlackFunds row for canary {CANARY_ADDR} at block {CANARY_BLOCK}")
    destroyed = destroy_rows[0]["amount_units"]
    corrected = naive - destroyed
    return {
        "naive": naive, "actual": actual, "destroyed": destroyed, "corrected": corrected,
        "naive_matches_expected": naive == CANARY_EXPECTED_UNITS,
        "naive_fails": naive != actual,
        "corrected_passes": corrected == actual,
    }


# --- table writer (single small table; same pattern as g0/g1) ---

SCHEMA_VERSION = 1


def _git_commit():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=_HERE, text=True).strip()
    except Exception:
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


def run(cfg):
    os.makedirs(_OUT_DIR, exist_ok=True)
    if os.path.exists(_RECEIPTS_PATH):
        os.remove(_RECEIPTS_PATH)

    pinned = cfg["blocks"]
    pre_start_block = pinned["pre_period_start"]["block"]
    pre_end_block = pinned["pre_period_end"]["block"]

    checks = []
    all_ranges = []

    usdt_events = _load_usdt_events()
    circle_blacklisted = _load_circle_blacklisted()
    cohort_rows = _load_cohort()

    # --- (i) coin-level supply invariant ---
    windows = _pick_windows(pre_start_block, pre_end_block)
    print(f"[g2] (i) supply invariant: {len(windows)} windows x 2 coins")
    supply_rows = []
    for coin_name, token_addr in COINS.items():
        for start, end, label in windows:
            supply_before = _total_supply(NARROW, token_addr, start - 1)
            supply_after = _total_supply(NARROW, token_addr, end)
            delta_supply = supply_after - supply_before
            net_raw, ranges = _net_mint_burn(token_addr, start, end, "g2", f"{coin_name} {label}")
            all_ranges.extend(ranges)
            raw_match = delta_supply == net_raw
            if coin_name == "USDT":
                correction = _usdt_correction(usdt_events, start, end)
                net_corrected = net_raw + correction
                corrected_match = delta_supply == net_corrected
            else:
                net_corrected, corrected_match = net_raw, raw_match
            supply_rows.append({
                "coin": coin_name, "window": label, "from_block": start, "to_block": end,
                "delta_supply": delta_supply, "net_raw": net_raw, "raw_match": raw_match,
                "net_corrected": net_corrected, "corrected_match": corrected_match,
            })
            print(f"[g2]   {coin_name} {label} [{start},{end}]: delta={delta_supply} raw_net={net_raw} "
                  f"({'match' if raw_match else 'MISMATCH'}){'' if coin_name == 'USDC' else f' corrected_net={net_corrected} ' + ('match' if corrected_match else 'MISMATCH')}")

    usdc_rows = [r for r in supply_rows if r["coin"] == "USDC"]
    usdt_rows = [r for r in supply_rows if r["coin"] == "USDT"]
    usdc_ok = all(r["raw_match"] for r in usdc_rows)
    usdt_corrected_ok = all(r["corrected_match"] for r in usdt_rows)
    usdt_raw_fails = sum(1 for r in usdt_rows if not r["raw_match"])
    checks.append((f"USDC supply invariant matches in all {len(usdc_rows)} windows", usdc_ok,
                    f"{sum(r['raw_match'] for r in usdc_rows)}/{len(usdc_rows)}"))
    checks.append((f"USDT supply invariant matches (after Issue/Redeem/DestroyedBlackFunds correction) in all {len(usdt_rows)} windows",
                    usdt_corrected_ok, f"{sum(r['corrected_match'] for r in usdt_rows)}/{len(usdt_rows)}"))
    checks.append(("USDT raw (Transfer-only) invariant fails in >=1 window before correction", usdt_raw_fails >= 1,
                    f"{usdt_raw_fails}/{len(usdt_rows)} raw windows failed"))

    # --- (ii) freeze intersection ---
    print("[g2] (ii) freeze intersection: cohort x (Circle Blacklisted lifetime, USDT AddedBlackList/DestroyedBlackFunds)")
    cohort_by_addr = {r["address"]: r for r in cohort_rows}
    freeze_events = (
        [{"address": r["address"], "event": "Blacklisted", "block": r["block"], "date": r["date"],
          "tx_hash": r["tx_hash"], "log_index": r["log_index"]} for r in circle_blacklisted]
        + [{"address": r["address"], "event": r["event"], "block": r["block"], "date": r["date"],
            "tx_hash": r["tx_hash"], "log_index": r["log_index"]}
           for r in usdt_events if r["event"] in ("AddedBlackList", "DestroyedBlackFunds")]
    )
    frozen_rows = []
    for ev in freeze_events:
        row = cohort_by_addr.get(ev["address"])
        if row is None:
            continue
        frozen_rows.append({
            "wallet_id": row["wallet_id"], "address": ev["address"], "event": ev["event"],
            "block": ev["block"], "date": ev["date"], "tx_hash": ev["tx_hash"], "log_index": ev["log_index"],
        })
    frozen_addr_count = len({r["address"] for r in frozen_rows})
    print(f"[g2]   {len(frozen_rows)} freeze events touch {frozen_addr_count} distinct cohort addresses")

    # --- (iii) stratified reconciliation + canary ---
    stratified_picks = _select_stratified_wallets(cohort_rows)
    wallet_addrs = sorted({row["address"] for row, _ in stratified_picks})
    print(f"[g2] (iii) stratified reconciliation: {len(stratified_picks)} (wallet,coin) pairs, {len(wallet_addrs)} addresses")
    _assert_or_cap(WIDE, wallet_addrs)
    addr_topics = [_addr_topic(a) for a in wallet_addrs]

    flow_by_addr_coin = {}
    for coin_name, token_addr in COINS.items():
        combined_logs = []
        for position, topics in (("from", [TRANSFER_TOPIC, addr_topics]), ("to", [TRANSFER_TOPIC, None, addr_topics])):
            start_line = _line_count(_RECEIPTS_PATH)
            logs = rpc.get_logs(WIDE, pre_start_block + 1, pre_end_block, address=token_addr, topics=topics, gate="g2")
            recs = _new_receipts(start_line)
            _assert_component_tiled(recs, pre_start_block + 1, pre_end_block, f"stratified {coin_name} {position}")
            all_ranges.extend(_tag_ranges(recs, token_addr, topics))
            combined_logs.extend(logs)
        d = defaultdict(lambda: defaultdict(int))
        for l in combined_logs:
            frm, to, val = _transfer_decode(l)
            blk = _log_block(l)
            if frm in wallet_addrs:
                d[frm][blk] -= val
            if to in wallet_addrs:
                d[to][blk] += val
        for a in wallet_addrs:
            flow_by_addr_coin[(a, coin_name)] = dict(d.get(a, {}))

    target_blocks = sorted(random.Random(20260906).sample(range(pre_start_block + 1, pre_end_block + 1), 3))
    print(f"[g2]   3 shared target blocks: {target_blocks}")

    recon_rows = []
    for row, coin in stratified_picks:
        addr, token_addr = row["address"], COINS[coin]
        pre_start_bal = _balance_of(NARROW, token_addr, addr, pre_start_block)
        flows = flow_by_addr_coin[(addr, coin)]
        sorted_blocks = sorted(flows)
        for target in target_blocks:
            running = pre_start_bal
            for blk in sorted_blocks:
                if blk > target:
                    break
                running += flows[blk]
            if coin == "USDT":
                for ev in usdt_events:
                    if ev["event"] == "DestroyedBlackFunds" and ev["address"] == addr and pre_start_block < ev["block"] <= target:
                        running -= ev["amount_units"]
            if running < 0:
                raise AssertionError(f"reconstructed negative balance: {addr} {coin}@{target} = {running}")
            fresh = _balance_of(NARROW, token_addr, addr, target)
            recon_rows.append({
                "wallet_id": row["wallet_id"], "address": addr, "coin": coin, "block": target,
                "reconstructed": running, "fresh": fresh, "match": running == fresh,
            })

    stratified_matches = sum(1 for r in recon_rows if r["match"])
    stratified_ok = stratified_matches == len(recon_rows) == 30
    checks.append((f"stratified reconciliation {len(recon_rows)}/30 reconciled, tolerance 0", stratified_ok,
                    f"{stratified_matches}/{len(recon_rows)} matched"
                    + ("" if stratified_ok else f"; mismatches: {[r for r in recon_rows if not r['match']][:3]}")))

    canary = _canary_check(usdt_events, all_ranges)
    canary_ok = canary["naive_matches_expected"] and canary["naive_fails"] and canary["corrected_passes"]
    checks.append((
        "canary: naive Transfer-only reconstruction fails vs balanceOf, corrected reconstruction passes",
        canary_ok,
        f"naive={canary['naive']} (expected {CANARY_EXPECTED_UNITS}) actual={canary['actual']} corrected={canary['corrected']}",
    ))

    checks.append((f"OR-array request ({len(wallet_addrs)} addresses) within {WIDE}'s cap", True,
                    f"{len(wallet_addrs)} <= {rpc.ENDPOINTS[WIDE]['or_array_cap']}"))

    # --- cross-endpoint reverification (5%, min 5, of all range fetches this gate) ---
    print(f"[g2] cross-endpoint reverification (5%, min 5, of {len(all_ranges)} ranges)")
    reverified = _cross_endpoint_reverify(all_ranges)
    print(f"[g2]   reverified {len(reverified)} ranges")

    # --- write frozen_in_cohort.csv ---
    frozen_columns = [
        ("wallet_id", "int", "S2 cohort dense id"), ("address", "str", "0x, 42 chars, lowercase"),
        ("event", "str", "Blacklisted|AddedBlackList|DestroyedBlackFunds"),
        ("block", "int", "event block"), ("date", "str", "UTC YYYY-MM-DD"),
        ("tx_hash", "str", "0x, 66 chars, lowercase"), ("log_index", "int", "within-tx log index"),
    ]
    frozen_path, frozen_sha, frozen_n = _write_table(
        "frozen_in_cohort", frozen_rows, frozen_columns, ("tx_hash", "log_index"),
        input_hashes=[],
        notes="S2 cohort addresses (out/g1/cohort.csv) intersected with the lifetime Circle "
              "Blacklisted set (out/g0/circle_events.csv) and USDT AddedBlackList/DestroyedBlackFunds "
              "(out/g0/usdt_events.csv). Flagging only -- no cohort row is dropped, and cohort.csv "
              "itself is not modified (out of this item's Touches scope; join on wallet_id/address).",
    )

    # --- validation checks ---
    unit_issues = _validate_units_columns(frozen_path, [c[0] for c in frozen_columns if c[0].endswith("_units")])
    checks.append(("every *_units column matches ^-?[0-9]+$", not unit_issues, "; ".join(unit_issues) or "ok (no *_units column in frozen_in_cohort.csv)"))
    sidecar_ok = os.path.exists(os.path.join(_OUT_DIR, "frozen_in_cohort.schema.json"))
    checks.append(("schema.json sidecar present", sidecar_ok, "ok" if sidecar_ok else "missing"))

    # --- summary.md ---
    passed = sum(1 for _, ok, _ in checks if ok)
    failed = [(n, d) for n, ok, d in checks if not ok]
    all_pass = not failed

    summary_lines = [
        "# Gate 2 -- reconstruction integrity",
        "",
        "## (i) Coin-level supply invariant",
        "",
        "| coin | window | blocks | delta_supply | net_raw | raw_match | net_corrected | corrected_match |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in supply_rows:
        summary_lines.append(
            f"| {r['coin']} | {r['window']} | [{r['from_block']},{r['to_block']}] | {r['delta_supply']} | "
            f"{r['net_raw']} | {'PASS' if r['raw_match'] else 'FAIL'} | {r['net_corrected']} | "
            f"{'PASS' if r['corrected_match'] else 'FAIL'} |"
        )
    summary_lines += [
        "",
        "## (ii) Freeze intersection",
        "",
        f"{len(frozen_rows)} freeze events touch {frozen_addr_count} distinct cohort addresses "
        f"(Circle lifetime Blacklisted: {len(circle_blacklisted)} events; USDT AddedBlackList + "
        f"DestroyedBlackFunds: {sum(1 for r in usdt_events if r['event'] in ('AddedBlackList', 'DestroyedBlackFunds'))} events) "
        "-- flagged in `frozen_in_cohort.csv`, not dropped from the cohort.",
        "",
        "## (iii) Canary + stratified reconciliation",
        "",
        f"Stratified: {stratified_matches}/{len(recon_rows)} (wallet, coin, block) reconciliations matched, tolerance 0.",
        f"Canary {CANARY_ADDR} @ block {CANARY_BLOCK}: naive={canary['naive']} "
        f"(expected {CANARY_EXPECTED_UNITS}), actual balanceOf={canary['actual']}, "
        f"corrected={canary['corrected']} -- naive {'FAILS' if canary['naive_fails'] else 'unexpectedly matches'} vs actual, "
        f"corrected {'PASSES' if canary['corrected_passes'] else 'FAILS'}.",
        "",
        f"Cross-endpoint reverification: {len(reverified)} ranges re-fetched and matched.",
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
        "## Outputs",
        "",
        f"- `frozen_in_cohort.csv` ({frozen_n} rows, sha256 `{frozen_sha}`)",
        "",
        f"**Gate 2 result: {'PASS' if all_pass else 'FAIL'}.**",
    ]
    with open(os.path.join(_OUT_DIR, "summary.md"), "w") as f:
        f.write("\n".join(summary_lines) + "\n")

    print(f"[g2] {passed}/{len(checks)} acceptance checks passed")
    if failed:
        for name, detail in failed:
            print(f"[g2] FAIL: {name} -- {detail}")
        return 1
    return 0
