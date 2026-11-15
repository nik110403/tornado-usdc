# EXPLORATORY, POST-RESULTS, dated 2026-11-12; not confirmatory; does not reclassify H1.
# O4 helper functions (pure; no file IO). Sourced by o4_caps.R and tests/test_o4_caps.R AFTER the committed R/ code (needs data.table, fixest, clampv).
# Nothing in R/ is edited: caps are computed here exactly like cap_values_all (R/estimate.R) but with a probs argument.

# cut-points over ALL baseline wallet-days (panel attr `baseline_days`), both groups pooled, unweighted, type 7
o4_caps <- function(panel, event_day, probs) {
  bd <- attr(panel, "baseline_days")
  b <- if (is.null(bd)) panel[day_index < event_day] else panel[day_index %in% bd]
  q <- function(x) quantile(x, probs, type = 7, names = FALSE)
  list(D = q(b$D), A = q(b$A), legs = q(c(b$usdc_B, b$usdt_B)), n_wallet_days = nrow(b))
}

# estimation frame: prep_est(panel, event_day, "none") (uncapped) + B; capped columns for one cap pair
#   adds cu, ct (clamped legs at the pooled pair), Dleg = cu - ct, Aleg = cu + ct, Down/Aown (own-rule clamps)
o4_capped <- function(d0, caps) {
  d <- copy(d0)
  d[, `:=`(cu = clampv(usdc_B, caps$legs), ct = clampv(usdt_B, caps$legs), Down = clampv(D, caps$D), Aown = clampv(A, caps$A))]
  d[, `:=`(Dleg = cu - ct, Aleg = cu + ct)]
  d
}

# weighted TWFE DiD (spec of R/estimate.R estimate_did) of column y on tp | uid + day_index, optional day subset;
# one fit, SEs under ~uid and ~stratum; CIs t with df = clusters - 1
o4_fit <- function(d, y, days = NULL) {
  if (!is.null(days)) d <- d[day_index %in% days]
  fit <- feols(as.formula(sprintf("%s ~ tp | uid + day_index", y)), data = d, weights = d$w, cluster = ~uid,
               fixef.tol = 1e-11, fixef.iter = 10000, notes = FALSE)
  rbindlist(lapply(c("uid", "stratum"), function(cl) {
    se <- unname(se(fit, vcov = as.formula(paste0("~", cl)))["tp"]); b <- unname(coef(fit)["tp"])
    G <- uniqueN(d[[cl]]); tc <- qt(0.975, G - 1)
    data.table(cluster = cl, beta = b, se = se, lo = b - tc * se, hi = b + tc * se, df = G - 1, p = 2 * pt(-abs(b / se), G - 1), n_obs = nobs(fit), n_clusters = G)
  }))
}

# per-wallet contribution to the DiD beta (balanced panel, wallet-constant weights; exact identity):
#   treated: w*delta/sum(w treated); controls: -w*delta/sum(w controls); delta = post mean - pre mean of y (event day already excluded)
o4_contrib <- function(d, y, event_day) {
  d <- copy(d); d[, yy := get(y)]
  x <- d[, .(m1 = mean(yy[day_index > event_day]), m0 = mean(yy[day_index < event_day])), by = .(uid, treat, w, stratum)]
  x[, delta := m1 - m0]
  Wt <- x[treat == TRUE, sum(w)]; Wc <- x[treat == FALSE, sum(w)]
  x[, contribution := fifelse(treat, w * delta / Wt, -w * delta / Wc)]
  x[, c("m1", "m0") := NULL]
  x[]
}

# the out/ write guard (same rule as o3_pretrend.R): never out/recovery; under <root>/out only out/exploratory/<dir>
o4_out_guard <- function(p, root) {
  if (grepl("(^|/)out/recovery(/|$)", p) || (startsWith(p, file.path(root, "out")) && !startsWith(p, file.path(root, "out", "exploratory", ""))))
    stop("refusing to write under out/ except out/exploratory/<dir> (never into out/recovery/)", call. = FALSE)
  invisible(TRUE)
}

# in-time rank: coefficients b (named by day) -> 60/35 contrast for fake date s (fake day dropped)
o4_contrast <- function(b, s, nb = 60L, na = 35L) mean(b[as.character((s + 1L):(s + na))]) - mean(b[as.character((s - nb):(s - 1L))])
