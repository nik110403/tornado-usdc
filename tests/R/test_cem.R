set.seed(11)
mkcov <- function() {
  n_t <- 400; n_c <- 1500
  d <- data.table(uid = 1:(n_t + n_c), treat = rep(c(TRUE, FALSE), c(n_t, n_c)),
                  log_hold = c(rnorm(n_t, 9), rnorm(n_c, 8.5)), transfers = rpois(n_t + n_c, 6),
                  age = c(rnorm(n_t, 5), rnorm(n_c, 4)), eth = rexp(n_t + n_c),
                  usdc_share = sample(c(0, 1, runif(8)), n_t + n_c, TRUE))
  d$age[sample(nrow(d), 40)] <- NA
  d
}
cov <- mkcov(); vars <- c("log_hold", "transfers", "age", "eth")
test("ratio cap, no replacement, IKP eq. 6 weights: exposed 1, control (m_C/m_T)(m_T,s/m_C,s)", {
  m <- cem_match(cov, vars, "none", ratio = 3, seed = 5)
  mm <- m$matched
  stopifnot(anyDuplicated(mm$uid) == 0)
  s <- mm[, .(nt = sum(treat), nc = sum(!treat), wc = sum(w[!treat])), by = stratum]
  mT <- sum(mm$treat); mC <- sum(!mm$treat)
  stopifnot(all(s$nc <= 3 * s$nt), all(s$nc >= 1), all(abs(s$wc - (mC / mT) * s$nt) < 1e-9),
            abs(sum(mm[treat == FALSE, w]) - mC) < 1e-9,
            all(mm[treat == TRUE, w] == 1),
            m$n_exposed - m$dropped_exposed == sum(mm$treat),
            sum(m$strata$n_exposed) == sum(mm$treat))
})
test("dropped-exposed counted when a stratum has no control", {
  d <- copy(cov); d[treat == TRUE & uid <= 20, `:=`(log_hold = 40, transfers = 5000L)]
  m <- cem_match(d, vars, "none", n_bins = 3, seed = 1)
  stopifnot(m$dropped_exposed >= 20 || m$dropped_exposed > 0)
})
test("seeded: same seed identical, different seed differs", {
  a <- cem_match(cov, vars, "none", seed = 3)$matched; b <- cem_match(cov, vars, "none", seed = 3)$matched
  c2 <- cem_match(cov, vars, "none", seed = 4)$matched
  stopifnot(identical(a, b), !identical(a$uid, c2$uid))
})
test("usdc modes: none adds no key; three_bins separates 0 / interior / 1", {
  b <- coarsen_usdc_share(c(0, .3, .99, 1, NA), c(0, .5), "three_bins", 4)
  stopifnot(identical(b, c(0L, 1L, 1L, 2L, -1L)), is.null(coarsen_usdc_share(.5, .5, "none", 4)))
  n0 <- cem_match(cov, vars, "none", seed = 1)$n_strata_kept
  n3 <- cem_match(cov, vars, "three_bins", seed = 1)$n_strata_kept
  nc <- cem_match(cov, vars, "continuous", seed = 1)$n_strata_kept
  stopifnot(n3 >= n0, nc >= n0)
})
test("matched units: matched exposed + dropped = all exposed", {
  m <- cem_match(cov, vars, "three_bins", seed = 2)
  stopifnot(sum(m$matched$treat) + m$dropped_exposed == sum(cov$treat))
})
test("cut-points: type-7 25/50/75 among exposed, duplicates removed, value == cut-point goes to the higher bin", {
  ref <- c(1, 2, 3, 4, 5, 6, 7, 8, 9)                       # q25 = 3, q50 = 5, q75 = 7 exactly
  b <- coarsen_q(c(2.9, 3, 4.9, 5, 6.9, 7, 9), ref, 4)
  stopifnot(identical(as.integer(b), c(0L, 1L, 1L, 2L, 2L, 3L, 3L)), identical(attr(b, "cuts"), c(3, 5, 7)))
  d <- coarsen_q(c(0, 1, 2), c(0, 0, 0, 0, 1, 2), 4)        # q25 = q50 = 0 -> duplicates removed
  stopifnot(identical(attr(d, "cuts"), c(0, 0.75)), identical(as.integer(d), c(1L, 2L, 2L)))
})
test("never-sent age (NA) is its own bin; any other missing covariate stops the run", {
  b <- coarsen_q(c(NA, 1, 5), c(1, 2, 3, 4, 5), 4, na_ok = TRUE)
  stopifnot(b[1] == -1L)
  d <- copy(cov); d$eth[3] <- NA
  e <- try(cem_match(d, vars, "none", seed = 1), silent = TRUE)
  stopifnot(inherits(e, "try-error"), grepl("missing values in 'eth'", conditionMessage(attr(e, "condition"))))
  m <- cem_match(cov, vars, "none", seed = 1)               # cov has NA ages -> fine
  stopifnot(nrow(m$matched) > 0)
})
test("cut-points use exposed wallets only (controls do not move them)", {
  m1 <- cem_match(cov, vars, "none", seed = 1)
  d <- copy(cov); d[treat == FALSE, log_hold := log_hold + 5]
  m2 <- cem_match(d, vars, "none", seed = 1)
  stopifnot(identical(m1$cutpoints$log_hold, m2$cutpoints$log_hold))
})
test("draw is invariant to input row order; ties broken by address; RNG kind fixed", {
  d <- copy(cov); d[, address := sprintf("0x%06x", (uid * 7919L) %% 100003L)]
  a <- cem_match(d, vars, "three_bins", seed = SEED_PRIMARY_CEM)$matched
  b <- cem_match(d[sample(.N)], vars, "three_bins", seed = SEED_PRIMARY_CEM)$matched
  stopifnot(identical(a, b), identical(RNGkind()[1:3], c("Mersenne-Twister", "Inversion", "Rejection")))
})
