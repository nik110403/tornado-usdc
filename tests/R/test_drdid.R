# S19 DRDID freeze tests: fixture-only (synthetic frames; no file under out/ is read). Runnable standalone:
#   Rscript tests/R/test_drdid.R
if (!exists("classify_ci")) {
  a <- grep("^--file=", commandArgs(FALSE), value = TRUE)
  setwd(normalizePath(file.path(dirname(sub("^--file=", "", a[1])), "..", "..")))
  for (f in c("utils", "decide", "cem", "estimate", "validate", "synth", "pre", "blacklist")) source(file.path("R", paste0(f, ".R")))
}
for (f in c("presign", "drdid")) if (!exists(c(presign = "run_presign", drdid = "drdid_estimate")[[f]])) source(file.path("R", paste0(f, ".R")))
if (!exists("test")) {
  .t_total <- 0L
  test <- function(name, expr) {
    r <- try(force(expr), silent = TRUE)
    if (inherits(r, "try-error")) stop(sprintf("TEST FAILED: %s\n%s", name, conditionMessage(attr(r, "condition"))), call. = FALSE)
    .t_total <<- .t_total + 1L; cat(sprintf("  ok  %s\n", name))
  }
  standalone <- TRUE
} else standalone <- FALSE

local({
# synthetic eligible frame: true ATT on capped D = -0.3 for exposed, treatment mildly selected on log B
set.seed(5)
n <- 900L
B <- exp(rnorm(n, 8, .7)); tr <- runif(n) < plogis(-1.2 + 0.25 * (log(B) - 8))
fr <- data.table(address = sprintf("0x%040x", seq_len(n)), treat = tr, B = B, usdc_share = runif(n), transfers = rpois(n, 9),
                 eth_wei = 1e18 * rexp(n), age = ifelse(runif(n) < .12, NA, 1.2e7 - rexp(n, 1e-6)))
fr[, y0_unc := rnorm(.N, 0.2 * usdc_share, .4)]; fr[, y1_unc := y0_unc + rnorm(.N, 0, .2) - 0.3 * treat]
fr[, `:=`(y0_cap = pmax(pmin(y0_unc, 1), -1), y1_cap = pmax(pmin(y1_unc, 1), -1))]

test("freeze record: package, version, function, exact arguments, trimming, covariates are pinned; installed API matches", {
  f <- DRDID_FREEZE
  stopifnot(f$package == "DRDID", f$version == "1.4.0", f$function_name == "drdid_imp_panel", f$trim_level == 0.99, isFALSE(f$arguments$boot),
            isFALSE(f$arguments$inffunc), identical(f$formals, names(formals(DRDID::drdid_imp_panel))),
            identical(f$covariates, colnames(drdid_covariates(fr))), grepl("controls", f$trimming, ignore.case = TRUE), grepl("0\\.99", f$trimming))
  chk <- drdid_check_installed(); stopifnot(chk$ok, chk$installed_version == "1.4.0")
  rec <- drdid_freeze_record(); stopifnot(rec$installed, grepl("^[0-9a-f]{64}$", rec$function_source_sha256))
  p <- tempfile(fileext = ".json"); on.exit(unlink(p))
  jsonlite::write_json(rec, p, auto_unbox = TRUE, pretty = TRUE, digits = NA)
  j <- jsonlite::fromJSON(p); stopifnot(j$package == "DRDID", j$function_name == "drdid_imp_panel", j$arguments$trim.level == 0.99)
})
test("covariate transforms are exact: log B, share, log1p counts, ETH in ETH, age 0 + never-sent indicator, intercept, no exchange share", {
  x <- drdid_covariates(fr[1:5][, `:=`(B = c(100, 1000, 5, 7, 9), age = c(NA, 0, 1e6, NA, 3), eth_wei = c(0, 1e18, 2e18, 5e17, 0), transfers = c(0, 1, 2, 3, 4), usdc_share = c(0, .5, 1, .25, .75))])
  stopifnot(all(x[, "intercept"] == 1), isTRUE(all.equal(unname(x[, "log_B"]), log(c(100, 1000, 5, 7, 9)))),
            isTRUE(all.equal(unname(x[, "log1p_eth_balance_eth"]), log1p(c(0, 1, 2, .5, 0)))), isTRUE(all.equal(unname(x[, "log1p_age_blocks"]), log1p(c(0, 0, 1e6, 0, 3)))),
            identical(unname(x[, "never_sent"]), c(1, 0, 0, 1, 0)), isTRUE(all.equal(unname(x[, "log1p_transfer_count"]), log1p(0:4))), !("exchange_share" %in% colnames(x)))
})
test("rows equal a direct call of the frozen function with the frozen arguments; analytic influence-function CI", {
  r <- drdid_estimate(fr, "capped_D")
  d <- DRDID::drdid_imp_panel(y1 = fr$y1_cap, y0 = fr$y0_cap, D = as.numeric(fr$treat), covariates = drdid_covariates(fr), i.weights = NULL, boot = FALSE, inffunc = FALSE, trim.level = 0.99)
  stopifnot(r$status == "estimated", r$att == d$ATT, r$se == d$se, r$lo == d$lci, r$hi == d$uci, r$package == "DRDID", r$fn == "drdid_imp_panel", r$trim_level == 0.99,
            r$decision_case == classify_ci(d$lci, d$uci, DELTA_D)$case, abs(r$att - (-0.3)) < 0.1, r$ps_flag %in% 0:1)
  u <- drdid_estimate(fr, "uncapped_D")
  du <- DRDID::drdid_imp_panel(y1 = fr$y1_unc, y0 = fr$y0_unc, D = as.numeric(fr$treat), covariates = drdid_covariates(fr), trim.level = 0.99)
  stopifnot(u$status == "estimated", u$att == du$ATT, u$n_exposed == sum(fr$treat), u$n_control == sum(!fr$treat))
})
test("trimming: the frozen trim level is passed to the package; trimmed controls are counted from the package's own propensity fit", {
  e <- drdid_estimate(fr, "capped_D"); stopifnot(e$status == "estimated", e$n_controls_trimmed == 0L, e$trim_level == 0.99, !is.na(e$n_exposed_above_trim_untrimmed))
  fz <- modifyList(DRDID_FREEZE, list(trim_level = 0.25))                      # a level that trims: proves the argument reaches the package
  t5 <- drdid_estimate(fr, "capped_D", freeze = fz)
  d5 <- DRDID::drdid_imp_panel(y1 = fr$y1_cap, y0 = fr$y0_cap, D = as.numeric(fr$treat), covariates = drdid_covariates(fr), trim.level = 0.25)
  ps <- pmin(DRDID:::pscore.cal(as.numeric(fr$treat), drdid_covariates(fr), rep(1, nrow(fr)), nrow(fr))$pscore, 1 - 1e-06)
  stopifnot(t5$n_controls_trimmed == sum(!fr$treat & ps >= 0.25), t5$n_controls_trimmed > 0, t5$att == d5$ATT, t5$att != e$att)
})
test("fix round 1: secondary-IPT (ps.flag = 1) is recorded, not aborted; trim counts survive package warnings", {
  r <- drdid_estimate(fr, "capped_D")
  stopifnot("ipt_secondary_algorithm" %in% names(r), identical(r$ipt_secondary_algorithm, r$ps_flag == 1L), !is.na(r$n_controls_trimmed))
  X <- drdid_covariates(fr); D <- as.numeric(fr$treat)
  ps_warn <- function(D, int.cov, i.weights, n) { warning("trust algorithm did not converge to a verified IPT fit"); list(pscore = rep(c(.995, .5), length.out = length(D)), flag = 1) }
  cnt <- drdid_trim_counts(D, X, 0.99, ps_fn = ps_warn)
  stopifnot(cnt[["ctrl"]] == sum(D == 0 & rep(c(.995, .5), length.out = length(D)) >= .99), cnt[["exp"]] == sum(D == 1 & rep(c(.995, .5), length.out = length(D)) >= .99), cnt[["ctrl"]] > 0)
  stopifnot(is.na(drdid_trim_counts(D, X, 0.99, ps_fn = function(...) stop("x"))[["ctrl"]]))
  stopifnot(grepl("ONLY controls", DRDID_FREEZE$trimming), grepl("REMAIN in the IPT", DRDID_FREEZE$trimming), grepl("pscore/\\(1-pscore\\)", DRDID_FREEZE$trimming))
})
test("failed / non-convergent fits are reported 'not estimated' with the reason; the model is never changed", {
  g <- copy(fr); g[, never_sent_force := 0]; g[, age := 1e6]                 # never_sent column becomes constant 0 => singular design
  r <- drdid_estimate(g, "capped_D")
  stopifnot(r$status == "not estimated", nzchar(r$reason), is.na(r$att), is.na(r$lo))
  g2 <- copy(fr); g2[1, y1_cap := NA_real_]
  r2 <- drdid_estimate(g2, "capped_D"); stopifnot(r2$status == "not estimated", grepl("non-finite", r2$reason))
  g3 <- copy(fr); g3[, treat := FALSE]
  r3 <- drdid_estimate(g3, "capped_D"); stopifnot(r3$status == "not estimated")
})
test("frozen implementation unavailable => 'not estimated', never substituted", {
  fz <- modifyList(DRDID_FREEZE, list(version = "9.9.9"))
  r <- drdid_estimate(fr, "capped_D", freeze = fz); stopifnot(r$status == "not estimated", grepl("differs from frozen", r$reason), is.na(r$att))
  fz2 <- modifyList(DRDID_FREEZE, list(package = "DRDIDnotinstalled"))
  r2 <- drdid_estimate(fr, "capped_D", freeze = fz2); stopifnot(r2$status == "not estimated", grepl("not installed", r2$reason))
  fz3 <- modifyList(DRDID_FREEZE, list(formals = c("y1", "y0")))
  stopifnot(drdid_estimate(fr, "capped_D", freeze = fz3)$status == "not estimated")
  stopifnot(!any(grepl("lm\\(|glm\\(|feols\\(", deparse(drdid_estimate))))      # no in-house substitute estimator inside the row function
})
test("drdid_frame: B, D, caps and post/baseline means computed exactly from integer base units; B <= 0 dropped and counted", {
  mk <- function(u, usdc, usdt) rbind(data.table(uid = u, day_index = DRDID_BASE_DAYS, usdc_units = usdc, usdt_units = usdt),
                                      data.table(uid = u, day_index = DRDID_POST_DAYS, usdc_units = usdc * 2, usdt_units = usdt))
  w <- rbind(mk("0xa", 3e6, 1e6), mk("0xb", 0, 0))
  cv <- data.table(address = c("0xa", "0xb"), arm = c("both", "control"), transfer_count_total = c(3L, 4L), eth_balance_wei_units = c("1000000000000000000", "0"), wallet_age_blocks = c(NA, 100))
  f <- drdid_frame(w, cv, c("0xa", "0xb"), c(-10, 10))
  # B(0xa) = 4 dollars; D baseline = (3-1)/4 = .5; D post = (6-1)/4 = 1.25
  stopifnot(nrow(f) == 1, f$address == "0xa", f$B == 4, f$y0_unc == .5, f$y1_unc == 1.25, attr(f, "n_dropped_B") == 1, is.na(f$age), f$eth_wei == 1e18)
  f2 <- drdid_frame(w, cv, c("0xa", "0xb"), c(-10, 1))
  stopifnot(f2$y1_cap == 1, f2$y0_cap == .5)
  stopifnot(fails <- inherits(try(drdid_frame(w[uid != "0xb" | day_index < 30], cv, c("0xa", "0xb"), c(-1, 1)), silent = TRUE), "try-error"))
})

})
if (standalone) cat(sprintf("ALL %d DRDID TESTS PASSED\n", .t_total))
