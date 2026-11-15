#!/usr/bin/env Rscript
# EXPLORATORY, POST-RESULTS, dated 2026-10-30. Real-data check of the closed-form o1_fit (plan addendum A4) against the O3 feols fits on the SIGNED window only.
# Usage: Rscript exploratory/tests/check_o1_closedform_real.R --repo-root <tree holding out/recovery>      (stdout only; writes nothing; never reads out/exploratory/o1-*)
# Rebuilds d_all exactly as exploratory/o3_pretrend.R (signed post panel, own caps, 8 Aug excluded), runs o1_fit(ref 59) for D, A x uid, stratum on PRE_DAYS (0..59) and
# ALL_DAYS (0..59, 61..95), compares with out/exploratory/o3-2026-10-28/o3_vcov_{D,A}_{uid,stratum}_{pre,all}.rds (coef, vcov embedded on the days) and, for uid/pre, with the
# signed post_pretrend.csv coef and se (days 0..58). Exit 1 if max|db| > 1e-7 or any V relative difference > 1e-6 (entries >= 1e-3 max|V|; scaled difference also printed).
argv <- commandArgs(TRUE); i <- match("--repo-root", argv)
if (is.na(i) || i == length(argv)) stop("--repo-root is required", call. = FALSE)
main_root <- normalizePath(argv[i + 1L], mustWork = TRUE)
script_root <- local({ a <- grep("^--file=", commandArgs(FALSE), value = TRUE); normalizePath(file.path(dirname(sub("^--file=", "", a[1])), "..", "..")) })
setwd(script_root)
for (f in file.path(script_root, "R", paste0(c("utils", "decide", "cem", "estimate", "pre", "presign", "drdid", "post"), ".R"))) source(f)
stopifnot(requireNamespace("jsonlite", quietly = TRUE)); library(jsonlite)
fixest::setFixest_nthreads(1L); data.table::setDTthreads(1L)
O1_NO_MAIN <- TRUE; source(file.path(script_root, "exploratory", "o1_estimate.R"))

REC <- file.path(main_root, "out", "recovery"); SIGNED <- file.path(REC, "post/r-gen-M-post"); O3DIR <- file.path(main_root, "out/exploratory/o3-2026-10-28")
inp <- c(pre_covariates = "main/gen-M/covariates.csv", pre_panel = "main/gen-M/wallet_coin_day_pre.csv", post_panel = "post/gen-M-post/post_wallet_coin_day.csv",
         match_membership = "presign/main-gen-M-20261027/match_membership.csv", analysis_parameters = "presign/main-gen-M-20261027/analysis_parameters.json",
         eligible_frame_coverage = "presign/main-gen-M-20261027/eligible_frame_coverage.csv")
inp <- setNames(file.path(REC, inp), names(inp))
params <- po_read_params(inp[["analysis_parameters"]]); po_check_population(params$population_id, params$generation_id)
mem <- po_read_membership(inp[["match_membership"]], params)
pre <- po_pre_stage(dirname(inp[["pre_covariates"]]), params, mem, inp[["eligible_frame_coverage"]])
post <- po_read_post_panel(inp[["post_panel"]], params, required_addresses = pre$need_addr, exact_addresses = pre$retained)
panel <- po_panel(rbind(pre$wide_pre[uid %in% pre$need_addr], post$wide), mem$matched)
d_all <- prep_est(panel, POST_EVENT_DAY, "own"); d_all[, ex := as.numeric(treat)]
PRE_DAYS <- MAIN_BASELINE_DAYS; ALL_DAYS <- c(PRE_DAYS, POST_DAYS)
pt <- fread(file.path(SIGNED, "post_pretrend.csv"))

fail <- FALSE
for (o in c("D", "A")) for (cl in c("uid", "stratum")) for (win in c("pre", "all")) {
  days <- if (win == "pre") PRE_DAYS else ALL_DAYS
  t0 <- Sys.time(); n <- o1_fit(d_all[day_index %in% days], o, cl, ref = 59L); el <- as.numeric(difftime(Sys.time(), t0, units = "secs"))
  if (!is.null(n$error)) { cat(sprintf("%s %s %s: o1_fit ERROR %s\n", o, cl, win, n$error)); fail <- TRUE; next }
  r <- readRDS(file.path(O3DIR, sprintf("o3_vcov_%s_%s_%s.rds", o, cl, win)))
  stopifnot(identical(as.integer(r$day_index), as.integer(n$days)))
  db <- max(abs(n$b - r$coef)); big <- abs(r$vcov) >= 1e-3 * max(abs(r$vcov))
  dvr <- max(abs(n$V - r$vcov)[big] / abs(r$vcov)[big]); dvs <- max(abs(n$V - r$vcov)) / max(abs(r$vcov))
  dse <- max(abs(sqrt(diag(n$V)) - sqrt(diag(r$vcov))))
  line <- sprintf("%s %-7s %s: G %d (O3 %d) | max|db| %.2e | V rel %.2e, scaled %.2e | max|dse| %.2e", o, cl, win, n$G, r$n_clusters, db, dvr, dvs, dse)
  if (cl == "uid" && win == "pre") {
    s <- pt[outcome == o & day_index %in% setdiff(PRE_DAYS, 59L)][order(day_index)]; stopifnot(nrow(s) == length(PRE_DAYS) - 1L)
    k <- as.character(s$day_index); dc <- max(abs(n$b[k] - s$coef)); ds <- max(abs(sqrt(diag(n$V))[k] - s$se))
    line <- paste0(line, sprintf(" | vs signed csv: max|dcoef| %.2e, max|dse| %.2e", dc, ds))
    if (dc > 1e-7) fail <- TRUE
  }
  cat(line, sprintf("| %.1fs\n", el))
  if (n$G != r$n_clusters || db > 1e-7 || dvr > 1e-6) { fail <- TRUE; cat("  ^ FAIL\n") }
}
cat(if (fail) "CHECK FAILED\n" else "CHECK PASSED: closed form agrees with the O3 feols fits and the signed pre-trend CSV\n")
quit(status = as.integer(fail))
