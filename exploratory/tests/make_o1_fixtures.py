"""Deterministic generator of the SYNTHETIC O1 fixtures (no real wallet, no real balance, no RPC).
Run: python3 exploratory/tests/make_o1_fixtures.py   -> exploratory/tests/fixtures/*"""
import csv
import json
import os
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
PIN, BLOCKS_PER_DAY, DAY0 = 1000, 4, 1000 + 1 + 4 * 100 + 3   # 6-hour synthetic blocks; day -100 = blocks 1001..1004
W = {"aa01": ("exposed", "0x" + "a" * 38 + "01"), "aa02": ("exposed", "0x" + "a" * 38 + "02"),
     "bb01": ("control", "0x" + "b" * 38 + "01"), "bb02": ("control", "0x" + "b" * 38 + "02")}
EXT = "0x" + "e" * 38 + "01"
OPEN = {"USDC": {W["aa01"][1]: 100_000_000, W["aa02"][1]: 0, W["bb01"][1]: 50_000_000, W["bb02"][1]: 7_000_000},
        "USDT": {W["aa01"][1]: 0, W["aa02"][1]: 30_000_000, W["bb01"][1]: 20_000_000, W["bb02"][1]: 40_000_000}}
# (block, coin, from, to, value): both directions, a same-day pair, a tracked-to-tracked transfer, an exact-zero path
T = [(1003, "USDC", W["aa01"][1], EXT, 10_000_000), (1010, "USDC", EXT, W["aa02"][1], 25_000_000),
     (1010, "USDT", W["aa02"][1], W["aa01"][1], 5_000_000), (1050, "USDC", W["bb01"][1], EXT, 50_000_000),
     (1051, "USDC", EXT, W["bb01"][1], 1), (1200, "USDT", EXT, W["bb02"][1], 3_000_000),
     (1403, "USDC", EXT, W["aa01"][1], 123_456), (1404, "USDT", W["bb01"][1], EXT, 2_000_000)]
DESTROYED = [{"address": W["bb02"][1], "block": 1100, "amount": 5_000_000}]


def day_of_block(b):
    return -100 + (b - PIN - 1) // BLOCKS_PER_DAY


def main():
    os.makedirs(FIX, exist_ok=True)
    dest = [dict(d, date=(date(2022, 6, 9) + timedelta(days=day_of_block(d["block"]))).isoformat()) for d in DESTROYED]
    bal = {c: dict(OPEN[c]) for c in OPEN}
    for b, c, f, t, v in T:
        if f in bal[c]:
            bal[c][f] -= v
        if t in bal[c]:
            bal[c][t] += v
    for d in dest:
        bal["USDT"][d["address"]] -= d["amount"]
    with open(os.path.join(FIX, "o1_ledger.json"), "w") as f:
        json.dump({"pin": PIN, "day0": DAY0, "open": OPEN, "transfers": T, "destroyed": dest, "expected_day0": bal}, f, indent=1, sort_keys=True)
    with open(os.path.join(FIX, "o1_membership.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["address", "arm", "stratum", "matched", "weight"])
        for k, (arm, a) in sorted(W.items(), key=lambda kv: kv[1][1]):
            w.writerow([a, arm, 1, "TRUE", 1])
        w.writerow(["0x" + "d" * 40, "control", 1, "FALSE", 0])
    ids = {a: i + 1 for i, (arm, a) in enumerate(sorted(W.values(), key=lambda x: x[1]))}
    with open(os.path.join(FIX, "o1_signed_cov.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["wallet_id", "address"])
        for a, i in ids.items():
            w.writerow([i, a])
    with open(os.path.join(FIX, "o1_signed_panel.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["wallet_id", "coin", "day_index", "balance_units"])
        for a, i in ids.items():
            for c in ("USDC", "USDT"):
                w.writerow([i, c, 0, bal[c][a]]); w.writerow([i, c, 1, bal[c][a] + 999])   # day 1 row must be ignored


if __name__ == "__main__":
    main()
