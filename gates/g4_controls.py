"""Gate 4 -- control baseline panel + matching covariates (both arms).

For the g1b frame survivors (controls) plus the g1 floor-passing exposed
wallets, rebuild the 60-day baseline `wallet_coin_day_pre` exactly as g1 does
(opening balanceOf at pre_period_start, Transfer walk, USDT
DestroyedBlackFunds correction, integers only), apply the same floors, and
write `out/g4/covariates.csv` -- one row per floor-passing wallet of BOTH
arms -- plus `out/g4/wallet_coin_day_pre.csv` (floor-passing wallets only).

HARD RULE: no Transfer / balance / getLogs read for any block after
pre_period_end (15,298,282). Every query goes through `_guard_block`, and at
the end every receipt is asserted <= pre_period_end.

Resumable: opening balances, each Transfer-walk unit, eth balances and the
wallet-age bisection are appended to jsonl checkpoints in out/g4/; re-running
`python3 run_all.py --gate g4 --generation NAME --baseline-blacklist-dir DIR --input-dir ROOT` skips finished units. A checkpoint from a
different frame (sha256 of the address universe) is refused, never reused.

Transfer walk: mevblocker by default (parallel 10,000-block windows; tenderly's 1 GB/day
egress quota stalls a full ~172k-address walk, and the routed main run admits no G4_* env
override). Replay / cross-check runs on tenderly (REPLAY_ENDPOINT). Standalone only:
G4_WALK_ENDPOINT=tenderly walks whole-range 1,000-address OR arrays (~2-4 s per request).
Each (coin, position, 1,000-address batch) is one unit; a position-`from`
unit only credits the `from` leg and a `to` unit only the `to` leg, so no
cross-unit de-duplication is needed (a transfer between two tracked wallets
appears in two units, once per side). Loud-fail only: a failed range is
bisected (on a result-cap error OR a result count >= the endpoint cap); other
failures back off and retry the same request, then raise GapError, which
aborts the gate. The walk calls lib.rpc._fetch_one_range directly: the S7
cross-endpoint fallback of lib.rpc.get_logs does NOT apply to it. 5 % of
walk units (stratified by the endpoint that fetched them) are re-fetched
range by range on a second endpoint and compared by count and sha256 over
the sorted log keys.

Hardening (post security review): point-query replies are strictly validated
(a null / empty / short result is a retry, never a silent 0); decoded logs
are asserted to be this token's Transfer(topic0, 3 topics) with unique
(blockHash, logIndex) per unit and a block->timestamp map that is consistent
and monotone; a balance-identity audit (opening balanceOf + sum of walked
deltas == balanceOf @ pre_period_end, tolerance 0) covers every floor-passer
(or a seeded stratified sample, see G4_AUDIT_MAX); every run writes one generation
dir (out/g4/gen-<id>/) and the `current` symlink is flipped atomically only when every acceptance check
passes; the canonical names (covariates.csv, ...) are symlinks to current/<name>.

Wallet age is a PROXY: block of the first *outgoing* transaction (smallest
block b <= pre_period_end with eth_getTransactionCount(b) >= 1, by batched
bisection); empty if the wallet never sent a tx by pre_period_end. A wallet
that only ever received is "never sent" and has no age.

Exchange-share covariate: NEEDS_CONTEXT -- column left empty. No CEX label
set is invented; supply a verified, citable list to fill it.

Issuer mint/redeem flag (item 28b): transfers to/from the zero address only.
Issuer treasury addresses are not in the g0 outputs (g0 stores pool and
blacklist events, no treasury labels) -- NEEDS_CONTEXT, not guessed.
"""
import csv
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import time
import threading
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from gates import g1_cohort as g1
from gates import g1b_frame
from lib import keccak, rpc

_HERE = os.path.dirname(os.path.abspath(__file__))
_OUT_DIR = os.environ.get("G4_OUT_DIR") or os.path.join(_HERE, "..", "out", "g4")
_G0_DIR = os.path.join(_HERE, "..", "out", "g0")
_G1_DIR = os.path.join(_HERE, "..", "out", "g1")
_G1B_DIR = os.path.join(_HERE, "..", "out", "g1b")
_RECEIPTS_PATH = os.path.join(_OUT_DIR, "receipts.jsonl")

USDC = g1.USDC
USDT = g1.USDT
COINS = g1.COINS
ZERO_ADDRESS = g1._ZERO_ADDRESS
TRANSFER_TOPIC = keccak.topic("Transfer(address,address,uint256)")
assert TRANSFER_TOPIC == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

PRE_START_DATE = g1.PRE_START_DATE
N_DAYS = g1.N_DAYS
FLOOR_MIN_UNITS = g1.FLOOR_MIN_UNITS
FLOOR_MIN_NONZERO_DAYS = g1.FLOOR_MIN_NONZERO_DAYS

WALK = os.environ.get("G4_WALK_ENDPOINT", "mevblocker")  # Transfer walk endpoint (see docstring + _walk_fetch); routed main refuses the env override
RESULT_CAP = {"tenderly": 20_000, "mevblocker": 10_000, "drpc": 10_000}  # a reply this long may be truncated -> bisect
BATCH = "mevblocker"     # batched eth_call / eth_getBalance / eth_getTransactionCount + reverify
WALK_ARRAY = rpc.ENDPOINTS["tenderly"]["or_array_cap"]  # 1,000: unit identity, independent of WALK_ENDPOINT
# Second endpoint for re-verification. NOT drpc: on 2026-10-15 drpc's free plan answered HTTP 400
# ("ranges over 10000 blocks are not supported on free plan") for every eth_getLogs window > ~100
# blocks, which made the old mevblocker->drpc replay infeasible (GapError at [14989561,14999560]).
REPLAY_ENDPOINT = {"tenderly": "mevblocker", "mevblocker": "tenderly"}
REPLAY_WORKERS = {"mevblocker": 6, "tenderly": 2, "drpc": 3}   # tenderly 429s on bursts
if WALK not in REPLAY_ENDPOINT:
    raise RuntimeError(f"G4_WALK_ENDPOINT={WALK!r} must be one of {sorted(REPLAY_ENDPOINT)}")
BALANCE_BATCH = 250      # eth_call batch size (g1/g1c: ~300-499 accepted, 406 above)
ETH_BATCH = 500
NONCE_BATCH = 250
AGE_GROUP = 10_000       # wallets per bisection checkpoint group
REVERIFY_FRACTION = 0.05
REVERIFY_WINDOW = 10_000  # mevblocker's measured real getLogs cap (see g1_cohort)
RECON_N = 10
WORKERS = 6              # mevblocker point queries scale ~linearly with concurrent batches; 8 hit HTTP 429 (handled by backoff)
SEED = 20261015
SCHEMA_VERSION = 1
AUDIT_MAX = int(os.environ.get("G4_AUDIT_MAX", "200000"))  # balance-identity audit: all floor-passers up to this many, else stratified sample
AUDIT_MIN_SAMPLE = 2_000

_WALL = {}  # step -> seconds (this invocation)


# --- hard rule -----------------------------------------------------------

class PostPeriodRead(AssertionError):
    pass


def _guard_block(block, pre_end):
    """Every block number about to be sent to an RPC passes through here."""
    if block > pre_end:
        raise PostPeriodRead(f"refusing to read block {block} > pre_period_end {pre_end}")
    return block


def _assert_receipts_within(path, pre_end):
    """Every receipt's to_block (or point-query block) must be <= pre_end."""
    n = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            top = rec.get("toBlock", rec.get("block"))
            if top is None or top > pre_end:
                raise PostPeriodRead(f"receipt beyond pre_period_end {pre_end}: {line[:200]}")
            n += 1
    return n


# --- pure helpers (unit-tested) -----------------------------------------

def _assert_tiles(ranges, lo, hi):
    """ranges: iterable of (start, end) inclusive. Must tile [lo, hi] exactly."""
    cursor = lo
    for s, e in sorted(ranges):
        if s > cursor:
            raise AssertionError(f"gap: expected block {cursor}, next range starts at {s}")
        if s < cursor:
            raise AssertionError(f"overlap: range [{s},{e}] overlaps coverage up to {cursor - 1}")
        cursor = e + 1
    if cursor - 1 != hi:
        raise AssertionError(f"ranges cover up to {cursor - 1}, expected {hi}")


def reconstruct_series(opening, flows, n_days=N_DAYS):
    """Day-end balances: opening + cumulative flow. Raises on a negative
    balance (a reconstruction error, never clamped)."""
    bal = opening
    out = []
    for d in range(n_days):
        bal += flows.get(d, 0)
        if bal < 0:
            raise AssertionError(f"reconstructed negative balance on day {d}: {bal}")
        out.append(bal)
    return out


def floor_stats(usdc_s, usdt_s):
    """(mean_total_units floor-div, nonzero_days, floor_pass) as g1 computes them."""
    n = len(usdc_s)
    nonzero = sum(1 for u, t in zip(usdc_s, usdt_s) if u > 0 or t > 0)
    mean_total = sum(u + t for u, t in zip(usdc_s, usdt_s)) // n
    return mean_total, nonzero, (mean_total >= FLOOR_MIN_UNITS and nonzero >= FLOOR_MIN_NONZERO_DAYS)


def usdc_share_ppm(mean_usdc, mean_usdt):
    """USDC share of mean holdings in parts per million (integer; empty if no holdings)."""
    tot = mean_usdc + mean_usdt
    return (mean_usdc * 1_000_000) // tot if tot > 0 else None


def bisect_first_send(addrs, nonce_fn, hi):
    """First block b in [0, hi] with nonce(b) >= 1, per address, by batched
    bisection. nonce_fn(list[(addr, block)]) -> list[int]. Returns
    {addr: block | None}; None = nonce(hi) == 0 (never sent by `hi`).
    One nonce_fn call per round covers every still-open address."""
    out = {}
    final = nonce_fn([(a, hi) for a in addrs])
    state = {}
    for a, n in zip(addrs, final):
        if n >= 1:
            state[a] = [0, hi]
        else:
            out[a] = None
    while state:
        open_ = []
        for a, (lo, h) in list(state.items()):
            if lo >= h:
                out[a] = lo
                del state[a]
            else:
                open_.append((a, (lo + h) // 2))
        if not open_:
            break
        for (a, mid), n in zip(open_, nonce_fn(open_)):
            lo, h = state[a]
            state[a] = [lo, mid] if n >= 1 else [mid + 1, h]
    return out


def blacklist_state_at(events, pre_end):
    """events: iterable of (block, log_index, kind) with kind in {'add','remove'}
    for ONE address and ONE coin. State at pre_end considering only events
    with block <= pre_end (later events are never consulted)."""
    state = False
    for blk, li, kind in sorted(e for e in events if e[0] <= pre_end):
        state = (kind == "add")
    return state


def _digest(logs, with_block=False):
    """sha256 over the sorted log keys. with_block=True (stored as "v2:<hex>")
    also covers blockNumber + blockTimestamp, which the bare log key omits."""
    if with_block:
        keys = sorted(rpc._log_key(l) + (l.get("blockNumber"), l.get("blockTimestamp")) for l in logs)
        return "v2:" + hashlib.sha256(json.dumps(keys, sort_keys=True).encode()).hexdigest()
    keys = sorted(rpc._log_key(l) for l in logs)
    return hashlib.sha256(json.dumps(keys, sort_keys=True).encode()).hexdigest()


def _digest_like(stored, logs):
    """Digest of `logs` in the same scheme as the stored digest."""
    return _digest(logs, with_block=stored.startswith("v2:"))


def _log_day(log):
    ts = log.get("blockTimestamp")
    if not ts:
        raise RuntimeError(f"log missing blockTimestamp: tx={log.get('transactionHash')}")
    return (datetime.fromtimestamp(int(ts, 16), tz=timezone.utc).date() - PRE_START_DATE).days


def check_block_timestamps(logs):
    """Day buckets come from the endpoint's blockTimestamp, so check it is
    consistent with blockNumber: one timestamp per block, and non-decreasing
    in block number. Raises on a violation."""
    seen = {}
    for l in logs:
        blk, ts = int(l["blockNumber"], 16), int(l["blockTimestamp"], 16)
        if seen.setdefault(blk, ts) != ts:
            raise AssertionError(f"block {blk} carries two different blockTimestamps")
    prev = None
    for blk in sorted(seen):
        if prev is not None and seen[blk] < prev[1]:
            raise AssertionError(f"blockTimestamp not monotone in blockNumber: block {prev[0]} ts {prev[1]} > block {blk} ts {seen[blk]}")
        prev = (blk, seen[blk])


_WORD32 = re.compile(r"^0x[0-9a-fA-F]{64}$")
_ADDR_WORD = re.compile(r"^0x0{24}[0-9a-fA-F]{40}$")  # 32-byte topic: 12 zero bytes + 20-byte address


def _strict_transfer_shape(l):
    """The g1 decoder is permissive (`data` "0x" -> 0, topics sliced by [-40:]).
    Before it runs, a Transfer must carry exactly 32 bytes of `data` and two
    properly zero-padded address topics; anything else raises."""
    data, topics = l.get("data"), l.get("topics") or []
    if not (isinstance(data, str) and _WORD32.match(data)):
        raise AssertionError(f"Transfer data is not exactly 32 bytes: {data!r} tx={l.get('transactionHash')}")
    for i in (1, 2):
        if not (isinstance(topics[i], str) and _ADDR_WORD.match(topics[i])):
            raise AssertionError(f"Transfer topic[{i}] is not a zero-padded 32-byte address: {topics[i]!r} tx={l.get('transactionHash')}")


def decode_unit_logs(logs, pos, addr_set, pre_start, pre_end, token, by_day=False):
    """Aggregate one walk unit's logs. pos 'from' credits only the from leg
    (-value), pos 'to' only the to leg (+value). Returns
    {addr: [{day: delta}, legs, mint_redeem_legs]}; with by_day=True each record
    carries a 4th element {day: [legs, mint_redeem_legs]} (used by g4_june for
    window-restricted counts). Asserts the endpoint
    returned only what the filter asked for (this token's Transfer, 3 topics,
    tracked address in the right topic slot), nothing outside the window, no
    (blockHash, logIndex) twice, and block/timestamp consistency."""
    agg = {}
    uniq = set()
    token = token.lower()
    check_block_timestamps(logs)
    for l in logs:
        if str(l.get("address", "")).lower() != token:
            raise AssertionError(f"log from {l.get('address')}, expected token {token}: tx={l.get('transactionHash')}")
        topics = l.get("topics") or []
        if len(topics) != 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
            raise AssertionError(f"not a 3-topic Transfer log: tx={l.get('transactionHash')} topics={topics}")
        k = (l.get("blockHash"), l.get("logIndex"))
        if k[0] is None or k[1] is None or k in uniq:
            raise AssertionError(f"missing or duplicate (blockHash, logIndex) {k} in unit: tx={l.get('transactionHash')}")
        uniq.add(k)
        blk = int(l["blockNumber"], 16)
        if not (pre_start < blk <= pre_end):
            raise PostPeriodRead(f"log block {blk} outside ({pre_start}, {pre_end}]")
        _strict_transfer_shape(l)
        frm, to, value = g1._transfer_decode(l)
        me, other, sign = (frm, to, -1) if pos == "from" else (to, frm, 1)
        if me not in addr_set:
            raise AssertionError(f"endpoint returned a log for untracked address {me}")
        day = _log_day(l)
        if not (0 <= day < N_DAYS):
            raise AssertionError(f"Transfer log outside pre-period window: {l['transactionHash']} day {day}")
        rec = agg.setdefault(me, [{}, 0, 0, {}] if by_day else [{}, 0, 0])
        rec[0][day] = rec[0].get(day, 0) + sign * value
        rec[1] += 1
        if by_day:
            dl = rec[3].setdefault(day, [0, 0])
            dl[0] += 1
        if other == ZERO_ADDRESS:
            rec[2] += 1
            if by_day:
                dl[1] += 1
    return agg


def pick_audit_set(passers, arm_of, max_n, seed=SEED, min_sample=AUDIT_MIN_SAMPLE):
    """All floor-passers if <= max_n. Otherwise every exposed passer plus a
    seeded random sample of controls filling the budget (never fewer than
    min_sample controls)."""
    if len(passers) <= max_n:
        return list(passers)
    exposed = [a for a in passers if arm_of[a] != "control"]
    ctrl = [a for a in passers if arm_of[a] == "control"]
    k = min(len(ctrl), max(max_n - len(exposed), min_sample))
    return sorted(exposed + random.Random(seed).sample(ctrl, k))


# --- checkpoints ---------------------------------------------------------

class _Ckpt:
    """Append-only jsonl {k, v}; load() -> dict. flush + fsync per write.
    Load tolerates (and truncates) an unparseable FINAL line only -- a torn
    last write; an unparseable line anywhere else raises."""

    def __init__(self, name):
        self.path = os.path.join(_OUT_DIR, name)
        self.data = {}
        if os.path.exists(self.path):
            self._load()
        self._fh = open(self.path, "a")

    def _load(self):
        with open(self.path, "rb") as f:
            raw = f.read()
        lines = raw.split(b"\n")
        tail = lines.pop()  # b"" when the file ends with a newline
        if tail.strip():
            lines.append(tail)  # unterminated final line
        good = 0  # bytes of fully accepted prefix
        for i, line in enumerate(lines):
            if not line.strip():
                good += len(line) + 1
                continue
            try:
                rec = json.loads(line)
                self.data[rec["k"]] = rec["v"]
            except (ValueError, KeyError, TypeError) as e:
                if i != len(lines) - 1:
                    raise RuntimeError(f"{self.path}: corrupt line {i + 1} (not the final line): {e}") from e
                _log(f"{self.path}: dropping unparseable final line {i + 1} (torn write)")
                with open(self.path, "r+b") as f:
                    f.truncate(good)
                return
            good += len(line) + 1
        if tail.strip():  # valid but unterminated final line: terminate it
            with open(self.path, "ab") as f:
                f.write(b"\n")

    def put(self, k, v):
        self._fh.write(json.dumps({"k": k, "v": v}) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self.data[k] = v

    def close(self):
        self._fh.close()


def _read_ckpt_file(path):
    """Read-only load of a _Ckpt jsonl (no truncation, no append handle). Missing file -> RuntimeError (fail closed)."""
    if not os.path.isfile(path) or os.path.islink(path):
        raise RuntimeError(f"{path} missing: cannot attest the walk/replay endpoints")
    data = {}
    with open(path) as f:
        for line in f:
            if line.strip():
                rec = json.loads(line)
                data[rec["k"]] = rec["v"]
    return data


def assert_single_provider(work_dir):
    """Before publishing: every checkpointed walk range ran on WALK and every reverify record is WALK -> REPLAY_ENDPOINT[WALK].
    A mixed-endpoint (resumed under another G4_WALK_ENDPOINT) or legacy-format checkpoint raises. Returns (walk, replay)."""
    replay = REPLAY_ENDPOINT[WALK]
    walk = _read_ckpt_file(os.path.join(work_dir, "walk_checkpoint.jsonl"))
    seen = Counter(r[4] for v in walk.values() for r in v["ranges"])
    if set(seen) != {WALK}:
        raise RuntimeError(f"walk checkpoint endpoints {dict(seen)} != the single walk endpoint {WALK!r}; refusing to publish")
    rv = _read_ckpt_file(os.path.join(work_dir, "reverify_checkpoint.jsonl"))
    seen_rv = Counter((r.get("source"), r.get("verifier")) if isinstance(r, dict) else None for r in rv.values())
    if set(seen_rv) != {(WALK, replay)}:
        raise RuntimeError(f"reverify checkpoint (source, verifier) {dict(seen_rv)} != {(WALK, replay)}; refusing to publish")
    return WALK, replay


def _assert_walk_within(walk_data, pre_end):
    """Every checkpointed walk range (not only receipts) must end <= pre_end."""
    n = 0
    for k, unit in walk_data.items():
        for r in unit["ranges"]:
            if r[1] > pre_end:
                raise PostPeriodRead(f"walk checkpoint {k} range {r[:2]} beyond pre_period_end {pre_end}")
            n += 1
    return n


_RECEIPT_LOCK = threading.Lock()


def _receipt(rec):
    with _RECEIPT_LOCK, open(_RECEIPTS_PATH, "a") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


def _pmap(fn, items, workers=None):
    """Ordered parallel map; any worker exception propagates (never skipped)."""
    items = list(items)
    if len(items) <= 1:
        return [fn(i) for i in items]
    with ThreadPoolExecutor(workers or WORKERS) as ex:
        return list(ex.map(fn, items))


def _timed(step):
    class _T:
        def __enter__(s):
            s.t = time.time()

        def __exit__(s, *a):
            _WALL[step] = _WALL.get(step, 0.0) + time.time() - s.t
    return _T()


def _log(msg):
    print(f"[g4] {msg}", flush=True)


# --- batched point queries (all guarded + receipted) ---------------------

_HEX_WORD = re.compile(r"^0x[0-9a-fA-F]{64}$")
_HEX_NUM = re.compile(r"^0x[0-9a-fA-F]+$")


def _valid_word(r):
    """eth_call balanceOf: exactly one 32-byte word (66 chars)."""
    if not (isinstance(r, str) and _HEX_WORD.match(r)):
        raise ValueError(f"malformed balanceOf result {r!r}")
    return r


def _valid_num(r):
    """eth_getBalance / eth_getTransactionCount: non-null 0x-hex, >= 1 digit."""
    if not (isinstance(r, str) and _HEX_NUM.match(r)):
        raise ValueError(f"malformed hex quantity result {r!r}")
    return r


_VALIDATORS = {"eth_call": _valid_word, "eth_getBalance": _valid_num, "eth_getTransactionCount": _valid_num}


def _batch_post(endpoint, calls, max_retries=8, backoff_s=1.0, validate=None):
    """Like g1b_frame._batch_post (ids matched, never dropped) but 429 / 5xx /
    network errors back off and retry the SAME batch (g1b bisects on 429, which
    just multiplies requests under a rate limit); only 400/406/413 bisect."""
    if not calls:
        return []
    cfg = rpc.ENDPOINTS[endpoint]
    payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)]
    last = None
    for attempt in range(max_retries):
        req = urllib.request.Request(
            cfg["url"], data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": rpc.USER_AGENT}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = json.loads(resp.read())
            if not isinstance(raw, list):
                raise RuntimeError(f"{endpoint} batch reply is not a list: {str(raw)[:200]}")
            by_id = {}
            for item in raw:
                if "error" in item:
                    raise RuntimeError(f"{endpoint} batch id={item.get('id')} -> {item['error']}")
                by_id[item["id"]] = item["result"]
            missing = [i for i in range(len(calls)) if i not in by_id]
            if missing:
                raise RuntimeError(f"{endpoint} batch reply missing {len(missing)} of {len(calls)} ids (first {missing[:5]})")
            out = [by_id[i] for i in range(len(calls))]
            if validate is not None:
                out = [validate(r) for r in out]
            return out
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 406, 413) and len(calls) > 1:
                mid = len(calls) // 2
                return (_batch_post(endpoint, calls[:mid], max_retries, backoff_s, validate=validate)
                        + _batch_post(endpoint, calls[mid:], max_retries, backoff_s, validate=validate))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, RuntimeError, KeyError, TypeError) as e:
            last = e
        time.sleep(min(60.0, backoff_s * (2 ** attempt)))
    raise RuntimeError(f"{endpoint} batch of {len(calls)} failed after {max_retries} retries: {last}")


def _point_batch(kind, method, items, pre_end, coin=None):
    """items: list of (params_without_block..., block) -> built by caller as
    (method_params, block). Returns raw results. One receipt per call."""
    blocks = [_guard_block(b, pre_end) for _, b in items]
    calls = [(method, p) for p, _ in items]
    if method not in _VALIDATORS:
        raise ValueError(f"no strict result validator for {method}")
    results = _batch_post(BATCH, calls, validate=_VALIDATORS[method])  # raises RuntimeError on failure, never skips
    _receipt({
        "gate": "g4", "kind": kind, "endpoint": BATCH, "coin": coin,
        "fromBlock": min(blocks), "toBlock": max(blocks), "addressCount": len(items),
        "sha256": hashlib.sha256(json.dumps(results, sort_keys=True).encode()).hexdigest(),
        "t": time.time(),
    })
    return results


def _balance_of_many(coin, addrs, block, pre_end, ckpt):
    """Checkpointed, parallel batched balanceOf(addr) at one pinned block."""
    token = COINS[coin]
    todo = []
    out = {}
    for bi in range(0, len(addrs), BALANCE_BATCH):
        key = f"{coin}:{block}:{bi}"
        chunk = addrs[bi:bi + BALANCE_BATCH]
        got = ckpt.data.get(key)
        if got is not None and set(got) == set(chunk):
            out.update(got)
        else:
            todo.append((key, chunk))

    def fetch(kc):
        key, chunk = kc
        items = [([{"to": token, "data": g1._balance_of_data(a)}, hex(block)], block) for a in chunk]
        res = _point_batch("balanceOf", "eth_call", items, pre_end, coin)
        return key, {a: g1._hex_to_int(r) for a, r in zip(chunk, res)}

    for g0_i in range(0, len(todo), WORKERS * 8):  # checkpoint every few worker-rounds
        for key, got in _pmap(fetch, todo[g0_i:g0_i + WORKERS * 8]):
            ckpt.put(key, got)
            out.update(got)
        _log(f"  balanceOf {coin} @ {block}: {min(g0_i + WORKERS * 8, len(todo))}/{len(todo)} batches fetched")
    return out


# --- Transfer walk -------------------------------------------------------

class _ResultCap(Exception):
    """Endpoint refused: more results than its cap (loud, not truncation)."""


def _is_result_cap(err):
    m = str(err).lower()
    return "more than" in m and "results" in m


def _fetch_endpoint(endpoint, token, topics, start, end, pre_end, max_retries=10, backoff_s=2.0):
    """One getLogs on `endpoint` for [start,end]. Result-cap errors raise
    _ResultCap immediately (caller bisects); 429/5xx/network errors back off
    and retry; anything still failing raises GapError."""
    _guard_block(end, pre_end)
    last = None
    for attempt in range(max_retries):
        try:
            return rpc._fetch_one_range(endpoint, start, end, token, topics)
        except rpc.RpcError as e:
            if _is_result_cap(e):
                raise _ResultCap(str(e)) from e
            last = e
        except OSError as e:  # URLError / HTTPError / timeouts
            last = e
        time.sleep(min(120.0, backoff_s * (2 ** attempt)))  # tenderly 429s can last minutes
    raise rpc.GapError(f"{endpoint} [{start},{end}] failed: {last}")


def _fetch_adaptive(endpoint, token, topics, start, end, pre_end):
    """[start,end] on `endpoint` -> (logs, [(s, e, count, digest, endpoint)]).
    Bisects on the endpoint's result cap; at the minimum window a cap hit
    raises (no silent truncation exists, so none is possible to mask)."""
    try:
        result, _ = _fetch_endpoint(endpoint, token, topics, start, end, pre_end)
        if len(result) >= RESULT_CAP[endpoint]:  # at/over the endpoint's cap: may be truncated, never trust
            raise _ResultCap(f"{endpoint} returned {len(result)} >= cap {RESULT_CAP[endpoint]}")
        return result, [(start, end, len(result), _digest(result, with_block=True), endpoint)]
    except _ResultCap as e:
        if end - start + 1 <= 1:
            raise rpc.GapError(f"{endpoint} [{start},{end}] exceeds the result cap at one block: {e}")
        mid = (start + end) // 2
        l1, r1 = _fetch_adaptive(endpoint, token, topics, start, mid, pre_end)
        l2, r2 = _fetch_adaptive(endpoint, token, topics, mid + 1, end, pre_end)
        return l1 + l2, r1 + r2


def _windows(start, end, width=REVERIFY_WINDOW):
    ws = start
    while ws <= end:
        we = min(ws + width - 1, end)
        yield ws, we
        ws = we + 1


def _fetch_windowed(endpoint, token, topics, start, end, pre_end, workers):
    """[start,end] in <=10,000-block windows (mevblocker's real cap), in
    parallel, each bisecting on the endpoint's own result cap."""
    parts = _pmap(lambda w: _fetch_adaptive(endpoint, token, topics, w[0], w[1], pre_end),
                  list(_windows(start, end)), workers)
    return [l for p in parts for l in p[0]], [r for p in parts for r in p[1]]


def _walk_fetch(token, topics, start, end, pre_end):
    """tenderly: one whole-range request per unit (bisected on its 20k-result
    cap). Any other WALK endpoint (the default, mevblocker; tenderly's 1 GB/day
    egress quota makes it unsuitable for a full walk) walks in parallel 10,000-block windows."""
    if WALK == "tenderly":
        return _fetch_adaptive(WALK, token, topics, start, end, pre_end)
    return _fetch_windowed(WALK, token, topics, start, end, pre_end, REPLAY_WORKERS.get(WALK, WORKERS))


def _unit_is_v2(rec):
    """A walk record is v2 only if every range digest is the "v2:" scheme (covers blockNumber +
    blockTimestamp). Legacy digests omit them, so a legacy unit could hide a wrong day bucket."""
    rng = rec.get("ranges") if isinstance(rec, dict) else None
    return bool(rng) and all(str(r[3]).startswith("v2:") for r in rng)


def _unit_topics(pos, chunk):
    tl = [g1._addr_topic(a) for a in chunk]
    return [TRANSFER_TOPIC, tl] if pos == "from" else [TRANSFER_TOPIC, None, tl]


def _run_walk(universe, pre_start, pre_end, ckpt, by_day=False):
    units = []
    for coin in COINS:
        for pos in ("from", "to"):
            for bi in range(0, len(universe), WALK_ARRAY):
                units.append((coin, pos, bi))
    total = len(units)
    done = sum(1 for c, p, b in units if _unit_is_v2(ckpt.data.get(f"{c}:{p}:{b}")))
    legacy = sum(1 for c, p, b in units if f"{c}:{p}:{b}" in ckpt.data) - done
    if legacy:
        _log(f"{legacy} legacy (pre-v2 digest) walk units will be RE-WALKED, never reused")
    _log(f"Transfer walk: {total} units ({done} already checkpointed), {WALK} {WALK_ARRAY}-address arrays, "
         f"blocks {pre_start + 1}..{pre_end}")
    t0 = time.time()
    fresh_done = 0
    for n, (coin, pos, bi) in enumerate(units, 1):
        key = f"{coin}:{pos}:{bi}"
        if _unit_is_v2(ckpt.data.get(key)):
            continue
        chunk = universe[bi:bi + WALK_ARRAY]
        topics = _unit_topics(pos, chunk)
        logs, ranges = _walk_fetch(COINS[coin], topics, pre_start + 1, pre_end, pre_end)
        _assert_tiles([(s, e) for s, e, *_ in ranges], pre_start + 1, pre_end)
        agg = decode_unit_logs(logs, pos, set(chunk), pre_start, pre_end, COINS[coin], by_day=by_day)
        ckpt.put(key, {
            "ranges": [[s, e, cnt, dig, ep] for s, e, cnt, dig, ep in ranges],
            "logs": len(logs),
            "addrs": {a: [sorted(r[0].items()), r[1], r[2]] + ([sorted(r[3].items())] if by_day else [])
                      for a, r in agg.items()},
        })
        for s, e, cnt, dig, ep in ranges:  # receipts only after the unit is durably checkpointed
            _receipt({"gate": "g4", "kind": "getLogs", "unit": key, "endpoint": ep, "fromBlock": s, "toBlock": e,
                      "addressCount": len(chunk), "topics_position": pos, "coin": coin, "logCount": cnt,
                      "sha256": dig, "t": time.time()})
        fresh_done += 1
        el = time.time() - t0
        eta = el / fresh_done * (total - done - fresh_done)
        _log(f"  walk unit {n}/{total} {key}: {len(logs)} logs, {len(agg)} addrs | "
             f"{el:.0f}s elapsed, ETA {eta:.0f}s")
        time.sleep(0.25)  # tenderly 429s on bursts


def _merge_walk(ckpt, universe_set, by_day=False):
    """flows[coin][addr] = {day: delta}; legs[coin][addr]; mr[coin][addr].
    by_day=True (records written with by_day) additionally returns
    dlegs[coin][addr] = {day: [legs, mint_redeem_legs]}."""
    flows = {c: defaultdict(dict) for c in COINS}
    legs = {c: defaultdict(int) for c in COINS}
    mr = {c: defaultdict(int) for c in COINS}
    dlegs = {c: defaultdict(dict) for c in COINS}
    for key, rec in ckpt.data.items():
        if not _unit_is_v2(rec):
            raise RuntimeError(f"walk unit {key} is a legacy (pre-v2) record: digests omit blockNumber/blockTimestamp")
        coin = key.split(":")[0]
        for a, rec_a in rec["addrs"].items():
            if a not in universe_set:
                continue
            days, n, m = rec_a[:3]
            if by_day:
                if len(rec_a) != 4:
                    raise RuntimeError(f"walk unit {key} has no per-day leg counts (not a by_day record)")
                for d, (nl, nm) in rec_a[3]:
                    dd = dlegs[coin][a].setdefault(d, [0, 0])
                    dd[0] += nl
                    dd[1] += nm
            fa = flows[coin][a]
            for d, v in days:
                fa[d] = fa.get(d, 0) + v
            legs[coin][a] += n
            mr[coin][a] += m
    return (flows, legs, mr, dlegs) if by_day else (flows, legs, mr)


# --- reverify ------------------------------------------------------------

def _replay(endpoint, token, topics, s, e, pre_end):
    """Re-fetch [s,e] on the second endpoint in <=10,000-block windows."""
    return _fetch_windowed(endpoint, token, topics, s, e, pre_end, REPLAY_WORKERS[endpoint])[0]


def pick_reverify_units(walk_data, fraction=REVERIFY_FRACTION, seed=SEED, min_per_endpoint=3):
    """Unit-level sample, stratified by the endpoint that fetched the unit's
    ranges: per endpoint max(min_per_endpoint, ceil(fraction * units)) units
    (all of them if fewer). Returns (sorted unit keys, {endpoint: n_units})."""
    by_ep = defaultdict(set)
    for key in walk_data:
        for _, _, _, _, ep in walk_data[key]["ranges"]:
            by_ep[ep].add(key)
    rng = random.Random(seed)
    picked, per = set(), {}
    for ep in sorted(by_ep):
        units = sorted(by_ep[ep])
        n = min(len(units), max(min_per_endpoint, math.ceil(len(units) * fraction)))
        sel = rng.sample(units, n)
        per[ep] = n
        picked.update(sel)
    return sorted(picked), per


REVERIFY_VERSION = 2   # bump when the comparison semantics change; older entries are not trusted


def _rv_record(cnt, dig, source, verifier):
    return {"v": REVERIFY_VERSION, "count": cnt, "digest": dig, "source": source, "verifier": verifier}


def _rv_cached_ok(rec, cnt, dig, source, verifier):
    """A cached reverify entry counts only if it is a dict bound to THIS source digest + log count,
    source endpoint, verifier endpoint and validation version. Legacy `True` entries do not."""
    return isinstance(rec, dict) and rec == _rv_record(cnt, dig, source, verifier)


def _reverify(walk_ckpt, universe, pre_end, rv_ckpt):
    """Re-fetch every range of a stratified sample of units on the second
    endpoint. Returns (n_cached, n_fresh, {endpoint: n_units}). A cached entry is
    accepted only if bound to this range's digest/count/endpoints/version (legacy
    `True` or mismatching entries are re-compared). Mismatch, source == verifier
    endpoint, or nothing compared at all raises."""
    units, per = pick_reverify_units(walk_ckpt.data)
    todo = [(key, *r) for key in units for r in walk_ckpt.data[key]["ranges"]]
    if not todo:
        raise AssertionError("reverify sample is empty -- nothing was re-verified")
    _log(f"cross-endpoint reverify: {len(units)} units ({per}), {len(todo)} ranges")
    n_cached = n_fresh = 0
    for i, (key, s, e, cnt, dig, ep) in enumerate(todo, 1):
        rk = f"{key}:{s}:{e}"
        other = REPLAY_ENDPOINT[ep]
        if other == ep:
            raise AssertionError(f"reverify {rk}: source and verifier endpoint are both {ep}")
        if _rv_cached_ok(rv_ckpt.data.get(rk), cnt, dig, ep, other):
            n_cached += 1
            continue
        coin, pos, bi = key.split(":")
        chunk = universe[int(bi):int(bi) + WALK_ARRAY]
        _guard_block(e, pre_end)
        alt = _replay(other, COINS[coin], _unit_topics(pos, chunk), s, e, pre_end)
        check_block_timestamps(alt)
        if len(alt) != cnt or _digest_like(dig, alt) != dig:
            raise AssertionError(f"reverify mismatch {rk}: {ep} count={cnt} {other} count={len(alt)} "
                                 f"(digest equal: {_digest_like(dig, alt) == dig})")
        _receipt({"gate": "g4", "kind": "reverify", "unit": key, "endpoint": other, "fromBlock": s, "toBlock": e,
                  "logCount": len(alt), "sha256": dig, "sha256_with_block": _digest(alt, with_block=True), "t": time.time()})
        rv_ckpt.put(rk, _rv_record(cnt, dig, ep, other))
        n_fresh += 1
        _log(f"  reverify {i}/{len(todo)} {rk}: {len(alt)} logs match")
    if n_cached + n_fresh == 0:
        raise AssertionError("reverify compared 0 ranges")
    return n_cached, n_fresh, per


# --- inputs --------------------------------------------------------------

def _load_exposed_floor():
    path = os.path.join(_G1_DIR, "cohort.csv")
    out = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r["floor_pass"] == "True":
                out[r["address"]] = r
    return out


def _load_controls():
    path = os.path.join(_G1B_DIR, "frame_survivors.csv")
    with open(path, newline="") as f:
        return sorted(r["address"] for r in csv.DictReader(f))


def _load_destroyed(pre_start, pre_end):
    return g1._load_usdt_destroyed(pre_start, pre_end)


def _load_blacklist_events(universe_set, pre_end):
    """Per-address, per-coin add/remove events with block <= pre_end (later
    events are dropped here, never consulted)."""
    ev = {"USDC": defaultdict(list), "USDT": defaultdict(list)}
    with open(os.path.join(_G0_DIR, "circle_events.csv"), newline="") as f:
        for r in csv.DictReader(f):
            a, b = r["address"].lower(), int(r["block"])
            if a in universe_set and b <= pre_end and r["event"] in ("Blacklisted", "UnBlacklisted"):
                ev["USDC"][a].append((b, int(r["log_index"]), "add" if r["event"] == "Blacklisted" else "remove"))
    with open(os.path.join(_G0_DIR, "usdt_events.csv"), newline="") as f:
        for r in csv.DictReader(f):
            a, b = r["address"].lower(), int(r["block"])
            if a in universe_set and b <= pre_end and r["event"] in ("AddedBlackList", "RemovedBlackList"):
                ev["USDT"][a].append((b, int(r["log_index"]), "add" if r["event"] == "AddedBlackList" else "remove"))
    return ev


def _resolve_blacklist(cfg, pinned, universe_set):
    """(events, info). info is None on the default out/g0 path; on the opt-in baseline path it records
    the source (cfg|env), realpath and manifest sha256, and the env-var route is logged loudly."""
    from_cfg = cfg.get("baseline_blacklist_dir")
    bl_dir = from_cfg or os.environ.get("G4_BASELINE_BLACKLIST_DIR")
    if not bl_dir:
        return _load_blacklist_events(universe_set, pinned["pre_period_end"]["block"]), None
    from gates import g0_baseline_blacklist as g0b
    base = g0b.load_baseline(bl_dir, "main", pinned=pinned)
    info = {"source": "cfg" if from_cfg else "env:G4_BASELINE_BLACKLIST_DIR", "path": bl_dir,
            "realpath": os.path.realpath(bl_dir), "manifest_sha256": base.manifest_sha256}
    if not from_cfg:
        _log(f"!! blacklist flags come from the baseline manifest {info['realpath']} via ENV G4_BASELINE_BLACKLIST_DIR "
             f"(manifest sha256 {base.manifest_sha256[:12]}), NOT from out/g0")
    return base.events(universe_set), info


def _git_commit():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=_HERE, text=True).strip()
    except (subprocess.CalledProcessError, OSError):
        return "unknown"


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_schema(name, columns, pk, n_rows, sha, input_hashes, notes, outdir=None):
    with open(os.path.join(outdir or _OUT_DIR, f"{name}.schema.json"), "w") as f:
        json.dump({
            "schema_version": SCHEMA_VERSION,
            "columns": [{"name": c[0], "dtype": c[1], "units": c[2]} for c in columns],
            "primary_key": list(pk), "null_policy": "empty string = missing / not applicable",
            "row_count": n_rows, "input_hashes": input_hashes, "producing_commit": _git_commit(),
            "sha256": sha, "notes": notes,
        }, f, indent=2, sort_keys=True)
        f.write("\n")


# --- output schemas (shared with g4_june) ---------------------------------

COV_COLS = [
    ("wallet_id", "int", "dense id by sorted address over this gate's universe (controls + exposed floor-passers)"),
    ("address", "str", "0x, 42 chars, lowercase"),
    ("arm", "str", "control|withdrawal|deposit|both (exposed arms from g1)"),
    ("mean_total_units", "int", "mean over 60 pre-period days of (USDC+USDT) base units, floor div"),
    ("mean_usdc_units", "int", "mean USDC base units, floor div"),
    ("mean_usdt_units", "int", "mean USDT base units, floor div"),
    ("nonzero_days", "int", "pre-period days with nonzero total balance"),
    ("usdc_share_ppm", "int", "mean_usdc / (mean_usdc + mean_usdt) in parts per million; empty if no holdings"),
    ("transfer_count_total", "int", "baseline Transfer legs (from + to appearances), USDC + USDT"),
    ("transfer_count_usdc", "int", "baseline USDC Transfer legs"),
    ("transfer_count_usdt", "int", "baseline USDT Transfer legs"),
    ("eth_balance_wei_units", "int", "eth_getBalance at pre_period_end, wei"),
    ("first_send_block", "int", "PROXY for wallet age: block of first outgoing tx (bisection on eth_getTransactionCount); empty = never sent by pre_period_end"),
    ("wallet_age_blocks", "int", "pre_period_end - first_send_block; empty if never sent"),
    ("mint_redeem_legs", "int", "baseline transfers to/from the zero address (issuer mint/redeem, zero address only; treasury addresses NEEDS_CONTEXT)"),
    ("issuer_mint_redeem_flag", "bool", "mint_redeem_legs > 0"),
    ("blacklisted_usdc_at_pre_end", "bool", "USDC blacklist membership at pre_period_end (g0 Blacklisted/UnBlacklisted, block <= pre_period_end); flag, not a drop"),
    ("blacklisted_usdt_at_pre_end", "bool", "USDT blacklist membership at pre_period_end (g0 AddedBlackList/RemovedBlackList); flag, not a drop"),
    ("exchange_share", "", "NEEDS_CONTEXT: empty -- no verified, citable CEX label list supplied"),
]

PANEL_COLS = [
    ("wallet_id", "int", "dense id, same as covariates.csv"), ("coin", "str", "USDC|USDT"),
    ("date", "str", "UTC YYYY-MM-DD"), ("day_index", "int", "days since pre-period start (2022-06-09), 0-based"),
    ("balance_units", "int", "base units (6 dec)"), ("arm", "str", "control|withdrawal|deposit|both"),
    ("nonzero", "bool", "balance_units > 0 this day (this coin)"),
]


# --- run -----------------------------------------------------------------

# --- generation promotion ------------------------------------------------

_CANONICAL = ("covariates.csv", "wallet_coin_day_pre.csv", "covariates.schema.json", "wallet_coin_day_pre.schema.json")


def _symlink_atomic(target, link):
    tmp = f"{link}.tmp-{os.getpid()}"
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, link)


def promote_generation(out_dir, gen_name, files=_CANONICAL):
    """Make generation dir `out_dir/gen_name` current with ONE atomic step: flip the
    `current` symlink. The canonical names (out_dir/covariates.csv, ...) are fixed relative
    symlinks `current/<name>` (created once), so every reader sees files of a single run."""
    gen = os.path.join(out_dir, gen_name)
    for fn in files:
        if not os.path.exists(os.path.join(gen, fn)):
            raise RuntimeError(f"generation {gen_name} is missing {fn}; not promoting")
    _symlink_atomic(gen_name, os.path.join(out_dir, "current"))
    for fn in files:
        link = os.path.join(out_dir, fn)
        if not (os.path.islink(link) and os.readlink(link) == f"current/{fn}"):
            _symlink_atomic(f"current/{fn}", link)  # one-time migration of pre-generation regular files


def prune_generations(out_dir):
    """Keep the current generation, the newest older one (rollback) and the newest failed one."""
    cur = os.path.basename(os.path.realpath(os.path.join(out_dir, "current")))
    gens = sorted(d for d in os.listdir(out_dir) if d.startswith("gen-") and os.path.isdir(os.path.join(out_dir, d)))
    older = [g for g in gens if g < cur]
    keep = {cur, *older[-1:]}
    failed = sorted(d for d in os.listdir(out_dir) if d.startswith("failed-"))
    keep.update(failed[-1:])
    for d in gens + failed:
        if d not in keep:
            shutil.rmtree(os.path.join(out_dir, d), ignore_errors=True)


def run(cfg):
    os.makedirs(_OUT_DIR, exist_ok=True)
    gen_name = "gen-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + _git_commit()[:8]
    for d in os.listdir(_OUT_DIR):  # crash leftovers: a generation dir that never got its summary
        dp = os.path.join(_OUT_DIR, d)
        if d.startswith("gen-") and os.path.isdir(dp) and not os.path.exists(os.path.join(dp, "summary.md")):
            shutil.rmtree(dp)
    stage = os.path.join(_OUT_DIR, gen_name)
    os.makedirs(stage)
    pinned = cfg["blocks"]
    pre_start = pinned["pre_period_start"]["block"]
    pre_end = pinned["pre_period_end"]["block"]
    t_run = time.time()
    checks = []

    exposed = _load_exposed_floor()
    controls = [a for a in _load_controls() if a not in exposed]
    sample_n = int(os.environ.get("G4_SAMPLE_CONTROLS", "0"))
    if sample_n:
        controls = controls[:sample_n]
        exposed = dict(sorted(exposed.items())[:max(1, sample_n // 10)])
        _log(f"SAMPLE MODE: {len(controls)} controls + {len(exposed)} exposed (out dir {_OUT_DIR})")
    overlap = len(set(_load_controls()) & set(exposed))
    arm_of = {a: "control" for a in controls}
    for a, r in exposed.items():
        arm_of[a] = r["arm"]
    universe = sorted(arm_of)
    universe_set = set(universe)
    wallet_id = {a: i for i, a in enumerate(universe)}
    frame_sha = hashlib.sha256("\n".join(universe).encode()).hexdigest()
    _log(f"universe {len(universe)} = {len(controls)} controls + {len(exposed)} exposed floor-passers "
         f"({overlap} frame survivors were exposed and moved to the exposed arm); frame sha {frame_sha[:12]}")

    meta_path = os.path.join(_OUT_DIR, "meta.json")
    meta = {"frame_sha256": frame_sha, "pre_start": pre_start, "pre_end": pre_end, "walk_array": WALK_ARRAY}
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            if json.load(f) != meta:
                raise RuntimeError(f"{meta_path} differs from this run's frame/blocks -- stale checkpoints; "
                                   "move out/g4 aside to start fresh")
    else:
        with open(meta_path, "w") as f:
            json.dump(meta, f, sort_keys=True)

    # --- opening balances ---
    bal_ck = _Ckpt("balances_checkpoint.jsonl")
    with _timed("opening balances"):
        _log(f"opening balanceOf @ {pre_start} for {len(universe)} wallets x 2 coins on {BATCH}")
        opening = {c: _balance_of_many(c, universe, pre_start, pre_end, bal_ck) for c in COINS}

    # --- Transfer walk ---
    walk_ck = _Ckpt("walk_checkpoint.jsonl")
    with _timed("transfer walk"):
        _run_walk(universe, pre_start, pre_end, walk_ck)
    flows, legs, mr = _merge_walk(walk_ck, universe_set)

    # --- USDT DestroyedBlackFunds correction (applied to every tracked wallet; g1 gated on USDT
    # participation -- see SPEC S8 Deviation) ---
    destroyed = [r for r in _load_destroyed(pre_start, pre_end) if r["address"] in universe_set]
    destroyed_applied = 0
    for r in destroyed:
        day = (date.fromisoformat(r["date"]) - PRE_START_DATE).days
        if 0 <= day < N_DAYS:
            fa = flows["USDT"][r["address"]]
            fa[day] = fa.get(day, 0) - r["amount"]
            destroyed_applied += 1
    _log(f"applied {destroyed_applied} in-window USDT DestroyedBlackFunds events to tracked wallets")

    # --- reconstruction + floors (series kept only for floor-passers) ---
    series = {}
    stats = {}
    with _timed("reconstruct"):
        for a in universe:
            s_usdc = reconstruct_series(opening["USDC"][a], flows["USDC"].get(a, {}))
            s_usdt = reconstruct_series(opening["USDT"][a], flows["USDT"].get(a, {}))
            mean_total, nonzero, ok = floor_stats(s_usdc, s_usdt)
            stats[a] = (mean_total, nonzero, ok, sum(s_usdc) // N_DAYS, sum(s_usdt) // N_DAYS)
            if ok:
                series[a] = (s_usdc, s_usdt)
    passers = sorted(series)
    ctrl_pass = [a for a in passers if arm_of[a] == "control"]
    exp_pass = [a for a in passers if arm_of[a] != "control"]
    _log(f"floor-passing: {len(passers)} ({len(ctrl_pass)} control, {len(exp_pass)} exposed of {len(exposed)})")

    # --- exposed re-derivation vs g1 cohort.csv (informational) ---
    diff = [a for a in exposed if stats[a][0] != int(exposed[a]["pre_mean_total_units"])]
    _log(f"exposed re-derivation vs g1 cohort.csv pre_mean_total_units: {len(diff)}/{len(exposed)} differ")

    # --- ETH balance at pre_end (floor-passers) ---
    eth_ck = _Ckpt("eth_checkpoint.jsonl")
    eth = {}
    with _timed("eth balance"):
        _log(f"eth_getBalance @ {pre_end} for {len(passers)} floor-passing wallets")
        todo = []
        for bi in range(0, len(passers), ETH_BATCH):
            chunk = passers[bi:bi + ETH_BATCH]
            got = eth_ck.data.get(str(bi))
            if got is not None and set(got) == set(chunk):
                eth.update(got)
            else:
                todo.append((str(bi), chunk))

        def fetch_eth(kc):
            k, chunk = kc
            res = _point_batch("eth_getBalance", "eth_getBalance", [([a, hex(pre_end)], pre_end) for a in chunk], pre_end)
            return k, {a: g1._hex_to_int(r) for a, r in zip(chunk, res)}

        for k, got in _pmap(fetch_eth, todo):
            eth_ck.put(k, got)
            eth.update(got)

    # --- wallet age: first outgoing tx via bisection on nonce ---
    age_ck = _Ckpt("age_checkpoint.jsonl")
    first_send = {}

    def nonce_fn(pairs):
        parts = [pairs[i:i + NONCE_BATCH] for i in range(0, len(pairs), NONCE_BATCH)]
        res = _pmap(lambda part: _point_batch("eth_getTransactionCount", "eth_getTransactionCount",
                                              [([a, hex(b)], b) for a, b in part], pre_end), parts)
        return [g1._hex_to_int(r) for part in res for r in part]

    with _timed("wallet age"):
        _log(f"wallet age: nonce bisection over [0, {pre_end}] for {len(passers)} wallets "
             f"in groups of {AGE_GROUP} ({BATCH}, batch {NONCE_BATCH})")
        t_age = time.time()
        for gi in range(0, len(passers), AGE_GROUP):
            group = passers[gi:gi + AGE_GROUP]
            got = age_ck.data.get(str(gi))
            if got is None or set(got) != set(group):
                got = bisect_first_send(group, nonce_fn, pre_end)
                age_ck.put(str(gi), got)
            first_send.update(got)
            _log(f"  age group {gi // AGE_GROUP + 1}/{-(-len(passers) // AGE_GROUP)} done, "
                 f"{time.time() - t_age:.0f}s elapsed")

    # --- blacklist flags (baseline, <= pre_end only) ---
    # Opt-in (cfg['baseline_blacklist_dir'] / G4_BASELINE_BLACKLIST_DIR): events from an accepted
    # g0_baseline_blacklist generation (deployment -> pre_period_end, main cutoff) instead of out/g0.
    bl, bl_info = _resolve_blacklist(cfg, pinned, universe_set)
    if bl_info:
        with open(os.path.join(stage, "baseline_blacklist.json"), "w") as f:
            json.dump(bl_info, f, sort_keys=True, indent=1)

    # --- g2-style reconciliation: 10 control floor-passers x 2 coins, balanceOf @ pre_end, tolerance 0 ---
    rng = random.Random(SEED)
    ranked = sorted(ctrl_pass, key=lambda a: (stats[a][0], a))
    pick = []
    if ranked:
        step = max(1, len(ranked) // RECON_N)
        pick = [ranked[min(i * step + rng.randrange(step), len(ranked) - 1)] for i in range(min(RECON_N, len(ranked)))]
        pick = sorted(set(pick))
    recon_ck = _Ckpt("recon_checkpoint.jsonl")
    mism = []
    with _timed("reconciliation"):
        for coin in COINS:
            fresh = _balance_of_many(coin, pick, pre_end, pre_end, recon_ck)
            for a in pick:
                recon = series[a][0 if coin == "USDC" else 1][-1]
                if recon != fresh[a]:
                    mism.append(f"{a} {coin}: reconstructed={recon} balanceOf={fresh[a]}")
    checks.append((f"day-end reconstructed balance == balanceOf @ pre_period_end, {len(pick)} control wallets x 2 coins, tolerance 0",
                   bool(pick) and not mism, "; ".join(mism) or f"ok ({len(pick)} wallets, {2 * len(pick)} pairs)"))

    # --- balance-identity audit: opening + sum(walked deltas) == balanceOf @ pre_end, tolerance 0, per coin ---
    audit_set = pick_audit_set(passers, arm_of, AUDIT_MAX)
    audit_ck = _Ckpt("audit_checkpoint.jsonl")
    audit_mism = []
    with _timed("balance-identity audit"):
        _log(f"balance-identity audit: balanceOf @ {pre_end} for {len(audit_set)} of {len(passers)} floor-passers x 2 coins")
        for ci, coin in enumerate(COINS):
            fresh = _balance_of_many(coin, audit_set, pre_end, pre_end, audit_ck)
            for a in audit_set:
                recon = series[a][ci][-1]
                if recon != fresh[a]:
                    audit_mism.append((a, arm_of[a], coin, recon, fresh[a]))
    if audit_mism:
        with open(os.path.join(stage, "audit_mismatches.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["address", "arm", "coin", "reconstructed", "balanceOf_pre_end"])
            w.writerows(audit_mism)
    checks.append((f"balance identity: opening balanceOf @ {pre_start} + sum of walked daily deltas == balanceOf @ pre_period_end, "
                   f"{len(audit_set)} of {len(passers)} floor-passers x 2 coins, tolerance 0",
                   bool(audit_set) and not audit_mism,
                   (f"{len(audit_mism)} mismatches: " + "; ".join(f"{a} {c} recon={r} chain={f}" for a, _, c, r, f in audit_mism[:50])
                    + (" ... (full list: audit_mismatches.csv)" if len(audit_mism) > 50 else ""))
                   if audit_mism else f"0 mismatches ({len(audit_set)} wallets, {2 * len(audit_set)} pairs)"))

    # --- walk ranges at/over their endpoint's result cap (cannot be re-asserted after the fact for old units) ---
    capped = [(k, r) for k in walk_ck.data for r in walk_ck.data[k]["ranges"]
              if r[2] >= RESULT_CAP.get(r[4], float("inf"))]
    checks.append(("no checkpointed walk range returned >= its endpoint result cap (possible truncation)", not capped,
                   f"{len(capped)} ranges at/over cap" + (f": {[(k, r[0], r[1], r[2]) for k, r in capped[:10]]}" if capped else "")))

    # --- reverify ---
    rv_ck = _Ckpt("reverify_checkpoint.jsonl")
    with _timed("reverify"):
        rv_cached, rv_fresh, rv_per = _reverify(walk_ck, universe, pre_end, rv_ck)
    checks.append(("walk units re-verified on a second endpoint, stratified by walk endpoint (count + sha256); "
                   "cached entries must be bound to digest/count/endpoints/version", rv_cached + rv_fresh > 0,
                   f"{rv_cached + rv_fresh} ranges matched: {rv_cached} from bound cache, {rv_fresh} freshly compared; "
                   f"units per endpoint {rv_per}"))

    # --- covariates.csv + panel ---
    cov_path = os.path.join(stage, "covariates.csv")
    with open(cov_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[c[0] for c in COV_COLS])
        w.writeheader()
        for a in passers:
            mean_total, nonzero, _, m_usdc, m_usdt = stats[a]
            fs = first_send[a]
            share = usdc_share_ppm(m_usdc, m_usdt)
            lt, lu, ms = legs["USDC"].get(a, 0), legs["USDT"].get(a, 0), mr["USDC"].get(a, 0) + mr["USDT"].get(a, 0)
            w.writerow({
                "wallet_id": wallet_id[a], "address": a, "arm": arm_of[a], "mean_total_units": mean_total,
                "mean_usdc_units": m_usdc, "mean_usdt_units": m_usdt, "nonzero_days": nonzero,
                "usdc_share_ppm": "" if share is None else share,
                "transfer_count_total": lt + lu, "transfer_count_usdc": lt, "transfer_count_usdt": lu,
                "eth_balance_wei_units": eth[a],
                "first_send_block": "" if fs is None else fs,
                "wallet_age_blocks": "" if fs is None else pre_end - fs,
                "mint_redeem_legs": ms, "issuer_mint_redeem_flag": ms > 0,
                "blacklisted_usdc_at_pre_end": blacklist_state_at(bl["USDC"].get(a, ()), pre_end),
                "blacklisted_usdt_at_pre_end": blacklist_state_at(bl["USDT"].get(a, ()), pre_end),
                "exchange_share": "",
            })
    cov_sha = _sha256_file(cov_path)

    panel_path = os.path.join(stage, "wallet_coin_day_pre.csv")
    n_panel = 0
    with open(panel_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[c[0] for c in PANEL_COLS])
        w.writeheader()
        for a in passers:
            for ci, coin in enumerate(COINS):
                for d, bal in enumerate(series[a][ci]):
                    w.writerow({"wallet_id": wallet_id[a], "coin": coin,
                                "date": (PRE_START_DATE + timedelta(days=d)).isoformat(), "day_index": d,
                                "balance_units": bal, "arm": arm_of[a], "nonzero": bal > 0})
                    n_panel += 1
    panel_sha = _sha256_file(panel_path)

    in_hash = [frame_sha] + ([bl_info['manifest_sha256']] if bl_info else [])
    _write_schema("covariates", COV_COLS, ("wallet_id",), len(passers), cov_sha, in_hash,
                  "Floor-passing wallets of both arms. Confidential (address-level): never committed. "
                  "exchange_share is NEEDS_CONTEXT (empty). first_send_block is a proxy for wallet age.", outdir=stage)
    _write_schema("wallet_coin_day_pre", PANEL_COLS, ("wallet_id", "coin", "day_index"), n_panel, panel_sha, in_hash,
                  "Floor-passing wallets only (both arms); dense over 60 pre-period days x 2 coins.", outdir=stage)

    # --- hard-rule assertion + final checks ---
    n_receipts = _assert_receipts_within(_RECEIPTS_PATH, pre_end)
    _assert_walk_within(walk_ck.data, pre_end)
    checks.append((f"no receipt beyond pre_period_end ({pre_end})", True, f"{n_receipts} receipts, max to_block <= {pre_end}"))
    unit_issues = (g1._validate_units_columns(cov_path, ["mean_total_units", "mean_usdc_units", "mean_usdt_units",
                                                         "eth_balance_wei_units"])
                   + g1._validate_units_columns(panel_path, ["balance_units"]))
    checks.append(("every *_units column matches ^-?[0-9]+$", not unit_issues, "; ".join(unit_issues) or "ok"))
    checks.append(("schema.json sidecars present",
                   all(os.path.exists(os.path.join(stage, f"{n}.schema.json")) for n in ("covariates", "wallet_coin_day_pre")), "ok"))
    never_sent = sum(1 for a in passers if first_send[a] is None)
    mix = Counter(r[4] for k in walk_ck.data for r in walk_ck.data[k]["ranges"])
    mix_s = ", ".join(f"{ep} {n}" for ep, n in sorted(mix.items())) + f" (this invocation's walk endpoint={WALK}, replay endpoint={REPLAY_ENDPOINT[WALK]})"

    passed = sum(1 for _, ok, _ in checks if ok)
    failed = [(n, d) for n, ok, d in checks if not ok]
    walls = "; ".join(f"{k} {v:.0f}s" for k, v in _WALL.items())
    lines = [
        "# Gate 4 -- control baseline panel + matching covariates (both arms)",
        "",
        f"Universe: {len(universe)} wallets = {len(controls)} control (g1b frame survivors, minus {overlap} that are exposed) "
        f"+ {len(exposed)} exposed floor-passing (g1 cohort.csv floor_pass).",
        f"Baseline: {pre_start + 1}..{pre_end} (60 days); opening balanceOf @ {pre_start}; Transfer walk endpoint mix (ranges): {mix_s}; "
        f"USDT DestroyedBlackFunds events applied: {destroyed_applied}.",
        f"**Floor-passing control wallets: {len(ctrl_pass)} of {len(controls)}.** "
        f"Floor-passing exposed re-derived: {len(exp_pass)} of {len(exposed)} "
        f"(g1 reported {len(exposed)}; {len(diff)} differ on pre_mean_total_units vs g1 cohort.csv -- informational).",
        f"Floor: mean total >= {FLOOR_MIN_UNITS} base units ($100) and >= {FLOOR_MIN_NONZERO_DAYS} nonzero days. "
        "Baseline blacklist membership is flagged in covariates.csv, not dropped.",
        (f"Blacklist source: baseline manifest {bl_info['realpath']} (via {bl_info['source']}, sha256 {bl_info['manifest_sha256']})."
         if bl_info else "Blacklist source: out/g0 event files (default path)."),
        f"Wallet age (proxy: first outgoing tx block): {len(passers) - never_sent} have one, {never_sent} never sent by pre_period_end.",
        f"Issuer mint/redeem flag (zero address only): {sum(1 for a in passers if mr['USDC'].get(a, 0) + mr['USDT'].get(a, 0) > 0)} wallets.",
        "",
        "## NEEDS_CONTEXT",
        "",
        "- `exchange_share`: column left EMPTY. No verified, citable CEX label list was supplied; none is invented.",
        "- Issuer treasury addresses (item 28b): not present in the g0 outputs; flag covers the zero address only.",
        "",
        "## Acceptance checks", "", "| check | result | detail |", "|---|---|---|",
    ]
    for name, ok, detail in checks:
        lines.append(f"| {name} | {'PASS' if ok else 'FAIL'} | {detail} |")
    lines += [
        "", f"{passed}/{len(checks)} checks passed.", "",
        f"Generation: `{gen_name}` (all outputs below come from this one run; `out/g4/current` -> this directory).", "",
        f"Wall clock this invocation: {time.time() - t_run:.0f}s ({walls}). Resumable: re-run the same command.",
        "",
        "## Outputs", "",
        f"- `covariates.csv` ({len(passers)} rows, sha256 `{cov_sha}`) -- address-level, confidential, untracked",
        f"- `wallet_coin_day_pre.csv` ({n_panel} rows, sha256 `{panel_sha}`) -- address-level, confidential, untracked",
    ]
    body = "\n".join(lines) + "\n"
    if failed:  # nothing is promoted; the generation is kept for inspection
        with open(os.path.join(stage, "summary.FAILED.md"), "w") as f:
            f.write(body)
        failed_dir = os.path.join(_OUT_DIR, "failed-" + gen_name[4:])
        os.replace(stage, failed_dir)
        with open(os.path.join(_OUT_DIR, "summary.FAILED.md"), "w") as f:
            f.write(body)
    else:
        with open(os.path.join(stage, "summary.md"), "w") as f:
            f.write(body)
        promote_generation(_OUT_DIR, gen_name)  # the single atomic switch
        # summary.md is a real file (it is the committed artefact); it names its generation
        with open(os.path.join(_OUT_DIR, "summary.md.tmp"), "w") as f:
            f.write(body)
        os.replace(os.path.join(_OUT_DIR, "summary.md.tmp"), os.path.join(_OUT_DIR, "summary.md"))
        prune_generations(_OUT_DIR)
    _log(f"{passed}/{len(checks)} acceptance checks passed")
    for name, detail in failed:
        _log(f"FAIL: {name} -- {detail}")
    return 0 if not failed else 1
