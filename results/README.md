# results/ — aggregates shipped with v1.0

Aggregate tables only (no wallet addresses, no per-wallet rows; checked for any 0x-prefixed 40-hex string). They are the
inputs of `paper/exhibits.R` (`Rscript paper/exhibits.R --out <dir> [--in results]`), copied byte-for-byte from the private
working copy's untracked `out/` tree. Hashes below are of the shipped files. The signed-hash check (`tools/verify_release.py`)
does not cover these files: they were produced after signing and are not among the hashed bytes.

| shipped file | source in the private working copy | sha256 | data rows |
|---|---|---|---|
| results/post/post_estimates.csv | out/recovery/post/r-gen-M-post/post_estimates.csv | 6c2cab850d6a05006bb272eface022e892123d71f6c878f7d04cfe6416db8d08 | 16 |
| results/post/post_decisions.csv | out/recovery/post/r-gen-M-post/post_decisions.csv | 36c857bf631a00c771e437cc339718a761efdf83713dd169a4b714b5932a6504 | 15 |
| results/post/post_rematches.csv | out/recovery/post/r-gen-M-post/post_rematches.csv | eba6eb10353f914f96017594290dfbf603c02c0b690dad100a00da10464b5748 | 5 |
| results/post/post_cap_robustness.csv | out/recovery/post/r-gen-M-post/post_cap_robustness.csv | ab4d125e2300b6adf29eb7a79c7729af6a42495759e4fc53af92b0c28dc97cfb | 8 |
| results/post/post_short_window.csv | out/recovery/post/r-gen-M-post/post_short_window.csv | daaae1320ee7a8c5adc98f5aeb94801dc0a795a5200de9bc143746ad6af8356a | 8 |
| results/post/post_drdid.csv | out/recovery/post/r-gen-M-post/post_drdid.csv | c3179b2ba12f168d174c24478b3357f68d1e2e5895a4f2b064233d64068fb603 | 2 |
| results/post/post_pretrend.csv | out/recovery/post/r-gen-M-post/post_pretrend.csv | 4c03ebb2f0e09fe83b0f6a65f09cbc1ae6d74d82cc888f7b01a24831a844b4a3 | 120 |
| results/post/post_time_path.csv | out/recovery/post/r-gen-M-post/post_time_path.csv | 5954a5832fd8f0ea7edad8fcf2c85b4f22d6d7270612138422fa50f45b1f320c | 72 |
| results/post/post_cluster_sizes.csv | out/recovery/post/r-gen-M-post/post_cluster_sizes.csv | bd1a0e58d669e0b1f5f173bd715e00555fa7af1e477e2dac4d9a7dd0538621b4 | 2 |
| results/post/post_weight_concentration.csv | out/recovery/post/r-gen-M-post/post_weight_concentration.csv | 9a4173274280f90aae59084f02b46f15daa07de78989a7281e318d11119a65b6 | 3 |
| results/post/post_h2.csv | out/recovery/post/r-gen-M-post/post_h2.csv | 2d8b7c31d7a75fcfed3fc63a47370c551d23889a6372f05a7e80041770a39fd2 | 6 |
| results/post/post_reporting_rows.csv | out/recovery/post/r-gen-M-post/post_reporting_rows.csv | 93275a617fb35504526052e374c6dddc383aafe05243e963cd1d81040cbfddf7 | 25 |
| results/o3/o3_numbers.csv | out/exploratory/o3-2026-10-28/o3_numbers.csv (exploratory, post-results, 28 Oct 2026) | a21401fd6d17f2b857a5825c1064f2cdf70b4a083e54c9b1c53a84cce5238777 | 186 |
| results/placebo/placebo_estimates_june_pre.csv | out/recovery/june_pre/gen-20261027-junef-pre/placebo_estimates.csv (June placebo, fake event 9 Jul; capped D = row usdc_mode=three_bins, cap=own) | 0ad075a52000e1d4d5984b8ff04a4f89d773159c7a67d40f9acb697f6f5c50da | 9 |
