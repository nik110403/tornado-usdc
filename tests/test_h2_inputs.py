"""tools/h2_inputs.py -- synthetic persisted chunks; no network, no out/ reads."""
import csv
import json

import pytest

import run_all
from gates import g1_cohort as g1
from lib import rpc
from tools import h2_inputs as h2

CUT = 1000
SPAN = (100, CUT)
SPANS = {"junef": (100, 500), "s22": (400, CUT)}   # overlap [400, 500] like the real S21/S22 stores
P1, P2, P3 = ("0x" + "%040x" % i for i in (0x1001, 0x1002, 0x1003))
EMIT = "0x" + "%040x" % 0x7777
EMIT2 = "0x" + "%040x" % 0x8888
POOLS = [P1, P2, P3]


def addr(i):
    return "0x" + "%040x" % i


ALICE, BOB, CAROL, DAVE, ERIN = (addr(i) for i in (0xA1, 0xB2, 0xC3, 0xD4, 0xE5))
CTRL = addr(0xC0)
TX = lambda n: "0x" + "%064x" % n  # noqa: E731
pad = lambda a: "0x" + a[2:].rjust(64, "0")  # noqa: E731


def dlog(block, idx, tx, pool, data="0x00"):
    return {"blockNumber": hex(block), "logIndex": hex(idx), "transactionHash": TX(tx), "address": pool,
            "topics": [g1.DEPOSIT_TOPIC, TX(9)], "data": data}


def nlog(block, idx, tx, who, emitter=EMIT):
    return {"blockNumber": hex(block), "logIndex": hex(idx), "transactionHash": TX(tx), "address": emitter,
            "topics": [g1.ENCRYPTED_NOTE_TOPIC, pad(who)], "data": "0x"}


def wlog(block, idx, tx):   # a withdrawal sharing the sweep_a store (topic filter must skip it)
    return {"blockNumber": hex(block), "logIndex": hex(idx), "transactionHash": TX(tx), "address": P1,
            "topics": [g1.WITHDRAWAL_TOPIC, pad(ALICE)], "data": "0x"}


def store(d, address, topics, span, logs, tile=100, endpoint=None):
    endpoint = endpoint or ("mevblocker" if str(d).split("/")[-1].startswith("s") else "tenderly")   # jd/jn = junef (tenderly), sd*/sn* = S22 (mevblocker)
    binding = rpc.make_binding("c0ffee:0", "cfg", {"x": "y"}, endpoint, address, topics)
    s = rpc.ChunkStore(str(d), binding)
    lo = span[0]
    while lo <= span[1]:
        hi = min(lo + tile - 1, span[1])
        s._write_chunk(lo, hi, [l for l in logs if lo <= int(l["blockNumber"], 16) <= hi], b"b")
        lo = hi + 1
    return str(d)


def write_csv(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
    return str(path)


# world: ALICE 2 deposits (one USDC pool P1), BOB 1 deposit, CAROL withdrawal-only (matched exposed, 0), DAVE exposed unmatched,
# ERIN deposits only in the overlap, plus one unresolved deposit.
def world():
    D = [dlog(120, 1, 1, P1), dlog(130, 1, 2, P2), dlog(140, 1, 3, P3), dlog(450, 1, 4, P3),   # overlap block 450
         dlog(700, 1, 5, P2), dlog(710, 2, 6, P2), dlog(710, 3, 6, P2)]                          # tx 6: two deposits, two notes
    N = [nlog(120, 0, 1, ALICE), nlog(130, 0, 2, ALICE), nlog(140, 0, 3, BOB), nlog(450, 0, 4, ERIN),
         nlog(710, 0, 6, ALICE, EMIT2), nlog(710, 1, 6, ALICE, EMIT2)]
    return D, N, [wlog(150, 5, 50)]


@pytest.fixture
def env(tmp_path):
    D, N, W = world()
    d = tmp_path
    a = {
        "junef_deposit": store(d / "jd", POOLS, [[g1.DEPOSIT_TOPIC]], SPANS["junef"], [l for l in D if int(l["blockNumber"], 16) <= 500]),
        "junef_note": store(d / "jn", None, [[g1.WITHDRAWAL_TOPIC, g1.ENCRYPTED_NOTE_TOPIC]], SPANS["junef"],
                            [l for l in N + W if int(l["blockNumber"], 16) <= 500]),
        "s22_deposit": store(d / "sd", POOLS, [[g1.DEPOSIT_TOPIC]], SPANS["s22"], [l for l in D if int(l["blockNumber"], 16) >= 400]),
        "s22_note": store(d / "sn", None, [[g1.ENCRYPTED_NOTE_TOPIC]], SPANS["s22"], [l for l in N if int(l["blockNumber"], 16) >= 400]),
        "cohort": write_csv(d / "cohort.csv", ["wallet_id", "address", "arm"],
                            [[0, ALICE, "both"], [1, BOB, "deposit"], [2, CAROL, "withdrawal"], [3, DAVE, "withdrawal"], [4, ERIN, "deposit"]]),
        "match_membership": write_csv(d / "mm.csv", ["address", "arm", "matched"],
                                      [[ALICE, "exposed", "TRUE"], [BOB, "exposed", "TRUE"], [CAROL, "exposed", "TRUE"],
                                       [DAVE, "exposed", "FALSE"], [ERIN, "exposed", "TRUE"], [CTRL, "control", "TRUE"]]),
        "eligible_frame_coverage": write_csv(d / "efc.csv", ["address", "screen_status"],
                                             [[x, "retained"] for x in (ALICE, BOB, CAROL, DAVE, ERIN, CTRL)]),
    }
    return a, d


def kw(**over):
    k = dict(cutoff=CUT, spans=SPANS, oracles=None, cohort_sha=None, note_emitters=frozenset({EMIT, EMIT2}),
             usdc_pools=(P1, P2), n_pools=3)
    k.update(over)
    return k


def test_counts_flags_and_withdrawal_only_zero(env):
    a, _ = env
    out = h2.produce(a, POOLS, **kw())
    assert dict(out["deposit_counts"]) == {ALICE: 4, BOB: 1, CAROL: 0, ERIN: 1}       # overlap deposit counted once; DAVE unmatched -> absent
    assert dict(out["usdc_pool_flag"]) == {ALICE: "TRUE", BOB: "FALSE", CAROL: "FALSE", DAVE: "FALSE", ERIN: "FALSE"}
    r = out["receipt"]["results"]
    assert (r["deposit_logs"], r["matched"], r["residual"]) == (7, 6, 1) and r["duplicates_collapsed"] == {"deposit": 1, "encryptednote": 1}
    assert r["zero"] == 1 and r["flagged"] == 1


def test_unresolved_deposit_reported_not_attributed(env, tmp_path):
    a, d = env
    D, N, W = world()
    D.append(dlog(800, 1, 7, P1))      # no note
    a.update(s22_deposit=store(d / "sd2", POOLS, [[g1.DEPOSIT_TOPIC]], SPANS["s22"], [l for l in D if int(l["blockNumber"], 16) >= 400]))
    r = h2.produce(a, POOLS, **kw())["receipt"]["results"]
    assert r["residual"] == 2 and r["unresolved_usdc"][P1] == 1 and r["unresolved_usdc"][P2] == 1 and r["matched"] == 6


def test_post_cutoff_logs_filtered(env):
    a, d = env
    D, N, W = world()
    D2, N2 = D + [dlog(990, 1, 8, P2), dlog(1000, 1, 9, P2)], N + [nlog(990, 0, 8, BOB), nlog(1000, 0, 9, BOB)]
    # S22 store covers [400, 1000]; a block-1000 log is inside coverage; tighten the cutoff to 995 so 1000 is filtered
    a.update(s22_deposit=store(d / "sd3", POOLS, [[g1.DEPOSIT_TOPIC]], SPANS["s22"], [l for l in D2 if int(l["blockNumber"], 16) >= 400]),
             s22_note=store(d / "sn3", None, [[g1.ENCRYPTED_NOTE_TOPIC]], SPANS["s22"], [l for l in N2 if int(l["blockNumber"], 16) >= 400]))
    out = h2.produce(a, POOLS, **kw(cutoff=995))
    assert dict(out["deposit_counts"])[BOB] == 2          # block 990 counted, block 1000 dropped
    assert out["receipt"]["chunk_proofs"]["s22_deposit"]["dropped_above_cutoff"] == 1


def test_multi_sender_tx_fails(env):
    a, d = env
    D, N, W = world()
    N2 = N + [nlog(700, 0, 5, BOB)] + [nlog(700, 1, 5, ALICE)]       # tx 5: two notes (and one deposit) AND two senders
    a.update(s22_note=store(d / "sn4", None, [[g1.ENCRYPTED_NOTE_TOPIC]], SPANS["s22"], [l for l in N2 if int(l["blockNumber"], 16) >= 400]))
    with pytest.raises(h2.H2Error, match="2 distinct note senders"):
        h2.produce(a, POOLS, **kw())


def test_note_count_mismatch_fails(env):
    a, d = env
    D, N, W = world()
    N2 = [l for l in N if not (l["transactionHash"] == TX(6) and l["logIndex"] == hex(1))]   # tx 6 keeps 1 note for 2 deposits
    a.update(s22_note=store(d / "sn5", None, [[g1.ENCRYPTED_NOTE_TOPIC]], SPANS["s22"], [l for l in N2 if int(l["blockNumber"], 16) >= 400]))
    with pytest.raises(h2.H2Error, match="1 notes != 2 Deposit logs"):
        h2.produce(a, POOLS, **kw())


def test_unpinned_emitter_fails(env):
    a, _ = env
    with pytest.raises(h2.H2Error, match="outside the pinned set"):
        h2.produce(a, POOLS, **kw(note_emitters=frozenset({EMIT})))


def test_conflicting_duplicate_log_fails(env):
    a, d = env
    D, N, W = world()
    bad = dict(dlog(450, 1, 4, P3), data="0x99")                        # same (tx, logIndex) as the S21 copy, different content
    a.update(s22_deposit=store(d / "sd6", POOLS, [[g1.DEPOSIT_TOPIC]], SPANS["s22"],
                               [bad if (l["transactionHash"], l["logIndex"]) == (TX(4), hex(1)) else l for l in D if int(l["blockNumber"], 16) >= 400]))
    with pytest.raises(h2.H2Error, match="conflicting duplicate"):
        h2.produce(a, POOLS, **kw())


def test_chunk_coverage_gap_fails(env):
    a, d = env
    import os
    os.remove(sorted(p for p in (d / "jd").iterdir() if p.name.startswith("chunk-"))[1])
    with pytest.raises(h2.H2Error, match="gap"):
        h2.produce(a, POOLS, **kw())


def test_depositor_cohort_oracle_fails(env):
    a, d = env
    write_csv(d / "cohort.csv", ["wallet_id", "address", "arm"], [[0, ALICE, "both"], [1, BOB, "withdrawal"], [2, ERIN, "deposit"]])
    with pytest.raises(h2.H2Error, match="depositor set"):
        h2.produce(a, POOLS, **kw())


def test_cohort_sha_asserted(env):
    a, _ = env
    with pytest.raises(h2.H2Error, match="cohort sha256"):
        h2.produce(a, POOLS, **kw(cohort_sha="0" * 64))


def test_oracle_mismatch_fails(env):
    a, _ = env
    o = {"deposit_logs": 8, "matched": 6, "residual": 1, "n_matched_exposed": 4, "n_eligible_exposed": 5}
    with pytest.raises(h2.H2Error, match="oracle deposit_logs"):
        h2.produce(a, POOLS, **kw(oracles=o))


def test_eligible_exposed_missing_from_frame_coverage_fails(env):
    a, d = env
    write_csv(d / "efc.csv", ["address", "screen_status"], [[ALICE, "retained"]])
    with pytest.raises(h2.H2Error, match="eligible_frame_coverage"):
        h2.produce(a, POOLS, **kw())


def test_quantile_type7():
    assert h2.quantile7([0, 0, 1, 2, 5], 0.5) == 1.0 and h2.quantile7([0, 1], 0.5) == 0.5 and h2.quantile7([0, 0, 0, 3], 2 / 3) == 0.0


def test_write_outputs_atomic_and_failure_writes_nothing(env, tmp_path, monkeypatch):
    a, _ = env
    out = h2.produce(a, POOLS, **kw())
    dest = tmp_path / "o" / "new"
    h2.write_outputs(out, str(dest))
    assert sorted(p.name for p in dest.iterdir()) == ["deposit_counts.csv", "h2_inputs_receipt.json", "h2_inputs_summary.md", "usdc_pool_flag.csv"]
    assert (dest / "deposit_counts.csv").read_text().splitlines()[0] == "address,deposit_count"
    assert json.loads((dest / "h2_inputs_receipt.json").read_text())["output_sha256"]
    with pytest.raises(h2.H2Error, match="already exists"):
        h2.write_outputs(out, str(dest))
    assert not [p for p in (tmp_path / "o").iterdir() if p.name.startswith(".h2-")]


def test_cli_through_run_all_fails_closed_exit_3(env, tmp_path, capsys):
    a, _ = env
    out = tmp_path / "cli_out"
    argv = ["--gate", "h2_inputs", "--outdir", str(out), "--cohort", a["cohort"], "--match-membership", a["match_membership"],
            "--eligible-frame-coverage", a["eligible_frame_coverage"], "--junef-deposit", a["junef_deposit"], "--junef-note", a["junef_note"],
            "--s22-deposit", a["s22_deposit"], "--s22-note", a["s22_note"]]
    assert run_all.main(argv) == 3                       # pinned cohort sha / real blocks do not match the fixture
    assert "REFUSED (nothing written)" in capsys.readouterr().out and not out.exists()


# --- review fixes (security round): binding trust, symlinks, malformed input, write lock ---------------------------------

def _rebind(path, mutate):
    bp = path + "/binding.json"
    b = json.load(open(bp))
    mutate(b["query"])
    json.dump(b, open(bp, "w"))


@pytest.mark.parametrize("key,mut", [
    ("junef_deposit", lambda q: q.update(endpoint="mevblocker")),          # wrong endpoint for a junef store
    ("s22_deposit", lambda q: q.update(endpoint="tenderly")),              # wrong endpoint for an S22 store
    ("junef_deposit", lambda q: q.update(address=q["address"][:-1], shape="multi_address")),   # pool set shrunk
    ("s22_deposit", lambda q: q.update(address=list(reversed(q["address"])) + [addr(1)])),     # pool set grown
    ("junef_note", lambda q: q.update(topics=[[g1.ENCRYPTED_NOTE_TOPIC]])),                     # topics narrowed
    ("s22_note", lambda q: q.update(address=[P1], shape="single_address")),                      # note store address-scoped
    ("s22_note", lambda q: q.update(shape="multi_address")),                                       # shape lie
])
def test_store_binding_must_match_expected(env, key, mut):
    a, _ = env
    _rebind(a[key], mut)
    with pytest.raises(h2.H2Error, match="binding query"):
        h2.produce(a, POOLS, **kw())


def test_all_four_stores_bind_proof_recorded_in_receipt(env):
    a, _ = env
    r = h2.produce(a, POOLS, **kw())["receipt"]
    for k, ep in (("junef_deposit", "tenderly"), ("junef_note", "tenderly"), ("s22_deposit", "mevblocker"), ("s22_note", "mevblocker")):
        pr = r["chunk_proofs"][k]
        assert pr["binding_endpoint"] == ep and len(pr["binding_sha256"]) == 64
    assert r["oracles_enforced"] is False and r["pinned_oracles"] is None
    assert r["designated_pools"] == sorted(POOLS)


def test_pinned_oracles_recorded_when_enforced():
    from tools import h2_inputs as m
    assert m.ORACLES["deposit_logs"] == 163_696          # the pins that a production receipt carries as `pinned_oracles`


def test_symlinked_inputs_refused(env, tmp_path):
    a, d = env
    import os
    os.symlink(a["cohort"], d / "cohort_link.csv")
    with pytest.raises(h2.H2Error, match="symlink"):
        h2.produce({**a, "cohort": str(d / "cohort_link.csv")}, POOLS, **kw())
    os.symlink(a["junef_deposit"], d / "jd_link")
    with pytest.raises(h2.H2Error, match="symlink|real directory"):
        h2.produce({**a, "junef_deposit": str(d / "jd_link")}, POOLS, **kw())
    chunk = sorted(p for p in (d / "sn").iterdir() if p.name.startswith("chunk-"))[0]
    real = d / "real_chunk"
    chunk.rename(real)
    os.symlink(real, chunk)
    with pytest.raises(h2.H2Error, match="symlink"):
        h2.produce(a, POOLS, **kw())


def test_malformed_input_is_h2error_not_traceback(env):
    a, d = env
    (d / "cohort.csv").write_text("wallet_id\n1\n")                       # no address column -> KeyError internally
    with pytest.raises(h2.H2Error, match="malformed input"):
        h2.produce(a, POOLS, **kw())
    (d / "jd" / "binding.json").write_text("{not json")
    with pytest.raises(h2.H2Error, match="malformed input"):
        h2.produce(a, POOLS, **kw())


def test_wrong_pool_count_fails(env):
    a, _ = env
    with pytest.raises(h2.H2Error, match="designated pools"):
        h2.produce(a, POOLS[:2], **kw())


def test_outdir_lock_is_mkdir_and_failure_cleans_up(env, tmp_path, monkeypatch):
    a, _ = env
    out = h2.produce(a, POOLS, **kw())
    dest = tmp_path / "locked"
    dest.mkdir()                                                           # pre-existing (even empty) dir refused
    with pytest.raises(h2.H2Error, match="already exists"):
        h2.write_outputs(out, str(dest))
    fail = tmp_path / "failing"
    monkeypatch.setattr(h2, "_fsync_dir", lambda p: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        h2.write_outputs(out, str(fail))
    assert not fail.exists() and not [p for p in tmp_path.iterdir() if p.name.startswith(".h2-")]
