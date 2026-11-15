# Gate 1 -- exposed cohort crosstab

Cohort (post-exclusion): 86571. Tracked wallets (rule baseline_transfer_required): 8370.
In-panel (nonzero balance >=1 pre-period day): 7066. Floor-passing: 4502.

### arm x holding_type -- all in-panel

| arm | USDC-only | USDT-only | both | none | total |
|---|---|---|---|---|---|
| withdrawal | 1865 | 721 | 414 | 0 | 3000 |
| deposit | 1838 | 470 | 591 | 0 | 2899 |
| both | 720 | 170 | 277 | 0 | 1167 |
| **total** | 4423 | 1361 | 1282 | 0 | **7066** |

### arm x holding_type -- floor-passing

| arm | USDC-only | USDT-only | both | none | total |
|---|---|---|---|---|---|
| withdrawal | 1122 | 358 | 346 | 0 | 1826 |
| deposit | 1190 | 192 | 525 | 0 | 1907 |
| both | 453 | 71 | 245 | 0 | 769 |
| **total** | 2765 | 621 | 1116 | 0 | **4502** |

### both-coin cell, daily resolution

Wallet-days (per participating wallet, per day) where both USDC and USDT were simultaneously positive, summed across the pre-period, for holding_type=both wallets, by arm:

| arm | both-coin wallet-days |
|---|---|
| withdrawal | 13514 |
| deposit | 20093 |
| both | 8080 |

**Kill rule: floor-passing in-panel exposed wallets (both arms pooled) = 4502 -- KILL if < 1500, PASS if >= 1500, TARGET if >= 3300. Result: TARGET.**
