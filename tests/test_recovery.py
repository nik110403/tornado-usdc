"""Census recovery: lib.rpc.ChunkStore / fetch_logs_scoped and g4_june's sweep wiring. No network."""
import json
import os

import pytest

from gates import g4_controls as g4
from gates import g4_june as gj
from lib import rpc

T0 = "0x" + "aa" * 32
A1 = "0x" + "11" * 20
POOLS = [A1, "0x" + "22" * 20]


def _log(block, idx=0, addr=A1, tx=None, topic=T0):
    return {"blockNumber": hex(block), "transactionHash": tx or "0x" + f"{block:032x}{idx:032x}",
            "logIndex": hex(idx), "address": addr, "topics": [topic], "data": "0x"}


class Chain:
    """Fake provider: serves logs from `events`; records every request; `fail(endpoint, a, b)` may raise."""

    def __init__(self, events, fail=None):
        self.events, self.fail, self.calls = events, fail, []

    def __call__(self, endpoint, a, b, address, topics):
        self.calls.append((endpoint, a, b))
        if self.fail:
            self.fail(endpoint, a, b)
        out = [l for l in self.events if a <= int(l["blockNumber"], 16) <= b]
        return out, json.dumps(out).encode()


def _store(tmp_path, endpoint="tenderly", address=None, commit="c1", cfg="cfg1", inputs=None, name="s"):
    topics = [[T0]]
    return rpc.ChunkStore(str(tmp_path / name), rpc.make_binding(commit, cfg, inputs or {"g0": "h"}, endpoint, address, topics))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(rpc.time, "sleep", lambda *_: None)


def _install(monkeypatch, chain):
    monkeypatch.setattr(rpc, "_fetch_one_range", chain)
    return chain


def test_interrupted_run_resumes_without_refetching_completed_chunks(monkeypatch, tmp_path):
    events = [_log(b) for b in (5, 150, 250, 399)]

    def boom(endpoint, a, b):
        if a >= 200:
            raise rpc.GapError("provider down")

    chain = _install(monkeypatch, Chain(events, fail=boom))
    st = _store(tmp_path)
    with pytest.raises(rpc.GapError):
        rpc.acquire_logs(st, 0, 399, 100)
    done = [(a, b) for _, a, b in chain.calls if a < 200]
    assert done == [(0, 99), (100, 199)]
    # new process: fresh store object over the same directory, healthy provider
    chain2 = _install(monkeypatch, Chain(events))
    st2 = _store(tmp_path)
    logs = rpc.acquire_logs(st2, 0, 399, 100)
    assert [(a, b) for _, a, b in chain2.calls] == [(200, 299), (300, 399)]  # nothing completed was refetched
    assert len(logs) == 4
    proof = st2.coverage_proof(0, 399)
    assert proof["chunks"] == 4 and proof["events"] == proof["unique_events"] == 4
    assert len(proof["attempt_ids"]) == 2  # cross-attempt coverage: one proof spans both attempts' chunks


def test_every_attempt_is_preserved(monkeypatch, tmp_path):
    chain = _install(monkeypatch, Chain([_log(1)], fail=lambda e, a, b: (_ for _ in ()).throw(rpc.GapError("x"))))
    st = _store(tmp_path)
    with pytest.raises(rpc.GapError):
        rpc.acquire_logs(st, 0, 99, 100)
    _install(monkeypatch, Chain([_log(1)]))
    rpc.acquire_logs(_store(tmp_path), 0, 99, 100)
    kinds = [json.loads(l)["kind"] for l in open(tmp_path / "s" / "attempts.jsonl")]
    assert kinds == ["open", "error", "open", "ok"]


def test_quota_cools_down_same_range_never_bisects_or_falls_back(monkeypatch, tmp_path):
    n = {"k": 0}

    def quota(endpoint, a, b):
        n["k"] += 1
        if n["k"] <= 2:
            raise rpc.RpcError("HTTP Error 429: Too Many Requests")

    chain = _install(monkeypatch, Chain([_log(3)], fail=quota))
    slept = []
    monkeypatch.setattr(rpc.time, "sleep", lambda s: slept.append(s))
    logs = rpc.acquire_logs(_store(tmp_path), 0, 99, 100, cooldowns=(7, 9))
    assert len(logs) == 1
    assert chain.calls == [("tenderly", 0, 99)] * 3  # same range, same provider
    assert slept == [7, 9]


def test_quota_exhaustion_is_a_hard_failure_not_bisection(monkeypatch, tmp_path):
    def quota(endpoint, a, b):
        raise rpc.RpcError("429 quota exceeded")

    chain = _install(monkeypatch, Chain([], fail=quota))
    with pytest.raises(rpc.QuotaError):
        rpc.acquire_logs(_store(tmp_path), 0, 99, 100, cooldowns=(0, 0))
    assert chain.calls == [("tenderly", 0, 99)] * 3
    assert not [f for f in os.listdir(tmp_path / "s") if f.startswith("chunk-")]


def test_quota_is_not_a_range_size_error():
    assert rpc.classify_error(rpc.RpcError("HTTP Error 429: Too Many Requests")) == "quota"
    assert rpc.classify_error(rpc.RpcError("daily request quota exceeded")) == "quota"  # 'exceed' must not win
    assert rpc.classify_error(rpc.RpcError("query returned more than 10000 results")) == "bisect"
    assert rpc.classify_error(rpc.RpcError("HTTP Error 400: Bad Request")) == "bisect"
    assert rpc.classify_error(rpc.RpcError("HTTP Error 502: Bad Gateway")) == "transient"
    assert rpc.classify_error(rpc.RpcError("eth_getLogs -> response has neither result nor error")) == "transient"


def test_result_cap_bisects_and_leaves_are_checkpointed(monkeypatch, tmp_path):
    monkeypatch.setitem(rpc.ENDPOINTS["tenderly"], "result_cap", 40)
    cap = rpc.result_cap("tenderly")
    dense = [_log(b, i) for b in range(0, 8) for i in range(cap // 4)]  # blocks 0-7 hold 2*cap logs

    chain = _install(monkeypatch, Chain(dense))
    st = _store(tmp_path)
    logs = rpc.acquire_logs(st, 0, 7, 8)
    assert len(logs) == len(dense)
    leaves = sorted(st._index)
    assert leaves == [(0, 1), (2, 3), (4, 5), (6, 7)]  # whole window and halves were over the cap
    assert st.coverage_proof(0, 7)["chunks"] == 4
    # resume re-reads the leaves without asking the provider
    chain2 = _install(monkeypatch, Chain(dense))
    assert len(rpc.acquire_logs(_store(tmp_path), 0, 7, 8)) == len(dense)
    assert chain2.calls == []


def test_result_cap_at_one_block_raises(monkeypatch, tmp_path):
    monkeypatch.setitem(rpc.ENDPOINTS["tenderly"], "result_cap", 40)
    cap = rpc.result_cap("tenderly")
    _install(monkeypatch, Chain([_log(0, i) for i in range(cap)]))
    with pytest.raises(rpc.ResultCapError):
        rpc.acquire_logs(_store(tmp_path), 0, 3, 4)


def test_http400_dense_window_bisects_instead_of_falling_back(monkeypatch, tmp_path):
    def dense(endpoint, a, b):
        if b - a + 1 > 50:
            raise rpc.RpcError("HTTP Error 400: Bad Request")

    chain = _install(monkeypatch, Chain([_log(10), _log(70)], fail=dense))
    logs = rpc.acquire_logs(_store(tmp_path), 0, 99, 100)
    assert len(logs) == 2
    assert {e for e, _, _ in chain.calls} == {"tenderly"}  # never drpc


def test_query_shaped_bounds_mevblocker(monkeypatch, tmp_path):
    assert rpc.shape_cap("mevblocker", "single_address") == 50_000
    assert rpc.shape_cap("mevblocker", "multi_address") == 10_000   # unmeasured -> conservative
    assert rpc.shape_cap("mevblocker", "topic_only") == 10_000
    assert rpc.shape_cap("tenderly", "topic_only") == 2_000_000
    chain = _install(monkeypatch, Chain([]))
    rpc.acquire_logs(_store(tmp_path, endpoint="mevblocker", address=POOLS), 0, 24_999, 50_000)
    assert [(a, b) for _, a, b in chain.calls] == [(0, 9_999), (10_000, 19_999), (20_000, 24_999)]
    chain = _install(monkeypatch, Chain([]))
    rpc.acquire_logs(_store(tmp_path, endpoint="mevblocker", address=A1, name="one"), 0, 24_999, 50_000)
    assert [(a, b) for _, a, b in chain.calls] == [(0, 24_999)]
    with pytest.raises(rpc.RpcError):  # a direct oversize request is refused
        rpc.fetch_logs_scoped("mevblocker", 0, 20_000, address=POOLS, topics=None)


def test_result_caps_in_config_match_g4():
    assert {e: rpc.result_cap(e) for e in g4.RESULT_CAP} == g4.RESULT_CAP


def test_corrupt_chunk_fails_never_silently_refetched(monkeypatch, tmp_path):
    chain = _install(monkeypatch, Chain([_log(3)]))
    st = _store(tmp_path)
    rpc.acquire_logs(st, 0, 99, 100)
    path = tmp_path / "s" / "chunk-0000000000-0000000099.json"
    for damage in (lambda t: t[: len(t) // 2],                       # torn/truncated JSON
                   lambda t: t.replace('"logCount":1', '"logCount":2'),
                   lambda t: t.replace(A1, "0x" + "33" * 20)):       # payload no longer matches its hash
        good = path.read_text()
        path.write_text(damage(good))
        chain2 = _install(monkeypatch, Chain([_log(3)]))
        with pytest.raises(rpc.CheckpointError):
            rpc.acquire_logs(_store(tmp_path), 0, 99, 100)
        assert chain2.calls == []  # a corrupt chunk is an error, not a cache miss
        path.write_text(good)


def test_chunk_log_outside_bound_query_is_rejected(monkeypatch, tmp_path):
    _install(monkeypatch, Chain([_log(3, addr="0x" + "99" * 20)]))
    with pytest.raises(rpc.CheckpointError):
        # fetched fine, but reading it back (coverage) shows a log outside the bound address filter
        st = _store(tmp_path, address=A1)
        rpc.acquire_logs(st, 0, 99, 100)
        st.coverage_proof(0, 99)


def test_cross_config_checkpoints_are_rejected(monkeypatch, tmp_path):
    _install(monkeypatch, Chain([_log(3)]))
    rpc.acquire_logs(_store(tmp_path), 0, 99, 100)
    for kw in ({"commit": "c2"}, {"cfg": "cfg2"}, {"inputs": {"g0": "other"}}, {"address": A1}, {"endpoint": "drpc"}):
        with pytest.raises(rpc.CheckpointError, match="different producer/config"):
            _store(tmp_path, **kw)
    _store(tmp_path)  # the identical binding still opens


def test_chunk_bound_to_other_binding_is_rejected_even_if_dir_binding_forged(monkeypatch, tmp_path):
    _install(monkeypatch, Chain([_log(3)]))
    rpc.acquire_logs(_store(tmp_path), 0, 99, 100)
    # attacker/accident: binding.json rewritten to a new binding but the old chunk is left in place
    st_new_binding = rpc.make_binding("c2", "cfg1", {"g0": "h"}, "tenderly", None, [[T0]])
    (tmp_path / "s" / "binding.json").write_text(rpc._canon(st_new_binding))
    st = rpc.ChunkStore(str(tmp_path / "s"), st_new_binding)
    with pytest.raises(rpc.CheckpointError, match="binding"):
        st.read_chunk(0, 99)


def test_coverage_proof_detects_gap_overlap_and_duplicates(monkeypatch, tmp_path):
    _install(monkeypatch, Chain([_log(3), _log(150)]))
    st = _store(tmp_path)
    rpc.acquire_logs(st, 0, 199, 100)
    st.coverage_proof(0, 199)
    with pytest.raises(rpc.CoverageError, match="cover up to"):
        st.coverage_proof(0, 299)
    os.remove(tmp_path / "s" / "chunk-0000000000-0000000099.json")
    st2 = _store(tmp_path)
    with pytest.raises(rpc.CoverageError, match="gap"):
        st2.coverage_proof(0, 199)
    # duplicate (tx, logIndex) in two chunks
    dup = _log(3, tx="0x" + "ee" * 32)
    dup2 = {**dup, "blockNumber": hex(150)}
    _install(monkeypatch, Chain([dup, dup2]))
    st3 = _store(tmp_path, name="dup")
    rpc.acquire_logs(st3, 0, 199, 100)
    with pytest.raises(rpc.CoverageError, match="duplicate"):
        st3.coverage_proof(0, 199)


def test_misaligned_stored_chunk_fails(monkeypatch, tmp_path):
    _install(monkeypatch, Chain([]))
    rpc.acquire_logs(_store(tmp_path), 0, 99, 100)
    with pytest.raises(rpc.CheckpointError, match="misaligned"):
        rpc.acquire_logs(_store(tmp_path), 0, 199, 50)  # 50-block plan vs a stored 100-block chunk


def test_no_fallback_in_scoped_fetch(monkeypatch):
    seen = []

    def fetch(endpoint, a, b, address, topics):
        seen.append(endpoint)
        raise rpc.RpcError("HTTP Error 502: Bad Gateway")

    monkeypatch.setattr(rpc, "_fetch_one_range", fetch)
    with pytest.raises(rpc.GapError):
        rpc.fetch_logs_scoped("tenderly", 0, 99, max_retries=2)
    assert seen == ["tenderly", "tenderly"]


def test_malformed_reply_is_hard_failure(monkeypatch):
    for bad in ({"x": 1}, [{"blockNumber": "0x1"}], "nope"):
        monkeypatch.setattr(rpc, "_fetch_one_range", lambda *a, bad=bad: (bad, b"x"))
        with pytest.raises(rpc.GapError, match="malformed"):
            rpc.fetch_logs_scoped("tenderly", 0, 9)


def test_chunk_write_oserror_propagates(monkeypatch, tmp_path):
    _install(monkeypatch, Chain([_log(1)]))
    st = _store(tmp_path)

    def bad(path, obj):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(rpc, "_atomic_write_json", bad)
    with pytest.raises(OSError):
        rpc.acquire_logs(st, 0, 99, 100)


def test_guard_blocks_post_cutoff_before_any_request(monkeypatch, tmp_path):
    chain = _install(monkeypatch, Chain([]))
    with pytest.raises(g4.PostPeriodRead):
        rpc.acquire_logs(_store(tmp_path), 0, 200, 100, guard=gj._guard(150))
    assert [(a, b) for _, a, b in chain.calls] == [(0, 99)]


def test_g4_june_sweep_resumes_and_writes_receipts(monkeypatch, tmp_path):
    receipts = []
    monkeypatch.setattr(g4, "_receipt", lambda rec: receipts.append(rec))
    events = [_log(b) for b in (10, 120, 230)]
    ctx = ("commit", "cfg", {"g0": "h"})

    def boom(endpoint, a, b):
        if a >= 200:
            raise rpc.GapError("down")

    _install(monkeypatch, Chain(events, fail=boom))
    with pytest.raises(rpc.GapError):
        gj._sweep(str(tmp_path), "sweep_a", "tenderly", 0, 299, None, [[T0]], 100, ctx, 10_000, "g")
    chain = _install(monkeypatch, Chain(events))
    logs, proof = gj._sweep(str(tmp_path), "sweep_a", "tenderly", 0, 299, None, [[T0]], 100, ctx, 10_000, "g")
    assert [(a, b) for _, a, b in chain.calls] == [(200, 299)]
    assert len(logs) == 3 and proof["chunks"] == 3 and len(proof["attempt_ids"]) == 2
    assert [(r["fromBlock"], r["toBlock"]) for r in receipts] == [(0, 99), (100, 199), (200, 299)]
    # different commit => the same directory refuses to be reused
    with pytest.raises(rpc.CheckpointError):
        gj._sweep(str(tmp_path), "sweep_a", "tenderly", 0, 299, None, [[T0]], 100, ("other", "cfg", {"g0": "h"}), 10_000, "g")


def test_get_logs_fallback_false_stays_on_primary(monkeypatch):
    seen = []

    def fetch(endpoint, a, b, address, topics):
        seen.append(endpoint)
        raise rpc.RpcError("boom")

    monkeypatch.setattr(rpc, "_fetch_one_range", fetch)
    with pytest.raises(rpc.GapError):
        rpc.get_logs("tenderly", 0, 99, gate=None, max_retries=1, fallback=False)
    assert seen == ["tenderly"]
