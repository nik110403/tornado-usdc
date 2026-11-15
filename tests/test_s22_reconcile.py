"""diagnostics/s22_reconcile.py -- fixture world + fake RPC. No network, no out/ reads."""
import argparse
import builtins
import inspect
import json
import os

import pytest

from diagnostics import s22_reconcile as rc
from gates import g1_cohort as g1
from gates import g1b_frame as g1b
from lib import rpc

PRE_END = 20_000
PINNED = {"census_start": {"block": 100}, "june_end": {"block": 200}, "pre_period_start": {"block": 1000},
          "pre_period_end": {"block": PRE_END}}
FETCH_LO = 150
WINDOWS = g1b.original_windows(PINNED)


def a(i):
    return "0x%040x" % i


def pad(addr):
    return "0x" + addr[2:].rjust(64, "0")


P1, P2, PX = a(0x1001), a(0x1002), a(0x1003)  # two designated pools + a foreign instance
ALICE, BOB, CAROL, DAVE, ERIN, FRANK = (a(i) for i in (0xA1, 0xB2, 0xC3, 0xD4, 0xE5, 0xF6))
S1, S2, C1 = a(0x51), a(0x52), a(0x53)
ZERO = rc.ZERO
TX = lambda n: "0x" + ("%064x" % n)  # noqa: E731


def log(block, idx, tx, address, topics, data="0x"):
    return {"blockNumber": hex(block), "logIndex": hex(idx), "transactionHash": TX(tx), "blockHash": TX(block),
            "address": address, "topics": topics, "data": data}


def wlog(block, tx, pool, recipient):
    return log(block, 1, tx, pool, [g1.WITHDRAWAL_TOPIC, pad(a(0x99))], "0x" + pad(recipient)[2:] + "00" * 64)


def dlog(block, tx, pool):
    return log(block, 2, tx, pool, [g1.DEPOSIT_TOPIC, TX(7)], "0x" + "00" * 64)


def nlog(block, tx, who, instance=P1):
    return log(block, 0, tx, instance, [g1.ENCRYPTED_NOTE_TOPIC, pad(who)])


def tlog(block, tx, token, frm, to):
    return log(block, 3, tx, token, [g1.TRANSFER_TOPIC, pad(frm), pad(to)], "0x1")


WORLD_TORNADO = [
    wlog(105, 1, PX, FRANK),    # foreign instance: all-instance exclusion only
    wlog(120, 2, P1, ALICE),
    wlog(160, 3, P2, CAROL),    # overlap [150, 200]
    wlog(250, 4, P1, ZERO),     # 0x0 recipient
    nlog(110, 5, ERIN, PX),
    nlog(130, 6, BOB),
    nlog(170, 8, CAROL, P2),    # overlap
    nlog(260, 10, DAVE),
    dlog(130, 6, P1),
    dlog(140, 11, P1),          # no EncryptedNote -> residual
    dlog(170, 8, P2),           # overlap
    dlog(260, 10, P1),
]
def solo(k):
    """A block inside window k and inside no other window (windows can overlap in a small fixture)."""
    s0, e0 = WINDOWS[k]
    others = [w for i, w in enumerate(WINDOWS) if i != k]
    return next(b for b in range(s0, e0 + 1) if not any(s <= b <= e for s, e in others))


WORLD_FRAME = [  # window 0: alice->s1 ; window 1: s1->s2, c1->s2
    tlog(solo(0), 20, g1.USDC.lower(), ALICE, S1),
    tlog(solo(1), 21, g1.USDT.lower(), S1, S2),
    tlog(solo(1), 22, g1.USDT.lower(), C1, S2),
]
NEXT_INDEX = {P1: 3, P2: 1}


class FakeNet:
    def __init__(self, world, next_index=None, fail_fetch=False):
        self.world, self.next_index = world, next_index or dict(NEXT_INDEX)
        self.endpoints, self.requests, self.fail_fetch = [], [], fail_fetch

    def chain_id(self, endpoint):
        self.endpoints.append(endpoint)
        return 1

    def fetch(self, endpoint, lo, hi, address, topics):
        self.endpoints.append(endpoint)
        self.requests.append((lo, hi))
        if self.fail_fetch:
            raise rpc.GapError("provider down")
        t0 = {t.lower() for t in topics[0]}
        out = [l for l in self.world
               if lo <= int(l["blockNumber"], 16) <= hi and l["topics"][0].lower() in t0
               and (address is None or l["address"].lower() in address)]
        return out, json.dumps(out).encode()

    def call(self, endpoint, to, data, block):
        self.endpoints.append(endpoint)
        assert data == rc.NEXT_INDEX_SELECTOR and block == PINNED["pre_period_end"]["block"]
        return hex(self.next_index[to])


def write_chunks(directory, address, topics, ranges, world):
    binding = rpc.make_binding("c0ffee:0", "cfg", {"x": "y"}, "tenderly", address, topics)
    store = rpc.ChunkStore(str(directory), binding)
    t0 = {t.lower() for t in topics[0]}
    for lo, hi in ranges:
        logs = [l for l in world if lo <= int(l["blockNumber"], 16) <= hi and l["topics"][0].lower() in t0
                and (address is None or l["address"].lower() in {x.lower() for x in address})]
        store._write_chunk(lo, hi, logs, b"body")


def build(tmp_path, screen_drop=(), screen_extra=(), excl_drop=(), g1b=True, world=None):
    world = (world or []) + WORLD_TORNADO + WORLD_FRAME
    s22 = tmp_path / "s22out"
    (s22 / "g0").mkdir(parents=True)
    (s22 / "g1").mkdir()
    (s22 / "g0" / "manifest.csv").write_text(
        "address,class,in_circle_38\n" + "".join(f"{p},instance,True\n" for p in (P1, P2)) + f"{PX},instance,False\n")
    screen = {ALICE: "withdrawal", BOB: "deposit", CAROL: "both", DAVE: "deposit"}
    for x in screen_drop:
        screen.pop(x)
    screen.update({x: "withdrawal" for x in screen_extra})
    (s22 / "g1" / "eligibility_screen.csv").write_text(
        "address,arm,screen,screen_block\n" + "".join(f"{k},{v},eoa,{PRE_END}\n" for k, v in screen.items()))
    (s22 / "g1" / "summary.md").write_text(
        "Withdrawal arm: 3 logs, 3 unique recipients.\n"
        "Deposit arm: 4 designated-pool Deposit logs, 4 EncryptedNote logs (all instances), 3 matched (75.0%), "
        "1 unmatched residual\n")
    recs = []
    for topic, ac, spans in ((g1.WITHDRAWAL_TOPIC, 2, [(100, 149, 1), (150, PRE_END, 2)]),
                             (g1.DEPOSIT_TOPIC, 2, [(100, 149, 2), (150, PRE_END, 2)]),
                             (g1.ENCRYPTED_NOTE_TOPIC, 0, [(100, 149, 2), (150, PRE_END, 2)])):
        recs += [{"gate": "g1", "endpoint": "tenderly", "addressCount": ac, "topics": [[topic]], "fromBlock": f,
                  "toBlock": t, "logCount": n, "sha256": "ab"} for f, t, n in spans]
    recs.append({"gate": "g1", "endpoint": "mevblocker", "addressCount": 1, "topics": [g1.TRANSFER_TOPIC],
                 "fromBlock": 1, "toBlock": 2, "logCount": 9, "sha256": "cd"})  # noise line
    (s22 / "g1" / "receipts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))

    s21 = tmp_path / "s21.work"
    pools = sorted([P1, P2])
    write_chunks(s21 / "census_chunks" / "sweep_a", None, [[g1.WITHDRAWAL_TOPIC, g1.ENCRYPTED_NOTE_TOPIC]],
                 [(100, 150), (151, 200)], world)
    write_chunks(s21 / "census_chunks" / "sweep_b", pools, [[g1.DEPOSIT_TOPIC]], [(100, 200)], world)

    g1b_dir = tmp_path / "g1b"
    g1b_dir.mkdir()
    if g1b:
        excl = {ALICE, BOB, CAROL, DAVE, ERIN, FRANK, ZERO} - set(excl_drop)
        (g1b_dir / "exclusion.csv").write_text("wallet_id,address,source\n" + "".join(
            f"{i},{x},both\n" for i, x in enumerate(sorted(excl))))
        (g1b_dir / "frame_survivors.csv").write_text(
            "wallet_id,address,windows_seen,transfer_count,weight_ipw\n"
            f"1,{S1},2,2,1.0\n2,{S2},1,2,1.0\n")
        uniq = {0: 2, 1: 3}
        rows = "".join(f"| {i} | {s}-{e} | {uniq.get(i, 0)} | 0 |\n" for i, (s, e) in enumerate(WINDOWS))
        (g1b_dir / "summary.md").write_text(
            f"Dropped -- in exclusion set: 1. Dropped -- has code at {PRE_END}: 1.\n\n| window | block range | unique addrs |"
            " survivor addrs |\n|---|---|---|---|\n" + rows)
        logn = {0: 1, 1: 2}
        (g1b_dir / "receipts.jsonl").write_text("".join(json.dumps(
            {"gate": "g1b", "endpoint": "tenderly", "addressCount": 2, "topics": [[g1.TRANSFER_TOPIC]],
             "fromBlock": s, "toBlock": e, "logCount": logn.get(i, 0), "sha256": "ee"}) + "\n"
            for i, (s, e) in enumerate(WINDOWS)))
    args = argparse.Namespace(s22_out=str(s22), s21_work=str(s21), g1b_out=str(g1b_dir),
                              outdir=str(tmp_path / "rec_out"), endpoint="mevblocker", dry_run=False)
    return args, world


@pytest.fixture(autouse=True)
def _no_cooldown(monkeypatch):
    monkeypatch.setattr(rc, "COOLDOWNS", ())
    monkeypatch.setattr(rpc.os, "fsync", lambda fd: None)  # fixture chunks need no durability


def go(args, world, **netkw):
    net = FakeNet(world, **netkw)
    code, res = rc.run(args, net=net, pinned=PINNED, fetch_lo=FETCH_LO)
    return code, res, net


def status(res):
    return {c["id"]: c["status"] for c in res["checks"]}


def test_equal_sets_pass(tmp_path):
    args, world = build(tmp_path)
    att = tmp_path / "s21.work" / "census_chunks" / "sweep_a" / "attempts.jsonl"
    before = att.read_text()
    code, res, net = go(args, world)
    assert code == 0, status(res)
    assert att.read_text() == before  # S21 work dir untouched by the read-only store
    assert set(status(res).values()) == {"PASS"}, status(res)
    assert set(net.endpoints) == {"mevblocker"}
    out = tmp_path / "rec_out"
    assert {"reconcile.json", "summary.md", "receipts.jsonl"} <= set(os.listdir(out))
    disk = json.load(open(out / "reconcile.json"))
    assert disk["endpoint"] == "mevblocker" and disk["chain_id"] == 1 and disk["block_bounds"]["fetch_hi"] == PRE_END
    assert "s22/g1/receipts.jsonl" in disk["input_sha256"] and "g1b/exclusion.csv" in disk["input_sha256"]
    assert any(k.startswith("s21/sweep_a/chunk-") for k in disk["input_sha256"])
    assert len(open(out / "receipts.jsonl").read().splitlines()) > 5


def test_missing_address_fails_with_diff(tmp_path):
    args, world = build(tmp_path, screen_drop=[DAVE])
    code, res, _ = go(args, world)
    assert code == 1
    st = status(res)
    assert st["a2_depositors_set_equality"] == "FAIL" and st["a1_recipients_set_equality"] == "PASS"
    a2 = next(c for c in res["checks"] if c["id"] == "a2_depositors_set_equality")
    assert a2["rebuilt_only"] == 1 and a2["rebuilt_only_first"] == [DAVE]
    assert st["a5_full_span_totals"] == "FAIL"  # screen row count too


def test_extra_screen_address_fails(tmp_path):
    args, world = build(tmp_path, screen_extra=[a(0x77)])
    code, res, _ = go(args, world)
    assert code == 1 and status(res)["a1_recipients_set_equality"] == "FAIL"


def test_zero_address_handling(tmp_path):
    args, world = build(tmp_path)
    code, res, _ = go(args, world)
    a1 = next(c for c in res["checks"] if c["id"] == "a1_recipients_set_equality")
    assert a1["zero_in_rebuilt"] and not a1["zero_in_screen"] and a1["equal"] and a1["status"] == "PASS"
    # the screen must never carry 0x0
    (tmp_path / "z").mkdir()
    args2, world2 = build(tmp_path / "z", screen_extra=[ZERO])
    code2, res2, _ = go(args2, world2)
    assert code2 == 1 and status(res2)["a1_recipients_set_equality"] == "FAIL"


def test_fallback_disabled_raises(tmp_path):
    args, world = build(tmp_path)
    net = FakeNet(world, fail_fetch=True)
    with pytest.raises(rpc.GapError):
        rc.run(args, net=net, pinned=PINNED, fetch_lo=FETCH_LO)
    assert set(net.endpoints) == {"mevblocker"}  # never another provider
    assert not os.path.exists(tmp_path / "rec_out" / "reconcile.json")


def test_independent_provider_required(tmp_path):
    args, world = build(tmp_path)
    args.endpoint = "tenderly"
    with pytest.raises(ValueError, match="independent"):
        go(args, world)
    assert not os.path.exists(args.outdir)


def test_existing_outdir_refused(tmp_path):
    args, world = build(tmp_path)
    os.makedirs(args.outdir)
    net = FakeNet(world)
    with pytest.raises(FileExistsError):
        rc.run(args, net=net, pinned=PINNED, fetch_lo=FETCH_LO)
    assert net.endpoints == []


def test_outdir_inside_input_refused(tmp_path):
    args, world = build(tmp_path)
    args.outdir = os.path.join(args.s22_out, "new")
    with pytest.raises(ValueError, match="inside"):
        go(args, world)


def test_next_index_mismatch_fails(tmp_path):
    args, world = build(tmp_path)
    code, res, _ = go(args, world, next_index={P1: 3, P2: 2})
    b = next(c for c in res["checks"] if c["id"] == "b_next_index_sum")
    assert code == 1 and b["status"] == "FAIL" and b["sum_nextIndex"] == 5 and b["s22_summary_deposit_logs"] == 4


def test_frame_and_exclusion_pending_when_g1b_absent(tmp_path):
    args, world = build(tmp_path, g1b=False)
    code, res, _ = go(args, world)
    st = status(res)
    assert code == 0 and st["c_frame_windows"] == "PENDING" and st["d_exclusion_closure"] == "PENDING"
    assert all(v == "PASS" for k, v in st.items() if k[0] in "ab")


def test_exclusion_shortfall_fails(tmp_path):
    args, world = build(tmp_path, excl_drop=[FRANK])  # an S21 all-instance recipient missing from g1b
    code, res, _ = go(args, world)
    d = next(c for c in res["checks"] if c["id"] == "d_exclusion_closure")
    assert code == 1 and d["status"] == "FAIL" and d["per_source"]["s21_all_instance"]["first"] == [FRANK]


def test_frame_window_mismatch_fails(tmp_path):
    extra = [tlog(solo(5), 30, g1.USDC.lower(), S1, a(0x60))]  # g1b never saw this transfer
    args, world = build(tmp_path, world=extra)
    code, res, _ = go(args, world)
    c = next(c for c in res["checks"] if c["id"] == "c_frame_windows")
    assert code == 1 and c["status"] == "FAIL" and list(WINDOWS[5]) in c["windows_mismatching"]


def test_overlap_disagreement_fails(tmp_path):
    args, world = build(tmp_path)
    net = FakeNet(world)
    orig = net.fetch

    def lying(endpoint, lo, hi, address, topics):  # fetch reports a different recipient for the overlap withdrawal
        logs, body = orig(endpoint, lo, hi, address, topics)
        logs = [dict(l, data="0x" + pad(BOB)[2:] + "00" * 64) if l["transactionHash"] == TX(3) else l for l in logs]
        return logs, body

    net.fetch = lying
    code, res = rc.run(args, net=net, pinned=PINNED, fetch_lo=FETCH_LO)
    st = status(res)
    assert code == 1 and st["a4_overlap_agreement"] == "FAIL"


def test_dry_run_plans_without_network(tmp_path, capsys):
    args, world = build(tmp_path)
    args.dry_run = True
    net = FakeNet(world)
    code, plan = rc.run(args, net=net, pinned=PINNED, fetch_lo=FETCH_LO)
    assert code == 0 and net.endpoints == [] and not os.path.exists(args.outdir)
    assert plan["pools"] == 2 and plan["a_getLogs"]["total"] == 6 and plan["c_getLogs"]["windows"] == 30
    assert plan["b_eth_call"] == 2 and '"a_getLogs"' in capsys.readouterr().out


def test_receipts_are_streamed(tmp_path, monkeypatch):
    assert inspect.isgeneratorfunction(rc.stream_receipts)
    path = tmp_path / "big.jsonl"
    with open(path, "w") as f:
        for i in range(20000):
            f.write(json.dumps({"endpoint": "tenderly", "addressCount": 2, "gate": "g1", "topics": [[g1.WITHDRAWAL_TOPIC]],
                                "fromBlock": i, "toBlock": i, "logCount": 1}) + "\n")

    class Lines:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *e):
            self.fh.close()

        def __iter__(self):
            return iter(self.fh)

        def read(self, *a):
            raise AssertionError("whole-file read")

        readlines = read

    real_open = builtins.open
    monkeypatch.setattr(rc, "open", lambda p, m="r", *a, **k: Lines(real_open(p, m, *a, **k)), raising=False)
    out = rc.scan_receipts(str(path), {json.dumps([[g1.WITHDRAWAL_TOPIC]]): ("W", "tenderly", {2}, "g1")})
    assert out["lines"] == 20000 and len(out["ranges"]["W"]) == 20000
    monkeypatch.undo()
    assert out["sha256"] == rc.sha256_file(str(path))
    assert rc.span_sum(out["ranges"]["W"], 0, 19999) == (20000, True, "tiled")
    assert rc.span_sum(out["ranges"]["W"][1:], 0, 19999)[1] is False


def test_binding_mismatch_blocks(tmp_path):
    args, world = build(tmp_path)
    bpath = tmp_path / "s21.work" / "census_chunks" / "sweep_b" / "binding.json"
    b = json.load(open(bpath))
    b["query"]["topics"] = [[g1.WITHDRAWAL_TOPIC]]
    json.dump(b, open(bpath, "w"))
    with pytest.raises(rpc.CheckpointError):
        go(args, world)
