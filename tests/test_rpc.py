import json

import pytest

from lib import rpc


def _write_receipts(path, ranges):
    with open(path, "w") as f:
        for start, end in ranges:
            f.write(json.dumps({
                "gate": "test", "endpoint": "drpc", "fromBlock": start, "toBlock": end,
                "addressCount": 0, "topics": None, "logCount": 0, "sha256": "x", "t": 0,
            }) + "\n")


def test_tiling_passes_for_contiguous_ranges(tmp_path):
    path = tmp_path / "receipts.jsonl"
    _write_receipts(path, [(0, 99), (100, 199)])
    assert rpc.assert_tiling(str(path), 0, 199) is True


def test_tiling_fails_on_gap(tmp_path):
    path = tmp_path / "receipts.jsonl"
    _write_receipts(path, [(0, 99), (101, 199)])
    with pytest.raises(AssertionError, match="gap"):
        rpc.assert_tiling(str(path), 0, 199)


def test_tiling_fails_on_overlap(tmp_path):
    path = tmp_path / "receipts.jsonl"
    _write_receipts(path, [(0, 100), (100, 199)])
    with pytest.raises(AssertionError, match="overlap"):
        rpc.assert_tiling(str(path), 0, 199)


def test_cap_enforcement_splits_and_never_exceeds(monkeypatch):
    """A 10,001-block drpc request (cap 10,000) must be split into >= 2
    sub-requests, none of which exceeds the cap."""
    seen_ranges = []

    def fake_fetch_one_range(endpoint, from_block, to_block, address, topics):
        seen_ranges.append((from_block, to_block))
        return [], b"[]"

    monkeypatch.setattr(rpc, "_fetch_one_range", fake_fetch_one_range)

    rpc.get_logs("drpc", 0, 10000, gate=None)  # inclusive span of 10,001 blocks

    assert len(seen_ranges) >= 2
    cap = rpc.ENDPOINTS["drpc"]["block_range_cap"]
    for start, end in seen_ranges:
        assert end - start + 1 <= cap, f"range [{start},{end}] exceeds cap {cap} -- was sent, never split"


def test_swallowed_error_path_fails_the_run(monkeypatch):
    """A persistent failure (e.g. mocked HTTP 500) on one range must raise
    GapError -- never be silently skipped."""

    def always_fails(endpoint, from_block, to_block, address, topics):
        raise rpc.RpcError("mocked 500")

    monkeypatch.setattr(rpc, "_fetch_one_range", always_fails)
    monkeypatch.setattr(rpc.time, "sleep", lambda *_: None)  # don't actually back off in tests

    with pytest.raises(rpc.GapError):
        rpc.get_logs("drpc", 0, 99, gate=None, max_retries=1)
