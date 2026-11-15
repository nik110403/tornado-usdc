# Gate 1 -- exposed cohort by arm x holding type, floor, kill rule

Withdrawal arm: 151180 logs, 60591 unique recipients.
Deposit arm: 163696 designated-pool Deposit logs, 138773 EncryptedNote logs (all instances), 138772 matched (84.8%), 24924 unmatched residual.
Eligibility cutoff: pre_period_end = 15298282 (exposure logs + eth_getCode); event_block 15302549 used for neither.
Participation rule (unresolved, recorded): `baseline_transfer_required`. Wallets with a baseline Transfer: 8370; tracked: 8370.
Deduped cohort: 86901; excluded 330 contract addresses; 86571 EOA cohort members.
Tracked wallets (rule baseline_transfer_required): 8370. USDT DestroyedBlackFunds applied: 0.
In-panel: 7066. Floor-passing (pooled): 4502 -- TARGET.
Cross-endpoint reverification: 328 ranges re-fetched and matched.

## Acceptance checks

| check | result | detail |
|---|---|---|
| withdrawal-arm unique recipients >= 60,629 | FAIL | got 60591 |
| deposit arm resolves >= 70% of designated-pool deposits via EncryptedNote | PASS | got 0.8477 (138772/163696) |
| day-end reconstructed balance == balanceOf @ pre_period_end, random 50, tolerance 0 | PASS | ok (50 pairs) |
| every *_units column matches ^-?[0-9]+$ | PASS | ok |
| schema.json sidecars present | PASS | ok |

4/5 checks passed.

## Outputs

- `cohort.csv` (86571 rows, sha256 `63e00508adc5fad8281d4139cac036193661b71acc0aa96b1c8b63bcc7425efb`)
- `wallet_coin_day_pre.csv` (1004400 rows, sha256 `c0b9e2d5e6b046d231b351ff658a6b736f77006bb43f9da8b9945aafea10a425`)
- `deposit_residual.csv` (24924 rows, sha256 `89a155647b5bcfdef83dcf45f0a79ca878d17511450aaf7494a9f3dedf8f95f4`)
- `eligibility_screen.csv` (86901 rows, sha256 `5ad6be3fee20cb6630c70e240649975752aeb00acbbe6e6a9efdc1f662a2125f`)
- `crosstab.md`
