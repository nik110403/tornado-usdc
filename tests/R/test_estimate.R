options(tusdc.synthetic = TRUE)
r <- synth_one(777, beta = -0.15, n_exposed = 400, outcomes = c("D", "A"), cluster_stratum = TRUE)
ev <- r$event_day
test("base units converted inside R only; B_i recomputed exactly", {
  stopifnot(nrow(r$panel[day_index == ev]) > 0)
  w <- data.table(uid = c(1L, 1L, 1L), day_index = 0:2, usdc_units = c(3e6, 5e6, 9e6), usdt_units = c(1e6, 1e6, 1e6))
  m <- data.table(uid = 1L, treat = TRUE, stratum = 1L, w = 1)
  p <- build_panel(w, m, 0:1)               # B = mean(4,6) = 5 dollars
  stopifnot(all.equal(p$D, c(2, 4, 8) / 5), all.equal(p$A, c(4, 6, 10) / 5))
})
test("event day excluded and Post starts the day after", {
  d <- prep_est(r$panel, ev)
  stopifnot(!(ev %in% d$day_index), all(d[treat == TRUE & day_index > ev, tp] == 1),
            all(d[treat == TRUE & day_index < ev, tp] == 0), all(d[treat == FALSE, tp] == 0))
})
test("own cap: D and A each clamped at their OWN type-7 1/99 over baseline wallet-days, same values every day", {
  cv <- cap_values_all(r$panel, ev); d <- prep_est(r$panel, ev, cap = "own")
  base <- r$panel[day_index %in% attr(r$panel, "baseline_days")]
  stopifnot(cv$D[1] < cv$D[2], cv$A[1] < cv$A[2],
            all.equal(cv$D, unname(quantile(base$D, c(.01, .99), type = 7))), all.equal(cv$A, unname(quantile(base$A, c(.01, .99), type = 7))),
            max(d$D) <= cv$D[2] + 1e-12, min(d$D) >= cv$D[1] - 1e-12, max(d$A) <= cv$A[2] + 1e-12, min(d$A) >= cv$A[1] - 1e-12,
            identical(attr(d, "caps"), cv), cv$n_wallet_days == nrow(base))
  n <- prep_est(r$panel, ev, cap = "none"); stopifnot(isTRUE(all.equal(n$D, r$panel[day_index != ev, D])))
})
test("cell-mean identity vs feols beta, 1e-10 (D, A, capped D)", {
  for (o in c("D", "A")) stopifnot(abs(estimate_did(r$panel, ev, o)$beta - cellmean_did(r$panel, ev, o)) < 1e-10)
  stopifnot(abs(estimate_did(r$panel, ev, "D", cap = "own")$beta - cellmean_did(r$panel, ev, "D", "own")) < 1e-10)
})
test("hand alternating-projections + lm equals feols beta", {
  stopifnot(abs(estimate_did(r$panel, ev, "D")$beta - altproj_beta(r$panel, ev, "D")) < 1e-9)
})
test("wallet-level RI: reproduces beta, deterministic under seed, 1000 draws", {
  a <- ri_test(r$panel, ev, seed = 9); b <- ri_test(r$panel, ev, seed = 9)
  stopifnot(identical(a, b), a$draws == 1000, abs(a$beta - r$D$beta) < 1e-10, a$ri_p > 0, a$ri_p <= 1)
})
test("wallet and stratum clustering both run; same beta", {
  stopifnot(abs(r$D$beta - r$D_stratum$beta) < 1e-10, r$D$n_clusters > r$D_stratum$n_clusters)
})
test("H1 power Phi(0.05/SE - 1.96): prespec reference values", {
  stopifnot(abs(power_normal(0.01916) - 0.742) < 0.001, abs(power_normal(1.1732) - 0.028) < 0.001,
            abs(power_normal(0.05 / 1.96) - 0.5) < 1e-12,                      # SE where 0.05/SE = 1.96 -> Phi(0) = 0.5
            power_normal(0.01) > power_normal(0.02), abs(mde_h1(0.02) - (0.10 + 2.80 * 0.02)) < 1e-12)
  pw <- power_at(r$panel, ev, -0.15); stopifnot(pw$se > 0, pw$power > 0, pw$power <= 1, abs(pw$mde - mde_h1(pw$se)) < 1e-12)
  stopifnot(abs(power_zero_null(0, 0.02) - 0.05) < 1e-12)                     # old zero-null number kept as diagnostic only
})
# Counterexample: wallet holds (0.8, 0.2) * B every baseline day (small noise), post-event both legs x30.
cx <- local({
  set.seed(3); n <- 30L; bd <- 0:9; days <- 0:15
  wide <- CJ(uid = 1:n, day_index = days)
  b <- 1000 * (1 + runif(n))
  wide[, e := ifelse(day_index %in% bd, runif(.N, -0.05, 0.05), 0)]
  wide[, f := ifelse(day_index > 10, 30, 1)]
  wide[, `:=`(usdc_units = round(0.8 * b[uid] * (1 + e) * f * 1e6), usdt_units = round(0.2 * b[uid] * (1 + e) * f * 1e6))]
  m <- data.table(uid = 1:n, treat = TRUE, stratum = 1L, w = 1)
  build_panel(wide[, .(uid, day_index, usdc_units, usdt_units)], m, bd)
})
dD <- function(cap) { wd <- wallet_deltas(cx, 10, "D", cap); weighted.mean(wd$delta, wd$w) }
test("counterexample: leg-pooled cap gives dD ~ -0.6, own cap gives dD > 0, uncapped dD huge positive", {
  stopifnot(dD("leg_pooled") < -0.55, dD("leg_pooled") > -0.65, dD("own") > 0, dD("own") < 0.2, dD("none") > 10)
})
test("leg_pooled: legs capped at one pooled pair over both legs' baseline values; D, A formed from capped legs", {
  d <- prep_est(cx, 10, "leg_pooled"); cv <- cap_values_all(cx, 10)$legs
  base <- cx[day_index %in% 0:9]
  stopifnot(all.equal(cv, unname(quantile(c(base$usdc_B, base$usdt_B), c(.01, .99), type = 7))),
            all(d$D <= 0.6 + 0.2 & d$A <= 2 * cv[2] + 1e-12), all.equal(d$A, clampv(d$usdc_B, cv) + clampv(d$usdt_B, cv)),
            all(d$usdc_B == cx[day_index != 10, usdc_B]))                       # legs themselves are never capped
})
test("clip incidence: weighted share strictly beyond each cap by arm x tail x period", {
  base <- 0:99; post <- 101:110
  pn <- rbind(data.table(uid = 1L, treat = TRUE, w = 1, stratum = 1L, day_index = c(base, post), D = c(base / 99, rep(10, 10))),
              data.table(uid = 2L, treat = FALSE, w = 2, stratum = 1L, day_index = c(base, post), D = c(base / 99, rep(0.5, 10))))
  pn[, A := 1]; pn[, `:=`(usdc_B = 1, usdt_B = 0)]; setattr(pn, "baseline_days", base)
  ci <- clip_incidence(pn, 100)
  g <- function(o, tl, a, p) ci[outcome == o & tail == tl & arm == a & period == p, share]
  stopifnot(abs(g("D", "upper", "exposed", "post") - 1) < 1e-12, abs(g("D", "upper", "exposed", "baseline") - 0.01) < 1e-12,
            g("D", "upper", "control", "post") == 0, abs(g("D", "upper", "control", "baseline") - 0.01) < 1e-12,
            g("D", "lower", "exposed", "baseline") == 0.01, g("A", "upper", "exposed", "post") == 0,
            clip_flag(ci)$cap_sensitive_clip)
})
test("golden-table snapshot (tests/R/golden/estimates.csv)", {
  g <- synth_one(20261016, beta = -0.15, n_exposed = 250, outcomes = c("D", "A"), cluster_stratum = TRUE)
  cur <- data.table(
    metric = c("beta_D", "se_D_wallet", "se_D_stratum", "beta_A", "se_A", "dropped_exposed", "n_controls"),
    value = c(g$D$beta, g$D$se, g$D_stratum$se, g$A$beta, g$A$se, g$match$dropped_exposed, g$match$n_controls_matched))
  gp <- file.path("tests", "R", "golden", "estimates.csv")
  if (Sys.getenv("UPDATE_GOLDEN") == "1" || !file.exists(gp)) { fwrite(cur, gp, scipen = 0); cat("    (golden written)\n") }
  old <- fread(gp)
  stopifnot(identical(old$metric, cur$metric), max(abs(old$value - cur$value)) < 1e-8)
})
options(tusdc.synthetic = FALSE)
