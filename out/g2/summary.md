# Gate 2 -- reconstruction integrity

## (i) Coin-level supply invariant

| coin | window | blocks | delta_supply | net_raw | raw_match | net_corrected | corrected_match |
|---|---|---|---|---|---|---|---|
| USDC | random-1 | [15237517,15238516] | -33545201430000 | -33545201430000 | PASS | -33545201430000 | PASS |
| USDC | random-2 | [15071222,15072221] | 0 | 0 | PASS | 0 | PASS |
| USDC | random-3 | [14973428,14974427] | 112639854840000 | 112639854840000 | PASS | 112639854840000 | PASS |
| USDC | random-4 | [15233494,15234493] | -6716779430000 | -6716779430000 | PASS | -6716779430000 | PASS |
| USDC | random-5 | [15198608,15199607] | 63863806330000 | 63863806330000 | PASS | 63863806330000 | PASS |
| USDC | fixed-canary-window | [15365400,15365600] | 2853167420000 | 2853167420000 | PASS | 2853167420000 | PASS |
| USDT | random-1 | [15237517,15238516] | 0 | 0 | PASS | 0 | PASS |
| USDT | random-2 | [15071222,15072221] | 0 | 0 | PASS | 0 | PASS |
| USDT | random-3 | [14973428,14974427] | 0 | 0 | PASS | 0 | PASS |
| USDT | random-4 | [15233494,15234493] | 0 | 0 | PASS | 0 | PASS |
| USDT | random-5 | [15198608,15199607] | 0 | 0 | PASS | 0 | PASS |
| USDT | fixed-canary-window | [15365400,15365600] | -1199336943238 | 0 | FAIL | -1199336943238 | PASS |

## (ii) Freeze intersection

42 freeze events touch 25 distinct cohort addresses (Circle lifetime Blacklisted: 278 events; USDT AddedBlackList + DestroyedBlackFunds: 3054 events) -- flagged in `frozen_in_cohort.csv`, not dropped from the cohort.

## (iii) Canary + stratified reconciliation

Stratified: 30/30 (wallet, coin, block) reconciliations matched, tolerance 0.
Canary 0x5cb182a51c452937abecc9e81a9663efbeb425f5 @ block 15365491: naive=1199336943238 (expected 1199336943238), actual balanceOf=0, corrected=0 -- naive FAILS vs actual, corrected PASSES.

Cross-endpoint reverification: 5 ranges re-fetched and matched.

## Acceptance checks

| check | result | detail |
|---|---|---|
| USDC supply invariant matches in all 6 windows | PASS | 6/6 |
| USDT supply invariant matches (after Issue/Redeem/DestroyedBlackFunds correction) in all 6 windows | PASS | 6/6 |
| USDT raw (Transfer-only) invariant fails in >=1 window before correction | PASS | 1/6 raw windows failed |
| stratified reconciliation 30/30 reconciled, tolerance 0 | PASS | 30/30 matched |
| canary: naive Transfer-only reconstruction fails vs balanceOf, corrected reconstruction passes | PASS | naive=1199336943238 (expected 1199336943238) actual=0 corrected=0 |
| OR-array request (10 addresses) within tenderly's cap | PASS | 10 <= 1000 |
| every *_units column matches ^-?[0-9]+$ | PASS | ok (no *_units column in frozen_in_cohort.csv) |
| schema.json sidecar present | PASS | ok |

8/8 checks passed.

## Outputs

- `frozen_in_cohort.csv` (42 rows, sha256 `3a7cb7cfdb79565d8cb204f66fdb1c03e607a2dc0af9b4f4e9c803dd3e550c61`)

**Gate 2 result: PASS.**
