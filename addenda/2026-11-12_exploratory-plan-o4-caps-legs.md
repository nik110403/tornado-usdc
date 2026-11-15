# Exploratory plan O4 — capped coin legs, cap grid, cap-free outcomes, top-k leave-out, in-time rank — dated 12 November 2026

**Status.** EXPLORATORY, POST-RESULTS, dated 2026-11-12; not confirmatory; does not reclassify H1. Written and hashed BEFORE any of the outputs below exist. The signed files (`prespec/*`, `R/`, `config/`, `gates/`, `run_all.*`) are read (sourced) only, never edited. The signed decision (H1 under capped D, β −0.152) stands whatever these rows show. Source of the requests: journal-submission tribunal chg-74cd74bd (AIER `plans/paper-final/tribunal/2026-11-12-r10-journal-review/`), seat 3 F1, F2, F3 (+ seat 1 cap grid, seat 2 cap-free outcome), refuted-and-standing items S4, S5.

**Inputs.** The signed post run `out/recovery/post/r-gen-M-post` (trace `post_trace.json`, input hashes checked as in `exploratory/o3_pretrend.R`); the panel is rebuilt with the committed helpers (`po_*`, `build_panel`, `prep_est`, `estimate_did`). Same wallets, CEM weights, baseline B, event day (8 Aug 2022 dropped), 35-day post window as the signed run. For F2: `out/exploratory/o1-est-2026-10-30-r5-provenance/o1_paths.csv` and the signed `post_time_path.csv`.

**Outputs.** `out/exploratory/o4-2026-11-12/` only. Every csv carries the EXPLORATORY banner. MANIFEST.sha256, sessionInfo, summary.md. Two runs byte-identical (single thread).

## Rows (all weighted TWFE, `~ tp | uid + day_index`, both `~uid` and `~stratum` clustering, 95% t-intervals, df = clusters − 1)

1. **F1 capped legs.** Under the pre-specified `leg_pooled` cap (one pooled 1/99 pair over USDC/B and USDT/B baseline wallet-days), estimate cu = clamp(USDC/B) and ct = clamp(USDT/B) as outcomes. Assert cu − ct and cu + ct reproduce the signed `leg_pooled` D (−0.150312) and A (−0.143496) to 1e-9. Also report uncapped USDC/B and USDT/B (assert equal to `post_estimates.csv`).
2. **F3 cap grid.** Cap pairs 0.1/99.9, 0.5/99.5, 1/99 (signed, assert reproduces −0.152 and the signed A), 2.5/97.5, 5/95, plus none (assert +0.817), each under the "own" rule (D and A at their own percentiles of pooled unweighted baseline wallet-days, type 7) and under the `leg_pooled` rule. For each: 35-day β for D and A, and the 7→9 Aug move (DiD on days 7 Aug and 9 Aug only = event-study coefficient on 9 Aug with 7 Aug as reference). Report the cut-points and the clip incidence for each pair.
3. **F3 cap-free outcomes.** (a) 1{USDC > 0}; (b) 1{USDC + USDT = 0} (address emptied; seat 3 F10); (c) asinh of the dollar gap, asinh(USDC − USDT) in dollars. 35-day β and 7→9 Aug move each. No cap.
4. **F3 top-k leave-out.** Uncapped D β: per-wallet contribution to β (treated: w_i·Δ_i / Σw_treated; controls: −w_i·Δ_i / Σw_controls, Δ_i = post mean − pre mean) written to file; refit dropping the top k = 1, 5, 10, 20, 50, 100 wallets by |contribution|. Also report the share of the uncapped β carried by the top 10 (the "47.9%" figure, now from a committed script).
5. **F2 in-time rank (descriptive, uncalibrated).** On the extended pre path (o1_paths.csv, capped D, days −100..59), every fake date with a full 60-day window before and 35 days after inside the path: the 60/35 contrast (mean of the 35 days after minus mean of the 60 days before, fake day dropped). Rank the real contrast (from the signed path) among them by value and by |value|. Overlapping windows; not a p-value; labelled uncalibrated.

## Reading rule, fixed now

The question asked of rows 2–4 is only: **does the capped sign of D hold at moderate caps (0.5/99.5 to 5/95), and where does it flip?** Report the flip point plainly; do not choose a cap. Nothing here changes the paper's H1 verdict; any text using these rows says "exploratory, after the results".
