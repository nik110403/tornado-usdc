#!/usr/bin/env Rscript
# EXPLORATORY, POST-RESULTS, dated 2026-11-12; not confirmatory; does not reclassify H1.
# O4 (journal-review tribunal chg-74cd74bd, seat 3 F1/F2/F3): capped coin legs, cap grid, cap-free outcomes, top-k leave-out, in-time rank.
# Spec (binding, hashed): addenda/2026-11-12_exploratory-plan-o4-caps-legs.md. Rows 1-5 as written there; nothing in the definitions is changed.
# Usage: Rscript exploratory/o4_caps.R --repo-root <tree holding out/recovery> --outdir <new dir>   (both required; no defaults)
# Rebuilds the SIGNED post-run panel with the committed R/ helpers (as exploratory/o3_pretrend.R); caps for the grid are computed in
# exploratory/o4_lib.R like cap_values_all (R/estimate.R) with a probs argument. Writes only to --outdir (out/exploratory/<dir>).
t0 <- Sys.time()
EXPLORATORY_DATE <- "2026-11-12"
BANNER <- sprintf("EXPLORATORY, POST-RESULTS, dated %s; not confirmatory; does not reclassify H1", EXPLORATORY_DATE)

# ---- args -------------------------------------------------------------------------------------------------------------
argv <- commandArgs(TRUE)
opt <- function(name) {
  i <- match(paste0("--", name), argv)
  if (is.na(i) || i == length(argv) || startsWith(argv[i + 1L], "--")) stop(sprintf("--%s is required (no default)", name), call. = FALSE)
  argv[i + 1L]
}
known <- c("--repo-root", "--outdir")
if (length(bad <- setdiff(argv[startsWith(argv, "--")], known))) stop("unknown argument(s): ", paste(bad, collapse = " "), call. = FALSE)
main_root <- normalizePath(opt("repo-root"), mustWork = TRUE)
lex_clean <- function(p) {
  p <- path.expand(p); if (!startsWith(p, "/")) p <- file.path(getwd(), p)
  parts <- strsplit(p, "/+")[[1]]; out <- character()
  for (q in parts[nzchar(parts)]) if (q == "..") out <- head(out, -1L) else if (q != ".") out <- c(out, q)
  paste0("/", paste(out, collapse = "/"))
}
script_root <- local({
  a <- grep("^--file=", commandArgs(FALSE), value = TRUE)
  normalizePath(file.path(dirname(sub("^--file=", "", a[1])), ".."))
})
source(file.path(script_root, "exploratory", "o4_lib.R"))   # function definitions only (o4_out_guard needs nothing else)
out_guard <- function(p) o4_out_guard(p, main_root)
outdir <- lex_clean(opt("outdir")); out_guard(outdir)
if (dir.exists(outdir) && length(list.files(outdir, all.files = TRUE, no.. = TRUE))) stop("outdir must be new or empty: ", outdir, call. = FALSE)
local({
  a <- outdir; rest <- character()
  while (!file.exists(a) && a != "/") { rest <- c(basename(a), rest); a <- dirname(a) }
  out_guard(do.call(file.path, as.list(c(normalizePath(a, mustWork = TRUE), rest))))
})
dir.create(outdir, recursive = TRUE, showWarnings = FALSE)
outdir <- normalizePath(outdir, mustWork = TRUE); out_guard(outdir)

setwd(script_root)
code_files <- file.path(script_root, "R", paste0(c("utils", "decide", "cem", "estimate", "pre", "presign", "drdid", "post"), ".R"))
for (f in code_files) source(f)
stopifnot(requireNamespace("jsonlite", quietly = TRUE))
library(jsonlite)
fixest::setFixest_nthreads(1L)
data.table::setDTthreads(1L)

REC <- file.path(main_root, "out", "recovery")
inp_paths <- c(
  pre_covariates = file.path(REC, "main/gen-M/covariates.csv"),
  pre_panel = file.path(REC, "main/gen-M/wallet_coin_day_pre.csv"),
  post_panel = file.path(REC, "post/gen-M-post/post_wallet_coin_day.csv"),
  match_membership = file.path(REC, "presign/main-gen-M-20261027/match_membership.csv"),
  analysis_parameters = file.path(REC, "presign/main-gen-M-20261027/analysis_parameters.json"),
  eligible_frame_coverage = file.path(REC, "presign/main-gen-M-20261027/eligible_frame_coverage.csv"))
SIGNED <- file.path(REC, "post/r-gen-M-post")
O1PATHS <- file.path(main_root, "out/exploratory/o1-est-2026-10-30-r5-provenance/o1_paths.csv")

# ---- 1. input hashes against the signed trace ------------------------------------------------------------------------
trace <- fromJSON(file.path(SIGNED, "post_trace.json"), simplifyVector = FALSE)
hash_tab <- rbindlist(lapply(names(inp_paths), function(k) {
  got <- sha256_file(inp_paths[[k]]); want <- trace$input_sha256[[k]]
  data.table(input = k, path = sub(paste0(main_root, "/"), "", inp_paths[[k]], fixed = TRUE), sha256 = got, signed_sha256 = want, ok = identical(got, want))
}))
print(hash_tab[, .(input, ok)])
if (!all(hash_tab$ok)) stop("input sha256 differs from post_trace.json input_sha256: stop", call. = FALSE)

# ---- 2. rebuild the signed post panel (same call chain as R/post.R run_post) ---------------------------------------------
params <- po_read_params(inp_paths[["analysis_parameters"]]); po_check_population(params$population_id, params$generation_id)
mem <- po_read_membership(inp_paths[["match_membership"]], params)
pre <- po_pre_stage(dirname(inp_paths[["pre_covariates"]]), params, mem, inp_paths[["eligible_frame_coverage"]])
post <- po_read_post_panel(inp_paths[["post_panel"]], params, required_addresses = pre$need_addr, exact_addresses = pre$retained)
wide_all <- rbind(pre$wide_pre[uid %in% pre$need_addr], post$wide)
panel <- po_panel(wide_all, mem$matched)
rm(wide_all, post, pre); invisible(gc())
cat(sprintf("panel: %d rows, %d wallets\n", nrow(panel), uniqueN(panel$uid)))

EV <- POST_EVENT_DAY                                  # 60 = 8 Aug 2022 (dropped)
stopifnot(EV == 60L, PRE_PERIOD_START + 59L == as.Date("2022-08-07"), PRE_PERIOD_START + 61L == as.Date("2022-08-09"))
D7 <- 59L; D9 <- 61L                                  # 7 Aug, 9 Aug
signed_csv <- function(f) fread(file.path(SIGNED, f))
TOL <- 1e-9
assertions <- list()
assert_eq <- function(id, got, want, tol = TOL) {
  d <- abs(got - want); ok <- is.finite(d) && d <= tol
  assertions[[length(assertions) + 1L]] <<- data.table(id = id, got = got, expected = want, abs_diff = d, tol = tol, pass = ok)
  if (!ok) stop(sprintf("ASSERTION FAILED %s: got %.15g expected %.15g |diff| %.3g (tol %g)", id, got, want, d, tol), call. = FALSE)
}
wcsv <- function(dt, f) fwrite(cbind(data.table(banner = BANNER), dt), file.path(outdir, f))

# ---- 3. estimation frame (uncapped legs, D, A; 8 Aug dropped) + B -----------------------------------------------------------
d0 <- prep_est(panel, EV, "none")                       # D, A, usdc_B, usdt_B uncapped; tp; stratum
d0 <- merge(d0, unique(panel[, .(uid, B)]), by = "uid", sort = FALSE)
stopifnot(uniqueN(panel$uid) == nrow(unique(panel[, .(uid, B)])))
caps_signed <- cap_values_all(panel, EV)
assert_eq("o4_caps_1_99_D_lo", o4_caps(panel, EV, c(.01, .99))$D[1], caps_signed$D[1])
assert_eq("o4_caps_1_99_D_hi", o4_caps(panel, EV, c(.01, .99))$D[2], caps_signed$D[2])
assert_eq("o4_caps_1_99_legs_lo", o4_caps(panel, EV, c(.01, .99))$legs[1], caps_signed$legs[1])
assert_eq("o4_caps_1_99_legs_hi", o4_caps(panel, EV, c(.01, .99))$legs[2], caps_signed$legs[2])
est_signed <- signed_csv("post_estimates.csv")
cmp_signed <- function(id, r, o, cp) {
  for (cl in c("uid", "stratum")) {
    s <- est_signed[outcome == o & cap == cp & cluster == cl]; stopifnot(nrow(s) == 1L); x <- r[cluster == cl]
    assert_eq(sprintf("%s_%s_beta", id, cl), x$beta, s$beta); assert_eq(sprintf("%s_%s_se", id, cl), x$se, s$se)
  }
}
RES <- function(r, ...) { a <- data.table(...); cbind(a[rep(1L, nrow(r))], r) }

# ---- ROW 1: capped legs at the pre-specified leg_pooled pair (1/99 pooled over USDC/B and USDT/B) ---------------------------
cat("row 1\n")
P0 <- c(.01, .99)
dc <- o4_capped(d0, o4_caps(panel, EV, P0))
r1 <- list()
add1 <- function(outcome, y, d = dc, def) { r <- o4_fit(d, y); r1[[length(r1) + 1L]] <<- RES(r, outcome = outcome, definition = def); r }
f_cu <- add1("cu_capped_usdc_B", "cu", def = "clamp(USDC/B) at pooled 1/99 leg cut-points")
f_ct <- add1("ct_capped_usdt_B", "ct", def = "clamp(USDT/B) at pooled 1/99 leg cut-points")
f_Dl <- add1("Dleg_cu_minus_ct", "Dleg", def = "cu - ct = signed leg_pooled D, direct fit")
f_Al <- add1("Aleg_cu_plus_ct", "Aleg", def = "cu + ct = signed leg_pooled A, direct fit")
f_u1 <- add1("usdc_B_uncapped", "usdc_B", def = "USDC/B uncapped")
f_u2 <- add1("usdt_B_uncapped", "usdt_B", def = "USDT/B uncapped")
sg <- function(f) est_signed[outcome == "D" & cap == "leg_pooled" & cluster == "uid"]
for (cl in c("uid", "stratum")) {
  b <- function(f) f[cluster == cl]$beta
  assert_eq(sprintf("row1_cu_minus_ct_beta_vs_signed_leg_pooled_D_%s", cl), b(f_cu) - b(f_ct), est_signed[outcome == "D" & cap == "leg_pooled" & cluster == cl]$beta)
  assert_eq(sprintf("row1_cu_plus_ct_beta_vs_signed_leg_pooled_A_%s", cl), b(f_cu) + b(f_ct), est_signed[outcome == "A" & cap == "leg_pooled" & cluster == cl]$beta)
}
cmp_signed("row1_Dleg_direct", f_Dl, "D", "leg_pooled"); cmp_signed("row1_Aleg_direct", f_Al, "A", "leg_pooled")
cmp_signed("row1_usdc_B_uncapped", f_u1, "usdc_B", "none"); cmp_signed("row1_usdt_B_uncapped", f_u2, "usdt_B", "none")
# lincomb rows (beta only: separate fits carry no cross-covariance)
r1_lin <- rbindlist(lapply(c("uid", "stratum"), function(cl) data.table(
  cluster = cl, cu_minus_ct_beta = f_cu[cluster == cl]$beta - f_ct[cluster == cl]$beta, signed_leg_pooled_D_beta = est_signed[outcome == "D" & cap == "leg_pooled" & cluster == cl]$beta,
  cu_plus_ct_beta = f_cu[cluster == cl]$beta + f_ct[cluster == cl]$beta, signed_leg_pooled_A_beta = est_signed[outcome == "A" & cap == "leg_pooled" & cluster == cl]$beta)))
wcsv(rbindlist(r1), "o4_row1_legs.csv"); wcsv(r1_lin, "o4_row1_lincomb_check.csv")

# ---- ROW 2: cap grid, own and leg_pooled rules, 35-day and 7->9 Aug -----------------------------------------------------------
cat("row 2\n")
GRID <- list(`0.1/99.9` = c(.001, .999), `0.5/99.5` = c(.005, .995), `1/99` = c(.01, .99), `2.5/97.5` = c(.025, .975), `5/95` = c(.05, .95))
r2 <- list(); r2cut <- list(); r2clip <- list()
base_days <- attr(panel, "baseline_days")
d0[, period := fifelse(day_index %in% base_days, "baseline", "post")]
clip_tab <- function(d, col, lo, hi, what, pair, rule) {
  x <- d[[col]]
  rbind(
    d[, .(share_below = mean(x < lo), share_above = mean(x > hi), n_wallet_days = .N), by = .(period)][, `:=`(weighting = "unweighted", arm = "all")],
    d[, .(share_below = sum(w * (x < lo)) / sum(w), share_above = sum(w * (x > hi)) / sum(w), n_wallet_days = .N), by = .(period, arm = fifelse(treat, "exposed", "control"))][, weighting := "CEM-weighted"])[
    , `:=`(variable = what, pair = pair, rule = rule, cut_lo = lo, cut_hi = hi)]
}
for (pn in names(GRID)) {
  cp <- o4_caps(panel, EV, GRID[[pn]]); dd <- o4_capped(d0, cp)
  r2cut[[length(r2cut) + 1L]] <- data.table(pair = pn, D_lo = cp$D[1], D_hi = cp$D[2], A_lo = cp$A[1], A_hi = cp$A[2], legs_lo = cp$legs[1], legs_hi = cp$legs[2], n_baseline_wallet_days = cp$n_wallet_days)
  for (v in list(list("D", cp$D, "own"), list("A", cp$A, "own"))) r2clip[[length(r2clip) + 1L]] <- clip_tab(dd, v[[1]], v[[2]][1], v[[2]][2], v[[1]], pn, "own")
  for (v in list(list("usdc_B", cp$legs, "leg_pooled"), list("usdt_B", cp$legs, "leg_pooled"))) r2clip[[length(r2clip) + 1L]] <- clip_tab(dd, v[[1]], v[[2]][1], v[[2]][2], v[[1]], pn, "leg_pooled")
  for (ru in c("own", "leg_pooled")) for (o in c("D", "A")) {
    y <- if (ru == "own") c(D = "Down", A = "Aown")[[o]] else c(D = "Dleg", A = "Aleg")[[o]]
    for (wn in c("35day", "move_7to9aug")) {
      r <- o4_fit(dd, y, if (wn == "35day") NULL else c(D7, D9))
      r2[[length(r2) + 1L]] <- RES(r, rule = ru, pair = pn, outcome = o, window = wn)
      if (pn == "1/99" && wn == "35day") cmp_signed(sprintf("row2_signed_%s_%s", ru, o), r, o, ru)
    }
  }
}
for (o in c("D", "A")) for (wn in c("35day", "move_7to9aug")) {   # none: identical under both rules (no clamping)
  r <- o4_fit(d0, o, if (wn == "35day") NULL else c(D7, D9))
  for (ru in c("own", "leg_pooled")) r2[[length(r2) + 1L]] <- RES(r, rule = ru, pair = "none", outcome = o, window = wn)
  if (wn == "35day") cmp_signed(sprintf("row2_signed_none_%s", o), r, o, "none")
}
grid2 <- rbindlist(r2)
assert_eq("row2_uncapped_D_uid_beta_0.817144", grid2[pair == "none" & rule == "own" & outcome == "D" & window == "35day" & cluster == "uid"]$beta, 0.817144, 5e-7)
wcsv(grid2, "o4_row2_grid.csv"); wcsv(rbindlist(r2cut), "o4_row2_cutpoints.csv"); wcsv(rbindlist(r2clip), "o4_row2_clip_incidence.csv")

# ---- ROW 3: cap-free outcomes ---------------------------------------------------------------------------------------------------
cat("row 3\n")
d3 <- copy(d0)
d3[, `:=`(o_usdc_pos = as.numeric(usdc_B > 0), o_emptied = as.numeric(usdc_B == 0 & usdt_B == 0), o_asinh_gap = asinh((usdc_B - usdt_B) * B))]
r3 <- list()
for (o in list(c("o_usdc_pos", "1{USDC > 0}"), c("o_emptied", "1{USDC + USDT = 0} (address emptied)"), c("o_asinh_gap", "asinh(USDC - USDT), dollars"))) for (wn in c("35day", "move_7to9aug")) {
  r <- o4_fit(d3, o[1], if (wn == "35day") NULL else c(D7, D9))
  r3[[length(r3) + 1L]] <- RES(r, outcome = o[2], window = wn)
}
r3 <- rbindlist(r3); wcsv(r3, "o4_row3_capfree_outcomes.csv")

# ---- ROW 4: top-k leave-out of uncapped D ------------------------------------------------------------------------------------------
cat("row 4\n")
ct <- o4_contrib(d0, "D", EV)
beta_full <- grid2[pair == "none" & rule == "own" & outcome == "D" & window == "35day" & cluster == "uid"]$beta
assert_eq("row4_sum_of_contributions_equals_uncapped_beta", sum(ct$contribution), beta_full)
ct[, abs_contribution := abs(contribution)]
setorder(ct, -abs_contribution, uid); ct[, rank := .I]
wcsv(ct[, .(rank, uid, treat, stratum, w, delta, contribution, abs_contribution, share_of_beta = contribution / beta_full)], "o4_row4_contributions.csv")
top <- ct[rank <= 10]
top10 <- data.table(beta_uncapped_D = beta_full, sum_contribution_top10 = sum(top$contribution), share_of_beta_top10 = sum(top$contribution) / beta_full,
                    share_abs_top10_of_sum_abs = sum(top$abs_contribution) / sum(ct$abs_contribution), n_treated_in_top10 = sum(top$treat), n_control_in_top10 = sum(!top$treat))
wcsv(top10, "o4_row4_top10_share.csv")
r4 <- list(RES(grid2[pair == "none" & rule == "own" & outcome == "D" & window == "35day", .(cluster, beta, se, lo, hi, df, p, n_obs, n_clusters)], k = 0L))
for (k in c(1L, 5L, 10L, 20L, 50L, 100L)) {
  dk <- d0[!uid %in% ct[rank <= k]$uid]
  r4[[length(r4) + 1L]] <- RES(o4_fit(dk, "D"), k = k)
}
r4 <- rbindlist(r4)
r4 <- r4[order(k, cluster)]
wcsv(r4, "o4_row4_leaveout.csv")

# ---- ROW 5: in-time rank (descriptive, uncalibrated) ------------------------------------------------------------------------------------------
cat("row 5\n")
o1 <- fread(O1PATHS)[variant == "capped" & outcome == "D" & cluster == "uid"]
stopifnot(nrow(o1) == 160L, all(o1$day_index == -100:59))
bo1 <- setNames(o1$coef, o1$day_index)
pt <- signed_csv("post_pretrend.csv")[outcome == "D"]; tp_ <- signed_csv("post_time_path.csv")[outcome == "D"]
bs <- setNames(numeric(0), character(0))
bs <- c(setNames(pt$coef, pt$day_index), setNames(tp_$coef, tp_$day_index))   # 0..58, 59 (ref, 0), 61..95 (reference row in time_path carries 59 = 0 too)
bs <- bs[!duplicated(names(bs))]
stopifnot(setequal(as.integer(names(bs)), c(0:59, 61:95)), bs[["59"]] == 0)
real <- mean(bs[as.character(61:95)]) - mean(bs[as.character(0:59)])
assert_eq("row5_real_contrast_vs_signed_beta_pre_post_fits_mixed", real, est_signed[outcome == "D" & cap == "own" & cluster == "uid"]$beta, 1e-3)
fake_days <- (-100L + 60L):(59L - 35L)               # -40..24: 60 days before and 35 days after all inside the path
fake <- data.table(fake_day = fake_days, date = format(PRE_PERIOD_START + fake_days), contrast = vapply(fake_days, function(s) o4_contrast(bo1, s), 0))
fake[, abs_contrast := abs(contrast)]
rk <- data.table(real_contrast = real, n_fake_dates = nrow(fake),
                 rank_by_value_1_most_negative = 1L + sum(fake$contrast < real), rank_by_abs_1_largest = 1L + sum(fake$abs_contrast > abs(real)),
                 n_in_ranking = nrow(fake) + 1L, fake_min = min(fake$contrast), fake_max = max(fake$contrast), fake_min_date = fake$date[which.min(fake$contrast)],
                 max_abs_diff_o1_vs_signed_pre_coefs_days0to58 = max(abs(bo1[as.character(0:58)] - bs[as.character(0:58)])),
                 note = "DESCRIPTIVE, UNCALIBRATED: overlapping windows; not a p-value. real contrast from signed post_pretrend (days 0..58, ref 59 = 0) + post_time_path (61..95); fake contrasts from o1_paths capped D uid (days -100..59)")
wcsv(fake, "o4_row5_fake_contrasts.csv"); wcsv(rk, "o4_row5_rank.csv")

# ---- outputs -----------------------------------------------------------------------------------------------------------------------------
wcsv(rbindlist(assertions), "o4_assertions.csv"); wcsv(hash_tab, "o4_input_hashes.csv")
writeLines(c(sprintf("# %s", BANNER), capture.output(sessionInfo()), "", "# sha256 of sourced committed R code and of this script + lib:",
             vapply(c(code_files, file.path(script_root, "exploratory", c("o4_caps.R", "o4_lib.R"))), function(f) sprintf("%s  %s", sha256_file(f), sub(paste0(script_root, "/"), "", f, fixed = TRUE)), "")),
           file.path(outdir, "sessionInfo.txt"))

fmt <- function(x, d = 4) formatC(x, format = "f", digits = d)
cell <- function(r) sprintf("%s [%s, %s]", fmt(r$beta), fmt(r$lo), fmt(r$hi))
tab <- function(dt, labfun) {   # dt has uid and stratum rows in the same order; one md row per uid row
  u <- dt[cluster == "uid"]; s <- dt[cluster == "stratum"]; stopifnot(nrow(u) == nrow(s))
  vapply(seq_len(nrow(u)), function(i) sprintf("| %s | %s | %s |", labfun(u[i]), cell(u[i]), cell(s[i])), "")
}
hdr <- c("| row | beta [95% CI], ~uid | beta [95% CI], ~stratum |", "|---|---|---|")
sm <- c(sprintf("# %s", BANNER), "",
        sprintf("O4 `exploratory/o4_caps.R` (plan `addenda/2026-11-12_exploratory-plan-o4-caps-legs.md`). Run date %s; runtime %.1f min; single thread. All CIs t with df = clusters - 1 (uid 15,982 clusters; stratum 692).", format(Sys.Date()), as.numeric(difftime(Sys.time(), t0, units = "mins"))),
        sprintf("Inputs: all %d input sha256 equal post_trace.json. Assertions (all passed): %d (o4_assertions.csv).", nrow(hash_tab), length(assertions)), "",
        "## Row 1 - capped legs at the signed leg_pooled pair (pooled 1/99)", "", hdr,
        tab(rbindlist(r1)[order(match(outcome, unique(outcome)), cluster)], function(u) u$outcome),
        "", sprintf("cu - ct (separate fits) = %s (uid), signed leg_pooled D = %s; cu + ct = %s, signed leg_pooled A = %s (o4_row1_lincomb_check.csv).", fmt(r1_lin[cluster == "uid"]$cu_minus_ct_beta, 6), fmt(r1_lin[cluster == "uid"]$signed_leg_pooled_D_beta, 6), fmt(r1_lin[cluster == "uid"]$cu_plus_ct_beta, 6), fmt(r1_lin[cluster == "uid"]$signed_leg_pooled_A_beta, 6)), "",
        "## Row 2 - cap grid (own = D and A at their own percentiles; leg_pooled = pooled leg pair)", "",
        { cc <- rbindlist(r2cut); c("Cut-points (type 7, pooled unweighted baseline wallet-days):", "", "| pair | D lo | D hi | A lo | A hi | legs lo | legs hi |", "|---|---|---|---|---|---|---|",
            sprintf("| %s | %s | %s | %s | %s | %s | %s |", cc$pair, fmt(cc$D_lo), fmt(cc$D_hi), fmt(cc$A_lo), fmt(cc$A_hi), fmt(cc$legs_lo), fmt(cc$legs_hi))) }, "",
        "Clip incidence (CEM-weighted share of wallet-days outside the cut-points) is in o4_row2_clip_incidence.csv.", "")
for (o in c("D", "A")) for (wn in c("35day", "move_7to9aug")) {
  g <- grid2[outcome == o & window == wn]
  sm <- c(sm, sprintf("### Row 2: %s, %s", o, if (wn == "35day") "35-day beta" else "7 -> 9 Aug move (days 59 and 61 only)"), "", hdr,
          unlist(lapply(c("own", "leg_pooled"), function(ru) { x <- g[rule == ru]; x[, pair := factor(pair, c(names(GRID), "none"))]; setorder(x, pair, cluster)
            tab(x, function(u) sprintf("%s, %s", ru, u$pair)) })), "")
}
flip <- function(ru) {
  g <- grid2[outcome == "D" & window == "35day" & cluster == "uid" & rule == ru]; g[, pair := factor(pair, c(names(GRID), "none"))]; setorder(g, pair)
  sprintf("%s: %s", ru, paste(sprintf("%s %s (%s)", g$pair, fmt(g$beta, 4), ifelse(g$beta < 0, "-", "+")), collapse = "; "))
}
sm <- c(sm, "### Flip of the sign of capped D (35-day beta, ~uid) across the cap grid", "", paste0("- ", flip("own")), paste0("- ", flip("leg_pooled")),
        "", "## Row 3 - cap-free outcomes (no cap)", "", hdr, tab(r3[order(window, outcome, cluster)], function(u) sprintf("%s, %s", u$outcome, u$window)), "",
        "## Row 4 - top-k leave-out, uncapped D (35-day)", "", hdr,
        tab(r4[order(k, cluster)], function(u) sprintf("drop top %d wallets by |contribution|", u$k)), "",
        sprintf("Sum of per-wallet contributions = %s (uncapped beta %s). Top 10 by |contribution|: share of the uncapped beta = %s (sum of their contributions %s; %d treated, %d control); their share of the sum of |contributions| = %s.",
                fmt(sum(ct$contribution), 6), fmt(beta_full, 6), fmt(top10$share_of_beta_top10, 4), fmt(top10$sum_contribution_top10, 4), top10$n_treated_in_top10, top10$n_control_in_top10, fmt(top10$share_abs_top10_of_sum_abs, 4)), "",
        "## Row 5 - in-time rank of the real 60/35 contrast (descriptive, uncalibrated; overlapping windows; not a p-value)", "",
        sprintf("Real contrast (mean of 35 post days minus mean of 60 baseline days, capped D) = %s. Fake dates: %d (days %d..%d). Rank among %d contrasts: by value (1 = most negative) %d; by |value| (1 = largest) %d. Fake range [%s, %s] (min on %s). Max |o1 - signed| over pre coefficients days 0..58: %s.",
                fmt(rk$real_contrast, 6), nrow(fake), min(fake_days), max(fake_days), rk$n_in_ranking, rk$rank_by_value_1_most_negative, rk$rank_by_abs_1_largest, fmt(rk$fake_min, 6), fmt(rk$fake_max, 6), rk$fake_min_date, formatC(rk$max_abs_diff_o1_vs_signed_pre_coefs_days0to58, format = "g", digits = 3)), "",
        "## Reading rule", "", "Exploratory, after the results. The question is only whether the capped sign of D holds at moderate caps (0.5/99.5 to 5/95) and where it flips; no cap is chosen and the signed H1 verdict (capped D, beta -0.152) stands.")
writeLines(sm, file.path(outdir, "summary.md"))
fl <- setdiff(sort(list.files(outdir)), c("summary.md", "MANIFEST.sha256"))
writeLines(vapply(fl, function(f) sprintf("%s  %s", sha256_file(file.path(outdir, f)), f), ""), file.path(outdir, "MANIFEST.sha256"))
cat("done\n")
