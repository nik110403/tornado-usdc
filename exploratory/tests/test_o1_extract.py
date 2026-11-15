"""Fixture-only tests for exploratory/o1_extract.py. No network; no real wallet; no out/ read."""
import copy
import csv
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
from exploratory import o1_extract as o1  # noqa: E402
from gates import g4_controls as g4  # noqa: E402
from lib import rpc  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
LED = json.load(open(os.path.join(FIX, "o1_ledger.json")))
PIN, DAY0 = LED["pin"], LED["day0"]
STEP = 21600
T_PIN = o1._ts_utc(o1.FIRST_DATE) - STEP
AA1, AA2, BB1, BB2 = ["0x" + c * 38 + n for c, n in (("a", "01"), ("a", "02"), ("b", "01"), ("b", "02"))]


def ts_of(b):
    return T_PIN + (b - PIN) * STEP


class FakeChain:
    """Ledger-backed chain. `tamper[endpoint]` edits the log list; `gap(ep, coin, topics)` / `bal_fail(ep, coin, addrs, block)` inject failures."""

    def __init__(self, tamper=None, gap=None, bal_fail=None):
        self.tamper, self.gap, self.bal_fail = tamper or {}, gap, bal_fail
        self.log_calls, self.bal_calls = [], []

    def block_ts(self, ep, b):
        return ts_of(b)

    def balance_of(self, ep, coin, addrs, block):
        self.bal_calls.append((ep, coin, tuple(addrs), block))
        if self.bal_fail and self.bal_fail(ep, coin, addrs, block):
            raise RuntimeError("archive read failed")
        out = []
        for a in addrs:
            v = LED["open"][coin].get(a, 0)
            for b, c, f, t, val in LED["transfers"]:
                if c == coin and b <= block:
                    v += (val if t == a else 0) - (val if f == a else 0)
            if coin == "USDT":
                v -= sum(d["amount"] for d in LED["destroyed"] if d["address"] == a and d["block"] <= block)
            out.append(v)
        return out

    def logs(self, ep, coin, topics, start, end, guard):
        assert end <= guard
        self.log_calls.append((ep, coin, start, end))
        if self.gap and self.gap(ep, coin, topics):
            raise rpc.GapError(f"{ep} [{start},{end}] failed")
        if len(topics) == 1:                                   # DestroyedBlackFunds, topic-only
            assert topics[0] == o1.DESTROYED_TOPIC and coin == "USDT"
            logs = []
            for i, d in enumerate(LED["destroyed"]):
                if start <= d["block"] <= end:
                    b = d["block"]
                    logs.append({"address": g4.COINS["USDT"].lower(), "blockNumber": hex(b), "blockTimestamp": hex(ts_of(b)),
                                 "blockHash": f"0x{b:064x}", "logIndex": hex(900 + i), "transactionHash": f"0x{900 + i:064x}",
                                 "topics": [o1.DESTROYED_TOPIC], "data": "0x" + "0" * 24 + d["address"][2:] + f"{d['amount']:064x}"})
            if ep in self.tamper:
                logs = self.tamper[ep](logs)
            return logs, [(start, end, len(logs), g4._digest(logs, with_block=True), ep)]
        want = {t[-40:] for t in (topics[1] if topics[1] is not None else topics[2])}
        slot = 1 if topics[1] is not None else 2
        logs = []
        for i, (b, c, f, t, val) in enumerate(LED["transfers"]):
            if c != coin or not (start <= b <= end):
                continue
            if (f if slot == 1 else t)[2:] not in want:
                continue
            logs.append({"address": g4.COINS[coin].lower(), "blockNumber": hex(b), "blockTimestamp": hex(ts_of(b)),
                         "blockHash": f"0x{b:064x}", "logIndex": hex(i), "transactionHash": f"0x{i:064x}",
                         "topics": [g4.TRANSFER_TOPIC, "0x" + f[2:].rjust(64, "0"), "0x" + t[2:].rjust(64, "0")], "data": "0x" + f"{val:064x}"})
        if ep in self.tamper:
            logs = self.tamper[ep](logs)
        return logs, [(start, end, len(logs), g4._digest(logs, with_block=True), ep)]


def _wanted(topics):
    """address topics of a Transfer unit query ([] for the topic-only DestroyedBlackFunds query)."""
    return [] if len(topics) == 1 else (topics[1] or topics[2])


def mk_cfg(tmp_path, **kw):
    arms = o1.load_universe(os.path.join(FIX, "o1_membership.csv"))
    cfg = {"arms": arms, "signed0": o1.load_signed_day0(os.path.join(FIX, "o1_signed_cov.csv"), os.path.join(FIX, "o1_signed_panel.csv")),
           "pin": PIN, "day0": DAY0, "outdir": str(tmp_path), "walk_ep": "mevblocker", "verify_ep": "tenderly",
           "max_mismatch_frac": 0.005, "max_reverify_frac": 0.01, "reverify_fraction": 0.05, "producer": "test-producer"}
    cfg.update(kw)
    return cfg


def rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


# ---- loaders / blocks --------------------------------------------------------------------------------------------

def test_loaders_fixture():
    cfg = mk_cfg("x")
    assert len(cfg["arms"]) == 4 and cfg["arms"][AA1] == "exposed" and cfg["arms"][BB2] == "control"   # unmatched row excluded
    assert len(cfg["signed0"]) == 8 and cfg["signed0"][(AA1, "USDC")] == LED["expected_day0"]["USDC"][AA1]   # day 1 rows ignored


def test_pin_and_day0_block_checks_and_bisection():
    assert o1.check_pin_blocks(ts_of, PIN, DAY0)["pin"] == T_PIN
    with pytest.raises(o1.Abort):
        o1.check_pin_blocks(ts_of, PIN + 1, DAY0)
    with pytest.raises(o1.Abort):
        o1.check_pin_blocks(ts_of, PIN, DAY0 - 1)
    assert o1.last_block_before(o1._ts_utc(o1.FIRST_DATE), ts_of, 900, 2000) == PIN
    assert o1.last_block_before(o1._ts_utc(o1.DAY0_DATE.replace(day=10)), ts_of, PIN, 2000) == DAY0


# ---- identity: pass / mismatch -----------------------------------------------------------------------------------

def test_identity_pass_and_panel(tmp_path):
    s = o1.run_extraction(mk_cfg(tmp_path), FakeChain())
    assert s["counts_by_arm"] == {"control": {"ok": 2}, "exposed": {"ok": 2}} and not s["aborted"]
    assert s["destroyed_events_applied"] == 1 and s["reverify"]["n_log_mismatches"] == 0 and s["reverify"]["n_balance_mismatches"] == 0
    panel = rows(tmp_path / "o1_panel.csv")
    assert len(panel) == 4 * 2 * 100 and {r["day_index"] for r in panel} == {str(d) for d in range(-100, 0)}
    assert all(r["banner"].startswith("EXPLORATORY, POST-RESULTS") for r in panel)
    exp = LED["expected_day0"]
    # day -1 (end of 8 Jun) = day-0 balance minus the 9-Jun flows (none in the fixture except block 1403/1404 which are day 0)
    by = {(r["address"], r["coin"], int(r["day_index"])): int(r["balance_units"]) for r in panel}
    assert by[(AA1, "USDC", -1)] == exp["USDC"][AA1] - 123_456 and by[(BB1, "USDT", -1)] == exp["USDT"][BB1] + 2_000_000
    assert by[(BB2, "USDT", -100)] == LED["open"]["USDT"][BB2] and by[(BB2, "USDT", -50)] == LED["open"]["USDT"][BB2] - 5_000_000 + 3_000_000
    assert by[(AA1, "USDC", -100)] == LED["open"]["USDC"][AA1] - 10_000_000     # block 1003 is on 1 Mar
    assert (tmp_path / "MANIFEST.sha256").exists() and o1.assert_receipts(str(tmp_path / "receipts.jsonl"), PIN, DAY0) > 0


def test_identity_mismatch_excluded_and_counted(tmp_path):
    cfg = mk_cfg(tmp_path)
    cfg["signed0"] = dict(cfg["signed0"]); cfg["signed0"][(BB1, "USDC")] += 1
    with pytest.raises(o1.Abort, match="mismatch fraction"):                # 1/2 complete control wallets > 0.5% -> stop, no panel
        o1.run_extraction(cfg, FakeChain())
    st = {r["address"]: r for r in rows(tmp_path / "o1_wallet_status.csv")}
    assert st[BB1]["status"] == "mismatch" and st[BB1]["reason"].startswith("identity_USDC_diff_-1") and st[AA1]["status"] == "ok"
    assert not (tmp_path / "o1_panel.csv").exists()
    out2 = tmp_path / "b"; out2.mkdir()
    cfg2 = dict(cfg, outdir=str(out2), max_mismatch_frac=0.5)                # flag-settable fraction (1/2 is not > 0.5): excluded + counted by arm
    s = o1.run_extraction(cfg2, FakeChain())
    assert s["counts_by_arm"] == {"control": {"ok": 1, "mismatch": 1}, "exposed": {"ok": 2}} and s["n_mismatch"] == 1
    assert s["mismatch_by_arm"]["control"] == {"n_complete": 2, "n_mismatch": 1, "fraction": 0.5} and s["mismatch_by_arm"]["exposed"]["n_mismatch"] == 0
    assert BB1 not in {r["address"] for r in rows(out2 / "o1_panel.csv")}


def test_classify_no_signed_day0_and_negative_are_mismatch_not_error():
    arms = {AA1: "exposed", AA2: "exposed"}
    opening = {(c, a): 0 for c in o1.COINS for a in arms}
    flows = {("USDC", AA2): {-90: -5}}
    signed = {(AA2, "USDC"): 0, (AA2, "USDT"): 0}
    r = o1.classify_wallets(arms, opening, flows, set(), [], signed, PIN, DAY0)
    assert r[AA1]["reason"] == "no_signed_day0_USDC" and r[AA2]["reason"] == "negative_balance_USDC_day-90"
    assert r[AA1]["status"] == r[AA2]["status"] == "mismatch"


# ---- gaps / missing pin ------------------------------------------------------------------------------------------

def test_gap_marks_only_touched_wallets_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(o1, "WALK_ARRAY", 2)
    gap = lambda ep, coin, topics: any(BB1[-40:] in t for t in _wanted(topics))   # noqa: E731
    s = o1.run_extraction(mk_cfg(tmp_path), FakeChain(gap=gap))
    assert s["counts_by_arm"] == {"control": {"missing": 2}, "exposed": {"ok": 2}} and s["n_walk_units_gap"] == 4
    st = {r["address"]: r for r in rows(tmp_path / "o1_wallet_status.csv")}
    assert st[BB2]["reason"] == "gap" and st[AA1]["status"] == "ok"
    panel_addrs = {r["address"] for r in rows(tmp_path / "o1_panel.csv")}
    assert panel_addrs == {AA1, AA2}                                           # never zero-filled


def test_gap_then_resume_refetches_only_failed_units(tmp_path, monkeypatch):
    monkeypatch.setattr(o1, "WALK_ARRAY", 2)
    gap = lambda ep, coin, topics: coin == "USDT" and any(BB1[-40:] in t for t in _wanted(topics))   # noqa: E731
    o1.run_extraction(mk_cfg(tmp_path), FakeChain(gap=gap))
    ch = FakeChain()
    s = o1.run_extraction(mk_cfg(tmp_path), ch)                               # same dir, same params = resume
    walk_calls = [c for c in ch.log_calls if c[0] == "mevblocker"]
    assert len(walk_calls) == 2 and {c[1] for c in walk_calls} == {"USDT"}    # only the two failed USDT units (from, to)
    assert s["counts_by_arm"] == {"control": {"ok": 2}, "exposed": {"ok": 2}}
    assert not [c for c in ch.bal_calls if c[3] == PIN and c[0] == "mevblocker"]   # pin balances came from the checkpoint


def test_missing_pin_balance_for_either_coin_is_missing_never_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(o1, "OPEN_BATCH", 2)
    fail = lambda ep, coin, addrs, block: coin == "USDT" and block == PIN and AA1 in addrs   # noqa: E731
    s = o1.run_extraction(mk_cfg(tmp_path), FakeChain(bal_fail=fail))
    assert s["counts_by_arm"] == {"control": {"ok": 2}, "exposed": {"missing": 2}}
    st = {r["address"]: r for r in rows(tmp_path / "o1_wallet_status.csv")}
    assert st[AA1]["reason"] == "pin_missing_USDT"
    pins = {(r["address"], r["coin"]): r["balance_units"] for r in rows(tmp_path / "o1_pin_balances.csv")}
    assert pins[(AA1, "USDT")] == "" and pins[(AA1, "USDC")] != ""             # blank, not 0


def test_pin_balance_falls_back_to_second_endpoint_then_reverify_uses_the_other_one(tmp_path):
    fail = lambda ep, coin, addrs, block: ep == "mevblocker" and block == PIN   # noqa: E731
    with pytest.raises(RuntimeError, match="archive read failed"):    # records came from tenderly -> reverify must read mevblocker: down, nothing claimed
        o1.run_extraction(mk_cfg(tmp_path), FakeChain(bal_fail=fail))
    ck = o1.Ckpt(str(tmp_path / "checkpoint.jsonl"))
    assert {v["ep"] for k, v in ck.data.items() if k.startswith("open:")} == {"tenderly"}
    ck.close()
    s = o1.run_extraction(mk_cfg(tmp_path), FakeChain())                  # resume with a healthy endpoint
    assert s["counts_by_arm"] == {"control": {"ok": 2}, "exposed": {"ok": 2}}


# ---- reverify: range count + sha256 -----------------------------------------------------------------------------

def _drop_one(logs):
    return logs[1:] if logs and len(logs[0]["topics"]) == 3 else logs


def _alter_value(logs):
    if not logs or len(logs[0]["topics"]) != 3:
        return logs
    l = copy.deepcopy(logs[0]); l["data"] = "0x" + f"{int(l['data'], 16) + 1:064x}"
    return [l] + logs[1:]


def test_reverify_detects_count_mismatch(tmp_path):
    with pytest.raises(o1.Abort):
        o1.run_extraction(mk_cfg(tmp_path), FakeChain(tamper={"tenderly": _drop_one}))
    s = json.load(open(tmp_path / "o1_summary.json"))
    assert s["reverify"]["n_log_mismatches"] > 0 and s["aborted"] and s["n_mismatch"] == 0   # reverify differences are not identity mismatches
    assert {r["reason"] for r in rows(tmp_path / "o1_wallet_status.csv") if r["status"] == "missing"} == {"reverify"}   # missing path, not `mismatch`
    assert s["reverify"]["by_arm"]["exposed"]["fraction"] > 0.01 and s["reverify_over_threshold_arms"]


def test_reverify_detects_digest_mismatch_with_equal_count(tmp_path):
    cfg = mk_cfg(tmp_path)
    ch = FakeChain(tamper={"tenderly": _alter_value})
    with pytest.raises(o1.Abort):
        o1.run_extraction(cfg, ch)
    ck = o1.Ckpt(str(tmp_path / "checkpoint.jsonl"))
    rv = [v for k, v in ck.data.items() if k.startswith("reverify:")][0]
    assert rv["log_mismatches"] and all(m["got_n"] == m["stored_n"] and m["digest_equal"] is False for m in rv["log_mismatches"])


def test_compare_reverify_pure():
    d = g4._digest([], with_block=True)
    assert o1.compare_reverify({("a", "USDC", "from"): (2, "x")}, {("a", "USDC", "from"): (2, "x")}) == []
    assert o1.compare_reverify({("a", "USDC", "from"): (0, d)}, {}) == []                      # absent == empty on both sides
    r = o1.compare_reverify({("a", "USDC", "from"): (2, "x")}, {("a", "USDC", "from"): (2, "y")})
    assert r[0]["digest_equal"] is False and r[0]["stored_n"] == r[0]["got_n"] == 2


def test_reverify_balance_mismatch_marks_wallet(tmp_path):
    def fail(ep, coin, addrs, block):
        return False
    ch = FakeChain(bal_fail=fail)
    orig = ch.balance_of
    ch.balance_of = lambda ep, coin, addrs, block: [v + (1 if (ep == "tenderly" and block == DAY0) else 0) for v in orig(ep, coin, addrs, block)]
    with pytest.raises(o1.Abort):
        o1.run_extraction(mk_cfg(tmp_path), ch)
    assert json.load(open(tmp_path / "o1_summary.json"))["reverify"]["n_balance_mismatches"] > 0


def test_reverify_endpoint_down_raises_not_claims(tmp_path):
    gap = lambda ep, coin, topics: ep == "tenderly"   # noqa: E731
    with pytest.raises(rpc.GapError):
        o1.run_extraction(mk_cfg(tmp_path), FakeChain(gap=gap))


def test_sample_is_stratified_and_seeded():
    arms = {f"0x{i:040x}": ("exposed" if i % 2 else "control") for i in range(400)}
    a = o1.pick_reverify_sample(arms, list(arms), 0.05)
    assert a == o1.pick_reverify_sample(arms, list(arms), 0.05) and len(a) == 20
    assert sum(arms[x] == "exposed" for x in a) == 10


# ---- hard rules -------------------------------------------------------------------------------------------------

def test_post_day0_block_refused():
    with pytest.raises(g4.PostPeriodRead):
        o1._guard(DAY0 + 1, DAY0)


def test_single_slot_lock(tmp_path):
    import fcntl
    with open(tmp_path / ".slot.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        with pytest.raises(o1.Abort, match="slot"):
            o1.run_extraction(mk_cfg(tmp_path), FakeChain())


def test_out_guard_semantics(tmp_path):
    repo = tmp_path / "repo"; (repo / "out" / "exploratory").mkdir(parents=True); (repo / "out" / "recovery").mkdir()
    r = str(repo)
    for bad in ("out/recovery/x", "out/g4/x", "out", "out/exploratory/../recovery/y", "out/../out/z"):
        with pytest.raises(o1.Abort):
            o1.prepare_outdir(os.path.join(r, bad), r)
    ok = o1.prepare_outdir(os.path.join(r, "out/exploratory/o1-test"), r)
    assert os.path.isdir(ok)
    with pytest.raises(o1.Abort, match="new or empty"):           # non-empty without --resume
        (repo / "out/exploratory/o1-test/f").write_text("x")
        o1.prepare_outdir(ok, r)
    (repo / "out/exploratory/o1-test/run_params.json").write_text("{}")
    assert o1.prepare_outdir(ok, r, resume=True) == ok            # resume of an O1 run
    os.symlink(str(repo / "out" / "recovery"), str(tmp_path / "link"))
    with pytest.raises(o1.Abort):                                 # parent-symlink into out/recovery
        o1.prepare_outdir(str(tmp_path / "link" / "new"), r)
    assert o1.prepare_outdir(str(tmp_path / "elsewhere"), r)      # outside out/ entirely is allowed (as o3)


SIGNED_BOUND = ["prespec", "config", "R", "gates", "lib", "run_all.R", "run_all.py", "pyproject.toml"]


def test_no_edit_of_signed_bound_files():
    """Nothing the signed results depend on changed since the pre-O1 base commit 4de4d16.

    The signed-bound set is the one exploratory/README.md names ("prespec/*, config/blocks.json, run_all.*, R/, gates/") plus
    lib/ (rpc, keccak, blocks: imported by the signed gates and by o1_extract, hence part of its producer hash) and
    pyproject.toml (dependency pins). Whole directories are used, not single files, so a new file in them is caught too.
    Committed outputs under out/ are NOT in the set: they are autopilot-synced results, not pipeline code, and graphify-out/
    and other housekeeping commits legitimately change the repo between the two commits.
    Non-vacuity: the base commit must resolve, and every listed path must exist at HEAD (so the pathspec cannot silently match
    nothing)."""
    import subprocess
    repo = os.path.join(HERE, "..", "..")
    base = "4de4d16ef6c1c3271c7cfe030a2a09b2d8468478"
    assert SIGNED_BOUND
    ok = subprocess.run(["git", "cat-file", "-e", base + "^{commit}"], cwd=repo, capture_output=True, text=True)
    if ok.returncode != 0:
        import pytest
        pytest.skip("requires the private repository history (base commit 4de4d16 is not in this tree); see PROVENANCE.md")
    for p in SIGNED_BOUND:
        assert os.path.exists(os.path.join(repo, p)), f"signed-bound path missing at HEAD: {p}"
    r = subprocess.run(["git", "diff", "--name-only", base, "HEAD", "--"] + SIGNED_BOUND, cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == [], f"signed-bound files changed since {base[:7]}: {r.stdout.split()}"


# =================================================== S33b fix round ===================================================

import hashlib  # noqa: E402
import stat  # noqa: E402


def _arms400(n_exposed_mm=0, n_control_mm=0):
    arms = {f"0x{i:040x}": ("exposed" if i % 2 else "control") for i in range(400)}
    res = {a: {"status": "ok", "reason": "", "series": None} for a in arms}
    ex = [a for a in sorted(arms) if arms[a] == "exposed"][:n_exposed_mm]
    co = [a for a in sorted(arms) if arms[a] == "control"][:n_control_mm]
    for a in ex + co:
        res[a] = {"status": "mismatch", "reason": "identity_USDC_diff_1", "series": None}
    return arms, res


def test_mismatch_stop_is_a_fraction_of_complete_wallets_in_either_arm():
    arms, res = _arms400(1, 0)                                    # 1/200 = 0.5% exactly: not over
    st = o1.mismatch_stats(arms, res)
    assert st["exposed"] == {"n_complete": 200, "n_mismatch": 1, "fraction": 0.005} and o1.over_threshold(st, o1.MAX_MISMATCH_FRAC) == []
    arms, res = _arms400(0, 2)                                    # 2/200 = 1% in the CONTROL arm alone -> stop
    assert o1.over_threshold(o1.mismatch_stats(arms, res), 0.005) == ["control"]
    arms, res = _arms400(1, 1)                                    # pooled count 2 but each arm at 0.5%: not over (not an absolute pooled count)
    assert o1.over_threshold(o1.mismatch_stats(arms, res), 0.005) == []
    arms, res = _arms400(2, 0)                                    # incomplete (missing) wallets leave the denominator
    for a in [a for a in sorted(arms) if arms[a] == "exposed" and res[a]["status"] == "ok"][:100]:
        res[a] = {"status": "missing", "reason": "gap", "series": None}
    assert o1.mismatch_stats(arms, res)["exposed"]["n_complete"] == 100
    assert o1.MAX_MISMATCH_FRAC == 0.005 and o1.MAX_REVERIFY_FRAC == 0.01 and o1.REVERIFY_FRACTION == 0.05 and o1.REVERIFY_SEED == 808


def test_negative_and_absent_day0_count_in_the_mismatch_fraction():
    arms = {AA1: "exposed", AA2: "exposed", BB1: "control", BB2: "control"}
    opening = {(c, a): 0 for c in o1.COINS for a in arms}
    flows = {("USDC", AA2): {-90: -5}}
    signed = {(a, c): 0 for a in (AA2, BB1, BB2) for c in o1.COINS}              # AA1 has no signed day-0 row
    r = o1.classify_wallets(arms, opening, flows, set(), [], signed)
    st = o1.mismatch_stats(arms, r)
    assert st["exposed"] == {"n_complete": 2, "n_mismatch": 2, "fraction": 1.0} and st["control"]["n_mismatch"] == 0


def test_reverify_sample_5pct_within_arm_seed_808_no_minimum():
    import random
    arms = {f"0x{i:040x}": ("exposed" if i % 2 else "control") for i in range(100)}     # 50 per arm -> ceil(2.5) = 3
    smp = o1.pick_reverify_sample(arms, list(arms))
    assert sum(arms[x] == "exposed" for x in smp) == 3 and sum(arms[x] == "control" for x in smp) == 3
    rng, want = random.Random(808), []
    for arm in ("control", "exposed"):                                               # one seeded stream, arms in sorted order
        want += rng.sample(sorted(a for a in arms if arms[a] == arm), 3)
    assert smp == sorted(want)
    small = {f"0x{i:040x}": ("exposed" if i % 2 else "control") for i in range(20)}     # 10 per arm -> 1, NOT the old minimum of 3
    assert len(o1.pick_reverify_sample(small, list(small))) == 2


def test_reverify_difference_is_missing_path_and_has_its_own_threshold(tmp_path):
    cfg = mk_cfg(tmp_path, max_reverify_frac=1.0, reverify_fraction=1.0)               # re-verify everyone, tolerate any difference rate
    s = o1.run_extraction(cfg, FakeChain(tamper={"tenderly": _drop_one}))
    assert not s["aborted"] and s["n_mismatch"] == 0 and s["reverify"]["n_log_mismatches"] > 0
    st = {r["address"]: r for r in rows(tmp_path / "o1_wallet_status.csv")}
    differ = [a for a, r in st.items() if r["reason"] == "reverify"]
    assert differ and all(st[a]["status"] == "missing" for a in differ)
    assert not set(differ) & {r["address"] for r in rows(tmp_path / "o1_panel.csv")}     # missing path, not zero-filled
    assert o1.over_threshold({"exposed": {"fraction": 0.011}, "control": {"fraction": 0.0}}, 0.01) == ["exposed"]
    assert o1.over_threshold({"exposed": {"fraction": 0.01}, "control": {"fraction": 0.0}}, 0.01) == []


def test_reverify_derived_daily_path_is_compared_separately(tmp_path):
    cfg = mk_cfg(tmp_path)
    o1.run_extraction(cfg, FakeChain())
    ck = o1.Ckpt(str(tmp_path / "checkpoint.jsonl"))
    universe = sorted(cfg["arms"])
    col = o1.collect(ck, universe, PIN, DAY0)
    res = o1.classify_wallets(cfg["arms"], col["opening"], col["flows"], col["gap"], col["destroyed"], cfg["signed0"])
    series = {a: {c: dict(res[a]["series"][c]) for c in o1.COINS} for a in universe}
    rc = o1.Receipts(str(tmp_path / "r2.jsonl"))
    clean = o1.run_reverify(FakeChain(), rc, universe, col, series, cfg["signed0"], PIN, DAY0, "mevblocker", "tenderly")
    assert clean["log_mismatches"] == clean["balance_mismatches"] == clean["path_mismatches"] == []
    series[AA1]["USDC"][-50] += 1                                                       # only the derived path disagrees
    bad = o1.run_reverify(FakeChain(), rc, universe, col, series, cfg["signed0"], PIN, DAY0, "mevblocker", "tenderly")
    assert bad["log_mismatches"] == bad["balance_mismatches"] == [] and [(m["addr"], m["coin"], m["first_day"]) for m in bad["path_mismatches"]] == [(AA1, "USDC", -50)]
    ck.close()


def test_reverify_catches_destroyed_range_difference(tmp_path):
    drop_destroyed = lambda logs: [] if logs and len(logs[0]["topics"]) == 1 else logs   # noqa: E731
    with pytest.raises(o1.Abort):
        o1.run_extraction(mk_cfg(tmp_path, reverify_fraction=1.0), FakeChain(tamper={"tenderly": drop_destroyed}))
    rv = [v for k, v in o1.Ckpt(str(tmp_path / "checkpoint.jsonl")).data.items() if k.startswith("reverify:")][0]
    assert any(m["pos"] == "destroyed" for m in rv["log_mismatches"]) and rv["path_mismatches"]


# ---- DestroyedBlackFunds as log ranges ---------------------------------------------------------------------------

def test_destroyed_comes_from_log_ranges_not_a_csv(tmp_path):
    ch = FakeChain()
    s = o1.run_extraction(mk_cfg(tmp_path), ch)
    assert s["destroyed_range_ok"] and s["destroyed_events_applied"] == 1
    assert any(len(c) == 4 and c[1] == "USDT" for c in ch.log_calls)
    assert "destroyed_csv" not in json.dumps(s["params"]) and not hasattr(o1, "load_destroyed")
    assert any(json.loads(l)["unit"] == "destroyed" for l in open(tmp_path / "receipts.jsonl") if '"kind": "getLogs"' in l)


def test_destroyed_range_gap_makes_every_wallet_incomplete(tmp_path):
    gap = lambda ep, coin, topics: len(topics) == 1   # noqa: E731
    s = o1.run_extraction(mk_cfg(tmp_path), FakeChain(gap=gap))
    assert s["counts_by_arm"] == {"control": {"missing": 2}, "exposed": {"missing": 2}} and not s["destroyed_range_ok"]
    assert {r["reason"] for r in rows(tmp_path / "o1_wallet_status.csv")} == {"gap"}


def test_failed_range_is_retried_once_on_the_second_endpoint(tmp_path):
    fired = []

    def gap(ep, coin, topics):
        key = (ep, coin, len(topics))
        if ep == "mevblocker" and coin == "USDC" and len(topics) == 2 and key not in fired:       # USDC 'from' unit and the destroyed range fail once on the primary
            fired.append(key)
            return True
        if ep == "mevblocker" and len(topics) == 1 and key not in fired:
            fired.append(key)
            return True
        return False
    ch = FakeChain(gap=gap)
    s = o1.run_extraction(mk_cfg(tmp_path), ch)
    assert s["counts_by_arm"] == {"control": {"ok": 2}, "exposed": {"ok": 2}} and s["n_walk_units_gap"] == 0 and s["destroyed_range_ok"]
    recs = [json.loads(l) for l in open(tmp_path / "receipts.jsonl")]
    failed = [r for r in recs if r["kind"] == "getLogs_failed"]
    assert {(r["unit"], r["endpoint"]) for r in failed} == {("unit:USDC:from:0", "mevblocker"), ("destroyed", "mevblocker")}
    assert {r["endpoint"] for r in recs if r["kind"] == "getLogs" and r["unit"] in ("unit:USDC:from:0", "destroyed")} == {"tenderly"}
    ck = o1.Ckpt(str(tmp_path / "checkpoint.jsonl"))
    assert ck.data["unit:USDC:from:0"]["ep"] == "tenderly" and ck.data["unit:USDC:to:0"]["ep"] == "mevblocker"
    ck.close()


# ---- security: out guard ----------------------------------------------------------------------------------------

def test_out_guard_dot_repo_root_and_symlinked_root(tmp_path, monkeypatch):
    repo = tmp_path / "repo"; (repo / "out" / "g4").mkdir(parents=True); (repo / "out" / "exploratory").mkdir()
    monkeypatch.chdir(repo)
    for bad in ("out/g4/x", "out/recovery/x", "out"):
        with pytest.raises(o1.Abort):
            o1.prepare_outdir(bad, ".")                              # --repo-root . : the relative root must not blind the guard
    with pytest.raises(o1.Abort):
        o1.out_guard(str(repo / "out" / "g4" / "x"), ".")
    assert o1.prepare_outdir("out/exploratory/o1-ok", ".")
    link = tmp_path / "rootlink"; os.symlink(str(repo), str(link))
    with pytest.raises(o1.Abort):                                    # symlinked root escaping into out/g4 via the link
        o1.prepare_outdir(str(link / "out" / "g4" / "x"), str(link))
    with pytest.raises(o1.Abort):
        o1.prepare_outdir(str(link / "out" / "g4"), str(link))
    os.symlink(str(repo / "out" / "g4"), str(tmp_path / "g4link"))   # root given as a symlink INTO out/g4 itself
    with pytest.raises(o1.Abort):
        o1.prepare_outdir(str(tmp_path / "g4link" / "new"), str(repo))
    assert o1.prepare_outdir(str(link / "out" / "exploratory" / "o1-via-link"), str(link))


# ---- security: run_params binds the producer and the thresholds -------------------------------------------------

@pytest.mark.parametrize("field,val", [("producer", "other-producer"), ("max_mismatch_frac", 0.5), ("max_reverify_frac", 0.5), ("reverify_fraction", 0.5)])
def test_resume_refuses_any_change_of_producer_or_thresholds(tmp_path, field, val):
    o1.run_extraction(mk_cfg(tmp_path), FakeChain())
    p = json.load(open(tmp_path / "run_params.json"))
    assert p["producer"] == "test-producer" and p["max_mismatch_frac"] == 0.005 and p["max_reverify_frac"] == 0.01 and p["reverify_seed"] == 808
    with pytest.raises(o1.Abort, match="run_params.json differs"):
        o1.run_extraction(mk_cfg(tmp_path, **{field: val}), FakeChain())


def test_default_producer_identity_hashes_the_four_source_files(tmp_path):
    cfg = mk_cfg(tmp_path); cfg.pop("producer")
    o1.run_extraction(cfg, FakeChain())
    prod = json.load(open(tmp_path / "run_params.json"))["producer"]
    assert prod == rpc.producer_identity(o1.PRODUCER_FILES) and ":" in prod
    assert [os.path.basename(f) for f in o1.PRODUCER_FILES] == ["o1_extract.py", "g4_controls.py", "g1_cohort.py", "rpc.py", "keccak.py"] and all(os.path.isfile(f) for f in o1.PRODUCER_FILES)


# ---- security: checkpoint integrity -----------------------------------------------------------------------------

def test_checkpoint_record_hash_is_verified_on_load(tmp_path):
    p = str(tmp_path / "c.jsonl")
    ck = o1.Ckpt(p); ck.put("a", {"x": 1}); ck.put("b", {"x": 2}); ck.close()
    rec = json.loads(open(p).readline())
    assert rec["h"] == hashlib.sha256(json.dumps(rec["v"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    lines = open(p).read().split("\n")
    open(p, "w").write(lines[0].replace('"x": 1', '"x": 9') + "\n" + lines[1] + "\n")
    with pytest.raises(RuntimeError, match="hash mismatch"):
        o1.Ckpt(p)
    d = json.loads(lines[0]); d.pop("h")                                   # a record without a hash is not accepted either
    open(p, "w").write(json.dumps(d) + "\n")
    with pytest.raises(RuntimeError):
        o1.Ckpt(p)


@pytest.mark.parametrize("bad", [-1, True, "5", 1.5, None])
def test_checkpoint_balance_values_must_be_nonnegative_ints(tmp_path, bad):
    universe = sorted(mk_cfg(tmp_path)["arms"])
    ck = o1.Ckpt(str(tmp_path / "c.jsonl"))
    ck.put("open:USDC:0", {"ep": "mevblocker", "vals": {a: (bad if a == universe[0] else 1) for a in universe}})   # valid hash, wrong type
    with pytest.raises(RuntimeError, match="non-negative int"):
        o1.collect(ck, universe, PIN, DAY0)
    with pytest.raises(RuntimeError, match="non-negative int"):
        o1.acquire_openings(FakeChain(), ck, o1.Receipts(str(tmp_path / "r.jsonl")), universe, PIN, DAY0, "mevblocker", "tenderly")
    ck.close()


def test_checkpoint_unit_record_is_type_checked(tmp_path):
    universe = sorted(mk_cfg(tmp_path)["arms"])
    ck = o1.Ckpt(str(tmp_path / "c.jsonl"))
    for coin in o1.COINS:
        ck.put(f"open:{coin}:0", {"ep": "mevblocker", "vals": {a: 1 for a in universe}})
    for c in o1.COINS:
        for pos in ("from", "to"):
            ck.put(f"unit:{c}:{pos}:0", {"ok": True, "ep": "mevblocker", "ranges": [[PIN + 1, DAY0, 0, "v2:x", "mevblocker"]], "logs": 0, "wallets": {}})
    ck.put("destroyed", {"ok": True, "ep": "mevblocker", "ranges": [[PIN + 1, DAY0, 0, "v2:x", "mevblocker"]], "logs": 0,
                         "wallets": {AA1: [[[-5, "7"]], 1, "v2:y"]}})          # delta as a string
    with pytest.raises(RuntimeError, match="malformed"):
        o1.collect(ck, universe, PIN, DAY0)
    ck.close()


def test_torn_final_line_is_dropped_and_a_complete_unterminated_tail_is_kept(tmp_path):
    p = str(tmp_path / "c.jsonl")
    ck = o1.Ckpt(p); ck.put("a", {"x": 1}); ck.put("b", {"x": 2}); ck.close()
    good = open(p, "rb").read()
    open(p, "ab").write(b'{"k": "c", "v": {"x"')                           # torn write, no newline
    ck = o1.Ckpt(p)
    assert set(ck.data) == {"a", "b"} and open(p, "rb").read() == good       # truncated back to the last complete line
    ck.put("c", {"x": 3}); ck.close()
    assert set(o1.Ckpt(p).data) == {"a", "b", "c"}
    # a COMPLETE record that merely lost its newline: kept, newline restored BEFORE the next append (no glued lines)
    p2 = str(tmp_path / "c2.jsonl")
    ck = o1.Ckpt(p2); ck.put("a", {"x": 1}); ck.put("b", {"x": 2}); ck.close()
    raw = open(p2, "rb").read(); assert raw.endswith(b"\n")
    open(p2, "wb").write(raw[:-1])
    ck = o1.Ckpt(p2); assert set(ck.data) == {"a", "b"}
    ck.put("c", {"x": 3}); ck.close()
    ck = o1.Ckpt(p2); assert set(ck.data) == {"a", "b", "c"}; ck.close()
    assert all(l.endswith(b"}") for l in open(p2, "rb").read().split(b"\n")[:-1])
    # a newline-terminated corrupt line is corruption, not a torn tail
    p3 = str(tmp_path / "c3.jsonl")
    open(p3, "wb").write(good + b"garbage\n")
    with pytest.raises(RuntimeError, match="corrupt line"):
        o1.Ckpt(p3)


# ---- security: atomic writes, O_NOFOLLOW, symlinks --------------------------------------------------------------

def test_atomic_text_keeps_the_old_file_on_failure_and_leaves_no_temp(tmp_path):
    f = tmp_path / "out.csv"; f.write_text("old")
    with pytest.raises(ZeroDivisionError):
        with o1.atomic_text(str(f)) as h:
            h.write("half-written"); 1 / 0
    assert f.read_text() == "old" and os.listdir(tmp_path) == ["out.csv"]
    with o1.atomic_text(str(f)) as h:
        h.write("new")
    assert f.read_text() == "new" and os.listdir(tmp_path) == ["out.csv"]


def test_outputs_replace_symlinks_rather_than_writing_through_them(tmp_path):
    victim = tmp_path / "victim.txt"; victim.write_text("keep")
    os.symlink(str(victim), str(tmp_path / "o.csv"))
    with o1.atomic_text(str(tmp_path / "o.csv")) as h:
        h.write("x")
    assert victim.read_text() == "keep" and not os.path.islink(tmp_path / "o.csv")
    os.symlink(str(victim), str(tmp_path / "ck.jsonl"))
    with pytest.raises(OSError):
        o1.Ckpt(str(tmp_path / "ck.jsonl"))
    os.symlink(str(victim), str(tmp_path / "rc.jsonl"))
    with pytest.raises(OSError):
        o1.Receipts(str(tmp_path / "rc.jsonl")).add({"kind": "x"})
    assert victim.read_text() == "keep"


def test_resume_refuses_symlink_entries_in_the_outdir(tmp_path):
    o1.run_extraction(mk_cfg(tmp_path), FakeChain())
    os.symlink("/etc/hostname", str(tmp_path / "evil"))
    with pytest.raises(o1.Abort, match="symlink"):
        o1.run_extraction(mk_cfg(tmp_path), FakeChain())
    repo = tmp_path.parent / (tmp_path.name + "_repo"); (repo / "out" / "exploratory").mkdir(parents=True)
    d = repo / "out" / "exploratory" / "o1-r"; d.mkdir(); (d / "run_params.json").write_text("{}"); os.symlink("/etc/hostname", str(d / "x"))
    with pytest.raises(o1.Abort, match="symlink"):
        o1.prepare_outdir(str(d), str(repo), resume=True)


# ---- receipts for every block read ------------------------------------------------------------------------------

def test_every_block_read_is_a_receipt_and_header_reads_are_timestamp_only(tmp_path):
    o1.run_extraction(mk_cfg(tmp_path), FakeChain())
    recs = [json.loads(l) for l in open(tmp_path / "receipts.jsonl")]
    hdr = [r for r in recs if r["kind"] == "block_header"]
    assert sorted(r["blockNumber"] for r in hdr) == [PIN, PIN + 1, DAY0, DAY0 + 1] and all(r["timestamp_only"] is True for r in hdr)
    assert o1.assert_receipts(str(tmp_path / "receipts.jsonl"), PIN, DAY0) == len(recs)
    for bad in ({"kind": "block_header", "blockNumber": DAY0 + 2, "timestamp_only": True}, {"kind": "block_header", "blockNumber": DAY0 + 1},
                {"kind": "getLogs", "toBlock": DAY0 + 1}, {"kind": "balanceOf", "block": DAY0 + 1}, {"kind": "balanceOf"}):
        q = tmp_path / "bad.jsonl"; q.write_text(json.dumps(bad) + "\n")
        with pytest.raises(g4.PostPeriodRead):
            o1.assert_receipts(str(q), PIN, DAY0)


# ---- strict log fields ------------------------------------------------------------------------------------------

def _log(**kw):
    l = {"blockNumber": "0x10", "blockTimestamp": "0x20", "logIndex": "0x1", "blockHash": "0x" + "ab" * 32}
    l.update(kw)
    return l


def test_strict_hex_removed_and_one_blockhash_per_block():
    o1.check_log_fields([_log(), _log(logIndex="0x2")])
    for k, v in (("blockNumber", "16"), ("blockNumber", "0x"), ("blockNumber", " 0x10"), ("blockNumber", "0x1_0"), ("blockNumber", "-0x10"),
                 ("blockTimestamp", "0x2g"), ("blockTimestamp", 32), ("logIndex", "1"), ("logIndex", None), ("blockHash", "0x12"), ("blockHash", None)):
        with pytest.raises(AssertionError):
            o1.check_log_fields([_log(**{k: v})])
    with pytest.raises(AssertionError, match="removed"):
        o1.check_log_fields([_log(removed=True)])
    o1.check_log_fields([_log(removed=False)])
    with pytest.raises(AssertionError, match="two different blockHashes"):
        o1.check_log_fields([_log(), _log(logIndex="0x2", blockHash="0x" + "cd" * 32)])
    with pytest.raises(AssertionError):
        o1._hexint("10")
    assert o1._hexint("0xAbC") == 0xabc


def test_removed_or_malformed_log_from_an_endpoint_aborts_the_run(tmp_path):
    def removed(logs):
        return [dict(logs[0], removed=True)] + logs[1:] if logs else logs
    with pytest.raises(AssertionError, match="removed"):
        o1.run_extraction(mk_cfg(tmp_path), FakeChain(tamper={"mevblocker": removed}))


def test_symlink_free_outdir_is_required_for_a_new_run_too(tmp_path):
    os.symlink("/etc/hostname", str(tmp_path / "pre"))
    with pytest.raises(o1.Abort, match="symlink"):
        o1.run_extraction(mk_cfg(tmp_path), FakeChain())


# ---- S33 lows -----------------------------------------------------------------------------------------------------

def test_strict_regexes_reject_trailing_newline():
    with pytest.raises(AssertionError):
        o1._hexint("0x1f\n")
    assert not o1._ADDR.fullmatch("0x" + "a" * 40 + "\n") and o1._ADDR.fullmatch("0x" + "a" * 40)
    assert not o1._HASH.fullmatch("0x" + "a" * 64 + "\n")


def test_run_wide_block_hash_map(monkeypatch):
    def lg(h):
        return {"blockNumber": "0x5", "blockTimestamp": "0x1", "logIndex": "0x0", "blockHash": "0x" + h * 64}
    monkeypatch.setattr(o1, "_RUN_BLOCKS", {})
    o1.check_log_fields([lg("a")])
    with pytest.raises(AssertionError, match="across units"):
        o1.check_log_fields([lg("b")])


def test_cached_record_type_checks():
    with pytest.raises(RuntimeError):
        o1._check_blocks_rec({"pin": 1})
    o1._check_blocks_rec({"pin": 1, "pin+1": 2, "day0": 3, "day0+1": 4})
    good = {"sample_n": 1, "log_mismatches": [], "balance_mismatches": [], "path_mismatches": []}
    o1._check_rv_rec(good, ["0xa"])
    for bad in ({**good, "sample_n": 2}, {**good, "log_mismatches": [{"addr": "0xother"}]}, {**good, "path_mismatches": "x"}, []):
        with pytest.raises(RuntimeError):
            o1._check_rv_rec(bad, ["0xa"])
    with pytest.raises(RuntimeError):
        o1._check_balances([1, -1], ["a", "b"])
    o1._check_balances([1, 0], ["a", "b"])


def test_producer_files_include_keccak_and_dirty_check(tmp_path):
    import subprocess
    assert any(f.endswith(os.path.join("lib", "keccak.py")) for f in o1.PRODUCER_FILES)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    f = tmp_path / "p.py"; f.write_text("x")
    with pytest.raises(o1.Abort, match="uncommitted"):
        o1.assert_producers_clean([str(f)], str(tmp_path))
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "add", "p.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"], check=True)
    o1.assert_producers_clean([str(f)], str(tmp_path))


# ---- resume tolerates HEAD moves that touch no producer file --------------------------------------------------------

def _git(repo, *a):
    import subprocess
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True,
                          capture_output=True, text=True).stdout.strip()


def _producer_repo(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "p.py").write_text("x"); (repo / "other.txt").write_text("a")
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "c1")
    return repo, [str(repo / "p.py")]


def test_resume_accepts_head_move_with_only_non_producer_changes(tmp_path):
    repo, files = _producer_repo(tmp_path)
    h1 = _git(repo, "rev-parse", "HEAD")
    (repo / "other.txt").write_text("b"); _git(repo, "commit", "-qam", "c2")
    h2 = _git(repo, "rev-parse", "HEAD")
    assert h1 != h2
    assert o1.check_resume_producer(f"{h1}:{'a' * 16}", f"{h2}:{'a' * 16}", files, str(repo)) is True
    assert o1.check_resume_producer(f"{h2}:{'a' * 16}", f"{h2}:{'a' * 16}", files, str(repo)) is False


def test_resume_refuses_producer_file_changed_between_commits(tmp_path):
    repo, files = _producer_repo(tmp_path)
    h1 = _git(repo, "rev-parse", "HEAD")
    (repo / "p.py").write_text("y"); _git(repo, "commit", "-qam", "c2")
    h2 = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(o1.Abort, match="producer files changed"):      # even if a (forged) equal file hash is claimed
        o1.check_resume_producer(f"{h1}:{'a' * 16}", f"{h2}:{'a' * 16}", files, str(repo))
    with pytest.raises(o1.Abort, match="differs"):                     # honest case: file hash half differs
        o1.check_resume_producer(f"{h1}:{'a' * 16}", f"{h2}:{'b' * 16}", files, str(repo))


def test_resume_refuses_unknown_stored_head(tmp_path):
    repo, files = _producer_repo(tmp_path)
    h = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(o1.Abort, match="unknown"):
        o1.check_resume_producer(f"{'1' * 40}:{'a' * 16}", f"{h}:{'a' * 16}", files, str(repo))


def test_resume_refuses_stored_head_not_an_ancestor(tmp_path):
    repo, files = _producer_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "side"); (repo / "other.txt").write_text("s"); _git(repo, "commit", "-qam", "s")
    side = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-"); (repo / "other.txt").write_text("m"); _git(repo, "commit", "-qam", "m")
    h = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(o1.Abort, match="not an ancestor"):
        o1.check_resume_producer(f"{side}:{'a' * 16}", f"{h}:{'a' * 16}", files, str(repo))


def test_run_extraction_resume_logs_head_move_and_keeps_run_params(tmp_path, monkeypatch):
    out = tmp_path / "out"; out.mkdir()
    o1.run_extraction(mk_cfg(out, producer=f"{'1' * 40}:{'a' * 16}"), FakeChain())
    before = (out / "run_params.json").read_bytes()
    cur = f"{'2' * 40}:{'a' * 16}"
    monkeypatch.setattr(o1, "check_resume_producer", lambda s, c, f, r: True)
    o1.run_extraction(mk_cfg(out, producer=cur), FakeChain())
    assert (out / "run_params.json").read_bytes() == before
    log = [json.loads(l) for l in open(out / "resume_log.jsonl")]
    assert log[0]["stored_producer"].startswith("1" * 40) and log[0]["resuming_producer"] == cur


# ---- 429 backoff around g4._batch_post (LiveChain.balance_of) ----------------------------------------------------

def _rl(): return RuntimeError("tenderly batch of 250 failed after 8 retries: HTTP Error 429: Too Many Requests")


def _flaky(n, exc):
    st = {"n": 0}
    def fn():
        st["n"] += 1
        if st["n"] <= n:
            raise exc
        return "ok"
    return fn, st


def test_quota_backoff_waits_long_and_exponentially_then_succeeds():
    sleeps = []
    fn, st = _flaky(3, _rl())
    assert o1.with_quota_backoff(fn, sleeps.append) == "ok" and st["n"] == 4
    assert len(sleeps) == 3 and all(22 <= s for s in sleeps) and sleeps[2] > sleeps[0]


def test_quota_backoff_gives_up_after_bounded_total_and_reraises():
    sleeps = []
    fn, st = _flaky(10 ** 6, _rl())
    with pytest.raises(RuntimeError, match="429"):
        o1.with_quota_backoff(fn, sleeps.append)
    assert 300 < sum(sleeps) <= o1.QUOTA_BACKOFF_TOTAL_S


def test_quota_backoff_leaves_other_errors_alone():
    sleeps = []
    fn, st = _flaky(10, RuntimeError("tenderly batch of 250 failed after 8 retries: HTTP Error 500"))
    with pytest.raises(RuntimeError, match="500"):
        o1.with_quota_backoff(fn, sleeps.append)
    assert sleeps == [] and st["n"] == 1


def test_balance_of_retries_same_endpoint_on_429(monkeypatch):
    seen = []
    def fake_post(ep, calls, validate=None):
        seen.append(ep)
        if len(seen) < 3:
            raise _rl()
        return ["0x" + "0" * 63 + "5"] * len(calls)
    monkeypatch.setattr(o1.g4, "_batch_post", fake_post)
    monkeypatch.setattr(o1.time, "sleep", lambda s: None)
    assert o1.LiveChain().balance_of("tenderly", "USDC", ["0x" + "1" * 40], 5) == [5]
    assert seen == ["tenderly"] * 3


# ---- S33: --verify-endpoint, transport-error backoff, reverify resume ----------------------------------------------

def _run_main(monkeypatch, tmp_path, extra):
    seen = {}
    monkeypatch.setattr(o1, "assert_producers_clean", lambda *a, **k: None)
    monkeypatch.setattr(o1, "prepare_outdir", lambda out, root, resume: str(tmp_path))
    monkeypatch.setattr(o1, "run_extraction", lambda cfg, chain: seen.update(cfg) or {"counts_by_arm": {}})
    argv = ["run", "--membership", os.path.join(FIX, "o1_membership.csv"), "--signed-cov", os.path.join(FIX, "o1_signed_cov.csv"),
            "--signed-panel", os.path.join(FIX, "o1_signed_panel.csv"), "--pin-block", str(PIN), "--day0-block", str(DAY0),
            "--out", str(tmp_path)] + extra
    return seen, o1.main(argv)


def test_verify_endpoint_option_default_override_and_validation(monkeypatch, tmp_path):
    seen, _ = _run_main(monkeypatch, tmp_path, ["--walk-endpoint", "mevblocker"])
    assert seen["verify_ep"] == g4.REPLAY_ENDPOINT["mevblocker"]                       # default unchanged
    seen, _ = _run_main(monkeypatch, tmp_path, ["--walk-endpoint", "mevblocker", "--verify-endpoint", "drpc"])
    assert seen["verify_ep"] == "drpc" and seen["walk_ep"] == "mevblocker"
    with pytest.raises(o1.Abort, match="must differ"):
        _run_main(monkeypatch, tmp_path, ["--walk-endpoint", "mevblocker", "--verify-endpoint", "mevblocker"])
    with pytest.raises(SystemExit):                                                      # not a known endpoint
        _run_main(monkeypatch, tmp_path, ["--verify-endpoint", "nowhere"])


def test_transport_errors_are_retried_under_the_bounded_backoff():
    import ssl
    import urllib.error
    ssl_msg = "https://gateway.tenderly.co batch of 3 failed after 5 retries: <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] Hostname mismatch>"
    for first in (RuntimeError(ssl_msg), urllib.error.URLError("boom"), ssl.SSLCertVerificationError("Hostname mismatch")):
        calls, waits = [], []

        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise first
            return "ok"
        assert o1.with_quota_backoff(fn, sleep=waits.append, rng=lambda a, b: 1.0) == "ok"
        assert len(calls) == 3 and len(waits) == 2
    calls, waits = [], []                                                                # never recovers -> bounded, then raises

    def always():
        calls.append(1)
        raise RuntimeError(ssl_msg)
    with pytest.raises(RuntimeError, match="CERTIFICATE_VERIFY_FAILED"):
        o1.with_quota_backoff(always, sleep=waits.append, rng=lambda a, b: 1.0)
    assert sum(waits) <= o1.QUOTA_BACKOFF_TOTAL_S


@pytest.mark.parametrize("err", [RuntimeError("batch id=1 -> {'code': -32000, 'message': 'execution reverted'}"),
                                 RuntimeError("batch reply missing 2 of 5 ids"), ValueError("bad word"),
                                 __import__("urllib.error").error.HTTPError("u", 500, "x", {}, None)])
def test_data_validation_and_http_errors_are_never_retried(err):
    calls = []

    def fn():
        calls.append(1)
        raise err
    with pytest.raises(type(err)):
        o1.with_quota_backoff(fn, sleep=lambda s: calls.append("slept"))
    assert calls == [1]


def test_reverify_resumes_from_partial_checkpoint(tmp_path):
    fail = lambda ep, coin, addrs, block: ep == "tenderly" and block == DAY0   # noqa: E731  last reverify reads die
    with pytest.raises(RuntimeError, match="archive read failed"):
        o1.run_extraction(mk_cfg(tmp_path), FakeChain(bal_fail=fail))
    ck = o1.Ckpt(str(tmp_path / "checkpoint.jsonl"))
    units = [k for k in ck.data if k.startswith("rvu:")]
    assert units and not [k for k in ck.data if k.startswith("reverify:")]             # partial progress, no final record
    ck.close()
    ch = FakeChain()
    s = o1.run_extraction(mk_cfg(tmp_path), ch)
    assert not [c for c in ch.log_calls if c[0] == "tenderly"]                         # logs came from the checkpoint
    assert not [c for c in ch.bal_calls if c[0] == "tenderly" and c[1] == "USDC" and c[3] == PIN]   # cached before the failure
    assert s["counts_by_arm"] == {"control": {"ok": 2}, "exposed": {"ok": 2}}
    (tmp_path / "ref").mkdir()
    ref = o1.run_extraction(mk_cfg(tmp_path / "ref"), FakeChain())
    assert s["reverify"] == ref["reverify"]                                            # same verdict as an uninterrupted run


def test_balance_of_splits_and_paces_tenderly_but_not_other_endpoints(monkeypatch):
    sizes, sleeps = [], []

    def post(endpoint, calls, validate=None):
        sizes.append(len(calls))
        return [hex(int(c[1][0]["data"][-8:], 16)) for c in calls]

    monkeypatch.setattr(o1.g4, "_batch_post", post)
    monkeypatch.setattr(o1.time, "sleep", sleeps.append)
    addrs = ["0x" + f"{i:040x}" for i in range(1, 13)]
    assert o1.LiveChain().balance_of("tenderly", "USDC", addrs, 100) == list(range(1, 13))
    assert sizes == [5, 5, 2] and sleeps == [1.0, 1.0]
    sizes.clear(), sleeps.clear()
    assert o1.LiveChain().balance_of("mevblocker", "USDC", addrs, 100) == list(range(1, 13))
    assert sizes == [12] and sleeps == []
    assert o1.LiveChain().balance_of("tenderly", "USDC", [], 100) == []
