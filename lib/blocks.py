"""Timestamp -> block resolution by bisection, and a pinned-blocks store
(`config/blocks.json`). All analysis windows are timestamp-anchored; a block
number is resolved once and then pinned so every later gate uses the same
numeric value -- never re-resolved, never `latest`.
"""
import json
import os
from datetime import datetime, timezone

from . import rpc

_HERE = os.path.dirname(os.path.abspath(__file__))
_BLOCKS_PATH = os.path.join(_HERE, "..", "config", "blocks.json")


def load_pinned():
    if not os.path.exists(_BLOCKS_PATH):
        return {}
    with open(_BLOCKS_PATH) as f:
        return json.load(f)


def _save(pinned):
    with open(_BLOCKS_PATH, "w") as f:
        json.dump(pinned, f, indent=2, sort_keys=True)
        f.write("\n")


def pin(name, block, timestamp):
    """Record a resolved block number + its ISO-8601 UTC timestamp under
    `name` in config/blocks.json. Overwrites any prior pin for that name."""
    pinned = load_pinned()
    pinned[name] = {"block": block, "timestamp": timestamp}
    _save(pinned)
    return pinned[name]


def _to_epoch(iso_utc):
    return int(datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).timestamp())


def _to_iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def block_timestamp(endpoint, block_number):
    """ISO-8601 UTC timestamp of a given block number."""
    blk = rpc.get_block_by_number(endpoint, block_number)
    return _to_iso(int(blk["timestamp"], 16))


def block_at(endpoint, iso_utc, lo=0, hi=None):
    """Binary-search the highest block whose timestamp <= the target
    ISO-8601 UTC timestamp. Returns the block number (does not pin it --
    callers pin explicitly via `pin()`)."""
    target = _to_epoch(iso_utc)

    if hi is None:
        hi = rpc.block_number(endpoint)

    def ts(n):
        blk = rpc.get_block_by_number(endpoint, n)
        return int(blk["timestamp"], 16)

    lo_ts, hi_ts = ts(lo), ts(hi)
    if target < lo_ts:
        raise ValueError(f"target {iso_utc} ({target}) is before block {lo}'s timestamp ({lo_ts})")
    if target > hi_ts:
        raise ValueError(f"target {iso_utc} ({target}) is after block {hi}'s (head) timestamp ({hi_ts})")

    while lo < hi:
        mid = (lo + hi + 1) // 2
        mid_ts = ts(mid)
        if mid_ts <= target:
            lo = mid
        else:
            hi = mid - 1
    return lo
