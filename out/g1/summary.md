# Gate 1 -- exposed cohort by arm x holding type, floor, kill rule

Withdrawal arm: 151468 logs, 60707 unique recipients.
Deposit arm: 163870 designated-pool Deposit logs, 138947 EncryptedNote logs (all instances), 138946 matched (84.8%), 24924 unmatched residual.
Deduped cohort: 87051; excluded 331 contract addresses; 86720 EOA cohort members.
Participating (>=1 pre-period Transfer): 8384. USDT DestroyedBlackFunds applied: 0.
In-panel: 7080. Floor-passing (pooled): 4508 -- TARGET.
Cross-endpoint reverification: 327 ranges re-fetched and matched.

## Acceptance checks

| check | result | detail |
|---|---|---|
| withdrawal-arm unique recipients >= 60,629 | PASS | got 60707 |
| deposit arm resolves >= 70% of designated-pool deposits via EncryptedNote | PASS | got 0.8479 (138946/163870) |
| day-end reconstructed balance == balanceOf @ pre_period_end, random 50, tolerance 0 | PASS | ok (50 pairs) |
| every *_units column matches ^-?[0-9]+$ | PASS | ok |
| schema.json sidecars present | PASS | ok |

5/5 checks passed.

## Outputs

- `cohort.csv` (86720 rows, sha256 `e99563611925f094de1bf1ffed66bc905fa6918ce63fd7a2ce5b5c9bfdab6633`)
- `wallet_coin_day_pre.csv` (1006080 rows, sha256 `a1f37162c4e404e559338c0bdcc3761ae3c69bb0d75bd7a7ee9e2963bccf16e5`)
- `deposit_residual.csv` (24924 rows, sha256 `89a155647b5bcfdef83dcf45f0a79ca878d17511450aaf7494a9f3dedf8f95f4`)
- `crosstab.md`
