"""Unit tests for gates/g4_controls.py pure helpers. No network."""
import json
import os

import pytest

from gates import g4_controls as g4

PRE_END = 15_298_282


def test_guard_block_refuses_post_period():
    assert g4._guard_block(PRE_END, PRE_END) == PRE_END
    with pytest.raises(g4.PostPeriodRead):
        g4._guard_block(PRE_END + 1, PRE_END)


def test_receipt_assert_catches_late_block(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_text(json.dumps({"fromBlock": 1, "toBlock": PRE_END}) + "\n" + json.dumps({"block": PRE_END - 5}) + "\n")
    assert g4._assert_receipts_within(str(p), PRE_END) == 2
    p.write_text(json.dumps({"fromBlock": 1, "toBlock": PRE_END + 1}) + "\n")
    with pytest.raises(g4.PostPeriodRead):
        g4._assert_receipts_within(str(p), PRE_END)
    p.write_text(json.dumps({"kind": "no-block-field"}) + "\n")
    with pytest.raises(g4.PostPeriodRead):
        g4._assert_receipts_within(str(p), PRE_END)


def test_point_batch_refuses_before_sending(monkeypatch):
    monkeypatch.setattr(g4, "_batch_post", lambda *a, **k: pytest.fail("request was sent"))
    with pytest.raises(g4.PostPeriodRead):
        g4._point_batch("x", "eth_getBalance", [(["0x0", "0x1"], PRE_END + 1)], PRE_END)


def test_assert_tiles():
    g4._assert_tiles([(0, 9), (10, 19)], 0, 19)
    with pytest.raises(AssertionError, match="gap"):
        g4._assert_tiles([(0, 9), (11, 19)], 0, 19)
    with pytest.raises(AssertionError, match="overlap"):
        g4._assert_tiles([(0, 10), (10, 19)], 0, 19)
    with pytest.raises(AssertionError):
        g4._assert_tiles([(0, 9)], 0, 19)


def test_reconstruct_series_and_negative_guard():
    s = g4.reconstruct_series(10, {0: 5, 2: -15, 3: 1}, n_days=4)
    assert s == [15, 15, 0, 1]
    with pytest.raises(AssertionError, match="negative"):
        g4.reconstruct_series(10, {1: -11}, n_days=3)


def test_reconstruct_exact_above_2_53():
    big = 55_000_000_000_000_001
    assert g4.reconstruct_series(big, {1: 1}, n_days=2) == [big, big + 1]


def test_floor_stats_boundaries():
    m = g4.FLOOR_MIN_UNITS
    # 14 nonzero days at exactly mean 100 USD over 60 days -> passes
    s = [m * 60 // 14 + 1] * 14 + [0] * 46
    mean, nz, ok = g4.floor_stats(s, [0] * 60)
    assert nz == 14 and mean >= m and ok
    # 13 nonzero days fails the day floor even with a high mean
    mean, nz, ok = g4.floor_stats([10 * m] * 13 + [0] * 47, [0] * 60)
    assert nz == 13 and not ok
    # mean below $100 fails
    mean, nz, ok = g4.floor_stats([m - 1] * 60, [0] * 60)
    assert mean < m and not ok


def test_usdc_share_ppm_integer():
    assert g4.usdc_share_ppm(1, 3) == 250_000
    assert g4.usdc_share_ppm(0, 5) == 0
    assert g4.usdc_share_ppm(0, 0) is None
    assert isinstance(g4.usdc_share_ppm(7, 3), int)


def _fake_nonce(first_send):
    """first_send: {addr: block or None}; nonce(b) = 1 if b >= first_send else 0."""
    calls = []

    def fn(pairs):
        calls.append(len(pairs))
        return [1 if first_send[a] is not None and b >= first_send[a] else 0 for a, b in pairs]
    fn.calls = calls
    return fn


def test_bisect_first_send_exact():
    truth = {"a": 0, "b": 1, "c": 12345, "d": PRE_END, "e": None}
    fn = _fake_nonce(truth)
    assert g4.bisect_first_send(list(truth), fn, PRE_END) == truth
    # batched: one call per round covering every open address, ~log2(range) rounds
    assert len(fn.calls) <= 26
    assert fn.calls[0] == 5 and all(c <= 4 for c in fn.calls[1:])


def test_bisect_never_queries_past_hi():
    seen = []

    def fn(pairs):
        seen.extend(b for _, b in pairs)
        return [1 for _ in pairs]

    g4.bisect_first_send(["a"], fn, 1000)
    assert max(seen) <= 1000


def test_blacklist_state_ignores_post_period_events():
    ev = [(100, 0, "add"), (200, 0, "remove"), (PRE_END + 5, 0, "add")]
    assert g4.blacklist_state_at(ev, PRE_END) is False
    assert g4.blacklist_state_at(ev[:1], PRE_END) is True
    assert g4.blacklist_state_at([(PRE_END + 1, 0, "add")], PRE_END) is False
    assert g4.blacklist_state_at([], PRE_END) is False


_LI = [0]


def _log(frm, to, value, block, ts, tx="0x1", token=None, li=None):
    t = lambda a: "0x" + a[2:].rjust(64, "0")
    _LI[0] += 1
    return {"blockNumber": hex(block), "blockTimestamp": hex(ts), "transactionHash": tx,
            "address": token or g4.USDC, "blockHash": hex(block), "logIndex": hex(_LI[0] if li is None else li),
            "topics": [g4.TRANSFER_TOPIC, t(frm), t(to)], "data": "0x" + f"{value:064x}"}


A = "0x" + "a" * 40
B = "0x" + "b" * 40
Z = g4.ZERO_ADDRESS
D0 = 1654732800  # 2022-06-09 00:00:00 UTC


def test_decode_unit_logs_from_and_to_legs():
    logs = [_log(A, B, 7, 14_929_600, D0 + 10), _log(Z, A, 100, 14_929_601, D0 + 86400 + 5)]
    out_from = g4.decode_unit_logs(logs[:1], "from", {A}, 14_929_560, PRE_END, g4.USDC)
    assert out_from == {A: [{0: -7}, 1, 0]}
    out_to = g4.decode_unit_logs(logs, "to", {A, B}, 14_929_560, PRE_END, g4.USDC)
    assert out_to[B] == [{0: 7}, 1, 0]
    assert out_to[A] == [{1: 100}, 1, 1]          # mint from zero address: counted + flagged


def test_decode_unit_logs_rejects_untracked_and_out_of_window():
    with pytest.raises(AssertionError, match="untracked"):
        g4.decode_unit_logs([_log(A, B, 1, 14_929_600, D0)], "from", {B}, 14_929_560, PRE_END, g4.USDC)
    with pytest.raises(g4.PostPeriodRead):
        g4.decode_unit_logs([_log(A, B, 1, PRE_END + 1, D0)], "from", {A}, 14_929_560, PRE_END, g4.USDC)
    with pytest.raises(AssertionError, match="outside pre-period"):
        g4.decode_unit_logs([_log(A, B, 1, 14_929_600, D0 - 86400)], "from", {A}, 14_929_560, PRE_END, g4.USDC)


def test_point_batch_writes_guarded_receipt(monkeypatch, tmp_path):
    monkeypatch.setattr(g4, "_RECEIPTS_PATH", str(tmp_path / "r.jsonl"))
    monkeypatch.setattr(g4, "_batch_post", lambda ep, calls, **k: ["0x1"] * len(calls))
    out = g4._point_batch("eth_getBalance", "eth_getBalance", [([A, hex(5)], 5), ([B, hex(9)], 9)], PRE_END)
    assert out == ["0x1", "0x1"]
    rec = json.loads((tmp_path / "r.jsonl").read_text())
    assert (rec["fromBlock"], rec["toBlock"], rec["addressCount"]) == (5, 9, 2)
    assert g4._assert_receipts_within(str(tmp_path / "r.jsonl"), PRE_END) == 1


def test_batch_post_429_retries_same_batch_not_bisect(monkeypatch):
    import urllib.error
    sizes = []

    class R:
        def __init__(self, b):
            self.b = b

        def read(self):
            return self.b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout=None):
        items = json.loads(req.data)
        sizes.append(len(items))
        if len(sizes) <= 2:
            raise urllib.error.HTTPError(req.full_url, 429, "slow down", {}, None)
        return R(json.dumps([{"id": it["id"], "result": hex(it["id"])} for it in reversed(items)]).encode())

    monkeypatch.setattr(g4.urllib.request, "urlopen", fake)
    monkeypatch.setattr(g4.time, "sleep", lambda *_: None)
    out = g4._batch_post("mevblocker", [("eth_getBalance", [A, "0x1"])] * 4)
    assert out == ["0x0", "0x1", "0x2", "0x3"] and sizes == [4, 4, 4]


def test_batch_post_gives_up_loudly(monkeypatch):
    import urllib.error

    def fake(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "x", {}, None)

    monkeypatch.setattr(g4.urllib.request, "urlopen", fake)
    monkeypatch.setattr(g4.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError, match="failed after"):
        g4._batch_post("mevblocker", [("eth_getBalance", [A, "0x1"])], max_retries=2)


def test_fetch_adaptive_bisects_on_result_cap_and_tiles(monkeypatch):
    from lib import rpc
    calls = []

    def fake(endpoint, a, b, address, topics):
        calls.append((a, b))
        if b - a + 1 > 100:
            raise rpc.RpcError("tenderly eth_getLogs -> {'message': 'Query returned more than 20000 results.'}")
        return [{"blockHash": "h", "transactionHash": f"t{a}", "logIndex": "0x0", "address": "x",
                 "topics": [], "data": "0x"}], b"[]"

    monkeypatch.setattr(rpc, "_fetch_one_range", fake)
    monkeypatch.setattr(g4.time, "sleep", lambda *_: pytest.fail("cap hit must bisect immediately, not back off"))
    logs, ranges = g4._fetch_adaptive("tenderly", g4.USDC, [], 0, 399, PRE_END)
    g4._assert_tiles([(s, e) for s, e, *_ in ranges], 0, 399)
    assert len(logs) == len(ranges) == 4 and all(e - s + 1 <= 100 for s, e, *_ in ranges)


def test_fetch_adaptive_retries_transient_then_raises(monkeypatch):
    from lib import rpc
    n = []

    def fake(endpoint, a, b, address, topics):
        n.append(1)
        raise rpc.RpcError("mevblocker eth_getLogs -> boom")

    monkeypatch.setattr(rpc, "_fetch_one_range", fake)
    monkeypatch.setattr(g4.time, "sleep", lambda *_: None)
    with pytest.raises(rpc.GapError):
        g4._fetch_endpoint("tenderly", g4.USDC, [], 0, 9, PRE_END, max_retries=3)
    assert len(n) == 3


def test_fetch_adaptive_refuses_post_period(monkeypatch):
    from lib import rpc
    monkeypatch.setattr(rpc, "_fetch_one_range", lambda *a: pytest.fail("sent"))
    with pytest.raises(g4.PostPeriodRead):
        g4._fetch_adaptive("tenderly", g4.USDC, [], PRE_END - 5, PRE_END + 1, PRE_END)


def test_fetch_windowed_tiles_in_10k_windows(monkeypatch):
    from lib import rpc
    seen = []

    def fake(endpoint, a, b, address, topics):
        seen.append((a, b))
        return [{"blockHash": "h", "transactionHash": f"t{a}", "logIndex": "0x0", "address": "x",
                 "topics": [], "data": "0x"}], b"[]"

    monkeypatch.setattr(rpc, "_fetch_one_range", fake)
    logs, ranges = g4._fetch_windowed("mevblocker", g4.USDC, [], 14_929_561, PRE_END, PRE_END, 3)
    g4._assert_tiles([(s, e) for s, e, *_ in ranges], 14_929_561, PRE_END)
    assert all(e - s + 1 <= 10_000 for s, e in seen) and len(logs) == len(ranges) == 37


# --- hardening tests ------------------------------------------------------

import urllib.error  # noqa: E402


class _Resp:
    def __init__(self, b):
        self.b = b

    def read(self):
        return self.b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_replies(monkeypatch, replies):
    """replies: list of python objects returned in turn (last repeats)."""
    n = []

    def fake(req, timeout=None):
        i = min(len(n), len(replies) - 1)
        n.append(1)
        return _Resp(json.dumps(replies[i]).encode())

    monkeypatch.setattr(g4.urllib.request, "urlopen", fake)
    monkeypatch.setattr(g4.time, "sleep", lambda *_: None)
    return n


WORD = "0x" + "0" * 63 + "5"


@pytest.mark.parametrize("bad", [None, "0x", "", "0x0", "0x" + "0" * 62, WORD + "00"])
def test_balanceof_rejects_malformed_then_retries(monkeypatch, bad):
    n = _fake_replies(monkeypatch, [[{"id": 0, "result": bad}], [{"id": 0, "result": WORD}]])
    assert g4._batch_post("mevblocker", [("eth_call", [])], validate=g4._VALIDATORS["eth_call"]) == [WORD]
    assert len(n) == 2


@pytest.mark.parametrize("bad", [None, "0x", "", "5", 7])
def test_quantity_rejects_malformed_then_retries(monkeypatch, bad):
    n = _fake_replies(monkeypatch, [[{"id": 0, "result": bad}], [{"id": 0, "result": "0x0"}]])
    assert g4._batch_post("mevblocker", [("eth_getBalance", [])], validate=g4._VALIDATORS["eth_getBalance"]) == ["0x0"]
    assert len(n) == 2


def test_batch_missing_id_raises_and_retries(monkeypatch):
    n = _fake_replies(monkeypatch, [[{"id": 0, "result": "0x1"}], [{"id": 0, "result": "0x1"}, {"id": 1, "result": "0x2"}]])
    assert g4._batch_post("mevblocker", [("eth_getBalance", [])] * 2) == ["0x1", "0x2"]
    assert len(n) == 2
    _fake_replies(monkeypatch, [[{"id": 0, "result": "0x1"}]])
    with pytest.raises(RuntimeError, match="failed after"):
        g4._batch_post("mevblocker", [("eth_getBalance", [])] * 2, max_retries=2)


def test_point_batch_always_validates(monkeypatch):
    monkeypatch.setattr(g4, "_RECEIPTS_PATH", "/nonexistent/x")
    _fake_replies(monkeypatch, [[{"id": 0, "result": None}]])
    with pytest.raises(RuntimeError, match="failed after"):
        g4._point_batch("x", "eth_getBalance", [([A, "0x1"], 1)], PRE_END)
    with pytest.raises(ValueError):
        g4._point_batch("x", "eth_getCode", [([A, "0x1"], 1)], PRE_END)


def test_decode_rejects_wrong_token_topic_arity_duplicates():
    ok = _log(A, B, 1, 14_929_600, D0)
    with pytest.raises(AssertionError, match="expected token"):
        g4.decode_unit_logs([_log(A, B, 1, 14_929_600, D0, token=g4.USDT)], "from", {A}, 14_929_560, PRE_END, g4.USDC)
    bad = dict(ok, topics=[ok["topics"][0], ok["topics"][1]])
    with pytest.raises(AssertionError, match="3-topic"):
        g4.decode_unit_logs([bad], "from", {A}, 14_929_560, PRE_END, g4.USDC)
    bad = dict(ok, topics=["0x" + "11" * 32] + ok["topics"][1:])
    with pytest.raises(AssertionError, match="3-topic"):
        g4.decode_unit_logs([bad], "from", {A}, 14_929_560, PRE_END, g4.USDC)
    with pytest.raises(AssertionError, match="duplicate"):
        g4.decode_unit_logs([ok, dict(ok)], "from", {A}, 14_929_560, PRE_END, g4.USDC)
    # same block, different logIndex: fine
    g4.decode_unit_logs([ok, _log(A, B, 2, 14_929_600, D0)], "from", {A}, 14_929_560, PRE_END, g4.USDC)


def test_block_timestamp_consistency():
    a = _log(A, B, 1, 14_929_600, D0 + 10)
    b = _log(A, B, 1, 14_929_601, D0 + 5)       # later block, earlier timestamp
    with pytest.raises(AssertionError, match="monotone"):
        g4.check_block_timestamps([a, b])
    c = _log(A, B, 1, 14_929_600, D0 + 11)      # same block, different timestamp
    with pytest.raises(AssertionError, match="two different"):
        g4.check_block_timestamps([a, c])
    g4.check_block_timestamps([a, _log(A, B, 1, 14_929_601, D0 + 22)])


def test_digest_with_block_covers_number_and_timestamp():
    a = _log(A, B, 1, 14_929_600, D0)
    b = dict(a, blockTimestamp=hex(D0 + 86400))
    assert g4._digest([a]) == g4._digest([b])                      # legacy digest blind to it
    d1, d2 = g4._digest([a], with_block=True), g4._digest([b], with_block=True)
    assert d1 != d2 and d1.startswith("v2:")
    assert g4._digest_like(d1, [a]) == d1 and g4._digest_like(g4._digest([a]), [a]) == g4._digest([a])


def test_fetch_adaptive_bisects_when_result_hits_cap(monkeypatch):
    from lib import rpc
    cap = g4.RESULT_CAP["mevblocker"]

    def fake(endpoint, a, b, address, topics):
        n = cap if b - a + 1 > 100 else 1       # silently truncated at the cap, no error
        return [{"blockHash": "h", "transactionHash": f"t{a}", "logIndex": hex(i), "address": "x",
                 "topics": [], "data": "0x"} for i in range(n)], b"[]"

    monkeypatch.setattr(rpc, "_fetch_one_range", fake)
    logs, ranges = g4._fetch_adaptive("mevblocker", g4.USDC, [], 0, 399, PRE_END)
    g4._assert_tiles([(s, e) for s, e, *_ in ranges], 0, 399)
    assert len(ranges) == 4 and all(e - s + 1 <= 100 for s, e, *_ in ranges)


def test_default_walk_is_mevblocker_replay_tenderly_and_walk_dispatches_windowed(monkeypatch):
    """Routed main (no G4_* env admitted) resolves WALK=mevblocker, replay=tenderly, batch=mevblocker, unit identity unchanged."""
    assert "G4_WALK_ENDPOINT" not in __import__("os").environ
    assert g4.WALK == "mevblocker" and g4.REPLAY_ENDPOINT[g4.WALK] == "tenderly" and g4.BATCH == "mevblocker"
    assert g4.WALK_ARRAY == 1000
    seen = {}
    monkeypatch.setattr(g4, "_fetch_windowed", lambda ep, tok, tp, s, e, pe, w: seen.update(ep=ep, w=w) or ([], []))
    monkeypatch.setattr(g4, "_fetch_adaptive", lambda *a: pytest.fail("tenderly whole-range path used"))
    g4._walk_fetch(g4.USDC, [], 0, 9, PRE_END)
    assert seen == {"ep": "mevblocker", "w": g4.REPLAY_WORKERS["mevblocker"]}


def test_walk_endpoint_validated_at_import():
    import importlib
    import os
    os.environ["G4_WALK_ENDPOINT"] = "bogus"
    try:
        with pytest.raises(RuntimeError, match="G4_WALK_ENDPOINT"):
            importlib.reload(g4)
    finally:
        del os.environ["G4_WALK_ENDPOINT"]
        importlib.reload(g4)


def test_ckpt_torn_final_line_truncated_other_bad_line_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(g4, "_OUT_DIR", str(tmp_path))
    p = tmp_path / "c.jsonl"
    p.write_bytes(b'{"k": "a", "v": 1}\n{"k": "b", "v": 2}\n{"k": "c", "v"')
    c = g4._Ckpt("c.jsonl")
    assert c.data == {"a": 1, "b": 2}
    c.put("d", 4)
    c.close()
    assert g4._Ckpt("c.jsonl").data == {"a": 1, "b": 2, "d": 4}
    p.write_bytes(b'{"k": "a", "v": 1}\nGARBAGE\n{"k": "b", "v": 2}\n')
    with pytest.raises(RuntimeError, match="corrupt line 2"):
        g4._Ckpt("c.jsonl")


def test_ckpt_valid_unterminated_final_line_gets_newline(monkeypatch, tmp_path):
    monkeypatch.setattr(g4, "_OUT_DIR", str(tmp_path))
    (tmp_path / "c.jsonl").write_bytes(b'{"k": "a", "v": 1}')
    c = g4._Ckpt("c.jsonl")
    c.put("b", 2)
    c.close()
    assert g4._Ckpt("c.jsonl").data == {"a": 1, "b": 2}


def test_pick_reverify_units_stratified_by_endpoint():
    data = {}
    for i in range(100):
        data[f"u{i}"] = {"ranges": [[0, 9, 1, "d", "tenderly" if i < 80 else "mevblocker"]]}
    units, per = g4.pick_reverify_units(data)
    assert per == {"mevblocker": 3, "tenderly": 4} and len(units) == 7
    assert g4.pick_reverify_units({}) == ([], {})


def test_pick_audit_set():
    arm = {f"a{i}": ("control" if i < 90 else "both") for i in range(100)}
    allp = sorted(arm)
    assert g4.pick_audit_set(allp, arm, 200) == allp
    s = g4.pick_audit_set(allp, arm, 50, min_sample=20)
    assert all(a in s for a in allp if arm[a] != "control") and len(s) == 50
    assert s == g4.pick_audit_set(allp, arm, 50, min_sample=20)


# --- security re-review fixes -------------------------------------------

def test_batch_split_children_keep_strict_validation(monkeypatch):
    """HTTP 413 splits the batch; a null result in a child must still raise, never reach g1._hex_to_int."""
    sizes = []

    def fake(req, timeout=None):
        items = json.loads(req.data)
        sizes.append(len(items))
        if len(items) > 2:
            raise urllib.error.HTTPError(req.full_url, 413, "too big", {}, None)
        return _Resp(json.dumps([{"id": it["id"], "result": None if it["id"] == 1 else "0x5"} for it in items]).encode())

    monkeypatch.setattr(g4.urllib.request, "urlopen", fake)
    monkeypatch.setattr(g4.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError, match="failed after"):
        g4._batch_post("mevblocker", [("eth_getBalance", [A, "0x1"])] * 4, max_retries=2,
                       validate=g4._VALIDATORS["eth_getBalance"])
    assert 2 in sizes  # the split really happened
    # happy path through the same split
    monkeypatch.setattr(g4.urllib.request, "urlopen",
                        lambda req, timeout=None: (_ for _ in ()).throw(urllib.error.HTTPError(req.full_url, 413, "x", {}, None))
                        if len(json.loads(req.data)) > 2 else
                        _Resp(json.dumps([{"id": it["id"], "result": "0x5"} for it in json.loads(req.data)]).encode()))
    assert g4._batch_post("mevblocker", [("eth_getBalance", [A, "0x1"])] * 4,
                          validate=g4._VALIDATORS["eth_getBalance"]) == ["0x5"] * 4


@pytest.mark.parametrize("mut", [
    lambda l: l.update(data="0x"),
    lambda l: l.update(data="0x" + "0" * 62),
    lambda l: l.update(data="0x" + "0" * 66),
    lambda l: l.update(data=None),
    lambda l: l["topics"].__setitem__(1, "0x" + "1" * 24 + "a" * 40),     # nonzero padding
    lambda l: l["topics"].__setitem__(2, "0x" + "0" * 24 + "a" * 38),     # short word
    lambda l: l["topics"].__setitem__(1, "0x" + "0" * 26 + "a" * 40),     # long word
])
def test_decode_rejects_malformed_transfer_shape(mut):
    l = _log(A, B, 5, 14_929_600, D0)
    mut(l)
    with pytest.raises(AssertionError):
        g4.decode_unit_logs([l], "from", {A}, 14_929_560, PRE_END, g4.USDC)


class _FakeCk:
    def __init__(self, data=None):
        self.data, self.puts = dict(data or {}), []

    def put(self, k, v):
        self.data[k] = v
        self.puts.append(k)


def _rv_setup(monkeypatch, rv_data, alt_logs=None):
    dig = "v2:" + "ab" * 32
    walk = _FakeCk({"USDC:from:0": {"ranges": [[10, 19, 0, dig, "mevblocker"]]}})
    monkeypatch.setattr(g4, "_RECEIPTS_PATH", "/nonexistent/x")
    calls = []
    monkeypatch.setattr(g4, "_replay", lambda *a: calls.append(a) or (alt_logs or []))
    monkeypatch.setattr(g4, "_receipt", lambda r: None)
    monkeypatch.setattr(g4, "_digest_like", lambda stored, logs: stored)
    return walk, _FakeCk(rv_data), calls, dig


def test_reverify_legacy_true_entry_is_recompared(monkeypatch):
    walk, rv, calls, dig = _rv_setup(monkeypatch, {"USDC:from:0:10:19": True})
    cached, fresh, _ = g4._reverify(walk, [A], PRE_END, rv)
    assert (cached, fresh) == (0, 1) and len(calls) == 1
    assert rv.data["USDC:from:0:10:19"] == g4._rv_record(0, dig, "mevblocker", "tenderly")


def test_reverify_bound_entry_is_cached_and_mismatching_is_not(monkeypatch):
    good = lambda dig: g4._rv_record(0, dig, "mevblocker", "tenderly")
    dig = "v2:" + "ab" * 32
    walk, rv, calls, _ = _rv_setup(monkeypatch, {"USDC:from:0:10:19": good(dig)})
    assert g4._reverify(walk, [A], PRE_END, rv)[:2] == (1, 0) and not calls
    for bad in (good("v2:" + "cd" * 32),                                   # other source digest
                {**good(dig), "count": 3},                                 # other log count
                {**good(dig), "verifier": "drpc"},                         # other verifier endpoint
                {**good(dig), "source": "tenderly"},                       # other source endpoint
                {**good(dig), "v": g4.REVERIFY_VERSION - 1}):              # older validation version
        walk, rv, calls, _ = _rv_setup(monkeypatch, {"USDC:from:0:10:19": bad})
        assert g4._reverify(walk, [A], PRE_END, rv)[:2] == (0, 1) and len(calls) == 1


def test_reverify_same_source_and_verifier_raises(monkeypatch):
    walk, rv, calls, _ = _rv_setup(monkeypatch, {})
    monkeypatch.setitem(g4.REPLAY_ENDPOINT, "mevblocker", "mevblocker")
    with pytest.raises(AssertionError, match="source and verifier"):
        g4._reverify(walk, [A], PRE_END, rv)
    assert not calls


def test_reverify_empty_sample_raises(monkeypatch):
    walk, rv, calls, _ = _rv_setup(monkeypatch, {})
    with pytest.raises(AssertionError, match="empty"):
        g4._reverify(_FakeCk({}), [A], PRE_END, rv)


def test_reverify_mismatch_raises(monkeypatch):
    walk, rv, calls, _ = _rv_setup(monkeypatch, {}, alt_logs=[{"x": 1}])
    monkeypatch.setattr(g4, "check_block_timestamps", lambda logs: None)
    with pytest.raises(AssertionError, match="mismatch"):
        g4._reverify(walk, [A], PRE_END, rv)


def test_legacy_walk_unit_detected_rewalked_and_refused_in_merge():
    legacy = {"ranges": [[1, 9, 2, "ab" * 32, "tenderly"]], "logs": 2, "addrs": {}}
    v2 = {"ranges": [[1, 9, 2, "v2:" + "ab" * 32, "mevblocker"]], "logs": 2, "addrs": {}}
    assert not g4._unit_is_v2(legacy) and g4._unit_is_v2(v2)
    assert not g4._unit_is_v2(None) and not g4._unit_is_v2({"ranges": []})
    with pytest.raises(RuntimeError, match="legacy"):
        g4._merge_walk(_FakeCk({"USDC:from:0": legacy}), {A})
    g4._merge_walk(_FakeCk({"USDC:from:0": v2}), {A})


def test_run_walk_refetches_legacy_unit(monkeypatch):
    legacy = {"ranges": [[1, 9, 0, "ab" * 32, "tenderly"]], "logs": 0, "addrs": {}}
    ck = _FakeCk({f"{c}:{p}:0": legacy for c in g4.COINS for p in ("from", "to")})
    fetched = []

    def fake_walk(token, topics, start, end, pre_end):
        fetched.append(token)
        return [], [(start, end, 0, "v2:" + "00" * 32, "mevblocker")]

    monkeypatch.setattr(g4, "_walk_fetch", fake_walk)
    monkeypatch.setattr(g4, "_receipt", lambda r: None)
    monkeypatch.setattr(g4.time, "sleep", lambda *_: None)
    g4._run_walk([A], 4, 20, ck)
    assert len(fetched) == 4 and all(g4._unit_is_v2(ck.data[k]) for k in ck.data)
    fetched.clear()
    g4._run_walk([A], 4, 20, ck)
    assert fetched == []   # v2 units are reused


def test_promote_generation_is_atomic_single_pointer(tmp_path):
    out = str(tmp_path)
    (tmp_path / "covariates.csv").write_text("legacy-real-file")   # pre-generation layout
    for gname, tag in (("gen-1", "A"), ("gen-2", "B")):
        d = tmp_path / gname
        d.mkdir()
        for fn in g4._CANONICAL:
            (d / fn).write_text(f"{tag}:{fn}")
        g4.promote_generation(out, gname)
        for fn in g4._CANONICAL:   # canonical read paths always resolve into ONE generation
            assert (tmp_path / fn).read_text() == f"{tag}:{fn}"
        assert os.readlink(tmp_path / "current") == gname
    g4.prune_generations(out)
    assert (tmp_path / "gen-1").exists() and (tmp_path / "gen-2").exists()


def test_promote_refuses_incomplete_generation(tmp_path):
    (tmp_path / "gen-1").mkdir()
    (tmp_path / "gen-1" / "covariates.csv").write_text("x")
    with pytest.raises(RuntimeError, match="missing"):
        g4.promote_generation(str(tmp_path), "gen-1")
    assert not os.path.lexists(tmp_path / "current")
