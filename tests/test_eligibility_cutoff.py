"""S16: g1 end-to-end on a synthetic chain with mocked RPC. No network, no out/ reads.

Fixture addresses (all lowercase keys):
  alice  withdrawal @1500 (pre-cutoff), EOA                       -> in cohort
  bob    withdrawal @2200 (event-block-only interaction)          -> NOT in cohort
  carol  withdrawal @1500, code appears @2300 (event-block-only)  -> stays (EOA at cutoff)
  dave   withdrawal @1500, contract created @1800 (pre-cutoff)    -> excluded
  erin   withdrawal @1200 + EncryptedNote deposit @1300 (both)    -> both arms
  frank  withdrawal @1100, no Transfer, passive balance           -> rule-dependent
"""
import csv
import json
from datetime import datetime, timezone

import pytest

from gates import g1_cohort as g1

PRE_START, PRE_END, EVENT, CENSUS = 1000, 2000, 2500, 10
POOL = "0x" + "aa" * 20
RELAYER_T = "0x" + "0" * 24 + "bb" * 20
ADDR = {n: "0x" + c * 40 for n, c in
        dict(alice="1", bob="2", carol="3", dave="4", erin="5", frank="6", peer="7").items()}
NAMES = {v: k for k, v in ADDR.items()}
TS = hex(int(datetime(2022, 6, 20, 12, tzinfo=timezone.utc).timestamp()))
PASSIVE = 500 * 10 ** 6
BAL0 = {"alice": 300 * 10 ** 6, "carol": 300 * 10 ** 6, "erin": 300 * 10 ** 6, "frank": PASSIVE}


def _pad(a):
    return "0x" + a[2:].rjust(64, "0")


def _wd(block, who, i):
    return {"address": POOL, "blockNumber": hex(block), "transactionHash": f"0x{i:064x}", "logIndex": hex(0),
            "topics": [g1.WITHDRAWAL_TOPIC, RELAYER_T], "data": "0x" + ADDR[who][2:].rjust(64, "0") + "00" * 64,
            "blockTimestamp": TS}


def _dep(block, i):
    return {"address": POOL, "blockNumber": hex(block), "transactionHash": f"0x{i:064x}", "logIndex": hex(1),
            "topics": [g1.DEPOSIT_TOPIC], "data": "0x", "blockTimestamp": TS}


def _note(block, who, i):
    return {"address": "0x" + "cc" * 20, "blockNumber": hex(block), "transactionHash": f"0x{i:064x}",
            "logIndex": hex(2), "topics": [g1.ENCRYPTED_NOTE_TOPIC, _pad(ADDR[who])], "data": "0x",
            "blockTimestamp": TS}


def _xfer(block, frm, to, val, i):
    return {"address": g1.USDC, "blockNumber": hex(block), "blockHash": f"0xh{block}", "transactionHash": f"0x{i:064x}",
            "logIndex": hex(0), "topics": [g1.TRANSFER_TOPIC, _pad(ADDR[frm]), _pad(ADDR[to])],
            "data": hex(val), "blockTimestamp": TS}


WITHDRAWALS = [_wd(1500, "alice", 1), _wd(2200, "bob", 2), _wd(1500, "carol", 3), _wd(1500, "dave", 4),
               _wd(1200, "erin", 5), _wd(1100, "frank", 6)]
DEPOSITS = [_dep(1300, 7)]
NOTES = [_note(1300, "erin", 7)]
TRANSFERS = [_xfer(1500, "alice", "peer", 10 ** 6, 20), _xfer(1500, "peer", "carol", 10 ** 6, 21),
             _xfer(1500, "erin", "peer", 10 ** 6, 22), _xfer(2200, "alice", "peer", 10 ** 6, 23)]
CONTRACT_FROM = {"dave": 1800, "carol": 2300, "bob": 2300}


class Chain:
    def __init__(self):
        self.log_calls, self.code_blocks = [], []

    def get_logs(self, endpoint, frm, to, address, topics, gate, window):
        self.log_calls.append((frm, to, topics[0]))
        t0 = topics[0][0] if isinstance(topics[0], list) else topics[0]
        if t0 == g1.WITHDRAWAL_TOPIC:
            pool = WITHDRAWALS
        elif t0 == g1.DEPOSIT_TOPIC:
            pool = DEPOSITS
        elif t0 == g1.ENCRYPTED_NOTE_TOPIC:
            pool = NOTES
        else:
            pool = []
            if address == g1.USDC:
                pos = 1 if len(topics) == 2 else 2
                want = topics[pos]
                pool = [l for l in TRANSFERS if l["topics"][pos] in want]
        return [l for l in pool if frm <= int(l["blockNumber"], 16) <= to]

    def get_code(self, endpoint, addrs, block):
        self.code_blocks.append(block)
        return {a: ("0x6080" if NAMES[a] in CONTRACT_FROM and block >= CONTRACT_FROM[NAMES[a]] else "0x")
                for a in addrs}

    def balance_of(self, endpoint, token, addrs, block):
        out = {}
        for a in addrs:
            bal = BAL0.get(NAMES[a], 0) if token == g1.USDC else 0
            if token == g1.USDC:
                for l in TRANSFERS:
                    b = int(l["blockNumber"], 16)
                    if PRE_START < b <= block:
                        v = int(l["data"], 16)
                        bal += v if _pad(a) == l["topics"][2] else (-v if _pad(a) == l["topics"][1] else 0)
            out[a] = bal
        return out


@pytest.fixture
def run_g1(tmp_path, monkeypatch):
    chain = Chain()
    monkeypatch.setattr(g1, "_OUT_DIR", str(tmp_path))
    monkeypatch.setattr(g1, "_RECEIPTS_PATH", str(tmp_path / "receipts.jsonl"))
    monkeypatch.setattr(g1, "_load_designated_pools", lambda: [POOL])
    monkeypatch.setattr(g1, "_load_usdt_destroyed", lambda a, b: [])
    monkeypatch.setattr(g1, "_get_logs_chunked", chain.get_logs)
    monkeypatch.setattr(g1, "_batch_get_code", chain.get_code)
    monkeypatch.setattr(g1, "_batch_balance_of", chain.balance_of)
    monkeypatch.setattr(g1, "_line_count", lambda p: 0)
    monkeypatch.setattr(g1, "_new_receipts", lambda n: [])
    monkeypatch.setattr(g1, "_assert_component_tiled", lambda *a, **k: None)
    monkeypatch.setattr(g1, "_cross_endpoint_reverify", lambda r: [])
    blocks = {k: {"block": v} for k, v in dict(census_start=CENSUS, pre_period_start=PRE_START,
                                               pre_period_end=PRE_END, event_block=EVENT).items()}

    def go(rule):
        cfg = {"blocks": blocks}
        if rule is not None:
            cfg["participation_rule"] = rule
        g1.run(cfg)  # returns 1 (population checks cannot pass on a fixture); tables are still written
        with open(tmp_path / "cohort.csv", newline="") as f:
            rows = {r["address"]: r for r in csv.DictReader(f)}
        return rows, chain, tmp_path
    return go


def test_exposure_and_contract_screen_use_pre_cutoff_not_event_block(run_g1):
    rows, chain, out = run_g1(g1.RULE_NONE)
    assert max(to for _, to, _ in chain.log_calls) == PRE_END  # no exposure fetch reaches event_block
    assert set(chain.code_blocks) == {PRE_END}
    a = ADDR
    assert a["bob"] not in rows                                 # event-block-only interaction is out
    assert a["dave"] not in rows                                # contract at the cutoff is out
    assert a["carol"] in rows                                   # code only after the cutoff does not exclude
    assert rows[a["erin"]]["arm"] == "both" and rows[a["erin"]]["both_arms"] == "True"
    assert all(k == k.lower() and len(k) == 42 for k in rows)   # stable lowercase keys
    summary = (out / "summary.md").read_text()
    assert f"pre_period_end = {PRE_END}" in summary and f"event_block {EVENT} used for neither" in summary
    with open(out / "eligibility_screen.csv", newline="") as f:
        screen = {r["address"]: r["screen"] for r in csv.DictReader(f)}
    assert screen[a["dave"]] == "contract" and screen[a["carol"]] == "eoa" and a["bob"] not in screen


def test_participation_rule_changes_only_passive_holders(run_g1):
    rows_bt, _, _ = run_g1(g1.RULE_BASELINE_TRANSFER)
    f = ADDR["frank"]
    assert rows_bt[f]["has_baseline_transfer"] == "False"
    assert rows_bt[f]["in_panel"] == "False" and rows_bt[f]["floor_pass"] == "False"
    rows_none, _, out = run_g1(g1.RULE_NONE)
    assert rows_none[f]["has_baseline_transfer"] == "False"     # flag identical under both rules
    assert rows_none[f]["in_panel"] == "True" and rows_none[f]["floor_pass"] == "True"
    assert rows_none[f]["pre_mean_total_units"] == str(PASSIVE)  # passive holding reconstructed
    for who in ("alice", "carol", "erin"):                      # transfer participants identical under both rules
        a = ADDR[who]
        assert rows_bt[a] == rows_none[a]
        assert rows_bt[a]["has_baseline_transfer"] == "True"
    assert "participation_rule=none" in json.loads((out / "cohort.schema.json").read_text())["notes"]
    assert "`none`" in (out / "summary.md").read_text()


@pytest.mark.parametrize("rule", [None, "", "maybe"])
def test_participation_rule_has_no_default(run_g1, rule):
    with pytest.raises(ValueError, match="participation_rule"):
        run_g1(rule)
