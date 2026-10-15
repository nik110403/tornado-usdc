"""JSON-RPC client with per-endpoint caps, retry-with-verification, receipts,
tiling assertion, and cross-endpoint re-verification.

Never `latest` — every caller passes a numeric block. Every range fetch is
appended to `out/g<N>/receipts.jsonl`; at gate end `assert_tiling` proves the
receipts cover the declared span with no gap and no overlap. A range that
still fails after retry-then-other-endpoint raises GapError and the gate
fails — we never silently skip a bad range.
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_HERE, "..", "config", "endpoints.json")

with open(_CONFIG_PATH) as _f:
    _CONFIG = json.load(_f)

CHAIN_ID = _CONFIG["chain_id"]
USER_AGENT = _CONFIG["user_agent"]
ENDPOINTS = _CONFIG["endpoints"]

_OTHER_ENDPOINT = {
    "tenderly": "drpc",
    "drpc": "tenderly",
    "mevblocker": "drpc",
}


class GapError(Exception):
    """A block range could not be fetched from either endpoint."""


class RpcError(Exception):
    pass


def _endpoint_cfg(endpoint):
    if endpoint not in ENDPOINTS:
        raise RpcError(f"unknown endpoint {endpoint!r} (not in config/endpoints.json)")
    return ENDPOINTS[endpoint]


def _post(endpoint, method, params, timeout=30):
    cfg = _endpoint_cfg(endpoint)
    payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    req = urllib.request.Request(
        cfg["url"],
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    obj = json.loads(body)
    if "error" in obj:
        raise RpcError(f"{endpoint} {method} -> {obj['error']}")
    return obj["result"], body


def assert_chain_id(endpoint):
    result, _ = _post(endpoint, "eth_chainId", [])
    got = int(result, 16)
    if got != CHAIN_ID:
        raise RpcError(f"{endpoint} chain id {got} != expected {CHAIN_ID}")
    return got


def _split_ranges(from_block, to_block, cap):
    """Yield (start, end) inclusive sub-ranges each spanning at most `cap`
    blocks."""
    start = from_block
    while start <= to_block:
        end = min(start + cap - 1, to_block)
        yield start, end
        start = end + 1


def _receipt_path(gate):
    out_dir = os.path.join(_HERE, "..", "out", gate)
    os.makedirs(out_dir, exist_ok=True)
    return os.path.join(out_dir, "receipts.jsonl")


def _append_receipt(gate, record):
    if gate is None:
        return
    path = _receipt_path(gate)
    with open(path, "a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def _sha256_payload(payload_bytes):
    return hashlib.sha256(payload_bytes).hexdigest()


def _fetch_one_range(endpoint, from_block, to_block, address, topics):
    params = [{
        "fromBlock": hex(from_block),
        "toBlock": hex(to_block),
    }]
    if address is not None:
        params[0]["address"] = address
    if topics is not None:
        params[0]["topics"] = topics
    result, body = _post(endpoint, "eth_getLogs", params)
    return result, body


def get_logs(endpoint, from_block, to_block, address=None, topics=None, gate=None,
             max_retries=3, backoff_s=1.0):
    """Fetch eth_getLogs over [from_block, to_block], auto-split to the
    endpoint's block-range cap. Every sub-range fetch is retried on error
    (backoff, then the other endpoint) and appended to receipts.jsonl. Raises
    GapError if a sub-range fails on both endpoints after retries -- never
    returns a partial/skip result silently.
    """
    if to_block < from_block:
        raise RpcError(f"to_block {to_block} < from_block {from_block}")
    cap = _endpoint_cfg(endpoint)["block_range_cap"]
    all_logs = []
    for start, end in _split_ranges(from_block, to_block, cap):
        logs = _fetch_range_with_retry(
            endpoint, start, end, address, topics, gate, max_retries, backoff_s
        )
        all_logs.extend(logs)
    return all_logs


def _fetch_range_with_retry(endpoint, start, end, address, topics, gate, max_retries, backoff_s):
    tried = []
    for attempt_endpoint in (endpoint, _OTHER_ENDPOINT.get(endpoint)):
        if attempt_endpoint is None or attempt_endpoint in tried:
            continue
        tried.append(attempt_endpoint)
        last_err = None
        for attempt in range(max_retries):
            try:
                result, body = _fetch_one_range(attempt_endpoint, start, end, address, topics)
                _append_receipt(gate, {
                    "gate": gate,
                    "endpoint": attempt_endpoint,
                    "fromBlock": start,
                    "toBlock": end,
                    "addressCount": _address_count(address),
                    "topics": topics,
                    "logCount": len(result),
                    "sha256": _sha256_payload(body),
                    "t": time.time(),
                })
                return result
            except (RpcError, urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = e
                time.sleep(backoff_s * (2 ** attempt))
        # exhausted retries on this endpoint; fall through to the other one
        _last_err_for_endpoint = last_err
    raise GapError(
        f"range [{start},{end}] failed on {tried} after retries: {last_err}"
    )


def _address_count(address):
    if address is None:
        return 0
    if isinstance(address, list):
        return len(address)
    return 1


def call(endpoint, to, data, block):
    """eth_call at a pinned block (hex int or 'latest' is refused)."""
    if isinstance(block, str) and block == "latest":
        raise RpcError("call() refuses block='latest' -- pin a numeric block")
    block_hex = block if isinstance(block, str) else hex(block)
    result, _ = _post(endpoint, "eth_call", [{"to": to, "data": data}, block_hex])
    return result


def get_code(endpoint, addr, block):
    if isinstance(block, str) and block == "latest":
        raise RpcError("get_code() refuses block='latest' -- pin a numeric block")
    block_hex = block if isinstance(block, str) else hex(block)
    result, _ = _post(endpoint, "eth_getCode", [addr, block_hex])
    return result


def block_number(endpoint):
    """Current head block number. Only used for bisection upper-bound
    discovery -- never as a query pin."""
    result, _ = _post(endpoint, "eth_blockNumber", [])
    return int(result, 16)


def get_block_by_number(endpoint, block, full_tx=False):
    if isinstance(block, str) and block == "latest":
        raise RpcError("get_block_by_number() refuses block='latest' -- pin a numeric block")
    block_hex = block if isinstance(block, str) else hex(block)
    result, _ = _post(endpoint, "eth_getBlockByNumber", [block_hex, full_tx])
    return result


def _log_key(log):
    return (
        log.get("blockHash"),
        log.get("transactionHash"),
        log.get("logIndex"),
        log.get("address"),
        tuple(log.get("topics", [])),
        log.get("data"),
    )


def assert_tiling(receipts_path, from_block, to_block):
    """Read receipts.jsonl and assert the union of [fromBlock, toBlock]
    ranges (per gate) exactly tiles [from_block, to_block]: no gap, no
    overlap. Raises AssertionError with the offending range otherwise."""
    ranges = []
    with open(receipts_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            ranges.append((rec["fromBlock"], rec["toBlock"]))
    if not ranges:
        raise AssertionError(f"no receipts in {receipts_path}")
    ranges.sort()
    cursor = from_block
    for start, end in ranges:
        if start > cursor:
            raise AssertionError(f"gap in receipts: expected block {cursor}, next range starts at {start}")
        if start < cursor:
            raise AssertionError(f"overlap in receipts: range [{start},{end}] overlaps prior coverage up to {cursor - 1}")
        cursor = end + 1
    if cursor - 1 != to_block:
        raise AssertionError(f"receipts cover up to {cursor - 1}, expected {to_block}")
    return True


def reverify(receipts_path, fraction=0.05, min_n=5, gate=None):
    """Re-fetch a sample of ranges (>= min_n, or `fraction` of all ranges,
    whichever is larger) on the other endpoint and assert identical logCount
    and identical sha256 over the sorted comparable log-tuple set. Returns
    the list of ranges re-verified.
    """
    import math

    recs = []
    with open(receipts_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            recs.append(json.loads(line))
    if not recs:
        raise AssertionError(f"no receipts in {receipts_path}")

    n = max(min_n, math.ceil(len(recs) * fraction))
    n = min(n, len(recs))
    sample = recs[:n]  # deterministic: first n by file order (receipts are already tiled/sorted on write)

    verified = []
    for rec in sample:
        other = _OTHER_ENDPOINT.get(rec["endpoint"])
        if other is None:
            continue
        # topics may be None or a list; address may be None/str/list
        result, body = _fetch_one_range(other, rec["fromBlock"], rec["toBlock"], None, rec["topics"])
        if len(result) != rec["logCount"]:
            raise AssertionError(
                f"reverify logCount mismatch for [{rec['fromBlock']},{rec['toBlock']}]: "
                f"{rec['endpoint']}={rec['logCount']} {other}={len(result)}"
            )
        keys = sorted(_log_key(log) for log in result)
        digest = hashlib.sha256(json.dumps(keys, sort_keys=True).encode()).hexdigest()
        # compare against a same-shape digest computed from the original endpoint's response
        orig_result, _ = _fetch_one_range(rec["endpoint"], rec["fromBlock"], rec["toBlock"], None, rec["topics"])
        orig_keys = sorted(_log_key(log) for log in orig_result)
        orig_digest = hashlib.sha256(json.dumps(orig_keys, sort_keys=True).encode()).hexdigest()
        if digest != orig_digest:
            raise AssertionError(
                f"reverify sha256 mismatch for [{rec['fromBlock']},{rec['toBlock']}] "
                f"between {rec['endpoint']} and {other}"
            )
        verified.append((rec["fromBlock"], rec["toBlock"]))
    return verified
