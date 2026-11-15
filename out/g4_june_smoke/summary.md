# Gate 4 (June placebo) -- June-only population -- SMOKE RUN (2 windows, sampled exposed; NOT the placebo population)

**june_end = block 15053225 @ 2022-06-30T23:59:28Z** (last block <= 2022-06-30T23:59:59Z; next block 15053226 @ 2022-07-01T00:00:00Z); pre_period_end = 15298282.
Tornado sweep (census_start 9000000 -> june_end): 151429 Withdrawal logs (141331 at the 23 designated pools), 127626 EncryptedNote, 152549 pool Deposit logs; exposed candidates 80214; exclusion set 81289.
Frame: 2 of 30 drawn windows x 400 blocks, seed 20251015 (random.Random.sample over starts in [14929561, 15052826]); 13551 unique candidates; full draw in `window_draw.json`.
Universe: 12695 = 12296 control + 399 exposed EOAs (code screen at june_end).
Walk: opening balanceOf @ 14929560; Transfer walk 14929561..15298282; endpoint mix (ranges): tenderly 343 (this invocation's G4_WALK_ENDPOINT=tenderly); USDT DestroyedBlackFunds events applied: 0.
**June-floor-passing: 5781 (5754 control, 27 exposed of 399).** Floors over the 22 June days: mean total >= 100000000 base units ($100) and >= 6 nonzero days.
Wallet age (proxy: first outgoing tx block <= june_end): 5217 have one, 564 never sent.
Blacklist membership at any time from 9 Jun to june_end (added then removed counts) is flagged (columns `blacklisted_*_at_pre_end` keep g4's names), not dropped.

## NEEDS_CONTEXT

- `exchange_share`: column left EMPTY (no verified, citable CEX label list).
- Issuer treasury addresses: flag covers the zero address only.

## Frame windows

| window | blocks | Transfer logs | unique addrs |
|---|---|---|---|
| 0 | 14939843-14940242 | 12027 | 8967 |
| 1 | 14941549-14941948 | 6509 | 5284 |

## Acceptance checks

| check | result | detail |
|---|---|---|
| june_end 15053225 (2022-06-30T23:59:28Z) is the last block <= 2022-06-30T23:59:59Z, pinned, and < pre_period_end 15298282 | PASS | next block 15053226 @ 2022-07-01T00:00:00Z |
| no control is in the Tornado exclusion set (<= june_end), is a contract at june_end, or is the zero address | PASS | 12296 controls |
| day-end reconstructed balance == balanceOf @ pre_period_end, 10 control wallets x 2 coins, tolerance 0 | PASS | ok (10 wallets, 20 pairs) |
| balance identity: opening balanceOf @ 14929560 + sum of walked daily deltas == balanceOf @ pre_period_end, 5781 of 5781 floor-passers x 2 coins, tolerance 0 | PASS | 0 mismatches (5781 wallets, 11562 pairs) |
| no checkpointed walk range returned >= its endpoint result cap (possible truncation) | PASS | 0 ranges at/over cap |
| walk units re-verified on a second endpoint, stratified by walk endpoint (count + sha256); cached entries must be bound to digest/count/endpoints/version | PASS | 5 ranges matched: 5 from bound cache, 0 freshly compared; units per endpoint {'tenderly': 3} |
| no receipt beyond pre_period_end (15298282) | PASS | 11086 receipts, max to_block <= 15298282 |
| every *_units column matches ^-?[0-9]+$ | PASS | ok |
| schema.json sidecars present | PASS | ok |
| covariates.csv header == g4's column list | PASS | 19 columns |

10/10 checks passed.

Generation: `gen-20261016T152533Z-f40ae53d` (`out/g4_june_smoke/current` -> this directory).

Wall clock this invocation: 31s (opening balances 0s; transfer walk 0s; reconstruct 2s; eth balance 0s; wallet age 0s; reconciliation 0s; balance-identity audit 0s; reverify 0s). Resumable: re-run the same command.

## Outputs

- `covariates.csv` (5781 rows, sha256 `55e38719720ef54a2142401a461a7ca2929c72df3d3a0cefb924307014e9cd7e`) -- address-level, confidential, untracked
- `wallet_coin_day_pre.csv` (693720 rows, sha256 `efcaf32c7b6825e6a91d1dfb8881d087b798dec354e535b662926ca2b6f3e41f`) -- address-level, confidential, untracked
