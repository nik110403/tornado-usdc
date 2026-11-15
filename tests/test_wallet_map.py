"""Tests for tools/wallet_map.py (covariates.csv -> WALLET_MAP). Synthetic; no RPC, no out/ data."""
import csv
import hashlib

import pytest

import run_all
from gates import g5_post as g5
from tools import wallet_map as wm

A, B, C = ("0x" + c * 40 for c in "abc")


def cov(tmp_path, rows, header="wallet_id,address,arm,mean_total_units"):
    p = tmp_path / "covariates.csv"
    p.write_text(header + "\n" + "".join(",".join(map(str, r)) + "\n" for r in rows))
    return p


def args(p, out):
    return ["--gate", "wallet_map", "--covariates", str(p), "--population-id", "main", "--generation-id", "gen-M", "--out", str(out)]


def test_projection_and_roundtrip_via_g5_loader(tmp_path):
    p = cov(tmp_path, [(0, A, "control", 1), (1, B, "withdrawal", 2), (2, C, "control", 3)])
    out = tmp_path / "sub" / "wallet_map.csv"
    assert run_all.main(args(p, out)) == 0
    raw = out.read_bytes()
    assert raw.startswith(b"population_id,generation_id,wallet_id,address\n") and b"\r" not in raw
    frame = tmp_path / "frame.csv"
    frame.write_text("population_id,generation_id,address,source_membership,screen_status,screen_reason\n"
                     + "".join(f"main,gen-M,{a},control,eligible,\n" for a in (A, B, C)))
    sha = lambda f: hashlib.sha256(f.read_bytes()).hexdigest()
    pop = {"population_id": "main", "generation_id": "gen-M", "frame_path": str(frame), "wallet_map_path": str(out)}
    got, _, stats = g5._load_frame(pop, {"eligible"}, {str(frame): sha(frame), str(out): sha(out)})
    assert got == {A: (0, "control"), B: (1, "control"), C: (2, "control")} and stats["map_rows"] == 3


@pytest.mark.parametrize("rows,why", [
    ([(0, A, "x", 1), (0, B, "x", 1)], "duplicate"),
    ([(0, A, "x", 1), (1, A, "x", 1)], "duplicate"),
    ([(0, "0x" + "A" * 40, "x", 1)], "lowercase"),
    ([(0, "0xabc", "x", 1)], "lowercase"),
    ([("a", A, "x", 1)], "integer"),
    ([], "no data rows"),
])
def test_refusals(tmp_path, capsys, rows, why):
    out = tmp_path / "w.csv"
    assert run_all.main(args(cov(tmp_path, rows), out)) == 2
    assert why in capsys.readouterr().err and not out.exists()


def test_missing_column_and_bad_ids_and_no_overwrite(tmp_path, capsys):
    out = tmp_path / "w.csv"
    assert run_all.main(args(cov(tmp_path, [(A, 1)], header="address,arm"), out)) == 2
    assert "header" in capsys.readouterr().err
    p = cov(tmp_path, [(0, A, "x", 1)])
    assert run_all.main(["--gate", "wallet_map", "--covariates", str(p), "--population-id", "a,b", "--generation-id", "g", "--out", str(out)]) == 2
    assert run_all.main(args(p, out)) == 0
    before = out.read_bytes()
    assert run_all.main(args(p, out)) == 2 and out.read_bytes() == before
    assert "exists" in capsys.readouterr().err
