"""S16: g1b contract screen is anchored at pre_period_end, not event_block. Mocked RPC; no network, no out/ reads."""
import csv
import json

from gates import g1b_frame as g1b

PRE_START, PRE_END, EVENT, CENSUS = 1000, 1999, 3000, 10
ZERO_T = "0x" + "0" * 64


def _t(a):
    return "0x" + a[2:].rjust(64, "0")


EOA = "0x" + "1" * 40
LATE_CONTRACT = "0x" + "2" * 40   # code only after the cutoff -> must survive
EARLY_CONTRACT = "0x" + "3" * 40  # code at the cutoff -> dropped
EXCLUDED = "0x" + "4" * 40        # Tornado exclusion-set member -> dropped without a code check


def _setup(tmp_path, monkeypatch, stale_checkpoint=False):
    monkeypatch.setattr(g1b, "_OUT_DIR", str(tmp_path))
    monkeypatch.setattr(g1b, "_RECEIPTS_PATH", str(tmp_path / "receipts.jsonl"))
    ckpt = tmp_path / "code_check.jsonl"
    monkeypatch.setattr(g1b, "_CODE_CHECKPOINT_PATH", str(ckpt))
    if stale_checkpoint:  # a prior event_block-anchored run must not be reused
        ckpt.write_text(json.dumps({"address": EARLY_CONTRACT, "has_code": False, "block": EVENT}) + "\n")
    code_blocks, sweep_to = [], []

    def chunked(endpoint, frm, to, address, topics, gate, window):
        sweep_to.append(to)
        return [{"topics": [g1b.WITHDRAWAL_TOPIC, ZERO_T], "data": "0x" + EXCLUDED[2:].rjust(64, "0") + "00" * 64}]

    def get_logs(endpoint, frm, to, address, topics, gate):
        return [{"topics": [g1b.TRANSFER_TOPIC, _t(a), _t(b)], "data": "0x1"}
                for a, b in ((EOA, LATE_CONTRACT), (EARLY_CONTRACT, EXCLUDED))]

    def batch_code(endpoint, addrs, block, batch_size=1500):
        code_blocks.append(block)
        return {a: ("0x6080" if (a == EARLY_CONTRACT or (a == LATE_CONTRACT and block >= EVENT)) else "0x")
                for a in addrs}

    monkeypatch.setattr(g1b, "_get_logs_chunked", chunked)
    monkeypatch.setattr(g1b.rpc, "get_logs", get_logs)
    monkeypatch.setattr(g1b, "_batch_get_code", batch_code)
    monkeypatch.setattr(g1b, "_line_count", lambda p: 0)
    monkeypatch.setattr(g1b, "_new_receipts", lambda n: [])
    monkeypatch.setattr(g1b, "_assert_component_tiled", lambda *a, **k: None)
    monkeypatch.setattr(g1b, "_cross_endpoint_reverify", lambda r: [])
    monkeypatch.setattr(g1b, "_read_s2_floor_count", lambda: None)
    blocks = {k: {"block": v} for k, v in dict(census_start=CENSUS, pre_period_start=PRE_START,
                                               pre_period_end=PRE_END, event_block=EVENT).items()}
    return {"blocks": blocks}, code_blocks, sweep_to


def _survivors(tmp_path):
    with open(tmp_path / "frame_survivors.csv", newline="") as f:
        return {r["address"] for r in csv.DictReader(f)}


def test_contract_screen_anchored_at_pre_period_end(tmp_path, monkeypatch):
    cfg, code_blocks, sweep_to = _setup(tmp_path, monkeypatch)
    g1b.run(cfg)
    assert set(code_blocks) == {PRE_END}      # never event_block
    assert set(sweep_to) == {PRE_END}
    # code appearing only after the cutoff does not drop a candidate; code at the cutoff does
    assert _survivors(tmp_path) == {EOA, LATE_CONTRACT}
    assert f"has code at {PRE_END}" in (tmp_path / "summary.md").read_text()


def test_stale_event_block_checkpoint_not_reused(tmp_path, monkeypatch):
    cfg, code_blocks, _ = _setup(tmp_path, monkeypatch, stale_checkpoint=True)
    g1b.run(cfg)
    assert _survivors(tmp_path) == {EOA, LATE_CONTRACT}   # stale has_code=False @event_block ignored
    assert code_blocks and set(code_blocks) == {PRE_END}
