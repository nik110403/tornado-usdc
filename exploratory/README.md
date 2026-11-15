# exploratory/

EXPLORATORY, POST-RESULTS, dated 2026-10-28; not confirmatory; does not reclassify H1.

Nothing in this directory is part of the signed pipeline. It holds post-results diagnostics written after the
signed post run (`out/recovery/post/r-gen-M-post`, trace hash `f83e6201...`). The signed-bound files (`prespec/*`,
`config/blocks.json`, `run_all.*`, `R/`, `gates/`) are only read (sourced), never edited.

## o3_pretrend.R

Script behind the transcript-only numbers of the pre-trend tribunal (`plans/tornado-usdc-pipeline/tribunal/2026-10-28-pretrend/`,
refuter item R5).

```
Rscript exploratory/o3_pretrend.R --repo-root <tree holding out/recovery> --outdir <new or empty dir>
```

Both arguments are required; the script refuses to write under `out/`. Use the R library of the signed run
(`R_LIBS_USER=<library holding the pinned packages>` (see `install_r_packages.R`)). Runtime 5.5-6 minutes, about 6 GB RAM; do not run two copies at once.

What it does:

1. Checks the sha256 of the six signed inputs against `post_trace.json` `input_sha256` (stop on mismatch).
2. Rebuilds the signed panel with the committed helpers (`po_read_params`, `po_read_membership`, `po_pre_stage`,
   `po_read_post_panel`, `po_panel`, `estimate_did`, `prep_est`): no estimation logic is copied.
3. Asserts (stops on failure; `o3_assertions.csv` has a `source` column and `summary.md` reports count and tolerance per source):
   `signed` (tolerance 1e-9): pooled capped D and A beta and SE (uid and stratum clustering) equal `post_estimates.csv`; every
   event-study coefficient and SE equals `post_pretrend.csv` (pre window) and `post_time_path.csv` (pre+post window);
   `internal` (tolerance 1e-9): weekly day-weighted post mean equals the 35-day recentred contrast (self-consistency of this script);
   `review` (tolerances 5e-5, 5e-3 or exact 0): the 9 F1/F2 reproduction checks, whose expected values come from the tribunal
   review/refuter (4-decimal reported values), not from the signed run.
4. Re-fits the event study (same spec as `po_event_study`, `i(day_index, ex, ref = 59) | uid + day_index`) keeping the full
   covariance, under `~uid` and `~stratum`, and derives every number from the saved coefficients and covariance.

Outputs (all in `--outdir`; every csv has the EXPLORATORY banner as its first column; two runs are byte-identical, single thread):

| File | Content |
|---|---|
| `o3_numbers.csv` | one row per reproduced number: id, outcome, cluster, estimate, se, lo, hi, df, p, definition |
| `o3_vcov_{D,A}_{uid,stratum}_{pre,all}.rds`, `o3_vcov_long.csv`, `o3_coefficients.csv` | event-study coefficients and full covariance (pre-only and pre+post) |
| `o3_extrapolation.csv`, `o3_sliding15.csv` | coefficient arithmetic (no inferential uncertainty) |
| `o3_weekly_*.csv` | weekly aggregation C, C beta, C Sigma C', weekly-binned refit diagnostic |
| `o3_clip_incidence.csv`, `o3_clip_by_week.csv` | clip incidence by arm x tail x period / week |
| `o3_assertions.csv`, `o3_input_hashes.csv` | assertion log and input hashes |
| `MANIFEST.sha256`, `sessionInfo.txt`, `summary.md` | output hashes (all but `summary.md`), session and code hashes, run summary |

Conventions: CIs and p-values use t with df = clusters - 1; the Wald F has df (59, clusters - 1). Where the transcript's
definition is ambiguous the script reports each reading as its own row with the definition spelled out (for example
`trend_from_8jul_sample` vs `trend_from_8jul_term`, `rw_*_calendar` vs `rw_*_steps`); none was tuned to a target number. The review-sourced assertion values are regression guards on the reported numbers, not targets the script was fitted to.

Windows and caps (F8): `extrap_resid_*` uses 12 named windows but only 9 distinct day sets (`last40` = `from_29jun`,
`last20` = `from_19jul`, `last10` = `from_29jul`; duplicates are flagged in the row definition). `fake_event_9jul_*` takes the
own caps and B from days 0..59; the signed June placebo uses June-only caps (`R/pre.R:365`), so the two are not the same placebo.

Placebo and baseline variants (F1, F2): the primary sliding 15-vs-15 placebo skips one day like the real contrast skips 8 Aug
(`sliding15_gap30_*`, fake days s+16..s+30 vs base s..s+14, s = 0..29, 30 windows; ratio `real_vs_extreme_gap_placebo_ratio_*`);
`sliding15_nogap_*` (no skipped day, 31 windows) is a labelled alternative. `postmean_minus_base_excl7aug_*` are the refuter's
last-k variants (7 Aug reference coefficient 0 left out of the baseline mean); `postmean_minus_base_last*` keep it in. Rank-selected
maxima (`pre_oneday_move_rank*`, `largest_pre_two_day_move_*`) carry no CI/p (nominal, post-selection SE only);
`rw_post_mean_band95_*` is a heuristic, not a test.

## Crosswalk: transcript number to `o3_numbers.csv` row id (D unless stated; uid and stratum are separate rows)

| Transcript number | Row id |
|---|---|
| Wald F of 59 pre coefficients (2.06 uid, 2.12 stratum; A extra) | `wald_pre59_D_pre_F`, `wald_pre59_A_pre_F` |
| post mean minus 7 Aug (-0.103) | `postmean_minus_base_7aug_only_D` |
| last 5 / 10 / 20 / 30 / from 1 Jul (-0.098 / -0.106 / -0.127 / -0.129 / -0.124) | `postmean_minus_base_last5_D`, `..._last10_D`, `..._last20_D`, `..._last30_D`, `..._from_1jul_D` |
| last 7 / 15 / 40 / from 8 Jul (not stated or refuter-only) | `..._last7_D`, `..._last15_D`, `..._last40_D`, `..._from_8jul_D` |
| all 60 days identity (-0.1519) | `postmean_minus_base_all60_D` |
| refuter last-k, 7 Aug left out (-0.1072 / -0.1306 / -0.1298 / -0.1277 / -0.1248) | `postmean_minus_base_excl7aug_last10_D`, `..._last20_D`, `..._last30_D`, `..._last40_D`, `..._from_1jul_D` |
| fake event 9 Jul (-0.045; A -0.066) | `fake_event_9jul_D`, `fake_event_9jul_A` |
| exposed-group trend, all days (-0.080) | `trend_all_days_D` |
| trend, sample from 8 Jul (-0.135) / alternative term reading | `trend_from_8jul_sample_D` / `trend_from_8jul_term_D` |
| extrapolation residual, all 60 (-0.055) | `extrap_resid_all60_D` |
| extrapolation residual last 20 / 10 / 15 / 7 / 30 (-0.003 / -0.048 / -0.025 / -0.024 / -0.083) | `extrap_resid_last20_D`, `..._last10_D`, `..._last15_D`, `..._last7_D`, `..._last30_D` |
| extrapolation residual from 29 Jun / 1 Jul (-0.114 / -0.123) | `extrap_resid_from_29jun_D`, `extrap_resid_from_1jul_D` |
| random-walk daily-change SD (0.01068; A 0.0131) | `rw_sd_daily_change_D`, `rw_sd_daily_change_A` |
| detrended lag-1 autocorrelation (0.917; A 0.876) | `rw_detrended_lag1_acf_D`, `rw_detrended_lag1_acf_A` (Pearson: `..._cor_*`) |
| RW post-mean SD and band (0.0388, +-0.076) | `rw_post_mean_sd_calendar_D`, `rw_post_mean_band95_calendar_D` (alt `..._steps_*`) |
| 9 Aug two-day jump (-0.0906) vs largest pre two-day move (-0.0571) | `jump_9aug_two_day_D`, `largest_pre_two_day_move_D`, `jump_ratio_D` |
| largest one-day pre moves (-0.0442; 13->14 Jun -0.0195) | `pre_oneday_move_rank1_D` ... `rank5_D` |
| 30 sliding 15-day placebo contrasts, range -0.083 to +0.028, "1.5x" | `sliding15_gap30_min_D`, `sliding15_gap30_max_D`, `real_vs_extreme_gap_placebo_ratio_D` (alternative: `sliding15_nogap_*`) |
| real 15v15 (-0.128) | `real_15v15_D` |
| weekly post day-weighted mean (-0.10271) | `weekly_post_dayweighted_D` |

## o1_extract.py + o1_estimate.R (O1: extended pre-period 1 Mar - 8 Jun 2022, SPEC S33b)

EXPLORATORY, POST-RESULTS, dated 2026-10-28; not confirmatory; does not reclassify H1. Written against fixtures only (no real wallet was queried, nothing under
`out/` was read). Conformance target: section 2 of the FINAL plan `PLAN_o1_o2_2026-10-28.md` (revised after tribunal chg-808f70bb); where code and plan disagree the plan wins.

```
python3 exploratory/o1_extract.py resolve-blocks --endpoint mevblocker        # pin + day-0 block by timestamp bisection (non-sample RPC)
python3 exploratory/o1_extract.py run --membership <match_membership.csv> --signed-cov <covariates.csv> --signed-panel <wallet_coin_day_pre.csv> \
    --pin-block N --day0-block N --out out/exploratory/o1-<date> [--resume] [--walk-endpoint mevblocker] \
    [--max-mismatch-frac 0.005] [--max-reverify-frac 0.01] [--reverify-fraction 0.05]
Rscript exploratory/o1_estimate.R --repo-root <tree holding out/recovery> --o1-dir <the extraction --out> --outdir out/exploratory/o1-est-<date> \
    [--high-ge 0.20] [--low-le 0.10] [--stable-tol 0.05] [--disclose-looser-threshold "<text>"]
```

Fit (A4, `PLAN_o1_o2_2026-10-28.addenda.md`): `o1_fit` is closed form on an N x T matrix, not `feols` (the `feols` version was OOM-killed at > 15 GB on 2.5M rows x 159 columns); same estimator, agreement with `feols` tested on fixtures, and `tests/check_o1_closedform_real.R --repo-root <tree holding out/recovery>` must pass against O3 before the estimate is rerun. Each fit adds under 1 GB on top of the process's about 4 GB panel build (about 0.6 GB and 8 s per fit measured on a synthetic 16k x 160 panel); an unbalanced panel is an error.

Extraction (plan 2.2). Opening balance = `balanceOf` at the pinned last block before 2022-03-01 00:00 UTC; Transfer walk (gates/g4 windowed getLogs, 1,000-address
units) plus USDT DestroyedBlackFunds, fetched as topic-only log ranges (no CSV is read) under the same rules, through the last block of 9 Jun (`--day0-block`);
the identity (tolerance 0) ties the reconstructed end-of-9-Jun balance to the signed pre-panel `day_index 0` for both coins. Pin and day-0 blocks are timestamp-verified
at start; every block read (including the pin+1 and day0+1 header reads, marked `timestamp_only`) is a receipt. A failed log range is retried ONCE on the second
endpoint (per unit: the whole pin+1..day0 range) before the unit is marked gap. Per wallet: `ok` | `mismatch` (identity, negative reconstructed balance, no signed day 0)
| `missing` (a gap in any unit touching the wallet, the DestroyedBlackFunds range, a pin balance unreadable on both endpoints, or a re-verify difference; never zero-filled).
Stops (exit 2, status + summary written, no panel): mismatches > `--max-mismatch-frac` (0.005) of the COMPLETE wallets in EITHER arm; or > `--max-reverify-frac` (0.01) of the
re-verified wallets of either arm differing. Re-verify: ceil(5%) of the `ok` wallets within arm (seed 808, no minimum) re-read on the endpoint other than the one that produced
each record: pin balances, day-0 balances (vs the signed panel), log ranges (count + sha256 per wallet/coin/position, DestroyedBlackFunds included) and the DERIVED daily path.
Hardening: realpath'd repo root in the `--out` guard; `run_params.json` binds the producer identity (`rpc.producer_identity` over o1_extract.py, g4_controls.py, g1_cohort.py,
rpc.py), thresholds, seed and input hashes, and a resume refuses any change; the checkpoint stores a sha256 per record (verified on load), type-checks balances as non-negative
ints, drops only an unterminated torn tail and restores a lost final newline; outputs are temp + fsync + `os.replace`, opened `O_NOFOLLOW`, and a resume refuses an outdir
holding a symlink; strict `0x` hex, no `removed` logs, one blockHash per block. Single process, flock on `<out>/.slot.lock`.
Outputs: `o1_panel.csv` (ok wallets, day_index -100..-1), `o1_wallet_status.csv`, `o1_pin_balances.csv`, `o1_summary.json`, `receipts.jsonl`, `MANIFEST.sha256`.

Estimation (plan 2.3-2.7). The estimation refuses unless the manifest lists `o1_panel.csv`, `o1_pin_balances.csv` and `o1_summary.json`, the extraction's membership /
covariates / pre-panel hashes equal the signed `post_trace.json` `input_sha256` (`match_membership` / `pre_covariates` / `pre_panel`), and the extraction thresholds were not looser
than the plan defaults (or `--disclose-looser-threshold` is given; the disclosure is printed and written to `summary.md`). Signed panel rebuilt as in o3 (hash-checked),
extended with the O1 rows; B and the signed caps unchanged. Event study `i(day_index, ex, ref = 59) | uid + day_index` on days -100..59, both clusterings, capped and uncapped D and
A; wallets without an O1 path are dropped from the fit (count and weight share by arm in `o1_dropped_missing_path.csv`); with none dropped the 9 Jun-7 Aug coefficients are asserted
equal to `post_pretrend.csv` within 1e-9. E3 bands are `high` (L >= +0.20) / `low` (L <= +0.10) / `between`; the random-walk rates (D 29% / 32%, A 37%; continued drift 65-78% D,
75-85% A) are printed beside every E3 result; the slope is a point estimate with no interval. E4 always reports the primary (11->13 May) and the secondary (largest two-day move
8-14 May) plus the cumulative change c(13 May)-c(8 May); only the primary drives the trigger, which fires if it fires in the full OR the restricted row. Tie-break (plan 2.5):
a different restricted band gives "between: full and restricted rows disagree"; both `low` but not both stable gives "low, not stable". `o1_reading.md` prints the section 2.7 text
per outcome/cluster, the joint D/A rule and the coverage table beside it. The age-restricted row re-matches within the signed MATCHED wallets that pass the pin predicate (the
set whose pin state was extracted; plan 2.1 population), CEM recomputed by the prespec formula (`cem_match_fixed`, same strata and cut-points), exposed wallets without a control
dropped, dropped weight share reported by arm; signed caps stay in force. Assumption flagged: the "stable" reference m is the mean of days 0..59 (including the zero reference coefficient).

Tests (fixtures in `exploratory/tests/fixtures/`, generated by `exploratory/tests/make_o1_fixtures.py`; synthetic wallets, logs and day-0 values):
`python3 -m pytest exploratory/tests/ -q` and `Rscript exploratory/tests/test_o1_estimate.R`.

## o1_restricted_posthoc.R (post-hoc variants of the O1 age-restricted row, 2026-10-30)

EXPLORATORY, POST-HOC VARIANT; disclosed under plan 3.2 beside the as-planned row; not confirmatory; does not reclassify H1. `o1_estimate.R` is unchanged (the as-planned run stands); this script sources it as a
library (`O1_NO_MAIN`) and calls the same `o1_analyse`, `o1_flatten`, `o1_tiebreak`. It recomputes the exact type-7 cut-points with `po_primary_cuts` (the as-planned row used the 15-digit JSON cut-points) and STOPS unless their
hash equals `params$cutpoints_hash`. V1: exact cuts, 3:1 redraw. V2: exact cuts, no redraw (all admitted controls of a stratum kept, weights `(m_C/m_T)*(n_exposed/n_ctrl_sel)`). Outputs per variant (`v1_*`, `v2_*`): paths, estimands,
tiebreak vs the signed full-sample row, dropped weight by arm, wallet counts by arm, strata, restricted vcov rds; plus `summary.md`, `PROVENANCE.txt`, `MANIFEST.sha256`. Same CLI as `o1_estimate.R`:
`Rscript exploratory/o1_restricted_posthoc.R --repo-root <tree holding out/recovery> --o1-dir <extraction dir> --outdir out/exploratory/o1-posthoc-<date>`. Test: `Rscript exploratory/tests/test_o1_restricted_posthoc.R`.

## o2_honestdid.R (O2, plan section 1; SPEC S32)

EXPLORATORY, POST-RESULTS. HonestDiD sensitivity of the post mean of the capped event study, built and tested on SYNTHETIC inputs only
before the plan hash (the code is parameterised wherever the final plan may differ; nothing here has seen `out/`).

Isolated library (the signed one is never written to; `exploratory/rlib` is gitignored and holds HonestDiD plus only its new dependencies, none of the
`POST_PINNED_PACKAGES`; `exploratory/tests/test_o2_synthetic.R` checks that and that `verify_post_package_versions` still passes):

```
export R_LIBS_USER=$PWD/exploratory/rlib:$HOME/R/x86_64-pc-linux-gnu-library/4.3     # rlib FIRST, signed library second
# install (once). Current CRAN HonestDiD 0.2.8 needs CVXR >= 1.8 -> Matrix >= 1.7 (would shadow the signed Matrix 1.6.5) and a newer Rust toolchain for clarabel, so it
# is NOT installable here. Pinned instead: HonestDiD 0.2.7 (CVXR >= 1.0) + CVXR 1.0-15 (CRAN archive); both are checked at run time (CFG$honestdid_version).
Rscript -e 'l <- "exploratory/rlib"; install.packages(c("Rmpfr","ECOSolveR","osqp","scs"), lib = l, repos = "https://cloud.r-project.org")
            install.packages("https://cran.r-project.org/src/contrib/Archive/CVXR/CVXR_1.0-15.tar.gz", lib = l, repos = NULL, type = "source")'
curl -O https://cran.r-project.org/src/contrib/Archive/HonestDiD/HonestDiD_0.2.7.tar.gz && R CMD INSTALL --library=exploratory/rlib HonestDiD_0.2.7.tar.gz
```

Runtime (measured, synthetic, 8-core host): one FLCI smoothness point ~50-70 s at 59 pre periods (~10 s at 6); one relative-magnitudes (C-LF) point on the daily
calendar took 385 s at `gridPoints = 5` (HonestDiD's default is 1000, so the daily RM grid at the default is infeasible; the runtime valve below decides) and 20-60 s on
the 7-pre-bin weekly calendar. Per-cell peak memory about 0.5 GB. HonestDiD's RM test inversion runs over a theta grid `[grid.lb, grid.ub]` of `gridPoints` points, in ABSOLUTE
units, centred at 0 (not at the estimate): `[-k sdTheta, +k sdTheta]` with `sdTheta = sqrt(l' Sigma_post l)`, k = 20 by default (deltarm.R l.253-283). The harness always passes the
grid explicitly (`gridPoints = 1000`, k = 20 without a valve) so that the step `dtheta = (grid.ub - grid.lb)/(gridPoints - 1)` is known; it is written beside every RM interval
(`o2_evals.csv`, column `dtheta`). A hit at EITHER edge of the grid (`lo_at_grid_edge`, `hi_at_grid_edge`) or HonestDiD's "CI is open at one of the endpoints" warning
(`open_endpoint`) voids THAT interval only (addendum A2 item 2): its status is `edge: ...`, it is neither clear nor lost, and the other intervals of the cell are unaffected. `--rm-grid-lb/--rm-grid-ub`
(absolute bounds) and `--repo-root` exist for synthetic tests only and are refused otherwise. Every evaluation row also records the package's warnings (`warnings`, nothing is muffled; A2 item 7) and `eval_flag` =
"LP for eta did not converge" when HonestDiD reported that; the evaluation is otherwise unaltered.

Order of work (real run): 1. `--valve` (once, before any cell); 2. the 8 cells, each with the same `--valve-plan`; 3. `--collect`.

```
Rscript exploratory/o2_honestdid.R --valve --in <o3 out dir> --out out/exploratory/o2-<date>/valve --signed-time-path <post_time_path.csv>
Rscript exploratory/o2_honestdid.R --cell daily_uid_D --in <o3 out dir> --out out/exploratory/o2-<date>/cells/daily_uid_D --signed-time-path <post_time_path.csv> --valve-plan out/exploratory/o2-<date>/valve/o2_valve_plan.csv
Rscript exploratory/o2_honestdid.R --collect --cells-dir <dir of the 8 cell dirs> --out out/exploratory/o2-<date>/collected --valve-dir out/exploratory/o2-<date>/valve
```

Real runs write only below `out/exploratory/<dir>` (whitelist: any other path inside the repo, bare `out/exploratory`, `out/recovery`, `..` traversal, are refused; outside the repo only
`--synthetic` test runs may write). 8 cells = calendar daily|weekly x cluster uid|stratum x outcome D|A; real runs stay at <= 4 concurrent.

**Runtime valve (plan 1.5 step 5 + addendum A1 item 6).** `--valve` times ONE relative-magnitudes call at Mbar = 0 per calendar on that calendar's uid/D cell, with the planned method
(C-LF) and `gridPoints` (1000); its interval is discarded unread (`o2_valve_probes.csv` holds the probed cell id, setting, seconds and an ok/edge/failed/timeout class only; nothing is printed).
A probe that is not `ok` (failed, timed out or hit the grid edge) trips the valve for that calendar and its seconds feed no T (T is NA until a re-probe under a later step succeeds).
Projected time of a cell `T = probe seconds x (planned evaluations of that cell) x 1.5 + SD time of the cell`; planned evaluations = RM grid points (40) + bisection evaluations, each threshold x flag counted
separately (D cells: 40 + 2 x 2 x 10 = 80; A cells: 40 + 3 x 2 x 10 = 100); the SD time (daily only; weekly smoothness is not computed) is one measured FLCI point at M = 0, discarded unread, times the
cell's planned SD evaluations (17 grid points + thresholds x 10). Trigger: `T > 2 h` for any cell (`--valve-cell-seconds 7200`), or the sum over the 8 cells > 8 h wall-clock after parallelism (`--valve-total-seconds 28800`,
`--valve-procs 4`; 4 cells per calendar). If it fires, an ORDERED chain runs, the trigger being re-checked after every step (re-probing under the new setting after (ii) and (iii)) and the
chain stopping as soon as the rule passes; it is applied per calendar x restriction, never per grid point:

1. (i) parallel: one process per cell (results identical; only the wall-clock sum changes);
2. (ii) method `Conditional` instead of C-LF for the affected calendar;
3. (iii) `gridPoints = 200` (floor) and the theta grid tightened to `[l'beta_post - 8 sdTheta, l'beta_post + 8 sdTheta]`, centred on the estimate (addendum A2 item 1; the planned and (ii) grids stay +-20 sdTheta
   around 0; `dtheta` and `rm_grid_center` are printed beside every interval);
4. (iv) daily RM is not computed (an explicit "daily RM not computed (valve iv)" row appears in `o2_breakdown.csv` and `o2_evals.csv`); weekly RM stays. A weekly calendar still over
   the limit after (iii) keeps RM, its rows are labelled "valve exhausted" (`valve_label`), and its time is excluded from the sum that could push daily RM to (iv) (A2 item 6); the log says the trigger still fires.

Sum trigger (A2 item 5): the chain is applied first to calendars with a cell over the 2 h limit, else to the largest contributor to the sum. Under the defaults (T per cell <= 2 h, 8 cells, 4 processes, 8 h limit)
the sum trigger is believed unreachable after step (i): with every cell at most 2 h the 8 cells total at most 16 h, so the 4-process wall-clock max(largest cell, sum/4) is at most 4 h, below the 8 h limit;
before (i) the sequential sum can exceed 8 h and (i) cures it. The rule is kept in the code (tested with scripted probes and non-default limits) and disclosed here.

Outputs: `o2_valve_plan.csv` (one row per calendar: `rm_method`, `rm_grid_points`, `rm_grid_k`, `rm_grid_center` (`zero` or `estimate`), `rm_computed`, `exhausted`, `fallback`, probe seconds and status, projected T), `o2_valve_log.csv` (every check and step), `o2_valve_probes.csv`.
Every cell of a calendar, uid AND stratum, reads that calendar's row via `--valve-plan`, so both clusterings run under the same method and grid (collect refuses a mismatch). Real cells require
`--valve-plan`; it cannot be used together with `--rm-method/--rm-grid-points`. Shrinking the pre-window is forbidden. There are no `coarse` or `continue` options.

**Two clearance flags (addendum A1).** Every RM interval has `clears_<tau>_nominal` (upper bound < tau) and `clears_<tau>_conservative` (upper bound + dtheta < tau; implies the true clearance, since
the reported upper end sits at or below the true one by less than one step). Steps 2-4 and 6 of the breakdown rule run on each flag from the same grid evaluations; bisection runs inside each
flag's own bracket (evaluations are cached on method, gridPoints, grid bounds, alpha, bound, seed and M, so a shared bracket shares them). `o2_breakdown.csv` has one row per restriction x threshold x
flag (`nominal`, `conservative`; SD/FLCI has no theta grid, the two coincide, one row `single`), with both brackets; the HEADLINE is the conservative one (`headline = TRUE`, `headline_status`).
A failed, timed-out or edge-voided grid point is never a clearance (A2 item 3): a failure before the first loss makes the flag's scan "undetermined beyond M=m" (m = last point of the
contiguous clearing prefix, in `last_clearing_M`); a failure inside a bisection bracket stops that bisection and the row carries `bracket_flag` = "failure in bracket". If only the conservative flag fails at M = 0 the cell reads "no initial clearance (conservative); nominal clears at 0" and the headline is "no initial clearance".

**Joint rule (plan 1.4; collect).** `o2_joint.csv`, per calendar x restriction x outcome x threshold x flag: the joint breakdown is the SMALLER of the uid and stratum breakdowns (binding
cluster named); if either has no initial clearance the joint is "no initial clearance"; no-crossing on one side takes the other's breakdown; if a side is "undetermined beyond M=m" the joint is bounded by m
(or by the other side's certified value, whichever is smaller) with `joint_qualifier` = "undetermined" - it never returns the other side's larger breakdown as if the failed side had cleared (exception: the
other side's bracket lies wholly at or below m, then that bracket is the joint); "failure in bracket" of either side travels as the qualifier; other undetermined statuses are not resolved in favour of either.
The merged `o2_evals.csv` gets `joint_clears_*` columns on the grid points: TRUE only if both clusterings clear, FALSE if either loses, NA otherwise. Headline joint = conservative flag.

**Timeouts.** `--point-seconds` limits one evaluation. HonestDiD swallows the resulting error into a printed message, so the harness captures it (and checks elapsed time) and writes
status `timeout`, never `failed: empty`; a timeout is neither clear nor lost.

Every HonestDiD call passes `seed = 0` and `parallel = FALSE` explicitly. `postPeriodMomentsOnly` is not an argument of `createSensitivityResults*`; the pinned 0.2.7 default (TRUE) runs, and the
script asserts that default and `seed = 0` at start-up (A2 item 8).

Input: `o3_vcov_<O>_<cl>_all.rds` (O3 layout, already recentred on 7 Aug). The script refuses a file whose day-59 coefficient or covariance row is not
exactly 0 (never re-recentres) and, for real inputs, requires `--signed-time-path` (coefficients equal the signed `post_time_path.csv` within 1e-9).
Weekly: bins as `o3_weekly_bins.csv`, C beta and C Sigma C' with the reference bin subtracted once (rows are contrasts, asserted), 7 pre + 5 post bins,
target weights (6,7,7,7,8)/35 and the identity to the 35-day mean asserted; weekly smoothness is written as "not implemented (no calendar-aware mapping)".

Per cell (`--out`): `o2_evals.csv` (every evaluation: grid and bisection; interval, status, seconds, method, `grid_points`, `grid_lb`, `grid_ub`, `dtheta`, `clears_<tau>_nominal/_conservative`),
`o2_breakdown.csv` (see above; status, last clearing / first losing grid point, bracket after 10 halvings, non-monotone flag and re-clearing points, failure points), `o2_original.csv`
(unrestricted HonestDiD interval and the target beside normal and t intervals), `o2_timing.csv` (method, gridPoints, fallback in force, valve probe seconds, cell seconds),
`o2_input_hashes.csv`, `sessionInfo.txt`, `MANIFEST.sha256`. Rule (plan 1.5): evaluate all grid points; first grid point must clear
(else "no initial clearance"); breakdown = first grid point that loses clearance; ten bisections inside [last clearing, first losing] only; later re-clearing is flagged non-monotone and
not searched; failures are never clear or lost; no loss on the grid is "no crossing within the grid".

Other parameters the final plan may change are CLI flags or `CFG` at the top: grids, thresholds, alpha, bisections, RM/SD method, planned `--rm-grid-points`, `--point-seconds`, the valve limits.
Any rule change still needs the dated addendum before an affected interval is read.

Collect verifies each cell's MANIFEST (and the valve dir's), refuses mixed HonestDiD/R versions, mixed synthetic/real banners and uid/stratum cells of one calendar that ran under different RM
settings, and writes the merged csvs (`o2_evals`, `o2_breakdown`, `o2_joint`, `o2_original`, `o2_timing`, `o2_input_hashes`, `o2_disagreement`), per-cell
`sessionInfo_<cell>.txt`, `summary.md` and a `MANIFEST.sha256` (header: exploratory, post-results, dated).

Test: `R_LIBS_USER=... Rscript exploratory/tests/test_o2_synthetic.R` (synthetic exact-linear inputs with analytic breakdowns, no-clearance, no-crossing, mock non-monotone and
failure-in-bracket cases, nominal vs conservative flags, lower-edge void, cache-key sensitivity, joint rule, valve chain ordering/re-probing/per-calendar behaviour with a scripted probe, the valve end to end
(fallbacks (ii), (iii), (iv) in cells), failed / timed-out / edge probes tripping the valve, weekly-exhausted not pushing daily, (iii) grid centring, warning capture, `--repo-root` rejection, default grid, timeout label, weekly weights, out/ whitelist, collect).

## o4_caps.R (+ o4_lib.R)

EXPLORATORY, POST-RESULTS, dated 2026-11-12. Script behind the cap/leg requests of the journal-submission tribunal chg-74cd74bd
(seat 3 F1, F2, F3). Binding spec, hashed before any output existed: `addenda/2026-11-12_exploratory-plan-o4-caps-legs.md` (rows 1-5; the
definitions are not changed here). Signed H1 (capped D, beta -0.152) stands whatever these rows show.

```
R_LIBS_USER=<pinned-library> Rscript exploratory/o4_caps.R --repo-root <tree holding out/recovery> --outdir out/exploratory/o4-2026-11-12
R_LIBS_USER=... Rscript exploratory/tests/test_o4_caps.R     # synthetic tests: clamp/leg identity, contribution identity, outdir guard
```

Same CLI, outdir guard, input-hash check and single-thread setting as `o3_pretrend.R`; the signed panel is rebuilt with the committed helpers.
`o4_lib.R` holds the pure functions (caps with a `probs` argument, computed like `cap_values_all`; clamped legs; DiD fit under both clusterings;
per-wallet contributions; guard). Do not run two copies at once (about 6 GB).

| Row | Files | Content |
|---|---|---|
| 1 capped legs | `o4_row1_legs.csv`, `o4_row1_lincomb_check.csv` | cu, ct (pooled 1/99 clamp), cu-ct, cu+ct, uncapped USDC/B, USDT/B; assertions vs signed leg_pooled D/A and post_estimates |
| 2 cap grid | `o4_row2_grid.csv`, `_cutpoints.csv`, `_clip_incidence.csv` | pairs 0.1/99.9, 0.5/99.5, 1/99, 2.5/97.5, 5/95, none; own and leg_pooled; 35-day beta and 7->9 Aug move (days 59 and 61 only) |
| 3 cap-free | `o4_row3_capfree_outcomes.csv` | 1{USDC>0}, 1{USDC+USDT=0}, asinh(USDC-USDT) dollars |
| 4 top-k | `o4_row4_contributions.csv`, `_top10_share.csv`, `_leaveout.csv` | per-wallet contribution to the uncapped D beta, top-10 share, refits dropping top k = 1..100 |
| 5 in-time rank | `o4_row5_fake_contrasts.csv`, `_rank.csv` | descriptive, uncalibrated; real 60/35 contrast ranked among fake dates on `o1_paths.csv` |

Plus `o4_assertions.csv` (stop on failure; tolerance 1e-9 unless stated), `o4_input_hashes.csv`, `MANIFEST.sha256`, `sessionInfo.txt`, `summary.md`
(committed, as for o3; the rest of `out/` is gitignored). Every csv has the EXPLORATORY banner as its first column.
