"""S22 prep: gates/presign_side_files.py -- the four presign side files. Synthetic fixtures only; no network, no out/."""
import csv
import hashlib
import json
import os
import shutil
import subprocess
from collections import defaultdict

import pytest

import run_all
from gates import g0_baseline_blacklist as g0b
from gates import g1b_frame, presign_side_files as psf
from lib import blocks

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START, END = psf.PRE_START_BLOCK, psf.PRE_END_BLOCK
needs_r = pytest.mark.skipif(shutil.which("Rscript") is None, reason="Rscript not installed")


def _sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def a(i):
    return "0x%040x" % i


def write_circle(path, n_freeze=38, extra=True):
    rows = [(a(800000 + i), "Blacklisted", 15302600 + i, "2022-08-08", "0x%064x" % i, i) for i in range(n_freeze)]
    if extra:  # rows that must NOT enter the freeze set: other date, other event, same-day unblacklist
        rows += [(a(810000), "Blacklisted", 15200000, "2022-08-07", "0x%064x" % 900, 0),
                 (a(810001), "Blacklisted", 15400000, "2022-08-09", "0x%064x" % 901, 0),
                 (a(810002), "UnBlacklisted", 15302700, "2022-08-08", "0x%064x" % 902, 0)]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(psf.CIRCLE_COLS)
        w.writerows(rows)


def write_inputs(root, cohort_arms=None, survivors=None, circle_n=38):
    """root/{g0,g1,g1b}: circle_events, cohort (5 exposed), frame_survivors (7 + 1 mixed-case duplicate-free)."""
    root = str(root)
    write_circle(os.path.join(root, "g0", "circle_events.csv"), circle_n)
    os.makedirs(os.path.join(root, "g1"), exist_ok=True)
    os.makedirs(os.path.join(root, "g1b"), exist_ok=True)
    cohort_arms = cohort_arms or {a(1): "withdrawal", a(2): "deposit", a(3): "both", a(4): "withdrawal", a(5): "deposit"}
    with open(os.path.join(root, "g1", "cohort.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["wallet_id", "address", "arm", "floor_pass"])
        for i, (ad, arm) in enumerate(sorted(cohort_arms.items())):
            w.writerow([i, ad, arm, str(i % 2 == 0)])
    survivors = survivors if survivors is not None else [a(100 + i) for i in range(7)]
    with open(os.path.join(root, "g1b", "frame_survivors.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["wallet_id", "address", "windows_seen"])
        for i, ad in enumerate(survivors):
            w.writerow([i, ad.upper().replace("0X", "0x") if i == 0 else ad, 1])
    return root


def write_baseline(d):
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "manifest.json"), "w").write('{"fixture": true}')
    open(os.path.join(d, "events.csv"), "w").write("coin,address,event,block,tx_hash,log_index\n")
    return str(d)


def fake_baseline(events):
    """events: [(coin, address, kind, block, log_index)] -> g0_baseline_blacklist.Baseline with those verified events."""
    ev = {c: defaultdict(list) for c in psf.COINS}
    for coin, ad, kind, b, li in events:
        ev[coin][ad].append((b, li, kind))
    return lambda d, consumer, pinned=None: g0b.Baseline(ev, END, consumer, _sha(os.path.join(d, "manifest.json")))


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = write_inputs(tmp_path / "in")
    base = write_baseline(tmp_path / "base")
    monkeypatch.setattr(psf, "_producer_dirty", lambda: [])
    monkeypatch.setattr(psf, "OUT_ROOT", str(tmp_path))   # test-only relocation of the allowed output root
    _receipts(os.path.join(root, "g1b", "receipts.jsonl"), g1b_frame.original_windows(blocks.load_pinned()))
    monkeypatch.setattr(psf.g0_baseline_blacklist, "load_baseline", fake_baseline([
        ("USDC", a(10), "add", START - 5000, 0),                                           # on at start -> flagged
        ("USDT", a(11), "add", START + 10, 3), ("USDT", a(11), "remove", START + 20, 1),   # added inside, removed -> flagged
        ("USDC", a(12), "add", START - 9, 0), ("USDC", a(12), "remove", START - 8, 0),     # off before start -> not flagged
    ]))
    return tmp_path, root, base


def test_direct_freeze_exactly_38_of_the_date_and_event(tmp_path):
    p = str(tmp_path / "c.csv")
    write_circle(p)
    got = psf.direct_freeze(p)
    assert got == sorted(a(800000 + i) for i in range(38))
    for n in (37, 39):
        write_circle(p, n_freeze=n, extra=False)
        with pytest.raises(psf.SideFileError, match="exactly 38"):
            psf.direct_freeze(p)


def _poison_circle(p, poison_rows):
    write_circle(str(p))
    with open(str(p), "a") as f:
        f.write("".join(poison_rows))


BOUND = 15302766   # config/blocks.json circle_cluster_end


def test_direct_freeze_never_parses_rows_past_cluster_end(tmp_path):
    p = tmp_path / "c.csv"
    # every field after `block` is garbage / would raise if parsed (bad address on a FREEZE_DATE Blacklisted row, no date, stray quote)
    _poison_circle(p, ["0xNOTHEX,Blacklisted,%d,2022-08-08,zz,q\n" % (BOUND + 1),
                       "0xNOTHEX,Blacklisted,99999999,\"unterminated,\"\"\n",
                       "x,y,%d,\n" % (BOUND + 5) + ""])
    assert psf.direct_freeze(str(p)) == sorted(a(800000 + i) for i in range(38))
    # a row AT/below the bound IS parsed (control: same poison inside the bound raises)
    _poison_circle(p, ["0xNOTHEX,Blacklisted,%d,2022-08-08,zz,q\n" % BOUND])
    with pytest.raises(psf.SideFileError, match="not a 0x"):
        psf.direct_freeze(str(p))


EVENT_BLOCK = 15302549   # config/blocks.json event_block


def test_direct_freeze_skips_gap_rows_before_event_block_on_block_alone(tmp_path):
    p = tmp_path / "c.csv"
    st = {}
    # (pre_period_end, event_block) gap: poison that would raise if parsed, incl. a quote in the address column
    _poison_circle(p, ["0xNOTHEX,Blacklisted,%d,2022-08-08,zz,q\n" % (psf.PRE_END_BLOCK + 1),
                       "\"bad,Blacklisted,%d,2022-08-08,zz,q\n" % (EVENT_BLOCK - 1)])
    assert psf.direct_freeze(str(p), stats=st) == sorted(a(800000 + i) for i in range(38))
    assert st["direct_freeze_block_window"] == [EVENT_BLOCK, BOUND] and st["rows_skipped_out_of_window"] == 2 + 2
    # control: the same poison AT event_block is in window and raises
    _poison_circle(p, ["0xNOTHEX,Blacklisted,%d,2022-08-08,zz,q\n" % EVENT_BLOCK])
    with pytest.raises(psf.SideFileError, match="not a 0x"):
        psf.direct_freeze(str(p))


def test_direct_freeze_block_checked_before_shape_and_errors_do_not_echo_row(tmp_path):
    p = tmp_path / "c.csv"
    # out-of-window row with a quote in address AND event: skipped, not a malformed-row error
    _poison_circle(p, ['"a,"b,%d,SECRETDATE,x,y\n' % (BOUND + 1)])
    psf.direct_freeze(str(p))
    # a quote in ONLY the address, or ONLY the event, of a post-bound / pre-event_block row is skipped, never raised
    _poison_circle(p, ['"a,Blacklisted,%d,2022-08-08,x,y\n' % (BOUND + 1), 'a,"Blacklisted,%d,2022-08-08,x,y\n' % (EVENT_BLOCK - 1)])
    psf.direct_freeze(str(p))
    # unicode digits / signs / spaces are not ASCII digits
    for tok in ("\u0663\u0664", "-5", "+5", " 5", "5.0", ""):
        _poison_circle(p, ["SECRETADDR,SECRETEV,%s,SECRETDATE,x,y\n" % tok])
        with pytest.raises(psf.SideFileError, match="non-integer block") as e:
            psf.direct_freeze(str(p))
        assert "SECRET" not in str(e.value)
    _poison_circle(p, ["SECRETADDR,SECRETEV\n"])
    with pytest.raises(psf.SideFileError, match="malformed row") as e:
        psf.direct_freeze(str(p))
    assert "SECRET" not in str(e.value)
    # in-window quote is still malformed (fail closed)
    _poison_circle(p, ['"a,"b,%d,2022-08-08,x,y\n' % EVENT_BLOCK])
    with pytest.raises(psf.SideFileError, match="malformed in-window"):
        psf.direct_freeze(str(p))


def test_direct_freeze_long_block_token_refused_before_int_and_not_echoed(tmp_path):
    p = tmp_path / "c.csv"
    for tok in ("9" * 20, "9" * 5000):
        _poison_circle(p, ["%s,Blacklisted,%s,2022-08-08,x,y\n" % (a(1), tok)])
        with pytest.raises(psf.SideFileError, match="too long") as e:
            psf.direct_freeze(str(p))
        assert tok[:10] not in str(e.value)
    _poison_circle(p, ["%s,Blacklisted,%s,2022-08-08,x,y\n" % (a(1), "9" * 19)])   # 19 digits is parsed (then out of window)
    psf.direct_freeze(str(p))
    _poison_circle(p, ["%s,Blacklisted,TOKEN123,2022-08-08,x,y\n" % a(1)])
    with pytest.raises(psf.SideFileError, match="non-integer block") as e:
        psf.direct_freeze(str(p))
    assert "TOKEN123" not in str(e.value)


def test_run_converts_unexpected_exceptions_to_rc1_without_traceback(env, monkeypatch, capsys):
    tmp_path, root, base = env
    monkeypatch.setattr(psf, "produce", lambda *a_, **k: (_ for _ in ()).throw(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "SECRETROW")))
    rc = psf.run({"input_root": root, "baseline_blacklist_dir": base, "output_dir": str(tmp_path / "o")})
    err = capsys.readouterr().err
    assert rc == 1 and "FAILED" in err and "SECRET" not in err and "Traceback" not in err


def test_produce_pins_circle_cluster_end_and_attests_window(env):
    tmp_path, root, base = env
    pinned = blocks.load_pinned()
    for bad in (BOUND + 1, BOUND - 1):
        cfg = {**pinned, "circle_cluster_end": {**pinned["circle_cluster_end"], "block": bad}}
        with pytest.raises(psf.SideFileError, match="circle_cluster_end"):
            psf.produce(root, base, str(tmp_path / f"o{bad}"), pinned=cfg)
    for bad in (EVENT_BLOCK + 1, EVENT_BLOCK - 1):
        cfg = {**pinned, "event_block": {**pinned["event_block"], "block": bad}}
        with pytest.raises(psf.SideFileError, match="event_block"):
            psf.produce(root, base, str(tmp_path / f"e{bad}"), pinned=cfg)
    man = psf.produce(root, base, str(tmp_path / "ok"))
    assert man["direct_freeze_block_window"] == [EVENT_BLOCK, BOUND]
    assert man["rows_skipped_out_of_window"] == 2 and man["max_block_parsed"] == 15302700


def test_direct_freeze_unordered_file_and_bad_block_fail_closed(tmp_path):
    p = tmp_path / "c.csv"
    # block column is not an integer (cannot be tested against the cutoff) -> fail closed, never silently skipped
    _poison_circle(p, ["%s,Blacklisted,notanumber,2022-08-08,0x1,0\n" % a(1)])
    with pytest.raises(psf.SideFileError, match="non-integer block"):
        psf.direct_freeze(str(p))
    _poison_circle(p, ["short,row\n"])
    with pytest.raises(psf.SideFileError, match="malformed row"):
        psf.direct_freeze(str(p))
    # unordered blocks are expected (g0 sorts by tx_hash): a post-bound row BEFORE in-bound rows must not change the result
    write_circle(str(p))
    lines = p.read_text().splitlines(keepends=True)
    p.write_text(lines[0] + "".join(reversed(lines[1:])))
    assert psf.direct_freeze(str(p)) == sorted(a(800000 + i) for i in range(38))


def test_direct_freeze_rejects_bad_header_and_address(tmp_path):
    p = tmp_path / "c.csv"
    p.write_text("address,event\n")
    with pytest.raises(psf.SideFileError, match="header"):
        psf.direct_freeze(str(p))
    write_circle(str(p))
    p.write_text(p.read_text().replace(a(800000), "0xnothex"))
    with pytest.raises(psf.SideFileError, match="not a 0x"):
        psf.direct_freeze(str(p))


EVENT_FIXTURE = [
    # coin, address, event (R names), block, log_index
    ("USDC", a(1), "Blacklisted", START - 100, 0),                                         # opening state on -> flagged
    ("USDC", a(2), "Blacklisted", START - 100, 0), ("USDC", a(2), "UnBlacklisted", START - 50, 0),   # off at start -> not
    ("USDC", a(3), "Blacklisted", START + 5, 0), ("USDC", a(3), "UnBlacklisted", START + 6, 0),      # add inside, removed -> flagged
    ("USDC", a(4), "Blacklisted", START, 2), ("USDC", a(4), "UnBlacklisted", START, 9),    # add+remove AT start: R `>=` -> flagged
    ("USDC", a(5), "Blacklisted", START, 9), ("USDC", a(5), "UnBlacklisted", START, 2),    # remove then add at start -> on at start
    ("USDC", a(6), "Blacklisted", END, 0),                                                 # add exactly at end -> flagged
    ("USDC", a(7), "Blacklisted", END + 1, 0),                                             # after end -> never used
    ("USDC", a(8), "Blacklisted", START - 10, 0), ("USDC", a(8), "UnBlacklisted", START - 10, 1), ("USDC", a(8), "Blacklisted", START + 40, 0),  # re-add
    ("USDT", a(1), "AddedBlackList", START - 100, 0), ("USDT", a(1), "RemovedBlackList", START - 90, 0),  # USDT off; USDC on -> flagged once
    ("USDT", a(9), "AddedBlackList", START - 3, 0),                                        # USDT opening on
    ("USDT", a(10), "AddedBlackList", START + 1, 0), ("USDT", a(10), "RemovedBlackList", END + 5, 0),  # removal after end ignored
    ("USDT", a(11), "RemovedBlackList", START - 3, 0),                                     # remove with no add
    ("USDT", a(2), "AddedBlackList", START - 1, 0),                                        # coins independent: USDC off, USDT on -> flagged
    ("USDT", a(12), "AddedBlackList", END + 2, 0),
]
KIND = {"Blacklisted": "add", "UnBlacklisted": "remove", "AddedBlackList": "add", "RemovedBlackList": "remove"}


def _py_events():
    ev = {c: defaultdict(list) for c in psf.COINS}
    for coin, ad, e, b, li in EVENT_FIXTURE:
        ev[coin][ad].append((b, li, KIND[e]))
    return ev


def test_blacklist_any_matches_hand_expectation():
    assert psf.blacklist_any(_py_events()) == [a(i) for i in (1, 2, 3, 4, 5, 6, 8, 9, 10)]


@needs_r
def test_blacklist_any_python_equals_r_blacklisted_any_in_window(tmp_path):
    f = tmp_path / "ev.csv"
    with open(f, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["coin", "address", "event", "block", "log_index"])
        w.writerows(EVENT_FIXTURE)
    code = (f'suppressWarnings(library(data.table)); source("R/blacklist.R"); '
            f'x <- blacklisted_any_in_window(fread("{f}")); writeLines(sort(as.vector(x), method = "radix"))')
    r = subprocess.run(["Rscript", "-e", code], cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-1500:]
    r_set = sorted(r.stdout.split())
    assert r_set == psf.blacklist_any(_py_events()) and len(r_set) == 9


def test_candidates_labels_union_and_defects(tmp_path):
    root = write_inputs(tmp_path / "in")
    rows, n_c, n_s, n_o, n_n = psf.candidates(os.path.join(root, "g1", "cohort.csv"), os.path.join(root, "g1b", "frame_survivors.csv"))
    d = dict(rows)
    assert (n_c, n_s, n_o, n_n, len(rows)) == (5, 7, 0, 0, 12) and [r[0] for r in rows] == sorted(d)
    assert d[a(1)] == "g1_withdrawal" and d[a(2)] == "g1_deposit" and d[a(3)] == "g1_both" and d[a(100)] == "g1b_frame"  # upper-case input lowered
    root2 = write_inputs(tmp_path / "in2", survivors=[a(100), a(1)])   # a(1) also a cohort member
    rows2, n_c2, n_s2, n_o2, n_n2 = psf.candidates(os.path.join(root2, "g1", "cohort.csv"), os.path.join(root2, "g1b", "frame_survivors.csv"))
    # a(1) has floor_pass True (fixture: index 0): exposed label wins, as in g4_controls
    assert (n_c2, n_s2, n_o2, n_n2, len(rows2)) == (5, 2, 1, 0, 6) and dict(rows2)[a(1)] == "g1_withdrawal"
    root3 = write_inputs(tmp_path / "in3", survivors=[a(100), a(100)])
    with pytest.raises(psf.SideFileError, match="duplicate"):
        psf.candidates(os.path.join(root3, "g1", "cohort.csv"), os.path.join(root3, "g1b", "frame_survivors.csv"))


def test_candidates_non_floor_cohort_frame_overlap_is_a_control_as_in_g4(tmp_path):
    """g4_controls: exposed = cohort rows with floor_pass == "True"; controls = frame minus exposed. A cohort row without
    floor_pass that is also a frame survivor is therefore a control (g1b_frame), a cohort row outside the frame stays g1_<arm>."""
    root = write_inputs(tmp_path / "in", survivors=[a(100), a(1), a(2), a(3)])   # fixture floor_pass: a(1) True, a(2) False, a(3) True
    rows, n_c, n_s, ov_f, ov_n = psf.candidates(os.path.join(root, "g1", "cohort.csv"), os.path.join(root, "g1b", "frame_survivors.csv"))
    d = dict(rows)
    assert (n_c, n_s, ov_f, ov_n, len(rows)) == (5, 4, 2, 1, 6)
    assert d[a(1)] == "g1_withdrawal" and d[a(3)] == "g1_both"        # floor-passing overlap: exposed label
    assert d[a(2)] == "g1b_frame"                                     # non-floor overlap: control label
    assert d[a(4)] == "g1_withdrawal" and d[a(5)] == "g1_deposit"     # cohort, not in frame: unchanged (a(4) is floor, a(5) is not)


def test_candidates_requires_floor_pass_column(tmp_path):
    root = write_inputs(tmp_path / "in")
    p = os.path.join(root, "g1", "cohort.csv")
    rows = list(csv.reader(open(p)))
    with open(p, "w", newline="") as f:
        csv.writer(f).writerows([[c for k, c in enumerate(r) if k != 3] for r in rows])
    with pytest.raises(psf.SideFileError, match="floor_pass"):
        psf.candidates(p, os.path.join(root, "g1b", "frame_survivors.csv"))


def test_g1b_run_uses_original_windows_helper_not_a_second_draw():
    import inspect
    src = inspect.getsource(g1b_frame.run)
    assert "original_windows(pinned)" in src and "rng.sample" not in src and "random.Random" not in src


def test_original_windows_are_the_g1b_draw_and_inside_range():
    import random
    pinned = blocks.load_pinned()
    pre_start, pre_end = pinned["pre_period_start"]["block"], pinned["pre_period_end"]["block"]
    rng = random.Random(g1b_frame.WINDOW_SEED)  # independent restatement of g1b_frame.run's draw
    starts = sorted(rng.sample(range(pre_start, pre_end - g1b_frame.WINDOW_BLOCKS + 1 + 1), g1b_frame.N_WINDOWS))
    w = g1b_frame.original_windows(pinned)
    assert w == [(s, s + 399) for s in starts] and len(w) == 30
    assert all(START <= s and e <= END and e - s == 399 for s, e in w)


def _receipts(path, windows, step=100, skip=None):
    with open(path, "w") as f:
        f.write(json.dumps({"fromBlock": 9000000, "toBlock": 9999999}) + "\n")   # an unrelated component (exclusion sweep)
        for k, (s, e) in enumerate(windows):
            cur = s
            while cur <= e:
                end = min(cur + step - 1, e)
                if not (skip == k and cur == s + step):
                    f.write(json.dumps({"fromBlock": cur, "toBlock": end}) + "\n")
                cur = end + 1


def test_window_receipts_check(tmp_path):
    w = g1b_frame.original_windows(blocks.load_pinned())
    assert any(w[i + 1][0] <= w[i][1] for i in range(len(w) - 1))   # the real draw has overlapping windows; the check must cope
    p = str(tmp_path / "r.jsonl")
    _receipts(p, w)
    assert psf.check_window_receipts(w, p).startswith("ok")
    _receipts(p, w, step=37)
    assert psf.check_window_receipts(w, p).startswith("ok")
    _receipts(p, w, skip=3)
    with pytest.raises(psf.SideFileError, match="no exact tiling"):
        psf.check_window_receipts(w, p)
    _receipts(p, [(s + 1, e + 1) for s, e in w])   # the fetched windows differ from the recomputed ones
    with pytest.raises(psf.SideFileError, match="no exact tiling"):
        psf.check_window_receipts(w, p)


def test_produce_writes_files_manifest_and_is_deterministic(env):
    tmp_path, root, base = env
    pinned = blocks.load_pinned()
    out = str(tmp_path / "side1")
    man = psf.produce(root, base, out)
    assert sorted(os.listdir(out)) == sorted([*psf.OUTPUTS, psf.MANIFEST])
    assert open(out + "/direct_freeze.txt").read().splitlines()[0] == "address" and len(open(out + "/direct_freeze.txt").read().splitlines()) == 39
    assert open(out + "/blacklist_any.txt").read().splitlines() == ["address", a(10), a(11)]
    cl = open(out + "/candidates.csv").read().splitlines()
    assert cl[0] == "address,source_membership" and len(cl) == 13 and f"{a(1)},g1_withdrawal" in cl
    assert len(open(out + "/original_windows.csv").read().splitlines()) == 31
    m = json.load(open(out + "/" + psf.MANIFEST))
    assert m == man and len(m["producing_commit"]) == 40 and len(m["producer_source_sha256"]) == 16
    assert m["baseline_manifest_sha256"] == _sha(base + "/manifest.json")
    assert m["window_receipts_check"].startswith("ok") and "g1b/receipts.jsonl" in m["inputs"]
    for n in psf.OUTPUTS:
        assert m["outputs"][n] == {"sha256": _sha(f"{out}/{n}"), "rows": len(open(f"{out}/{n}").read().splitlines()) - 1}
    for k, v in m["inputs"].items():
        p = os.path.join(root, k) if not k.startswith("baseline_blacklist/") else os.path.join(base, k.split("/")[1])
        assert v["sha256"] == _sha(p)
    assert m["counts"] == {"direct_freeze": 38, "blacklist_any": 2, "candidates": 12, "cohort": 5, "frame_survivors": 7, "cohort_frame_overlap_floor_pass": 0, "cohort_frame_overlap_non_floor": 0, "original_windows": 30}
    assert m["window_receipts_check_skipped"] is False
    out2 = str(tmp_path / "side2")
    man2 = psf.produce(root, base, out2)
    assert man2 == man and all(_sha(f"{out}/{n}") == _sha(f"{out2}/{n}") for n in (*psf.OUTPUTS, psf.MANIFEST))


def test_produce_failures_leave_nothing_and_outdir_is_immutable(env):
    tmp_path, root, base = env
    out = str(tmp_path / "side")
    os.makedirs(out)
    open(out + "/x", "w").write("x")
    with pytest.raises(psf.SideFileError, match="not an empty directory"):
        psf.produce(root, base, out)
    shutil.rmtree(out)
    write_circle(os.path.join(root, "g0", "circle_events.csv"), n_freeze=37, extra=False)   # a defect in an input
    with pytest.raises(psf.SideFileError, match="exactly 38"):
        psf.produce(root, base, out)
    assert os.listdir(str(tmp_path)).count("side") == 0 and not [n for n in os.listdir(str(tmp_path)) if n.startswith("side.tmp-")]
    os.makedirs(out)                       # an existing EMPTY directory is accepted
    write_circle(os.path.join(root, "g0", "circle_events.csv"))
    psf.produce(root, base, out)
    assert os.path.isfile(out + "/" + psf.MANIFEST)
    with pytest.raises(psf.SideFileError):  # and is then immutable
        psf.produce(root, base, out)


def test_produce_refuses_dirty_producer_symlink_outdir_and_changed_pre_range(env, monkeypatch):
    tmp_path, root, base = env
    monkeypatch.setattr(psf, "_producer_dirty", lambda: ["gates/g1b_frame.py"])
    with pytest.raises(psf.SideFileError, match="differ from HEAD"):
        psf.produce(root, base, str(tmp_path / "o"))
    monkeypatch.setattr(psf, "_producer_dirty", lambda: [])
    (tmp_path / "real").mkdir()
    (tmp_path / "ln").symlink_to(tmp_path / "real")
    with pytest.raises(psf.SideFileError, match="symlink"):
        psf.produce(root, base, str(tmp_path / "ln"))
    bad = blocks.load_pinned()
    bad["pre_period_end"] = {"block": END + 1}
    with pytest.raises(psf.SideFileError, match="pinned pre_period"):
        psf.produce(root, base, str(tmp_path / "o2"), pinned=bad)


def test_baseline_events_after_cutoff_are_not_served_to_the_producer():
    """The producer relies on load_baseline(consumer='main') clipping at pre_period_end; R additionally filters block <= end."""
    ev = {"USDC": {a(1): [(END + 1, 0, "add")]}, "USDT": {}}
    assert psf.blacklist_any(ev) == []


def test_run_all_presign_side_requires_every_flag_and_never_calls_the_chain(env, monkeypatch):
    tmp_path, root, base = env
    calls = []
    monkeypatch.setattr(run_all.rpc, "assert_chain_id", lambda ep: calls.append("chain") or 1)
    full = ["--gate", "presign_side", "--input-dir", root, "--baseline-blacklist-dir", base, "--outdir", str(tmp_path / "o")]
    for i in (2, 4, 6):
        argv = full[:i] + full[i + 2:]   # drop one flag+value pair
        with pytest.raises(SystemExit) as e:
            run_all.main(argv)
        assert e.value.code == 2, argv
    for extra in (["--generation", "g"], ["--participation-rule", "none"]):
        with pytest.raises(SystemExit) as e:
            run_all.main(full + extra)
        assert e.value.code == 2
    with pytest.raises(SystemExit):                  # --outdir belongs to presign_side only
        run_all.main(["--gate", "g0_baseline", "--generation", "g", "--outdir", str(tmp_path / "o")])
    os.makedirs(tmp_path / "full")
    open(tmp_path / "full" / "x", "w").write("x")
    with pytest.raises(SystemExit):
        run_all.main(full[:-1] + [str(tmp_path / "full")])
    os.remove(os.path.join(root, "g1", "cohort.csv"))
    with pytest.raises(SystemExit):
        run_all.main(full)
    assert calls == []
    write_inputs(root)
    full_ = full[:-1] + [str(tmp_path / "x" / ".." / "o")]       # raw `..` is refused before abspath collapses it
    with pytest.raises(SystemExit):
        run_all.main(full_)
    os.remove(os.path.join(root, "g1b", "receipts.jsonl"))       # absent receipts fail unless explicitly skipped
    assert run_all.main(full) == 1 and not os.path.exists(tmp_path / "o")
    assert run_all.main(full + ["--skip-window-receipts-check"]) == 0
    assert json.load(open(tmp_path / "o" / psf.MANIFEST))["window_receipts_check_skipped"] is True
    shutil.rmtree(tmp_path / "o")
    _receipts(os.path.join(root, "g1b", "receipts.jsonl"), g1b_frame.original_windows(blocks.load_pinned()))
    assert run_all.main(full) == 0                   # offline run end to end through the CLI
    assert calls == [] and os.path.isfile(tmp_path / "o" / psf.MANIFEST)
    assert "presign_side" not in [n for n, _ in run_all.ALL_GATES]


# --- fix round 2 ---------------------------------------------------------------------------------------------------------

def test_outdir_must_be_strictly_under_the_allowed_root_and_nothing_is_created_elsewhere(env, monkeypatch):
    tmp_path, root, base = env
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setattr(psf, "OUT_ROOT", str(allowed))
    outside = tmp_path / "deep" / "a" / "b"
    with pytest.raises(psf.SideFileError, match="strictly under"):
        psf.produce(root, base, str(outside))
    assert not (tmp_path / "deep").exists()                          # no implicit makedirs of arbitrary parents
    with pytest.raises(psf.SideFileError, match="strictly under"):
        psf.produce(root, base, str(allowed))                        # the root itself is not a new directory under it
    with pytest.raises(psf.SideFileError, match=r"\.\."):
        psf.produce(root, base, str(allowed / "x" / ".." / "y"))     # raw `..`
    psf.produce(root, base, str(allowed / "sub" / "ok"))             # creation inside the root is fine
    assert os.path.isfile(allowed / "sub" / "ok" / psf.MANIFEST)


def test_outdir_must_not_share_a_path_with_input_baseline_population_roots_or_source(env, monkeypatch):
    tmp_path, root, base = env
    allowed = tmp_path / "allowed"
    monkeypatch.setattr(psf, "OUT_ROOT", str(allowed))
    inside_in = allowed / "in"
    shutil.copytree(root, inside_in)
    with pytest.raises(psf.SideFileError, match="shares a path with the input dir"):
        psf.produce(str(inside_in), base, str(inside_in / "g1" / "new"))
    b_in = allowed / "base"
    shutil.copytree(base, b_in)
    with pytest.raises(psf.SideFileError, match="shares a path with the baseline dir"):
        psf.produce(root, str(b_in), str(b_in / "new"))
    monkeypatch.setattr(psf, "_MAIN_ROOT", str(allowed / "mainpop"))
    monkeypatch.setattr(psf, "_JUNE_ROOT", str(allowed / "junepop"))
    for n in ("mainpop", "junepop"):
        with pytest.raises(psf.SideFileError, match="main root|june root"):
            psf.produce(root, base, str(allowed / n / "gen"))
    monkeypatch.setattr(psf, "_SOURCE_DIRS", (str(allowed / "src"),))
    with pytest.raises(psf.SideFileError, match="source tree"):
        psf.produce(root, base, str(allowed / "src" / "gen"))
    # the real defaults: out/recovery/presign_side is clear of main/june/source
    monkeypatch.undo()
    assert not any(psf._shares(os.path.realpath(psf.OUT_ROOT), os.path.realpath(d)) for d in (psf._MAIN_ROOT, psf._JUNE_ROOT, *psf._SOURCE_DIRS))


def test_run_all_validates_outdir_containment_before_anything_runs(env, monkeypatch):
    tmp_path, root, base = env
    monkeypatch.setattr(psf, "OUT_ROOT", str(tmp_path / "allowed"))
    with pytest.raises(SystemExit) as e:
        run_all.main(["--gate", "presign_side", "--input-dir", root, "--baseline-blacklist-dir", base, "--outdir", str(tmp_path / "elsewhere")])
    assert e.value.code == 2 and not (tmp_path / "elsewhere").exists()


def test_all_reads_come_from_private_copies(env, monkeypatch):
    """The originals are mutated after the producer has taken its copies; the outputs and the recorded hashes/row counts must
    describe the copies (what was verified), and nothing may fail or silently mix the two."""
    tmp_path, root, base = env
    cohort = os.path.join(root, "g1", "cohort.csv")
    orig_sha, orig_rows = _sha(cohort), len(open(cohort).read().splitlines()) - 1
    inner = psf.g0_baseline_blacklist.load_baseline

    def mutating(d, consumer, pinned=None):
        assert not os.path.abspath(d).startswith(os.path.abspath(str(tmp_path / "base")))   # baseline is verified from a private copy too
        with open(cohort, "a") as f:
            f.write("99,%s,withdrawal,True\n" % a(77777))
        return inner(d, consumer, pinned=pinned)
    monkeypatch.setattr(psf.g0_baseline_blacklist, "load_baseline", mutating)
    out = str(tmp_path / "side")
    man = psf.produce(root, base, out)
    assert man["inputs"]["g1/cohort.csv"] == {"sha256": orig_sha, "rows": orig_rows}
    assert a(77777) not in open(out + "/candidates.csv").read()
    assert _sha(cohort) != orig_sha                                  # the original really changed; the outputs ignore it


def test_symlinked_input_directory_components_are_rejected(env):
    tmp_path, root, base = env
    for sub in ("g0", "g1", "g1b"):
        real = tmp_path / f"real_{sub}"
        shutil.move(os.path.join(root, sub), real)
        os.symlink(real, os.path.join(root, sub))
        with pytest.raises(psf.SideFileError, match="symlink"):
            psf.produce(root, base, str(tmp_path / f"o_{sub}"))
        os.remove(os.path.join(root, sub))
        shutil.move(real, os.path.join(root, sub))
    real_base = tmp_path / "real_base"
    shutil.move(base, real_base)
    os.symlink(real_base, base)
    with pytest.raises(psf.SideFileError, match="symlink"):
        psf.produce(root, base, str(tmp_path / "o_base"))


def test_producer_check_covers_keccak_untracked_and_missing_files(tmp_path, monkeypatch):
    assert "lib/keccak.py" in [os.path.relpath(p, psf._ROOT) for p in psf.PRODUCER_FILES]
    repo = tmp_path / "repo"
    repo.mkdir()
    env_ = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    git = lambda *c: subprocess.run(["git", *c], cwd=repo, check=True, capture_output=True, env=env_)
    git("init", "-q")
    files = [str(repo / n) for n in ("a.py", "b.py")]
    for p in files:
        open(p, "w").write("x\n")
    git("add", "a.py")
    git("commit", "-qm", "c")
    monkeypatch.setattr(psf, "_ROOT", str(repo))
    monkeypatch.setattr(psf, "PRODUCER_FILES", tuple(files))
    assert any("b.py" in d and "untracked" in d for d in psf._producer_dirty())       # b.py was never added
    git("add", "b.py")
    git("commit", "-qm", "d")
    assert psf._producer_dirty() == []
    open(files[0], "a").write("y\n")
    assert psf._producer_dirty() == ["a.py"]
    git("checkout", "a.py")
    os.remove(files[1])
    assert any("b.py" in d and "missing" in d for d in psf._producer_dirty())


def test_absent_receipts_fail_unless_explicitly_skipped(env):
    tmp_path, root, base = env
    os.remove(os.path.join(root, "g1b", "receipts.jsonl"))
    with pytest.raises(psf.SideFileError, match="--skip-window-receipts-check"):
        psf.produce(root, base, str(tmp_path / "o1"))
    assert not os.path.exists(tmp_path / "o1")
    man = psf.produce(root, base, str(tmp_path / "o2"), skip_window_receipts_check=True)
    assert man["window_receipts_check_skipped"] is True and man["window_receipts_check"].startswith("skipped")


def test_check_outdir_accepts_normal_out_layout_but_refuses_inside_input_subdirs(env, monkeypatch):
    tmp_path, root, base = env
    monkeypatch.setattr(psf, "OUT_ROOT", str(tmp_path / "out" / "recovery" / "presign_side"))
    inp = str(tmp_path / "out")
    got = psf.check_outdir(str(tmp_path / "out" / "recovery" / "presign_side" / "gen-1"), inp, base)
    assert got.endswith("gen-1")
    monkeypatch.setattr(psf, "OUT_ROOT", str(tmp_path / "out"))
    with pytest.raises(psf.SideFileError, match="input dir"):
        psf.check_outdir(str(tmp_path / "out" / "g1" / "new"), inp, base)


def test_private_copy_refuses_a_symlinked_source(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("x")
    ln = tmp_path / "ln.txt"
    ln.symlink_to(real)
    psf._copy_nofollow(str(real), str(tmp_path / "ok.txt"))
    assert (tmp_path / "ok.txt").read_text() == "x"
    with pytest.raises(psf.SideFileError, match="symlinks"):
        psf._copy_nofollow(str(ln), str(tmp_path / "bad.txt"))
