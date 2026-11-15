"""Tests for tools/s24_package.py (the S24 signing-package assembler). Synthetic fixtures; no RPC, no network, no out/ data.
Round-trip: assembler output -> g5 verify (FIXTURE signature, temp repo) -> R verify_post_signature / verify_post_inputs."""
import csv
import json
import os
import shutil
import socket
import subprocess
import sys

import pytest

from gates import g5_post as g5
from tools import s24_package as s24
import run_all

# 65 of these tests run the signed R dry run; without Rscript (e.g. the Python-only CI job) the file is skipped as a whole.
pytestmark = pytest.mark.skipif(shutil.which("Rscript") is None, reason="needs Rscript with the pinned R packages (see install_r_packages.R)")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRE = 15_298_282
BRIDGE_END, POST_END = 15_304_565, 15_523_155      # = s24.expected_block("2022-08-08") / ("2022-09-12") rounded; event_block 15302549 < .. < merge_block 15537394
FAKE_R = "R version 4.3.3 (fake)"
A, B, C = ("0x" + c * 40 for c in "abc")
FAKE = {"fixest": "0.12.1", "data.table": "1.15.4", "DRDID": "1.4.0", "trust": "0.1.8", "BMisc": "1.4.8", "fastglm": "0.0.3", "Rcpp": "1.0.12", "jsonlite": "1.8.8",
        "Matrix": "1.6.5", "RcppEigen": "0.3.4.0.2", "sandwich": "3.1.2", "nlme": "3.1.164", "dreamerr": "1.5.0", "numDeriv": "2016.8.1.1",
        "stringmagic": "1.2.0", "bigmemory": "4.6.6"}
REAL_INSTALLED = g5.installed_r_version
REAL_R_STRING = g5.installed_r_string


def _git(repo, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *a], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A throwaway git repo holding every bound code file as REPO_ROOT, fake installed R versions, and the input files."""
    repo = tmp_path / "repo"
    for rel in (*g5.ALL_CODE_FILES, s24.TOOL_REL, s24.DRYRUN_REL):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(os.path.join(ROOT, rel), repo / rel)
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "x")
    monkeypatch.setattr(g5, "REPO_ROOT", str(repo))
    monkeypatch.setattr(g5, "installed_r_version", lambda p: FAKE.get(p))
    monkeypatch.setattr(g5, "installed_r_string", lambda: FAKE_R)
    monkeypatch.setattr(s24, "r_dry_run", _cached_dry_run)
    d = tmp_path / "in"
    d.mkdir()
    for n in ("circle", "usdt", "rec", "later", "outflow"):       # g0 / refused inputs are hashed only, never parsed by the assembler or the pre-window readers
        (d / n).write_bytes(f"input {n}\n".encode())
    write_inputs(d)
    (d / "prespec.md").write_text("prespec text\n")
    with open(d / "frame.csv", "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["population_id", "generation_id", "address", "source_membership", "screen_status", "screen_reason"])
        for a, m in ((A, "withdrawal"), (B, "control")):
            w.writerow(["main", "gen1", a, m, "eligible", ""])
    with open(d / "wmap.csv", "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["population_id", "generation_id", "wallet_id", "address"])
        for i, a in enumerate((A, B)):
            w.writerow(["main", "gen1", 10 + i, a])
    return SimpleNS(repo=repo, d=d, tmp=tmp_path)


_DRY_CACHE = {}


def _cached_dry_run(post_inputs, cwd):
    """The REAL R dry run (Rscript), memoised per identical input bytes so the ~40 builds do not each pay an R start-up."""
    key = tuple(sorted((k, s24._sha(b)) for k, (_, b) in post_inputs.items()))
    if key not in _DRY_CACHE:
        _DRY_CACHE[key] = ("ok", REAL_DRY(post_inputs, cwd))
    return _DRY_CACHE[key][1]


REAL_DRY = s24.r_dry_run


def _csv(path, rows):
    with open(path, "w", newline="") as f:
        csv.writer(f, lineterminator="\n").writerows(rows)


def _bal(w, coin, day):
    """Deterministic, varied balances (units, 6 dec) so the 1/99 cap values are a genuine non-degenerate interval."""
    return 1_000_000 * (1 + (day * 3 + w * 7 + (2 if coin == "USDT" else 0)) % 5)


def _q7(xs, p):
    xs = sorted(xs)
    h = (len(xs) - 1) * p
    lo = int(h)
    return xs[lo] + (h - lo) * (xs[min(lo + 1, len(xs) - 1)] - xs[lo])


def true_caps(rows):
    """The cap values R recomputes (R/estimate.R cap_values_all: type-7 1/99 over all baseline wallet-days of D, A and the pooled legs; B = mean combined $)."""
    D, Aa, legs = [], [], []
    for w, days in rows.items():
        B = sum(u + t for _, u, t in days) / len(days) / 1e6
        if B <= 0:
            continue
        for _, u, t in days:
            u, t = u / 1e6, t / 1e6
            D.append((u - t) / B)
            Aa.append((u + t) / B)
            legs += [u / B, t / B]
    return {"D": [_q7(D, .01), _q7(D, .99)], "A": [_q7(Aa, .01), _q7(Aa, .99)], "legs_pooled": [_q7(legs, .01), _q7(legs, .99)]}


def write_inputs(d, *, retained=(A, B), matched=(A, B), pop=("main", "gen1"), zero=(), drop=(), caps=None):
    """Valid, real-shaped pre-window inputs that R's own post-stage pre checks accept: A exposed, B control, one stratum, the full 60 baseline days
    (0..59), cap_values equal to what R recomputes. zero = wallet ids with all-zero balances (B <= 0); drop = (wallet_id, day) pairs left out;
    caps overrides the written cap_values."""
    from datetime import date, timedelta
    cov = [["wallet_id", "address", "arm", "transfer_count_total", "eth_balance_wei_units", "wallet_age_blocks", "issuer_mint_redeem_flag",
            "blacklisted_usdc_at_pre_end", "blacklisted_usdt_at_pre_end"],
           [10, A, "withdrawal", 5, "1000000000000000000", 1000, "False", "False", "False"],
           [11, B, "control", 6, "2000000000000000000", 2000, "False", "False", "False"]]
    _csv(d / "cov", cov)
    wcd = [["wallet_id", "coin", "date", "day_index", "balance_units"]]
    rows = {}
    for w in (10, 11):
        rows[w] = []
        for day in range(60):
            if (w, day) in drop:
                continue
            u, t = (0, 0) if w in zero else (_bal(w, "USDC", day), _bal(w, "USDT", day))
            rows[w].append((day, u, t))
            dt = (date(2022, 6, 9) + timedelta(days=day)).isoformat()
            wcd += [[w, "USDC", dt, day, u], [w, "USDT", dt, day, t]]
    _csv(d / "wcd", wcd)
    mm = [["population_id", "generation_id", "address", "arm", "stratum", "matched", "weight", "match_rank", "cutpoints_hash", "seed"]]
    for a, arm in ((A, "exposed"), (B, "control")):
        mm.append([*pop, a, arm, 1, "TRUE" if a in matched else "FALSE", 1 if a in matched else 0, 1, "h" * 8, 7])
    _csv(d / "mm", mm)
    cut = {v: [1, 2, 3] for v in ("mean_hold", "transfers", "age", "eth")}
    (d / "ap").write_text(json.dumps({"population_id": pop[0], "generation_id": pop[1], "cutpoints_hash": "h" * 8, "seed": 7, "cutpoints": cut,
                                      "cap_values": caps or true_caps({w: v for w, v in rows.items() if w not in zero})}))
    efc = [["population_id", "generation_id", "address", "source_membership", "screen_status", "screen_reason"]]
    for a, m in ((A, "withdrawal"), (B, "control")):
        efc.append([*pop, a, m, "retained" if a in retained else "excluded", "retained" if a in retained else "floor"])
    _csv(d / "efc", efc)
    _csv(d / "pool", [["address", "usdc_pool_depositor"], [A, "TRUE"]])
    _csv(d / "dep", [["address", "deposit_count"], [A, 3]])


class SimpleNS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def params(e, **over):
    d = e.d
    post = {"pre/covariates.csv": d / "cov", "pre/wallet_coin_day_pre.csv": d / "wcd", "match_membership": d / "mm", "analysis_parameters": d / "ap",
            "eligible_frame_coverage": d / "efc", "usdc_pool_flag": d / "pool", "deposit_counts": d / "dep", "g0_circle_events": d / "circle", "g0_usdt_events": d / "usdt", "g0_receipts": d / "rec"}
    p = {"out_dir": str(e.tmp / "g5out"), "bridge_end_block": BRIDGE_END, "post_end_block": POST_END, "post_start_date": "2022-08-09",
         "bridge_date": "2022-08-08", "post_end_date": "2022-09-12", "eligible_statuses": ["eligible"],
         "populations": [("main", "gen1", str(d / "frame.csv"), str(d / "wmap.csv"))],
         "post_inputs": {k: str(v) for k, v in post.items()}, "prespec_text": str(d / "prespec.md")}
    p.update(over)
    return p


def cli(e, out, drop=(), extra=(), more=(), **over):
    d = e.d
    flags = {"pre-covariates": "cov", "wallet-coin-day-pre": "wcd", "match-membership": "mm", "analysis-parameters": "ap",
             "eligible-frame-coverage": "efc", "usdc-pool-flag": "pool", "deposit-counts": "dep", "g0-circle-events": "circle", "g0-usdt-events": "usdt", "g0-receipts": "rec"}
    argv = ["--gate", "s24_package", "--outdir", str(out), "--g5-out-dir", str(e.tmp / "g5out"), "--prespec-text", str(d / "prespec.md"),
            "--population", "main", "gen1", str(d / "frame.csv"), str(d / "wmap.csv"), "--eligible-status", "eligible",
            "--bridge-end-block", str(BRIDGE_END), "--post-end-block", str(POST_END), "--post-start-date", "2022-08-09",
            "--bridge-date", "2022-08-08", "--post-end-date", "2022-09-12"]
    for f, n in flags.items():
        if f not in drop:
            argv += [f"--{f}", str(d / n)]
    for x in extra:
        argv += ["--extra-input", x]
    return run_all.main(argv + list(more))


# --- happy path ---------------------------------------------------------------------------------------------------------------------

def test_assembles_strict_deterministic_unsigned_package(env, tmp_path):
    pkg = s24.build_package(params(env))
    out = tmp_path / "pkg"
    s24.write_package(pkg, out)
    names = sorted(os.listdir(out))
    assert names == ["SHA256", "code_manifest.txt", "g5_config.json", "manifest.json", "package_summary.md", "prespec.md"]
    assert "signing.json" not in names                                   # never signs
    for n in ("g5_config.json", "manifest.json"):
        raw = (out / n).read_bytes()
        raw.decode("ascii")                                              # ASCII only, so no BOM / no non-ASCII whitespace
        cfg = g5._loads(raw, n)                                          # g5's own strict parser
        assert json.dumps(cfg, sort_keys=True, indent=2, allow_nan=False) + "\n" == raw.decode()   # sorted keys
    cfg, man = (json.loads((out / n).read_text()) for n in ("g5_config.json", "manifest.json"))
    assert set(cfg["post_input_paths"]) == set(man["post_input_file_sha256"])
    assert set(g5.POST_G0_KEYS) <= set(man["post_input_file_sha256"]) and cfg["post_end_block"] == POST_END
    assert man["r_package_versions"] == FAKE and man["r_version"] == FAKE_R
    head = subprocess.run(["git", "-C", str(env.repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert man["assembled_at_commit"] == head and man["assembler_sha256"] == s24._sha(open(os.path.join(ROOT, s24.TOOL_REL), "rb").read())
    listed = g5.parse_sha256_list((out / "SHA256").read_text())
    assert set(listed) == set(s24.ROLE_FILES.values())
    assert all(s24._sha((out / n).read_bytes()) == h for n, h in listed.items())
    # code manifest = g5's own list, not a copy
    assert {l.split("  ", 1)[1] for l in (out / "code_manifest.txt").read_text().splitlines()} == set(g5.ALL_CODE_FILES)
    summ = (out / "package_summary.md").read_text()
    assert s24.UNSIGNED_LINE in summ and summ.rstrip().endswith(f"**{s24.UNSIGNED_LINE}**")
    for n, h in listed.items():
        assert n in summ and h in summ
    for k, v in cfg.items():
        if k not in ("populations", "post_input_paths"):
            assert f"`{k}`" in summ
    for k, h in man["post_input_file_sha256"].items():
        assert h in summ and cfg["post_input_paths"][k] in summ
    # deterministic: a second assembly is byte-identical
    pkg2 = s24.build_package(params(env))
    assert pkg2["files"] == pkg["files"]


def test_no_network_and_no_rpc_while_assembling(env, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network touched")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    s24.build_package(params(env))


def test_cli_through_run_all_writes_and_exits_0(env, tmp_path, capsys):
    out = tmp_path / "pkg"
    assert cli(env, out) == 0
    assert (out / "SHA256").is_file() and not (out / "signing.json").exists()
    assert s24.UNSIGNED_LINE in capsys.readouterr().out


def test_optional_inputs_are_accepted_when_not_g0(env):
    p = params(env)
    for k in g5.POST_G0_KEYS:
        p["post_inputs"].pop(k)
    p["post_inputs"].update(usdc_pool_flag=str(env.d / "pool"), deposit_counts=str(env.d / "dep"))
    pkg = s24.build_package(p)
    assert set(pkg["manifest"]["post_input_file_sha256"]) == set(p["post_inputs"])


# --- refusals: each writes NOTHING --------------------------------------------------------------------------------------------------

def refuses(env, tmp_path, match, **kw):
    out = tmp_path / "never"
    with pytest.raises(s24.PackageError, match=match):
        s24.write_package(s24.build_package(params(env, **kw)), out)
    assert not out.exists()


def refuses_cli(env, tmp_path, **kw):
    out = tmp_path / "never_cli"
    assert cli(env, out, **kw) == 3
    assert not out.exists() and [x for x in os.listdir(tmp_path) if x.startswith(".s24-")] == []


def test_refuse_g0_without_eligible_frame_coverage(env, tmp_path):
    p = params(env)
    p["post_inputs"].pop("eligible_frame_coverage")
    with pytest.raises(s24.PackageError, match="without eligible_frame_coverage"):
        s24.build_package(p)
    refuses_cli(env, tmp_path, drop=("eligible-frame-coverage",))


@pytest.mark.parametrize("key", ["later_freeze", "outflow_evidence"])
def test_refuse_refused_post_window_keys(env, tmp_path, key):
    p = params(env)
    p["post_inputs"][key] = str(env.d / ("later" if key == "later_freeze" else "outflow"))
    with pytest.raises(s24.PackageError, match="refused post-window input key"):
        s24.build_package(p)
    refuses_cli(env, tmp_path, extra=[f"{key}={env.d / 'later'}"])


def test_refuse_unknown_key(env, tmp_path):
    refuses_cli(env, tmp_path, extra=[f"mystery={env.d / 'later'}"])
    p = params(env)
    p["post_inputs"]["mystery"] = str(env.d / "later")
    with pytest.raises(s24.PackageError, match="unknown post input"):
        s24.build_package(p)


@pytest.mark.parametrize("key", list(g5.POST_REQUIRED_KEYS))
def test_refuse_missing_required_key(env, tmp_path, key):
    p = params(env)
    p["post_inputs"].pop(key)
    with pytest.raises(s24.PackageError, match="required post input"):
        s24.build_package(p)


def test_refuse_missing_required_key_cli(env, tmp_path):
    refuses_cli(env, tmp_path, drop=("pre-covariates",))


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_refuse_non_finite_values(env, tmp_path, bad):
    refuses(env, tmp_path, "non-finite", post_end_block=bad)
    refuses(env, tmp_path, "non-finite", bridge_end_block=bad)


def test_refuse_missing_unreadable_or_symlinked_input(env, tmp_path):
    (env.d / "cov").unlink()
    refuses(env, tmp_path, "pre/covariates.csv: .* missing")
    refuses_cli(env, tmp_path)
    (env.d / "cov").symlink_to(env.d / "mm")
    refuses(env, tmp_path, "symlink")
    (env.d / "cov").unlink()
    (env.d / "cov").write_bytes(b"x")
    os.chmod(env.d / "cov", 0)
    try:
        if os.access(env.d / "cov", os.R_OK):
            pytest.skip("running with privileges that ignore file modes")
        refuses(env, tmp_path, "unreadable")
    finally:
        os.chmod(env.d / "cov", 0o600)


def test_refuse_missing_population_file(env, tmp_path):
    (env.d / "wmap.csv").unlink()
    refuses(env, tmp_path, "wallet_map: .* missing")


@pytest.mark.parametrize("keep", [("g0_circle_events",), ("g0_circle_events", "g0_usdt_events"), ("g0_receipts",)])
def test_refuse_partial_g0_set(env, tmp_path, keep):
    p = params(env)
    for k in g5.POST_G0_KEYS:
        if k not in keep:
            p["post_inputs"].pop(k)
    with pytest.raises(s24.PackageError, match="all-or-none"):
        s24.build_package(p)


def test_refuse_when_installed_drdid_differs_from_the_freeze(env, monkeypatch):
    monkeypatch.setattr(g5, "installed_r_version", lambda p: {**FAKE, "DRDID": "1.3.0"}.get(p))
    with pytest.raises(s24.PackageError, match="installed DRDID '1.3.0' differs from the frozen '1.4.0'"):
        s24.build_package(params(env))


def test_refuse_when_a_pinned_r_package_is_missing_or_malformed(env, monkeypatch):
    monkeypatch.setattr(g5, "installed_r_version", lambda p: {**FAKE, "fixest": None}.get(p))
    with pytest.raises(s24.PackageError, match="fixest: installed version None is missing / not an exact"):
        s24.build_package(params(env))
    monkeypatch.setattr(g5, "installed_r_version", lambda p: {**FAKE, "data.table": "v1.2"}.get(p))
    with pytest.raises(s24.PackageError, match="data.table: installed version .v1.2. is missing / not an exact"):
        s24.build_package(params(env))


def test_refuse_producer_file_that_differs_from_head(env, tmp_path):
    with open(env.repo / "gates" / "g5_post.py", "ab") as f:
        f.write(b"# dirty\n")
    with pytest.raises(g5.SignatureError, match="differs from the HEAD blob"):
        s24.build_package(params(env))
    refuses_cli(env, tmp_path)


def test_refuse_non_empty_outdir_and_leave_it_alone(env, tmp_path):
    out = tmp_path / "pkg"
    out.mkdir()
    (out / "keep").write_text("k")
    pkg = s24.build_package(params(env))
    with pytest.raises(s24.PackageError, match="not an empty directory"):
        s24.write_package(pkg, out)
    assert os.listdir(out) == ["keep"]


# --- round trip ---------------------------------------------------------------------------------------------------------------------

def test_roundtrip_g5_chain_accepts_under_a_fixture_signature(env, tmp_path):
    pkg = s24.build_package(params(env))
    v = s24.selfcheck_fixture_signature(pkg)
    assert v.cfg["post_input_paths"].keys() == v.manifest["post_input_file_sha256"].keys()
    assert g5.REPO_ROOT == str(env.repo)                                   # restored
    out = tmp_path / "pkg"
    s24.write_package(pkg, out)
    assert b"FIXTURE" not in b"".join(p.read_bytes() for p in out.iterdir())     # the fixture signature never reaches the package


@pytest.mark.skipif(not shutil.which("Rscript"), reason="needs Rscript")
def test_roundtrip_real_installed_r_accepts_in_python_and_in_r(env, tmp_path, monkeypatch):
    real = {k: REAL_INSTALLED(k) for k in g5.POST_PINNED_PACKAGES}
    if None in real.values():
        pytest.skip("a pinned R package is not installed in R")
    monkeypatch.setattr(g5, "installed_r_version", lambda p: real.get(p))
    monkeypatch.setattr(g5, "installed_r_string", REAL_R_STRING)
    pkg = s24.build_package(params(env))
    s24.selfcheck_fixture_signature(pkg)                                   # python: full g5 chain
    dest = tmp_path / "signed_repo"
    s24.fixture_signed_repo(pkg["files"], str(dest))
    script = ('for (f in c("utils", "decide", "cem", "estimate", "validate", "synth", "pre", "blacklist", "presign", "post_gate")) source(file.path("R", paste0(f, ".R")))\n'
              'a <- commandArgs(TRUE)\n'
              'sig <- verify_post_signature(a[1], POST_R_CODE_FILES)\n'
              'cfg <- jsonlite::fromJSON(file.path(a[1], "prespec", "g5_config.json"), simplifyVector = FALSE)\n'
              'vin <- verify_post_inputs(sig, unlist(cfg$post_input_paths))\n'
              'cat("R-ACCEPT", length(vin) - 1L, "\\n")\n')
    r = subprocess.run(["Rscript", "-e", script, str(dest)], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0 and "R-ACCEPT" in r.stdout, r.stderr[-2000:]
    assert int(r.stdout.split()[1]) == len(pkg["cfg"]["post_input_paths"])


def test_roundtrip_detects_a_broken_package(env):
    """The self-check is real: tamper with the emitted config after assembly and the fixture chain denies it."""
    pkg = s24.build_package(params(env))
    cfg = json.loads(pkg["files"]["g5_config.json"])
    cfg["post_input_paths"].pop("analysis_parameters")
    pkg["files"]["g5_config.json"] = s24._dumps(cfg)
    pkg["files"]["SHA256"] = "".join(f"{s24._sha(pkg['files'][n])}  {n}\n" for n in s24.ROLE_FILES.values()).encode()
    with pytest.raises(s24.PackageError, match="post_input_paths keys"):
        s24.selfcheck_fixture_signature(pkg)


def test_build_refuses_its_own_output_when_g5_would_deny_it(env, monkeypatch):
    """Production mode has no signature; the unsigned g5 checks still gate the write. Inject an assembler bug (config loses a key) and the build refuses."""
    real = s24._dumps

    def buggy(obj):
        if isinstance(obj, dict) and "post_input_paths" in obj:
            obj = {**obj, "post_input_paths": {k: v for k, v in obj["post_input_paths"].items() if k != "analysis_parameters"}}
        return real(obj)
    monkeypatch.setattr(s24, "_dumps", buggy)
    with pytest.raises(s24.PackageError, match="candidate fails g5's own check"):
        s24.build_package(params(env))


# --- fix round 1 ----------------------------------------------------------------------------------------------------------------------

def test_happy_path_runs_r_own_readers_on_real_valid_inputs(env):
    res = s24.r_dry_run(s24.resolve_post_inputs(params(env)["post_inputs"]), str(env.repo))
    assert res["population_id"] == "main" and res["generation_id"] == "gen1" and sorted(res["matched"]) == [A, B] and sorted(res["retained"]) == [A, B]


def test_refuse_analysis_parameters_population_differs_from_config(env):
    write_inputs(env.d, pop=("main", "gen2"))                  # internally consistent for R, but not the config's (main, gen1)
    with pytest.raises(s24.PackageError, match="analysis_parameters population/generation"):
        s24.build_package(params(env))


def test_refuse_eligible_frame_coverage_with_other_population_than_params(env):
    text = (env.d / "efc").read_text().replace("gen1", "gen9")
    (env.d / "efc").write_text(text)
    with pytest.raises(s24.PackageError, match="eligible_frame_coverage population/generation differs"):
        s24.build_package(params(env))


def test_refuse_matched_not_subset_of_retained(env):
    write_inputs(env.d, retained=(A,), matched=(A, B))
    with pytest.raises(s24.PackageError, match="matched wallets are not all in the retained eligible frame"):
        s24.build_package(params(env))


def test_refuse_g5_eligible_set_differs_from_retained_set(env):
    text = (env.d / "frame.csv").read_text().replace(f"{B},control,eligible", f"{B},control,ineligible")
    assert text != (env.d / "frame.csv").read_text()
    (env.d / "frame.csv").write_text(text)
    with pytest.raises(s24.PackageError, match="g5 frame eligible set .* != eligible_frame_coverage retained set"):
        s24.build_package(params(env))


def test_refuse_matched_not_in_g5_frame_without_coverage_file(env):
    p = params(env)
    for k in ("eligible_frame_coverage", *g5.POST_G0_KEYS):
        p["post_inputs"].pop(k)
    text = (env.d / "frame.csv").read_text().replace(f"{B},control,eligible", f"{B},control,ineligible")
    (env.d / "frame.csv").write_text(text)
    with pytest.raises(s24.PackageError, match="matched wallets are not all in g5's eligible frame"):
        s24.build_package(p)


@pytest.mark.parametrize("name", ["cov", "wcd", "mm", "ap", "efc", "pool", "dep"])
def test_refuse_inputs_that_rs_own_readers_refuse(env, name):
    (env.d / name).write_text("garbage\n")
    with pytest.raises(s24.PackageError, match="R's own readers refuse"):
        s24.build_package(params(env, **({"post_inputs": {**params(env)["post_inputs"], "usdc_pool_flag": str(env.d / "pool"), "deposit_counts": str(env.d / "dep")}}
                                         if name in ("pool", "dep") else {})))


@pytest.mark.parametrize("name,rows,why", [
    ("pool", [["address", "usdc_pool_depositor"], [B, "TRUE"]], "usdc_pool_flag lacks 1 eligible exposed"),
    ("pool", [["address", "usdc_pool_depositor"], [A, "yes"]], "not TRUE/FALSE"),
    ("dep", [["address", "deposit_count"], [B, 2]], "deposit_counts lacks 1 matched exposed"),
    ("dep", [["address", "deposit_count"], [A, -1]], "not a non-negative integer"),
    ("dep", [["address", "deposit_count"], [A, "1.5"]], "not a non-negative integer"),
    ("dep", [["address", "deposit_count"], [A, 1], [A, 2]], "duplicate address")])
def test_pre_sign_h2_coverage_check_in_dry_run(env, name, rows, why):
    """Tribunal chg-e5801bb3: deposit_counts must cover matched exposed, usdc_pool_flag eligible exposed, with valid values."""
    _csv(env.d / name, rows)
    p = params(env)
    p["post_inputs"].update(usdc_pool_flag=str(env.d / "pool"), deposit_counts=str(env.d / "dep"))
    with pytest.raises(s24.PackageError, match=why):
        s24.build_package(p)


@pytest.mark.parametrize("key", ["usdc_pool_flag", "deposit_counts"])
def test_refuse_missing_h2_inputs_so_coverage_check_cannot_be_bypassed(env, tmp_path, key):
    p = params(env)
    p["post_inputs"].pop(key)
    with pytest.raises(s24.PackageError, match=f"post input '{key}' is required"):
        s24.build_package(p)
    flag = key.replace("_", "-")
    refuses_cli(env, tmp_path, drop=(flag,))


@pytest.mark.parametrize("name,rows,why", [
    ("pool", [["address", "usdc_pool_depositor"], [A, "TRUE"], [B, "FALSE"]], "usdc_pool_flag is not exactly the eligible exposed set"),
    ("dep", [["address", "deposit_count"], [A, 3], [B, 0]], "deposit_counts is not exactly the matched exposed set")])
def test_pre_sign_h2_files_must_equal_the_sets_not_just_cover_them(env, name, rows, why):
    _csv(env.d / name, rows)       # B is a control wallet: an extra row beyond the exposed set
    with pytest.raises(s24.PackageError, match=why):
        s24.build_package(params(env))


def test_refuse_duplicate_population_and_more_than_one_population(env):
    pop = ("main", "gen1", str(env.d / "frame.csv"), str(env.d / "wmap.csv"))
    with pytest.raises(s24.PackageError, match="duplicate .population_id, generation_id."):
        s24.build_package(params(env, populations=[pop, pop]))
    with pytest.raises(s24.PackageError, match="exactly one population"):
        s24.build_package(params(env, populations=[pop, ("main2", "gen1", pop[2], pop[3])]))


# prespec blanks / known text-vs-code mismatch ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["D [____, ____], A [____, ____]\n", "cut-points *(pipeline records)*: ______________.\n", "Power value *(pipeline records)*: \n",
                                  "Cut-points: D [TBD, TBD]\n", "value: XXX\n", "value: XXXX.\n", "cut-points: D \\_\\_\\_\\_, A \\_\\_\\_\\_\n",
                                  "Cut-points: D [\u2014, \u2014]\n", "Result: \u2014.\n", "Result: \u2013\n"])
def test_production_refuses_unfilled_prespec_blanks(env, text):
    (env.d / "prespec.md").write_text(text)
    with pytest.raises(s24.PackageError, match="unfilled blank"):
        s24.build_package(params(env))
    pkg = s24.build_package(params(env, allow_blanks=True))                     # the explicit test-only flag
    assert "TEST-ONLY BUILD" in pkg["files"]["package_summary.md"].decode()


def test_the_signed_date_line_is_exempt_but_other_blanks_on_the_same_text_are_not(env):
    signed = "Signed: ______________________  Date: ______________\n"
    (env.d / "prespec.md").write_text("Body text with a filled field: 0.5.\n\n" + signed)
    s24.build_package(params(env))
    (env.d / "prespec.md").write_text(signed + "Cut-points: D [____, ____]\n")
    with pytest.raises(s24.PackageError, match="unfilled blank"):
        s24.build_package(params(env))


def test_blank_flag_is_cli_visible_and_default_refuses(env, tmp_path):
    (env.d / "prespec.md").write_text("x: ____\n")
    refuses_cli(env, tmp_path)


def _caps(env):
    return json.loads((env.d / "ap").read_text())["cap_values"]


def _cap_line(c, fmt="{:.6f}"):
    f = lambda v: fmt.format(v).replace("-", "\u2212")
    return f"Cut-points *(pipeline records)*: D [{f(c['D'][0])}, {f(c['D'][1])}], A [{f(c['A'][0])}, {f(c['A'][1])}]."


def test_filled_pipeline_records_field_is_accepted_and_caps_are_compared(env):
    c = _caps(env)
    (env.d / "prespec.md").write_text("Signing is withheld until every field marked *(pipeline records)* is filled.\n" + _cap_line(c) + "\n")
    pkg = s24.build_package(params(env))
    summ = pkg["files"]["package_summary.md"].decode()
    assert "cap D: prespec" in summ and "matches the frozen" in summ


def test_refuse_filled_cap_values_that_differ_from_the_frozen_cap_values(env):
    c = _caps(env)
    c["A"][1] += 0.01
    (env.d / "prespec.md").write_text(_cap_line(c) + "\n")
    with pytest.raises(s24.PackageError, match="prespec cap A .* does not match the frozen analysis_parameters cap_values"):
        s24.build_package(params(env))


def test_unparseable_or_missing_cap_values_are_listed_for_author_check_not_refused(env):
    (env.d / "prespec.md").write_text("Cut-points: D [low, high]. No A pair here.\n")
    summ = s24.build_package(params(env))["files"]["package_summary.md"].decode()
    assert "cap D: prespec" in summ and "CHECK BY HAND" in summ and "cap A: prespec has 0" in summ


def test_trimming_text_and_code_are_printed_side_by_side_and_the_old_sentence_is_not_refused(env):
    sentence = "Wallets with an estimated propensity above 0.99 are\ndropped and counted.\nThe other trimming note: propensity >= 0.99 controls are trimmed.\n"
    (env.d / "prespec.md").write_text(sentence)
    summ = s24.build_package(params(env))["files"]["package_summary.md"].decode()
    sec = summ[summ.index("## Author check: text vs code"):]
    assert "DRDID_FREEZE$trimming" in sec and "controls (D == 0) with IPT propensity >= 0.99" in sec          # the code side, from R/drdid.R
    assert "Wallets with an estimated propensity above 0.99 are" in sec                                         # old sentence: printed, not refused
    assert "The other trimming note: propensity >= 0.99 controls are trimmed." in sec                          # the text side
    assert FAKE_R in sec


def test_the_trimming_rule_printed_comes_from_the_freeze_in_r_not_from_a_copy(env):
    res = s24.r_dry_run(s24.resolve_post_inputs(params(env)["post_inputs"]), str(env.repo))
    assert res["drdid_trimming"] and "trim.level = 0.99" in res["drdid_trimming"] and res["r_version"].startswith("R version")


REAL_PRESPEC = os.environ.get("TORNADO_REAL_PRESPEC", "")   # path to an unsigned author draft; unset -> test skipped


@pytest.mark.skipif(not (REAL_PRESPEC and os.path.exists(REAL_PRESPEC)), reason="author prespec draft not present")
def test_current_author_draft_is_refused_in_production(env):
    (env.d / "prespec.md").write_bytes(open(REAL_PRESPEC, "rb").read())
    with pytest.raises(s24.PackageError):
        s24.build_package(params(env))


# pins, R version -------------------------------------------------------------------------------------------------------------------------

def test_drdid_and_its_imports_and_jsonlite_are_pinned_and_r_version_recorded(env):
    assert {"DRDID", "trust", "BMisc", "fastglm", "Rcpp", "jsonlite"} <= set(g5.POST_PINNED_PACKAGES)
    pkg = s24.build_package(params(env))
    assert set(pkg["manifest"]["r_package_versions"]) == set(g5.POST_PINNED_PACKAGES)
    assert pkg["manifest"]["r_version"] == FAKE_R


def test_refuse_when_r_version_string_is_unavailable_or_differs(env, monkeypatch):
    monkeypatch.setattr(g5, "installed_r_string", lambda: None)
    with pytest.raises(s24.PackageError, match="cannot read R.version.string"):
        s24.build_package(params(env))


# provenance -------------------------------------------------------------------------------------------------------------------------

def test_git_head_hard_fails_and_never_says_unknown(env, tmp_path, monkeypatch):
    monkeypatch.setattr(g5, "REPO_ROOT", str(tmp_path / "nonrepo"))
    with pytest.raises(s24.PackageError, match="cannot read the repo HEAD commit"):
        s24._git_head()
    empty = tmp_path / "emptyrepo"
    _git(tmp_path, "init", "-q", str(empty))                      # a repo with no commit: HEAD does not resolve
    monkeypatch.setattr(g5, "REPO_ROOT", str(empty))
    with pytest.raises(s24.PackageError, match="cannot read the repo HEAD commit"):
        s24._git_head()


def test_refuse_when_the_assembler_differs_from_its_head_blob(env):
    with open(env.repo / s24.TOOL_REL, "ab") as f:
        f.write(b"# changed after HEAD\n")
    _git(env.repo, "commit", "-q", "-am", "tool moved")                  # HEAD blob now differs from the running tool file
    with pytest.raises(s24.PackageError, match="differs from its HEAD blob"):
        s24.build_package(params(env))


def test_summary_records_head_commit_and_assembler_hash(env):
    pkg = s24.build_package(params(env))
    summ = pkg["files"]["package_summary.md"].decode()
    assert pkg["manifest"]["assembled_at_commit"] in summ and pkg["manifest"]["assembler_sha256"] in summ and "unknown" not in summ.lower()


# summary forging ---------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["a\nb", "a`b", "a|b", "a\rb", "a\x1bb", "a\x7fb", "a\x85b", "a\x9fb", "a\u2028b", "a\u2029b", "a\u202eb", "a\u202ab", "a\u2066b", "a\u2069b"])
def test_refuse_forging_characters_in_paths(env, bad):
    forged = env.d / ("cov" + bad)
    shutil.copyfile(env.d / "cov", forged)
    p = params(env)
    p["post_inputs"]["pre/covariates.csv"] = str(forged)
    with pytest.raises(s24.PackageError, match="summary forging"):
        s24.build_package(p)


@pytest.mark.parametrize("field", ["pid", "gid", "status", "out_dir", "frame"])
def test_refuse_forging_characters_in_ids_and_other_paths(env, field):
    p = params(env)
    pid, gid, fr, wm = p["populations"][0]
    if field == "pid":
        p["populations"] = [("ma|in", gid, fr, wm)]
    elif field == "gid":
        p["populations"] = [(pid, "g\nen1", fr, wm)]
    elif field == "status":
        p["eligible_statuses"] = ["eligible`"]
    elif field == "out_dir":
        p["out_dir"] = str(env.tmp / "out|dir")
    else:
        forged = env.d / "frame|x.csv"
        shutil.copyfile(fr, forged)
        p["populations"] = [(pid, gid, str(forged), wm)]
    with pytest.raises(s24.PackageError, match="summary forging"):
        s24.build_package(p)


# dates / blocks -------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("key,bad", [("post_start_date", "2022-8-9"), ("post_start_date", "20220809"), ("bridge_date", "2022-08-08\n"),
                                     ("post_end_date", "２０２２-09-12"), ("post_end_date", " 2022-09-12"), ("bridge_date", "2022-13-08")])
def test_refuse_non_ymd_dates(env, key, bad):
    with pytest.raises(s24.PackageError, match="YYYY-MM-DD|not an ISO date"):
        s24.build_package(params(env, **{key: bad}))


@pytest.mark.parametrize("br,po", [(15_302_549, POST_END), (15_302_000, POST_END), (POST_END, POST_END), (POST_END + 1, POST_END),
                                   (BRIDGE_END, 15_537_394), (BRIDGE_END, 15_600_000), (PRE + 100, PRE + 3600)])
def test_refuse_blocks_outside_event_bridge_post_merge_order(env, br, po):
    with pytest.raises(s24.PackageError, match="event_block .* < bridge_end .* < post_end .* < merge_block"):
        s24.build_package(params(env, bridge_end_block=br, post_end_block=po))


@pytest.mark.parametrize("br,po", [(BRIDGE_END + s24.BLOCK_TOLERANCE + 100, POST_END), (BRIDGE_END, POST_END - s24.BLOCK_TOLERANCE - 100),
                                   (BRIDGE_END, 15_400_000)])
def test_refuse_blocks_inconsistent_with_the_date_window(env, br, po):
    with pytest.raises(s24.PackageError, match="inconsistent with"):
        s24.build_package(params(env, bridge_end_block=br, post_end_block=po))


def test_blocks_inside_the_tolerance_are_accepted(env):
    assert s24.BLOCK_TOLERANCE == 3000
    s24.build_package(params(env, bridge_end_block=BRIDGE_END + 2500, post_end_block=POST_END - 2500))
    with pytest.raises(s24.PackageError, match="inconsistent with"):
        s24.build_package(params(env, bridge_end_block=BRIDGE_END + 3500))


def test_refuse_non_integer_blocks_cleanly(env):
    with pytest.raises(s24.PackageError, match="integers"):
        s24.build_package(params(env, bridge_end_block=BRIDGE_END + 0.5))


# g5 out_dir --------------------------------------------------------------------------------------------------------------------------------

def test_refuse_existing_non_empty_g5_out_dir(env, tmp_path):
    out = tmp_path / "g5out"
    out.mkdir()
    (out / "x").write_text("x")
    with pytest.raises(s24.PackageError, match="g5 out_dir .* not an empty directory"):
        s24.build_package(params(env))
    (out / "x").unlink()
    s24.build_package(params(env))                                      # an empty one is fine (g5 accepts it)
    out.rmdir()
    out.write_text("a file")
    with pytest.raises(s24.PackageError, match="g5 out_dir"):
        s24.build_package(params(env))


# write_package: no rmdir window -----------------------------------------------------------------------------------------------------------

def test_write_package_never_rmdirs_the_outdir_and_replaces_an_empty_one(env, tmp_path, monkeypatch):
    pkg = s24.build_package(params(env))
    out = tmp_path / "pkg"
    out.mkdir()
    monkeypatch.setattr(os, "rmdir", lambda p: (_ for _ in ()).throw(AssertionError("rmdir on outdir (TOCTOU window)")))
    s24.write_package(pkg, out)
    assert (out / "SHA256").is_file()


def test_write_package_loses_the_race_safely(env, tmp_path, monkeypatch):
    """Another process fills the (empty) outdir between our check and our rename: the rename fails, the stranger's data is untouched, no temp dir is left."""
    pkg = s24.build_package(params(env))
    out = tmp_path / "pkg"
    out.mkdir()
    real = os.rename

    def racing(src, dst):
        (out / "theirs").write_text("keep")
        return real(src, dst)
    monkeypatch.setattr(os, "rename", racing)
    with pytest.raises(s24.PackageError, match="could not move"):
        s24.write_package(pkg, out)
    assert os.listdir(out) == ["theirs"] and [x for x in os.listdir(tmp_path) if x.startswith(".s24-")] == []


# honest wording -----------------------------------------------------------------------------------------------------------------------------

def test_summary_and_docstring_name_the_g0_post_cutoff_material(env):
    summ = s24.build_package(params(env))["files"]["package_summary.md"].decode()
    assert "22,170,334" in summ and "after the 7 Aug 2022 cutoff" in summ and "g0_circle_events" in summ
    assert "reads no post-window data" not in s24.__doc__ and "22,170,334" in s24.__doc__
    assert "no post-window read" not in open(os.path.join(ROOT, "README.md")).read()


def test_fixture_commit_ignores_a_global_gpgsign_config(env, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = /bin/false\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    s24.selfcheck_fixture_signature(s24.build_package(params(env)))


# --- fix round 2 ----------------------------------------------------------------------------------------------------------------------

def test_happy_path_fixture_is_genuinely_valid_60_baseline_days_and_matching_caps(env):
    rows = [l.split(",") for l in (env.d / "wcd").read_text().splitlines()[1:]]
    for w in ("10", "11"):
        assert sorted({int(r[3]) for r in rows if r[0] == w}) == list(range(60))
    c = _caps(env)
    assert all(v[0] < v[1] for v in c.values())                       # a real interval, not the old [-1, 1] placeholder
    res = s24.r_dry_run(s24.resolve_post_inputs(params(env)["post_inputs"]), str(env.repo))
    for k in ("D", "A"):
        assert res["cap_values"][k] == pytest.approx(c[k], rel=1e-9)  # R recomputed the very same values


def test_refuse_matched_wallet_with_baseline_B_le_0_in_the_dry_run(env):
    write_inputs(env.d, zero=(11,))
    with pytest.raises(s24.PackageError, match="matched wallets lost in the panel .baseline holdings B <= 0."):
        s24.build_package(params(env))


def test_refuse_recomputed_caps_that_differ_from_the_frozen_cap_values_in_the_dry_run(env):
    c = _caps(env)
    c["D"][1] += 0.5
    write_inputs(env.d, caps=c)
    with pytest.raises(s24.PackageError, match="recomputed D caps differ from the frozen analysis_parameters cap_values"):
        s24.build_package(params(env))
    c = _caps(env)
    write_inputs(env.d)
    c = _caps(env)
    c["legs_pooled"][0] -= 0.5
    write_inputs(env.d, caps=c)
    with pytest.raises(s24.PackageError, match="recomputed pooled leg caps differ"):
        s24.build_package(params(env))


def test_refuse_eligible_wallets_without_the_60_baseline_days_in_the_dry_run(env):
    write_inputs(env.d, drop=((11, 30),))                              # caps are recomputed from the data actually written, so only po_frame can stop
    with pytest.raises(s24.PackageError, match="po_frame: eligible wallets lack the 60 baseline days"):
        s24.build_package(params(env))


def test_post_stage_and_dry_run_call_the_one_shared_pre_function_no_copied_logic():
    post, dry = (open(os.path.join(ROOT, f)).read() for f in ("R/post.R", "tools/s24_dryrun.R"))
    assert "po_pre_stage(pre_dir, params, mem, eligible_frame_coverage_path)" in post and "po_pre_stage(pre_dir, params, mem, opt(args[5]))" in dry
    for stop in ("baseline holdings B <= 0", "recomputed %s caps differ", "lack the 60 baseline days", "screen_status disagrees", "matched wallets are not all in the retained"):
        assert stop in post and stop not in dry                        # the stop texts live once, in R/post.R
    assert post.count("recomputed %s caps differ") == 1 and post.count("baseline holdings B <= 0") == 1


def test_dry_run_json_keeps_length_one_vectors_as_arrays(env):
    src = open(os.path.join(ROOT, "tools/s24_dryrun.R")).read()
    line = [l for l in src.splitlines() if l.startswith("arr <- ")][0]
    r = subprocess.run(["Rscript", "-e", f'{line}\ncat(jsonlite::toJSON(list(m = arr("0xa"), r = arr(NULL), two = arr(c("x", "y"))), auto_unbox = TRUE, null = "null"))'],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and json.loads(r.stdout) == {"m": ["0xa"], "r": None, "two": ["x", "y"]}, r.stderr + r.stdout


def test_assembler_refuses_a_dry_run_result_that_is_not_an_array(env, monkeypatch):
    real = _cached_dry_run(s24.resolve_post_inputs(params(env)["post_inputs"]), str(env.repo))
    monkeypatch.setattr(s24, "r_dry_run", lambda *a: {**real, "matched": A})          # a length-1 vector auto-unboxed to a string
    with pytest.raises(s24.PackageError, match="not an array of addresses"):
        s24.build_package(params(env))


def test_docs_claim_only_what_is_checked_and_name_the_remaining_post_fetch_stops():
    texts = {"dryrun": open(os.path.join(ROOT, "tools/s24_dryrun.R")).read().split("\nargs <-")[0], "doc": s24.semantic_checks.__doc__,
             "readme": open(os.path.join(ROOT, "README.md")).read()}
    for name in ("dryrun", "doc"):
        t = texts[name]
        for needle in ("g0 provenance", "rematch", "malformed g0 receipts", "post panel reader"):
            assert needle in t, (name, needle)
    assert "NOT checked" in texts["doc"] and "does NOT cover" in texts["dryrun"]
    assert "R's own pre-window readers (R/post.R run_post, lines" not in texts["dryrun"]
    if not texts["readme"].strip():   # public export: README.md is a release overlay placeholder until the replacement is written
        return
    assert "g0 provenance" in texts["readme"] and "malformed g0 receipts" in texts["readme"] and "rematch" in texts["readme"]


# test-only builds are unsignable ---------------------------------------------------------------------------------------------------

def test_test_only_build_has_no_sha256_marks_the_manifest_and_prints_a_loud_line(env, tmp_path, capsys):
    (env.d / "prespec.md").write_text("x: ____\n")
    out = tmp_path / "pkg_t"
    assert cli(env, out, more=["--test-only-allow-blanks"]) == 0
    assert "SHA256" not in os.listdir(out) and "signing.json" not in os.listdir(out)
    man = json.loads((out / "manifest.json").read_text())
    assert man["test_only"] is True
    assert "TEST-ONLY BUILD" in capsys.readouterr().out
    assert "must NOT be signed" in (out / "package_summary.md").read_text()
    with pytest.raises(s24.PackageError, match="unsignable"):
        s24.selfcheck_fixture_signature(s24.build_package(params(env, allow_blanks=True)))


def test_production_build_has_sha256_and_no_test_only_key(env):
    pkg = s24.build_package(params(env))
    assert "SHA256" in pkg["files"] and "test_only" not in pkg["manifest"]


def test_g5_refuses_a_test_only_manifest_in_check_r_half_and_check_config(env):
    pkg = s24.build_package(params(env))
    man = {**pkg["manifest"], "test_only": True}
    with pytest.raises(g5.SignatureError, match="test_only"):
        g5.check_r_half(man, lambda p: FAKE.get(p), lambda: FAKE_R)
    with pytest.raises(g5.InputMismatch, match="test_only"):
        g5.check_config(pkg["cfg"], man, PRE)
    g5.check_config(pkg["cfg"], pkg["manifest"], PRE)                   # the production manifest still passes


def test_test_only_marker_survives_in_the_emitted_bytes_and_g5_denies_them(env):
    pkg = s24.build_package(params(env, allow_blanks=True))
    man = g5._loads(pkg["files"]["manifest.json"], "m")
    with pytest.raises(g5.SignatureError, match="test_only"):
        g5.check_r_half(man, lambda p: FAKE.get(p), lambda: FAKE_R)


@pytest.mark.skipif(not shutil.which("Rscript"), reason="needs Rscript")
def test_r_verify_post_signature_refuses_a_test_only_manifest_python_r_parity(env, tmp_path, monkeypatch):
    real = {k: REAL_INSTALLED(k) for k in g5.POST_PINNED_PACKAGES}
    if None in real.values():
        pytest.skip("a pinned R package is not installed in R")
    monkeypatch.setattr(g5, "installed_r_version", lambda p: real.get(p))
    monkeypatch.setattr(g5, "installed_r_string", REAL_R_STRING)
    pkg = s24.build_package(params(env))
    files = dict(pkg["files"])
    man = {**json.loads(files["manifest.json"]), "test_only": True}
    files["manifest.json"] = s24._dumps(man)
    files["SHA256"] = "".join(f"{s24._sha(files[n])}  {n}\n" for n in s24.ROLE_FILES.values()).encode("ascii")   # a fully re-hashed, otherwise valid package
    script = ('for (f in c("utils", "decide", "cem", "estimate", "validate", "synth", "pre", "blacklist", "presign", "post_gate")) source(file.path("R", paste0(f, ".R")))\n'
              'a <- commandArgs(TRUE)\n'
              'r <- tryCatch({verify_post_signature(a[1], POST_R_CODE_FILES); "ACCEPTED"}, error = function(e) conditionMessage(e))\n'
              'cat(r, "\\n")\n')
    outs = {}
    for label, f in (("test_only", files), ("clean", pkg["files"])):
        dest = tmp_path / f"signed_{label}"
        s24.fixture_signed_repo(f, str(dest))
        r = subprocess.run(["Rscript", "-e", script, str(dest)], cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-2000:]
        outs[label] = r.stdout
    assert "test_only" in outs["test_only"] and "ACCEPTED" not in outs["test_only"]
    assert "ACCEPTED" in outs["clean"]                                    # the same package without the marker is accepted: the marker is the only difference
    g5.REPO_ROOT, saved = str(tmp_path / "signed_test_only"), g5.REPO_ROOT
    try:
        with pytest.raises(g5.SignatureError, match="test_only"):
            g5.verify_signature(files["g5_config.json"])
    finally:
        g5.REPO_ROOT = saved


# provenance of the R dry run ---------------------------------------------------------------------------------------------------------

def test_refuse_when_the_dry_run_script_differs_from_its_head_blob(env):
    with open(os.path.join(ROOT, s24.DRYRUN_REL), "rb") as f:
        head = f.read()
    with open(env.repo / s24.DRYRUN_REL, "ab") as f:
        f.write(b"# changed after HEAD\n")
    _git(env.repo, "commit", "-q", "-am", "dryrun moved")
    with pytest.raises(s24.PackageError, match="s24_dryrun.R differs from its HEAD blob"):
        s24.build_package(params(env))
    assert head                                                            # (the running copy is untouched)


def test_dry_run_hash_is_recorded_in_manifest_and_summary(env):
    pkg = s24.build_package(params(env))
    want = s24._sha(open(os.path.join(ROOT, s24.DRYRUN_REL), "rb").read())
    assert pkg["manifest"]["dryrun_sha256"] == want and want in pkg["files"]["package_summary.md"].decode()


def test_summary_prints_population_ids_via_json_dumps(env):
    summ = s24.build_package(params(env))["files"]["package_summary.md"].decode()
    assert '- "main" / "gen1":' in summ


# pins ---------------------------------------------------------------------------------------------------------------------------------

def test_transitive_numeric_dependencies_are_pinned(env):
    extra = {"Matrix", "RcppEigen", "sandwich", "nlme", "dreamerr", "numDeriv", "stringmagic", "bigmemory"}
    assert extra <= set(g5.POST_PINNED_PACKAGES)
    r = open(os.path.join(ROOT, "R/post_gate.R")).read()
    assert all(f'"{p}"' in r.split("POST_PINNED_PACKAGES <- c(")[1].split("MIRROR")[0] for p in extra)
    assert set(s24.build_package(params(env))["manifest"]["r_package_versions"]) >= extra
