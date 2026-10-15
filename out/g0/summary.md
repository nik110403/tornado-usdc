# Gate 0 -- pool manifest + event censuses

Census: 9000000 -> 15302549 (topic-only, tenderly).
Emitters: 97. Instances: 91. Other: 6.
Deposit logs: 174184. Withdrawal logs: 161570.
Designated-pool set (instance AND in_circle_38): 23 members; designated-pool deposits: 163870.
Circle current cluster (15302549-15302766): 38 distinct addresses.
Circle lifetime history (9000000-22170334): 278 Blacklisted, 101 UnBlacklisted.
USDT lifetime history (9000000-22170334): 3406 events (AddedBlackList=2161, RemovedBlackList=180, DestroyedBlackFunds=893, Issue=168, Redeem=4).
Known USDC pools' on-chain denomination: {'0x4736dcf1b7a3d580672cce6e7c65cd5cc9cfba9d': 1000, '0xd96f2b1c14db8458374d9aca76e26c3d18364307': 100}.
Cross-endpoint reverification: 5 ranges re-fetched and matched.

## Acceptance checks

| check | result | detail |
|---|---|---|
| census deposit count == nextIndex() exactly for every instance | PASS | ok |
| circle current cluster has 38 distinct addresses | PASS | got 38 |
| designated set >= 19 members | PASS | got 23 |
| DAI pool 0x07687e702b410fa43f4cb4af7fa097918ffd2730 in designated set | PASS | {'address': '0x07687e702b410fa43f4cb4af7fa097918ffd2730', 'class': 'instance', 'token': '0x6b175474e89094c44da98b954eedeac495271d0f', 'denomination': 10000, 'denomination_raw': 10000000000000000000000, 'decimals': 18, 'nextIndex': 2933, 'deposits': 2933, 'withdrawals': 2716, 'first_block': 12164454, 'last_block': 15302464, 'in_circle_38': True} |
| DAI pool 0x23773e65ed146a459791799d01336db287f25334 in designated set | PASS | {'address': '0x23773e65ed146a459791799d01336db287f25334', 'class': 'instance', 'token': '0x6b175474e89094c44da98b954eedeac495271d0f', 'denomination': 100000, 'denomination_raw': 100000000000000000000000, 'decimals': 18, 'nextIndex': 3326, 'deposits': 3326, 'withdrawals': 3010, 'first_block': 12162115, 'last_block': 15298607, 'in_circle_38': True} |
| 0x527653ea119f3e6a1f5bd18fbf4714081d7b31ce classed other | PASS | other (not a census emitter -- probed directly, see _classify_probe) |
| 0x58e8dcc13be9780fc42e8723d8ead4cf46943df2 classed other | PASS | other (not a census emitter -- probed directly, see _classify_probe) |
| both known USDC pools read on chain | PASS | {'0x4736dcf1b7a3d580672cce6e7c65cd5cc9cfba9d': 1000, '0xd96f2b1c14db8458374d9aca76e26c3d18364307': 100} |
| total Deposit logs ~= 174,184 (<=1% deviation) | PASS | got 174184 |
| designated-pool deposits ~= 163,544 (<=1% deviation) | PASS | got 163870 |
| circle lifetime Blacklisted == 278 | PASS | got 278 |
| circle lifetime UnBlacklisted == 101 | PASS | got 101 |
| 22 Mar 2025 cluster contains 37 of the 38 | PASS | got 37 of 90 cluster addresses |
| 0x905b63fff465b9ffbf41dea908ceb12478ec7601 UnBlacklisted at 15,941,739 | PASS | found |
| 62-address Blacklisted cluster at 15,941,674-15,941,903 | PASS | got 62 |
| USDT DestroyedBlackFunds for 0x5cb18...25f5 at 15,365,491 | PASS | found |

16/16 checks passed.

## Outputs

- `manifest.csv` (97 rows, sha256 `52c742a58e56a3fc8c5420516741d40c082fd589731ee0978d5138c747c0c8db`)
- `circle_events.csv` (379 rows, sha256 `6ece4e7c9685bdfa3ea936b2b4ed35645c02e942f9c2de7d3b3997add6205901`)
- `usdt_events.csv` (3406 rows, sha256 `c07fdfd98b666b77e64b4d3d76b04fb33c9a38bb29058e71c1e1a6f5b4836516`)

Manifest path: `out/g0/manifest.csv`
Circle events path: `out/g0/circle_events.csv`
USDT events path: `out/g0/usdt_events.csv`
