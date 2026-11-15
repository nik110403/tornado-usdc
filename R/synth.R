# Synthetic wallet x day panel with a known beta, resembling the real design:
# 60 pre days (0..59), event day 60 (excluded), 35 post days (61..95); ~4,500 exposed, 3:1 controls,
# wallet ICC ~0.39, SD of wallet-level change ~0.97 (numbers: out/g3/summary.md).
SYN <- list(n_pre = 60L, n_post = 35L, n_exposed = 4500L, frame_mult = 5L,
            icc = 0.39, sd_delta = 0.97)

# Candidate frame covariates; controls drawn from a shifted distribution so strata differ in size.
simulate_covariates <- function(n_exposed = SYN$n_exposed, frame_mult = SYN$frame_mult) {
  n_c <- n_exposed * frame_mult
  mk <- function(n, mu, treat, off) data.table(
    treat = treat,
    log_hold = rnorm(n, mu, 1.6),
    transfers = rpois(n, exp(rnorm(n, 2.0 + off, 0.9))),
    age = ifelse(runif(n) < 0.1, NA_real_, 1.2e7 - rexp(n, 1 / (3e6 + off * 1e6))),
    eth = exp(rnorm(n, 0.5 + off, 1.8)),
    usdc_share = {u <- runif(n); ifelse(u < .2, 0, ifelse(u < .4, 1, rbeta(n, 1.5, 1.5)))})
  cov <- rbind(mk(n_exposed, 9.0, TRUE, 0), mk(n_c, 8.6, FALSE, -0.3))
  cov[, uid := .I]
  cov[]
}

# Outcome-space DGP: D = a_i + g_t + beta*treat*post + e ; A = 1 + e_A (+ beta_A*treat*post), baseline mean of A = 1
# exactly, so B_i (rebuilt from integer unit balances) equals the simulated B. Balances are NOT constrained
# to be non-negative (outcome-space simulation); the estimator does not care.
simulate_wide <- function(matched, cov, beta = -0.15, beta_a = 0, icc = SYN$icc,
                          sd_delta = SYN$sd_delta, n_pre = SYN$n_pre, n_post = SYN$n_post) {
  s2_e <- sd_delta^2 / (1 / n_pre + 1 / n_post)       # Var(delta_i) = s2_e (1/n_pre + 1/n_post)
  s2_a <- s2_e * icc / (1 - icc)
  days <- 0:(n_pre + n_post)                          # event day = n_pre
  nd <- length(days); n <- nrow(matched)
  post <- days > n_pre
  tr <- matched$treat
  gam <- rnorm(nd, 0, 0.3)
  a <- rnorm(n, 0, sqrt(s2_a))
  D <- outer(a, gam, `+`) + matrix(rnorm(n * nd, 0, sqrt(s2_e)), n, nd)
  D <- D + outer(as.numeric(tr), as.numeric(post)) * beta
  eA <- matrix(rnorm(n * nd, 0, 0.3), n, nd)
  eA <- eA - rowMeans(eA[, days < n_pre, drop = FALSE])
  A <- 1 + eA + outer(as.numeric(tr), as.numeric(post)) * beta_a
  B <- exp(cov[match(matched$uid, uid), log_hold])
  usdc <- round(B * (A + D) / 2 * UNITS_PER_DOLLAR)
  usdt <- round(B * (A - D) / 2 * UNITS_PER_DOLLAR)
  data.table(uid = rep(matched$uid, times = nd), day_index = rep(days, each = n),
             usdc_units = as.vector(usdc), usdt_units = as.vector(usdt))
}

synth_one <- function(seed, beta = -0.15, n_exposed = SYN$n_exposed, usdc_mode = "three_bins",
                      outcomes = c("D"), cluster_stratum = FALSE) {
  set.seed(seed)
  cov <- simulate_covariates(n_exposed)
  m <- cem_match(cov, vars = c("log_hold", "transfers", "age", "eth"), usdc_mode = usdc_mode, seed = seed)
  wide <- simulate_wide(m$matched, cov, beta = beta)
  panel <- build_panel(wide, m$matched, baseline_days = 0:(SYN$n_pre - 1L))
  ev <- SYN$n_pre
  res <- list(seed = seed, match = m, panel = panel, event_day = ev)
  res$D <- estimate_did(panel, ev, "D", "uid")
  if ("A" %in% outcomes) res$A <- estimate_did(panel, ev, "A", "uid")
  if (cluster_stratum) res$D_stratum <- estimate_did(panel, ev, "D", "stratum")
  res
}

# Acceptance (SPEC S9, restated 2026-10-15): within-2-SE coverage in [93.5, 97]% over 1,000 seeded sims.
COVERAGE_BAND <- c(0.935, 0.97)
run_synthetic <- function(n_sims = 1000L, beta = -0.15, base_seed = 20261015L) {
  cat(sprintf("== Synthetic stage: %d seeded sims, true beta_D = %.3f (base seed %d) ==\n", n_sims, beta, base_seed))
  # single demonstration run with all outputs + validations
  one <- synth_one(base_seed, beta, outcomes = c("D", "A"), cluster_stratum = TRUE)
  cem_report(one$match)
  for (k in c("D", "A", "D_stratum")) {
    r <- one[[k]]
    cat(sprintf("  [%s | cluster=%s] beta=%.4f se=%.4f CI=[%.4f, %.4f] n=%d clusters=%d\n",
                r$outcome, r$cluster, r$beta, r$se, r$lo, r$hi, r$n_obs, r$n_clusters))
  }
  cat("  decision D (delta=0.10): ", classify_ci(one$D$lo, one$D$hi, DELTA_D)$label, "\n")
  cat("  decision A (delta_A=0.05, H1a rule):", classify_h1a(one$A$lo, one$A$hi, DELTA_A), "\n")
  v <- run_validations(one$panel, one$event_day, seed = base_seed)
  pw <- power_at(one$panel, one$event_day, -0.15)
  cat(sprintf("  H1 power Phi(0.05/SE-1.96) at beta=-0.15: SE=%.4f power=%.4f (MDE delta+2.80*SE = %.4f)\n", pw$se, pw$power, pw$mde))
  rm(one); invisible(gc())

  # coverage loop (D, wallet-clustered SE)
  rows <- vector("list", n_sims)
  for (i in seq_len(n_sims)) {
    r <- synth_one(base_seed + i, beta)
    rows[[i]] <- data.table(seed = base_seed + i, beta = r$D$beta, se = r$D$se, lo = r$D$lo, hi = r$D$hi,
                            dropped = r$match$dropped_exposed)
    rm(r)
    if (i %% 25 == 0) { cat(sprintf("  ... %d/%d sims\n", i, n_sims)); invisible(gc()) }
  }
  s <- rbindlist(rows)
  true_beta <- beta
  s[, `:=`(z = (s$beta - true_beta) / se, within2 = abs(beta - true_beta) <= 2 * se,
           covers95 = lo <= true_beta & true_beta <= hi)]
  s[, case := vapply(seq_len(.N), function(i) classify_ci(lo[i], hi[i], DELTA_D)$case, 1L)]
  k <- sum(s$within2); n <- nrow(s)
  cov_ok <- k / n >= COVERAGE_BAND[1] && k / n <= COVERAGE_BAND[2]
  cat(sprintf("\nCoverage: beta within 2 SE in %d/%d sims = %.1f%% (acceptance [%.1f, %.1f]%%): %s\n",
              k, n, 100 * k / n, 100 * COVERAGE_BAND[1], 100 * COVERAGE_BAND[2], if (cov_ok) "PASS" else "FAIL"))
  cat(sprintf("  95%% CI covers truth: %d/%d | mean beta_hat %.4f (truth %.3f) | SD(beta_hat) %.4f vs mean SE %.4f | z mean %.3f, z sd %.3f\n",
              sum(s$covers95), n, mean(s$beta), true_beta, sd(s$beta), mean(s$se), mean(s$z), sd(s$z)))
  cat(sprintf("  exposed dropped by CEM per sim: mean %.1f\n", mean(s$dropped)))
  cat("  decision-case distribution on sims (delta = 0.10, true beta = ", true_beta, "):\n", sep = "")
  print(s[, .N, by = case][order(case)][, label := DECISION_LABELS[case]][])
  list(sims = s, validations = v, ok = cov_ok && v$ok)
}
