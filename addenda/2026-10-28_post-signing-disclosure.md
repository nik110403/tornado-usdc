# Post-signing disclosure addendum — dated 28 October 2026

**Status.** Written after the signature (pipeline commit `87c33de`, 2026-10-28 16:18:08 +0100) and after the post-event results (R post run, 2026-10-28 17:56, `out/recovery/post/r-gen-M-post/post_trace.json`, input trace `f83e6201…853e`). It amends nothing in `prespec/`; the signed files stay byte-identical (`prespec/SHA256`: prespec.md `e598ada7…07cc`, manifest.json `16eac963…6a`, g5_config.json `6341f75b…7e56`, code_manifest.txt `29cfe506…4c88`; signing.json `70a181fe…603e`). It records facts that §0 ("Prior access before signature") of the signed prespecification leaves out or states imprecisely. Source: pre-trend tribunal chg-cacc32a3 and its refuters (AIER `plans/tornado-usdc-pipeline/tribunal/2026-10-28-pretrend/`, findings R3, R4, R6).

## 1. Pre-signing placebo results not listed in §0

§0 lists the 15 October development placebo (three-bin matching), with its no-share row in a parenthesis (capped D β −0.0219, CI [−0.057, 0.013], case 3; the tracked `out/r_pre/summary.md` row gives 3,803 exposed / 10,064 controls / 263 strata), and the 23 October June placebo on the population before the participation rule (three-bin row). Three further fake-event (9 July) results **without USDC-share matching** (`usdc_mode = none`, capped D, `cap = own`) were on disk before the signature and are not listed:

| on disk since (file mtime) | file | exposed / controls / strata | capped D β | wallet 95% interval | case (wallet / stratum) | sha256 |
|---|---|---|---|---|---|---|
| 2026-10-16 19:53:40 | `out/r_pre/placebo_estimates.csv` | 3,615 / 9,506 / 263 | −0.0680 | [−0.0941, −0.0420] | 3 / 3 | `668a1f1ce79bdc6a146e167ba646241f609dc8073bb1461251ff469242d266f4` |
| 2026-10-23 21:48:09 | `out/recovery/june_pre/gen-20261023-s21-pre-20261023/placebo_estimates.csv` | 3,007 / 8,740 / 132 | −0.1210 | [−0.1505, −0.0916] | 2 / 2 | `60a9be5341e5e19f14de46ab8cd64d119509efbd4d064227e899c9c15e5e5ec4` |
| 2026-10-28 02:27:22 | `out/recovery/june_pre/gen-20261027-junef-pre/placebo_estimates.csv` | 3,007 / 8,701 / 196 | −0.1111 | [−0.1406, −0.0817] | 2 / 2 | `0ad075a52000e1d4d5984b8ff04a4f89d773159c7a67d40f9acb697f6f5c50da` |

The same files also hold `leg_pooled` cap rows (−0.0670, −0.1206, −0.1103) and uncapped rows. The placebo of record (§5 item 2, three-bin matching, June population with the participation rule) is unchanged: capped D β −0.0249, case 3 under both clusterings, in the third file. The no-share rows were seen before the signature; they did not change any signed rule.

The `out/r_pre/placebo_estimates.csv` file was rewritten on 16 October (19:53), one day after the 15 October placebo §0 describes; the tracked `out/r_pre/summary.md` (`b37514630310aae94a02f132151ca125664de8d697a6a09377f0b8a6b7f7dc3d`) matches the §0 three-bin figures.

§0 dates that development placebo 15 October. The git record bounds it: commit `ad79372` (15 Oct 19:01) holds no real-data numbers, and commit `c3fe5a5` (16 Oct 14:50:08) adds them in `out/r_pre/summary.md`. The pre-trend refuter (R4c) puts the run between 15 October 23:36 and 16 October 14:50, local time. The signed text is left as it is; this paragraph is the correction.

## 2. Evidence-package hash list

The `placebo_estimates.csv` files are git-ignored and were not hashed in the signed package. Their hashes at the time of this addendum, with the companion files:

```
668a1f1ce79bdc6a146e167ba646241f609dc8073bb1461251ff469242d266f4  out/r_pre/placebo_estimates.csv
b37514630310aae94a02f132151ca125664de8d697a6a09377f0b8a6b7f7dc3d  out/r_pre/summary.md
370b826187df81375725832dc06f5f64a34e7c4d5171f0077c15e4587f8db6b5  out/r_pre/prespec_manifest.json
8df40592785d0583d44a25696e676968925a0f40424c47e6a7c6d5cc0d65b452  out/r_pre/prespec_manifest.md
60a9be5341e5e19f14de46ab8cd64d119509efbd4d064227e899c9c15e5e5ec4  out/recovery/june_pre/gen-20261023-s21-pre-20261023/placebo_estimates.csv
fb85ec40bf643692bd64e32826ef10b6fa5bad6a25670637d82ff231ed4d74b5  out/recovery/june_pre/gen-20261023-s21-pre-20261023/summary.md
0ad075a52000e1d4d5984b8ff04a4f89d773159c7a67d40f9acb697f6f5c50da  out/recovery/june_pre/gen-20261027-junef-pre/placebo_estimates.csv
6a10141568143fcbca377cc5845fa7354182863218edfbea83165fee56572a70  out/recovery/june_pre/gen-20261027-junef-pre/summary.md
8c313c628741f46d5afc894a1585d33f8bf95a21c4bcefcd775c76c5c84ab34f  out/recovery/june_pre/gen-20261027-junef-pre/prespec_manifest.json
70a181fe95f52c3afcc24d2e354df3d6399ef1c859c0031b651321d6beb7603e  prespec/signing.json
```

## 3. The 30 June pre-event movement (prespec §5 item 1)

§5 item 1 asks for specific attention to 30 June 2022 (Tether froze 46 addresses that day). In the signed event study (`post_pretrend.csv`, sha `4c03ebb2…`), the largest one-day pre-event change in the capped-D coefficient is from 30 June to 1 July: 0.0522 → 0.0081 (−0.0441). The next-largest one-day changes are +0.0275 (17 → 18 July) and −0.0195 (13 → 14 June). For capped A the largest one-day change is 13 → 14 June (−0.0467), followed by 30 June → 1 July (−0.0300). This movement must be named wherever the pre-trend is described.

## 4. Wording boundaries that follow from the record

- The design is "pre-specified, signed and hashed privately" (origin: private repository). It is not "pre-registered" (prespec l.3: public registration waits for AIER's sign-off), and not "fixed before any post-event data" (§0 l.17: not a claim of complete blindness).
- The pooled β meets the signed H1 rule; it is not the size of the event effect. The pre-period coefficients decline through June, and parallel pre-trends are rejected by an exploratory joint test (to be reproduced in the committed script `exploratory/o3_pretrend.R`).
- Robustness rows share the baseline window and each assumes parallel trends; agreement among them does not test that assumption.
- No traced mechanism (no swap into USDT, no exit vs fresh addresses); no "persistent" effect.

## 5. Later exploratory work

Every analysis after the results is exploratory and dated, lives under `exploratory/`, and is fixed in a dated, hashed plan before new data are read. None of it reclassifies H1 (prespec §6).
