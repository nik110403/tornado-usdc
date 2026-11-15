"""S16: g1 pure helpers + deterministic cohort-delta artifact. Synthetic tmp files only; no network."""
import csv
import json

import pytest

from gates import g1_cohort as g1

A, B, C, D = ("0x" + c * 40 for c in "abcd")


def _cohort(path, rows, producing_commit=None, input_hashes=()):
    cols = ["wallet_id", "address", "arm", "both_arms", "floor_pass", "in_panel", "has_baseline_transfer"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for i, r in enumerate(rows):
            w.writerow({"wallet_id": i, "arm": "withdrawal", "both_arms": False, "floor_pass": True,
                        "in_panel": True, "has_baseline_transfer": True, **r})
    if producing_commit:
        (path.parent / (path.stem + ".schema.json")).write_text(
            json.dumps({"producing_commit": producing_commit, "input_hashes": list(input_hashes)}))


def _screen(path, mapping):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["address", "arm", "screen", "screen_block"])
        w.writeheader()
        for a, s in mapping.items():
            w.writerow({"address": a, "arm": "withdrawal", "screen": s, "screen_block": 1})


def test_eligibility_blocks_cutoff_vs_event_only():
    pinned = {"pre_period_end": {"block": 15298282}, "event_block": {"block": 15302549}}
    eb = g1.eligibility_blocks(pinned)
    assert eb["exposure_end"] == eb["contract_screen"] == 15298282
    assert eb["event_only"] == 15302549


def test_participation_rule_required_and_validated():
    assert g1.resolve_participation_rule({"participation_rule": "none"}) == "none"
    for cfg in ({}, {"participation_rule": None}, {"participation_rule": "default"}):
        with pytest.raises(ValueError):
            g1.resolve_participation_rule(cfg)


def test_select_participants_passive_holder():
    cohort = [B, A, C]
    part = {A: {"USDC"}, B: set()}
    assert g1.select_participants(cohort, part, g1.RULE_BASELINE_TRANSFER) == [A]
    assert g1.select_participants(cohort, part, g1.RULE_NONE) == sorted(cohort)
    with pytest.raises(ValueError):
        g1.select_participants(cohort, part, "x")


def test_cohort_delta_additions_removals_reasons_hashes(tmp_path):
    old_p, new_p = tmp_path / "old" , tmp_path / "new"
    old_p.mkdir(), new_p.mkdir()
    # old (event-block scope): A, B(will be a contract at cutoff), D(event-block-only? no: absent in new log)
    _cohort(old_p / "cohort.csv", [{"address": A}, {"address": B}, {"address": D}], "oldsha", ["h1"])
    # new: A (floor flips), C (new)
    _cohort(new_p / "cohort.csv", [{"address": A, "floor_pass": False}, {"address": C}], "newsha", ["h2", "h3"])
    _screen(old_p / "s.csv", {A: "eoa", B: "eoa", C: "contract", D: "eoa"})
    _screen(new_p / "s.csv", {A: "eoa", B: "contract", C: "eoa"})
    d = g1.build_cohort_delta(str(old_p / "cohort.csv"), str(new_p / "cohort.csv"),
                              str(old_p / "s.csv"), str(new_p / "s.csv"))
    assert d["added"] == [{"address": C, "reason": "contract_only_at_old_screen_block"}]
    assert d["removed"] == [{"address": B, "reason": "contract_at_new_cutoff"},
                            {"address": D, "reason": "no_exposure_log_by_new_cutoff"}]
    assert d["changed"] == [{"address": A, "fields": {"floor_pass": {"old": "True", "new": "False"}}}]
    assert d["old"]["producing_commit"] == "oldsha" and d["new"]["input_hashes"] == ["h2", "h3"]
    assert len(d["old"]["cohort_sha256"]) == 64 and d["old"]["cohort_sha256"] != d["new"]["cohort_sha256"]
    assert d["counts"] == {"old": 3, "new": 2}


def test_cohort_delta_deterministic_and_unattributed_without_screens(tmp_path):
    _cohort(tmp_path / "o.csv", [{"address": A}])
    _cohort(tmp_path / "n.csv", [{"address": B}])
    d1 = g1.build_cohort_delta(str(tmp_path / "o.csv"), str(tmp_path / "n.csv"))
    d2 = g1.build_cohort_delta(str(tmp_path / "o.csv"), str(tmp_path / "n.csv"))
    assert d1["added"][0]["reason"] == d1["removed"][0]["reason"] == "unattributed"
    assert d1["old"]["producing_commit"] is None
    s1 = g1.write_cohort_delta(d1, str(tmp_path / "a.json"))
    s2 = g1.write_cohort_delta(d2, str(tmp_path / "b.json"))
    assert s1 == s2


def test_cli_delta_hook(tmp_path, capsys):
    _cohort(tmp_path / "o.csv", [{"address": A}])
    _cohort(tmp_path / "n.csv", [{"address": A}, {"address": B}])
    out = tmp_path / "delta.json"
    assert g1.main(["delta", str(tmp_path / "o.csv"), str(tmp_path / "n.csv"), str(out)]) == 0
    assert json.loads(out.read_text())["added"] == [{"address": B, "reason": "unattributed"}]
    assert "+1 -0 ~0" in capsys.readouterr().out
