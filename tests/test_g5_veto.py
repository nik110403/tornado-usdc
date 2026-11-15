"""Unit tests for gates/g5_veto.py (the separate §5.9 veto walk). Fixtures only: a fake port, no network, nothing under out/."""
import json
import os
import shutil
import subprocess

import pytest

from gates import g4_controls as g4
from gates import g4_june
from gates import g5_post as g5
from gates import g5_veto as gv
import run_all
from test_g5_post import (A, B, C, D, E, POST_END, PRE, build, installed_r, repo, _log, word)  # noqa: F401  (fixtures: repo, installed_r are autouse there)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W1, W2, W3 = ("0x" + c * 40 for c in "123")


def circle(i, b, w):
    return _log(i, b, g5.COINS["USDC"], [gv.UNITS["circle_blacklisted"][1], word(w)], "0x")


def tether(i, b, w):
    return _log(i, b, g5.COINS["USDT"], [gv.UNITS["tether_added_blacklist"][1]], word(w))


LOGS = {"circle_blacklisted": [circle(1, PRE + 400, A), circle(2, PRE + 2000, B)], "tether_added_blacklist": [tether(3, PRE + 1000, C)]}


class FakeVetoPort:
    """walk(): three tiling ranges per unit on the walk endpoint; refetch(): the same logs on the other endpoint (optionally tampered)."""

    def __init__(self, logs=LOGS, tamper=None, cuts=(PRE + 1500, PRE + 2500)):
        self.logs, self.tamper, self.cuts, self.calls = logs, tamper, cuts, []

    def assert_chain(self):
        self.calls.append(("chain",))

    def walk(self, unit, s, e):
        self.calls.append(("walk", unit, s, e))
        ranges = []
        for a, b in ((s, self.cuts[0]), (self.cuts[0] + 1, self.cuts[1]), (self.cuts[1] + 1, e)):
            sub = [l for l in self.logs[unit] if a <= int(l["blockNumber"], 16) <= b]
            ranges.append((a, b, len(sub), g4._digest(sub, with_block=True), g4.WALK))
        return list(self.logs[unit]), ranges

    def refetch(self, endpoint, unit, s, e):
        self.calls.append(("refetch", endpoint, unit, s, e))
        sub = [l for l in self.logs[unit] if s <= int(l["blockNumber"], 16) <= e]
        return self.tamper(unit, sub) if self.tamper else sub


@pytest.fixture
def fx(tmp_path):
    cfg_path, cfg, p = build(tmp_path)
    return cfg_path, tmp_path / "veto_out", tmp_path / "work"


def go(fx, port=None, **kw):
    cfg_path, outdir, work = fx
    return gv.run(cfg_path, str(outdir), port or FakeVetoPort(), work_dir=str(work), **kw)


def test_end_to_end_output_is_complete_hashed_and_bound_to_the_signature(fx):
    port = FakeVetoPort()
    path, n, sha = go(fx, port)
    out = fx[1]
    m = json.loads((out / "veto_manifest.json").read_text())
    assert n == 3 and m["gate"] == "g5_veto" and m["pre_end_block"] == PRE and m["post_end_block"] == POST_END
    assert set(m["files"]) == {"veto_logs.csv", "receipts.jsonl", "reverify.jsonl"}
    for name, h in m["files"].items():
        assert g5._sha256_bytes((out / name).read_bytes()) == h
    assert m["signed_file_sha256"] == g5.verify_signature((fx[0] and open(fx[0], "rb").read())).signed_hashes
    assert m["walk_endpoint"] == g4.WALK and m["replay_endpoint"] == g4.REPLAY_ENDPOINT[g4.WALK] and m["chain_id"] == 1
    # one chain assertion first, a walk per unit over exactly (pre_end, post_end], every receipted range re-verified on the OTHER endpoint
    assert port.calls[0] == ("chain",)
    assert [c for c in port.calls if c[0] == "walk"] == [("walk", u, PRE + 1, POST_END) for u in gv.UNITS]
    rf = [c for c in port.calls if c[0] == "refetch"]
    assert len(rf) == 6 and all(c[1] == g4.REPLAY_ENDPOINT[g4.WALK] for c in rf)
    rec = [json.loads(l) for l in (out / "receipts.jsonl").read_text().splitlines()]
    assert len(rec) == 6 and all(r["gate"] == "g5_veto" and r["endpoint"] == g4.WALK and r["sha256"].startswith("v2:") for r in rec)
    g4._assert_tiles([(r["fromBlock"], r["toBlock"]) for r in rec if r["unit"] == "circle_blacklisted"], PRE + 1, POST_END)
    assert (out / "veto_logs.csv").read_text().count("\n") == 4          # header + 3 raw logs


def test_the_python_output_verifies_in_R_and_a_tampered_copy_does_not(fx, tmp_path):
    if not shutil.which("Rscript"):
        pytest.skip("no Rscript")
    go(fx)
    out = fx[1]

    def r_verify(d):
        m = json.loads((out / "veto_manifest.json").read_text())
        code = ('for (f in c("utils","pre","presign","post_gate","later_freeze")) source(file.path("R", paste0(f, ".R")));'
                f'sig <- list(signed_hashes = jsonlite::fromJSON("{out}/veto_manifest.json", simplifyVector = FALSE)$signed_file_sha256);'
                f'priv <- tempfile(); dir.create(priv); r <- lf_verify_veto(sig, "{d}", priv, repo_root = getwd(), pre_end = {PRE}L, post_end = {POST_END}L);'
                'cat(if (isTRUE(r$ok)) paste("OK", r$n_logs, r$n_ranges) else paste("ROW", r$reason))')
        env = dict(os.environ)
        return subprocess.run(["Rscript", "-e", code], cwd=ROOT, capture_output=True, text=True, env=env)
    r = r_verify(out)
    assert r.stdout == "OK 3 6", (r.stdout, r.stderr[-800:])                 # Py row text / rows_sha256 / v2 receipts are reproduced by R from the raw logs
    bad = tmp_path / "veto_bad"
    shutil.copytree(out, bad)
    (bad / "veto_logs.csv").write_text((bad / "veto_logs.csv").read_text().replace(f",{PRE + 400},", f",{PRE + 401},", 1))
    assert r_verify(bad).stdout.startswith("ROW ")


# --- pre-access: every denial happens before the port is touched -----------------------------------------------------------------

def test_no_signature_denies_before_any_rpc(tmp_path):
    cfg_path, cfg, p = build(tmp_path)
    shutil.rmtree(p["sd"])
    port = FakeVetoPort()
    with pytest.raises(g5.SignatureError) as e:
        gv.run(cfg_path, str(tmp_path / "o"), port, work_dir=str(tmp_path / "w"))
    assert e.value.pre_access is True and port.calls == [] and not (tmp_path / "w").exists()


@pytest.mark.parametrize("victim", ["gates/g5_veto.py", "R/later_freeze.R", "R/post_gate.R", "run_all.py"])
def test_new_files_must_be_in_the_signed_code_manifest_both_halves(tmp_path, victim):
    def drop(repo, sd):
        ls = [l for l in (sd / "code_manifest.txt").read_text().splitlines(True) if not l.endswith(f"  {victim}\n")]
        (sd / "code_manifest.txt").write_text("".join(ls))
    cfg_path, cfg, p = build(tmp_path, code_mutate=drop)
    port = FakeVetoPort()
    with pytest.raises(g5.SignatureError, match="does not bind") as e:
        gv.run(cfg_path, str(tmp_path / "o"), port, work_dir=str(tmp_path / "w"))
    assert victim in str(e.value) and port.calls == [] and e.value.pre_access is True


def test_r_half_and_input_hash_failures_deny_before_any_rpc(tmp_path):
    def refused(cfg, man):
        man["post_input_file_sha256"]["later_freeze"] = "cd" * 32
        cfg["post_input_paths"]["later_freeze"] = cfg["post_input_paths"]["match_membership"]
    cfg_path, cfg, p = build(tmp_path / "a", mutate=refused) if (tmp_path / "a").mkdir() is None else None
    port = FakeVetoPort()
    with pytest.raises(g5.SignatureError, match="refused"):
        gv.run(cfg_path, str(tmp_path / "o"), port, work_dir=str(tmp_path / "w"))
    (tmp_path / "b").mkdir()
    cfg_path, cfg, p = build(tmp_path / "b")
    open(cfg["post_input_paths"]["match_membership"], "ab").write(b"x")
    with pytest.raises(g5.InputMismatch):
        gv.run(cfg_path, str(tmp_path / "o2"), port, work_dir=str(tmp_path / "w2"))
    assert port.calls == []


def test_g0_receipts_hash_is_bound_like_the_other_signed_inputs_and_a_change_denies_before_any_rpc(tmp_path):
    """Q1 (finding H): the signed manifest carries the g0 receipts.jsonl sha256 next to the two g0 event-file keys; g5 hashes it before any RPC."""
    def add_g0(cfg, man):
        for k in g5.POST_G0_KEYS:
            f = tmp_path / f"{k}.dat"
            f.write_bytes(f"g0 {k}\n".encode())
            cfg["post_input_paths"][k] = str(f)
            man["post_input_file_sha256"][k] = g5._sha256_bytes(f.read_bytes())
    cfg_path, cfg, p = build(tmp_path / "ok", mutate=add_g0) if (tmp_path / "ok").mkdir() is None else None
    go_ok = gv.run(cfg_path, str(tmp_path / "o"), FakeVetoPort(), work_dir=str(tmp_path / "w"))    # all three signed + untouched: the walk runs
    assert go_ok[1] == 3
    (tmp_path / "bad").mkdir()
    cfg_path, cfg, p = build(tmp_path / "bad", mutate=add_g0)
    open(cfg["post_input_paths"]["g0_receipts"], "ab").write(b"x")
    port = FakeVetoPort()
    with pytest.raises(g5.InputMismatch, match="g0_receipts"):
        gv.run(cfg_path, str(tmp_path / "o2"), port, work_dir=str(tmp_path / "w2"))
    assert port.calls == []


def test_outdir_and_workdir_rules_deny_before_any_rpc(fx, tmp_path):
    cfg_path, outdir, work = fx
    port = FakeVetoPort()
    outdir.mkdir()
    (outdir / "stale").write_text("x")
    with pytest.raises(g5.PreAccessDenied, match="not empty"):
        gv.run(cfg_path, str(outdir), port, work_dir=str(work))
    with pytest.raises(g5.PreAccessDenied, match="disjoint"):
        gv.run(cfg_path, str(work / "inside"), port, work_dir=str(work))
    assert port.calls == []


def test_g5_main_work_dir_and_earlier_receipts_are_refused(fx):
    cfg_path, outdir, work = fx
    work.mkdir()
    (work / g5.BINDING_NAME).write_text(json.dumps({"gate": "g5"}))      # a g5 main-run work dir carries another binding
    port = FakeVetoPort()
    with pytest.raises(g5.CheckpointError, match="different producer"):
        go(fx, port)
    assert port.calls == []
    os.remove(work / g5.BINDING_NAME)
    go(fx, FakeVetoPort())
    shutil.rmtree(outdir)
    with pytest.raises(g5.PreAccessDenied, match="earlier run"):
        go(fx, FakeVetoPort())


def test_run_lock_is_taken(fx):
    cfg_path, outdir, work = fx
    work.mkdir()
    held = g4_june.acquire_run_lock(str(work))
    port = FakeVetoPort()
    try:
        with pytest.raises(g5.PreAccessDenied, match="work-dir setup failed"):
            go(fx, port)
    finally:
        held.close()
    assert port.calls == []


def test_request_guard_and_reverify_mismatch_publish_nothing(fx):
    g = gv.GuardedVetoPort(FakeVetoPort(), PRE, POST_END)
    for s, e in ((PRE, PRE + 5), (PRE + 1, POST_END + 1)):
        with pytest.raises(g5.SpanError):
            g.walk("circle_blacklisted", s, e)
        with pytest.raises(g5.SpanError):
            g.refetch("tenderly", "circle_blacklisted", s, e)
    drop = lambda unit, sub: sub[:-1] if unit == "circle_blacklisted" else sub
    with pytest.raises(g5.ReverifyError, match="mismatch"):
        go(fx, FakeVetoPort(tamper=drop))
    assert not fx[1].exists()                                                     # nothing published


def test_walk_that_does_not_tile_or_returns_foreign_logs_publishes_nothing(fx):
    class Gap(FakeVetoPort):
        def walk(self, unit, s, e):
            logs, ranges = super().walk(unit, s, e)
            return logs, ranges[:1] + ranges[2:]
    with pytest.raises(AssertionError, match="gap"):
        go(fx, Gap())
    assert not fx[1].exists()
    foreign = {**LOGS, "tether_added_blacklist": [circle(9, PRE + 1000, C)]}
    shutil.rmtree(fx[2], ignore_errors=True)
    with pytest.raises(gv.VetoError):
        go(fx, FakeVetoPort(logs=foreign))


# --- binding / mirrors / CLI -----------------------------------------------------------------------------------------------------

def test_new_files_are_in_both_signed_code_lists_and_mirror_the_R_side():
    for f in ("gates/g5_veto.py",):
        assert f in g5.CODE_FILES and f in g5.POST_R_CODE_FILES and f in g5.ALL_CODE_FILES
    assert "R/later_freeze.R" in g5.POST_R_CODE_FILES and "later_freeze" in {"later_freeze"}
    assert "later_freeze" in g5.POST_REFUSED_KEYS                                  # a hand-supplied csv is refused; the derived path uses other keys
    assert set(g5.POST_G0_KEYS) == {"g0_circle_events", "g0_usdt_events", "g0_receipts"} <= set(g5.POST_OPTIONAL_KEYS)
    if shutil.which("Rscript"):
        out = subprocess.run(["Rscript", "-e", 'source("R/later_freeze.R"); for (u in names(LF_UNITS)) cat(u, LF_UNITS[[u]]$address, LF_UNITS[[u]]$topic0, "\\n")'],
                             cwd=ROOT, capture_output=True, text=True, check=True).stdout.split("\n")
        assert {l.split()[0]: tuple(l.split()[1:]) for l in out if l.strip()} == gv.UNITS


def test_cli_routing_requires_config_and_outdir_and_denies_a_missing_config(tmp_path, capsys):
    with pytest.raises(SystemExit):
        run_all.main(["--gate", "g5_veto", "--config", str(tmp_path / "c.json")])
    with pytest.raises(SystemExit):
        run_all.main(["--gate", "g5_veto", "--outdir", str(tmp_path / "o")])
    assert run_all.main(["--gate", "g5_veto", "--config", str(tmp_path / "missing.json"), "--outdir", str(tmp_path / "o")]) == 3
    assert "DENIED" in capsys.readouterr().out
