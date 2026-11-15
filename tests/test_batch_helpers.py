"""Unit tests for the batched point-query helpers (copied across g1/g1b/g1c)
and g1c's _classify. urlopen is mocked; no network."""
import json
import urllib.error

import pytest

from gates import g1_cohort, g1b_frame, g1c_funnel

POST_MODULES = [g1_cohort, g1b_frame, g1c_funnel]


class _Resp:
    def __init__(self, obj):
        self._b = json.dumps(obj).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _echo_server(monkeypatch, mod, transform=lambda m, p: "0x" + "00" * 31 + "01", shuffle=False,
                 fail_over=None, record=None):
    """Fake urlopen: answers each batched call with transform(method, params).
    fail_over: raise HTTP 413 when the batch has more items than this."""
    def fake(req, timeout=None):
        items = json.loads(req.data)
        if record is not None:
            record.append(len(items))
        if fail_over is not None and len(items) > fail_over:
            raise urllib.error.HTTPError(req.full_url, 413, "too big", {}, None)
        out = [{"jsonrpc": "2.0", "id": it["id"], "result": transform(it["method"], it["params"])} for it in items]
        if shuffle:
            out.reverse()
        return _Resp(out)

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)


@pytest.mark.parametrize("mod", POST_MODULES)
def test_batch_post_matches_by_id_not_order(monkeypatch, mod):
    _echo_server(monkeypatch, mod, transform=lambda m, p: p[0], shuffle=True)
    calls = [("eth_getCode", [f"0x{i:040x}", "0x1"]) for i in range(5)]
    assert mod._batch_post("mevblocker", calls) == [c[1][0] for c in calls]


@pytest.mark.parametrize("mod", POST_MODULES)
def test_batch_post_empty(mod):
    assert mod._batch_post("mevblocker", []) == []


@pytest.mark.parametrize("mod", POST_MODULES)
def test_batch_post_bisects_on_413(monkeypatch, mod):
    sizes = []
    _echo_server(monkeypatch, mod, transform=lambda m, p: p[0], fail_over=3, record=sizes)
    calls = [("eth_getCode", [f"0x{i:040x}", "0x1"]) for i in range(8)]
    assert mod._batch_post("mevblocker", calls) == [c[1][0] for c in calls]
    assert max(s for s in sizes if s <= 3) <= 3 and sizes[0] == 8


@pytest.mark.parametrize("mod", POST_MODULES)
def test_batch_post_item_error_raises_after_retries(monkeypatch, mod):
    def fake(req, timeout=None):
        items = json.loads(req.data)
        return _Resp([{"jsonrpc": "2.0", "id": it["id"], "error": {"code": -32000, "message": "x"}} for it in items])

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError):
        mod._batch_post("mevblocker", [("eth_getCode", ["0x" + "0" * 40, "0x1"])], max_retries=2)


@pytest.mark.parametrize("mod", POST_MODULES)
def test_batch_post_network_error_raises_never_skips(monkeypatch, mod):
    def fake(req, timeout=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError):
        mod._batch_post("mevblocker", [("eth_getCode", ["0x" + "0" * 40, "0x1"])], max_retries=2)


@pytest.mark.parametrize("mod", [g1_cohort, g1b_frame])
def test_batch_get_code_chunks_and_maps(monkeypatch, mod):
    sizes = []
    _echo_server(monkeypatch, mod, transform=lambda m, p: "0x60" if p[0].endswith("1") else "0x", record=sizes)
    addrs = [f"0x{i:040x}" for i in range(7)]
    out = mod._batch_get_code("mevblocker", addrs, 15_298_282, batch_size=3)
    assert sizes == [3, 3, 1]
    assert set(out) == set(addrs)
    assert out[addrs[1]] == "0x60" and out[addrs[2]] == "0x"


def test_batch_balance_of_decodes_exact_integers(monkeypatch):
    big = 55_000_000_000_000_001  # > 2**53: a float round-trip would corrupt it
    seen = {}

    def tr(m, p):
        seen["block"] = p[1]
        a = p[0]["data"][-40:]
        if p[0]["data"][:10] != "0x70a08231":
            raise AssertionError("not a balanceOf selector")
        return hex(big) if a.endswith("1") else ("0x" if a.endswith("2") else "0x0")

    _echo_server(monkeypatch, g1_cohort, transform=tr)
    addrs = ["0x" + "0" * 39 + str(i) for i in range(3)]
    out = g1_cohort._batch_balance_of("mevblocker", g1_cohort.USDC, addrs, 15_298_282, batch_size=2)
    assert out == {addrs[0]: 0, addrs[1]: big, addrs[2]: 0}
    assert all(type(v) is int for v in out.values())
    assert seen["block"] == hex(15_298_282)


def test_batch_balance_of_receipted_writes_one_receipt_per_chunk(monkeypatch, tmp_path):
    _echo_server(monkeypatch, g1c_funnel, transform=lambda m, p: "0x5")
    rp = tmp_path / "receipts.jsonl"
    monkeypatch.setattr(g1c_funnel, "_RECEIPTS_PATH", str(rp))
    addrs = [f"0x{i:040x}" for i in range(5)]
    out = g1c_funnel._batch_balance_of_receipted("mevblocker", "USDC", g1c_funnel.USDC, addrs, 100, batch_size=2)
    assert set(out.values()) == {5}
    recs = [json.loads(l) for l in rp.read_text().splitlines()]
    assert [r["addressCount"] for r in recs] == [2, 2, 1]
    assert all(r["block"] == 100 and r["coin"] == "USDC" for r in recs)


@pytest.mark.parametrize("usdc,usdt,want", [
    ((True, False), (True, False), "both"),
    ((True, False), (False, True), "USDC-only"),   # never simultaneous: USDC-first tie-break
    ((False, False), (False, True), "USDT-only"),
    ((False, False), (False, False), "none"),
    ((True,), (True,), "both"),
    ((), (), "none"),
])
def test_classify(usdc, usdt, want):
    assert g1c_funnel._classify(usdc, usdt) == want
