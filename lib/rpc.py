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
import re
import subprocess
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
    try:
        obj = json.loads(body)
    except ValueError as e:  # 200 with an HTML/empty body: must hit retry + fallback
        raise RpcError(f"{endpoint} {method} -> non-JSON body ({len(body)} bytes): {e}") from e
    if not isinstance(obj, dict):
        raise RpcError(f"{endpoint} {method} -> unexpected JSON shape {type(obj).__name__}")
    if "error" in obj:
        raise RpcError(f"{endpoint} {method} -> {obj['error']}")
    if "result" not in obj:
        raise RpcError(f"{endpoint} {method} -> response has neither result nor error")
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
             max_retries=3, backoff_s=1.0, fallback=True):
    """Fetch eth_getLogs over [from_block, to_block], auto-split to the
    endpoint's block-range cap. Every sub-range fetch is retried on error
    (backoff, then the other endpoint) and appended to receipts.jsonl. Raises
    GapError if a sub-range fails on both endpoints after retries -- never
    returns a partial/skip result silently. `fallback=False` keeps the request on
    `endpoint` only (census sweeps: no cross-provider fan-out).
    """
    if to_block < from_block:
        raise RpcError(f"to_block {to_block} < from_block {from_block}")
    cap = _endpoint_cfg(endpoint)["block_range_cap"]
    all_logs = []
    for start, end in _split_ranges(from_block, to_block, cap):
        logs = _fetch_range_with_retry(
            endpoint, start, end, address, topics, gate, max_retries, backoff_s, fallback
        )
        all_logs.extend(logs)
    return all_logs


def _fetch_range_with_retry(endpoint, start, end, address, topics, gate, max_retries, backoff_s, fallback=True):
    """Fetch [start, end] on `endpoint`, falling back to the other endpoint.
    The window is re-split against *each* endpoint's own block_range_cap
    (the fallback's cap may be smaller than the primary's). Receipts for an
    endpoint attempt are appended only after every sub-range of that attempt
    succeeded, and outside the network try/except, so a receipt-write
    OSError (disk full) propagates instead of triggering a re-fetch."""
    tried = []
    last_err = None
    for attempt_endpoint in (endpoint, _OTHER_ENDPOINT.get(endpoint) if fallback else None):
        if attempt_endpoint is None or attempt_endpoint in tried:
            continue
        tried.append(attempt_endpoint)
        cap = _endpoint_cfg(attempt_endpoint)["block_range_cap"]
        collected = []  # (sub_start, sub_end, result, body)
        ok = True
        for sub_start, sub_end in _split_ranges(start, end, cap):
            fetched = None
            for attempt in range(max_retries):
                try:
                    fetched = _fetch_one_range(attempt_endpoint, sub_start, sub_end, address, topics)
                    break
                except (RpcError, urllib.error.URLError, TimeoutError, OSError) as e:
                    last_err = e
                    time.sleep(backoff_s * (2 ** attempt))
            if fetched is None:
                ok = False
                break
            collected.append((sub_start, sub_end) + tuple(fetched))
        if not ok:
            continue  # fall through to the other endpoint
        merged = []
        for sub_start, sub_end, result, body in collected:
            _append_receipt(gate, {
                "gate": gate,
                "endpoint": attempt_endpoint,
                "fromBlock": sub_start,
                "toBlock": sub_end,
                "addressCount": _address_count(address),
                "topics": topics,
                "logCount": len(result),
                "sha256": _sha256_payload(body),
                "t": time.time(),
            })
            merged.extend(result)
        return merged
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


# --- provider-scoped, incrementally checkpointed log acquisition ---------------
#
# Census sweeps (g4_june, g0_baseline_blacklist) go through ChunkStore, not get_logs():
#   * ONE provider per request: no fallback, so a deterministic failure on one provider
#     (e.g. tenderly on a dense window) is never re-issued as a 429 storm on another;
#   * errors are classified: quota/429 -> bounded cooldown on the SAME range, then a hard
#     failure (never bisection, never fallback); result cap / range too large -> bisect on
#     query-shaped bounds; anything else -> bounded retry, then hard failure;
#   * every completed chunk is written atomically and bound to producer commit, config
#     hash, input hashes and query shape; a corrupt or mismatched chunk FAILS the run
#     (CheckpointError) and is never silently refetched or reused.

class QuotaError(GapError):
    """Provider quota / rate limit (HTTP 429 ...). Not a range-size error."""


class ResultCapError(GapError):
    """Reply at/over the provider's result cap, or the range is too large for it: bisect."""


class CheckpointError(Exception):
    """A stored chunk is corrupt, misaligned, or bound to a different producer/config/query."""


class CoverageError(AssertionError):
    pass


QUOTA_COOLDOWNS = (30, 60, 120, 240, 480)  # s, bounded; then QuotaError
# Quota = an HTTP 429 status / JSON-RPC rate-limit code, or quota wording. A bare "429" substring is NOT
# enough (it occurs inside hex block numbers / hashes); "capacity" is a quota word and is checked before "exceed".
_QUOTA_RE = re.compile(r"http error 429|status(?: code)?[ :=]+429\b|\b429 too many|\bcode\W{1,4}-?(?:429|32029)\b"
                       r"|quota|rate[ -]limit|too many requests|capacity")
_BISECT_MARKERS = ("http error 400", "more than", "too many results", "result set", "limit exceeded",
                   "block range", "ranges over", "range too", "-32005", "response size", "exceed")


_HEX40 = re.compile(r"^[0-9a-f]{40}$")


def classify_error(err):
    """'quota' | 'bisect' | 'transient'. Quota is checked first and is never a range-size error."""
    if getattr(err, "code", None) == 429:  # urllib.error.HTTPError
        return "quota"
    m = str(err).lower()
    if _QUOTA_RE.search(m):
        return "quota"
    if any(t in m for t in _BISECT_MARKERS):
        return "bisect"
    return "transient"


def producer_identity(paths, head=None):
    """HEAD commit + sha256 of the producer source files: chunks written by edited code (even uncommitted)
    are not reused. Raises RuntimeError if git is unavailable -- identity is never "unknown"."""
    if head is None:
        try:
            head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=_HERE, text=True).strip()
        except (subprocess.CalledProcessError, OSError) as e:
            raise RuntimeError(f"cannot determine producer commit: {e}") from e
    if not _HEX40.match(head):
        raise RuntimeError(f"unexpected git HEAD {head!r}")
    h = hashlib.sha256()
    for path in sorted(paths):
        with open(path, "rb") as f:
            h.update(os.path.basename(path).encode() + b"\0" + hashlib.sha256(f.read()).digest())
    return f"{head}:{h.hexdigest()[:16]}"


def query_shape(address, topics):
    if address is None:
        return "topic_only"
    if isinstance(address, list) and len(address) != 1:
        return "multi_address"
    return "single_address"


def shape_cap(endpoint, shape):
    """Block-range cap for a query shape. An endpoint that records `shape_caps` is only trusted for
    the shapes listed there; any other shape gets `unmeasured_shape_cap` (conservative)."""
    cfg = _endpoint_cfg(endpoint)
    if "shape_caps" not in cfg:
        return cfg["block_range_cap"]
    return cfg["shape_caps"].get(shape, cfg["unmeasured_shape_cap"])


def result_cap(endpoint):
    return _endpoint_cfg(endpoint)["result_cap"]


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def canonical_sha256(obj):
    return hashlib.sha256(_canon(obj).encode()).hexdigest()


def _atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(_canon(obj))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def fetch_logs_scoped(endpoint, lo, hi, address=None, topics=None, max_retries=3, backoff_s=1.0):
    """One eth_getLogs for [lo, hi] on `endpoint` ONLY -> (logs, response_body). The range must fit
    shape_cap(). Raises QuotaError (no retry: the caller cools down), ResultCapError (reply at/over
    the result cap, or a range-size/400 style rejection: bisect), GapError (transient errors
    exhausted, or a malformed reply)."""
    shape = query_shape(address, topics)
    if hi < lo or hi - lo + 1 > shape_cap(endpoint, shape):
        raise RpcError(f"[{lo},{hi}] outside {endpoint} {shape} cap {shape_cap(endpoint, shape)}")
    last = None
    for attempt in range(max_retries):
        try:
            result, body = _fetch_one_range(endpoint, lo, hi, address, topics)
        except (RpcError, urllib.error.URLError, TimeoutError, OSError) as e:
            kind = classify_error(e)
            if kind == "quota":
                raise QuotaError(f"{endpoint} [{lo},{hi}]: {e}") from e
            if kind == "bisect":
                raise ResultCapError(f"{endpoint} [{lo},{hi}]: {e}") from e
            last = e
            time.sleep(backoff_s * (2 ** attempt))
            continue
        if not isinstance(result, list) or not all(
                isinstance(l, dict) and "blockNumber" in l and "transactionHash" in l and "logIndex" in l
                for l in result):
            raise GapError(f"{endpoint} [{lo},{hi}]: malformed eth_getLogs result")
        if len(result) >= result_cap(endpoint):
            raise ResultCapError(f"{endpoint} [{lo},{hi}]: {len(result)} logs at/over cap {result_cap(endpoint)}")
        return result, body
    raise GapError(f"{endpoint} [{lo},{hi}] failed after {max_retries} attempts: {last}")


def _log_block_num(log):
    b = log["blockNumber"]
    return int(b, 16) if isinstance(b, str) else int(b)


def _log_event_key(log):
    li = log["logIndex"]
    return (log["transactionHash"].lower(), int(li, 16) if isinstance(li, str) else int(li))


def _norm_address(address):
    if address is None:
        return None
    return sorted(a.lower() for a in (address if isinstance(address, list) else [address]))


def make_binding(producer_commit, config_sha256, input_hashes, endpoint, address, topics):
    """The identity a chunk is bound to. Anything that changes the query or the producer changes
    the binding, so old chunks are rejected instead of reused."""
    return {"producer_commit": producer_commit, "config_sha256": config_sha256,
            "input_hashes": dict(input_hashes),
            "query": {"endpoint": endpoint, "address": _norm_address(address),
                      "topics": topics, "shape": query_shape(address, topics)}}


class ChunkStore:
    """Directory of durably checkpointed getLogs chunks for ONE bound query.

    chunk-<lo>-<hi>.json   one completed range (atomic write), self-describing + hashed payload
    binding.json           the binding; re-opening with a different binding raises CheckpointError
    attempts.jsonl         append-only record of EVERY fetch attempt (ok / result_cap / quota / error)
    """

    def __init__(self, directory, binding):
        self.dir = directory
        self.binding = binding
        self.binding_sha256 = canonical_sha256(binding)
        self.attempt_id = f"{time.time_ns()}-{os.getpid()}"
        os.makedirs(directory, exist_ok=True)
        bpath = os.path.join(directory, "binding.json")
        if os.path.exists(bpath):
            try:
                with open(bpath) as f:
                    stored = json.load(f)
            except ValueError as e:
                raise CheckpointError(f"{bpath} is corrupt: {e}") from e
            if stored != binding:
                raise CheckpointError(f"{directory}: checkpoints are bound to a different producer/config/inputs/query "
                                      f"(stored {canonical_sha256(stored)[:12]} != {self.binding_sha256[:12]}); "
                                      f"move the directory aside to start fresh")
        else:
            _atomic_write_json(bpath, binding)
        self._index = {}
        for name in os.listdir(directory):
            if name.startswith("chunk-") and name.endswith(".json"):
                _, a, b = name[:-5].split("-")
                self._index[(int(a), int(b))] = os.path.join(directory, name)
        self._note({"kind": "open", "binding_sha256": self.binding_sha256, "chunks_present": len(self._index)})

    # -- durable records
    def _note(self, rec):
        rec = {**rec, "attempt_id": self.attempt_id, "t": time.time()}
        with open(os.path.join(self.dir, "attempts.jsonl"), "a") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _write_chunk(self, lo, hi, logs, body):
        rec = {"lo": lo, "hi": hi, "binding_sha256": self.binding_sha256, "attempt_id": self.attempt_id,
               "endpoint": self.binding["query"]["endpoint"], "logCount": len(logs),
               "response_sha256": _sha256_payload(body), "payload_sha256": canonical_sha256(logs),
               "t": time.time(), "logs": logs}
        path = os.path.join(self.dir, f"chunk-{lo:010d}-{hi:010d}.json")
        _atomic_write_json(path, rec)
        self._index[(lo, hi)] = path
        return rec

    def read_chunk(self, lo, hi):
        """Load + fully validate one stored chunk; any defect raises CheckpointError."""
        path = self._index[(lo, hi)]
        try:
            with open(path) as f:
                rec = json.load(f)
        except (ValueError, OSError) as e:
            raise CheckpointError(f"{path} unreadable/corrupt: {e}") from e
        q = self.binding["query"]
        try:
            ok = (rec["lo"] == lo and rec["hi"] == hi and rec["binding_sha256"] == self.binding_sha256
                  and rec["endpoint"] == q["endpoint"] and rec["logCount"] == len(rec["logs"])
                  and rec["payload_sha256"] == canonical_sha256(rec["logs"]) and bool(rec["response_sha256"]))
        except (KeyError, TypeError) as e:
            raise CheckpointError(f"{path} malformed: {e!r}") from e
        if not ok:
            raise CheckpointError(f"{path}: header/binding/payload hash mismatch")
        if rec["logCount"] >= result_cap(q["endpoint"]):
            raise CheckpointError(f"{path}: {rec['logCount']} logs at/over the result cap (possible truncation)")
        addrs = set(q["address"]) if q["address"] is not None else None
        t0s = None
        if q["topics"] and isinstance(q["topics"][0], list):
            t0s = {t.lower() for t in q["topics"][0]}
        elif q["topics"] and isinstance(q["topics"][0], str):
            t0s = {q["topics"][0].lower()}
        try:
            for l in rec["logs"]:
                if not lo <= _log_block_num(l) <= hi:
                    raise CheckpointError(f"{path}: log outside [{lo},{hi}]")
                if addrs is not None and l["address"].lower() not in addrs:
                    raise CheckpointError(f"{path}: log address outside the bound query")
                if t0s is not None and l["topics"][0].lower() not in t0s:
                    raise CheckpointError(f"{path}: log topic0 outside the bound query")
        except (KeyError, TypeError, IndexError, ValueError) as e:
            raise CheckpointError(f"{path}: malformed log: {e!r}") from e
        return rec

    # -- acquisition
    def acquire(self, lo, hi, fetch, window, guard=None, on_chunk=None, cooldowns=None):
        """Logs for [lo, hi], in `window` steps. Completed chunks are reused (validated), the rest fetched
        via `fetch(lo, hi) -> (logs, body)` and checkpointed one by one."""
        cooldowns = QUOTA_COOLDOWNS if cooldowns is None else cooldowns
        out, start = [], lo
        while start <= hi:
            end = min(start + window - 1, hi)
            out.extend(self._range(start, end, fetch, guard, on_chunk, cooldowns))
            start = end + 1
        return out

    def _range(self, lo, hi, fetch, guard, on_chunk, cooldowns):
        if (lo, hi) in self._index:
            return self.read_chunk(lo, hi)["logs"]
        over = [c for c in self._index if c[0] <= hi and c[1] >= lo]
        if over:
            if lo == hi or any(c[0] < lo or c[1] > hi for c in over):
                raise CheckpointError(f"stored chunks {sorted(over)[:3]} are misaligned with requested [{lo},{hi}]")
            return self._split(lo, hi, fetch, guard, on_chunk, cooldowns)  # an earlier attempt bisected here
        if guard is not None:
            guard(hi)
        waits = list(cooldowns)
        while True:
            try:
                logs, body = fetch(lo, hi)
            except QuotaError as e:
                self._note({"kind": "quota", "lo": lo, "hi": hi, "error": str(e)[:300]})
                if not waits:
                    raise
                wait = waits.pop(0)
                time.sleep(wait)  # same range, same provider; not a range-size error
                continue
            except ResultCapError as e:
                self._note({"kind": "result_cap", "lo": lo, "hi": hi, "error": str(e)[:300]})
                if lo == hi:
                    raise ResultCapError(f"[{lo},{hi}] at/over the result cap at one block: {e}") from e
                return self._split(lo, hi, fetch, guard, on_chunk, cooldowns)
            except GapError as e:
                self._note({"kind": "error", "lo": lo, "hi": hi, "error": str(e)[:300]})
                raise
            break
        rec = self._write_chunk(lo, hi, logs, body)  # outside the network try: an OSError here propagates
        self._note({"kind": "ok", "lo": lo, "hi": hi, "logCount": len(logs), "response_sha256": rec["response_sha256"]})
        if on_chunk is not None:
            on_chunk({k: v for k, v in rec.items() if k != "logs"})
        return logs

    def _split(self, lo, hi, fetch, guard, on_chunk, cooldowns):
        mid = (lo + hi) // 2
        return (self._range(lo, mid, fetch, guard, on_chunk, cooldowns)
                + self._range(mid + 1, hi, fetch, guard, on_chunk, cooldowns))

    def coverage_proof(self, lo, hi):
        """One proof over EVERY stored chunk (reused from earlier attempts and new): each chunk valid and
        bound, chunks tile [lo, hi] exactly (no gap, no overlap), (txHash, logIndex) unique across all
        chunks, per-chunk receipt hashes recorded. Raises CoverageError/CheckpointError otherwise."""
        cursor, seen, n_logs, lines, attempts = lo, set(), 0, [], set()
        for a, b in sorted(self._index):
            if a > cursor:
                raise CoverageError(f"gap: expected block {cursor}, next chunk starts at {a}")
            if a < cursor:
                raise CoverageError(f"overlap: chunk [{a},{b}] overlaps coverage up to {cursor - 1}")
            rec = self.read_chunk(a, b)
            for l in rec["logs"]:
                k = _log_event_key(l)
                if k in seen:
                    raise CoverageError(f"duplicate event {k} (chunk [{a},{b}])")
                seen.add(k)
            n_logs += rec["logCount"]
            attempts.add(rec["attempt_id"])
            lines.append(f"{a} {b} {rec['logCount']} {rec['payload_sha256']} {rec['response_sha256']}")
            cursor = b + 1
        if cursor - 1 != hi:
            raise CoverageError(f"chunks cover up to {cursor - 1}, expected {hi}")
        return {"lo": lo, "hi": hi, "chunks": len(lines), "events": n_logs, "unique_events": len(seen),
                "attempt_ids": sorted(attempts), "binding_sha256": self.binding_sha256,
                "chunks_digest_sha256": hashlib.sha256("\n".join(lines).encode()).hexdigest()}

    def logs(self, lo, hi):
        """All logs of [lo, hi] in block order, from validated chunks (call coverage_proof first)."""
        out = []
        for a, b in sorted(self._index):
            if lo <= a and b <= hi:
                out.extend(self.read_chunk(a, b)["logs"])
        return out


def acquire_logs(store, lo, hi, window, guard=None, on_chunk=None, cooldowns=None, max_retries=3, backoff_s=1.0):
    """ChunkStore.acquire over the store's bound query on its bound (single) provider. `window` is
    clamped to the query shape's cap."""
    q = store.binding["query"]
    window = min(window, shape_cap(q["endpoint"], q["shape"]))

    def fetch(a, b):
        return fetch_logs_scoped(q["endpoint"], a, b, address=q["address"], topics=q["topics"],
                                 max_retries=max_retries, backoff_s=backoff_s)

    return store.acquire(lo, hi, fetch, window, guard=guard, on_chunk=on_chunk, cooldowns=cooldowns)
