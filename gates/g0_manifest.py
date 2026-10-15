"""Gate 0 -- on-chain pool manifest + event censuses.

(a) topic-only eth_getLogs census (Deposit + Withdrawal, no address filter) over
    census_start -> event_block on tenderly.
(b) for every emitter: eth_getCode + eth_call denomination()/token()/nextIndex()
    at event_block -> classify instance vs other.
(c) Circle's *current* USDC blacklist cluster (event_block -> circle_cluster_end,
    expect 38 distinct addresses) -> in_circle_38 flag.
(d) Circle's full USDC Blacklisted/UnBlacklisted history, census_start -> apr2025.
(e) USDT's full AddedBlackList/RemovedBlackList/DestroyedBlackFunds/Issue/Redeem
    history, census_start -> apr2025.
(f) out/g0/manifest.csv; designated-pool set = instance AND in_circle_38.

Every range fetch goes through lib.rpc.get_logs (receipts + retry + tiling built
in). Tiling is asserted per logical span (census / circle cluster / circle full
history / usdt full history) by replaying only the receipts newly appended for
that span through lib.rpc.assert_tiling -- the shared receipts.jsonl file holds
several distinct, block-overlapping spans, so the whole-file tiling check in
lib.rpc only makes sense scoped to one span at a time.
"""
import csv
import hashlib
import json
import os
import subprocess
import time
import urllib.error
from collections import Counter

from lib import blocks, keccak, rpc

_HERE = os.path.dirname(os.path.abspath(__file__))
_OUT_DIR = os.path.join(_HERE, "..", "out", "g0")
_RECEIPTS_PATH = os.path.join(_OUT_DIR, "receipts.jsonl")

USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"

WIDE = "tenderly"   # wide-range topic/address-filtered eth_getLogs
NARROW = "drpc"     # eth_call archive state + narrow filtered getLogs

# --- topics/selectors -- always computed via lib.keccak, never copied from a
# document (see README / conventions: a prior review pass got "0 deposits"
# from a prefix-matched hash copied off a doc).
DEPOSIT_TOPIC = keccak.topic("Deposit(bytes32,uint32,uint256)")
WITHDRAWAL_TOPIC = keccak.topic("Withdrawal(address,bytes32,address,uint256)")
ENCRYPTED_NOTE_TOPIC = keccak.topic("EncryptedNote(address,bytes)")
BLACKLISTED_TOPIC = keccak.topic("Blacklisted(address)")
UNBLACKLISTED_TOPIC = keccak.topic("UnBlacklisted(address)")
ADDED_BLACKLIST_TOPIC = keccak.topic("AddedBlackList(address)")
REMOVED_BLACKLIST_TOPIC = keccak.topic("RemovedBlackList(address)")
DESTROYED_BLACKFUNDS_TOPIC = keccak.topic("DestroyedBlackFunds(address,uint256)")
ISSUE_TOPIC = keccak.topic("Issue(uint256)")
REDEEM_TOPIC = keccak.topic("Redeem(uint256)")

DENOMINATION_SELECTOR = keccak.selector("denomination()")
TOKEN_SELECTOR = keccak.selector("token()")
NEXT_INDEX_SELECTOR = keccak.selector("nextIndex()")
DECIMALS_SELECTOR = keccak.selector("decimals()")

_ZERO_ADDRESS = "0x" + "0" * 40


def _cross_check_topics():
    """The two topics the spec hands us a known-good value for -- catches a
    transposed ABI signature string before any topic is used to query."""
    assert BLACKLISTED_TOPIC == "0xffa4e6181777692565cf28528fc88fd1516ea86b56da075235fa575af6a4b855", BLACKLISTED_TOPIC
    assert ENCRYPTED_NOTE_TOPIC == "0xfa28df43db3553771f7209dcef046f3bdfea15870ab625dcda30ac58b82b4008", ENCRYPTED_NOTE_TOPIC


_cross_check_topics()

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


class CallFailed(Exception):
    """A specific eth_call reverted / this contract doesn't support the
    function -- a real classification signal, not a network error. Distinct
    from a transient failure, which is retried and then raises RuntimeError
    (hard gate failure -- never silently miscategorized as 'other')."""


def _eth_call(endpoint, to, data, block, max_retries=4, backoff_s=0.5):
    last_err = None
    for attempt in range(max_retries):
        try:
            _throttle(endpoint)
            return rpc.call(endpoint, to, data, block)
        except rpc.RpcError as e:
            raise CallFailed(str(e))
        except _NETWORK_ERRORS as e:
            last_err = e
            time.sleep(backoff_s * (2 ** attempt))
    raise RuntimeError(f"eth_call {to} {data} failed after {max_retries} network retries: {last_err}")


def _eth_call_int(endpoint, to, selector, block):
    result = _eth_call(endpoint, to, selector, block)
    if result in (None, "0x", "0x0"):
        raise CallFailed(f"{to} {selector} returned empty")
    return int(result, 16)


def _eth_call_address(endpoint, to, selector, block):
    result = _eth_call(endpoint, to, selector, block)
    if result in (None, "0x", "0x0"):
        raise CallFailed(f"{to} {selector} returned empty")
    return "0x" + result[-40:].lower()


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


def _token_decimals(endpoint, token_addr, block):
    if not token_addr or token_addr == _ZERO_ADDRESS:
        return 18  # native-ETH pool
    try:
        return _eth_call_int(endpoint, token_addr, DECIMALS_SELECTOR, block)
    except CallFailed:
        return 18  # legacy ERC20 without decimals() -- rare, assume 18


def _block_date(endpoint, block_num, cache):
    if block_num in cache:
        return cache[block_num]
    last_err = None
    for attempt in range(4):
        try:
            _throttle(endpoint)
            date = blocks.block_timestamp(endpoint, block_num)[:10]
            cache[block_num] = date
            return date
        except _NETWORK_ERRORS as e:
            last_err = e
            time.sleep(0.5 * (2 ** attempt))
    raise RuntimeError(f"block timestamp lookup for block {block_num} failed: {last_err}")


def _log_date(log, endpoint, cache):
    """Both tenderly and drpc return `blockTimestamp` directly on each log
    entry -- use it (avoids a separate eth_getBlockByNumber call per event).
    Falls back to a block-number lookup if a log ever lacks the field."""
    ts_hex = log.get("blockTimestamp")
    if ts_hex:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(ts_hex, 16), tz=timezone.utc).strftime("%Y-%m-%d")
    return _block_date(endpoint, _log_block(log), cache)


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


def _get_logs_chunked(endpoint, from_block, to_block, address, topics, gate, window):
    """Fetch eth_getLogs over [from_block, to_block] via lib.rpc.get_logs, in
    windows of at most `window` blocks that adaptively bisect on failure.

    Discovered empirically: tenderly enforces a ~50,000-result response cap
    independent of (and much tighter than, in dense ranges) its documented
    2,000,000-block range cap in config/endpoints.json -- a request that trips
    it comes back `GapError` (lib.rpc retries it on both endpoints, then gives
    up, since neither a repeat of the same oversized request nor drpc's
    10,000-block cap can satisfy it). Bisecting preserves full, non-
    overlapping coverage of the span, so receipts still tile it exactly --
    lib.rpc's own cap-based splitting and per-range receipts are unchanged.
    """
    all_logs = []
    start = from_block
    while start <= to_block:
        end = min(start + window - 1, to_block)
        all_logs.extend(_fetch_window_bisecting(endpoint, start, end, address, topics, gate))
        start = end + 1
    return all_logs


def _fetch_window_bisecting(endpoint, start, end, address, topics, gate, min_window=2000):
    try:
        return rpc.get_logs(endpoint, start, end, address=address, topics=topics, gate=gate)
    except rpc.GapError:
        if end - start + 1 <= min_window:
            raise
        mid = (start + end) // 2
        print(f"[g0]   window [{start},{end}] failed (likely a result-count cap) -- bisecting at {mid}")
        return (
            _fetch_window_bisecting(endpoint, start, mid, address, topics, gate, min_window)
            + _fetch_window_bisecting(endpoint, mid + 1, end, address, topics, gate, min_window)
        )


def _tag_ranges(recs, address, topics):
    """Pair each freshly-appended receipt with the real (address, topics) it
    was fetched with -- receipts.jsonl only stores `addressCount`, not the
    actual address(es), so lib.rpc.reverify() always replays with
    address=None. For an address-filtered fetch that under-constrains the
    replay (a topic like AddedBlackList(address) isn't USDT-unique), so we
    track the real query here and reverify with it below."""
    return [{"endpoint": r["endpoint"], "address": address, "topics": topics,
              "fromBlock": r["fromBlock"], "toBlock": r["toBlock"]} for r in recs]


def _cross_endpoint_reverify(all_ranges, fraction=0.05, min_n=5):
    """5% (min 5) of this gate's ranges, re-fetched on the other endpoint and
    compared by sorted log tuple. Reimplemented here (not lib.rpc.reverify)
    because that function replays via a raw single-shot request with no
    re-splitting -- it 400s on any wide tenderly-sourced range replayed
    against drpc's 10,000-block cap -- and always passes address=None,
    under-constraining any address-filtered original query. Going through
    lib.rpc.get_logs for both sides fixes both: it re-splits to fit whichever
    endpoint's own cap, and it uses the real address filter we tagged above."""
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


# --- event log decoding (Circle's Blacklistable indexes _account; USDT's old
# contract does not index its address/uint params -- detect from topics length
# rather than assume, so a wrong guess fails loudly instead of silently). ---

def _data_words(log):
    data_hex = log["data"][2:] if log["data"].startswith("0x") else log["data"]
    data_bytes = bytes.fromhex(data_hex) if data_hex else b""
    return [data_bytes[i:i + 32] for i in range(0, len(data_bytes), 32)]


def _decode_address_event(log, has_uint):
    """Decode a single-address(+optional uint) event, indexed or not."""
    topics = log["topics"]
    words = _data_words(log)
    if len(topics) >= 2:
        addr = "0x" + topics[1][-40:].lower()
        amount = int.from_bytes(words[0], "big") if has_uint and words else None
    else:
        addr = "0x" + words[0][-20:].hex().lower() if words else ""
        amount = int.from_bytes(words[1], "big") if has_uint and len(words) > 1 else None
    return addr, amount


def _decode_uint_only_event(log):
    topics = log["topics"]
    if len(topics) >= 2:
        return int(topics[1], 16)
    words = _data_words(log)
    return int.from_bytes(words[0], "big") if words else None


def _log_block(log):
    return int(log["blockNumber"], 16)


# --- table writer: CSV + schema.json sidecar (interchange schema) ---

SCHEMA_VERSION = 1


def _git_commit():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=_HERE, text=True).strip()
    except Exception:
        return "unknown"


def _write_table(name, rows, columns, primary_key, input_hashes, notes=None):
    """columns: list of (name, dtype, units). primary_key: str or tuple of
    column names (deterministic sort key). Writes out/g0/<name>.csv +
    out/g0/<name>.schema.json, returns (path, sha256_hex, row_count)."""
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


def _within_pct(actual, expected, pct):
    return abs(actual - expected) <= expected * pct


def run(cfg):
    os.makedirs(_OUT_DIR, exist_ok=True)
    if os.path.exists(_RECEIPTS_PATH):
        os.remove(_RECEIPTS_PATH)  # fresh ledger each run -- gitignored, regenerable

    pinned = cfg["blocks"]
    census_start = pinned["census_start"]["block"]
    event_block = pinned["event_block"]["block"]
    circle_cluster_end = pinned["circle_cluster_end"]["block"]
    apr2025_block = pinned["apr2025"]["block"]

    checks = []  # (name, passed, detail) -- collected, not fail-fast, so one
                 # multi-hour run surfaces every mismatch at once.
    all_ranges = []  # every sub-range fetched this gate, tagged with its real
                      # (address, topics) -- for _cross_endpoint_reverify below.

    # --- (a) topic-only census: Deposit + Withdrawal, no address filter ---
    print(f"[g0] (a) census: Deposit+Withdrawal {census_start} -> {event_block} on {WIDE}")
    start_line = _line_count(_RECEIPTS_PATH)
    census_logs = _get_logs_chunked(
        WIDE, census_start, event_block,
        address=None, topics=[[DEPOSIT_TOPIC, WITHDRAWAL_TOPIC]], gate="g0", window=250_000,
    )
    census_recs = _new_receipts(start_line)
    _assert_component_tiled(census_recs, census_start, event_block, "census")
    census_hashes = [r["sha256"] for r in census_recs]
    all_ranges.extend(_tag_ranges(census_recs, None, [[DEPOSIT_TOPIC, WITHDRAWAL_TOPIC]]))

    deposit_logs = [l for l in census_logs if l["topics"][0].lower() == DEPOSIT_TOPIC.lower()]
    withdrawal_logs = [l for l in census_logs if l["topics"][0].lower() == WITHDRAWAL_TOPIC.lower()]
    print(f"[g0]   {len(deposit_logs)} Deposit, {len(withdrawal_logs)} Withdrawal logs")

    emitters = sorted({l["address"].lower() for l in census_logs})
    print(f"[g0]   {len(emitters)} distinct emitters")

    dep_counts = Counter(l["address"].lower() for l in deposit_logs)
    wd_counts = Counter(l["address"].lower() for l in withdrawal_logs)
    first_block, last_block = {}, {}
    for l in census_logs:
        addr, blk = l["address"].lower(), _log_block(l)
        first_block[addr] = min(blk, first_block.get(addr, blk))
        last_block[addr] = max(blk, last_block.get(addr, blk))

    # --- (b) per-emitter classification ---
    print(f"[g0] (b) classifying {len(emitters)} emitters on {NARROW}")
    manifest_by_addr = {}
    for i, addr in enumerate(emitters):
        code = _get_code(NARROW, addr, event_block)
        if code in (None, "0x", "0x0"):
            raise AssertionError(f"emitter {addr} has empty code at block {event_block} -- integrity violation")
        cls, token_addr, denom_units, denom_raw_out, decimals_out, next_index = "other", "", "", "", "", ""
        try:
            # Ruling 2026-09-04 (SPEC S1 acceptance): instance = denomination()
            # AND nextIndex() succeed. token() is NOT part of the acceptance
            # test -- native-ETH Tornado pools (ETHTornado) have no token(),
            # only ERC20Tornado does; when it reverts, record token="ETH"
            # rather than declassifying the pool. (Fix-round-1 had left this
            # ambiguous -- literal-3-of-3 excluded every native-ETH pool and
            # missed the "designated-pool deposits ~= 163,544" bar by ~92%;
            # this ruling resolves it.)
            denom_raw = _eth_call_int(NARROW, addr, DENOMINATION_SELECTOR, event_block)
            next_index_raw = _eth_call_int(NARROW, addr, NEXT_INDEX_SELECTOR, event_block)
            try:
                token_addr = _eth_call_address(NARROW, addr, TOKEN_SELECTOR, event_block)
                decimals = _token_decimals(NARROW, token_addr, event_block)
            except CallFailed:
                token_addr = "ETH"
                decimals = 18
            scale = 10 ** decimals
            if denom_raw % scale == 0:
                denom_units = denom_raw // scale
            else:
                # not a round number of human units (e.g. a sub-8-decimal WBTC
                # tier) -- render as an exact decimal string, never floor to 0
                # and never a binary float (amounts-are-integers discipline).
                whole, frac = divmod(denom_raw, scale)
                denom_units = f"{whole}.{str(frac).zfill(decimals).rstrip('0') or '0'}"
            denom_raw_out, decimals_out = denom_raw, decimals
            next_index = next_index_raw
            cls = "instance"
        except CallFailed:
            cls = "other"
        manifest_by_addr[addr] = {
            "address": addr, "class": cls, "token": token_addr,
            "denomination": denom_units, "denomination_raw": denom_raw_out, "decimals": decimals_out,
            "nextIndex": next_index,
            "deposits": dep_counts.get(addr, 0), "withdrawals": wd_counts.get(addr, 0),
            "first_block": first_block.get(addr, ""), "last_block": last_block.get(addr, ""),
        }
        if (i + 1) % 10 == 0 or i + 1 == len(emitters):
            print(f"[g0]   classified {i + 1}/{len(emitters)}")

    def _classify_probe(addr, block):
        """Direct classification probe for an address that is NOT a census
        emitter -- used only by the acceptance check below, never written into
        manifest_by_addr (manifest.csv stays scoped to census emitters per (b)
        in the brief). Fix-round-1 finding: 0x527653ea.../0x58e8dcc1... never
        emit Deposit/Withdrawal in the census window (confirmed absent from
        census_logs) so they can't reach the loop above at all; independently
        confirmed via a live eth_call that denomination() and nextIndex() both
        revert on both addresses at event_block, i.e. they would be classed
        'other' under the same rule as (b) (ruling 2026-09-04: denomination()
        AND nextIndex() only -- token() is not part of the instance test).
        Probing directly here satisfies the acceptance criterion's
        plain-language intent without expanding manifest.csv's emitter-only
        scope."""
        try:
            _eth_call_int(NARROW, addr, DENOMINATION_SELECTOR, block)
            _eth_call_int(NARROW, addr, NEXT_INDEX_SELECTOR, block)
            return "instance"
        except CallFailed:
            return "other"

    # exact nextIndex cross-check per instance
    mismatches = [
        f"{a}: deposits={r['deposits']} nextIndex={r['nextIndex']}"
        for a, r in manifest_by_addr.items() if r["class"] == "instance" and r["deposits"] != r["nextIndex"]
    ]
    checks.append(("census deposit count == nextIndex() exactly for every instance", not mismatches, "; ".join(mismatches) or "ok"))

    # --- (c) Circle's current USDC blacklist cluster (event_block -> circle_cluster_end) ---
    print(f"[g0] (c) circle cluster: Blacklisted on USDC {event_block} -> {circle_cluster_end} on {NARROW}")
    start_line = _line_count(_RECEIPTS_PATH)
    cluster_logs = rpc.get_logs(
        NARROW, event_block, circle_cluster_end,
        address=USDC, topics=[[BLACKLISTED_TOPIC]], gate="g0",
    )
    cluster_recs = _new_receipts(start_line)
    _assert_component_tiled(cluster_recs, event_block, circle_cluster_end, "circle cluster")
    cluster_hashes = [r["sha256"] for r in cluster_recs]
    all_ranges.extend(_tag_ranges(cluster_recs, USDC, [[BLACKLISTED_TOPIC]]))
    in_circle_38 = sorted({_decode_address_event(l, has_uint=False)[0] for l in cluster_logs})
    checks.append(("circle current cluster has 38 distinct addresses", len(in_circle_38) == 38, f"got {len(in_circle_38)}"))
    in_circle_38_set = set(in_circle_38)

    for addr, row in manifest_by_addr.items():
        row["in_circle_38"] = addr in in_circle_38_set

    designated_set = {a for a, r in manifest_by_addr.items() if r["class"] == "instance" and r["in_circle_38"]}
    checks.append(("designated set >= 19 members", len(designated_set) >= 19, f"got {len(designated_set)}"))
    for dai_pool in ("0x07687e702b410fa43f4cb4af7fa097918ffd2730", "0x23773e65ed146a459791799d01336db287f25334"):
        checks.append((f"DAI pool {dai_pool} in designated set", dai_pool in designated_set,
                        manifest_by_addr.get(dai_pool, "not an emitter")))
    for other_addr in ("0x527653ea119f3e6a1f5bd18fbf4714081d7b31ce", "0x58e8dcc13be9780fc42e8723d8ead4cf46943df2"):
        row = manifest_by_addr.get(other_addr)
        if row is not None:
            cls, detail = row["class"], row["class"]
        else:
            cls = _classify_probe(other_addr, event_block)
            detail = f"{cls} (not a census emitter -- probed directly, see _classify_probe)"
        checks.append((f"{other_addr} classed other", cls == "other", detail))

    usdc_denoms = {}
    for usdc_pool in ("0x4736dcf1b7a3d580672cce6e7c65cd5cc9cfba9d", "0xd96f2b1c14db8458374d9aca76e26c3d18364307"):
        row = manifest_by_addr.get(usdc_pool)
        usdc_denoms[usdc_pool] = row["denomination"] if row else "not an emitter"
    checks.append(("both known USDC pools read on chain", all(v != "not an emitter" for v in usdc_denoms.values()), str(usdc_denoms)))

    total_deposits = len(deposit_logs)
    designated_deposits = sum(dep_counts.get(a, 0) for a in designated_set)
    checks.append(("total Deposit logs ~= 174,184 (<=1% deviation)", _within_pct(total_deposits, 174184, 0.01), f"got {total_deposits}"))
    checks.append(("designated-pool deposits ~= 163,544 (<=1% deviation)", _within_pct(designated_deposits, 163544, 0.01), f"got {designated_deposits}"))

    # --- (d) Circle full history: Blacklisted + UnBlacklisted, census_start -> apr2025 ---
    print(f"[g0] (d) circle full history: Blacklisted+UnBlacklisted on USDC {census_start} -> {apr2025_block} on {WIDE}")
    start_line = _line_count(_RECEIPTS_PATH)
    circle_full_logs = _get_logs_chunked(
        WIDE, census_start, apr2025_block,
        address=USDC, topics=[[BLACKLISTED_TOPIC, UNBLACKLISTED_TOPIC]], gate="g0", window=2_000_000,
    )
    circle_full_recs = _new_receipts(start_line)
    _assert_component_tiled(circle_full_recs, census_start, apr2025_block, "circle full history")
    circle_full_hashes = [r["sha256"] for r in circle_full_recs]
    all_ranges.extend(_tag_ranges(circle_full_recs, USDC, [[BLACKLISTED_TOPIC, UNBLACKLISTED_TOPIC]]))

    circle_blacklisted = [l for l in circle_full_logs if l["topics"][0].lower() == BLACKLISTED_TOPIC.lower()]
    circle_unblacklisted = [l for l in circle_full_logs if l["topics"][0].lower() == UNBLACKLISTED_TOPIC.lower()]
    # Ruling 2026-09-04 (SPEC S1 acceptance): 278 at the apr2025 pin
    # (2025-03-31T23:59:59Z) is the expected value; the DATA seat's 286/101
    # was anchored a few days later (8 Blacklisted on 2025-04-02 -> 278 + 8 =
    # 286 exactly). The pin is NOT moved mid-run. (Fix-round-1 had found the
    # same 278-vs-286 discrepancy and left it open pending this call.)
    checks.append(("circle lifetime Blacklisted == 278", len(circle_blacklisted) == 278, f"got {len(circle_blacklisted)}"))
    checks.append(("circle lifetime UnBlacklisted == 101", len(circle_unblacklisted) == 101, f"got {len(circle_unblacklisted)}"))

    date_cache = {}
    circle_rows = []
    for l in circle_blacklisted:
        addr, _ = _decode_address_event(l, has_uint=False)
        circle_rows.append({
            "address": addr, "event": "Blacklisted", "block": _log_block(l),
            "date": _log_date(l, NARROW, date_cache),
            "tx_hash": l["transactionHash"].lower(), "log_index": int(l["logIndex"], 16),
        })
    for l in circle_unblacklisted:
        addr, _ = _decode_address_event(l, has_uint=False)
        circle_rows.append({
            "address": addr, "event": "UnBlacklisted", "block": _log_block(l),
            "date": _log_date(l, NARROW, date_cache),
            "tx_hash": l["transactionHash"].lower(), "log_index": int(l["logIndex"], 16),
        })

    # Fix-round-1 finding: the brief's "22 Mar 2025 cluster" is an UnBlacklisted
    # event, not a Blacklisted one -- OFAC removed Tornado Cash from the SDN
    # list in Mar 2025, and Circle un-froze the affected addresses in bulk.
    # Verified directly: 2025-03-22 has zero Blacklisted rows but 90
    # UnBlacklisted rows; intersecting those 90 with the 38-address Aug-2022
    # cluster gives exactly 37, missing only 0x905b63ff...ec7601 -- the same
    # address the next check below shows was UnBlacklisted separately, at a
    # different block (15,941,739). Checked against "Blacklisted" (as
    # originally coded) this always reads 0 -- no such Blacklisted cluster
    # exists near that date at any window tried (verified up to Aug 2025).
    mar22_addrs = {r["address"] for r in circle_rows if r["event"] == "UnBlacklisted" and r["date"] == "2025-03-22"}
    checks.append(("22 Mar 2025 cluster contains 37 of the 38", len(mar22_addrs & in_circle_38_set) == 37,
                    f"got {len(mar22_addrs & in_circle_38_set)} of {len(mar22_addrs)} cluster addresses"))

    unbl_target = "0x905b63fff465b9ffbf41dea908ceb12478ec7601"
    hit = any(r["address"] == unbl_target and r["event"] == "UnBlacklisted" and r["block"] == 15941739 for r in circle_rows)
    checks.append((f"{unbl_target} UnBlacklisted at 15,941,739", hit, "found" if hit else "not found"))

    cluster62 = {r["address"] for r in circle_rows if r["event"] == "Blacklisted" and 15941674 <= r["block"] <= 15941903}
    checks.append(("62-address Blacklisted cluster at 15,941,674-15,941,903", len(cluster62) == 62, f"got {len(cluster62)}"))

    # --- (e) USDT full history ---
    print(f"[g0] (e) usdt full history: {census_start} -> {apr2025_block} on {WIDE}")
    usdt_topics = [ADDED_BLACKLIST_TOPIC, REMOVED_BLACKLIST_TOPIC, DESTROYED_BLACKFUNDS_TOPIC, ISSUE_TOPIC, REDEEM_TOPIC]
    start_line = _line_count(_RECEIPTS_PATH)
    usdt_logs = _get_logs_chunked(
        WIDE, census_start, apr2025_block,
        address=USDT, topics=[usdt_topics], gate="g0", window=2_000_000,
    )
    usdt_recs = _new_receipts(start_line)
    _assert_component_tiled(usdt_recs, census_start, apr2025_block, "usdt full history")
    usdt_hashes = [r["sha256"] for r in usdt_recs]
    all_ranges.extend(_tag_ranges(usdt_recs, USDT, [usdt_topics]))

    topic_names = {
        ADDED_BLACKLIST_TOPIC.lower(): "AddedBlackList", REMOVED_BLACKLIST_TOPIC.lower(): "RemovedBlackList",
        DESTROYED_BLACKFUNDS_TOPIC.lower(): "DestroyedBlackFunds", ISSUE_TOPIC.lower(): "Issue",
        REDEEM_TOPIC.lower(): "Redeem",
    }
    usdt_rows = []
    destroyed_hit = False
    for l in usdt_logs:
        name = topic_names[l["topics"][0].lower()]
        blk = _log_block(l)
        if name in ("Issue", "Redeem"):
            addr, amount = "", _decode_uint_only_event(l)
        else:
            addr, amount = _decode_address_event(l, has_uint=(name == "DestroyedBlackFunds"))
            if name == "DestroyedBlackFunds" and addr == "0x5cb182a51c452937abecc9e81a9663efbeb425f5" and blk == 15365491:
                destroyed_hit = True
        usdt_rows.append({
            "address": addr, "event": name, "block": blk,
            "date": _log_date(l, NARROW, date_cache),
            "tx_hash": l["transactionHash"].lower(), "log_index": int(l["logIndex"], 16),
            "amount_units": amount if amount is not None else "",
        })
    checks.append(("USDT DestroyedBlackFunds for 0x5cb18...25f5 at 15,365,491", destroyed_hit, "found" if destroyed_hit else "not found"))
    usdt_event_counts = Counter(r["event"] for r in usdt_rows)
    usdt_event_summary = ", ".join(f"{k}={usdt_event_counts.get(k, 0)}" for k in topic_names.values())

    # --- 5% cross-endpoint reverification over the whole gate's receipts ---
    print(f"[g0] cross-endpoint reverification (5%, min 5, of {len(all_ranges)} ranges)")
    reverified = _cross_endpoint_reverify(all_ranges)
    print(f"[g0]   reverified {len(reverified)} ranges")

    # --- (f) write outputs ---
    manifest_columns = [
        ("address", "str", "0x, 42 chars, lowercase"), ("class", "str", "instance|other"),
        ("token", "str", "0x, 42 chars, lowercase; \"ETH\" for native-ETH pools (token() reverts by design); empty if class=other"),
        ("denomination", "int|decimal-str", "human units of `token`; a decimal string if not a round unit; empty if class=other"),
        ("denomination_raw", "int", "raw denomination() base units; empty if class=other"),
        ("decimals", "int", "decimals() of `token` (18 for native-ETH pools); empty if class=other"),
        ("nextIndex", "int", "on-chain nextIndex(); empty if class=other"),
        ("deposits", "int", "census Deposit-log count"), ("withdrawals", "int", "census Withdrawal-log count"),
        ("in_circle_38", "bool", "member of the Aug-2022 Circle USDC blacklist cluster"),
        ("first_block", "int", "first census log block"), ("last_block", "int", "last census log block"),
    ]
    manifest_path, manifest_sha, manifest_n = _write_table(
        "manifest", list(manifest_by_addr.values()), manifest_columns, "address",
        input_hashes=census_hashes + cluster_hashes,
        notes="in_recon_19 column omitted -- the reconciliation file is not present in this repo copy (spec fallback).",
    )

    circle_columns = [
        ("address", "str", "0x, 42 chars, lowercase"), ("event", "str", "Blacklisted|UnBlacklisted"),
        ("block", "int", "block number"), ("date", "str", "UTC YYYY-MM-DD"),
        ("tx_hash", "str", "0x, 66 chars, lowercase"), ("log_index", "int", "within-tx log index"),
    ]
    circle_path, circle_sha, circle_n = _write_table(
        "circle_events", circle_rows, circle_columns, ("tx_hash", "log_index"),
        input_hashes=circle_full_hashes,
    )

    usdt_columns = [
        ("address", "str", "0x, 42 chars, lowercase; empty for Issue/Redeem (no address param)"),
        ("event", "str", "AddedBlackList|RemovedBlackList|DestroyedBlackFunds|Issue|Redeem"),
        ("block", "int", "block number"), ("date", "str", "UTC YYYY-MM-DD"),
        ("tx_hash", "str", "0x, 66 chars, lowercase"), ("log_index", "int", "within-tx log index"),
        ("amount_units", "int", "base units (6 dec); empty for AddedBlackList/RemovedBlackList"),
    ]
    usdt_path, usdt_sha, usdt_n = _write_table(
        "usdt_events", usdt_rows, usdt_columns, ("tx_hash", "log_index"),
        input_hashes=usdt_hashes,
    )

    passed = sum(1 for _, ok, _ in checks if ok)
    failed = [(n, d) for n, ok, d in checks if not ok]

    summary_lines = [
        "# Gate 0 -- pool manifest + event censuses",
        "",
        f"Census: {census_start} -> {event_block} (topic-only, {WIDE}).",
        f"Emitters: {len(emitters)}. Instances: {sum(1 for r in manifest_by_addr.values() if r['class'] == 'instance')}. "
        f"Other: {sum(1 for r in manifest_by_addr.values() if r['class'] == 'other')}.",
        f"Deposit logs: {total_deposits}. Withdrawal logs: {len(withdrawal_logs)}.",
        f"Designated-pool set (instance AND in_circle_38): {len(designated_set)} members; "
        f"designated-pool deposits: {designated_deposits}.",
        f"Circle current cluster ({event_block}-{circle_cluster_end}): {len(in_circle_38)} distinct addresses.",
        f"Circle lifetime history ({census_start}-{apr2025_block}): {len(circle_blacklisted)} Blacklisted, "
        f"{len(circle_unblacklisted)} UnBlacklisted.",
        f"USDT lifetime history ({census_start}-{apr2025_block}): {len(usdt_rows)} events "
        f"({usdt_event_summary}).",
        f"Known USDC pools' on-chain denomination: {usdc_denoms}.",
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
        f"- `manifest.csv` ({manifest_n} rows, sha256 `{manifest_sha}`)",
        f"- `circle_events.csv` ({circle_n} rows, sha256 `{circle_sha}`)",
        f"- `usdt_events.csv` ({usdt_n} rows, sha256 `{usdt_sha}`)",
        "",
        f"Manifest path: `{os.path.relpath(manifest_path, os.path.join(_HERE, '..'))}`",
        f"Circle events path: `{os.path.relpath(circle_path, os.path.join(_HERE, '..'))}`",
        f"USDT events path: `{os.path.relpath(usdt_path, os.path.join(_HERE, '..'))}`",
    ]
    with open(os.path.join(_OUT_DIR, "summary.md"), "w") as f:
        f.write("\n".join(summary_lines) + "\n")

    print(f"[g0] {passed}/{len(checks)} acceptance checks passed")
    if failed:
        for name, detail in failed:
            print(f"[g0] FAIL: {name} -- {detail}")
        return 1
    return 0
