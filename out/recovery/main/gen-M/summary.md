# Gate 4 -- control baseline panel + matching covariates (both arms)

Universe: 176798 wallets = 172296 control (g1b frame survivors, minus 0 that are exposed) + 4502 exposed floor-passing (g1 cohort.csv floor_pass).
Baseline: 14929561..15298282 (60 days); opening balanceOf @ 14929560; Transfer walk endpoint mix (ranges): mevblocker 26234 (this invocation's walk endpoint=mevblocker, replay endpoint=tenderly); USDT DestroyedBlackFunds events applied: 1.
**Floor-passing control wallets: 71784 of 172296.** Floor-passing exposed re-derived: 4502 of 4502 (g1 reported 4502; 0 differ on pre_mean_total_units vs g1 cohort.csv -- informational).
Floor: mean total >= 100000000 base units ($100) and >= 14 nonzero days. Baseline blacklist membership is flagged in covariates.csv, not dropped.
Blacklist source: baseline manifest /home/nik/Projects/analysis/tornado-usdc/out/recovery/blacklist_baseline/gen-20261023-s26 (via cfg, sha256 d20fd0c53eeb947828091cef97e9bb869d970ccb2291b0904f5561de0382d6fd).
Wallet age (proxy: first outgoing tx block): 72624 have one, 3662 never sent by pre_period_end.
Issuer mint/redeem flag (zero address only): 2 wallets.

## NEEDS_CONTEXT

- `exchange_share`: column left EMPTY. No verified, citable CEX label list was supplied; none is invented.
- Issuer treasury addresses (item 28b): not present in the g0 outputs; flag covers the zero address only.

## Acceptance checks

| check | result | detail |
|---|---|---|
| day-end reconstructed balance == balanceOf @ pre_period_end, 10 control wallets x 2 coins, tolerance 0 | PASS | ok (10 wallets, 20 pairs) |
| balance identity: opening balanceOf @ 14929560 + sum of walked daily deltas == balanceOf @ pre_period_end, 76286 of 76286 floor-passers x 2 coins, tolerance 0 | PASS | 0 mismatches (76286 wallets, 152572 pairs) |
| no checkpointed walk range returned >= its endpoint result cap (possible truncation) | PASS | 0 ranges at/over cap |
| walk units re-verified on a second endpoint, stratified by walk endpoint (count + sha256); cached entries must be bound to digest/count/endpoints/version | PASS | 1332 ranges matched: 0 from bound cache, 1332 freshly compared; units per endpoint {'mevblocker': 36} |
| no receipt beyond pre_period_end (15298282) | PASS | 37130 receipts, max to_block <= 15298282 |
| every *_units column matches ^-?[0-9]+$ | PASS | ok |
| schema.json sidecars present | PASS | ok |

7/7 checks passed.

Generation: `gen-20261026T235715Z-8fd41fcc` (all outputs below come from this one run; `out/g4/current` -> this directory).

Wall clock this invocation: 19821s (opening balances 800s; transfer walk 13816s; reconstruct 4s; eth balance 82s; wallet age 4253s; reconciliation 2s; balance-identity audit 326s; reverify 477s). Resumable: re-run the same command.

## Outputs

- `covariates.csv` (76286 rows, sha256 `a21a1524724066456a867304d9b872976b7ec2766d6347f533365b368fd4b4c0`) -- address-level, confidential, untracked
- `wallet_coin_day_pre.csv` (9154320 rows, sha256 `c2ea9ce316b8592346124b5119f9a0f8cdea228ad4baeff8bc72d66e8d831257`) -- address-level, confidential, untracked
