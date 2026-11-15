# Gate 3 -- pilot MDE on the wallet-level Delta

Inputs: `out/g1/cohort.csv` (sha256 `e99563611925f094de1bf1ffed66bc905fa6918ce63fd7a2ce5b5c9bfdab6633`), `out/g1/wallet_coin_day_pre.csv` (sha256 `a1f37162c4e404e559338c0bdcc3761ae3c69bb0d75bd7a7ee9e2963bccf16e5`). No RPC calls; no independent balance reconstruction -- every balance is S2's own.

Sample: sampled 500 of 4508 floor-passing wallets (seed 20260905). Winsorization caps (pooled, 1/99 pct of the scaled outcome y): [0.000000, 4.285714]. `d_it = y_iUSDC,t - y_iUSDT,t` (winsorized). Placebo Delta_i = mean(d, day_index 30..59) - mean(d, day_index 0..29) (T=60 pre-period days, split in half -- no post-period data exists yet).

## MDE table (both computation paths; SD(Delta) and wallet count per cell)

$0/$100 floor tiers coincide with pooled because the cohort floor rule already requires pre-period mean total holding >= $100.

| group | n wallets | SD(Delta) direct-agg | SD(Delta) OLS-slope | MDE@500 | MDE@1,500 | MDE@2,000 | MDE@3,300 |
|---|---|---|---|---|---|---|---|
| pooled | 500 | 0.9742 | 0.9742 | 0.1725 | 0.0996 | 0.0863 | 0.0672 |
| arm=withdrawal | 201 | 1.0324 | 1.0324 | 0.1828 | 0.1056 | 0.0914 | 0.0712 |
| arm=deposit | 214 | 0.8587 | 0.8587 | 0.1521 | 0.0878 | 0.0760 | 0.0592 |
| arm=both | 85 | 1.0770 | 1.0770 | 0.1907 | 0.1101 | 0.0954 | 0.0742 |
| floor>=$0 | 500 | 0.9742 | 0.9742 | 0.1725 | 0.0996 | 0.0863 | 0.0672 |
| floor>=$100 | 500 | 0.9742 | 0.9742 | 0.1725 | 0.0996 | 0.0863 | 0.0672 |
| floor>=$1,000 | 402 | 0.9278 | 0.9278 | 0.1643 | 0.0949 | 0.0821 | 0.0640 |
| floor>=$10,000 | 244 | 0.8835 | 0.8835 | 0.1564 | 0.0903 | 0.0782 | 0.0609 |
| dual-holder (holding_type=both) | 129 | 0.8115 | 0.8115 | 0.1437 | 0.0830 | 0.0719 | 0.0559 |
| dollar-weighted (pooled, unwinsorized) | 500 | 3.4704 | 3.4704 | 0.6146 | 0.3548 | 0.3073 | 0.2392 |

(MDE columns above are the direct-aggregation path; the OLS-slope path's MDE cells, computed from the same SD(Delta) OLS-slope column, are identical within the agreement tolerance below.)

## ICC and effective observations

| metric | value |
|---|---|
| ICC(d), pooled sample (500 wallets, T=60) | 0.3907 |
| MSB | 32.479378 |
| MSW | 0.822754 |
| effective obs per wallet @ T=60 | 2.495 |

## N per arm needed for target MDE (pooled SD(Delta), direct-agg path)

| target MDE | N per arm needed |
|---|---|
| 10 pp | 1489 |
| 5 pp | 5953 |

## Comparison to IDENT's pilot

IDENT pilot: SD(Delta)=1.44, ICC=0.23, MDE=13 pp at N=2000/arm. This pilot (pooled, floor-passing sample): SD(Delta)=0.9742, ICC=0.3907, MDE=8.63 pp at N=2000/arm.

## Acceptance checks

| check | result | detail |
|---|---|---|
| cohort.csv sha256 matches out/g1/cohort.schema.json | PASS | actual e99563611925f094de1bf1ffed66bc905fa6918ce63fd7a2ce5b5c9bfdab6633 expected e99563611925f094de1bf1ffed66bc905fa6918ce63fd7a2ce5b5c9bfdab6633 |
| cohort.csv primary key (wallet_id) strictly increasing / no duplicates | PASS | ok |
| wallet_coin_day_pre.csv sha256 matches out/g1/wallet_coin_day_pre.schema.json | PASS | actual a1f37162c4e404e559338c0bdcc3761ae3c69bb0d75bd7a7ee9e2963bccf16e5 expected a1f37162c4e404e559338c0bdcc3761ae3c69bb0d75bd7a7ee9e2963bccf16e5 |
| wallet_coin_day_pre.csv primary key (wallet_id,coin,date) strictly increasing / no duplicates | PASS | ok |
| every sampled wallet has a dense 60-day panel for both coins | PASS | ok |
| direct aggregation and OLS-slope Delta paths agree (tolerance 1e-09) | PASS | max abs per-wallet difference 3.553e-15 |

6/6 checks passed.

**Gate 3 result: PASS.**
