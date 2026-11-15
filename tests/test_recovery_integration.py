"""S20a/S20b: explicit immutable routing in run_all.py / run_all.R and pre-load population attestation (June + main). No network, fixtures only."""
import hashlib
import json
import os
import shutil
import subprocess

import pytest

import run_all
from gates import g1_cohort, g4_controls, g4_june
from lib import blocks, rpc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JUNE_END = 15053225
COMMIT = "a" * 40
HEAD_COMMIT = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), text=True).strip()  # a commit that exists (R checks git cat-file)
needs_r = pytest.mark.skipif(shutil.which("Rscript") is None, reason="Rscript not installed")


def _sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


@pytest.fixture
def routing(tmp_path, monkeypatch):
    base = tmp_path / "baseline"
    base.mkdir()
    (base / "manifest.json").write_text("{}")
    inp = tmp_path / "in"
    (inp / "g0").mkdir(parents=True)
    calls = []
    monkeypatch.setattr(run_all.rpc, "assert_chain_id", lambda ep: calls.append("chain") or 1)
    monkeypatch.setattr(run_all.blocks, "load_pinned", lambda: {})
    return base, inp, calls


def test_requires_explicit_flags_before_any_rpc(routing):
    base, inp, calls = routing
    for argv in (["--gate", "g4_june"],
                 ["--gate", "g4_june", "--generation", "g1", "--input-dir", str(inp)],
                 ["--gate", "g4_june", "--generation", "g1", "--baseline-blacklist-dir", str(base)],
                 ["--gate", "g0_baseline"],
                 ["--gate", "g1", "--generation", "g1"],
                 ["--gate", "g0_baseline", "--generation", "current"],
                 ["--gate", "g0_baseline", "--generation", "../x"]):
        with pytest.raises(SystemExit) as e:
            run_all.main(argv)
        assert e.value.code == 2, argv
    assert calls == []  # parser rejected everything before the chain-id RPC


def test_rejects_current_pointer_symlink_and_missing_dirs(routing, tmp_path):
    base, inp, calls = routing
    link = tmp_path / "linked"
    link.symlink_to(base)
    cur = tmp_path / "current"
    cur.mkdir()
    (cur / "manifest.json").write_text("{}")
    for b in (str(link), str(cur), str(tmp_path / "nope")):
        with pytest.raises(SystemExit):
            run_all.main(["--gate", "g4_june", "--generation", "g1", "--baseline-blacklist-dir", b, "--input-dir", str(inp)])
    empty = tmp_path / "emptybase"
    empty.mkdir()
    with pytest.raises(SystemExit):   # no manifest.json: not an accepted baseline generation
        run_all.main(["--gate", "g4_june", "--generation", "g1", "--baseline-blacklist-dir", str(empty), "--input-dir", str(inp)])
    with pytest.raises(SystemExit):   # input root without g0/
        run_all.main(["--gate", "g4_june", "--generation", "g1", "--baseline-blacklist-dir", str(base), "--input-dir", str(tmp_path)])
    assert calls == []


def test_g4_june_routing_is_explicit(routing, monkeypatch):
    base, inp, calls = routing
    seen = {}
    monkeypatch.setattr(g4_june, "run", lambda cfg: seen.update(cfg=cfg) or 0)
    assert run_all.main(["--gate", "g4_june", "--generation", "gen-A", "--baseline-blacklist-dir", str(base), "--input-dir", str(inp),
                         "--participation-rule", "baseline_transfer_required"]) == 0
    assert seen["cfg"]["participation_rule"] == "baseline_transfer_required"
    assert seen["cfg"]["baseline_blacklist_dir"] == str(base) and seen["cfg"]["generation"] == "gen-A"
    assert seen["cfg"]["output_dir"] == os.path.join(ROOT, "out", "recovery", "june", "gen-A")
    assert seen["cfg"]["input_root"] == str(inp)
    assert calls == ["chain"]


def test_g4_june_requires_participation_rule_before_rpc(routing):
    base, inp, calls = routing
    common = ["--gate", "g4_june", "--generation", "gen-R", "--baseline-blacklist-dir", str(base), "--input-dir", str(inp)]
    for extra in ([], ["--participation-rule", "maybe"]):
        with pytest.raises(SystemExit) as e:
            run_all.main(common + extra)
        assert e.value.code == 2, extra
    with pytest.raises(SystemExit):   # smoke needs it too
        run_all.main(common + ["--smoke"])
    assert calls == []   # parser rejected everything before the chain-id RPC


def test_g0_baseline_registered_and_opt_in(monkeypatch):
    assert "g0_baseline" in dict(run_all.GATES) and "g0_baseline" not in dict(run_all.ALL_GATES)
    seen = {}
    monkeypatch.setattr(run_all.rpc, "assert_chain_id", lambda ep: 1)
    monkeypatch.setattr(run_all.blocks, "load_pinned", lambda: {})
    monkeypatch.setattr(run_all.g0_baseline_blacklist, "run", lambda cfg: seen.update(cfg) or 0)
    assert run_all.main(["--gate", "g0_baseline", "--generation", "gen-B"]) == 0
    assert seen["generation"] == "gen-B"


# ---- R entrypoint: rejection happens before any CSV body is read (the CSVs here are garbage on purpose) ----------------

SCHEMA_NOTE = "participation_rule=%s (recorded, not defaulted; fixture)"
RULE = "baseline_transfer_required"
WIN = {"after_block_exclusive": 14929560, "to_block_inclusive": JUNE_END, "first_date": "2022-06-09", "last_date": "2022-06-30", "day_index_lt": 22}


def _june_dir(tmp_path, name="gen-J", schema_rule=RULE, meta=None, **override):
    d = tmp_path / name
    d.mkdir()
    for f in ("covariates.csv", "wallet_coin_day_pre.csv"):
        (d / f).write_text("not,a,valid,csv\n\x00\x01")
    (d / "meta.json").write_text(json.dumps(meta if meta is not None else {"participation_rule": RULE, "participation_window": WIN}))
    for sf, csv_ in (("covariates.schema.json", "covariates.csv"), ("wallet_coin_day_pre.schema.json", "wallet_coin_day_pre.csv")):
        (d / sf).write_text(json.dumps({"sha256": _sha(d / csv_), "notes": SCHEMA_NOTE % schema_rule}))
    man = {"population_id": "june_placebo", "generation_id": name, "producing_commit": COMMIT, "producer_source_sha256": "e" * 16, "schema_version": 1,
           "baseline_start_date": "2022-06-09", "baseline_end_date": "2022-06-30", "excluded_dates": [],
           "cutoff_block": JUNE_END, "input_file_sha256": {"manifest.csv": "b" * 64},
           "output_file_sha256": {f: _sha(d / f) for f in ("covariates.csv", "wallet_coin_day_pre.csv", "meta.json", "covariates.schema.json", "wallet_coin_day_pre.schema.json")},
           "participation_rule": RULE, "participation_window": WIN,
           "row_counts": {"covariates.csv": 2, "wallet_coin_day_pre.csv": 4}}
    man.update(override)
    man = {k: v for k, v in man.items() if v is not None}
    (d / "population_manifest.json").write_text(json.dumps(man))
    return d


def _rscript(*args, rule=RULE):
    if rule and "june" in args:
        args = (*args, "--participation-rule", rule)
    return subprocess.run(["Rscript", "run_all.R", "--stage", "pre", *args], cwd=ROOT, capture_output=True, text=True, timeout=300)


@needs_r
def test_r_cli_rejects_missing_routing(tmp_path):
    d = _june_dir(tmp_path)
    for args, msg in (([], "requires --placebo-pop"),
                      (["--placebo-pop", "june"], "requires --input-dir"),
                      (["--placebo-pop", "june", "--input-dir", str(d)], "requires --outdir"),
                      (["--placebo-pop", "g4_june", "--input-dir", str(d), "--outdir", str(tmp_path / "o")], "legacy"),
                      (["--placebo-pop", "g4", "--input-dir", str(d), "--outdir", str(tmp_path / "o")], "requires --g0dir")):
        r = _rscript(*args)
        assert r.returncode != 0 and msg in r.stderr, (args, r.stderr[-400:])


@needs_r
@pytest.mark.parametrize("override,msg", [
    ({"population_id": "main"}, "population_id"),
    ({"population_id": None}, "manifest lacks: population_id"),
    ({"producing_commit": "abc"}, "producing_commit"),
    ({"producing_commit": None}, "manifest lacks: producing_commit"),
    ({"cutoff_block": JUNE_END + 1}, "cutoff_block"),
    ({"cutoff_block": None}, "manifest lacks: cutoff_block"),
    ({"generation_id": "gen-other"}, "generation_id"),
    ({"output_file_sha256": {f: "c" * 64 for f in ("covariates.csv", "wallet_coin_day_pre.csv", "meta.json", "covariates.schema.json", "wallet_coin_day_pre.schema.json")}}, "sha256 differs"),
    ({"output_file_sha256": {"covariates.csv": "c" * 64}}, "lacks wallet_coin_day_pre.csv"),
    ({"participation_window": None}, "manifest participation_window is absent or differs"),                       # intermediate-commit generation: rule recorded, no window
    ({"participation_window": {**WIN, "after_block_exclusive": 14929561}}, "manifest participation_window is absent or differs"),
    ({"participation_window": {**WIN, "day_index_lt": 23}}, "manifest participation_window is absent or differs"),
    ({"producer_source_sha256": None}, "manifest lacks: producer_source_sha256"),
    ({"input_file_sha256": {}}, "input_file_sha256"),
    ({"input_file_sha256": None}, "manifest lacks: input_file_sha256"),
    ({"baseline_end_date": "2022-08-07"}, "baseline window"),
])
def test_r_cli_rejects_bad_manifest_before_csv_load(tmp_path, override, msg):
    d = _june_dir(tmp_path, **override)
    r = _rscript("--placebo-pop", "june", "--input-dir", str(d), "--outdir", str(tmp_path / "out"))
    assert r.returncode != 0 and "REJECTED (before any CSV load)" in r.stderr and msg in r.stderr, r.stderr[-500:]
    assert not (tmp_path / "out").exists()


@needs_r
def test_r_cli_requires_participation_rule_and_refuses_mismatch(tmp_path):
    d = _june_dir(tmp_path)
    r = _rscript("--placebo-pop", "june", "--input-dir", str(d), "--outdir", str(tmp_path / "o0"), rule=None)
    assert r.returncode != 0 and "requires --participation-rule" in r.stderr
    r = _rscript("--placebo-pop", "june", "--input-dir", str(d), "--outdir", str(tmp_path / "o1"), rule="none")   # generation says required
    assert r.returncode != 0 and "participation rule mismatch" in r.stderr and not (tmp_path / "o1").exists()
    r = _rscript("--placebo-pop", "june", "--input-dir", str(d), "--outdir", str(tmp_path / "o2"), rule="maybe")
    assert r.returncode != 0 and "--participation-rule must be one of" in r.stderr
    r = _rscript("--placebo-pop", "g4", "--input-dir", str(d), "--outdir", str(tmp_path / "o3"), "--g0dir", str(d), "--participation-rule", RULE)
    assert r.returncode != 0 and "applies to --placebo-pop june only" in r.stderr


@needs_r
@pytest.mark.parametrize("kw,msg", [
    ({"participation_rule": None}, "legacy generation"),                             # unrecorded rule + CLI baseline_transfer_required: refused
    ({"participation_rule": "none"}, "participation rule mismatch"),                # manifest says none, CLI + schema say required
    ({"schema_rule": "none"}, "participation rule mismatch"),                        # schema notes say none, manifest + CLI say required
])
def test_r_cli_rule_recording_is_cross_checked(tmp_path, kw, msg):
    d = _june_dir(tmp_path, **kw)
    r = _rscript("--placebo-pop", "june", "--input-dir", str(d), "--outdir", str(tmp_path / "out"))
    assert r.returncode != 0 and msg in r.stderr and not (tmp_path / "out").exists(), r.stderr[-400:]


@needs_r
def test_r_cli_legacy_admission_is_pinned_to_s21(tmp_path):
    """A manifest without participation_rule is accepted only as the pinned S21 generation: a stripped (non-S21) one is refused even under `none`."""
    def mk(name, meta=None, schema_rule=None, **kw):
        d = _june_dir(tmp_path, name=name, participation_rule=None, participation_window=None, meta=meta if meta is not None else {"frame_sha256": "x"}, **kw)
        for sf, csv_ in (("covariates.schema.json", "covariates.csv"), ("wallet_coin_day_pre.schema.json", "wallet_coin_day_pre.csv")):
            (d / sf).write_text(json.dumps({"sha256": _sha(d / csv_), "notes": "legacy notes"}))
        m = json.loads((d / "population_manifest.json").read_text())
        m["output_file_sha256"].update({sf: _sha(d / sf) for sf in ("covariates.schema.json", "wallet_coin_day_pre.schema.json")})
        m.update(kw)
        (d / "population_manifest.json").write_text(json.dumps(m)); return d
    r = _rscript("--placebo-pop", "june", "--input-dir", str(mk("gl1")), "--outdir", str(tmp_path / "o1"), rule=RULE)
    assert r.returncode != 0 and "legacy generation" in r.stderr and "REJECTED (before any CSV load)" in r.stderr
    r = _rscript("--placebo-pop", "june", "--input-dir", str(mk("gl2")), "--outdir", str(tmp_path / "o2"), rule="none")
    assert r.returncode != 0 and "not the pinned S21 generation" in r.stderr and not (tmp_path / "o2").exists()
    d = _june_dir(tmp_path, name="gl3", participation_rule=None)   # schema notes record a rule: refused (pin first, inconsistency would be next)
    r = _rscript("--placebo-pop", "june", "--input-dir", str(d), "--outdir", str(tmp_path / "o3"), rule="none")
    assert r.returncode != 0 and "REJECTED (before any CSV load)" in r.stderr


@needs_r
@pytest.mark.parametrize("kw,msg", [
    ({"meta": {"participation_window": WIN}}, "meta.json participation_rule"),
    ({"meta": {"participation_rule": "none", "participation_window": WIN}}, "meta.json participation_rule"),
    ({"meta": {"participation_rule": RULE}}, "meta.json participation_window"),
    ({"meta": {"participation_rule": RULE, "participation_window": {**WIN, "to_block_inclusive": JUNE_END + 1}}}, "meta.json participation_window"),
])
def test_r_cli_meta_must_carry_manifest_rule_and_window(tmp_path, kw, msg):
    d = _june_dir(tmp_path, **kw)
    r = _rscript("--placebo-pop", "june", "--input-dir", str(d), "--outdir", str(tmp_path / "out"))
    assert r.returncode != 0 and msg in r.stderr and not (tmp_path / "out").exists(), r.stderr[-400:]


@needs_r
def test_r_cli_refuses_unlisted_file_and_bad_panel_schema(tmp_path):
    d = _june_dir(tmp_path)
    (d / "stray.txt").write_text("x")
    r = _rscript("--placebo-pop", "june", "--input-dir", str(d), "--outdir", str(tmp_path / "o1"))
    assert r.returncode != 0 and "not listed in output_file_sha256: stray.txt" in r.stderr
    d = _june_dir(tmp_path, name="gen-P")
    (d / "wallet_coin_day_pre.schema.json").write_text(json.dumps({"sha256": "0" * 64, "notes": SCHEMA_NOTE % RULE}))
    r = _rscript("--placebo-pop", "june", "--input-dir", str(d), "--outdir", str(tmp_path / "o2"))
    assert r.returncode != 0 and "sha256 differs from manifest" in r.stderr
    d = _june_dir(tmp_path, name="gen-Q", output_file_sha256={f: "c" * 64 for f in ("covariates.csv", "wallet_coin_day_pre.csv", "meta.json", "covariates.schema.json")})
    r = _rscript("--placebo-pop", "june", "--input-dir", str(d), "--outdir", str(tmp_path / "o3"))
    assert r.returncode != 0 and "lacks wallet_coin_day_pre.schema.json" in r.stderr


@needs_r
def test_r_expected_window_equals_producer_window():
    """Py/R parity: R derives the window from config/blocks.json + June constants; it must equal what g4_june.participation_window records."""
    out = subprocess.run(["Rscript", "-e", 'invisible(sapply(c("utils","decide","cem","estimate","validate","synth","pre"), function(f) source(file.path("R", paste0(f, ".R"))))); '
                          'cat(jsonlite::toJSON(expected_participation_window(pinned_june_end()), auto_unbox = TRUE))'], cwd=ROOT, capture_output=True, text=True, timeout=300)
    pinned = blocks.load_pinned()
    assert out.returncode == 0, out.stderr[-400:]
    assert json.loads(out.stdout) == g4_june.participation_window(pinned["pre_period_start"]["block"], pinned["june_end"]["block"])


@needs_r
def test_r_cli_rejects_absent_manifest_current_and_nonempty_outdir(tmp_path):
    d = _june_dir(tmp_path)
    (d / "population_manifest.json").unlink()
    r = _rscript("--placebo-pop", "june", "--input-dir", str(d), "--outdir", str(tmp_path / "o1"))
    assert r.returncode != 0 and "population_manifest.json missing" in r.stderr
    (tmp_path / "ptr").symlink_to(_june_dir(tmp_path, "gen-K"))
    r = _rscript("--placebo-pop", "june", "--input-dir", str(tmp_path / "ptr"), "--outdir", str(tmp_path / "o2"))
    assert r.returncode != 0 and ("symlink" in r.stderr or "current" in r.stderr)
    full = tmp_path / "full"
    full.mkdir()
    (full / "x").write_text("x")
    r = _rscript("--placebo-pop", "june", "--input-dir", str(_june_dir(tmp_path, "gen-L")), "--outdir", str(full))
    assert r.returncode != 0 and "not empty" in r.stderr


# ---- producer (g4_june._publish) -> consumer (pre.R) on a fixture: the manifest the gate writes is the one R accepts ----

def _fixture_stage(stage, n=500):
    import csv
    import random
    from datetime import date, timedelta
    rng = random.Random(3)
    os.makedirs(stage)
    arms = ["control"] * (n * 7 // 10) + ["withdrawal"] * (n // 10) + ["deposit"] * (n // 10) + ["both"] * (n - n * 7 // 10 - 2 * (n // 10))
    rng.shuffle(arms)
    with open(os.path.join(stage, "covariates.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["wallet_id", "address", "arm", "transfer_count_total", "eth_balance_wei_units", "wallet_age_blocks",
                    "issuer_mint_redeem_flag", "blacklisted_usdc_at_pre_end", "blacklisted_usdt_at_pre_end"])
        for i in range(n):
            w.writerow([i, "0x%040x" % i, arms[i], rng.randint(1, 30), rng.randint(10**17, 10**19), rng.randint(10**4, 10**7),
                        "False", "False", "False"])
    base = [max(400, rng.lognormvariate(9, 0.6)) for _ in range(n)]
    with open(os.path.join(stage, "wallet_coin_day_pre.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["wallet_id", "coin", "date", "day_index", "balance_units"])
        for i in range(n):
            for coin in ("USDC", "USDT"):
                for d in range(60):
                    w.writerow([i, coin, (date(2022, 6, 9) + timedelta(days=d)).isoformat(), d, int(base[i] * rng.uniform(0.9, 1.1) * 1e6)])
    for sf, csv_ in (("covariates.schema.json", "covariates.csv"), ("wallet_coin_day_pre.schema.json", "wallet_coin_day_pre.csv")):
        with open(os.path.join(stage, sf), "w") as f:
            json.dump({"sha256": _sha(os.path.join(stage, csv_)), "notes": SCHEMA_NOTE % RULE}, f)
    return n, n * 2 * 60


def test_g4_june_requires_explicit_output_and_input(tmp_path):
    with pytest.raises(RuntimeError, match="no output dir"):
        g4_june.run({})
    with pytest.raises(RuntimeError, match="no input root"):
        g4_june.run({"output_dir": str(tmp_path / "o")})
    full = tmp_path / "full"
    full.mkdir()
    (full / "x").write_text("x")
    with pytest.raises(RuntimeError, match="immutable"):
        g4_june.run({"output_dir": str(full), "input_root": str(tmp_path)})
    assert not (tmp_path / "full.work").exists()


@needs_r
def test_gate_manifest_is_accepted_by_pre_r_end_to_end(tmp_path, monkeypatch):
    final = str(tmp_path / "gen-prod")
    stage = str(tmp_path / "stage")
    n_cov, n_panel = _fixture_stage(stage)
    work = _work_dir(tmp_path)
    inputs = {"manifest.csv": "b" * 64, "usdt_events.csv": "c" * 64, "baseline_blacklist_manifest": "d" * 64}
    ident = rpc.producer_identity(g4_june.PRODUCER_FILES)
    monkeypatch.setattr(g4_june, "_producer_dirty", lambda: [])  # the worktree may be mid-edit; dirtiness has its own test
    g4_june._publish(final, stage, work, False, inputs, JUNE_END, n_cov, n_panel, ident, participation_rule=RULE, participation_window=WIN)
    assert not os.path.exists(stage)
    man = json.load(open(os.path.join(final, "population_manifest.json")))
    assert man["population_id"] == "june_placebo" and man["generation_id"] == "gen-prod" and man["cutoff_block"] == JUNE_END
    assert man["participation_rule"] == RULE and "covariates.schema.json" in man["output_file_sha256"]
    assert man["participation_window"] == WIN
    assert man["input_file_sha256"] == inputs and man["row_counts"]["covariates.csv"] == n_cov
    assert man["producing_commit"] + ":" + man["producer_source_sha256"] == ident
    assert set(man["output_file_sha256"]) == {"covariates.csv", "wallet_coin_day_pre.csv", "meta.json", "covariates.schema.json", "wallet_coin_day_pre.schema.json"}
    r = _rscript("--placebo-pop", "june", "--input-dir", final, "--outdir", str(tmp_path / "res"), "--usdc-mode", "none")
    assert r.returncode == 0, r.stderr[-800:]
    assert (tmp_path / "res" / "prespec_manifest.json").exists()
    with open(os.path.join(final, "covariates.csv"), "a") as f:   # a post-hoc edit of an output is caught by the hash check
        f.write("\n")
    r = _rscript("--placebo-pop", "june", "--input-dir", final, "--outdir", str(tmp_path / "res2"))
    assert r.returncode != 0 and "sha256 differs" in r.stderr


def _work_dir(tmp_path):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    (work / "meta.json").write_text(json.dumps({"participation_rule": RULE, "participation_window": WIN}))   # the gate's meta carries both (g4_june.run)
    _write_ckpts(work)
    return str(work)


def _write_ckpts(work, walk_eps=("mevblocker",), rv=(("mevblocker", "tenderly"),)):
    """Minimal walk + reverify checkpoints in the _Ckpt jsonl format; default = the single-provider default run."""
    with open(os.path.join(work, "walk_checkpoint.jsonl"), "w") as f:
        for i, ep in enumerate(walk_eps):
            f.write(json.dumps({"k": f"USDC:0:{i}", "v": {"ranges": [[1, 2, 3, "d" * 64, ep]]}}) + "\n")
    with open(os.path.join(work, "reverify_checkpoint.jsonl"), "w") as f:
        for i, (src, ver) in enumerate(rv):
            f.write(json.dumps({"k": f"USDC:0:{i}:1:2", "v": {"v": 1, "count": 3, "digest": "d" * 64, "source": src, "verifier": ver}}) + "\n")


def test_smoke_manifest_is_not_a_june_placebo(tmp_path, monkeypatch):
    stage = str(tmp_path / "stage")
    _fixture_stage(stage, 10)
    monkeypatch.setattr(g4_june, "_producer_dirty", lambda: [])
    g4_june._publish(str(tmp_path / "gen-s"), stage, _work_dir(tmp_path), True, {"manifest.csv": "b" * 64}, JUNE_END, 10, 1200,
                     rpc.producer_identity(g4_june.PRODUCER_FILES), participation_rule=RULE, participation_window=WIN)
    assert json.load(open(tmp_path / "gen-s" / "population_manifest.json"))["population_id"] == "june_placebo_smoke"


def test_publish_requires_rule_and_checks_it_against_schema_notes(tmp_path, monkeypatch):
    ident = rpc.producer_identity(g4_june.PRODUCER_FILES)
    monkeypatch.setattr(g4_june, "_producer_dirty", lambda: [])
    stage = str(tmp_path / "s1")
    _fixture_stage(stage, 4)
    for bad in (None, "maybe", "none"):   # "none" != the rule the fixture schema records
        with pytest.raises((ValueError, RuntimeError), match="participation_rule"):
            g4_june._publish(str(tmp_path / "g1"), stage, _work_dir(tmp_path), False, {"a": "b" * 64}, JUNE_END, 4, 480, ident, participation_rule=bad, participation_window=WIN)
    assert os.path.isdir(stage) and not os.path.exists(tmp_path / "g1")


def test_publish_refuses_moved_head_dirty_sources_and_filled_target(tmp_path, monkeypatch):
    ident = rpc.producer_identity(g4_june.PRODUCER_FILES)
    monkeypatch.setattr(g4_june, "_producer_dirty", lambda: [])
    stale = ("f" * 40) + ":" + ident.split(":")[1]
    stage = str(tmp_path / "s1")
    _fixture_stage(stage, 4)
    with pytest.raises(RuntimeError, match="identity changed"):
        g4_june._publish(str(tmp_path / "g1"), stage, _work_dir(tmp_path), False, {"a": "b" * 64}, JUNE_END, 4, 480, stale, participation_rule=RULE, participation_window=WIN)
    monkeypatch.setattr(g4_june, "_producer_dirty", lambda: ["gates/g4_june.py"])
    with pytest.raises(RuntimeError, match="differ from HEAD"):
        g4_june._publish(str(tmp_path / "g1"), stage, _work_dir(tmp_path), False, {"a": "b" * 64}, JUNE_END, 4, 480, ident, participation_rule=RULE, participation_window=WIN)
    monkeypatch.setattr(g4_june, "_producer_dirty", lambda: [])
    (tmp_path / "g2").mkdir()
    (tmp_path / "g2" / "x").write_text("x")   # target filled between start and publish
    with pytest.raises(RuntimeError, match="immutable"):
        g4_june._publish(str(tmp_path / "g2"), stage, _work_dir(tmp_path), False, {"a": "b" * 64}, JUNE_END, 4, 480, ident, participation_rule=RULE, participation_window=WIN)
    assert os.path.isdir(stage)


def test_g4_june_rejects_work_suffix_and_symlinked_work(tmp_path):
    with pytest.raises(RuntimeError, match=r"\.work"):
        g4_june.run({"output_dir": str(tmp_path / "x.work"), "input_root": str(tmp_path)})
    (tmp_path / "real").mkdir()
    (tmp_path / "g.work").symlink_to(tmp_path / "real")
    with pytest.raises(RuntimeError, match="symlink"):
        g4_june.run({"output_dir": str(tmp_path / "g"), "input_root": str(tmp_path)})


def test_run_all_rejects_work_suffix_generation(routing):
    base, inp, calls = routing
    with pytest.raises(SystemExit):
        run_all.main(["--gate", "g0_baseline", "--generation", "gen.work"])


# ======================================================================================================================
# S20b: g1 participation rule, main-population generation routing, main presign stage
# ======================================================================================================================

PRE_END = 15298282
PRE_START = 14929560
RULES = ("baseline_transfer_required", "none")


@pytest.fixture
def main_routing(tmp_path, monkeypatch):
    base = tmp_path / "baseline"
    base.mkdir()
    (base / "manifest.json").write_text("{}")
    inp = tmp_path / "in"
    for d, f in run_all.MAIN_INPUT_FILES:
        (inp / d).mkdir(parents=True, exist_ok=True)
        (inp / d / f).write_text("x\n")
    (inp / "g0" / "manifest.csv").write_text("x\n")
    calls = []
    monkeypatch.setattr(run_all.rpc, "assert_chain_id", lambda ep: calls.append("chain") or 1)
    monkeypatch.setattr(run_all.blocks, "load_pinned", lambda: {"pre_period_end": {"block": PRE_END}})
    for k in [k for k in os.environ if k.startswith("G4_")]:
        monkeypatch.delenv(k, raising=False)
    return base, inp, calls


def test_g1_and_all_require_participation_rule_before_rpc(routing):
    _, _, calls = routing
    for gate in ("g1", "all"):
        with pytest.raises(SystemExit) as e:
            run_all.main(["--gate", gate])
        assert e.value.code == 2
    for argv in (["--gate", "g1", "--participation-rule", "maybe"], ["--gate", "g0", "--participation-rule", "none"]):
        with pytest.raises(SystemExit):
            run_all.main(argv)
    assert calls == []


@pytest.mark.parametrize("rule", RULES)
def test_participation_rule_reaches_g1_cfg(routing, monkeypatch, rule):
    seen = {}
    monkeypatch.setattr(g1_cohort, "run", lambda cfg: seen.update(cfg) or 0)
    assert run_all.main(["--gate", "g1", "--participation-rule", rule]) == 0
    assert seen["participation_rule"] == rule and g1_cohort.resolve_participation_rule(seen) == rule
    assert tuple(g1_cohort.PARTICIPATION_RULES) == RULES


def test_main_g4_requires_every_routing_flag_and_input_before_rpc(main_routing, tmp_path, monkeypatch):
    base, inp, calls = main_routing
    full = ["--gate", "g4", "--generation", "gen-M", "--baseline-blacklist-dir", str(base), "--input-dir", str(inp)]
    for drop in (2, 4, 6):   # each routing flag missing
        with pytest.raises(SystemExit):
            run_all.main(full[:drop] + full[drop + 2:])
    with pytest.raises(SystemExit):   # bare `--gate g4` (the old implicit out/g4 route)
        run_all.main(["--gate", "g4"])
    (inp / "g1" / "cohort.csv").unlink()
    with pytest.raises(SystemExit):
        run_all.main(full)
    (inp / "g1" / "cohort.csv").write_text("x\n")
    monkeypatch.setenv("G4_SAMPLE_CONTROLS", "10")
    with pytest.raises(SystemExit):
        run_all.main(full)
    monkeypatch.delenv("G4_SAMPLE_CONTROLS")
    monkeypatch.setenv("G4_BASELINE_BLACKLIST_DIR", str(base))
    with pytest.raises(SystemExit):
        run_all.main(full)
    monkeypatch.delenv("G4_BASELINE_BLACKLIST_DIR")
    link = tmp_path / "linked"
    link.symlink_to(base)
    with pytest.raises(SystemExit):
        run_all.main(full[:5] + [str(link)] + full[6:])
    with pytest.raises(SystemExit):   # --smoke is June-only
        run_all.main(full + ["--smoke"])
    with pytest.raises(SystemExit):
        run_all.main(["--gate", "g4", "--generation", "current", "--baseline-blacklist-dir", str(base), "--input-dir", str(inp)])
    assert calls == []


def test_main_g4_routes_to_distinct_main_root_and_rejects_nonempty_output(main_routing, monkeypatch, tmp_path):
    base, inp, calls = main_routing
    seen = {}
    monkeypatch.setattr(run_all, "run_g4_main", lambda cfg: seen.update(cfg=cfg) or 0)
    assert run_all.main(["--gate", "g4", "--generation", "gen-test-routing", "--baseline-blacklist-dir", str(base), "--input-dir", str(inp)]) == 0
    c = seen["cfg"]
    assert c["output_dir"] == os.path.join(ROOT, "out", "recovery", "main", "gen-test-routing")
    assert c["baseline_blacklist_dir"] == str(base) and c["input_root"] == str(inp) and "participation_rule" not in c
    assert run_all.RECOVERY_MAIN_ROOT != run_all.RECOVERY_JUNE_ROOT and not run_all.RECOVERY_MAIN_ROOT.startswith(run_all.RECOVERY_JUNE_ROOT)
    assert calls == ["chain"]
    root = tmp_path / "mainroot"
    (root / "gen-taken").mkdir(parents=True)
    (root / "gen-taken" / "x").write_text("x")
    monkeypatch.setitem(run_all.OUTPUT_ROOTS, "g4", str(root))
    with pytest.raises(SystemExit):
        run_all.main(["--gate", "g4", "--generation", "gen-taken", "--baseline-blacklist-dir", str(base), "--input-dir", str(inp)])


def _main_cfg(tmp_path, base, inp, name="gen-M"):
    return {"output_dir": str(tmp_path / "recovery" / "main" / name), "input_root": str(inp), "baseline_blacklist_dir": str(base),
            "blocks": {"pre_period_end": {"block": PRE_END}}}


class _FakeBaseline:
    def __init__(self, d):
        self.manifest_sha256 = _sha(os.path.join(d, "manifest.json"))


def _main_stage(stage, n=6, bl_sha=None, source="cfg"):
    """fixture_stage + the baseline_blacklist.json g4_controls writes on the routed baseline path."""
    out = _fixture_stage(stage, n)
    with open(os.path.join(stage, "baseline_blacklist.json"), "w") as f:
        json.dump({"source": source, "manifest_sha256": bl_sha or "d" * 64, "path": "x", "realpath": "x"}, f)
    return out


def _fake_g4_run(monkeypatch, rc=0, seen=None, mutate=None, bl_source="cfg"):
    def run(cfg):
        if seen is not None:
            seen.update(cfg=cfg, out=g4_controls._OUT_DIR, g0=g4_controls._G0_DIR, g1=g4_controls._G1_DIR, g1b=g4_controls._G1B_DIR)
        out = g4_controls._OUT_DIR
        stage = os.path.join(out, "gen-20260101T000000Z-abcdef12")
        shutil.rmtree(stage, ignore_errors=True)  # real g4 removes crash leftovers the same way
        _main_stage(stage, 6, _sha(os.path.join(cfg["baseline_blacklist_dir"], "manifest.json")), bl_source)
        if mutate:
            mutate()
        with open(os.path.join(stage, "summary.md"), "w") as f:
            f.write("s\n")
        with open(os.path.join(out, "meta.json"), "w") as f:
            f.write("{}")
        _write_ckpts(out)
        if rc == 0:
            os.symlink(os.path.basename(stage), os.path.join(out, "current"))
        return rc
    monkeypatch.setattr(g4_controls, "run", run)
    monkeypatch.setattr(run_all, "_producer_dirty", lambda: [])
    monkeypatch.setattr(run_all.g0_baseline_blacklist, "load_baseline", lambda d, consumer, pinned=None: _FakeBaseline(d))


def test_run_g4_main_retargets_publishes_manifest_last_and_restores(main_routing, tmp_path, monkeypatch):
    base, inp, _ = main_routing
    seen = {}
    _fake_g4_run(monkeypatch, seen=seen)
    before = (g4_controls._OUT_DIR, g4_controls._G0_DIR, g4_controls._G1_DIR, g4_controls._G1B_DIR)
    cfg = _main_cfg(tmp_path, base, inp)
    assert run_all.run_g4_main(cfg) == 0
    final = cfg["output_dir"]
    assert seen["out"] == final + ".work" and seen["g0"] == str(inp / "g0") and seen["g1"] == str(inp / "g1") and seen["g1b"] == str(inp / "g1b")
    assert seen["cfg"]["baseline_blacklist_dir"] == str(base)   # routed through cfg, never the env fallback
    assert before == (g4_controls._OUT_DIR, g4_controls._G0_DIR, g4_controls._G1_DIR, g4_controls._G1B_DIR)
    man = json.load(open(os.path.join(final, "population_manifest.json")))
    assert man["population_id"] == "main" and man["generation_id"] == "gen-M" and man["cutoff_block"] == PRE_END == 15298282
    assert (man["baseline_start_date"], man["baseline_end_date"], man["excluded_dates"]) == ("2022-06-09", "2022-08-07", ["2022-08-08"])
    assert man["input_file_sha256"]["baseline_blacklist_manifest"] == _sha(base / "manifest.json")
    assert set(man["input_file_sha256"]) == {"g0/usdt_events.csv", "g1/cohort.csv", "g1b/frame_survivors.csv", "baseline_blacklist_manifest"}
    assert {"covariates.csv", "wallet_coin_day_pre.csv", "meta.json", "summary.md"} <= set(man["output_file_sha256"])
    assert all(man["output_file_sha256"][n] == _sha(os.path.join(final, n)) for n in man["output_file_sha256"])
    assert man["row_counts"] == {"covariates.csv": 6, "wallet_coin_day_pre.csv": 6 * 120}
    assert os.path.isdir(final + ".work") and not os.path.lexists(final + ".work/current") and not os.path.lexists(final + "/current")
    with pytest.raises(RuntimeError, match="immutable"):   # a published generation is never reused
        run_all.run_g4_main(cfg)


def test_run_g4_main_failed_run_publishes_nothing_and_guards(main_routing, tmp_path, monkeypatch):
    base, inp, _ = main_routing
    _fake_g4_run(monkeypatch, rc=1)
    cfg = _main_cfg(tmp_path, base, inp)
    assert run_all.run_g4_main(cfg) == 1
    assert not os.path.exists(cfg["output_dir"])
    with pytest.raises(RuntimeError, match=r"\.work"):
        run_all.run_g4_main({**cfg, "output_dir": str(tmp_path / "x.work")})
    (tmp_path / "real").mkdir()
    (tmp_path / "recovery" / "main" / "g.work").symlink_to(tmp_path / "real")
    with pytest.raises(RuntimeError, match="symlink"):
        run_all.run_g4_main({**cfg, "output_dir": str(tmp_path / "recovery" / "main" / "g")})
    (inp / "g1b" / "frame_survivors.csv").write_text("")
    with pytest.raises(RuntimeError, match="missing or empty"):
        run_all.run_g4_main({**cfg, "output_dir": str(tmp_path / "recovery" / "main" / "h")})


def test_publish_main_refuses_moved_head_dirty_sources_and_filled_target(tmp_path, monkeypatch):
    ident = rpc.producer_identity(run_all.MAIN_PRODUCER_FILES)
    monkeypatch.setattr(run_all, "_producer_dirty", lambda: [])
    stage = str(tmp_path / "s1")
    _main_stage(stage, 4, "b" * 64)
    work = _work_dir(tmp_path)
    inputs = {"a": "b" * 64, "baseline_blacklist_manifest": "b" * 64}
    with pytest.raises(RuntimeError, match="identity changed"):
        run_all._publish_main(str(tmp_path / "g1"), stage, work, inputs, PRE_END, ("f" * 40) + ":" + ident.split(":")[1])
    monkeypatch.setattr(run_all, "_producer_dirty", lambda: ["run_all.py"])
    with pytest.raises(RuntimeError, match="differ from HEAD"):
        run_all._publish_main(str(tmp_path / "g1"), stage, work, inputs, PRE_END, ident)
    monkeypatch.setattr(run_all, "_producer_dirty", lambda: [])
    (tmp_path / "g2").mkdir()
    (tmp_path / "g2" / "x").write_text("x")
    with pytest.raises(RuntimeError, match="immutable"):
        run_all._publish_main(str(tmp_path / "g2"), stage, work, inputs, PRE_END, ident)
    assert os.path.isdir(stage)


def test_fix1_work_resume_is_bound_to_producer_identity_and_inputs(main_routing, tmp_path, monkeypatch):
    """HIGH: a .work left by another producer identity / other inputs / no binding at all is refused, never resumed."""
    base, inp, _ = main_routing
    _fake_g4_run(monkeypatch, rc=1)   # failed run: leaves .work + its binding behind
    cfg = _main_cfg(tmp_path, base, inp)
    assert run_all.run_g4_main(cfg) == 1
    work = cfg["output_dir"] + ".work"
    bind = json.load(open(os.path.join(work, run_all.MAIN_BINDING)))
    assert bind["producer_identity"] == rpc.producer_identity(run_all.MAIN_PRODUCER_FILES) and "g1/cohort.csv" in bind["inputs"]
    assert run_all.run_g4_main(cfg) == 1   # same identity + inputs: resume allowed
    real_ident = rpc.producer_identity
    monkeypatch.setattr(run_all.rpc, "producer_identity", lambda files: ("f" * 40) + ":" + "0" * 16)
    with pytest.raises(RuntimeError, match="different producer identity/inputs"):
        run_all.run_g4_main(cfg)
    monkeypatch.setattr(run_all.rpc, "producer_identity", real_ident)
    (inp / "g1" / "cohort.csv").write_text("changed\n")
    with pytest.raises(RuntimeError, match="different producer identity/inputs"):
        run_all.run_g4_main(cfg)
    (inp / "g1" / "cohort.csv").write_text("x\n")
    os.unlink(os.path.join(work, run_all.MAIN_BINDING))   # legacy/unattributable checkpoints
    with pytest.raises(RuntimeError, match="unbound checkpoints"):
        run_all.run_g4_main(cfg)


def test_fix1_input_changed_during_run_is_refused_at_publish(main_routing, tmp_path, monkeypatch):
    """MED TOCTOU: inputs/baseline manifest re-hashed at publish; a mid-run edit publishes nothing."""
    base, inp, _ = main_routing
    cfg = _main_cfg(tmp_path, base, inp)
    _fake_g4_run(monkeypatch, mutate=lambda: (inp / "g1b" / "frame_survivors.csv").write_text("edited\n"))
    with pytest.raises(RuntimeError, match="changed during the run"):
        run_all.run_g4_main(cfg)
    assert not os.path.exists(cfg["output_dir"])
    (inp / "g1b" / "frame_survivors.csv").write_text("x\n")
    cfg2 = _main_cfg(tmp_path, base, inp, "gen-N")
    _fake_g4_run(monkeypatch, mutate=lambda: (base / "manifest.json").write_text('{"x": 1}'))
    with pytest.raises(RuntimeError, match="changed during the run"):
        run_all.run_g4_main(cfg2)
    assert not os.path.exists(cfg2["output_dir"])


def test_fix1_published_baseline_blacklist_must_match_attested_manifest(main_routing, tmp_path, monkeypatch):
    base, inp, _ = main_routing
    cfg = _main_cfg(tmp_path, base, inp)
    _fake_g4_run(monkeypatch, bl_source="env:G4_BASELINE_BLACKLIST_DIR")
    with pytest.raises(RuntimeError, match="does not match the attested baseline manifest"):
        run_all.run_g4_main(cfg)
    stage = str(tmp_path / "sx")
    _main_stage(stage, 4, "9" * 64)   # blacklist loaded from a different manifest than attested
    monkeypatch.setattr(run_all, "_producer_dirty", lambda: [])
    with pytest.raises(RuntimeError, match="does not match the attested baseline manifest"):
        run_all._publish_main(str(tmp_path / "gz"), stage, _work_dir(tmp_path), dict(MAIN_INPUTS), PRE_END, rpc.producer_identity(run_all.MAIN_PRODUCER_FILES))
    shutil.rmtree(stage)
    os.makedirs(stage)
    _fixture_stage(str(tmp_path / "sy"), 4)
    shutil.copy(tmp_path / "sy" / "covariates.csv", stage)
    shutil.copy(tmp_path / "sy" / "wallet_coin_day_pre.csv", stage)   # no baseline_blacklist.json at all
    with pytest.raises(RuntimeError, match="lacks baseline_blacklist.json"):
        run_all._publish_main(str(tmp_path / "gz"), stage, _work_dir(tmp_path), dict(MAIN_INPUTS), PRE_END, rpc.producer_identity(run_all.MAIN_PRODUCER_FILES))


def test_fix1_producer_files_cover_every_shaping_source():
    rel = {os.path.relpath(p, ROOT) for p in run_all.MAIN_PRODUCER_FILES}
    assert {"gates/g1_cohort.py", "gates/g1b_frame.py", "gates/g4_june.py", "lib/keccak.py", "lib/blocks.py"} <= rel
    assert all(os.path.isfile(p) for p in run_all.MAIN_PRODUCER_FILES)


@pytest.mark.parametrize("var", ["G4_WALK_ENDPOINT", "G4_AUDIT_MAX", "G4_OUT_DIR", "G4_SAMPLE_CONTROLS", "G4_BASELINE_BLACKLIST_DIR"])
def test_fix1_every_g4_env_var_is_refused_in_run_g4_main_not_only_the_cli(main_routing, tmp_path, monkeypatch, var):
    base, inp, _ = main_routing
    _fake_g4_run(monkeypatch)
    monkeypatch.setenv(var, "1")
    with pytest.raises(RuntimeError, match=var):
        run_all.run_g4_main(_main_cfg(tmp_path, base, inp))
    with pytest.raises(SystemExit):
        run_all.main(["--gate", "g4", "--generation", "gen-E", "--baseline-blacklist-dir", str(base), "--input-dir", str(inp)])


# ---- R presign stage: pre-load attestation + e2e through the CLI --------------------------------------------------------

MAIN_FILES = ("covariates.csv", "wallet_coin_day_pre.csv", "meta.json")


def _rpresign(*args, test_root=True):
    """--main-root is test-only: honoured only with TORNADO_TEST_MAIN_ROOT set (test_root=False drops it, as in production)."""
    env = {**os.environ, "TORNADO_TEST_MAIN_ROOT": "1"} if test_root else {k: v for k, v in os.environ.items() if k != "TORNADO_TEST_MAIN_ROOT"}
    return subprocess.run(["Rscript", "run_all.R", "--stage", "presign", *args], cwd=ROOT, capture_output=True, text=True, timeout=600, env=env)


MAIN_INPUTS = {"g0/usdt_events.csv": "a" * 64, "g1/cohort.csv": "b" * 64, "g1b/frame_survivors.csv": "c" * 64, "baseline_blacklist_manifest": "b" * 64}


def _main_dir(tmp_path, name="gen-P", **override):
    d = tmp_path / "mainroot" / name
    d.mkdir(parents=True)
    for f in MAIN_FILES:
        (d / f).write_text("not,a,valid,csv\n\x00\x01")
    man = {"population_id": "main", "generation_id": name, "producing_commit": HEAD_COMMIT, "producer_source_sha256": "e" * 16, "schema_version": 1,
           "baseline_start_date": "2022-06-09", "baseline_end_date": "2022-08-07", "excluded_dates": ["2022-08-08"],
           "cutoff_block": PRE_END, "input_file_sha256": dict(MAIN_INPUTS),
           "output_file_sha256": {f: _sha(d / f) for f in MAIN_FILES}, "row_counts": {"covariates.csv": 2, "wallet_coin_day_pre.csv": 4}}
    man.update(override)
    (d / "population_manifest.json").write_text(json.dumps({k: v for k, v in man.items() if v is not None}))
    return d


def _write_side_manifest(d, **override):
    """Real side_manifest.json (as gates/presign_side_files.py writes it) over the four side files in `d`; call again after editing one."""
    names = {"direct_freeze.txt": "freeze.txt", "candidates.csv": "cands.csv", "blacklist_any.txt": "bl.txt", "original_windows.csv": "windows.csv"}
    man = {"inputs": {"g1/cohort.csv": {"sha256": MAIN_INPUTS["g1/cohort.csv"]}},
           "outputs": {k: {"sha256": _sha(d / v)} for k, v in names.items()}}
    man.update(override)
    (d / "side_manifest.json").write_text(json.dumps(man))


def _side_files(tmp_path, n=500, rule="none"):
    """Explicit side inputs of the presign CLI (all synthetic): candidates, original windows, any-time blacklist, 38 direct-freeze addresses."""
    d = tmp_path / "side"
    d.mkdir(exist_ok=True)
    (d / "cands.csv").write_text("address,source_membership\n" + "".join("0x%040x,g4\n" % i for i in range(n)) +
                                 "".join("0x%040x,frame_extra\n" % (900000 + i) for i in range(20)))
    (d / "windows.csv").write_text("start_block,end_block\n" + "".join(f"{PRE_START + 1000 * k},{PRE_START + 1000 * k + 399}\n" for k in range(30)))
    (d / "bl.txt").write_text("address\n" + "\n".join("0x%040x" % i for i in (5, 6, 7)) + "\n")
    (d / "freeze.txt").write_text("address\n" + "\n".join("0x%040x" % (800000 + i) for i in range(38)) + "\n")
    _write_side_manifest(d)
    (d / "cohort.schema.json").write_text(json.dumps({"sha256": MAIN_INPUTS["g1/cohort.csv"], "notes": f"participation_rule={rule} (UNRESOLVED upstream, S14; recorded, not defaulted). eligibility cutoff block 1"}))
    return {"--cohort-schema-file": str(d / "cohort.schema.json"), "--candidates-file": str(d / "cands.csv"), "--original-windows-file": str(d / "windows.csv"),
            "--blacklist-any-file": str(d / "bl.txt"), "--direct-freeze-file": str(d / "freeze.txt")}


def _presign_args(indir, outdir, side, rule="none", pid="main", main_root=None):
    a = ["--population-id", pid, "--input-dir", str(indir), "--outdir", str(outdir), "--participation-rule", rule,
         "--main-root", str(main_root or os.path.dirname(str(indir)))]
    return a + [x for kv in side.items() for x in kv]


@needs_r
def test_presign_cli_requires_every_flag_and_valid_rule(tmp_path):
    d = _main_dir(tmp_path)
    side = _side_files(tmp_path)
    full = _presign_args(d, tmp_path / "o", side)
    r = _rpresign()
    assert r.returncode != 0 and "requires --population-id" in r.stderr
    for flag in ("--input-dir", "--outdir", "--participation-rule", "--direct-freeze-file", "--candidates-file", "--original-windows-file", "--blacklist-any-file", "--cohort-schema-file"):
        i = full.index(flag)
        r = _rpresign(*(full[:i] + full[i + 2:]))
        assert r.returncode != 0 and f"requires {flag}" in r.stderr, (flag, r.stderr[-300:])
    r = _rpresign(*_presign_args(d, tmp_path / "o", side, rule="whatever"))
    assert r.returncode != 0 and "--participation-rule must be one of" in r.stderr
    r = _rpresign(*_presign_args(d, tmp_path / "o", side, pid="june_placebo"))
    assert r.returncode != 0 and "--population-id must be `main`" in r.stderr
    assert not (tmp_path / "o").exists()


@needs_r
@pytest.mark.parametrize("override,msg", [
    ({"population_id": "june_placebo"}, "population_id"),
    ({"population_id": "main_smoke"}, "population_id"),
    ({"population_id": None}, "manifest lacks: population_id"),
    ({"producing_commit": "abc"}, "producing_commit"),
    ({"producing_commit": None}, "manifest lacks: producing_commit"),
    ({"producer_source_sha256": "xyz"}, "producer_source_sha256"),
    ({"producer_source_sha256": None}, "manifest lacks: producer_source_sha256"),
    ({"cutoff_block": PRE_END + 1}, "cutoff_block"),
    ({"cutoff_block": JUNE_END}, "cutoff_block"),
    ({"cutoff_block": None}, "manifest lacks: cutoff_block"),
    ({"generation_id": "gen-other"}, "generation_id"),
    ({"baseline_end_date": "2022-06-30"}, "baseline window"),
    ({"excluded_dates": []}, "excluded_dates"),
    ({"input_file_sha256": {"a": "zz"}}, "non-sha256"),
    ({"input_file_sha256": None}, "manifest lacks: input_file_sha256"),
    ({"output_file_sha256": {"covariates.csv": "c" * 64, "wallet_coin_day_pre.csv": "c" * 64, "meta.json": "c" * 64}}, "sha256 differs"),
    ({"output_file_sha256": {"covariates.csv": "c" * 64}}, "lacks"),
])
def test_presign_cli_rejects_bad_manifest_before_csv_load(tmp_path, override, msg):
    d = _main_dir(tmp_path, **override)
    r = _rpresign(*_presign_args(d, tmp_path / "out", _side_files(tmp_path)))
    assert r.returncode != 0 and "MAIN POPULATION REJECTED (before any CSV load)" in r.stderr and msg in r.stderr, r.stderr[-500:]
    assert not (tmp_path / "out").exists()


@needs_r
def test_presign_cli_rejects_absent_manifest_pointer_symlink_and_nonempty_outdir(tmp_path):
    side = _side_files(tmp_path)
    d = _main_dir(tmp_path)
    (d / "population_manifest.json").unlink()
    r = _rpresign(*_presign_args(d, tmp_path / "o1", side))
    assert r.returncode != 0 and "population_manifest.json missing" in r.stderr and "MAIN POPULATION REJECTED" in r.stderr
    (tmp_path / "ptr").symlink_to(_main_dir(tmp_path, "gen-K"))
    r = _rpresign(*_presign_args(tmp_path / "ptr", tmp_path / "o2", side, main_root=tmp_path / "mainroot"))
    assert r.returncode != 0 and "symlink" in r.stderr and "MAIN POPULATION REJECTED" in r.stderr
    (tmp_path / "x" / "current").mkdir(parents=True)
    r = _rpresign(*_presign_args(tmp_path / "x" / "current", tmp_path / "o3", side))
    assert r.returncode != 0 and "current" in r.stderr
    full = tmp_path / "full"
    full.mkdir()
    (full / "x").write_text("x")
    r = _rpresign(*_presign_args(_main_dir(tmp_path, "gen-L"), full, side))
    assert r.returncode != 0 and "not empty" in r.stderr
    t = _main_dir(tmp_path, "gen-T2")
    (t / "covariates.csv").write_text("tampered")
    r = _rpresign(*_presign_args(t, tmp_path / "o4", side))
    assert r.returncode != 0 and "sha256 differs" in r.stderr


@needs_r
def test_main_presign_fixture_end_to_end_cli(tmp_path, monkeypatch):
    """Producer (run_all._publish_main) -> consumer (run_all.R --stage presign): the manifest the gate writes is the one R accepts.

    Equivalent shell command, once the fixture generation `gen-P` and the side files exist:
      Rscript run_all.R --stage presign --population-id main --input-dir <gen-P> --outdir <new dir> --participation-rule none
        --direct-freeze-file F --candidates-file F --original-windows-file F --blacklist-any-file F --cohort-schema-file <g1 cohort.schema.json>
    """
    final = str(tmp_path / "mainroot" / "gen-P")
    stage = str(tmp_path / "stage")
    _main_stage(stage, 500, "b" * 64)
    monkeypatch.setattr(run_all, "_producer_dirty", lambda: [])
    run_all._publish_main(final, stage, _work_dir(tmp_path), dict(MAIN_INPUTS), PRE_END, rpc.producer_identity(run_all.MAIN_PRODUCER_FILES))
    side = _side_files(tmp_path, rule="baseline_transfer_required")
    out = tmp_path / "presign-out"
    (tmp_path / "mismatch").mkdir(exist_ok=True)
    bad = _side_files(tmp_path / "mismatch", rule="none")   # g1 recorded `none`, CLI says baseline_transfer_required -> fail closed
    r0 = _rpresign(*_presign_args(final, tmp_path / "presign-mismatch", bad, rule="baseline_transfer_required"))
    assert r0.returncode != 0 and "participation rule mismatch" in r0.stderr and not (tmp_path / "presign-mismatch").exists()
    wrong = _side_files(tmp_path / "mismatch", rule="baseline_transfer_required")   # right rule, schema sha256 of some other cohort.csv
    sj = json.load(open(wrong["--cohort-schema-file"]))
    sj["sha256"] = "d" * 64
    open(wrong["--cohort-schema-file"], "w").write(json.dumps(sj))
    r0 = _rpresign(*_presign_args(final, tmp_path / "presign-wrongsha", wrong, rule="baseline_transfer_required"))
    assert r0.returncode != 0 and "does not match the verified g1/cohort.csv" in r0.stderr and not (tmp_path / "presign-wrongsha").exists()
    r = _rpresign(*_presign_args(final, out, side, rule="baseline_transfer_required"))
    assert r.returncode == 0, r.stderr[-1500:]
    for f in ("match_membership.csv", "analysis_parameters.json", "eligible_frame_coverage.csv", "cohort_hashes.json", "enlargement_request.json"):
        assert (out / f).exists(), f
    params = json.load(open(out / "analysis_parameters.json"))
    assert params["population_id"] == "main" and params["generation_id"] == "gen-P"
    assert params["transfer_participation_rule"] == "baseline_transfer_required"
    assert params["population_manifest"]["cutoff_block"] == PRE_END
    r2 = _rpresign(*_presign_args(final, out, side, rule="baseline_transfer_required"))   # outputs are immutable: second run into the same dir is refused
    assert r2.returncode != 0 and "not empty" in r2.stderr
    with open(os.path.join(final, "covariates.csv"), "a") as f:   # post-hoc edit of an output is caught by the hash check
        f.write("\n")
    r3 = _rpresign(*_presign_args(final, tmp_path / "presign-out2", side, rule="baseline_transfer_required"))
    assert r3.returncode != 0 and "sha256 differs" in r3.stderr


@needs_r
def test_s22p_side_files_from_gate_outputs_feed_the_presign_stage(tmp_path, monkeypatch):
    """gates/presign_side_files.py (synthetic g0/g1/g1b + baseline) -> the four side files -> run_all.R --stage presign passes."""
    import csv
    import test_presign_side_files as sf
    from gates import presign_side_files as psf
    final = _published_gen(tmp_path, monkeypatch, "gen-S")
    rows = list(csv.DictReader(open(os.path.join(final, "covariates.csv"))))
    cohort = {r["address"]: r["arm"] for r in rows if r["arm"] != "control"}
    frame = [r["address"] for r in rows if r["arm"] == "control"] + ["0x%040x" % (900000 + i) for i in range(20)]
    root = sf.write_inputs(tmp_path / "gates", cohort_arms=cohort, survivors=frame)
    base = sf.write_baseline(tmp_path / "bl")
    monkeypatch.setattr(psf, "_producer_dirty", lambda: [])
    monkeypatch.setattr(psf, "OUT_ROOT", str(tmp_path))
    monkeypatch.setattr(psf.g0_baseline_blacklist, "load_baseline", sf.fake_baseline(
        [("USDC", "0x%040x" % i, "add", PRE_START - 100, 0) for i in (5, 6, 7)]))
    side_dir = tmp_path / "side-out"
    psf.produce(root, base, str(side_dir), skip_window_receipts_check=True)   # no g1b receipts in this synthetic root
    # synthetic glue: the fixture population's g1/cohort.csv hash is a stand-in, so align the side manifest's recorded hash with it
    sm = json.load(open(side_dir / "side_manifest.json"))
    sm["inputs"]["g1/cohort.csv"]["sha256"] = MAIN_INPUTS["g1/cohort.csv"]
    (side_dir / "side_manifest.json").write_text(json.dumps(sm))
    side = {**_side_files(tmp_path, rule="none"), "--candidates-file": str(side_dir / "candidates.csv"), "--original-windows-file": str(side_dir / "original_windows.csv"),
            "--blacklist-any-file": str(side_dir / "blacklist_any.txt"), "--direct-freeze-file": str(side_dir / "direct_freeze.txt")}
    out = tmp_path / "presign-s22p"
    r = _rpresign(*_presign_args(final, out, side, rule="none"))
    assert r.returncode == 0, r.stderr[-1500:]
    for f in ("match_membership.csv", "eligible_frame_coverage.csv", "enlargement_request.json"):
        assert (out / f).exists(), f
    cov = list(csv.DictReader(open(out / "eligible_frame_coverage.csv")))
    assert len(cov) == 500 + 20 and {c["source_membership"] for c in cov} == {"g1_withdrawal", "g1_deposit", "g1_both", "g1b_frame"}
    assert {c["address"]: c["screen_reason"] for c in cov}["0x%040x" % 5] == "baseline_blacklist"


# ---- fix round 1 (S20b security review): R-side attestation / path / publish hardening ----------------------------------

def _published_gen(tmp_path, monkeypatch, name="gen-F", n=500, row_counts=None):
    final = str(tmp_path / "mainroot" / name)
    stage = str(tmp_path / f"stage-{name}")
    _main_stage(stage, n, "b" * 64)
    monkeypatch.setattr(run_all, "_producer_dirty", lambda: [])
    run_all._publish_main(final, stage, _work_dir(tmp_path), dict(MAIN_INPUTS), PRE_END, rpc.producer_identity(run_all.MAIN_PRODUCER_FILES))
    if row_counts:
        mp = os.path.join(final, "population_manifest.json")
        m = json.load(open(mp))
        m["row_counts"] = row_counts
        json.dump(m, open(mp, "w"))
    return final


@needs_r
@pytest.mark.parametrize("override,msg", [
    ({"input_file_sha256": {k: v for k, v in MAIN_INPUTS.items() if k != "g0/usdt_events.csv"}}, "input_file_sha256 keys must be exactly"),
    ({"input_file_sha256": {**MAIN_INPUTS, "extra": "e" * 64}}, "input_file_sha256 keys must be exactly"),
    ({"input_file_sha256": {"g1/cohort.csv": "b" * 64}}, "input_file_sha256 keys must be exactly"),
    ({"schema_version": 2}, "schema_version"),
    ({"schema_version": None}, "manifest lacks: schema_version"),
    ({"producing_commit": "b" * 40}, "not a commit of this repository"),
])
def test_fix1_r_exact_input_keys_schema_and_commit_exists(tmp_path, override, msg):
    d = _main_dir(tmp_path, **override)
    r = _rpresign(*_presign_args(d, tmp_path / "out", _side_files(tmp_path)))
    assert r.returncode != 0 and "MAIN POPULATION REJECTED (before any CSV load)" in r.stderr and msg in r.stderr, r.stderr[-500:]
    assert not (tmp_path / "out").exists()


@needs_r
def test_fix1_r_every_listed_output_is_hashed_and_unlisted_files_rejected(tmp_path):
    side = _side_files(tmp_path)
    d = _main_dir(tmp_path, "gen-U")
    (d / "extra.csv").write_text("x")   # present but not in output_file_sha256
    r = _rpresign(*_presign_args(d, tmp_path / "o1", side))
    assert r.returncode != 0 and "not listed in output_file_sha256: extra.csv" in r.stderr
    (d / "extra.csv").unlink()
    (d / "summary.md").write_text("s")
    man = json.load(open(d / "population_manifest.json"))
    man["output_file_sha256"]["summary.md"] = "c" * 64   # a listed non-required entry with a wrong hash
    json.dump(man, open(d / "population_manifest.json", "w"))
    r = _rpresign(*_presign_args(d, tmp_path / "o2", side))
    assert r.returncode != 0 and "summary.md sha256 differs" in r.stderr
    man["output_file_sha256"]["../escape"] = "c" * 64
    json.dump(man, open(d / "population_manifest.json", "w"))
    r = _rpresign(*_presign_args(d, tmp_path / "o3", side))
    assert r.returncode != 0 and "invalid or duplicate entry name" in r.stderr
    assert not any((tmp_path / o).exists() for o in ("o1", "o2", "o3"))


@needs_r
def test_fix1_r_input_dir_must_be_a_generation_directly_under_the_main_root(tmp_path):
    side = _side_files(tmp_path)
    d = _main_dir(tmp_path, "gen-R")
    r = _rpresign(*_presign_args(d, tmp_path / "o", side, main_root=tmp_path / "elsewhere"))
    assert r.returncode != 0 and "not a generation directly under the main root" in r.stderr
    r = _rpresign(*_presign_args(d, tmp_path / "o", side, main_root=d))   # the generation itself is not a root containing generations
    assert r.returncode != 0 and "not a generation directly under the main root" in r.stderr


@needs_r
def test_fix1_r_outdir_path_and_containment_checks(tmp_path):
    side = _side_files(tmp_path)
    d = _main_dir(tmp_path, "gen-O")
    root = tmp_path / "mainroot"
    (tmp_path / "recovery" / "june").mkdir(parents=True)
    (tmp_path / "afile").write_text("x")
    (tmp_path / "realdir").mkdir()
    (tmp_path / "linkdir").symlink_to(tmp_path / "realdir")
    cases = [(root / "gen-new", "inside the immutable population root"),
             (root / "gen-O", "inside the immutable population root"),
             (tmp_path / "recovery" / "june" / "gen-x", "inside the immutable population root"),
             (tmp_path / "afile", "not a directory"),
             (str(tmp_path / "sub" / ".." / "oo"), "`..`"),
             (tmp_path / "linkdir" / "oo", "symlink"),
             (tmp_path / "current", "current")]
    main_root = tmp_path / "recovery" / "main"
    (main_root).mkdir()
    import shutil as _sh
    _sh.copytree(d, main_root / "gen-O")
    for out, msg in cases:
        a = _presign_args(main_root / "gen-O", out, side, main_root=main_root)
        if "june" in str(out) or str(out).startswith(str(root)):
            a = _presign_args(main_root / "gen-O", out, side, main_root=main_root if "june" in str(out) else root)
        r = _rpresign(*a)
        assert r.returncode != 0 and msg in r.stderr, (str(out), r.stderr[-300:])
    assert not (tmp_path / "oo").exists() and not (tmp_path / "current").exists()


@needs_r
def test_fix1_r_side_files_get_path_checks(tmp_path):
    side = _side_files(tmp_path)
    d = _main_dir(tmp_path, "gen-S")
    real = tmp_path / "side" / "bl.txt"
    (tmp_path / "side" / "lnk.txt").symlink_to(real)
    (tmp_path / "cur").mkdir()
    (tmp_path / "cur" / "current").mkdir()
    for key, bad, msg in (("--blacklist-any-file", str(tmp_path / "side" / "lnk.txt"), "symlink"),
                          ("--direct-freeze-file", str(tmp_path / "cur" / "current" / "f.txt"), "current"),
                          ("--candidates-file", os.path.join(ROOT, "out", "g0", "usdt_events.csv"), "historical out/"),
                          ("--original-windows-file", str(tmp_path / "side" / ".." / "side" / "windows.csv"), "`..`"),
                          ("--candidates-file", str(tmp_path / "side"), "not a regular file")):
        r = _rpresign(*_presign_args(d, tmp_path / "o", {**side, key: bad}))
        assert r.returncode != 0 and "MAIN POPULATION REJECTED" in r.stderr and msg in r.stderr, (key, r.stderr[-300:])
    assert not (tmp_path / "o").exists()


@needs_r
def test_side_manifest_is_required_and_binds_every_side_file(tmp_path):
    d = _main_dir(tmp_path, "gen-SM")
    side = _side_files(tmp_path)
    sd = tmp_path / "side"

    def run(tag):
        r = _rpresign(*_presign_args(d, tmp_path / tag, side))
        assert r.returncode != 0 and not (tmp_path / tag).exists(), r.stderr[-400:]
        return r.stderr
    good = (sd / "side_manifest.json").read_text()
    (sd / "side_manifest.json").unlink()
    assert "side_manifest" in run("o-absent")
    for i, bad in enumerate(('{"outputs":{}}', "[1]", "{not json", '{"inputs":{"g1/cohort.csv":{}}}')):
        (sd / "side_manifest.json").write_text(bad)
        assert "side_manifest.json" in run(f"o-bad{i}"), bad
    man = json.loads(good)
    man["inputs"]["g1/cohort.csv"]["sha256"] = "B" * 64   # not lowercase 64-hex
    (sd / "side_manifest.json").write_text(json.dumps(man))
    assert "valid inputs[g1/cohort.csv].sha256" in run("o-badsha")
    (sd / "side_manifest.json").write_text(good)
    for name, f in (("direct_freeze_file", "freeze.txt"), ("candidates_file", "cands.csv"), ("blacklist_any_file", "bl.txt"), ("original_windows_file", "windows.csv")):
        orig = (sd / f).read_text()
        (sd / f).write_text(orig + "\n")   # edited after the manifest was written
        assert f"side file {name} differs from side_manifest.json" in run(f"o-edit-{name}")
        (sd / f).write_text(orig)
    man = json.loads(good)
    del man["outputs"]["candidates.csv"]
    (sd / "side_manifest.json").write_text(json.dumps(man))
    assert "outputs[candidates.csv]" in run("o-missing-out")


@needs_r
def test_fix1_r_row_count_mismatch_rejected_before_run_presign_and_failed_run_leaves_no_partial_outdir(tmp_path, monkeypatch):
    side = _side_files(tmp_path)
    bad = _published_gen(tmp_path, monkeypatch, "gen-RC", row_counts={"covariates.csv": 499, "wallet_coin_day_pre.csv": 60000})
    out = tmp_path / "rc-out"
    r = _rpresign(*_presign_args(bad, out, side))
    assert r.returncode != 0 and "row count differs from the population manifest" in r.stderr and "MAIN POPULATION REJECTED" in r.stderr
    assert not out.exists() and not list(tmp_path.glob("rc-out*"))
    good = _published_gen(tmp_path, monkeypatch, "gen-OK")
    (tmp_path / "side" / "cands.csv").write_text("address,source_membership\n0x%040x,g4\n" % 1)   # incomplete candidate universe: run_presign fails midway
    _write_side_manifest(tmp_path / "side")
    out2 = tmp_path / "partial-out"
    r = _rpresign(*_presign_args(good, out2, side))
    assert r.returncode != 0 and "candidates incomplete" in r.stderr
    assert not out2.exists() and not list(tmp_path.glob("partial-out*"))   # no outdir and no temp sibling left behind


@needs_r
def test_fix2_n1_relative_nonexistent_outdir_cannot_dodge_containment(tmp_path):
    """N1: a relative, not-yet-existing outdir under the real out/recovery/{main,june} or out/g4 was returned unchanged by
    normalizePath(mustWork = FALSE) and passed. Nothing is created: all of these are rejected before any filesystem write."""
    side = _side_files(tmp_path)
    d = _main_dir(tmp_path, "gen-N1")
    for rel, msg in (("out/recovery/main/newgen-fix2", "inside the immutable population root"),
                     ("out/recovery/june/x-fix2", "inside the immutable population root"),
                     ("out/g4/x-fix2", "historical out/")):
        r = _rpresign(*_presign_args(d, rel, side))   # --main-root relocated to tmp: the production roots must stay forbidden
        assert r.returncode != 0 and msg in r.stderr, (rel, r.stderr[-300:])
        assert not os.path.exists(os.path.join(ROOT, rel))


@needs_r
def test_fix2_n2_main_root_override_is_test_only_and_never_unlocks_production_roots(tmp_path):
    side = _side_files(tmp_path)
    d = _main_dir(tmp_path, "gen-N2")
    r = _rpresign(*_presign_args(d, tmp_path / "o", side), test_root=False)
    assert r.returncode != 0 and "--main-root is test-only" in r.stderr and not (tmp_path / "o").exists()
    # even with the test override, an absolute outdir inside the production main/june roots is refused
    for sub in ("recovery/main/gen-n2-abs", "recovery/june/gen-n2-abs"):
        r = _rpresign(*_presign_args(d, os.path.join(ROOT, "out", sub), side))
        assert r.returncode != 0 and "inside the immutable population root" in r.stderr
        assert not os.path.exists(os.path.join(ROOT, "out", sub))


def test_fix2_n4_work_binding_includes_config_hashes(main_routing, tmp_path, monkeypatch):
    """N4: editing config/blocks.json or config/endpoints.json between a failed run and its resume is refused."""
    base, inp, _ = main_routing
    cfgs = []
    for name in ("blocks.json", "endpoints.json"):
        p = tmp_path / ("cfg-" + name)
        p.write_text("{}\n")
        cfgs.append(str(p))
    monkeypatch.setattr(run_all, "MAIN_CONFIG_FILES", tuple(cfgs))
    _fake_g4_run(monkeypatch, rc=1)
    cfg = _main_cfg(tmp_path, base, inp)
    assert run_all.run_g4_main(cfg) == 1
    bind = json.load(open(os.path.join(cfg["output_dir"] + ".work", run_all.MAIN_BINDING)))
    assert set(bind["config"]) == {"cfg-blocks.json", "cfg-endpoints.json"}
    assert run_all.run_g4_main(cfg) == 1
    for p in cfgs:
        old = open(p).read()
        open(p, "w").write('{"edited": 1}\n')
        with pytest.raises(RuntimeError, match="different producer identity/inputs"):
            run_all.run_g4_main(cfg)
        open(p, "w").write(old)


def test_publish_main_records_observed_endpoints_and_refuses_mixed_checkpoints(tmp_path, monkeypatch):
    monkeypatch.setattr(run_all, "_producer_dirty", lambda: [])
    ident = rpc.producer_identity(run_all.MAIN_PRODUCER_FILES)
    stage = str(tmp_path / "s1")
    _main_stage(stage, 4, "b" * 64)
    inputs = {"a": "b" * 64, "baseline_blacklist_manifest": "b" * 64}
    work = _work_dir(tmp_path)
    for walk_eps, rv in ((("mevblocker", "tenderly"), (("mevblocker", "tenderly"),)),   # mixed walk endpoints
                         (("mevblocker",), (("tenderly", "mevblocker"),)),             # reverify from another provider pair
                         ((), (("mevblocker", "tenderly"),))):                         # empty walk
        _write_ckpts(work, walk_eps, rv)
        with pytest.raises(RuntimeError, match="refusing to publish"):
            run_all._publish_main(str(tmp_path / "g1"), stage, work, inputs, PRE_END, ident)
        assert not os.path.exists(tmp_path / "g1")
    _write_ckpts(work)
    os.unlink(os.path.join(work, "reverify_checkpoint.jsonl"))
    with pytest.raises(RuntimeError, match="missing"):
        run_all._publish_main(str(tmp_path / "g1"), stage, work, inputs, PRE_END, ident)
    _write_ckpts(work)
    run_all._publish_main(str(tmp_path / "g1"), stage, work, inputs, PRE_END, ident)
    man = json.load(open(tmp_path / "g1" / "population_manifest.json"))
    assert (man["walk_endpoint"], man["replay_endpoint"]) == ("mevblocker", "tenderly")


def test_june_manifest_records_endpoints_refuses_mixed_and_g4_env(tmp_path, monkeypatch):
    monkeypatch.setattr(g4_june, "_producer_dirty", lambda: [])
    ident = rpc.producer_identity(g4_june.PRODUCER_FILES)
    stage = str(tmp_path / "stage")
    _fixture_stage(stage, 10)
    work = _work_dir(tmp_path)
    _write_ckpts(work, ("mevblocker", "tenderly"))
    with pytest.raises(RuntimeError, match="refusing to publish"):
        g4_june._publish(str(tmp_path / "gen-j"), stage, work, True, {"manifest.csv": "b" * 64}, JUNE_END, 10, 1200, ident, participation_rule=RULE, participation_window=WIN)
    _write_ckpts(work)
    g4_june._publish(str(tmp_path / "gen-j"), stage, work, True, {"manifest.csv": "b" * 64}, JUNE_END, 10, 1200, ident, participation_rule=RULE, participation_window=WIN)
    man = json.load(open(tmp_path / "gen-j" / "population_manifest.json"))
    assert (man["walk_endpoint"], man["replay_endpoint"]) == ("mevblocker", "tenderly")
    monkeypatch.setenv("G4_WALK_ENDPOINT", "tenderly")
    with pytest.raises(RuntimeError, match="G4_WALK_ENDPOINT"):
        g4_june.run({"output_dir": str(tmp_path / "g2"), "input_root": str(tmp_path)})
    assert not os.path.exists(tmp_path / "g2.work")

# ---- S20: `--gate g5` is fail-closed: signature / signed config / signed input mapping before ANY data access or RPC ------------

import builtins

import test_g5_post as t5
from gates import g5_post


@pytest.fixture
def g5env(tmp_path, monkeypatch):
    """Throwaway git repo as REPO_ROOT; every way a post run could touch the chain or its inputs is instrumented."""
    repo = tmp_path / "repo"
    repo.mkdir()
    t5._git(repo, "init", "-q")
    monkeypatch.setattr(g5_post, "REPO_ROOT", str(repo))
    monkeypatch.setattr(g5_post, "installed_r_version", lambda p: t5.FAKE_R_VERSIONS.get(p))   # the fixtures pin FAKE_R_VERSIONS; no Rscript here
    monkeypatch.setattr(g5_post, "installed_r_string", lambda: t5.FAKE_R_STRING)
    seen = {"chain": [], "live": [], "frames": [], "opened": []}
    monkeypatch.setattr(run_all.rpc, "assert_chain_id", lambda ep: seen["chain"].append(ep) or 1)
    monkeypatch.setattr(g5_post, "LiveRpc", lambda *a, **k: seen["live"].append(a) or (_ for _ in ()).throw(AssertionError("RPC object constructed")))
    real_load = g5_post._load_frame
    monkeypatch.setattr(g5_post, "_load_frame", lambda *a, **k: seen["frames"].append(a) or real_load(*a, **k))
    real_open = builtins.open

    def spy(path, *a, **k):
        seen["opened"].append(os.fspath(path) if isinstance(path, (str, bytes, os.PathLike)) else path)
        return real_open(path, *a, **k)
    monkeypatch.setattr(builtins, "open", spy)
    return repo, seen


def _g5_denied(cfg_path, seen, capsys, canaries=()):
    assert run_all.main(["--gate", "g5", "--config", cfg_path]) == 3
    assert "g5 DENIED" in capsys.readouterr().out
    assert seen["chain"] == [] and seen["live"] == [] and seen["frames"] == []     # no RPC, no RPC object, no frame parsed
    assert not [p for p in seen["opened"] if str(p) in {str(c) for c in canaries}]  # no input file was even opened


def test_g5_cli_requires_config_and_rejects_it_elsewhere(g5env):
    for argv in (["--gate", "g5"], ["--gate", "g0", "--config", "x"], ["--gate", "g5", "--config", "x", "--generation", "g1"]):
        with pytest.raises(SystemExit) as e:
            run_all.main(argv)
        assert e.value.code == 2, argv
    assert g5env[1]["chain"] == []


def test_g5_cli_denies_missing_signature_before_any_data_access(g5env, tmp_path, capsys):
    repo, seen = g5env
    cfg_path, cfg, p = t5.build(tmp_path)
    seen["opened"].clear()   # building the fixture wrote the inputs; only reads from here on count
    (p["sd"] / "signing.json").unlink()
    t5.commit_all(repo)
    pop = cfg["populations"][0]
    _g5_denied(cfg_path, seen, capsys, canaries=[pop["frame_path"], pop["wallet_map_path"]])


def test_g5_cli_denies_when_nothing_was_ever_signed(g5env, tmp_path, capsys):
    repo, seen = g5env
    cfg_path, cfg, p = t5.build(tmp_path, commit=False)   # signed files exist on disk but are not committed
    seen["opened"].clear()
    pop = cfg["populations"][0]
    _g5_denied(cfg_path, seen, capsys, canaries=[pop["frame_path"], pop["wallet_map_path"]])


@pytest.mark.parametrize("victim", ["prespec.md", "manifest.json", "g5_config.json"])
def test_g5_cli_denies_mismatched_signature(g5env, tmp_path, capsys, victim):
    repo, seen = g5env
    cfg_path, cfg, p = t5.build(tmp_path)
    seen["opened"].clear()   # building the fixture wrote the inputs; only reads from here on count
    (p["sd"] / victim).write_text("tampered after signing\n")
    t5.commit_all(repo)                                  # committed but no longer matches its SHA256 entry
    pop = cfg["populations"][0]
    _g5_denied(cfg_path, seen, capsys, canaries=[pop["frame_path"], pop["wallet_map_path"]])


def test_g5_cli_denies_run_config_that_is_not_the_signed_config(g5env, tmp_path, capsys):
    repo, seen = g5env
    cfg_path, cfg, p = t5.build(tmp_path)
    seen["opened"].clear()   # building the fixture wrote the inputs; only reads from here on count
    other = tmp_path / "other_config.json"
    other.write_text(json.dumps({**cfg, "post_end_block": cfg["post_end_block"] + 1}))
    pop = cfg["populations"][0]
    assert run_all.main(["--gate", "g5", "--config", str(other)]) == 3
    assert seen["chain"] == [] and seen["live"] == [] and seen["frames"] == []
    assert not [x for x in seen["opened"] if str(x) in {pop["frame_path"], pop["wallet_map_path"]}]


def test_g5_cli_denies_missing_config_file(g5env, tmp_path, capsys):
    assert run_all.main(["--gate", "g5", "--config", str(tmp_path / "nope.json")]) == 3
    assert g5env[1]["chain"] == [] and g5env[1]["live"] == []


def test_g5_cli_with_valid_signature_reaches_the_post_gate_with_an_injected_port_only(g5env, tmp_path, monkeypatch, capsys):
    """Positive control: a signed fixture passes verification and reaches extraction; the only RPC object is the one the test injects."""
    repo, seen = g5env
    cfg_path, cfg, p = t5.build(tmp_path)
    seen["opened"].clear()   # building the fixture wrote the inputs; only reads from here on count
    real_run = g5_post.run
    monkeypatch.setattr(g5_post, "run", lambda path: real_run(path, t5.good_port(), reverifier=lambda a: {}))
    assert run_all.main(["--gate", "g5", "--config", cfg_path]) == 0
    assert seen["chain"] == [] and seen["live"] == [] and len(seen["frames"]) == 1
    assert os.path.exists(os.path.join(cfg["out_dir"], "post_wallet_coin_day.csv"))


def test_g5_cli_exit_codes_are_truthful_about_the_phase(g5env, tmp_path, monkeypatch, capsys):
    """3 = denied before any data/RPC; 4 = failed after data access/RPC began (never claims 'no RPC'); no raw traceback-only exits."""
    cfg = tmp_path / "c.json"
    cfg.write_text("{}")

    def raiser(exc):
        def f(path):
            raise exc
        return f
    pre = g5_post.SignatureError("x")
    pre.pre_access = True
    cases = [(pre, 3, "DENIED"), (g5_post.CheckpointError("binding"), 3, "DENIED"), (g5_post.PreAccessDenied("env"), 3, "DENIED"),
             (g5_post.SpanError("late span failure"), 4, "FAILED after"), (g5_post.InputMismatch("late"), 4, "FAILED after"),
             (g5_post.ReverifyError("mismatch"), 4, "FAILED after"), (g5_post.CheckpointCorrupt("cache"), 4, "FAILED after"),
             (RuntimeError("endpoint down"), 4, "FAILED after")]
    for exc, code, word in cases:
        monkeypatch.setattr(g5_post, "run", raiser(exc))
        assert run_all.main(["--gate", "g5", "--config", str(cfg)]) == code, exc
        out = capsys.readouterr().out
        assert word in out and (code == 4) == ("no post data read" not in out)
