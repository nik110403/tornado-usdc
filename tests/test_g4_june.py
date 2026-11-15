"""Unit tests for gates/g4_june.py. No network."""
import csv
import json
import os
import random
import re

import pytest

from gates import g4_controls as g4
from gates import g4_june as gj
from lib import blocks, rpc

PRE_START = 14_929_560
PRE_END = 15_298_282
TARGET = gj._epoch(gj.JUNE_END_TS)


def _fake_chain(monkeypatch, first_ts, seed=1, n=PRE_END + 10):
    """Block n has timestamp first_ts + cumulative irregular gaps; returns the reads log."""
    rng = random.Random(seed)
    cache = {}

    def ts(b):
        if b not in cache:
            # lazily extend a monotone sequence
            lo = max(cache) if cache else PRE_START - 1
            if not cache:
                cache[lo] = first_ts
            t = cache[lo]
            for k in range(lo + 1, b + 1):
                t += rng.choice([11, 12, 13, 14, 15, 20])
                cache[k] = t
        return cache[b]

    ts(PRE_END + 5)
    reads = []

    def get_block(endpoint, block, full_tx=False):
        reads.append(block)
        return {"timestamp": hex(cache[block])}

    monkeypatch.setattr(rpc, "get_block_by_number", get_block)
    return cache, reads


def test_cutoff_search_finds_last_block_leq_target(monkeypatch):
    cache, reads = _fake_chain(monkeypatch, TARGET - 22 * 86400 + 60)  # pre_start ~ 9 June 00:00:xx
    pins = {}
    jb, ts, nxt = gj.resolve_june_end("fake", PRE_START, PRE_END, {}, pin_fn=lambda n, b, t: pins.update({n: (b, t)}))
    brute = max(b for b in range(PRE_START, PRE_END + 1) if cache[b] <= TARGET)
    assert jb == brute
    assert gj._epoch(ts) <= TARGET < gj._epoch(nxt)
    assert pins == {"june_end": (jb, ts)}
    assert jb < PRE_END
    assert max(reads) <= PRE_END            # the search never reads past pre_period_end
    assert len(reads) < 60                  # binary search, not a scan


def test_cutoff_pinned_is_verified_not_researched(monkeypatch):
    cache, reads = _fake_chain(monkeypatch, TARGET - 22 * 86400 + 60)
    good = max(b for b in range(PRE_START, PRE_END + 1) if cache[b] <= TARGET)
    pins = []
    out = gj.resolve_june_end("fake", PRE_START, PRE_END, {"june_end": {"block": good}}, pin_fn=lambda *a: pins.append(a))
    assert out[0] == good and not pins and len(reads) == 2
    with pytest.raises(AssertionError, match="not the last block"):
        gj.resolve_june_end("fake", PRE_START, PRE_END, {"june_end": {"block": good - 3}}, pin_fn=lambda *a: None)


def test_cutoff_must_precede_pre_period_end(monkeypatch):
    _fake_chain(monkeypatch, TARGET - 22 * 86400 + 60)
    with pytest.raises(AssertionError, match="must be < pre_period_end"):
        gj.resolve_june_end("fake", PRE_START, PRE_END, {"june_end": {"block": PRE_END}}, pin_fn=lambda *a: None)


JUNE_END = 15_050_000


def test_window_draw_deterministic_and_contained():
    w1 = gj.draw_windows(PRE_START + 1, JUNE_END)
    assert w1 == gj.draw_windows(PRE_START + 1, JUNE_END)
    assert len(w1) == 30 and len(set(w1)) == 30
    assert all(PRE_START + 1 <= s and e <= JUNE_END and e - s + 1 == 400 for s, e in w1)
    assert [s for s, _ in w1] == sorted(random.Random(20251015).sample(range(PRE_START + 1, JUNE_END - 400 + 2), 30))
    assert w1 != gj.draw_windows(PRE_START + 1, JUNE_END, seed=1)


def test_window_draw_edge_is_inclusive():
    # a tiny range forces every legal start, including the last one that ends exactly at june_end
    w = gj.draw_windows(1000, 1000 + 400 + 28, n=30)
    assert (w[-1][1] <= 1428) and len(w) == 29 + 1


def test_june_floor_boundaries():
    m = g4.FLOOR_MIN_UNITS
    n = gj.N_JUNE_DAYS
    assert n == 22
    day = (m * n + 5) // 6 + 1  # 6 nonzero days whose 22-day mean is just above $100
    ok = [day] * 6 + [0] * 54
    mean, nz, p = gj.june_floor(ok, [0] * 60)
    assert nz == 6 and mean >= m and p
    mean, nz, p = gj.june_floor([day] * 5 + [0] * 55, [0] * 60)       # 5 nonzero June days -> fails
    assert nz == 5 and not p
    mean, nz, p = gj.june_floor([m * 10] * 5 + [0] * 17 + [m * 10] * 38, [0] * 60)  # post-June days do not count
    assert nz == 5 and not p
    mean, nz, p = gj.june_floor([m - 1] * 60, [0] * 60)               # 60-day-eligible but June mean < $100
    assert mean < m and not p
    mean, nz, p = gj.june_floor([0] * 22 + [10**12] * 38, [0] * 60)   # rich only after June
    assert mean == 0 and not p
    # USDT counts toward the combined holdings and the nonzero days
    mean, nz, p = gj.june_floor([0] * 60, [m * 3] * 10 + [0] * 50)
    assert nz == 10 and p


def test_guard_refuses_post_cutoff_block_before_any_request(monkeypatch):
    monkeypatch.setattr(g4, "_batch_post", lambda *a, **k: pytest.fail("request was sent"))
    with pytest.raises(g4.PostPeriodRead):
        gj._has_code_many(["0x" + "1" * 40], PRE_END + 1, PRE_END, _NoCkpt())
    with pytest.raises(g4.PostPeriodRead):
        g4._point_batch("x", "eth_getBalance", [(["0x0", "0x1"], PRE_END + 1)], PRE_END)
    with pytest.raises(g4.PostPeriodRead):
        gj._guard(PRE_END)(PRE_END + 1)


class _NoCkpt:
    data = {}

    def put(self, k, v):
        self.data[k] = v


def test_has_code_many_checkpoints_and_receipts(monkeypatch, tmp_path):
    monkeypatch.setattr(g4, "_RECEIPTS_PATH", str(tmp_path / "r.jsonl"))
    sent = []

    def post(ep, calls, validate=None, **k):
        sent.append(len(calls))
        return ["0x6080" if c[1][0].endswith("1") else "0x" for c in calls]

    monkeypatch.setattr(g4, "_batch_post", post)
    ck = _NoCkpt()
    ck.data = {}
    addrs = ["0x" + "0" * 39 + "1", "0x" + "0" * 39 + "2"]
    out = gj._has_code_many(addrs, JUNE_END, PRE_END, ck)
    assert out == {addrs[0]: True, addrs[1]: False}
    assert gj._has_code_many(addrs, JUNE_END, PRE_END, ck) == out and sent == [2]   # second call hit the checkpoint
    rec = json.loads((tmp_path / "r.jsonl").read_text())
    assert rec["toBlock"] == JUNE_END and g4._assert_receipts_within(str(tmp_path / "r.jsonl"), PRE_END) == 1


def test_valid_code_rejects_null():
    assert gj._valid_code("0x") == "0x" and gj._valid_code("0xab")
    for bad in (None, "", "ab", {"x": 1}):
        with pytest.raises(ValueError):
            gj._valid_code(bad)


def _tlog(addr, block, topic0, topics=(), data="0x", tx="0x1"):
    return {"address": addr, "blockNumber": hex(block), "transactionHash": tx, "topics": [topic0, *topics], "data": data}


def test_exposure_arms_and_exclusion():
    from gates import g1_cohort as g1
    from gates import g1b_frame as g1b
    pool, other = "0x" + "9" * 40, "0x" + "8" * 40
    w = lambda a: "0x" + a[2:].rjust(64, "0")
    A_, B_, C_ = "0x" + "a" * 40, "0x" + "b" * 40, "0x" + "c" * 40
    wd = [_tlog(pool, 100, g1b.WITHDRAWAL_TOPIC, [w(C_)], w(A_) + "00" * 64),    # A withdrew at a pool
          _tlog(other, 100, g1b.WITHDRAWAL_TOPIC, [w(C_)], w(B_) + "00" * 64),   # B withdrew at a non-designated instance
          _tlog(pool, 101, g1b.WITHDRAWAL_TOPIC, [w(C_)], w(g4.ZERO_ADDRESS) + "00" * 64)]
    notes = [_tlog(other, 200, g1b.ENCRYPTED_NOTE_TOPIC, [w(A_)], tx="0xd"), _tlog(other, 201, g1b.ENCRYPTED_NOTE_TOPIC, [w(C_)], tx="0xe")]
    deps = [_tlog(pool, 200, g1.DEPOSIT_TOPIC, tx="0xd"), _tlog(pool, 201, g1.DEPOSIT_TOPIC, tx="0xe"),
            _tlog(pool, 202, g1.DEPOSIT_TOPIC, tx="0xf")]                      # 0xf has no note: unattributed
    arms = gj.exposure_arms(wd, notes, deps, [pool], 1000)
    assert arms == {A_: "both", C_: "deposit"}                                 # B not at a designated pool; zero dropped
    assert gj.exclusion_set(wd, notes) == {A_, B_, C_, g4.ZERO_ADDRESS}        # exclusion covers every instance
    with pytest.raises(g4.PostPeriodRead):
        gj.exposure_arms(wd, notes, deps, [pool], 150)                         # a log after june_end is refused


def test_by_day_walk_records_roundtrip(tmp_path):
    d0 = 1654732800
    logs = [_l(g4.ZERO_ADDRESS, "to", d0 + 5), _l("0x" + "b" * 40, "to", d0 + 86400 * 25)]
    out = g4.decode_unit_logs(logs, "to", {A}, PRE_START, PRE_END, g4.USDC, by_day=True)
    assert out[A][3] == {0: [1, 1], 25: [1, 0]}
    g4._OUT_DIR = str(tmp_path)
    ck = g4._Ckpt("w.jsonl")
    ck.put("USDC:to:0", {"ranges": [[1, 2, 2, "v2:x", "tenderly"]], "logs": 2,
                         "addrs": {A: [sorted(out[A][0].items()), out[A][1], out[A][2], sorted(out[A][3].items())]}})
    flows, legs, mr, dlegs = g4._merge_walk(ck, {A}, by_day=True)
    assert dlegs["USDC"][A] == {0: [1, 1], 25: [1, 0]} and legs["USDC"][A] == 2 and mr["USDC"][A] == 1
    # without by_day the old 3-tuple shape still merges (g4 checkpoints), and by_day refuses them
    ck.put("USDC:to:1000", {"ranges": [[1, 2, 0, "v2:y", "tenderly"]], "logs": 0, "addrs": {A: [[], 1, 0]}})
    f3 = g4._merge_walk(ck, {A})
    assert len(f3) == 3
    with pytest.raises(RuntimeError, match="by_day"):
        g4._merge_walk(ck, {A}, by_day=True)


def _l(frm, pos, ts):
    blk = 14_929_600 + (ts - 1654732800) // 13
    t = lambda a: "0x" + a[2:].rjust(64, "0")
    gj_l = {"blockNumber": hex(blk), "blockTimestamp": hex(ts), "transactionHash": "0x1", "address": g4.USDC,
            "blockHash": hex(ts), "logIndex": hex(ts % 1000), "topics": [g4.TRANSFER_TOPIC, t(frm), t(A)],
            "data": "0x" + f"{5:064x}"}
    return gj_l


A = "0x" + "a" * 40


def test_schema_equality_with_g4():
    assert [(n, t) for n, t, _ in gj.COV_COLS] == [(n, t) for n, t, _ in g4.COV_COLS]
    assert [(n, t) for n, t, _ in gj.PANEL_COLS] == [(n, t) for n, t, _ in g4.PANEL_COLS]
    # and against the real g4 outputs when present (data dir is untracked)
    for root in (os.path.join(os.path.dirname(gj.__file__), "..", "out"),):
        p = os.path.join(root, "g4", "covariates.csv")
        if os.path.exists(p):
            with open(p, newline="") as f:
                assert next(csv.reader(f)) == [c[0] for c in gj.COV_COLS]
            with open(os.path.join(root, "g4", "wallet_coin_day_pre.csv"), newline="") as f:
                assert next(csv.reader(f)) == [c[0] for c in gj.PANEL_COLS]
            sch = json.load(open(os.path.join(root, "g4", "covariates.schema.json")))
            assert [(c["name"], c["dtype"]) for c in sch["columns"]] == [(n, t) for n, t, _ in gj.COV_COLS]
            break


def test_blacklisted_in_window_any_time():
    S, E = 100, 1000
    f = gj.blacklisted_in_window
    assert f([], S, E) is False
    assert f([(50, 0, "add")], S, E) is True                              # listed before the window, still listed
    assert f([(50, 0, "add"), (80, 0, "remove")], S, E) is False          # removed before the window
    assert f([(200, 0, "add"), (300, 0, "remove")], S, E) is True         # added then removed inside: counts
    assert f([(E + 1, 0, "add")], S, E) is False                          # after june_end never consulted
    assert f([(50, 0, "add"), (E + 1, 0, "remove")], S, E) is True


# --- S10c review fixes: lock, input preflight, result cap, quota, atomic/torn files ---

def test_run_lock_second_acquire_fails(tmp_path):
    first = gj.acquire_run_lock(str(tmp_path))
    with pytest.raises(gj.RunLocked):
        gj.acquire_run_lock(str(tmp_path))
    first.close()
    gj.acquire_run_lock(str(tmp_path)).close()  # released on close


def _g0(tmp_path, skip=None):
    for n in gj.INPUT_FILES:
        if n != skip:
            (tmp_path / n).write_text(f"data {n}\n")
    return str(tmp_path)


@pytest.mark.parametrize("missing", gj.INPUT_FILES)
def test_preflight_missing_input_raises(tmp_path, missing):
    with pytest.raises(RuntimeError, match=missing):
        gj.preflight_inputs(_g0(tmp_path, skip=missing))


def test_preflight_hashes_and_meta_mismatch_refuses(tmp_path):
    d = _g0(tmp_path)
    h1 = gj.preflight_inputs(d)
    assert set(h1) == set(gj.INPUT_FILES) and all(len(v) == 64 for v in h1.values())
    meta_path = str(tmp_path / "meta.json")
    meta = {"frame_sha256": "x", "inputs": h1}
    gj.check_meta(meta_path, meta, "g")
    gj.check_meta(meta_path, meta, "g")  # identical resume ok
    (tmp_path / "usdt_events.csv").write_text("changed\n")
    with pytest.raises(RuntimeError, match="stale"):
        gj.check_meta(meta_path, {**meta, "inputs": gj.preflight_inputs(d)}, "g")


def test_preflight_empty_file_raises(tmp_path):
    d = _g0(tmp_path)
    (tmp_path / "usdt_events.csv").write_text("")
    with pytest.raises(RuntimeError, match="usdt_events"):
        gj.preflight_inputs(d)


def test_repair_jsonl_tail(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_bytes(b'{"a": 1}\n{"b": 2}\n{"c": 3')
    assert gj.repair_jsonl_tail(str(p))
    assert p.read_bytes() == b'{"a": 1}\n{"b": 2}\n'
    p.write_bytes(b'{"a": 1}\n{"b": 2}')  # valid but unterminated
    assert gj.repair_jsonl_tail(str(p)) and p.read_bytes() == b'{"a": 1}\n{"b": 2}\n'
    assert not gj.repair_jsonl_tail(str(p))
    p.write_bytes(b'{"a')  # single torn record
    assert gj.repair_jsonl_tail(str(p)) and p.read_bytes() == b""


def test_blocks_save_atomic(monkeypatch, tmp_path):
    monkeypatch.setattr(blocks, "_BLOCKS_PATH", str(tmp_path / "blocks.json"))
    blocks.pin("x", 5, "2022-01-01T00:00:00Z")
    assert blocks.load_pinned()["x"]["block"] == 5
    assert os.listdir(tmp_path) == ["blocks.json"]


def test_assert_walk_within():
    ok = {"k": {"ranges": [[1, 10, 3, "d", "tenderly"]]}}
    assert g4._assert_walk_within(ok, 10) == 1
    with pytest.raises(g4.PostPeriodRead):
        g4._assert_walk_within(ok, 9)


# --- S15: census recovery wiring (full matrix in tests/test_recovery.py) ---

def test_sweep_resumes_from_chunks_and_rejects_other_binding(monkeypatch, tmp_path):
    t0 = "0x" + "aa" * 32
    ev = [{"blockNumber": hex(b), "transactionHash": f"0x{b:064x}", "logIndex": "0x0", "address": "0x" + "11" * 20,
           "topics": [t0], "data": "0x"} for b in (10, 150)]
    monkeypatch.setattr(g4, "_receipt", lambda rec: None)
    calls = []

    def fetch(endpoint, a, b, address, topics):
        calls.append((a, b))
        if len(calls) == 2:
            raise rpc.GapError("provider down")
        out = [l for l in ev if a <= int(l["blockNumber"], 16) <= b]
        return out, b"[]"

    monkeypatch.setattr(rpc, "_fetch_one_range", fetch)
    ctx = ("commit", "cfg", {"g0": "h"})
    with pytest.raises(rpc.GapError):
        gj._sweep(str(tmp_path), "s", "tenderly", 0, 199, None, [[t0]], 100, ctx, PRE_END, "g")
    logs, proof = gj._sweep(str(tmp_path), "s", "tenderly", 0, 199, None, [[t0]], 100, ctx, PRE_END, "g")
    assert calls == [(0, 99), (100, 199), (100, 199)] and len(logs) == 2 and proof["chunks"] == 2
    with pytest.raises(rpc.CheckpointError):
        gj._sweep(str(tmp_path), "s", "tenderly", 0, 199, None, [[t0]], 100, ("other", "cfg", {"g0": "h"}), PRE_END, "g")


def test_baseline_input_is_explicit(monkeypatch):
    monkeypatch.setenv("G4_JUNE_BASELINE_BLACKLIST_DIR", "/y")  # env var no longer honoured
    with pytest.raises(RuntimeError, match="never falls back"):
        gj.baseline_dir({})


def test_g4_main_baseline_opt_in_records_source_and_hash(monkeypatch, tmp_path):
    from gates import g0_baseline_blacklist as g0b
    seen = {}

    class FakeBase:
        manifest_sha256 = "ab" * 32

        def events(self, universe):
            seen["universe"] = universe
            return {"USDC": {}, "USDT": {}}

    monkeypatch.setattr(g0b, "load_baseline", lambda d, consumer, pinned=None: (seen.update(d=d, consumer=consumer) or FakeBase()))
    pinned = {"pre_period_end": {"block": PRE_END}}
    monkeypatch.setattr(g4, "_load_blacklist_events", lambda u, e: pytest.fail("default path used"))
    logs = []
    monkeypatch.setattr(g4, "_log", lambda m: logs.append(m))
    monkeypatch.setenv("G4_BASELINE_BLACKLIST_DIR", str(tmp_path))  # env route: loud log, source recorded
    bl, info = g4._resolve_blacklist({}, pinned, {"0xa"})
    assert info["source"].startswith("env:") and info["manifest_sha256"] == "ab" * 32
    assert info["realpath"] == os.path.realpath(str(tmp_path)) and seen["consumer"] == "main"
    assert any("ENV" in m for m in logs)
    logs.clear()  # cfg route: no env warning
    _, info = g4._resolve_blacklist({"baseline_blacklist_dir": str(tmp_path)}, pinned, {"0xa"})
    assert info["source"] == "cfg" and not logs
    monkeypatch.delenv("G4_BASELINE_BLACKLIST_DIR")  # default: unchanged out/g0 path, no info
    monkeypatch.setattr(g4, "_load_blacklist_events", lambda u, e: ("default", e))
    assert g4._resolve_blacklist({}, pinned, {"0xa"}) == (("default", PRE_END), None)


# ---- participation rule (baseline_transfer_required | none), as main g1 ----------------------------------------------

B = "0x" + "b" * 40
C = "0x" + "c" * 40
D = "0x" + "d" * 40
E = "0x" + "e" * 40


def _leg(token, frm, to, value, ts, n):
    t = lambda a: "0x" + a[2:].rjust(64, "0")
    return {"blockNumber": hex(14_929_600 + (ts - 1654732800) // 13), "blockTimestamp": hex(ts), "transactionHash": "0x%x" % n,
            "address": token, "blockHash": hex(ts + n), "logIndex": hex(n), "topics": [g4.TRANSFER_TOPIC, t(frm), t(to)],
            "data": "0x" + f"{value:064x}"}


def _walk_legs(tmp_path):
    """Merge a by_day walk (returns dlegs): A = zero-value third-party USDC leg on day 5, B = one USDT leg on day 5,
    C = no leg, D = ONLY a leg after the June placebo pre window (day 30, inside main's pre window), E = day-21 leg
    (last June day) plus a later one."""
    d0 = 1654732800
    g4._OUT_DIR = str(tmp_path)
    ck = g4._Ckpt("w.jsonl")
    for key, token, addr, frm, value, n, day in (("USDC:to:0", g4.USDC, A, B, 0, 1, 5), ("USDT:to:0", g4.USDT, B, C, 7, 2, 5),
                                                 ("USDC:to:1", g4.USDC, D, C, 3, 3, 30), ("USDC:to:2", g4.USDC, E, C, 3, 4, 21),
                                                 ("USDT:to:3", g4.USDT, E, C, 3, 5, 35)):
        logs = [_leg(token, frm, addr, value, d0 + day * 86400 + 5, n)]
        out = g4.decode_unit_logs(logs, "to", {A, B, C, D, E}, PRE_START, PRE_END, token, by_day=True)
        ck.put(key, {"ranges": [[1, 2, len(logs), "v2:x", "tenderly"]], "logs": len(logs),
                     "addrs": {a: [sorted(r[0].items()), r[1], r[2], sorted(r[3].items())] for a, r in out.items()}})
    return g4._merge_walk(ck, {A, B, C, D, E}, by_day=True)[3]


def test_participation_rule_filters_population_from_held_walk_legs(tmp_path):
    dlegs = _walk_legs(tmp_path)
    universe = sorted([A, B, C, D, E])
    # zero-value third-party log counts (A); C has none; D's only leg is AFTER the June pre window (inside main's window) -> excluded
    assert gj.june_participants(universe, dlegs, "baseline_transfer_required") == sorted([A, B, E])
    assert D not in gj.june_participants(universe, dlegs, "baseline_transfer_required")
    assert gj.june_participants(universe, dlegs, "none") == universe
    with pytest.raises(ValueError, match="participation_rule"):
        gj.june_participants(universe, dlegs, None)


def test_participation_window_is_june_pre_window_not_main():
    w = gj.participation_window(PRE_START, 15_053_225)
    assert w["after_block_exclusive"] == PRE_START and w["to_block_inclusive"] == 15_053_225 < PRE_END
    assert (w["first_date"], w["last_date"], w["day_index_lt"]) == ("2022-06-09", "2022-06-30", gj.N_JUNE_DAYS)


def test_run_requires_participation_rule_before_any_work(tmp_path):
    for cfg in ({}, {"participation_rule": "maybe"}):
        with pytest.raises(ValueError, match="participation_rule"):
            gj.run({"output_dir": str(tmp_path / "o"), "input_root": str(tmp_path), **cfg})
    assert not (tmp_path / "o.work").exists()


def test_rule_recorded_in_schema_notes_like_main():
    for rule in ("baseline_transfer_required", "none"):
        note = gj.participation_note(rule)
        assert note.startswith(f"participation_rule={rule} ")
        assert len(set(re.findall(r"\bparticipation_rule=[A-Za-z_-]+", note))) == 1   # R's cohort_schema_rule regex finds exactly one


def test_participation_window_has_a_lower_bound():
    """LOW-3: a leg on a negative day index (before pre_period_start) is not in (pre_period_start, june_end] and never counts."""
    dlegs = {c: {} for c in g4.COINS}
    dlegs["USDC"][A] = {-1: [1, 0]}
    dlegs["USDC"][B] = {0: [1, 0]}
    assert gj.june_participants([A, B], dlegs, "baseline_transfer_required") == [B]
