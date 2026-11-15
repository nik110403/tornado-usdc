# Gate 4 -- control baseline panel + matching covariates (both arms)

Universe: 176804 wallets = 172296 control (g1b frame survivors, minus 0 that are exposed) + 4508 exposed floor-passing (g1 cohort.csv floor_pass).
Baseline: 14929561..15298282 (60 days); opening balanceOf @ 14929560; Transfer walk endpoint mix (ranges): mevblocker 26234 (this invocation's G4_WALK_ENDPOINT=mevblocker); USDT DestroyedBlackFunds events applied: 1.
**Floor-passing control wallets: 71784 of 172296.** Floor-passing exposed re-derived: 4508 of 4508 (g1 reported 4508; 0 differ on pre_mean_total_units vs g1 cohort.csv -- informational).
Floor: mean total >= 100000000 base units ($100) and >= 14 nonzero days. Baseline blacklist membership is flagged in covariates.csv, not dropped.
Wallet age (proxy: first outgoing tx block): 72630 have one, 3662 never sent by pre_period_end.
Issuer mint/redeem flag (zero address only): 2 wallets.

## NEEDS_CONTEXT

- `exchange_share`: column left EMPTY. No verified, citable CEX label list was supplied; none is invented.
- Issuer treasury addresses (item 28b): not present in the g0 outputs; flag covers the zero address only.

## Acceptance checks

| check | result | detail |
|---|---|---|
| day-end reconstructed balance == balanceOf @ pre_period_end, 10 control wallets x 2 coins, tolerance 0 | PASS | ok (10 wallets, 20 pairs) |
| balance identity: opening balanceOf @ 14929560 + sum of walked daily deltas == balanceOf @ pre_period_end, 76292 of 76292 floor-passers x 2 coins, tolerance 0 | PASS | 0 mismatches (76292 wallets, 152584 pairs) |
| no checkpointed walk range returned >= its endpoint result cap (possible truncation) | PASS | 0 ranges at/over cap |
| walk units re-verified on a second endpoint, stratified by walk endpoint (count + sha256); cached entries must be bound to digest/count/endpoints/version | PASS | 1332 ranges matched: 0 from bound cache, 1332 freshly compared; units per endpoint {'mevblocker': 36} |
| no receipt beyond pre_period_end (15298282) | PASS | 63460 receipts, max to_block <= 15298282 |
| every *_units column matches ^-?[0-9]+$ | PASS | ok |
| schema.json sidecars present | PASS | ok |

7/7 checks passed.

Generation: `gen-20261015T213626Z-cbcb68c5` (all outputs below come from this one run; `out/g4/current` -> this directory).

Wall clock this invocation: 19710s (opening balances 791s; transfer walk 13762s; reconstruct 4s; eth balance 71s; wallet age 4251s; reconciliation 1s; balance-identity audit 329s; reverify 431s). Resumable: re-run the same command.

## Outputs

- `covariates.csv` (76292 rows, sha256 `f80c05e60501f18919b2670530511c9b70a5740a0fb2b3f413adf3f8c28f4eb3`) -- address-level, confidential, untracked
- `wallet_coin_day_pre.csv` (9155040 rows, sha256 `e0e3584e6b113a602c6636390a14ad7b0bc3a585b45ed4d9dd958d9f2442f407`) -- address-level, confidential, untracked
