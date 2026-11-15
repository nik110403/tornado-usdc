# Coarsened exact matching, hand-coded (MatchIt is not installed). Prespec §1 / §1a.
# Cut-points: 25/50/75th percentiles (R quantile type 7) among the eligible EXPOSED wallets before matching,
# duplicate cut-points removed, a value equal to a cut-point goes to the HIGHER bin (findInterval).
# NA only allowed for `age` (never-sent wallet) and then forms its own bin (-1); any other NA stops the run.
# Controls: <= `ratio` per exposed, without replacement within stratum (seeded). Weights: Iacus-King-Porro (2012)
# eq. 6 with counts after screening and subsampling: exposed 1; control (m_C/m_T) * (m_T,s / m_C,s).
# Exposed wallets in strata with no control are dropped (weight 0) and counted.

coarsen_q <- function(x, ref, n_bins = 4L, na_ok = FALSE, what = "covariate") {
  p <- seq(0, 1, length.out = n_bins + 1)[-c(1, n_bins + 1)]
  br <- unique(quantile(ref, p, na.rm = TRUE, names = FALSE, type = 7))
  if (anyNA(x) && !na_ok) stop(sprintf("CEM: missing values in '%s' (only never-sent age may be missing); run stopped, no imputation", what), call. = FALSE)
  b <- findInterval(x, br)          # x == cut-point -> higher bin
  b[is.na(x)] <- -1L
  attr(b, "cuts") <- br
  b
}

# USDC-share modes: "none" | "continuous" (exposed-quartile coarsened) | "three_bins" ({0}, (0,1), {1})
# Three bins are classified from the ratio BEFORE any rounding; callers must pass exactly 0 / exactly 1 only when a
# leg sum is zero (pre_match_frame does this).
coarsen_usdc_share <- function(x, ref, mode, n_bins) {
  switch(mode,
    none = NULL,
    continuous = coarsen_q(x, ref, n_bins, na_ok = FALSE, what = "usdc_share"),
    three_bins = ifelse(is.na(x), -1L, ifelse(x <= 0, 0L, ifelse(x >= 1, 2L, 1L))),
    stop("usdc_mode must be none|continuous|three_bins"))
}

# cov: data.table(uid, treat (logical), [address], <vars...>, usdc_share). `vars` are numeric columns to coarsen.
# Ordering/ties: wallets are sorted by `address` (lexicographic, C locale) if present, else by uid, BEFORE the
# random numbers are drawn; within a stratum controls are ranked by (random number, address).
cem_match <- function(cov, vars, usdc_mode = "none", n_bins = 4L, ratio = 3L, seed = SEED_PRIMARY_CEM,
                      age_var = "age") {
  cov <- as.data.table(cov)
  key <- if ("address" %in% names(cov)) "address" else "uid"
  setorderv(cov, key)   # data.table sorts character keys in C locale (lexicographic by byte)
  stopifnot(all(c("uid", "treat", vars) %in% names(cov)), anyDuplicated(cov$uid) == 0)
  ex <- cov$treat
  keys <- lapply(vars, function(v) coarsen_q(cov[[v]], cov[[v]][ex], 4L, na_ok = v == age_var, what = v))
  names(keys) <- vars
  cuts <- lapply(keys, attr, "cuts")
  us <- if (usdc_mode != "none") coarsen_usdc_share(cov$usdc_share, cov$usdc_share[ex], usdc_mode, n_bins)
  if (!is.null(us)) { keys$usdc_share <- us; if (!is.null(attr(us, "cuts"))) cuts$usdc_share <- attr(us, "cuts") }
  keys <- lapply(keys, as.integer)
  cov[, stratum := do.call(paste, c(unname(keys), sep = "|"))]
  cov[, stratum := match(stratum, unique(stratum))]
  set_rng_kind(); set.seed(seed)
  cov[, rnd := runif(.N)]
  cov[, ord := .I]                                   # position in address order = tie-breaker
  cov[treat == FALSE, rk := frankv(.SD, cols = c("rnd", "ord"), ties.method = "first"), by = stratum, .SDcols = c("rnd", "ord")]
  st <- cov[, .(n_exposed = sum(treat), n_ctrl_avail = sum(!treat)), by = stratum]
  st[, n_ctrl_sel := pmin(n_ctrl_avail, ratio * n_exposed)]
  st[, kept := n_exposed > 0 & n_ctrl_sel > 0]
  m_T <- st[kept == TRUE, sum(n_exposed)]; m_C <- st[kept == TRUE, sum(n_ctrl_sel)]
  st[, ctrl_weight := ifelse(kept, (m_C / m_T) * (n_exposed / n_ctrl_sel), 0)]   # IKP eq. 6
  cov <- merge(cov, st[, .(stratum, n_ctrl_sel, kept, ctrl_weight)], by = "stratum", sort = FALSE)
  m <- cov[kept == TRUE & (treat == TRUE | rk <= n_ctrl_sel),
           .(uid, treat, stratum, w = fifelse(treat, 1, ctrl_weight))]
  setorder(m, uid)
  list(matched = m,
       n_exposed = sum(ex),
       dropped_exposed = sum(st[kept == FALSE, n_exposed]),
       n_controls_matched = sum(!m$treat),
       strata = st[kept == TRUE][order(stratum)],
       n_strata_kept = st[, sum(kept)],
       cutpoints = cuts, m_T = m_T, m_C = m_C,
       usdc_mode = usdc_mode, n_bins = n_bins, ratio = ratio, seed = seed)
}

cem_report <- function(res) {
  st <- res$strata
  cat(sprintf("CEM [usdc_mode=%s, bins=%d, ratio<=%d:1, seed=%d]\n",
              res$usdc_mode, res$n_bins, res$ratio, res$seed))
  cat(sprintf("  exposed %d | dropped (no control in stratum) %d | matched exposed %d | matched controls %d\n",
              res$n_exposed, res$dropped_exposed, res$n_exposed - res$dropped_exposed, res$n_controls_matched))
  cat(sprintf("  strata kept %d | exposed/stratum: median %.0f, max %d | controls selected/stratum: median %.0f, max %d\n",
              nrow(st), median(st$n_exposed), max(st$n_exposed), median(st$n_ctrl_sel), max(st$n_ctrl_sel)))
  cat(sprintf("  control weights: min %.3f, median %.3f, max %.3f | effective control n %.0f\n",
              min(st$ctrl_weight), median(st$ctrl_weight), max(st$ctrl_weight),
              { w <- res$matched[treat == FALSE, w]; sum(w)^2 / sum(w^2) }))
}
