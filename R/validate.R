# In-lane validations (SPEC "Deferred": a, b, c).

# (a) Saturated cell-mean identity: with a balanced panel and wallet-constant weights, the
# weighted TWFE beta equals the weighted 2x2 DiD of group x period cell means.
cellmean_did <- function(panel, event_day, outcome = "D", cap = "none") {
  d <- prep_est(panel, event_day, cap)
  d[, post := day_index > event_day]
  d[, y := get(outcome)]
  wm <- d[, .(m = mean(y)), by = .(uid, treat, post, w)][
    , .(cm = sum(w * m) / sum(w)), by = .(treat, post)]
  g <- function(t, p) wm[treat == t & post == p, cm]
  (g(TRUE, TRUE) - g(TRUE, FALSE)) - (g(FALSE, TRUE) - g(FALSE, FALSE))
}

# (b) Hand-coded weighted alternating projections (wallet, day) + lm() on residualized columns.
altproj_beta <- function(panel, event_day, outcome = "D", cap = "none", tol = 1e-13, maxit = 500) {
  d <- prep_est(panel, event_day, cap)
  y <- d[[outcome]]; x <- d$tp; w <- d$w; u <- d$uid; t <- d$day_index
  demean <- function(v) {
    for (it in seq_len(maxit)) {
      v0 <- v
      v <- v - ave_w(v, u, w)
      v <- v - ave_w(v, t, w)
      if (max(abs(v - v0)) < tol) break
    }
    v
  }
  yt <- demean(y); xt <- demean(x)
  unname(coef(lm(yt ~ 0 + xt, weights = w))["xt"])
}
ave_w <- function(v, g, w) {
  s <- rowsum(v * w, g); sw <- rowsum(w, g)
  (s / sw)[match(g, rownames(s))]
}

# (c) Wallet-level randomization inference: permute the (treat, w) assignment across wallets,
# recompute beta from wallet-level changes (identical to the TWFE beta by identity (a)).
ri_test <- function(panel, event_day, outcome = "D", cap = "none", draws = 1000L, seed = 1L) {
  wd <- wallet_deltas(panel, event_day, outcome, cap)
  beta_fn <- function(tr, w, dl) {
    weighted.mean(dl[tr], w[tr]) - weighted.mean(dl[!tr], w[!tr])
  }
  obs <- beta_fn(wd$treat, wd$w, wd$delta)
  set.seed(seed)
  n <- nrow(wd)
  sims <- vapply(seq_len(draws), function(i) {
    p <- sample.int(n)
    beta_fn(wd$treat[p], wd$w[p], wd$delta)
  }, numeric(1))
  list(beta = obs, ri_sd = sd(sims), ri_p = (1 + sum(abs(sims) >= abs(obs))) / (draws + 1), draws = draws)
}

run_validations <- function(panel, event_day, cap = "none", draws = 1000L, seed = 1L, verbose = TRUE) {
  e <- estimate_did(panel, event_day, "D", "uid", cap)
  a <- cellmean_did(panel, event_day, "D", cap)
  b <- altproj_beta(panel, event_day, "D", cap)
  ri <- ri_test(panel, event_day, "D", cap, draws, seed)
  out <- data.table(
    check = c("cell-mean identity |feols - DiD|", "alt-projections + lm |feols - lm|",
              "RI beta equals feols beta", "RI SD / wallet-cluster SE (info)", "RI p-value (info)"),
    value = c(abs(e$beta - a), abs(e$beta - b), abs(ri$beta - e$beta), ri$ri_sd / e$se, ri$ri_p),
    tol = c(1e-10, 1e-9, 1e-10, NA, NA))
  out[, pass := ifelse(is.na(tol), NA, value <= tol)]
  if (verbose) { cat("Validations:\n"); print(out, digits = 6) }
  list(table = out, ok = all(out$pass, na.rm = TRUE), beta = e$beta, ri = ri)
}
