# Outcomes and Eq. (1): weighted TWFE DiD, D_it = beta*(Exposed_i x Post_t) + a_i + g_t + e_it.

# wide: data.table(uid, day_index, usdc_units, usdt_units) -- integer base units (6 dec).
# matched: output of cem_match()$matched (uid, treat, stratum, w).
# baseline_days: day_index values defining B_i (mean combined holdings).
build_panel <- function(wide, matched, baseline_days) {
  p <- merge(as.data.table(wide), matched, by = "uid")
  p[, `:=`(usdc = to_dollars(usdc_units), usdt = to_dollars(usdt_units))]
  B <- p[day_index %in% baseline_days, .(B = mean(usdc + usdt)), by = uid]
  p <- merge(p, B, by = "uid")
  dropped_B <- p[B <= 0, uniqueN(uid)]
  p <- p[B > 0]
  p[, `:=`(D = (usdc - usdt) / B, A = (usdc + usdt) / B, usdc_B = usdc / B, usdt_B = usdt / B)]
  p[, c("usdc_units", "usdt_units", "usdc", "usdt") := NULL]
  setattr(p, "baseline_days", sort(unique(baseline_days)))
  p[]
}

# Cap modes (prespec §2): "own" (primary; D and A each at their OWN type-7 1/99 percentiles), "none",
# "leg_pooled" (USDC/B and USDT/B at ONE pooled pair; D, A formed from the capped legs).
# Cut-points are computed over ALL baseline wallet-days of the final matched sample (panel attr `baseline_days`;
# fallback day_index < event_day), both groups pooled, UNWEIGHTED, R quantile type 7, and applied to every day.
CAP_MODES <- c("own", "none", "leg_pooled")
cap_mode <- function(cap) {                       # legacy logical support: TRUE -> "own", FALSE -> "none"
  if (is.logical(cap)) return(if (isTRUE(cap)) "own" else "none")
  match.arg(cap, CAP_MODES)
}
cap_probs <- c(.01, .99)
cap_values_all <- function(panel, event_day) {
  bd <- attr(panel, "baseline_days")
  b <- if (is.null(bd)) panel[day_index < event_day] else panel[day_index %in% bd]
  q <- function(x) quantile(x, cap_probs, type = 7, names = FALSE)
  list(D = q(b$D), A = q(b$A), legs = q(c(b$usdc_B, b$usdt_B)), n_wallet_days = nrow(b))
}
cap_values <- function(panel, event_day, probs = cap_probs) cap_values_all(panel, event_day)$D   # legacy accessor

clampv <- function(x, c2) pmin(pmax(x, c2[1]), c2[2])

# Prepare estimation sample: drop event day, define treat_post, apply cap mode. Legs (usdc_B, usdt_B) are never capped.
prep_est <- function(panel, event_day, cap = "none") {
  cap <- cap_mode(cap)
  d <- panel[day_index != event_day, .(uid, day_index, treat, w, stratum, D, A, usdc_B, usdt_B)]
  d[, tp := as.numeric(treat & day_index > event_day)]
  caps <- cap_values_all(panel, event_day)
  if (cap == "own") {
    d[, `:=`(D = clampv(D, caps$D), A = clampv(A, caps$A))]
  } else if (cap == "leg_pooled") {
    d[, `:=`(cu = clampv(usdc_B, caps$legs), ct = clampv(usdt_B, caps$legs))]
    d[, `:=`(D = cu - ct, A = cu + ct)][, c("cu", "ct") := NULL]
  }
  setattr(d, "caps", caps); setattr(d, "cap_mode", cap)
  d
}

estimate_did <- function(panel, event_day, outcome = "D", cluster = c("uid", "stratum"), cap = "none") {
  cluster <- match.arg(cluster)
  d <- prep_est(panel, event_day, cap)
  fit <- feols(as.formula(sprintf("%s ~ tp | uid + day_index", outcome)), data = d,
               weights = d$w, cluster = as.formula(paste0("~", cluster)),
               fixef.tol = 1e-11, fixef.iter = 10000, notes = FALSE)
  ci <- as.numeric(unlist(confint(fit, level = 0.95)["tp", ]))
  list(fit = fit, beta = unname(coef(fit)["tp"]), se = unname(se(fit)["tp"]),
       lo = ci[1], hi = ci[2], n_obs = nobs(fit),
       n_clusters = uniqueN(d[[cluster]]), cluster = cluster, outcome = outcome,
       cap = cap_mode(cap), caps = attr(d, "caps"), data = d)
}

# Per-wallet change in outcome (post mean - pre mean, event day excluded).
wallet_deltas <- function(panel, event_day, outcome = "D", cap = "none") {
  d <- prep_est(panel, event_day, cap)
  d[, post := day_index > event_day]
  d[, y := get(outcome)]
  d[, .(m1 = mean(y[post]), m0 = mean(y[!post])), by = .(uid, treat, w, stratum)][
    , delta := m1 - m0][]
}

# H1 planning power (prespec §3 "Power rule"): P(95% CI clears -delta | beta = -0.15) = Phi((|beta| - delta)/SE - 1.96),
# i.e. Phi(0.05/SE - 1.96). SE = larger of wallet- and stratum-clustered SE of the capped H1 outcome; NOT rescaled for
# window length. MDE = delta + 2.80*SE.
power_normal <- function(se, beta = -0.15, delta = DELTA_D, z = 1.96) pnorm((abs(beta) - delta) / se - z)
mde_h1 <- function(se, delta = DELTA_D) delta + 2.80 * se
# Diagnostic only (NOT a decision input): power of a two-sided test against ZERO, the old (wrong-null) number.
power_zero_null <- function(beta, se, alpha = 0.05) {
  z <- qnorm(1 - alpha / 2)
  pnorm(abs(beta) / se - z) + pnorm(-abs(beta) / se - z)
}

# SE of the weighted DiD from wallet-level changes (independent wallets); used for power.
se_wallet_level <- function(wd) {
  tr <- wd[treat == TRUE]; ct <- wd[treat == FALSE]
  v_t <- sum(tr$w^2 * (tr$delta - weighted.mean(tr$delta, tr$w))^2) / sum(tr$w)^2
  v_c <- sum(ct$w^2 * (ct$delta - weighted.mean(ct$delta, ct$w))^2) / sum(ct$w)^2
  sqrt(v_t + v_c)
}

# Power at the matched sample's *measured* wallet-level SE (synthetic stage / quick look; the pre stage uses
# max(wallet, stratum) clustered SEs from estimate_did).
power_at <- function(panel, event_day, beta = -0.15, outcome = "D", cap = "none") {
  wd <- wallet_deltas(panel, event_day, outcome, cap)
  se <- se_wallet_level(wd)
  list(beta = beta, se = se, power = power_normal(se, beta), mde = mde_h1(se),
       n_exposed = sum(wd$treat), n_controls = sum(!wd$treat),
       sd_delta_exposed = sd(wd[treat == TRUE, delta]),
       sd_delta_controls = sqrt(sum(wd[treat == FALSE, w] * (wd[treat == FALSE, delta] -
         weighted.mean(wd[treat == FALSE, delta], wd[treat == FALSE, w]))^2) / sum(wd[treat == FALSE, w])))
}

# Clip incidence (prespec §2): CEM-weighted share of wallet-days STRICTLY beyond each cap, by outcome (D, A) x tail
# (lower/upper) x arm x period (baseline = day < event, post = day > event).
clip_incidence <- function(panel, event_day) {
  caps <- cap_values_all(panel, event_day)
  d <- panel[day_index != event_day]
  d[, period := fifelse(day_index < event_day, "baseline", "post")]
  d[, arm := fifelse(treat, "exposed", "control")]
  rows <- list()
  for (o in c("D", "A")) {
    cp <- caps[[o]]
    for (tl in c("lower", "upper")) {
      d[, flag := if (tl == "lower") get(o) < cp[1] else get(o) > cp[2]]
      r <- d[, .(share = sum(w * flag) / sum(w)), by = .(arm, period)][, `:=`(outcome = o, tail = tl)]
      rows[[length(rows) + 1L]] <- r
    }
  }
  rbindlist(rows)[, .(outcome, tail, arm, period, share)]
}
