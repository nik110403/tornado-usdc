# Gate 1b -- control sampling frame probe

Exclusion sweep: 9000000 -> 15302549 (Withdrawal+EncryptedNote, topic-only, tenderly).
Withdrawal logs: 161570. EncryptedNote logs: 138947.
Exclusion set: 88130 addresses (61843 withdrawal recipients, 34250 EncryptedNote depositors).

Sample: 30 windows x 400 blocks, seed 20220609, drawn strictly inside the pre-period [14929560, 15298282] on tenderly.
Unique candidate addresses (from union to, all windows): 185044.
Dropped -- in exclusion set: 1211. Dropped -- has code at 15302549: 11537.
Survivors (control-frame population): 172296.

## Per-window detail

| window | block range | unique addrs | survivor addrs |
|---|---|---|---|
| 0 | 14962951-14963350 | 9485 | 8307 |
| 1 | 14964251-14964650 | 7450 | 6503 |
| 2 | 14964469-14964868 | 7099 | 6323 |
| 3 | 14974116-14974515 | 9992 | 8948 |
| 4 | 14980155-14980554 | 9671 | 8097 |
| 5 | 15006732-15007131 | 9541 | 8504 |
| 6 | 15012236-15012635 | 8890 | 7937 |
| 7 | 15024849-15025248 | 6380 | 5713 |
| 8 | 15052140-15052539 | 7925 | 7064 |
| 9 | 15057555-15057954 | 8858 | 8005 |
| 10 | 15059021-15059420 | 7653 | 6855 |
| 11 | 15076736-15077135 | 10992 | 10059 |
| 12 | 15102746-15103145 | 8134 | 7252 |
| 13 | 15117089-15117488 | 4919 | 4214 |
| 14 | 15128165-15128564 | 8270 | 7367 |
| 15 | 15132015-15132414 | 5740 | 5120 |
| 16 | 15137500-15137899 | 5689 | 4887 |
| 17 | 15172449-15172848 | 8144 | 7338 |
| 18 | 15179441-15179840 | 9414 | 8463 |
| 19 | 15184516-15184915 | 7602 | 6629 |
| 20 | 15187941-15188340 | 6963 | 6177 |
| 21 | 15208451-15208850 | 5787 | 4870 |
| 22 | 15222077-15222476 | 11662 | 10989 |
| 23 | 15233385-15233784 | 7542 | 6559 |
| 24 | 15237101-15237500 | 9909 | 8793 |
| 25 | 15244957-15245356 | 7020 | 6202 |
| 26 | 15245159-15245558 | 6817 | 5940 |
| 27 | 15262610-15263009 | 13975 | 13057 |
| 28 | 15280371-15280770 | 6238 | 5484 |
| 29 | 15286893-15287292 | 5784 | 5138 |

## Recurrence across windows (survivors only)

Mean windows seen: 1.26. Max: 30. Seen in >= 2 windows: 23775.

| windows seen | # survivors |
|---|---|
| 1 | 148521 |
| 2 | 17004 |
| 3 | 3540 |
| 4 | 1309 |
| 5 | 615 |
| 6 | 336 |
| 7 | 200 |
| 8 | 144 |
| 9 | 86 |
| 10 | 63 |
| 11 | 61 |
| 12 | 36 |
| 13 | 39 |
| 14 | 25 |
| 15 | 22 |
| 16 | 22 |
| 17 | 19 |
| 18 | 20 |
| 19 | 16 |
| 20 | 16 |
| 21 | 17 |
| 22 | 20 |
| 23 | 10 |
| 24 | 7 |
| 25 | 17 |
| 26 | 4 |
| 27 | 20 |
| 28 | 19 |
| 29 | 23 |
| 30 | 65 |

## Implied frame size (capture-recapture, Chapman's estimator, all 435 window pairs)

Median: 139067. Mean: 149511.

**implied frame size 139067 is >= 5x the S2 floor-passing exposed count (4508 -> threshold 22540)**

## Weight distribution (implied inverse-probability weight)

p10=0.3465 p50=1.0396 p90=1.0396 max=1.0396

Top-1% of survivors by legs hold 39.62% of total legs (1723 addresses / 456098 total legs).

## Acceptance checks

| check | result | detail |
|---|---|---|
| exclusion set size reported | PASS | 88130 addresses |
| no window straddles the pre-period bounds | PASS | ok |
| receipts tile each window | PASS | ok (structurally enforced -- a tiling failure above would have raised) |
| summary states implied frame size vs 5x S2 floor-passing count | PASS | implied frame size 139067 is >= 5x the S2 floor-passing exposed count (4508 -> threshold 22540) |

4/4 checks passed.

Cross-endpoint reverification: 5 ranges re-fetched and matched.

## Outputs

- `exclusion.csv` (88130 rows, sha256 `c5a0559d72790d53747d131e794e56c889ccaa5bc8be0154530beb7ef1c91468`)
- `frame_survivors.csv` (172296 rows, sha256 `c2c3be88a33a54ac260291ba4fcc26a1b8eb68bc3c2e61b78d68ac8dd0e0feb6`)

Exclusion path: `out/g1b/exclusion.csv`
Survivors path: `out/g1b/frame_survivors.csv`
