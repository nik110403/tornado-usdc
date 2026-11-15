# Doubly robust DiD robustness row (prespec §5 item 5; Sant'Anna & Zhao 2020 improved panel estimator). S19, ADDITIVE.
# The implementation is the R package DRDID (CRAN, GPL-3), function drdid_imp_panel(); nothing is hand-rolled and no other
# estimator is ever substituted: a missing / different package, a failed IPT fit or a non-finite result is reported as
# status "not estimated" with the reason, and the model is not changed.
#
# API confirmed from the installed package's own help (?DRDID::drdid_imp_panel, DRDID 1.4.0):
#   drdid_imp_panel(y1, y0, D, covariates, i.weights = NULL, boot = FALSE, boot.type = "weighted", nboot = NULL,
#                   inffunc = FALSE, trim.level = 0.995)
#   * y1/y0: post / pre outcome (n x 1); D: 1 = treated; covariates: n x k matrix, intercept must be supplied by the caller;
#   * propensity score by inverse probability tilting (IPT, Graham-Pinto-Pinto 2012), outcome model by weighted OLS;
#   * boot = FALSE -> analytic influence-function SE, se = sd(IF) * sqrt(n - 1) / n, CI = ATT -/+ 1.96 se;
#   * trim.level: native trimming. CONTROLS (D == 0) with fitted propensity >= trim.level are dropped from the estimator's
#     numerator (treated units are never trimmed by the package); the IPT fit itself uses every unit;
#   * a non-converged / unverified IPT fit STOPS with an error (no logit-MLE fallback), caught here and reported.
# Frozen values below are recorded in post_analysis_parameters.json before any real post file is read.

DRDID_FREEZE <- list(
  package = "DRDID", version = "1.4.0", license = "GPL-3", function_name = "drdid_imp_panel",
  formals = c("y1", "y0", "D", "covariates", "i.weights", "boot", "boot.type", "nboot", "inffunc", "trim.level"),
  arguments = list(i.weights = "NULL (unweighted: the estimator targets the screened exposed-wallet population, not the CEM-weighted set)",
                   boot = FALSE, boot.type = "weighted (unused: boot = FALSE)", nboot = "NULL (unused)", inffunc = FALSE,
                   trim.level = 0.99),
  trim_level = 0.99,
  trimming = paste("package-native, trim.level = 0.99 (matches prespec §5 item 5: 'control wallets with an estimated propensity of 0.99 or more are left out of the",
                   "estimate but stay in the fitted propensity and outcome models; exposed wallets are never trimmed'). Precisely: ONLY controls (D == 0) with IPT propensity >= 0.99 are removed from the ATT numerator and influence",
                   "function. Trimmed controls REMAIN in the IPT propensity fit and in the weighted-OLS outcome regression (weights pscore/(1-pscore)). Exposed wallets are",
                   "never trimmed by the package; the number of exposed wallets with propensity >= 0.99 is counted and reported (n_exposed_above_trim_untrimmed).",
                   "Counts use the package's own propensity fit (DRDID:::pscore.cal, diagnostic only, not an estimator)."),
  inference = "analytic influence-function SE (boot = FALSE); 95% CI = ATT -/+ 1.96 SE (package-computed lci/uci)",
  outcome_model = "linear (weighted OLS, package-internal), propensity model: IPT (package-internal)",
  covariates = c("intercept", "log_B", "usdc_share", "log1p_transfer_count", "log1p_eth_balance_eth", "log1p_age_blocks", "never_sent"),
  transforms = paste("log_B = log(B), B = wallet mean combined USDC+USDT dollars over the 60 baseline days; usdc_share = ratio of baseline sums",
                     "USDC/(USDC+USDT); log1p_transfer_count = log(1 + transfer_count_total); log1p_eth_balance_eth = log(1 + wei/1e18);",
                     "log1p_age_blocks = log(1 + wallet_age_blocks) with age 0 for never-sent wallets; never_sent = 1 iff wallet_age_blocks is",
                     "missing. Exchange share omitted. A covariate column that is constant on the sample makes the IPT fit fail and the row",
                     "is reported 'not estimated' (the specification is never altered)."),
  outcome = paste("y0 = wallet mean over baseline days 0..59 of the §2 H1 outcome D_it = (USDC - USDT)/B; y1 = wallet mean over post days",
                  "9 Aug..12 Sep (35 days); primary row = D capped at the frozen main caps (analysis_parameters cap_values$D), second row = uncapped D."),
  population = "screened, floor-passing exposed and frame wallets BEFORE matching (eligible_frame_coverage screen_status == retained)",
  failure_rule = "error / non-convergence / non-finite ATT or SE / package or version mismatch => status 'not estimated' with reason; no model switch")

DRDID_BASE_DAYS <- 0:59
DRDID_POST_DAYS <- 61:95                       # 9 Aug .. 12 Sep (8 Aug = day 60 excluded)

# TRUE iff the installed package matches the frozen record (package, version, function present, formals identical).
drdid_check_installed <- function(freeze = DRDID_FREEZE) {
  if (!requireNamespace(freeze$package, quietly = TRUE))
    return(list(ok = FALSE, reason = sprintf("package %s is not installed", freeze$package), installed_version = NA_character_))
  v <- as.character(utils::packageVersion(freeze$package))
  if (!identical(v, freeze$version))
    return(list(ok = FALSE, reason = sprintf("installed %s %s differs from frozen %s", freeze$package, v, freeze$version), installed_version = v))
  fn <- tryCatch(getExportedValue(freeze$package, freeze$function_name), error = function(e) NULL)
  if (!is.function(fn)) return(list(ok = FALSE, reason = sprintf("function %s not exported", freeze$function_name), installed_version = v))
  if (!identical(names(formals(fn)), freeze$formals)) return(list(ok = FALSE, reason = "function signature differs from the frozen one", installed_version = v))
  if (!identical(formals(fn)$trim.level, 0.995) || !isFALSE(formals(fn)$boot)) return(list(ok = FALSE, reason = "function defaults differ from the frozen ones", installed_version = v))
  list(ok = TRUE, reason = NA_character_, installed_version = v)
}

drdid_freeze_record <- function(freeze = DRDID_FREEZE) {
  chk <- drdid_check_installed(freeze)
  c(freeze, list(installed = chk$ok, installed_version = chk$installed_version, install_check = if (chk$ok) "ok" else chk$reason,
                 function_source_sha256 = if (chk$ok) {
                   f <- tempfile("drsrc"); on.exit(unlink(f))
                   writeLines(deparse(getExportedValue(freeze$package, freeze$function_name)), f); sha256_file(f)
                 } else NA_character_))
}

# fr: data.table(address, treat, B, usdc_share, transfers, eth_wei, age (NA = never sent), y0_cap, y1_cap, y0_unc, y1_unc)
drdid_covariates <- function(fr) {
  X <- cbind(intercept = 1, log_B = log(fr$B), usdc_share = fr$usdc_share, log1p_transfer_count = log1p(fr$transfers),
             log1p_eth_balance_eth = log1p(fr$eth_wei / 1e18), log1p_age_blocks = log1p(ifelse(is.na(fr$age), 0, fr$age)),
             never_sent = as.numeric(is.na(fr$age)))
  X
}

drdid_row <- function(label, status, reason, fr, extra = list()) {
  r <- data.table(row = label, status = status, reason = reason, att = NA_real_, se = NA_real_, lo = NA_real_, hi = NA_real_,
             decision_case = NA_integer_, n_exposed = sum(fr$treat), n_control = sum(!fr$treat), n_controls_trimmed = NA_integer_,
             n_exposed_above_trim_untrimmed = NA_integer_, ps_flag = NA_integer_, ipt_secondary_algorithm = NA, package = DRDID_FREEZE$package, version = NA_character_,
             fn = DRDID_FREEZE$function_name, trim_level = DRDID_FREEZE$trim_level)
  for (k in names(extra)) set(r, j = k, value = extra[[k]])
  r[]
}

# Trimmed-wallet counts from the package's own propensity fit. Warnings (e.g. the secondary-IPT fallback) are muffled, never abort the count.
drdid_trim_counts <- function(D, X, level, ps_fn = DRDID:::pscore.cal) {
  na <- c(ctrl = NA_integer_, exp = NA_integer_)
  tryCatch(withCallingHandlers({
    ps <- pmin(ps_fn(D, X, i.weights = rep(1, length(D)), n = length(D))$pscore, 1 - 1e-06)
    c(ctrl = sum(D == 0 & ps >= level), exp = sum(D == 1 & ps >= level))
  }, warning = function(w) invokeRestart("muffleWarning")), error = function(e) na)
}

# One DRDID row. outcome: "capped_D" | "uncapped_D".
drdid_estimate <- function(fr, outcome = c("capped_D", "uncapped_D"), freeze = DRDID_FREEZE) {
  outcome <- match.arg(outcome)
  if (!is.data.table(fr) || !nrow(fr) || !any(fr$treat) || !any(!fr$treat))
    return(drdid_row(outcome, "not estimated", "frame lacks exposed or control wallets", data.table(treat = logical(0))))
  chk <- drdid_check_installed(freeze)
  if (!chk$ok) return(drdid_row(outcome, "not estimated", chk$reason, fr))
  y0 <- fr[[if (outcome == "capped_D") "y0_cap" else "y0_unc"]]; y1 <- fr[[if (outcome == "capped_D") "y1_cap" else "y1_unc"]]
  X <- drdid_covariates(fr); D <- as.numeric(fr$treat)
  if (!all(is.finite(X)) || !all(is.finite(y0)) || !all(is.finite(y1)))
    return(drdid_row(outcome, "not estimated", "non-finite covariate or outcome value (e.g. B <= 0)", fr))
  fn <- getExportedValue(freeze$package, freeze$function_name)
  warns <- character(0)
  fit <- tryCatch(withCallingHandlers(
    fn(y1 = y1, y0 = y0, D = D, covariates = X, i.weights = NULL, boot = FALSE, inffunc = FALSE, trim.level = freeze$trim_level),
    warning = function(w) { warns <<- c(warns, conditionMessage(w)); invokeRestart("muffleWarning") }), error = function(e) e)
  if (inherits(fit, "error")) return(drdid_row(outcome, "not estimated", paste("estimator error:", conditionMessage(fit)), fr,
                                               list(version = chk$installed_version)))
  est <- c(fit$ATT, fit$se, fit$lci, fit$uci)
  if (length(est) != 4L || !all(is.finite(est)) || est[2] < 0 || est[3] > est[4])
    return(drdid_row(outcome, "not estimated", "non-finite or invalid ATT / SE / interval returned", fr, list(version = chk$installed_version)))
  # diagnostic only: the package's own propensity fit, to COUNT trimmed wallets (not an estimator)
  tr <- drdid_trim_counts(D, X, freeze$trim_level)
  r <- drdid_row(outcome, "estimated", if (length(warns)) paste("package warnings:", paste(unique(warns), collapse = " | ")) else NA_character_, fr,
                 list(version = chk$installed_version))
  r[, `:=`(att = est[1], se = est[2], lo = est[3], hi = est[4], decision_case = classify_ci(est[3], est[4], DELTA_D)$case,
           n_controls_trimmed = as.integer(tr[["ctrl"]]), n_exposed_above_trim_untrimmed = as.integer(tr[["exp"]]),
           ps_flag = as.integer(fit$ps.flag), ipt_secondary_algorithm = isTRUE(as.integer(fit$ps.flag) == 1L))]
  r[]
}

# Eligible-frame (pre-match) per-wallet collapse. wide: data.table(uid = address, day_index, usdc_units, usdt_units) over days 0..59 and
# 61..95; cov: address, arm, transfer_count_total, eth_balance_wei_units, wallet_age_blocks; addresses: eligible set; capD: frozen D caps.
# Wallets with B <= 0 cannot form D: they are dropped and counted (attr "n_dropped_B").
drdid_frame <- function(wide, cov, addresses, capD) {
  w <- wide[uid %in% addresses & (day_index %in% DRDID_BASE_DAYS | day_index %in% DRDID_POST_DAYS)]
  cnt <- w[, .N, by = uid]
  if (length(setdiff(addresses, w$uid)) || any(cnt$N != length(DRDID_BASE_DAYS) + length(DRDID_POST_DAYS)))
    stop("drdid_frame: eligible wallets lack baseline or post days", call. = FALSE)
  b <- w[day_index %in% DRDID_BASE_DAYS, .(B = mean(usdc_units + usdt_units) / UNITS_PER_DOLLAR, us = sum(usdc_units), ut = sum(usdt_units)), by = uid]
  b[, usdc_share := usdc_share_ratio(us, ut)]
  w <- merge(w, b[, .(uid, B)], by = "uid")
  n_drop <- sum(b$B <= 0)
  w <- w[B > 0]
  w[, D := (usdc_units - usdt_units) / UNITS_PER_DOLLAR / B]
  w[, Dc := clampv(D, capD)]
  a <- w[, .(y0_unc = mean(D[day_index %in% DRDID_BASE_DAYS]), y1_unc = mean(D[day_index %in% DRDID_POST_DAYS]),
             y0_cap = mean(Dc[day_index %in% DRDID_BASE_DAYS]), y1_cap = mean(Dc[day_index %in% DRDID_POST_DAYS]), B = B[1]), by = uid]
  c2 <- as.data.table(cov)[, .(uid = address, treat = arm != "control", transfers = as.numeric(transfer_count_total),
                               eth_wei = as.numeric(eth_balance_wei_units), age = as.numeric(wallet_age_blocks))]
  fr <- merge(merge(a, b[, .(uid, usdc_share)], by = "uid"), c2, by = "uid")
  setnames(fr, "uid", "address"); setorderv(fr, "address")
  setattr(fr, "n_dropped_B", n_drop)
  fr[]
}
