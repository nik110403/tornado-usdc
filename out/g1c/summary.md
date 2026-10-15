# Gate 1c -- control-side balance funnel probe (Q5)

Sample: 2000 addresses drawn seed=20220609 from gate 1b's 172296 control frame survivors (`out/g1b/frame_survivors.csv`).
balanceOf: USDC + USDT @ pre_period_start (14929560) and pre_period_end (15298282), batched eth_call on mevblocker.

PROXY notice: gate 1's floor is pre-period mean total holding >= $100 **and** >= 14 pre-period days with nonzero balance, computed on a 60-day daily panel. This probe has only two balanceOf snapshots (start, end) -- it cannot compute a day count, so "$100-proxy pass" below means mean(total_start, total_end) >= 100,000,000 base units ($100-equivalent) only. It is NOT gate 1's floor and is not comparable to it in isolation -- only the dual-holding rate comparison below is.

## Full sample (n=2000)

Positive stablecoin balance at either block: 1116 (55.8%).
Mean-of-two-blocks >= $100-equivalent proxy: 724 (36.2%).

Coin split @ pre_period_start: USDC-only 209 (10.4%), USDT-only 440 (22.0%), both 68 (3.4%), none 1283 (64.2%) -- any-positive 717 (35.9%)
Coin split @ pre_period_end:   USDC-only 230 (11.5%), USDT-only 434 (21.7%), both 91 (4.5%), none 1245 (62.2%) -- any-positive 755 (37.8%)
Coin split @ either block:     USDC-only 326 (16.3%), USDT-only 668 (33.4%), both 122 (6.1%), none 884 (44.2%) -- any-positive 1116 (55.8%)

Dual-holding rate (both coins positive at the same block, either block, / full sample n): 122/2000 = 6.1%.

## $100-proxy subset (n=724)

Positive stablecoin balance at either block: 724 (100.0%).
Mean-of-two-blocks >= $100-equivalent proxy: 724 (100.0%).

Coin split @ pre_period_start: USDC-only 149 (20.6%), USDT-only 298 (41.2%), both 61 (8.4%), none 216 (29.8%) -- any-positive 508 (70.2%)
Coin split @ pre_period_end:   USDC-only 158 (21.8%), USDT-only 266 (36.7%), both 79 (10.9%), none 221 (30.5%) -- any-positive 503 (69.5%)
Coin split @ either block:     USDC-only 218 (30.1%), USDT-only 402 (55.5%), both 104 (14.4%), none 0 (0.0%) -- any-positive 724 (100.0%)

Dual-holding rate (both coins positive at the same block, either block, / $100-proxy subset n): 104/724 = 14.4%.

## Comparison to gate 1's floor-passing exposed cohort

**Gate 1 floor-passing exposed cohort: 4508 wallets, 1117 dual holders (24.8%, out/g1/crosstab.md). Control-side $100-proxy subset (724 wallets): 104 dual holders (14.4%).**

## Acceptance checks

| check | result | detail |
|---|---|---|
| sample size == 2,000 | PASS | 2000 |
| 4x2,000 balance calls all returned (no None) | PASS | 8000 values across 4 series |
| receipts present | PASS | 32 receipts |
| at least one positive balance observed | PASS | True |

4/4 checks passed.

## Outputs

- `funnel_sample.csv` (2000 rows, sha256 `138e4d149fd1fe79b510961dfd3a7a9ce75734a5f7cf71e9aa7e1b4a61987683`)
- `receipts.jsonl` (32 receipts)
