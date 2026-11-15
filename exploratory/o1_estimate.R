#!/usr/bin/env Rscript
# EXPLORATORY, POST-RESULTS, dated 2026-10-28; not confirmatory; does not reclassify H1.
# O1 estimation (PLAN_o1_o2_2026-10-28.md section 2.3-2.6): the signed event study extended back to 1 Mar 2022 (day_index -100..59),
# reference 7 Aug (day 59), same wallets / CEM weights / baseline B / caps as the signed run, daily path c_t, estimands E1-E4 with bands,
# coverage table and the age-restricted secondary row.
#
# Bands, windows and the "stable" reference follow the FINAL plan section 2.4 (E3 high / low / between; E4 primary single move, secondary
# largest two-day move and cumulative change always reported, only the primary drives the trigger) and stay parameters (O1_DEFAULTS).
# Nothing here chooses a band after seeing data.
#
# Usage: Rscript exploratory/o1_estimate.R --repo-root <tree holding out/recovery> --o1-dir <o1_extract.py --out dir> --outdir <new dir under out/exploratory/>
#        [--high-ge 0.20] [--low-le 0.10] [--stable-tol 0.05] [--disclose-looser-threshold "<text>"]
# Writes only to --outdir (same guard as o3_pretrend.R: under --repo-root/out only out/exploratory/<dir>; out/recovery/ refused anywhere).
# Sourcing this file with O1_NO_MAIN <- TRUE defines the functions only (used by exploratory/tests/test_o1_estimate.R).
suppressPackageStartupMessages({ library(data.table); library(fixest) })
EXPLORATORY_DATE <- "2026-10-28"
BANNER <- sprintf("EXPLORATORY, POST-RESULTS, dated %s; not confirmatory; does not reclassify H1", EXPLORATORY_DATE)
O1_DAY0 <- as.Date("2022-06-09")                      # day_index 0 (as R/utils.R PRE_PERIOD_START; duplicated so the functions stand alone)
o1_day <- function(d) as.integer(as.Date(d) - O1_DAY0)
o1_date <- function(k) format(O1_DAY0 + k)
O1_FIRST_DAY <- -100L; O1_LAST_DAY <- 59L; O1_REF_DAY <- 59L

# ---- parameters (draft-plan defaults; every one overridable) --------------------------------------------------------------
O1_DEFAULTS <- list(
  e3_window = c("2022-03-01", "2022-05-08"),          # 1 Mar - 8 May (pre-Terra)
  high_ge = 0.20, low_le = 0.10,                      # E3 bands on the point estimate of L: high if L >= high_ge, low if L <= low_le, else between
  stable_tol = 0.05, stable_ref_days = 0:59,          # stable <=> E3 = low and |L - m| <= tol, m = mean of the 9 Jun-7 Aug coefficients (days 0..59)
  e4_single = c("2022-05-11", "2022-05-13"),          # E4 PRIMARY (drives the trigger): the single move c(13 May) - c(11 May)
  e4_range = c("2022-05-08", "2022-05-14"),           # E4 SECONDARY: largest |two-day move| with both days in this range (reported, never fires the trigger)
  e4_cum = c("2022-05-08", "2022-05-13"),             # E4 cumulative change c(13 May) - c(8 May) (reported)
  rw_horizons = 2:36,                                 # calendar days since 7 Aug of 9 Aug..12 Sep (O3 rw_post_mean_band95_calendar convention)
  months = list(Mar = c("2022-03-01", "2022-03-31"), Apr = c("2022-04-01", "2022-04-30"), May = c("2022-05-01", "2022-05-31"), `1-8Jun` = c("2022-06-01", "2022-06-08")))

# Plan 2.2 defaults for the extraction's stop thresholds; a looser setting must be disclosed to run the estimation (o1_check_extract_settings).
O1_PLAN_SETTINGS <- list(max_mismatch_frac = 0.005, max_reverify_frac = 0.01, reverify_fraction = 0.05, reverify_seed = 808)
# Plan section 0 / 2.4 E3 (refuter R1, 100k-draw simulation): how often a DRIFTLESS random walk backcast from c(9 Jun) lands in each band, and a
# continued drift in "high". Printed beside every E3 result. The plan states the driftless "low" rate for D only.
O1_RW_RATES <- list(D = list(rw_high = 0.29, rw_low = 0.32, drift_high = "0.65 (OLS-intercept anchor) to 0.78 (c(9 Jun) anchor)"),
                    A = list(rw_high = 0.37, rw_low = NA_real_, drift_high = "0.75 (OLS-intercept anchor) to 0.85 (c(9 Jun) anchor)"))
o1_rw_note <- function(outcome) {
  r <- O1_RW_RATES[[as.character(outcome)]]
  if (is.null(r)) return("random-walk rates: not stated in the plan for this outcome")
  sprintf("L cannot separate drift from a driftless random walk: a driftless random walk lands in 'high' %.0f%% and in 'low' %s of the time; a continued drift lands in 'high' %s (plan 0 / 2.4 E3)",
          100 * r$rw_high, if (is.na(r$rw_low)) "(rate not stated in the plan)" else sprintf("%.0f%%", 100 * r$rw_low), r$drift_high)
}

# ---- path helpers -----------------------------------------------------------------------------------------------------------
# A path is a named numeric vector: names = day_index (consecutive calendar days), values = c_t (reference day = 0).
o1_path <- function(days, coef) setNames(as.numeric(coef), as.character(days))
o1_at <- function(cvec, date) { v <- cvec[as.character(o1_day(date))]; if (anyNA(v)) stop("path lacks day ", date, call. = FALSE); unname(v) }
o1_window <- function(cvec, from, to) { k <- o1_day(from):o1_day(to); if (!all(as.character(k) %in% names(cvec))) stop("path lacks days in window ", from, "..", to, call. = FALSE); k }

# ---- E1 jump rank ------------------------------------------------------------------------------------------------------------
# Two-calendar-day moves c_{t+2} - c_t, both days in the path. Rank 1 = largest |move|; a rank is not a p-value. No one-day moves.
o1_moves2 <- function(cvec) {
  k <- as.integer(names(cvec)); stopifnot(!is.unsorted(k), all(diff(k) == 1L))
  n <- length(cvec); data.table(day_index = k[seq_len(n - 2L)], move = unname(cvec[3:n] - cvec[1:(n - 2L)]))
}
e1_jump_rank <- function(cvec, jump) {
  m <- o1_moves2(cvec)$move; a <- abs(m)
  neg <- m[m < 0]
  list(jump = jump, n_moves = length(m), rank_abs = 1L + sum(a > abs(jump)),
       n_negative_moves = length(neg), rank_among_negative = if (jump < 0) 1L + sum(abs(neg) > abs(jump)) else NA_integer_,
       sd_moves = sd(m), ratio_to_sd = abs(jump) / sd(m), largest_abs_move = max(a), ratio_to_largest = abs(jump) / max(a))
}

# ---- E2 dynamics (reported, no band) --------------------------------------------------------------------------------------------
e2_dynamics <- function(cvec, horizons = O1_DEFAULTS$rw_horizons) {
  ch <- diff(unname(cvec)); n <- length(cvec); sdc <- sd(ch)
  ar <- coef(lm(y ~ x, data.frame(y = unname(cvec[-1]), x = unname(cvec[-n]))))
  hz <- function(h) sdc * sqrt(sum(outer(h, h, pmin))) / length(h)
  list(n_days = n, sd_daily_change = sdc, lag1_autocorr_changes = cor(ch[-1], ch[-length(ch)]),
       ar1_coef = unname(ar[2]), ar1_intercept = unname(ar[1]),
       rw_post_mean_sd_calendar = hz(horizons), rw_post_mean_band95_calendar = 1.96 * hz(horizons))
}

# ---- E3 level (bands on the POINT estimate) ------------------------------------------------------------------------------------
o1_band <- function(L, high_ge = O1_DEFAULTS$high_ge, low_le = O1_DEFAULTS$low_le) {
  stopifnot(is.finite(L), low_le < high_ge)
  if (L >= high_ge) "high" else if (L <= low_le) "low" else "between"
}
# V: covariance of the path over the same named days (reference row/column zero), df = clusters - 1. The interval ignores group-level shocks
# and does NOT enter the band. The slope is a point estimate only (interval type "none"). `outcome` selects the random-walk rates printed beside L.
e3_level <- function(cvec, window = O1_DEFAULTS$e3_window, bands = O1_DEFAULTS[c("high_ge", "low_le")], V = NULL, df = NA_real_, outcome = NA_character_) {
  k <- o1_window(cvec, window[1], window[2]); y <- unname(cvec[as.character(k)])
  L <- mean(y); slope <- unname(coef(lm(y ~ k))[2])
  se <- lo <- hi <- NA_real_
  if (!is.null(V)) {
    w <- setNames(numeric(nrow(V)), rownames(V)); w[as.character(k)] <- 1 / length(k)
    se <- sqrt(drop(t(w) %*% V %*% w)); tc <- qt(0.975, df); lo <- L - tc * se; hi <- L + tc * se
  }
  list(L = L, slope_per_day = slope, slope_interval = "none", n_days = length(k), band = o1_band(L, bands$high_ge, bands$low_le),
       negative_flag = L < 0, sampling_se = se, sampling_lo = lo, sampling_hi = hi,
       interval_note = "sampling interval of L from the saved covariance; ignores group-level shocks; not part of the band",
       rw_rates_note = o1_rw_note(outcome))
}
# stable <=> (E3 = low) and |L - m| <= tol; m = mean of the 9 Jun-7 Aug coefficients. The AR coefficient is not part of the definition.
o1_stable <- function(e3, m, tol = O1_DEFAULTS$stable_tol) identical(e3$band, "low") && abs(e3$L - m) <= tol
o1_stable_ref <- function(cvec, days = O1_DEFAULTS$stable_ref_days) mean(cvec[as.character(days)])

# ---- E4 Terra -----------------------------------------------------------------------------------------------------------------------
# PRIMARY: the single move c(single[2]) - c(single[1]) (default 11 -> 13 May); it alone drives the trigger: |primary| >= |jump| (the 7->9 Aug move).
# SECONDARY: the largest |two-day move| with both days in `range` (default 8-14 May), sign reported; CUMULATIVE: c(cum[2]) - c(cum[1]). Both are
# always reported beside the primary and never fire the trigger (the "info_only" flag is for the reader).
e4_terra <- function(cvec, jump, single = O1_DEFAULTS$e4_single, range = O1_DEFAULTS$e4_range, cum = O1_DEFAULTS$e4_cum) {
  p <- o1_at(cvec, single[2]) - o1_at(cvec, single[1])
  k <- o1_window(cvec, range[1], range[2]); m <- o1_moves2(cvec[as.character(k)])
  i <- which.max(abs(m$move)); t0 <- m$day_index[i]; sec <- m$move[i]
  list(primary_move = p, primary_from_date = single[1], primary_to_date = single[2],
       secondary_move = sec, secondary_from_date = o1_date(t0), secondary_to_date = o1_date(t0 + 2L),
       cumulative_change = o1_at(cvec, cum[2]) - o1_at(cvec, cum[1]), cumulative_from_date = cum[1], cumulative_to_date = cum[2],
       jump = jump, trigger = abs(p) >= abs(jump), secondary_ge_jump_info_only = abs(sec) >= abs(jump))
}

# ---- one sample's estimands --------------------------------------------------------------------------------------------------------
o1_estimands <- function(cvec, jump, V = NULL, df = NA_real_, p = O1_DEFAULTS, outcome = NA_character_) {
  e3 <- e3_level(cvec, p$e3_window, p[c("high_ge", "low_le")], V, df, outcome)
  m <- o1_stable_ref(cvec, p$stable_ref_days)
  list(E1 = e1_jump_rank(cvec, jump), E2 = e2_dynamics(cvec, p$rw_horizons), E3 = e3, m_ref = m, stable = o1_stable(e3, m, p$stable_tol),
       E4 = e4_terra(cvec, jump, p$e4_single, p$e4_range, p$e4_cum))
}

# ---- restricted-row tie-break (can only downgrade; plan 2.5) -------------------------------------------------------------------------
# restricted = NULL (not estimable) -> the full-sample E3 stays, disclosed. Different restricted band -> E3 "between" ("between: full and restricted
# rows disagree"). Stable only if both rows are stable; when both are "low" and not both stable the reading is "low, not stable". The E4 trigger
# (primary move only) fires if it fires in the full sample OR the restricted row. `row` is the section 2.7 table row the text uses.
o1_tiebreak <- function(full, restricted) {
  trig_full <- isTRUE(full$E4$trigger)
  if (is.null(restricted)) {
    band <- full$E3$band; stable <- full$stable; same <- TRUE; trig <- trig_full; down <- FALSE
    note <- "restricted row not estimable: E3 stays full-sample"
  } else {
    same <- identical(full$E3$band, restricted$E3$band); band <- if (same) full$E3$band else "between"
    stable <- same && full$stable && restricted$stable; trig <- trig_full || isTRUE(restricted$E4$trigger)
    down <- !same || (full$stable && !restricted$stable)
    note <- if (same) "restricted E3 band equals the full-sample band" else sprintf("restricted band '%s' differs from full '%s': E3 declared between", restricted$E3$band, full$E3$band)
  }
  row <- if (band == "low") (if (stable) "low, stable" else "low, not stable") else band
  reading <- if (!same) "between: full and restricted rows disagree" else row
  list(e3_band = band, stable = stable, row = row, reading = reading, downgraded = down, e4_trigger = trig, note = note)
}

# ---- section 2.7 reading text (written before any March balance was read) ------------------------------------------------------------------
O1_READINGS <- data.table(
  row = c("high", "low, stable", "low, not stable", "between"),
  supports = c(
    "Before June the gap sat well above its late-July level; this is what a continued drift would produce, and also what a driftless random walk from the June level produces about 29% (D) / 37% (A) of the time. Only the break (E1) remains informative about the event.",
    "The full-window baseline is not contradicted as a counterfactual level.",
    "The gap sat at a different level before June than in the window (sign and size reported; L < 0 flagged).",
    "Nothing beyond the paths and E1/E2/E4."),
  cannot = c(
    "That the gap had been drifting (L does not separate drift from a random walk); any size statement against the 7-Aug or full-baseline reference.",
    "That the pooled -0.152 is 'the' size: at m ~ +0.05 the stable band (D [-0.001, +0.099]) also contains the late-July level (~ 0), so O1 cannot choose between the full-window and the 7-Aug reference. That the gap had no drift (a random walk lands in 'low' about 32% of the time for D).",
    "A trend chosen because it yields case 1 (the 1-Jul and 8-Jul windows lean case 1 and are reported as such, not adopted).",
    "-"))
O1_E4_READING <- c(supports = "An equally large move occurred at a non-sanction date; the 9-Aug move is not unusual for this series and is not specific to the sanction.",
                   cannot = "Any causal statement about what moved the gap on either date.")
# Joint rule (plan 2.7): a reading attributed to the stablecoin position as a whole needs D and A in the same row; otherwise the combined reading is "between".
o1_joint_row <- function(row_D, row_A) if (identical(row_D, row_A)) row_D else "between"
# tb: data.table(outcome, cluster, reading, row, e4_trigger); coverage: the o1_coverage table, printed beside the readings (plan 2.6 / 2.7).
o1_reading_lines <- function(tb, coverage = NULL) {
  out <- c(BANNER, "", "Section 2.7 readings (full sample after the tie-break). Every statement is exploratory and post-results; the signed H1 case is unchanged by any of them.", "")
  for (i in seq_len(nrow(tb))) {
    r <- tb[i]; txt <- O1_READINGS[O1_READINGS$row == r$row]
    out <- c(out, sprintf("[%s | cluster %s] E3 reading: %s (row used: %s)", r$outcome, r$cluster, r$reading, r$row),
             sprintf("  may support: %s", txt$supports), sprintf("  cannot support: %s", txt$cannot), sprintf("  %s", o1_rw_note(r$outcome)),
             if (isTRUE(r$e4_trigger)) sprintf("  E4 trigger FIRED (primary Terra move >= the 9-Aug move, full or restricted row). May support: %s Cannot support: %s", O1_E4_READING[["supports"]], O1_E4_READING[["cannot"]])
             else "  E4 trigger not fired (primary Terra move < the 9-Aug move in both rows)", "")
  }
  for (cl in unique(tb$cluster)) {
    d <- tb[tb$outcome == "D" & tb$cluster == cl]; a <- tb[tb$outcome == "A" & tb$cluster == cl]
    if (nrow(d) && nrow(a)) out <- c(out, sprintf("[joint D+A | cluster %s] combined reading row: %s%s", cl, o1_joint_row(d$row, a$row),
                                                  if (identical(d$row, a$row)) "" else sprintf(" (D: %s; A: %s; each outcome's row is reported alone under its own name)", d$row, a$row)))
  }
  if (!is.null(coverage)) out <- c(out, "", "Coverage and clip incidence (plan 2.6; reported without a threshold; no reading is suppressed because of them):", capture.output(print(coverage, nrows = Inf)))
  out
}

# ---- age-restricted predicate (state at the pin only) ----------------------------------------------------------------------------------
# cov: data.table(address, first_send_block); pin: data.table(address, usdc, usdt) (NA = missing read); pin_block. Admit <=> first_send_block <= pin_block
# OR usdc > 0 OR usdt > 0. A wallet whose first clause fails and whose pin balances are missing (either coin NA and no positive balance) is "unknown".
o1_age_predicate <- function(cov, pin, pin_block) {
  d <- merge(cov[, .(address, first_send_block = as.numeric(first_send_block))], pin[, .(address, usdc, usdt)], by = "address", all.x = TRUE)
  d[, c1 := !is.na(first_send_block) & first_send_block <= pin_block]
  d[, c2 := !is.na(usdc) & usdc > 0][, c3 := !is.na(usdt) & usdt > 0]
  d[, admitted := c1 | c2 | c3]
  d[, unknown := !admitted & (is.na(usdc) | is.na(usdt))]
  d[]
}
o1_admissions <- function(pred, arm_of) {
  pred[, arm := arm_of[address]]
  rbindlist(list(pred[, .(clause = "first_send_block<=pin", n = sum(c1)), by = arm], pred[, .(clause = "USDC pin balance>0", n = sum(c2)), by = arm],
                 pred[, .(clause = "USDT pin balance>0", n = sum(c3)), by = arm], pred[, .(clause = "admitted (any clause)", n = sum(admitted)), by = arm],
                 pred[, .(clause = "unknown (excluded)", n = sum(unknown)), by = arm], pred[, .(clause = "not admitted, not unknown", n = sum(!admitted & !unknown)), by = arm]))
}
o1_dropped_share <- function(full_matched, kept_uid) {   # full_matched: uid, treat, w -> dropped count and weight share by arm
  full_matched[, .(n_total = .N, n_dropped = sum(!(uid %in% kept_uid)), weight_share_dropped = sum(w[!(uid %in% kept_uid)]) / sum(w)), by = .(arm = fifelse(treat, "exposed", "control"))]
}

# ---- event-study fit and capped/uncapped data --------------------------------------------------------------------------------------------
clampv_ <- function(x, c2) pmin(pmax(x, c2[1]), c2[2])
# panel: build_panel() output on days -100..95 (B and the signed caps unchanged: B uses days 0..59 only). Signed caps are PASSED (params cap_values), not recomputed.
o1_prep <- function(panel, caps, capped = TRUE, event_day = 60L) {
  d <- panel[day_index != event_day & day_index >= O1_FIRST_DAY & day_index <= O1_LAST_DAY, .(uid, day_index, treat, w, stratum, D, A)]
  if (capped) d[, `:=`(D = clampv_(D, caps$D), A = clampv_(A, caps$A))]
  d[, ex := as.numeric(treat)]
  d[]
}
# Returns list(days, b, V, G, cluster, n_obs); the reference day carries coefficient 0 and a zero row/column. list(error = reason) on failure.
# Closed form (plan addendum A4), the SAME estimator as feols(y ~ i(day_index, ex, ref) | uid + day_index, weights = w, cluster, fixef.tol = 1e-11) of the
# earlier version (kept as a private reference in tests/test_o1_estimate.R): on a balanced panel with w constant within wallet the saturated day x arm model has
# b_t = (ybar_t^ex - ybar_ref^ex) - (ybar_t^c - ybar_ref^c) (w-weighted arm means), residual e_it = (y_it - ybar_i) - (ybar_t^arm - ybar^arm), and, because
# sum_t e_it = 0 within wallet, wallet-day score w_i (ex_i - exbar) e_it, bread [Q (I - 11'/T)]^-1 on the T-1 non-reference days, Q = sum_i w_i (ex_i - exbar)^2.
# V = adj * bread * (sum_g S_g S_g') * bread with fixest 0.14.2's default ssc: adj = (n-1)/(n-K) * G/(G-1); K = (T-1) coefficients + T day FE (the uid FE is nested
# in either cluster and not counted; checked against feols for uid and stratum). Zero-weight wallets are dropped as feols drops zero-weight rows (n and G of the
# adjustment then exclude them). Works on an N x T wide matrix, never an expanded design matrix. An unbalanced panel is an error, not a fallback.
o1_fit <- function(d, outcome, cluster, ref = O1_REF_DAY) {
  err <- function(m) list(error = m)
  days <- sort(unique(d$day_index)); Tn <- length(days)
  if (!(ref %in% days) || Tn < 3L) return(err("reference day absent or fewer than 3 days"))
  y <- d[[outcome]]; w <- d$w; ex <- d$ex; cl <- d[[cluster]]
  if (anyNA(y) || anyNA(w) || !all(is.finite(y)) || !all(is.finite(w))) return(err("NA or non-finite outcome or weight"))
  if (any(w < 0)) return(err("negative weight"))
  if (anyNA(ex) || !all(ex %in% c(0, 1)) || anyNA(cl)) return(err("exposure not 0/1 or cluster NA"))
  o <- order(d$uid, d$day_index); u <- d$uid[o]; N <- nrow(d) %/% Tn
  if (nrow(d) != N * Tn || !identical(d$day_index[o], rep(days, N)) || !identical(u, rep(u[seq(1L, by = Tn, length.out = N)], each = Tn)) || anyDuplicated(u[seq(1L, by = Tn, length.out = N)]))
    return(err("panel not balanced: every uid must have every day exactly once"))
  wide <- function(x) matrix(x[o], N, Tn, byrow = TRUE)
  Y <- wide(y); Wm <- wide(w); Em <- wide(ex); Cm <- wide(cl)
  wi <- Wm[, 1]; exi <- Em[, 1]; cg <- Cm[, 1]
  if (any(Wm != wi) || any(Em != exi) || any(Cm != cg)) return(err("weight, exposure or cluster varies within wallet"))
  keep <- wi > 0; n_adj <- sum(keep) * Tn            # feols drops zero-weight rows
  Y <- Y[keep, , drop = FALSE]; wi <- wi[keep]; exi <- exi[keep]; cg <- cg[keep]
  armok <- function(a) any(a) && sum(wi[a]) > 0
  if (!armok(exi == 1) || !armok(exi == 0)) return(err("an arm is empty (all days)"))
  am <- function(a) colSums(wi[a] * Y[a, , drop = FALSE]) / sum(wi[a])      # w-weighted arm mean per day
  m1 <- am(exi == 1); m0 <- am(exi == 0); ri <- match(ref, days)
  bfull <- (m1 - m1[ri]) - (m0 - m0[ri])
  yi <- rowMeans(Y); E <- Y - yi
  for (a in list(exi == 1, exi == 0)) E[a, ] <- sweep(E[a, , drop = FALSE], 2, am(a) - sum(wi[a] * yi[a]) / sum(wi[a]))
  exb <- sum(wi * exi) / sum(wi); Q <- sum(wi * (exi - exb)^2)
  if (!is.finite(Q) || Q <= 0) return(err("no exposure variation"))
  S <- rowsum(wi * (exi - exb) * E[, -ri, drop = FALSE], cg)               # cluster scores, G x (T-1)
  bread <- solve(Q * (diag(Tn - 1L) - 1 / Tn)); Gc <- nrow(S)
  Vr <- (n_adj - 1) / (n_adj - (2L * Tn - 1L)) * Gc / (Gc - 1) * (bread %*% crossprod(S) %*% bread)
  b <- setNames(numeric(Tn), days); b[-ri] <- bfull[-ri]
  V <- matrix(0, Tn, Tn, dimnames = list(days, days)); V[-ri, -ri] <- Vr
  if (!all(is.finite(b)) || !all(is.finite(V))) return(err("coefficients dropped (collinear) or non-finite"))
  list(days = days, b = b, V = V, G = uniqueN(d[[cluster]]), cluster = cluster, n_obs = nrow(d))
}

# ---- coverage / clip table (plan 2.6) -----------------------------------------------------------------------------------------------------
# panel: full matched panel (ALL matched wallets that have any row), ok_uid: wallets with an O1 path, matched: uid, treat, w (all matched wallets),
# caps: signed caps. By month x arm, CEM-weighted over matched wallet-days: share valid (wallet has an O1 path), share holding zero combined
# (usdc+usdt == 0, among valid), clip incidence beyond each cap by tail (among valid; D and A).
o1_coverage <- function(panel, ok_uid, matched, caps, months = O1_DEFAULTS$months) {
  rows <- list()
  for (mn in names(months)) {
    k <- o1_day(months[[mn]][1]):o1_day(months[[mn]][2]); nd <- length(k)
    pm <- panel[uid %in% ok_uid & day_index %in% k]
    for (arm in c("exposed", "control")) {
      mt <- matched[treat == (arm == "exposed")]; pa <- pm[treat == (arm == "exposed")]
      den <- sum(mt$w) * nd
      row <- data.table(month = mn, arm = arm, share_valid = sum(pa$w) / den, share_zero_combined = if (nrow(pa)) sum(pa$w * (pa$A == 0)) / sum(pa$w) else NA_real_)
      for (o in c("D", "A")) {
        cp <- caps[[o]]
        row[[paste0("clip_", o, "_lower")]] <- if (nrow(pa)) sum(pa$w * (pa[[o]] < cp[1])) / sum(pa$w) else NA_real_
        row[[paste0("clip_", o, "_upper")]] <- if (nrow(pa)) sum(pa$w * (pa[[o]] > cp[2])) / sum(pa$w) else NA_real_
      }
      rows[[length(rows) + 1L]] <- row
    }
  }
  rbindlist(rows)
}

# ---- analysis of one sample (panel -> paths, estimands) -----------------------------------------------------------------------------------------
# jumps: list(D = c_61 - c_59, A = ...) from the signed post_time_path.csv (7 -> 9 Aug). Returns list(paths, estimands, fits, dropped).
o1_analyse <- function(panel, ok_uid, caps, jumps, params = O1_DEFAULTS, clusters = c("uid", "stratum"), variants = c(capped = TRUE, uncapped = FALSE)) {
  paths <- list(); ests <- list(); fits <- list()
  for (vn in names(variants)) {
    d <- o1_prep(panel, caps, variants[[vn]])[uid %in% ok_uid]
    for (o in c("D", "A")) for (cl in clusters) {
      f <- o1_fit(d, o, cl); fits[[paste(vn, o, cl, sep = "|")]] <- f
      if (!is.null(f$error)) { ests[[length(ests) + 1L]] <- list(variant = vn, outcome = o, cluster = cl, status = "not_estimable", reason = f$error); next }
      cv <- o1_path(f$days, f$b)
      paths[[length(paths) + 1L]] <- data.table(variant = vn, outcome = o, cluster = cl, day_index = f$days, date = o1_date(f$days), coef = unname(f$b), se = sqrt(diag(f$V)), n_clusters = f$G)
      ests[[length(ests) + 1L]] <- list(variant = vn, outcome = o, cluster = cl, status = "estimated", est = o1_estimands(cv, jumps[[o]], f$V, f$G - 1, params, o))
    }
  }
  list(paths = rbindlist(paths), estimands = ests, fits = fits)
}

# flat table of estimands (one row per variant x outcome x cluster x estimand field)
o1_flatten <- function(ests, sample_label) {
  rbindlist(lapply(ests, function(e) {
    if (!identical(e$status, "estimated")) return(data.table(sample = sample_label, variant = e$variant, outcome = e$outcome, cluster = e$cluster, field = "status", value = e$status))
    rbindlist(lapply(names(e$est), function(g) {
      x <- e$est[[g]]
      scalar <- !is.list(x); if (scalar) x <- setNames(list(x), g)
      data.table(sample = sample_label, variant = e$variant, outcome = e$outcome, cluster = e$cluster, field = if (scalar) g else paste0(g, ".", names(x)), value = vapply(x, function(z) as.character(z)[1], ""))
    }))
  }))
}

# ---- extraction-output checks (pure; tested in tests/test_o1_estimate.R) ----------------------------------------------------------------------
O1_REQUIRED_FILES <- c("o1_panel.csv", "o1_pin_balances.csv", "o1_summary.json")
o1_check_manifest <- function(names, required = O1_REQUIRED_FILES) {
  if (!length(names) || anyDuplicated(names) || any(grepl("/", names, fixed = TRUE)) || any(names %in% c("", ".", ".."))) stop("o1 MANIFEST is empty, has duplicates or non-flat names", call. = FALSE)
  if (length(miss <- setdiff(required, names))) stop("o1 MANIFEST does not list: ", paste(miss, collapse = ", "), call. = FALSE)
  invisible(TRUE)
}
# o1_hashes: o1_summary.json params$input_hashes; trace_hashes: post_trace.json input_sha256. Extraction-side name -> signed-input name.
O1_HASH_MAP <- c(membership = "match_membership", signed_cov = "pre_covariates", signed_panel = "pre_panel")
o1_check_input_hashes <- function(o1_hashes, trace_hashes, map = O1_HASH_MAP) {
  for (k in names(map)) {
    a <- o1_hashes[[k]]; b <- trace_hashes[[map[[k]]]]
    if (is.null(a) || is.null(b) || !identical(as.character(a), as.character(b))) stop(sprintf("extraction input '%s' (sha256 %s) is not the signed '%s' (%s): stop", k, as.character(a)[1], map[[k]], as.character(b)[1]), call. = FALSE)
  }
  invisible(TRUE)
}
# Refuses when an extraction setting was looser than the plan default (or absent) and no disclosure text was given. Returns the deviations (character(0) = none).
o1_check_extract_settings <- function(params, disclosure = NULL, plan = O1_PLAN_SETTINGS) {
  dev <- character()
  chk <- function(nm, bad) { v <- params[[nm]]; if (is.null(v) || !is.numeric(v) || bad(v)) dev <<- c(dev, sprintf("%s=%s (plan %s)", nm, if (is.null(v)) "absent" else format(v), format(plan[[nm]]))) }
  chk("max_mismatch_frac", function(v) v > plan$max_mismatch_frac); chk("max_reverify_frac", function(v) v > plan$max_reverify_frac)
  chk("reverify_fraction", function(v) v < plan$reverify_fraction); chk("reverify_seed", function(v) v != plan$reverify_seed)
  if (length(dev) && (is.null(disclosure) || !nzchar(trimws(disclosure)))) stop("extraction ran with settings looser than the plan default and no --disclose-looser-threshold was given: ", paste(dev, collapse = "; "), call. = FALSE)
  dev
}

# ================================================= main =====================================================================================
if (!exists("O1_NO_MAIN")) local({
  argv <- commandArgs(TRUE)
  opt <- function(name, default = NULL) {
    i <- match(paste0("--", name), argv)
    if (is.na(i) || i == length(argv) || startsWith(argv[i + 1L], "--")) { if (is.null(default)) stop(sprintf("--%s is required", name), call. = FALSE); return(default) }
    argv[i + 1L]
  }
  known <- c("--repo-root", "--o1-dir", "--outdir", "--high-ge", "--low-le", "--stable-tol", "--disclose-looser-threshold")
  if (length(bad <- setdiff(argv[startsWith(argv, "--")], known))) stop("unknown argument(s): ", paste(bad, collapse = " "), call. = FALSE)
  main_root <- normalizePath(opt("repo-root"), mustWork = TRUE)
  lex_clean <- function(p) {
    p <- path.expand(p); if (!startsWith(p, "/")) p <- file.path(getwd(), p)
    parts <- strsplit(p, "/+")[[1]]; out <- character()
    for (q in parts[nzchar(parts)]) if (q == "..") out <- head(out, -1L) else if (q != ".") out <- c(out, q)
    paste0("/", paste(out, collapse = "/"))
  }
  out_guard <- function(p) if (grepl("(^|/)out/recovery(/|$)", p) || (startsWith(p, file.path(main_root, "out")) && !startsWith(p, file.path(main_root, "out", "exploratory", "")))) stop("refusing to write under out/ except out/exploratory/<dir> (never into out/recovery/)", call. = FALSE)
  outdir <- lex_clean(opt("outdir")); out_guard(outdir)
  if (dir.exists(outdir) && length(list.files(outdir, all.files = TRUE, no.. = TRUE))) stop("outdir must be new or empty: ", outdir, call. = FALSE)
  local({ a <- outdir; rest <- character(); while (!file.exists(a) && a != "/") { rest <- c(basename(a), rest); a <- dirname(a) }
          out_guard(do.call(file.path, as.list(c(normalizePath(a, mustWork = TRUE), rest)))) })
  o1_dir <- normalizePath(opt("o1-dir"), mustWork = TRUE)
  dir.create(outdir, recursive = TRUE, showWarnings = FALSE); outdir <- normalizePath(outdir, mustWork = TRUE); out_guard(outdir)
  params_o1 <- O1_DEFAULTS
  params_o1$high_ge <- as.numeric(opt("high-ge", params_o1$high_ge)); params_o1$low_le <- as.numeric(opt("low-le", params_o1$low_le))
  params_o1$stable_tol <- as.numeric(opt("stable-tol", params_o1$stable_tol))

  script_root <- local({ a <- grep("^--file=", commandArgs(FALSE), value = TRUE); normalizePath(file.path(dirname(sub("^--file=", "", a[1])), "..")) })
  setwd(script_root)
  for (f in file.path(script_root, "R", paste0(c("utils", "decide", "cem", "estimate", "pre", "presign", "drdid", "post"), ".R"))) source(f, local = FALSE)
  stopifnot(requireNamespace("jsonlite", quietly = TRUE)); library(jsonlite)
  fixest::setFixest_nthreads(1L); data.table::setDTthreads(1L)
  wcsv <- function(dt, f) fwrite(cbind(data.table(banner = BANNER), dt), file.path(outdir, f))

  # ---- extraction output must be complete and untampered
  man <- readLines(file.path(o1_dir, "MANIFEST.sha256")); mh <- sub("  .*$", "", man); mn <- sub("^[0-9a-f]+  ", "", man)
  o1_check_manifest(mn)                       # o1_panel.csv, o1_pin_balances.csv and o1_summary.json must be listed (hence hash-checked below)
  for (i in seq_along(man)) if (!identical(sha256_file(file.path(o1_dir, mn[i])), mh[i])) stop("o1 extraction file differs from its MANIFEST: ", mn[i], call. = FALSE)
  o1sum <- fromJSON(file.path(o1_dir, "o1_summary.json"), simplifyVector = FALSE)
  if (isTRUE(o1sum$aborted)) stop("o1 extraction was aborted; no estimation", call. = FALSE)
  setting_dev <- o1_check_extract_settings(o1sum$params, opt("disclose-looser-threshold", ""))
  if (length(setting_dev)) cat("DISCLOSED looser extraction settings:", paste(setting_dev, collapse = "; "), "\n")

  # ---- signed inputs (as o3_pretrend.R)
  REC <- file.path(main_root, "out", "recovery")
  inp_paths <- c(pre_covariates = file.path(REC, "main/gen-M/covariates.csv"), pre_panel = file.path(REC, "main/gen-M/wallet_coin_day_pre.csv"),
                 post_panel = file.path(REC, "post/gen-M-post/post_wallet_coin_day.csv"), match_membership = file.path(REC, "presign/main-gen-M-20261027/match_membership.csv"),
                 analysis_parameters = file.path(REC, "presign/main-gen-M-20261027/analysis_parameters.json"),
                 eligible_frame_coverage = file.path(REC, "presign/main-gen-M-20261027/eligible_frame_coverage.csv"))
  SIGNED <- file.path(REC, "post/r-gen-M-post")
  trace <- fromJSON(file.path(SIGNED, "post_trace.json"), simplifyVector = FALSE)
  o1_check_input_hashes(o1sum$params$input_hashes, trace$input_sha256)   # membership / covariates / pre panel the walk used == the signed inputs
  hash_tab <- rbindlist(lapply(names(inp_paths), function(k) data.table(input = k, sha256 = sha256_file(inp_paths[[k]]), signed_sha256 = trace$input_sha256[[k]])))
  hash_tab[, ok := sha256 == signed_sha256]; if (!all(hash_tab$ok)) stop("input sha256 differs from post_trace.json input_sha256: stop", call. = FALSE)
  wcsv(hash_tab, "o1_input_hashes.csv")
  params <- po_read_params(inp_paths[["analysis_parameters"]]); po_check_population(params$population_id, params$generation_id)
  mem <- po_read_membership(inp_paths[["match_membership"]], params)
  pre <- po_pre_stage(dirname(inp_paths[["pre_covariates"]]), params, mem, inp_paths[["eligible_frame_coverage"]])
  post <- po_read_post_panel(inp_paths[["post_panel"]], params, required_addresses = pre$need_addr, exact_addresses = pre$retained)
  wide_signed <- rbind(pre$wide_pre[uid %in% pre$need_addr], post$wide)
  # O1 extension: negative day_index rows of the wallets with a complete, identity-passing walk
  op <- fread(file.path(o1_dir, "o1_panel.csv"), select = c("address", "coin", "day_index", "balance_units"), colClasses = c(address = "character", coin = "character", day_index = "integer", balance_units = "character"))
  op[, bal := as.numeric(balance_units)]
  ow <- dcast(op[, .(uid = address, day_index, coin, bal)], uid + day_index ~ coin, value.var = "bal"); setnames(ow, c("USDC", "USDT"), c("usdc_units", "usdt_units"))
  if (any(ow$day_index >= 0L)) stop("o1 panel carries non-negative day_index rows: refused", call. = FALSE)
  if (length(bad <- setdiff(unique(ow$uid), mem$matched$uid))) stop(length(bad), " o1 panel wallets are not in the signed matched sample", call. = FALSE)
  ok_uid <- unique(ow$uid)
  wide_ext <- rbind(wide_signed, ow[, names(wide_signed), with = FALSE])
  panel <- po_panel(wide_ext, mem$matched)                       # B from days 0..59 only: identical to the signed B
  caps_signed <- list(D = as.numeric(params$cap_values$D), A = as.numeric(params$cap_values$A))
  caps_re <- cap_values_all(panel, POST_EVENT_DAY)
  for (k in c("D", "A")) if (!isTRUE(all.equal(caps_re[[k]], caps_signed[[k]], tolerance = 1e-9))) stop("recomputed ", k, " caps differ from the frozen cap_values", call. = FALSE)

  tp <- fread(file.path(SIGNED, "post_time_path.csv")); pt <- fread(file.path(SIGNED, "post_pretrend.csv"))
  jumps <- lapply(c(D = "D", A = "A"), function(o) { r <- tp[outcome == o & day_index == 61L]; stopifnot(nrow(r) == 1L, r$status == "estimated"); r$coef })
  matched <- mem$matched
  dropped <- o1_dropped_share(matched, ok_uid); wcsv(dropped, "o1_dropped_missing_path.csv")
  wcsv(o1_coverage(panel, ok_uid, matched, caps_signed, params_o1$months), "o1_coverage.csv")

  full <- o1_analyse(panel, ok_uid, caps_signed, jumps, params_o1)
  wcsv(full$paths, "o1_paths.csv")
  for (nm in names(full$fits)) if (is.null(full$fits[[nm]]$error)) saveRDS(list(banner = BANNER, fit = full$fits[[nm]]), file.path(outdir, sprintf("o1_fit_%s.rds", gsub("[|]", "_", nm))))

  # signed comparison: asserted when nobody is dropped (balanced panel, saturated day x arm), reported otherwise
  cmp <- rbindlist(lapply(c("D", "A"), function(o) {
    f <- full$fits[[paste("capped", o, "uid", sep = "|")]]; s <- pt[outcome == o & status == "estimated"]
    if (!is.null(f$error)) return(NULL)
    data.table(outcome = o, day_index = s$day_index, o1_coef = f$b[as.character(s$day_index)], signed_coef = s$coef)[, abs_diff := abs(o1_coef - signed_coef)]
  }))
  wcsv(cmp, "o1_signed_compare.csv")
  if (!any(dropped$n_dropped > 0)) { if (!all(cmp$abs_diff <= 1e-9)) stop("no wallet dropped but the 9 Jun-7 Aug coefficients differ from post_pretrend.csv by > 1e-9", call. = FALSE) }
  else cat(sprintf("wallets dropped (missing O1 path): coefficient differences vs signed reported, max %.3g\n", max(cmp$abs_diff, na.rm = TRUE)))

  # ---- age-restricted secondary row
  pin_block <- o1sum$params$pin
  pb <- fread(file.path(o1_dir, "o1_pin_balances.csv"), select = c("address", "coin", "balance_units"), colClasses = "character")
  pinw <- dcast(pb[, .(address, coin, v = suppressWarnings(as.numeric(balance_units)))], address ~ coin, value.var = "v"); setnames(pinw, c("USDC", "USDT"), c("usdc", "usdt"))
  cov_fs <- fread(inp_paths[["pre_covariates"]], select = c("address", "first_send_block"), colClasses = c(address = "character", first_send_block = "character"))
  pred <- o1_age_predicate(cov_fs[address %in% matched$uid], pinw, pin_block)
  arm_of <- setNames(fifelse(matched$treat, "exposed", "control"), matched$uid)
  wcsv(o1_admissions(copy(pred), arm_of), "o1_restricted_admissions.csv")
  restricted <- tryCatch({
    fr <- pre$frame[uid %in% pred[admitted == TRUE, address]]
    res <- cem_match_fixed(fr, CEM_VARS, params$cutpoints, "three_bins")
    pan_r <- po_panel(wide_ext[uid %in% res$matched$uid], res$matched)
    ok_r <- intersect(ok_uid, res$matched$uid)
    a <- o1_analyse(pan_r, ok_r, caps_signed, jumps, params_o1)
    list(res = res, analysis = a, ok = ok_r, dropped = o1_dropped_share(matched, res$matched$uid))
  }, error = function(e) list(error = conditionMessage(e)))
  if (!is.null(restricted$error)) {
    cat("restricted row not estimable: ", restricted$error, "\n")
    wcsv(o1_dropped_share(matched, character()), "o1_restricted_dropped_weight.csv")
    wcsv(data.table(status = "restricted row not estimable", reason = restricted$error), "o1_restricted_status.csv")
  } else {
    wcsv(restricted$dropped, "o1_restricted_dropped_weight.csv"); wcsv(restricted$analysis$paths, "o1_restricted_paths.csv")
  }
  # ---- estimands + tie-break
  flat <- o1_flatten(full$estimands, "full")
  if (is.null(restricted$error)) flat <- rbind(flat, o1_flatten(restricted$analysis$estimands, "restricted"))
  wcsv(flat, "o1_estimands.csv")
  tb <- rbindlist(lapply(full$estimands, function(e) {
    if (!identical(e$status, "estimated") || e$variant != "capped") return(NULL)
    r <- if (is.null(restricted$error)) Find(function(x) identical(x$status, "estimated") && x$variant == "capped" && x$outcome == e$outcome && x$cluster == e$cluster, restricted$analysis$estimands) else NULL
    t <- o1_tiebreak(e$est, if (is.null(r)) NULL else r$est)
    data.table(outcome = e$outcome, cluster = e$cluster, e3_band_final = t$e3_band, stable_final = t$stable, reading = t$reading, row = t$row,
               downgraded = t$downgraded, e4_trigger = t$e4_trigger, note = t$note, rw_rates_note = o1_rw_note(e$outcome))
  }))
  wcsv(tb, "o1_tiebreak.csv")
  rl <- o1_reading_lines(tb, fread(file.path(outdir, "o1_coverage.csv"), drop = "banner"))
  writeLines(rl, file.path(outdir, "o1_reading.md")); cat(paste(rl, collapse = "\n"), "\n")
  writeLines(c(BANNER, "", sprintf("O1 estimation. Wallets with an O1 path: %d of %d matched. Parameters: %s", length(ok_uid), nrow(matched), paste(sprintf("%s=%s", names(params_o1)[sapply(params_o1, is.atomic)], sapply(params_o1[sapply(params_o1, is.atomic)], function(x) paste(x, collapse = ","))), collapse = "; ")),
               "Conditioned on later eligibility and later scaling (9 Jun-7 Aug information): a March path is limited by that selection.",
               if (length(setting_dev)) paste0("DISCLOSED looser extraction settings: ", paste(setting_dev, collapse = "; "), ". Disclosure text (--disclose-looser-threshold): ", trimws(opt("disclose-looser-threshold", ""))) else "Extraction settings: plan defaults.",
               "Readings, random-walk rates and coverage: o1_reading.md"), file.path(outdir, "summary.md"))
  fl <- setdiff(list.files(outdir), "summary.md")
  writeLines(sprintf("%s  %s", vapply(file.path(outdir, fl), sha256_file, ""), fl), file.path(outdir, "MANIFEST.sha256"))
  cat("done:", outdir, "\n")
})
