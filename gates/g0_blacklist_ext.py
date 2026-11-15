"""Gate 0 extension (additive) -- blacklist event history from contract deployment to census_start - 1.

g0 fetches Circle USDC Blacklisted/UnBlacklisted and Tether USDT AddedBlackList/RemovedBlackList only from
census_start (block 9,000,000), so the opening blacklist state at that block is unknown. This fetches the same
four events over [deployment block, census_start - 1] and writes them to a SEPARATE file,
out/g0/blacklist_events_predeploy_ext.csv; no existing g0 output is read or rewritten. Receipts go to
out/g0_ext/receipts.jsonl (lib.rpc gate="g0_ext"), tiling is asserted per contract.

Endpoint: mevblocker (block_range_cap 50,000), NOT tenderly -- tenderly's quota is reserved for the g4_june sweep.
Deployment blocks (first block with non-empty eth_getCode, binary-searched 2026-10-16 on mevblocker AND drpc,
identical): USDC proxy 6,082,465; USDT 4,634,748.

Run: python3 -m gates.g0_blacklist_ext
"""
import csv
import json
import os
import sys
import time

from lib import blocks, keccak, rpc

_HERE = os.path.dirname(os.path.abspath(__file__))
_G0_DIR = os.path.join(_HERE, "..", "out", "g0")
_RECEIPTS_PATH = os.path.join(_HERE, "..", "out", "g0_ext", "receipts.jsonl")
OUT_CSV = os.path.join(_G0_DIR, "blacklist_events_predeploy_ext.csv")
GATE = "g0_ext"
ENDPOINT = "mevblocker"

USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
DEPLOY_BLOCK = {"USDC": 6082465, "USDT": 4634748}

TOPICS = {
    "USDC": {"Blacklisted": keccak.topic("Blacklisted(address)"),
             "UnBlacklisted": keccak.topic("UnBlacklisted(address)")},
    "USDT": {"AddedBlackList": keccak.topic("AddedBlackList(address)"),
             "RemovedBlackList": keccak.topic("RemovedBlackList(address)")},
}
ADDRESS = {"USDC": USDC, "USDT": USDT}
assert TOPICS["USDC"]["Blacklisted"] == "0xffa4e6181777692565cf28528fc88fd1516ea86b56da075235fa575af6a4b855"


def _decode_address(log):
    """Indexed (topics[1]) or unindexed (first data word) single-address event."""
    if len(log["topics"]) >= 2:
        return "0x" + log["topics"][1][-40:].lower()
    data = log["data"][2:]
    return "0x" + data[24:64].lower()


def _receipts_after(n):
    with open(_RECEIPTS_PATH) as f:
        return [json.loads(l) for l in f.readlines()[n:]]


def _count_lines():
    if not os.path.exists(_RECEIPTS_PATH):
        return 0
    with open(_RECEIPTS_PATH) as f:
        return sum(1 for _ in f)


# mevblocker rate-limits (HTTP 429) under a steady stream. No fallback to drpc (its 100-block cap would turn each
# 50k window into 500 calls and mix endpoints in the receipts): pace the windows, and on a failed window cool down
# and retry the SAME window (never shrink a span, never skip one).
rpc._OTHER_ENDPOINT[ENDPOINT] = None
PACE_S, COOLDOWN_S, MAX_COOLDOWNS = 1.5, 45, 20


def _get_logs_paced(start, to_block, symbol, topics):
    cap = rpc._endpoint_cfg(ENDPOINT)["block_range_cap"]
    logs = []
    for s, e in rpc._split_ranges(start, to_block, cap):
        for attempt in range(MAX_COOLDOWNS):
            try:
                logs.extend(rpc.get_logs(ENDPOINT, s, e, address=ADDRESS[symbol], topics=[list(topics.values())], gate=GATE))
                break
            except rpc.GapError as err:
                print(f"[g0_ext] {symbol} [{s},{e}] failed ({err}); cooldown {COOLDOWN_S}s", flush=True)
                time.sleep(COOLDOWN_S)
        else:
            raise rpc.GapError(f"{symbol} [{s},{e}] still failing after {MAX_COOLDOWNS} cooldowns")
        time.sleep(PACE_S)
    return logs


def fetch(symbol, to_block):
    start = DEPLOY_BLOCK[symbol]
    topics = TOPICS[symbol]
    n0 = _count_lines()
    logs = _get_logs_paced(start, to_block, symbol, topics)
    recs = _receipts_after(n0)
    tmp = _RECEIPTS_PATH + ".component_check"
    with open(tmp, "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    try:
        rpc.assert_tiling(tmp, start, to_block)
    finally:
        os.remove(tmp)
    assert all(r["endpoint"] == ENDPOINT for r in recs), "a range fell back to another endpoint"
    names = {v.lower(): k for k, v in topics.items()}
    rows = []
    for l in logs:
        assert l["address"].lower() == ADDRESS[symbol].lower()
        rows.append({"coin": symbol, "address": _decode_address(l), "event": names[l["topics"][0].lower()],
                     "block": int(l["blockNumber"], 16), "tx_hash": l["transactionHash"].lower(),
                     "log_index": int(l["logIndex"], 16)})
    return rows, len(recs)


def run(cfg=None):
    rpc.assert_chain_id(ENDPOINT)
    to_block = blocks.load_pinned()["census_start"]["block"] - 1
    os.makedirs(os.path.dirname(_RECEIPTS_PATH), exist_ok=True)
    rows, summary = [], []
    for symbol in ("USDC", "USDT"):
        r, n_ranges = fetch(symbol, to_block)
        rows.extend(r)
        counts = {e: sum(1 for x in r if x["event"] == e) for e in TOPICS[symbol]}
        summary.append(f"{symbol} [{DEPLOY_BLOCK[symbol]}, {to_block}] {n_ranges} ranges: {counts}")
        print("[g0_ext]", summary[-1])
    rows.sort(key=lambda x: (x["coin"], x["block"], x["log_index"]))
    os.makedirs(_G0_DIR, exist_ok=True)
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["coin", "address", "event", "block", "tx_hash", "log_index"])
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(os.path.dirname(_RECEIPTS_PATH), "summary.md"), "w") as f:
        f.write("# g0 blacklist-history extension (deployment -> census_start - 1)\n\n"
                f"Endpoint {ENDPOINT}; output out/g0/blacklist_events_predeploy_ext.csv ({len(rows)} rows).\n\n"
                + "\n".join(f"- {s}" for s in summary) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(run())
