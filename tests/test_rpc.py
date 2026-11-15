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


# --- S7 deferred findings: fallback re-split, malformed JSON, receipt OSError ---

class _FakeResp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("body", [b"<html>502</html>", b"", b"[1,2]", b'{"jsonrpc":"2.0","id":1}'])
def test_post_malformed_body_raises_rpcerror(monkeypatch, body):
    monkeypatch.setattr(rpc.urllib.request, "urlopen", lambda *a, **k: _FakeResp(body))
    with pytest.raises(rpc.RpcError):
        rpc._post("drpc", "eth_blockNumber", [])


def test_post_malformed_json_triggers_retry_then_fallback(monkeypatch):
    """An HTML 200 on the primary must go through retry + fallback, not escape."""
    calls = []

    def fake_fetch(endpoint, a, b, address, topics):
        calls.append(endpoint)
        if endpoint == "drpc":
            monkeypatch.setattr(rpc.urllib.request, "urlopen", lambda *a_, **k: _FakeResp(b"<html>"))
            return rpc._post("drpc", "eth_getLogs", [])
        return [], b"[]"

    monkeypatch.setattr(rpc, "_fetch_one_range", fake_fetch)
    monkeypatch.setattr(rpc.time, "sleep", lambda *_: None)
    out = rpc.get_logs("drpc", 0, 99, gate=None, max_retries=2)
    assert out == []
    assert calls == ["drpc", "drpc", "tenderly"]


def test_fallback_is_resplit_to_fallback_cap(monkeypatch):
    """tenderly (cap 2,000,000) fails -> fallback drpc (cap 10,000) must be
    asked in <=10,000-block pieces, never the tenderly-sized window."""
    seen = []

    def fake_fetch(endpoint, a, b, address, topics):
        seen.append((endpoint, a, b))
        if endpoint == "tenderly":
            raise rpc.RpcError("boom")
        return [], b"[]"

    monkeypatch.setattr(rpc, "_fetch_one_range", fake_fetch)
    monkeypatch.setattr(rpc.time, "sleep", lambda *_: None)
    rpc.get_logs("tenderly", 0, 25_000, gate=None, max_retries=1)
    drpc = [(a, b) for e, a, b in seen if e == "drpc"]
    cap = rpc.ENDPOINTS["drpc"]["block_range_cap"]
    assert drpc == list(rpc._split_ranges(0, 25_000, cap)) and len(drpc) > 3
    assert all(b - a + 1 <= rpc.ENDPOINTS["drpc"]["block_range_cap"] for a, b in drpc)


def test_fallback_receipts_tile_only_on_success(monkeypatch, tmp_path):
    recs = []
    monkeypatch.setattr(rpc, "_append_receipt", lambda gate, r: recs.append(r))
    monkeypatch.setattr(rpc.time, "sleep", lambda *_: None)

    def fake_fetch(endpoint, a, b, address, topics):
        if endpoint == "tenderly":
            raise rpc.RpcError("boom")
        return [{"x": 1}], b"[1]"

    monkeypatch.setattr(rpc, "_fetch_one_range", fake_fetch)
    out = rpc.get_logs("tenderly", 0, 15_000, gate="t", max_retries=1)
    cap = rpc.ENDPOINTS["drpc"]["block_range_cap"]
    want = list(rpc._split_ranges(0, 15_000, cap))
    assert len(out) == len(want)
    assert [(r["fromBlock"], r["toBlock"], r["endpoint"]) for r in recs] == [(a, b, "drpc") for a, b in want]


def test_receipt_oserror_is_not_treated_as_network_error(monkeypatch):
    """Disk-full on the receipt append must propagate -- not re-fetch the
    (successful) range or fall back to the other endpoint."""
    fetches = []

    def fake_fetch(endpoint, a, b, address, topics):
        fetches.append(endpoint)
        return [], b"[]"

    def bad_append(gate, rec):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(rpc, "_fetch_one_range", fake_fetch)
    monkeypatch.setattr(rpc, "_append_receipt", bad_append)
    monkeypatch.setattr(rpc.time, "sleep", lambda *_: None)
    with pytest.raises(OSError):
        rpc.get_logs("drpc", 0, 99, gate="t", max_retries=3)
    assert fetches == ["drpc"]


def test_git_commit_narrow_except(monkeypatch):
    from gates import g0_manifest
    import subprocess

    def boom(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(g0_manifest.subprocess, "check_output", boom)
    with pytest.raises(KeyboardInterrupt):
        g0_manifest._git_commit()

    def nogit(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(g0_manifest.subprocess, "check_output", nogit)
    assert g0_manifest._git_commit() == "unknown"


# --- S15: provider-scoped scoped fetch (full matrix in tests/test_recovery.py) ---

def test_scoped_quota_raises_without_retry_or_fallback(monkeypatch):
    seen = []

    def fetch(endpoint, a, b, address, topics):
        seen.append(endpoint)
        raise rpc.RpcError("HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(rpc, "_fetch_one_range", fetch)
    with pytest.raises(rpc.QuotaError):
        rpc.fetch_logs_scoped("tenderly", 0, 99)
    assert seen == ["tenderly"]  # no retry here (caller cools down), never drpc


def test_scoped_result_cap_and_400_are_bisect_signals(monkeypatch):
    cap = rpc.result_cap("tenderly")
    monkeypatch.setattr(rpc, "_fetch_one_range",
                        lambda *a: ([{"blockNumber": "0x1", "transactionHash": "0x1", "logIndex": "0x0"}] * cap, b"x"))
    with pytest.raises(rpc.ResultCapError):
        rpc.fetch_logs_scoped("tenderly", 0, 99)

    def http400(*a):
        raise rpc.RpcError("HTTP Error 400: Bad Request")

    monkeypatch.setattr(rpc, "_fetch_one_range", http400)
    with pytest.raises(rpc.ResultCapError):
        rpc.fetch_logs_scoped("tenderly", 0, 99)


def test_shape_caps_are_conservative_where_unmeasured():
    assert rpc.shape_cap("mevblocker", "single_address") == 50_000
    assert rpc.shape_cap("mevblocker", "multi_address") == rpc.shape_cap("mevblocker", "topic_only") == 10_000
    assert rpc.query_shape(None, [["0x1"]]) == "topic_only"
    assert rpc.query_shape(["0xa", "0xb"], None) == "multi_address" and rpc.query_shape(["0xa"], None) == "single_address"


def test_get_logs_fallback_false_never_leaves_primary(monkeypatch):
    seen = []

    def fetch(endpoint, a, b, address, topics):
        seen.append(endpoint)
        raise rpc.RpcError("boom")

    monkeypatch.setattr(rpc, "_fetch_one_range", fetch)
    monkeypatch.setattr(rpc.time, "sleep", lambda *_: None)
    with pytest.raises(rpc.GapError):
        rpc.get_logs("tenderly", 0, 99, gate=None, max_retries=2, fallback=False)
    assert seen == ["tenderly", "tenderly"]


def test_classify_hex_block_numbers_are_not_quota():
    msg = "query returned more than 10000 results; try block range 0x1429 to 0x2429"
    assert rpc.classify_error(rpc.RpcError(msg)) == "bisect"
    assert rpc.classify_error(rpc.RpcError("log 0xdeadbeef429cafe at block 14290429")) == "transient"
    assert rpc.classify_error(rpc.RpcError("capacity exceeded")) == "quota"  # 'capacity' wins over 'exceed'
    assert rpc.classify_error(rpc.RpcError('{"code": -32029, "message": "limit"}')) == "quota"
    err = rpc.urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
    assert rpc.classify_error(err) == "quota"


def test_producer_identity_binds_sources_and_never_unknown(monkeypatch, tmp_path):
    f = tmp_path / "p.py"
    f.write_text("a = 1\n")
    head = "ab" * 20
    a = rpc.producer_identity([str(f)], head=head)
    f.write_text("a = 2\n")  # uncommitted edit changes the identity
    assert rpc.producer_identity([str(f)], head=head) != a

    def nogit(*x, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(rpc.subprocess, "check_output", nogit)
    with pytest.raises(RuntimeError, match="producer commit"):
        rpc.producer_identity([str(f)])
    with pytest.raises(RuntimeError):
        rpc.producer_identity([str(f)], head="unknown")
