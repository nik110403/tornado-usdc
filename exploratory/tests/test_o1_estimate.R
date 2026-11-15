# Fixture-only tests for exploratory/o1_estimate.R (E1-E4, bands, tie-break, age predicate, event-study identities). No real data.
# Run from the repo root:  Rscript exploratory/tests/test_o1_estimate.R
a <- grep("^--file=", commandArgs(FALSE), value = TRUE)
setwd(normalizePath(file.path(dirname(sub("^--file=", "", a[1])), "..", "..")))
for (f in c("utils", "decide", "cem", "estimate")) source(file.path("R", paste0(f, ".R")))
O1_NO_MAIN <- TRUE
source("exploratory/o1_estimate.R")
.n <- 0L
test <- function(name, expr) {
  r <- try(force(expr), silent = TRUE)
  if (inherits(r, "try-error")) stop(sprintf("TEST FAILED: %s\n%s", name, conditionMessage(attr(r, "condition"))), call. = FALSE)
  .n <<- .n + 1L; cat(sprintf("  ok  %s\n", name))
}
near <- function(x, y, tol = 1e-12) stopifnot(isTRUE(all.equal(x, y, tolerance = tol)))
days <- -100:59

# ---- E1 ------------------------------------------------------------------------------------------------------------------------
test("E1: moves, abs rank, negative rank, ratios (hand-checked)", {
  cv <- o1_path(days, rep(0, length(days)))
  cv[as.character(c(-50, -49))] <- c(0.10, 0.10)     # c(-50) = +0.10 -> moves: (-52->-50) +0.10, (-51->-49) +0.10, (-50->-48) -0.10, (-49->-47) -0.10 ; one-day steps are NOT moves
  m <- o1_moves2(cv); stopifnot(nrow(m) == 158L, m[day_index == -52, move] == 0.10, m[day_index == -50, move] == -0.10, m[day_index == -100, move] == 0)
  r <- e1_jump_rank(cv, -0.09)
  stopifnot(r$n_moves == 158L, r$rank_abs == 1L + 4L, r$n_negative_moves == 2L, r$rank_among_negative == 1L + 2L)   # 4 moves with |m| = 0.10 > 0.09
  near(r$ratio_to_largest, 0.9); near(r$ratio_to_sd, 0.09 / sd(m$move))
  stopifnot(is.na(e1_jump_rank(cv, 0.09)$rank_among_negative))
})
# ---- E2 ------------------------------------------------------------------------------------------------------------------------
test("E2: SD of changes, lag-1 autocorr, AR(1) recovered exactly, RW band formula", {
  cv <- numeric(length(days)); cv[1] <- 1
  for (i in 2:length(days)) cv[i] <- 0.02 + 0.9 * cv[i - 1] + 0.003 * sin(i)   # deterministic, no noise term in the regression beyond the sine
  cv <- o1_path(days, cv); e <- e2_dynamics(cv, horizons = 2:4)
  ch <- diff(unname(cv)); near(e$sd_daily_change, sd(ch)); near(e$lag1_autocorr_changes, cor(ch[-1], ch[-length(ch)]))
  stopifnot(abs(e$ar1_coef - 0.9) < 0.01, abs(e$ar1_intercept - 0.02) < 0.01)
  h <- 2:4; near(e$rw_post_mean_sd_calendar, sd(ch) * sqrt(sum(outer(h, h, pmin))) / 3); near(e$rw_post_mean_band95_calendar, 1.96 * e$rw_post_mean_sd_calendar)
})
# ---- E3 ------------------------------------------------------------------------------------------------------------------------
test("E3 bands high / low / between at the boundaries and with parameters", {
  stopifnot(o1_band(0.20) == "high", o1_band(0.2000001) == "high", o1_band(0.10) == "low", o1_band(0.1000001) == "between",
            o1_band(0.15) == "between", o1_band(-0.3) == "low")
  stopifnot(o1_band(0.15, high_ge = 0.15, low_le = 0.05) == "high", o1_band(0.15, high_ge = 0.30, low_le = 0.16) == "low")
  stopifnot(!any(c("drift", "no drift", "inconclusive") %in% c(o1_band(0.3), o1_band(0), o1_band(0.15))))     # old labels are gone
  tryCatch({ o1_band(0.1, high_ge = 0.1, low_le = 0.1); stop("should have failed") }, error = function(e) stopifnot(!grepl("should have failed", conditionMessage(e))))
})
test("E3 level, slope, negative flag and sampling interval from V", {
  cv <- o1_path(days, 0.05 + 0.001 * (days + 100)); cv[] <- cv   # level over 1 Mar..8 May (days -100..-32): mean of 0.05 + 0.001*(0..68)
  V <- diag(1e-4, length(days)); dimnames(V) <- list(days, days)
  e <- e3_level(cv, V = V, df = 50)
  near(e$L, 0.05 + 0.001 * mean(0:68)); near(e$slope_per_day, 0.001); stopifnot(e$n_days == 69L, e$band == "low", !e$negative_flag)
  near(e$sampling_se, sqrt(1e-4 / 69)); near(e$sampling_hi - e$L, qt(0.975, 50) * e$sampling_se)
  stopifnot(e3_level(o1_path(days, rep(-0.2, 160)))$negative_flag)
  e2 <- e3_level(cv, window = c("2022-03-01", "2022-03-10")); stopifnot(e2$n_days == 10L)           # window is a parameter
  stopifnot(identical(e$slope_interval, "none"), !any(grepl("slope_(lo|hi|se)", names(e))))             # slope: point estimate only, no interval
})
test("E3 prints the random-walk rates beside every result (D 29/32, A 37 %, drift 65-78 / 75-85 %)", {
  cv <- o1_path(days, rep(0.3, 160))
  d <- e3_level(cv, outcome = "D"); a <- e3_level(cv, outcome = "A")
  stopifnot(grepl("29%", d$rw_rates_note), grepl("32%", d$rw_rates_note), grepl("0.65", d$rw_rates_note), grepl("0.78", d$rw_rates_note),
            grepl("37%", a$rw_rates_note), grepl("0.75", a$rw_rates_note), grepl("0.85", a$rw_rates_note), grepl("not stated", a$rw_rates_note))
  stopifnot(grepl("cannot separate drift", o1_rw_note("D")), grepl("not stated in the plan for this outcome", o1_rw_note(NA)))
  est <- suppressWarnings(o1_estimands(cv, -0.09, outcome = "A")); stopifnot(grepl("37%", est$E3$rw_rates_note))
})

test("stable: low and |L - m| <= tol; m = mean of days 0..59", {
  cv <- o1_path(days, c(rep(0.06, 100), rep(0.04, 60))); cv["59"] <- 0
  e <- e3_level(cv); m <- o1_stable_ref(cv); near(m, (59 * 0.04) / 60)
  stopifnot(o1_stable(e, m, 0.05), !o1_stable(e, m, 0.001), !o1_stable(list(band = "high", L = m), m), !o1_stable(list(band = "between", L = m), m))
})
# ---- E4 ------------------------------------------------------------------------------------------------------------------------
test("E4 primary (single 11->13 May) and secondary (largest two-day, 8-14 May) are both always reported; only the primary drives the trigger", {
  v <- setNames(numeric(length(days)), days)
  d <- function(x) as.character(o1_day(x))
  v[d("2022-05-11")] <- 0; v[d("2022-05-13")] <- -0.05                 # the 11->13 May move
  v[d("2022-05-09")] <- 0.0;  v[d("2022-05-10")] <- 0.04; v[d("2022-05-12")] <- -0.10   # a larger two-day move elsewhere: 10 -> 12 May = -0.14
  cv <- o1_path(days, v)
  e <- e4_terra(cv, jump = -0.0906)
  near(e$primary_move, -0.05); stopifnot(e$primary_from_date == "2022-05-11", e$primary_to_date == "2022-05-13")
  near(e$secondary_move, -0.14); stopifnot(e$secondary_from_date == "2022-05-10", e$secondary_to_date == "2022-05-12")
  stopifnot(!e$trigger, e$secondary_ge_jump_info_only)                  # the secondary exceeds the jump but does NOT fire the trigger
  near(e$cumulative_change, o1_at(cv, "2022-05-13") - o1_at(cv, "2022-05-08"))
  mx2 <- e4_terra(cv, jump = -0.0906, range = c("2022-05-11", "2022-05-14")); stopifnot(mx2$secondary_from_date %in% c("2022-05-11", "2022-05-12"))   # windows are parameters
  stopifnot(e4_terra(cv, -0.05)$trigger)                                 # |primary| >= |jump| at equality fires
  s3 <- e4_terra(cv, -0.2, single = c("2022-05-10", "2022-05-12")); near(s3$primary_move, -0.14); stopifnot(!s3$trigger)
  stopifnot(!("exceeds_jump" %in% names(e)), all(c("primary_move", "secondary_move", "cumulative_change", "trigger") %in% names(e)))
  w <- o1_flatten(list(list(status = "estimated", variant = "capped", outcome = "D", cluster = "uid", est = o1_estimands(cv, -0.0906, outcome = "D"))), "full")
  stopifnot(all(c("E4.primary_move", "E4.secondary_move", "E4.cumulative_change", "E4.trigger", "E3.rw_rates_note") %in% w$field))   # both reported in every table
})
# ---- tie-break ---------------------------------------------------------------------------------------------------------------------
test("restricted tie-break: between-disagree, low-not-stable, stable only if both, E4 trigger in either row", {
  mk <- function(L, st, band, trig = FALSE) list(E3 = list(band = band, L = L), stable = st, E4 = list(trigger = trig))
  f <- mk(0.05, TRUE, "low")
  t1 <- o1_tiebreak(f, mk(0.15, FALSE, "between")); stopifnot(t1$e3_band == "between", !t1$stable, t1$downgraded, t1$reading == "between: full and restricted rows disagree", t1$row == "between")
  t2 <- o1_tiebreak(f, mk(0.06, TRUE, "low")); stopifnot(t2$e3_band == "low", t2$stable, !t2$downgraded, t2$reading == "low, stable", t2$row == "low, stable")
  t3 <- o1_tiebreak(f, mk(0.08, FALSE, "low")); stopifnot(t3$e3_band == "low", !t3$stable, t3$downgraded, t3$reading == "low, not stable", t3$row == "low, not stable")
  t4 <- o1_tiebreak(f, NULL); stopifnot(t4$e3_band == "low", t4$stable, grepl("not estimable", t4$note), t4$reading == "low, stable")
  t5 <- o1_tiebreak(mk(0.3, FALSE, "high"), mk(0.05, TRUE, "low")); stopifnot(t5$e3_band == "between", !t5$stable, t5$reading == "between: full and restricted rows disagree")
  t6 <- o1_tiebreak(mk(0.3, FALSE, "high"), mk(0.31, FALSE, "high")); stopifnot(t6$reading == "high", t6$row == "high")
  t7 <- o1_tiebreak(mk(0.15, FALSE, "between"), mk(0.16, FALSE, "between")); stopifnot(t7$reading == "between", t7$row == "between")
  t8 <- o1_tiebreak(mk(0.05, FALSE, "low"), mk(0.05, TRUE, "low")); stopifnot(!t8$stable, t8$reading == "low, not stable")     # not both stable
  stopifnot(!o1_tiebreak(f, mk(0.06, TRUE, "low", FALSE))$e4_trigger)
  stopifnot(o1_tiebreak(mk(0.05, TRUE, "low", FALSE), mk(0.06, TRUE, "low", TRUE))$e4_trigger,       # fires in the restricted row only
            o1_tiebreak(mk(0.05, TRUE, "low", TRUE), mk(0.06, TRUE, "low", FALSE))$e4_trigger,       # fires in the full sample only
            o1_tiebreak(mk(0.05, TRUE, "low", TRUE), NULL)$e4_trigger)
})
test("section 2.7 reading text, joint D/A rule, coverage printed beside the reading", {
  tb <- data.table(outcome = c("D", "A"), cluster = "uid", reading = c("high", "low, not stable"), row = c("high", "low, not stable"), e4_trigger = c(TRUE, FALSE))
  cov <- data.table(month = "Mar", arm = "exposed", share_valid = 0.9)
  ln <- paste(o1_reading_lines(tb, cov), collapse = "\n")
  stopifnot(grepl("29% \\(D\\) / 37% \\(A\\)", ln), grepl("driftless random walk lands in 'high' 29%", ln), grepl("D: high; A: low, not stable", ln),
            grepl("combined reading row: between", ln), grepl("Coverage and clip incidence", ln), grepl("share_valid", ln), grepl("E4 trigger FIRED", ln),
            grepl("E4 trigger not fired", ln), grepl("EXPLORATORY, POST-RESULTS", ln), grepl("L does not separate drift from a random walk", ln))
  stopifnot(o1_joint_row("high", "high") == "high", o1_joint_row("low, stable", "low, not stable") == "between", nrow(O1_READINGS) == 4L)
})
# ---- age predicate ------------------------------------------------------------------------------------------------------------------
test("age predicate clauses, unknown, admissions by clause x arm", {
  cov <- data.table(address = paste0("w", 1:6), first_send_block = c("100", "300", "", "", "", "100"))
  pin <- data.table(address = paste0("w", 1:6), usdc = c(NA, 0, 5, 0, NA, 0), usdt = c(NA, 0, NA, 0, 0, 0))
  p <- o1_age_predicate(cov, pin, 200)
  # w1: send<=pin ->admit (balances missing irrelevant); w2: send 300>200, balances 0 -> not admitted, not unknown; w3: USDC>0 -> admit;
  # w4: no send, zero balances -> not admitted, not unknown; w5: no send, usdc missing -> unknown; w6: send<=pin
  stopifnot(identical(p[order(address), admitted], c(TRUE, FALSE, TRUE, FALSE, FALSE, TRUE)), identical(p[order(address), unknown], c(FALSE, FALSE, FALSE, FALSE, TRUE, FALSE)))
  arm <- setNames(c("exposed", "exposed", "control", "control", "control", "exposed"), paste0("w", 1:6))
  ad <- o1_admissions(p, arm)
  stopifnot(ad[clause == "admitted (any clause)" & arm == "exposed", n] == 2L, ad[clause == "unknown (excluded)" & arm == "control", n] == 1L,
            ad[clause == "USDC pin balance>0" & arm == "control", n] == 1L)
})
# ---- event study on a synthetic balanced panel ----------------------------------------------------------------------------------------
set.seed(7)
mk_panel <- function(n_t = 24, n_c = 48, drop_days = FALSE) {
  uid <- paste0("u", seq_len(n_t + n_c)); treat <- rep(c(TRUE, FALSE), c(n_t, n_c))
  grid <- CJ(i = seq_along(uid), day_index = c(-100:59, 61:70))
  grid[, `:=`(uid = uid[i], treat = treat[i])]
  gap <- function(k) ifelse(k < 0, 0.04 * sin(k / 7), 0.03) - ifelse(k > 60, 0.09, 0)
  base <- 1e6 * runif(length(uid), 50, 500)
  grid[, usdc_units := round(base[i] * (1 + 0.1 * rnorm(.N)) * (1 + ifelse(treat, gap(day_index), 0)))]
  grid[, usdt_units := round(base[i] * 0.5 * (1 + 0.05 * rnorm(.N)))]
  matched <- data.table(uid = uid, treat = treat, stratum = rep(1:3, length.out = length(uid)), w = ifelse(treat, 1, 0.5 + runif(length(uid))))
  list(wide = grid[, .(uid, day_index, usdc_units, usdt_units)], matched = matched)
}
S <- mk_panel()
panel <- build_panel(S$wide, S$matched, 0:59)
caps <- list(D = c(-3, 4), A = c(0, 5))
test("event study = weighted saturated day x arm difference (hand computation), reference 7 Aug = 0", {
  d <- o1_prep(panel, caps, TRUE)
  f <- o1_fit(d, "D", "uid"); stopifnot(is.null(f$error), length(f$days) == 160L, f$b[["59"]] == 0, all(f$V["59", ] == 0))
  wm <- function(x, w) sum(x * w) / sum(w)
  arm_mean <- function(tr, k) { s <- d[treat == tr & day_index == k]; wm(s$D, s$w) }
  for (k in c(-100L, -57L, -1L, 0L, 30L)) near(f$b[[as.character(k)]], (arm_mean(TRUE, k) - arm_mean(TRUE, 59)) - (arm_mean(FALSE, k) - arm_mean(FALSE, 59)), 1e-8)
  stopifnot(!(60L %in% f$days), max(f$days) == 59L)       # 8 Aug and post days are outside the O1 path
})
test("balanced panel: 9 Jun-7 Aug coefficients identical with and without the extension (the signed-equality condition)", {
  d_ext <- o1_prep(panel, caps, TRUE); d_sig <- d_ext[day_index >= 0L]
  fe <- o1_fit(d_ext, "D", "uid"); fs <- o1_fit(d_sig, "D", "uid")
  near(fe$b[as.character(0:59)], fs$b[as.character(0:59)], 1e-9)
  stopifnot(max(abs(fe$b[as.character(0:59)] - fs$b[as.character(0:59)])) <= 1e-9)   # coefficients only: clustered SEs legitimately differ with the window
})
# ---- closed-form o1_fit vs the previous feols call (plan addendum A4) -------------------------------------------------------------------
o1_fit_feols_ref <- function(d, outcome, cluster, ref = O1_REF_DAY) {     # private copy of the pre-A4 o1_fit body
  days <- sort(unique(d$day_index))
  fit <- feols(as.formula(sprintf("%s ~ i(day_index, ex, ref = %d) | uid + day_index", outcome, ref)), data = d, weights = d$w,
               cluster = as.formula(paste0("~", cluster)), fixef.tol = 1e-11, fixef.iter = 10000, notes = FALSE)
  cf <- coef(fit); k <- as.integer(sub("^day_index::(-?[0-9]+):ex$", "\\1", names(cf)))
  b <- setNames(numeric(length(days)), days); b[as.character(k)] <- cf
  V <- matrix(0, length(days), length(days), dimnames = list(days, days)); V[as.character(k), as.character(k)] <- vcov(fit)
  list(days = days, b = b, V = V)
}
# V agreement: elementwise relative to the feols entry over entries with |entry| >= 1e-3 * max|V| (entries below that are cancellation noise of the iterative
# feols solve, fixef.tol 1e-11: e.g. abs diff 4e-14 on an entry of 3e-8), and absolute diff scaled by max|V| over ALL entries. Scaled <= 1e-9; elementwise <= 1e-7
# (stratum clustering has 3 clusters, so V has rank <= 2 and small entries are differences of large ones; the elementwise floor is feols' own noise).
vdiff <- function(n, r) { big <- abs(r) >= 1e-3 * max(abs(r)); c(rel = max(abs(n - r)[big] / abs(r)[big]), scaled = max(abs(n - r)) / max(abs(r))) }
cf_worst <- new.env(); cf_worst$b <- 0; cf_worst$V <- 0; cf_worst$Vs <- 0
cf_acc <- function(n, r) { v <- vdiff(n$V, r$V); cf_worst$b <- max(cf_worst$b, max(abs(n$b - r$b))); cf_worst$V <- max(cf_worst$V, v[["rel"]]); cf_worst$Vs <- max(cf_worst$Vs, v[["scaled"]]) }
cf_ok <- function() stopifnot(cf_worst$b <= 1e-9, cf_worst$V <= 1e-7, cf_worst$Vs <= 1e-9)
cf_say <- function(l) cat(sprintf("      achieved on %s: max|db| = %.2e, max relative dV (entries >= 1e-3 max|V|) = %.2e, max |dV|/max|V| = %.2e\n", l, cf_worst$b, cf_worst$V, cf_worst$Vs))
cf_compare <- function(d, label) for (o in c("D", "A")) for (cl in c("uid", "stratum")) {
  n <- o1_fit(d, o, cl); r <- o1_fit_feols_ref(d, o, cl)
  stopifnot(is.null(n$error), identical(n$days, r$days), identical(dimnames(n$V), dimnames(r$V)), n$G == uniqueN(d[[cl]]), n$n_obs == nrow(d), n$cluster == cl)
  stopifnot(all(n$V[as.character(O1_REF_DAY), ] == 0), all(n$V[, as.character(O1_REF_DAY)] == 0), n$b[[as.character(O1_REF_DAY)]] == 0)
  cf_acc(n, r)
}
test("closed-form o1_fit = previous feols fit on the fixture panel (D, A; capped, uncapped; uid, stratum): b <= 1e-9, V scaled <= 1e-9", {
  for (cp in c(TRUE, FALSE)) cf_compare(o1_prep(panel, caps, cp), if (cp) "fixture capped" else "fixture uncapped")
  cf_ok(); cf_say("fixture")
})
mk_tiny <- function(zero_w = FALSE) {
  set.seed(11); N <- 14; dys <- c(-3:2, 4)
  d <- CJ(i = 1:N, day_index = dys); d[, `:=`(uid = paste0("w", i), ex = as.numeric(i <= 5), stratum = c(1, 1, 2, 2, 2, 3, 3, 3, 3, 1, 2, 3, 1, 2)[i])]
  wi <- c(0.4, 1, 2.5, 0.7, 1.3, 3, 0.2, 1.1, 0.9, 2, 1.7, 0.6, 1.4, 2.2); if (zero_w) wi[7] <- 0
  d[, w := wi[i]][, D := rnorm(.N) + ex * 0.2 * day_index + i / 7][, A := runif(.N) * (1 + ex)][]
}
test("closed-form o1_fit = previous feols fit on a tiny unequal-weight panel with several wallets per stratum (reference day 2)", {
  cf_worst$b <- 0; cf_worst$V <- 0; cf_worst$Vs <- 0
  d <- mk_tiny(); for (o in c("D", "A")) for (cl in c("uid", "stratum")) {
    n <- o1_fit(d, o, cl, ref = 2L); r <- o1_fit_feols_ref(d, o, cl, ref = 2L); stopifnot(is.null(n$error))
    cf_acc(n, r)
  }
  cf_ok(); cf_say("tiny panel")
})
test("closed-form o1_fit: a zero-weight wallet is dropped as feols drops its rows (b, V agree)", {
  d <- mk_tiny(TRUE); for (cl in c("uid", "stratum")) {
    n <- o1_fit(d, "D", cl, ref = 2L); r <- o1_fit_feols_ref(d, "D", cl, ref = 2L)
    v <- vdiff(n$V, r$V); stopifnot(is.null(n$error), max(abs(n$b - r$b)) <= 1e-9, v[["rel"]] <= 1e-7, v[["scaled"]] <= 1e-9, n$n_obs == nrow(d))
  }
})
test("closed-form o1_fit guards return list(error=...)", {
  d <- mk_tiny(); bad <- function(x, ...) { f <- o1_fit(x, "D", "uid", ref = 2L, ...); stopifnot(is.list(f), is.character(f$error), is.null(f$b)) }
  bad(d[-3]); bad(rbind(d, d[1]))                                                   # missing row (unbalanced), duplicate row
  x <- copy(d); x[uid == "w3" & day_index == 0, w := 9]; bad(x)                    # w varies within wallet
  x <- copy(d); x[3, D := NA]; bad(x); x <- copy(d); x[3, D := Inf]; bad(x)        # NA / non-finite y
  x <- copy(d); x[uid == "w3", w := NA_real_]; bad(x); x <- copy(d); x[uid == "w3", w := Inf]; bad(x)    # NA / non-finite w
  x <- copy(d); x[, ex := 0]; bad(x); x <- copy(d); x[, ex := 1]; bad(x)           # an arm empty
  x <- copy(d); x[ex == 1, w := 0]; bad(x)                                          # an arm empty after zero-weight drop
  x <- copy(d); x[uid == "w3", w := -1]; bad(x)                                     # negative weight
  stopifnot(!is.null(o1_fit(d, "D", "uid", ref = 2L)$b), !is.null(o1_fit(d, "D", "uid", ref = 99L)$error))
})
test("o1_fit is invariant to row order (the real panel carries negative days after day 59)", {
  d <- o1_prep(panel, caps, TRUE); f0 <- o1_fit(d, "D", "uid"); set.seed(808); f1 <- o1_fit(d[sample(.N)], "D", "uid")
  stopifnot(is.null(f1$error), identical(f0$b, f1$b), max(abs(f0$V - f1$V)) <= 1e-12 * max(abs(f0$V)))
})
test("o1_analyse: capped and uncapped paths, both clusterings, estimands present; caps are the PASSED signed caps", {
  jumps <- list(D = -0.0906, A = -0.0809)
  a <- o1_analyse(panel, unique(S$matched$uid), caps, jumps)
  stopifnot(setequal(unique(a$paths$variant), c("capped", "uncapped")), setequal(unique(a$paths$cluster), c("uid", "stratum")), nrow(a$paths) == 2 * 2 * 2 * 160)
  e <- Find(function(x) x$variant == "capped" && x$outcome == "D" && x$cluster == "uid", a$estimands)
  stopifnot(e$est$E1$jump == -0.0906, e$est$E3$band %in% c("high", "low", "between"), is.finite(e$est$E3$sampling_se))
  tight <- list(D = c(-0.2, 0.2), A = c(0, 5))                       # caps really are the passed ones
  b <- o1_analyse(panel, unique(S$matched$uid), tight, jumps, clusters = "uid", variants = c(capped = TRUE))
  stopifnot(!isTRUE(all.equal(b$paths[outcome == "D", coef], a$paths[variant == "capped" & outcome == "D" & cluster == "uid", coef])))
  fl <- o1_flatten(a$estimands, "full"); stopifnot(all(c("E1.rank_abs", "E3.band", "E4.trigger", "E4.primary_move", "E4.secondary_move", "stable") %in% fl$field))
})
test("dropped wallets: fit uses only wallets with an O1 path; dropped count and weight share by arm", {
  ok <- setdiff(unique(S$matched$uid), c("u1", "u2", "u30"))
  dr <- o1_dropped_share(S$matched, ok)
  stopifnot(dr[arm == "exposed", n_dropped] == 2L, dr[arm == "control", n_dropped] == 1L)
  near(dr[arm == "control", weight_share_dropped], S$matched[uid == "u30", w] / S$matched[treat == FALSE, sum(w)])
  a <- o1_analyse(panel, ok, caps, list(D = -0.09, A = -0.08), clusters = "uid", variants = c(capped = TRUE))
  stopifnot(a$fits[["capped|D|uid"]]$n_obs == length(ok) * 160L)
})
test("coverage table: month x arm shares, valid share reflects dropped wallets, clip incidence by tail", {
  ok <- setdiff(unique(S$matched$uid), "u30")
  p2 <- copy(panel); p2[uid == "u3" & day_index == -90L, D := 99]            # one upper D clip for an exposed wallet in March
  cv <- o1_coverage(p2, ok, S$matched, caps)
  stopifnot(setequal(cv$month, c("Mar", "Apr", "May", "1-8Jun")), nrow(cv) == 8L)
  mar_c <- cv[month == "Mar" & arm == "control"]
  near(mar_c$share_valid, 1 - S$matched[uid == "u30", w] / S$matched[treat == FALSE, sum(w)])
  stopifnot(cv[month == "Mar" & arm == "exposed", clip_D_upper] > 0, cv[month == "Apr" & arm == "exposed", clip_D_upper] == 0)
})
# ---- extraction-output checks (security) ------------------------------------------------------------------------------------------------
test("manifest must list o1_panel.csv, o1_pin_balances.csv and o1_summary.json (flat names, no duplicates)", {
  ok <- c("o1_panel.csv", "o1_pin_balances.csv", "o1_summary.json", "receipts.jsonl", "o1_wallet_status.csv")
  stopifnot(o1_check_manifest(ok))
  for (drop in O1_REQUIRED_FILES) { r <- tryCatch(o1_check_manifest(setdiff(ok, drop)), error = function(e) conditionMessage(e)); stopifnot(is.character(r), grepl(drop, r, fixed = TRUE)) }
  stopifnot(inherits(try(o1_check_manifest(c(ok, "../x")), silent = TRUE), "try-error"), inherits(try(o1_check_manifest(c(ok, ok[1])), silent = TRUE), "try-error"),
            inherits(try(o1_check_manifest(character()), silent = TRUE), "try-error"))
})
test("extraction input hashes must equal the signed trace input_sha256 for membership / pre_covariates / pre_panel", {
  tr <- list(match_membership = "aa", pre_covariates = "bb", pre_panel = "cc", post_panel = "dd")
  stopifnot(o1_check_input_hashes(list(membership = "aa", signed_cov = "bb", signed_panel = "cc"), tr))
  for (k in names(O1_HASH_MAP)) {
    h <- list(membership = "aa", signed_cov = "bb", signed_panel = "cc"); h[[k]] <- "zz"
    r <- tryCatch(o1_check_input_hashes(h, tr), error = function(e) conditionMessage(e)); stopifnot(is.character(r), grepl(k, r, fixed = TRUE))
    h[[k]] <- NULL; stopifnot(inherits(try(o1_check_input_hashes(h, tr), silent = TRUE), "try-error"))             # absent is refused too
  }
})
test("a looser mismatch / reverify threshold is refused unless disclosed; plan defaults pass silently", {
  plan <- list(max_mismatch_frac = 0.005, max_reverify_frac = 0.01, reverify_fraction = 0.05, reverify_seed = 808)
  stopifnot(length(o1_check_extract_settings(plan, "")) == 0L, length(o1_check_extract_settings(modifyList(plan, list(max_mismatch_frac = 0.001)), "")) == 0L)   # tighter is fine
  for (ch in list(list(max_mismatch_frac = 0.02), list(max_reverify_frac = 0.05), list(reverify_fraction = 0.01), list(reverify_seed = 1))) {
    p <- modifyList(plan, ch)
    stopifnot(inherits(try(o1_check_extract_settings(p, ""), silent = TRUE), "try-error"), inherits(try(o1_check_extract_settings(p, NULL), silent = TRUE), "try-error"),
              inherits(try(o1_check_extract_settings(p, "  "), silent = TRUE), "try-error"))
    d <- o1_check_extract_settings(p, "reviewer-approved: re-run after a code fix"); stopifnot(length(d) == 1L, grepl(names(ch), d, fixed = TRUE))
  }
  p <- plan; p$max_mismatch_frac <- NULL; stopifnot(inherits(try(o1_check_extract_settings(p, ""), silent = TRUE), "try-error"))   # absent = not the plan default
})
cat(sprintf("ALL %d O1 R TESTS PASSED\n", .n))
