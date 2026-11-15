#!/usr/bin/env Rscript
# EXPLORATORY, POST-RESULTS, dated 2026-10-28; not confirmatory; does not reclassify H1.
# O3 (pre-trend tribunal 2026-10-28, refuter R5): committed, dated, hashed script behind the transcript-only numbers.
# Usage: Rscript exploratory/o3_pretrend.R --repo-root <tree holding out/recovery> --outdir <new dir>   (both required; no defaults)
# Rebuilds the SIGNED post-run panel with the committed R/ helpers (po_pre_stage, po_read_post_panel, po_panel; nothing re-implemented),
# asserts it against the signed post_estimates / post_pretrend / post_time_path, then derives the exploratory families from the
# event-study coefficients and their full covariance. Writes only to --outdir (under --repo-root/out only out/exploratory/<dir>; out/recovery/ refused anywhere). Never edits or reads outside --repo-root/out.
t0 <- Sys.time()
EXPLORATORY_DATE <- "2026-10-28"
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
# outdir: made absolute against the ORIGINAL cwd (before setwd below) and lexically cleaned of ./ and ../ BEFORE the out/ guard; re-checked after dir.create
lex_clean <- function(p) {
  p <- path.expand(p); if (!startsWith(p, "/")) p <- file.path(getwd(), p)
  parts <- strsplit(p, "/+")[[1]]; out <- character()
  for (q in parts[nzchar(parts)]) if (q == "..") out <- head(out, -1L) else if (q != ".") out <- c(out, q)
  paste0("/", paste(out, collapse = "/"))
}
out_guard <- function(p) if (grepl("(^|/)out/recovery(/|$)", p) || (startsWith(p, file.path(main_root, "out")) && !startsWith(p, file.path(main_root, "out", "exploratory", "")))) stop("refusing to write under out/ except out/exploratory/<dir> (never into out/recovery/)", call. = FALSE)
outdir <- lex_clean(opt("outdir")); out_guard(outdir)
if (dir.exists(outdir) && length(list.files(outdir, all.files = TRUE, no.. = TRUE))) stop("outdir must be new or empty: ", outdir, call. = FALSE)
local({   # parent-symlink guard: resolve the deepest EXISTING ancestor and guard (resolved ancestor + remaining segments) BEFORE creating anything
  a <- outdir; rest <- character()
  while (!file.exists(a) && a != "/") { rest <- c(basename(a), rest); a <- dirname(a) }
  out_guard(do.call(file.path, as.list(c(normalizePath(a, mustWork = TRUE), rest))))
})
dir.create(outdir, recursive = TRUE, showWarnings = FALSE)
outdir <- normalizePath(outdir, mustWork = TRUE); out_guard(outdir)   # resolves symlinks; must still be outside out/ or inside out/exploratory/

script_root <- local({
  a <- grep("^--file=", commandArgs(FALSE), value = TRUE)
  normalizePath(file.path(dirname(sub("^--file=", "", a[1])), ".."))
})
# committed estimator code, sourced read-only in the documented order (R/post.R header); PRE_R_ROOT is only a constant here
setwd(script_root)
code_files <- file.path(script_root, "R", paste0(c("utils", "decide", "cem", "estimate", "pre", "presign", "drdid", "post"), ".R"))
for (f in code_files) source(f)
stopifnot(requireNamespace("jsonlite", quietly = TRUE))   # po_read_params uses jsonlite::fromJSON
library(jsonlite)
fixest::setFixest_nthreads(1L)             # single thread: byte-identical re-runs
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

signed_csv <- function(f) fread(file.path(SIGNED, f))
TOL <- 1e-9
assertions <- list()
assert_eq <- function(id, got, want, tol = TOL, source = "signed") {   # source: signed = signed run CSV; review = value from the tribunal review/refuter (regression guard); internal = self-consistency of this script
  d <- abs(got - want); ok <- is.finite(d) && d <= tol
  assertions[[length(assertions) + 1L]] <<- data.table(id = id, source = source, got = got, expected = want, abs_diff = d, tol = tol, pass = ok)
  if (!ok) stop(sprintf("ASSERTION FAILED %s: got %.15g expected %.15g |diff| %.3g (tol %g)", id, got, want, d, tol), call. = FALSE)
}

# ---- 3. pooled capped D / A under both clusterings ---------------------------------------------------------------------
est_signed <- signed_csv("post_estimates.csv")
for (o in c("D", "A")) for (cl in c("uid", "stratum")) {
  r <- estimate_did(panel, POST_EVENT_DAY, o, cl, "own")
  s <- est_signed[outcome == o & cap == "own" & cluster == cl]
  stopifnot(nrow(s) == 1L)
  assert_eq(sprintf("pooled_%s_own_%s_beta", o, cl), r$beta, s$beta)
  assert_eq(sprintf("pooled_%s_own_%s_se", o, cl), r$se, s$se)
}

# ---- 4. event-study fits (spec of R/post.R po_event_study, with vcov kept) -----------------------------------------------
d_all <- prep_est(panel, POST_EVENT_DAY, "own")      # capped D, A at the frozen own caps; 8 Aug excluded; carries `stratum`
d_all[, ex := as.numeric(treat)]
PRE_DAYS <- MAIN_BASELINE_DAYS                        # 0..59
ALL_DAYS <- c(PRE_DAYS, POST_DAYS)                    # 0..59, 61..95 (95 days; coefficient 59 is the reference, = 0)
CLUSTERS <- c("uid", "stratum")
es_fit <- function(outcome, days, cluster, rhs = sprintf("i(day_index, ex, ref = %d)", 59L), data = d_all) {
  d <- data[day_index %in% days]
  feols(as.formula(sprintf("%s ~ %s | uid + day_index", outcome, rhs)), data = d, weights = d$w, cluster = as.formula(paste0("~", cluster)),
        fixef.tol = 1e-11, fixef.iter = 10000, notes = FALSE)
}
# coefficient vector / vcov embedded on `days` (the reference day gets 0 and a zero row/column)
es_embed <- function(fit, days, cluster, data = d_all) {
  cf <- coef(fit); V <- vcov(fit)
  k <- as.integer(sub("^day_index::(-?[0-9]+):ex$", "\\1", names(cf)))
  stopifnot(!anyNA(k), setequal(c(k, 59L), days), all(is.finite(cf)))
  b <- setNames(numeric(length(days)), days); b[as.character(k)] <- cf
  W <- matrix(0, length(days), length(days), dimnames = list(days, days)); W[as.character(k), as.character(k)] <- V
  G <- uniqueN(data[day_index %in% days][[cluster]])
  list(b = b, V = W, G = G, days = days, cluster = cluster)
}
ES <- list()
for (o in c("D", "A")) for (cl in CLUSTERS) {
  cat(sprintf("event study fits: %s %s\n", o, cl))
  ES[[paste(o, cl, "pre", sep = "|")]] <- es_embed(es_fit(o, PRE_DAYS, cl), PRE_DAYS, cl)
  ES[[paste(o, cl, "all", sep = "|")]] <- es_embed(es_fit(o, ALL_DAYS, cl), ALL_DAYS, cl)
}
es <- function(o, cl, win) ES[[paste(o, cl, win, sep = "|")]]

pt <- signed_csv("post_pretrend.csv"); tp_ <- signed_csv("post_time_path.csv")
for (o in c("D", "A")) {
  e <- es(o, "uid", "pre"); s <- pt[outcome == o]
  for (k in setdiff(PRE_DAYS, 59L)) {
    r <- s[day_index == k]; stopifnot(nrow(r) == 1L, r$status == "estimated")
    assert_eq(sprintf("pretrend_%s_day%d_coef", o, k), e$b[[as.character(k)]], r$coef)
    assert_eq(sprintf("pretrend_%s_day%d_se", o, k), sqrt(e$V[as.character(k), as.character(k)]), r$se)
  }
  e <- es(o, "uid", "all"); s <- tp_[outcome == o]
  for (k in POST_DAYS) {
    r <- s[day_index == k]; stopifnot(nrow(r) == 1L, r$status == "estimated")
    assert_eq(sprintf("timepath_%s_day%d_coef", o, k), e$b[[as.character(k)]], r$coef)
    assert_eq(sprintf("timepath_%s_day%d_se", o, k), sqrt(e$V[as.character(k), as.character(k)]), r$se)
  }
}
cat(sprintf("assertions passed: %d\n", length(assertions)))

# ---- output helpers ---------------------------------------------------------------------------------------------------
wcsv <- function(dt, f) fwrite(cbind(data.table(banner = BANNER), dt), file.path(outdir, f))
tcrit <- function(df, level = 0.95) qt(1 - (1 - level) / 2, df)
NUM <- list()
add_num <- function(id, outcome, cluster, estimate, se = NA_real_, df = NA_real_, definition, ci = TRUE, p = NA_real_, inf = TRUE) {
  tc <- if (ci && inf && is.finite(se) && is.finite(df)) tcrit(df) else NA_real_
  if (inf && is.na(p) && is.finite(se) && is.finite(df) && se > 0) p <- 2 * pt(-abs(estimate / se), df)
  NUM[[length(NUM) + 1L]] <<- data.table(id = id, outcome = outcome, cluster = cluster, estimate = estimate, se = se, lo = estimate - tc * se, hi = estimate + tc * se,
                                          df = df, p = p, definition = definition)
}
lincom <- function(e, cvec) {   # cvec over e$days
  stopifnot(length(cvec) == length(e$days))
  c(est = sum(cvec * e$b), se = sqrt(drop(t(cvec) %*% e$V %*% cvec)))
}
cv_set <- function(days, pos = integer(), neg = integer(), neg_denominator = length(neg)) {   # mean(pos) - mean(neg)
  v <- setNames(numeric(length(days)), days)
  if (length(pos)) v[as.character(pos)] <- v[as.character(pos)] + 1 / length(pos)
  if (length(neg)) v[as.character(neg)] <- v[as.character(neg)] - 1 / neg_denominator
  v
}
dstr <- function(k) format(PRE_PERIOD_START + k)

# ---- 5. save the covariance ------------------------------------------------------------------------------------------
vc_long <- function(e, o, win) {
  dn <- e$days; ij <- CJ(i = seq_along(dn), j = seq_along(dn))
  data.table(outcome = o, window = win, cluster = e$cluster, row_term = dn[ij$i], col_term = dn[ij$j], value = as.vector(t(e$V)))
}
coef_long <- list(); vc_all <- list()
for (o in c("D", "A")) for (cl in CLUSTERS) for (win in c("pre", "all")) {
  e <- es(o, cl, win)
  saveRDS(list(banner = BANNER, outcome = o, cluster = cl, window = win, day_index = e$days, date = dstr(e$days), coef = e$b, vcov = e$V,
               n_clusters = e$G, df = e$G - 1L, note = "reference day 59 (7 Aug) has coefficient 0 and a zero row/column"),
          file.path(outdir, sprintf("o3_vcov_%s_%s_%s.rds", o, cl, win)))
  vc_all[[length(vc_all) + 1L]] <- vc_long(e, o, win)
  coef_long[[length(coef_long) + 1L]] <- data.table(outcome = o, window = win, cluster = cl, day_index = e$days, date = dstr(e$days), coef = e$b, se = sqrt(diag(e$V)), n_clusters = e$G)
}
wcsv(rbindlist(vc_all), "o3_vcov_long.csv")
wcsv(rbindlist(coef_long), "o3_coefficients.csv")

# ---- 6. transcript-only numbers --------------------------------------------------------------------------------------------
POST_N <- length(POST_DAYS)
for (o in c("D", "A")) for (cl in CLUSTERS) {
  epre <- es(o, cl, "pre"); eall <- es(o, cl, "all"); df <- eall$G - 1
  # joint Wald of the 59 pre coefficients
  for (win in c("pre", "all")) {
    e <- es(o, cl, win); idx <- as.character(0:58)
    b <- e$b[idx]; V <- e$V[idx, idx]; Fs <- drop(t(b) %*% solve(V, b)) / 59
    add_num(sprintf("wald_pre59_%s_%s_F", o, win), o, cl, Fs, NA, e$G - 1, p = pf(Fs, 59, e$G - 1, lower.tail = FALSE),
            definition = sprintf("joint Wald F of the 59 pre coefficients (days 0..58, ref 59), %s event study; estimate = F; df = G-1 = denominator df of F(59, G-1); p = upper tail", if (win == "pre") "pre-only (post_pretrend spec)" else "pre+post"))
  }
  # post mean minus late-baseline mean
  wins <- list(`7aug_only` = 59L, last5 = 55:59, last7 = 53:59, last10 = 50:59, last15 = 45:59, last20 = 40:59, last30 = 30:59, last40 = 20:59,
               from_1jul = 22:59, from_8jul = 29:59, `29jul_7aug` = 50:59, all60 = 0:59)
  for (nm in names(wins)) {
    S <- wins[[nm]]; r <- lincom(eall, cv_set(eall$days, POST_DAYS, S))
    add_num(sprintf("postmean_minus_base_%s_%s", nm, o), o, cl, r[["est"]], r[["se"]], df,
            definition = sprintf("mean of the 35 post-day event-study coefficients (9 Aug-12 Sep) minus mean of the pre coefficients over %s..%s (%d days, 7 Aug coefficient = 0 counts in the mean); pre+post fit, own caps", dstr(min(S)), dstr(max(S)), length(S)))
  }
  # F2: refuter's last-k variant, reference day 7 Aug (coefficient 0) left out of the baseline mean: days 59-k..58
  wins_x <- list(excl7aug_last10 = 49:58, excl7aug_last20 = 39:58, excl7aug_last30 = 29:58, excl7aug_last40 = 19:58, excl7aug_from_1jul = 22:58)
  want_x <- c(excl7aug_last10 = -0.1072, excl7aug_last20 = -0.1306, excl7aug_last30 = -0.1298, excl7aug_last40 = -0.1277, excl7aug_from_1jul = -0.1248)
  for (nm in names(wins_x)) {
    S <- wins_x[[nm]]; r <- lincom(eall, cv_set(eall$days, POST_DAYS, S))
    add_num(sprintf("postmean_minus_base_%s_%s", nm, o), o, cl, r[["est"]], r[["se"]], df,
            definition = sprintf("REFUTER VARIANT: mean of the 35 post-day coefficients minus mean of the pre coefficients over %s..%s (%d days), the 7 Aug reference coefficient (0) LEFT OUT of the baseline mean; pre+post fit, own caps", dstr(min(S)), dstr(max(S)), length(S)))
    if (o == "D" && cl == "uid") assert_eq(sprintf("postmean_minus_base_%s_D", nm), r[["est"]], want_x[[nm]], 5e-5, "review")
  }
  # fake event 9 Jul on the main matched sample: contrast 9 Jun-8 Jul vs 10 Jul-7 Aug, same TWFE and caps
  pp <- panel[day_index <= 59L]; setattr(pp, "baseline_days", attr(panel, "baseline_days"))
  r <- estimate_did(pp, PLACEBO_EVENT_DAY, o, cl, "own")
  add_num(sprintf("fake_event_9jul_%s", o), o, cl, r$beta, r$se, r$n_clusters - 1,
          definition = "estimate_did on the main matched sample restricted to days 0..59, fake event 9 Jul 2022 (day 30, excluded), own caps and B taken from days 0..59 (the signed June placebo uses June-only caps, R/pre.R:365, so this is not that placebo); tp = exposed x day>30")
  # exposed-group linear trend (F5)
  dd <- d_all[, .(uid, day_index, w, stratum, D, A, tp, ex)]
  dd[, trend := ex * day_index]
  dd[, trend8 := ex * pmax(day_index - 29L, 0L)]
  for (spec in list(list("trend_all_days", dd, "tp + trend", "all days 0..59,61..95; D ~ tp + exposed x day_index | uid + day_index"),
                    list("trend_from_8jul_sample", dd[day_index >= 29L], "tp + trend", "sample restricted to days >= 8 Jul (day 29); D ~ tp + exposed x day_index | uid + day_index"),
                    list("trend_from_8jul_term", dd, "tp + trend8", "all days; trend term exposed x max(day_index - 29, 0), i.e. a trend that starts 8 Jul"))) {
    f <- feols(as.formula(sprintf("%s ~ %s | uid + day_index", o, spec[[3]])), data = spec[[2]], weights = spec[[2]]$w,
               cluster = as.formula(paste0("~", cl)), fixef.tol = 1e-11, fixef.iter = 10000, notes = FALSE)
    add_num(sprintf("%s_%s", spec[[1]], o), o, cl, unname(coef(f)["tp"]), unname(se(f)["tp"]), uniqueN(spec[[2]][[cl]]) - 1,
            definition = paste0("exposed-group linear trend, own-capped ", o, ": ", sub("D ~ ", paste0(o, " ~ "), spec[[4]], fixed = TRUE), "; estimate = tp (post x exposed)"))
  }
  # two-day 9 Aug jump vs largest pre two-day move; largest one-day pre moves with dates
  b <- epre$b; V <- epre$V; nd <- length(PRE_DAYS)
  mv <- function(lag) vapply(seq_len(nd - lag), function(i) { v <- numeric(nd); v[i + lag] <- 1; v[i] <- -1; lincom(epre, v)[["est"]] }, 0)
  for (lag in 1:2) {
    ch <- mv(lag); i <- which.max(abs(ch))
    v <- numeric(nd); v[i + lag] <- 1; v[i] <- -1; r <- lincom(epre, v)
    if (lag == 2) {
      jump <- lincom(eall, cv_set(eall$days, 61L, 59L))   # coef(9 Aug) - coef(7 Aug) = coef(9 Aug): the two-day jump across the excluded 8 Aug
      add_num(sprintf("jump_9aug_two_day_%s", o), o, cl, jump[["est"]], jump[["se"]], df, definition = "event-study coefficient of 9 Aug relative to 7 Aug (two calendar days; 8 Aug excluded); pre+post fit")
      add_num(sprintf("largest_pre_two_day_move_%s", o), o, cl, r[["est"]], r[["se"]], epre$G - 1,
              inf = FALSE,
              definition = sprintf("largest |coef(t+2) - coef(t)| over pre days 0..59 (pre-only fit; 7 Aug coefficient 0): %s -> %s; rank-selected maximum: SE shown is nominal, post-selection, no CI/p reported", dstr(PRE_DAYS[i]), dstr(PRE_DAYS[i + 2])))
      add_num(sprintf("jump_ratio_%s", o), o, cl, abs(jump[["est"]]) / abs(r[["est"]]), definition = "|9 Aug two-day jump| / |largest pre two-day move|; point-estimate arithmetic, no inferential uncertainty", ci = FALSE)
    } else {
      ord <- order(-abs(ch))[1:5]
      for (rk in seq_along(ord)) {
        v <- numeric(nd); v[ord[rk] + 1] <- 1; v[ord[rk]] <- -1; r1 <- lincom(epre, v)
        add_num(sprintf("pre_oneday_move_rank%d_%s", rk, o), o, cl, r1[["est"]], r1[["se"]], epre$G - 1,
                inf = FALSE,
                definition = sprintf("rank %d largest |one-day change| of the pre coefficients (pre-only fit): %s -> %s; rank-selected: SE shown is nominal, post-selection, no CI/p reported", rk, dstr(PRE_DAYS[ord[rk]]), dstr(PRE_DAYS[ord[rk] + 1])))
      }
    }
  }
}

# coefficient-arithmetic families (point estimates only; cluster = "none"): extrapolation grid, random-walk band, sliding placebo contrasts
bpre <- function(o) es(o, "uid", "pre")$b          # pre coefficients (identical across clusterings: same fit)
ball <- function(o) es(o, "uid", "all")$b
diff_pre_all <- sapply(c("D", "A"), function(o) max(abs(ball(o)[as.character(PRE_DAYS)] - bpre(o))))
WINS <- list(all60 = 0:59, last40 = 20:59, last30 = 30:59, last20 = 40:59, last15 = 45:59, last10 = 50:59, last7 = 53:59,
             from_1jul = 22:59, from_8jul = 29:59, from_29jun = 20:59, from_19jul = 40:59, from_29jul = 50:59)
LBL <- "coefficient arithmetic, no inferential uncertainty"
dupnote <- function(nm) {   # F8: 12 named windows, only 9 distinct day sets
  first <- names(WINS)[match(list(WINS[[nm]]), WINS)]
  if (first == nm) "" else sprintf("[duplicate window: same days as %s; the 12 windows are 9 distinct] ", first)
}
extrap <- list(); rw <- list(); slide <- list()
for (o in c("D", "A")) {
  bp <- bpre(o); ba <- ball(o); post_mean <- mean(ba[as.character(POST_DAYS)])
  for (nm in names(WINS)) {
    S <- WINS[[nm]]; f <- lm(y ~ x, data.frame(x = S, y = bp[as.character(S)]))
    pred <- mean(predict(f, data.frame(x = POST_DAYS)))
    extrap[[length(extrap) + 1L]] <- data.table(outcome = o, window = nm, first_day = min(S), last_day = max(S), n_days = length(S), intercept = coef(f)[[1]], slope = coef(f)[[2]],
                                                predicted_post_mean = pred, post_mean_coef = post_mean, residual = post_mean - pred, label = LBL)
    add_num(sprintf("extrap_resid_%s_%s", nm, o), o, "none", post_mean - pred,
            definition = sprintf("%s%s: post mean coefficient (%.5f) minus mean prediction (%.5f) at days 61..95 from the unweighted OLS line through the pre coefficients over %s..%s (%d days, pre-only fit)", dupnote(nm), LBL, post_mean, pred, dstr(min(S)), dstr(max(S)), length(S)), ci = FALSE)
  }
  ch <- diff(bp); sdc <- sd(ch)
  res <- residuals(lm(y ~ x, data.frame(x = PRE_DAYS, y = bp)))
  ac_acf <- acf(res, lag.max = 1, plot = FALSE)$acf[2]; ac_cor <- cor(res[-1], res[-length(res)])
  hz <- function(h) sdc * sqrt(sum(outer(h, h, pmin))) / length(h)
  sd_cal <- hz(POST_DAYS - 59L); sd_step <- hz(seq_len(POST_N))
  add_num(sprintf("rw_sd_daily_change_%s", o), o, "none", sdc, definition = sprintf("sample SD of the 59 daily changes of the pre coefficients (pre-only fit); %s", LBL), ci = FALSE)
  add_num(sprintf("rw_detrended_lag1_acf_%s", o), o, "none", ac_acf, definition = "lag-1 autocorrelation (stats::acf) of the residuals of the pre coefficients regressed on day_index (60 days)", ci = FALSE)
  add_num(sprintf("rw_detrended_lag1_cor_%s", o), o, "none", ac_cor, definition = "alternative: Pearson cor(r_t, r_{t-1}) of the same residuals", ci = FALSE)
  add_num(sprintf("rw_post_mean_sd_calendar_%s", o), o, "none", sd_cal, definition = "SD of the post mean (35 days) under a driftless random walk with step SD = daily-change SD, horizons = calendar days since 7 Aug (2..36, the excluded 8 Aug is a step); Var = sigma^2 sum_ij min(h_i,h_j)/35^2", ci = FALSE)
  add_num(sprintf("rw_post_mean_band95_calendar_%s", o), o, "none", 1.96 * sd_cal, definition = "HEURISTIC, not a test or confidence interval: +- 1.96 x the calendar-horizon RW post-mean SD (the step SD is about the size of a one-day sampling SE, so the RW SD is not an inferential benchmark)", ci = FALSE)
  add_num(sprintf("rw_post_mean_sd_steps_%s", o), o, "none", sd_step, definition = "alternative: horizons 1..35 (8 Aug not counted as a step)", ci = FALSE)
  add_num(sprintf("rw_post_mean_band95_steps_%s", o), o, "none", 1.96 * sd_step, definition = "HEURISTIC, not a test or confidence interval: +- 1.96 x the step-horizon RW post-mean SD", ci = FALSE)
  # sliding 15-vs-15 contrasts over the pre period, with SEs from the pre-only vcov (both clusterings); real contrast from the pre+post fit.
  # PRIMARY (variant "gap"): fake = days s+16..s+30 vs base s..s+14, s = 0..29 (30 windows), skipping one day like the real contrast skips 8 Aug (days 61..75 vs 45..59).
  # ALTERNATIVE (variant "nogap"): fake = s+15..s+29 vs base s..s+14, s = 0..30 (31 windows), no skipped day.
  for (cl in CLUSTERS) {
    e <- es(o, cl, "pre")
    for (vr in c("gap", "nogap")) for (s in 0:(if (vr == "gap") 29 else 30)) {
      g <- if (vr == "gap") 16L else 15L
      r <- lincom(e, cv_set(e$days, (s + g):(s + g + 14), s:(s + 14)))
      slide[[length(slide) + 1L]] <- data.table(outcome = o, cluster = cl, variant = vr, start_day = s, base_from = dstr(s), base_to = dstr(s + 14), fake_from = dstr(s + g), fake_to = dstr(s + g + 14),
                                                estimate = r[["est"]], se = r[["se"]], df = e$G - 1)
    }
  }
  sl_all <- rbindlist(slide)[outcome == o & cluster == "uid"]
  sl <- sl_all[variant == "gap"]; sl_ng <- sl_all[variant == "nogap"]
  gapdef <- "mean(coef days s+16..s+30) - mean(coef days s..s+14), s = 0..29, skipping day s+15 like the real contrast skips 8 Aug"
  add_num(sprintf("sliding15_gap30_min_%s", o), o, "none", min(sl$estimate), definition = sprintf("PRIMARY: minimum over the %d sliding placebo-in-time contrasts %s; pre-only fit; %s", nrow(sl), gapdef, LBL), ci = FALSE)
  add_num(sprintf("sliding15_gap30_max_%s", o), o, "none", max(sl$estimate), definition = sprintf("PRIMARY: maximum of the same %d contrasts; %s", nrow(sl), LBL), ci = FALSE)
  for (nm in c("all31", "first30")) {
    x <- if (nm == "all31") sl_ng else sl_ng[start_day <= 29]
    add_num(sprintf("sliding15_nogap_%s_min_%s", nm, o), o, "none", min(x$estimate), definition = sprintf("ALTERNATIVE, no skipped day (does not mirror the real contrast): minimum over the %d contrasts mean(coef days s+15..s+29) - mean(coef days s..s+14) (s = %d..%d); pre-only fit; %s", nrow(x), min(x$start_day), max(x$start_day), LBL), ci = FALSE)
    add_num(sprintf("sliding15_nogap_%s_max_%s", nm, o), o, "none", max(x$estimate), definition = sprintf("ALTERNATIVE, no skipped day: maximum of the same %d contrasts; %s", nrow(x), LBL), ci = FALSE)
  }
  for (cl in CLUSTERS) {
    e <- es(o, cl, "all"); r <- lincom(e, cv_set(e$days, 61:75, 45:59))
    add_num(sprintf("real_15v15_%s", o), o, cl, r[["est"]], r[["se"]], e$G - 1, definition = "real 15-vs-15 contrast: mean of coefficients 9-23 Aug (days 61..75) minus mean over the last 15 pre days 24 Jul-7 Aug (45..59); pre+post fit")
  }
  real <- lincom(es(o, "uid", "all"), cv_set(es(o, "uid", "all")$days, 61:75, 45:59))[["est"]]
  add_num(sprintf("real_vs_extreme_gap_placebo_ratio_%s", o), o, "none", abs(real) / max(abs(sl$estimate)), definition = "PRIMARY: |real 15v15| / max |gap sliding placebo contrast| (30 windows); point-estimate arithmetic", ci = FALSE)
  add_num(sprintf("real_vs_extreme_nogap_placebo_ratio_%s", o), o, "none", abs(real) / max(abs(sl_ng$estimate)), definition = "ALTERNATIVE: |real 15v15| / max |no-gap sliding placebo contrast| (31 windows); point-estimate arithmetic", ci = FALSE)
  if (o == "D") {   # F1 expected values (review chg-49c748cc; 4-decimal reported values)
    assert_eq("sliding15_gap30_D_min", min(sl$estimate), -0.0833, 5e-5, "review"); assert_eq("sliding15_gap30_D_max", max(sl$estimate), 0.0279, 5e-5, "review")
    assert_eq("sliding15_gap30_D_n_windows", nrow(sl), 30, 0, "review"); assert_eq("real_vs_extreme_gap_placebo_ratio_D", abs(real) / max(abs(sl$estimate)), 1.54, 5e-3, "review")
  }
}
wcsv(rbindlist(extrap), "o3_extrapolation.csv")
wcsv(rbindlist(slide), "o3_sliding15.csv")

# ---- 7. weekly aggregation (diagnostic) -----------------------------------------------------------------------------------
wk_def <- data.table(
  bin = 1:13, label = c("9-19 Jun", "20-26 Jun", "27 Jun-3 Jul", "4-10 Jul", "11-17 Jul", "18-24 Jul", "25-31 Jul", "1-7 Aug (ref)", "9-14 Aug", "15-21 Aug", "22-28 Aug", "29 Aug-4 Sep", "5-12 Sep"),
  from = as.Date(c("2022-06-09", "2022-06-20", "2022-06-27", "2022-07-04", "2022-07-11", "2022-07-18", "2022-07-25", "2022-08-01", "2022-08-09", "2022-08-15", "2022-08-22", "2022-08-29", "2022-09-05")),
  to = as.Date(c("2022-06-19", "2022-06-26", "2022-07-03", "2022-07-10", "2022-07-17", "2022-07-24", "2022-07-31", "2022-08-07", "2022-08-14", "2022-08-21", "2022-08-28", "2022-09-04", "2022-09-12")))
wk_def[, `:=`(day_from = as.integer(from - PRE_PERIOD_START), day_to = as.integer(to - PRE_PERIOD_START))]
wk_def[, n_days := day_to - day_from + 1L][, period := fifelse(bin <= 8L, "pre", "post")]
stopifnot(sum(wk_def[period == "post", n_days]) == POST_N, sum(wk_def[period == "pre", n_days]) == 60L, weekdays(wk_def$from[2]) == "Monday")
REF_BIN <- 8L
day2bin <- rbindlist(lapply(seq_len(nrow(wk_def)), function(i) data.table(day_index = wk_def$day_from[i]:wk_def$day_to[i], wk = wk_def$bin[i])))
wcsv(wk_def[, .(bin, label, from = format(from), to = format(to), day_from, day_to, n_days, period)], "o3_weekly_bins.csv")
Cmat <- function(days) {   # non-reference bins: day-count-weighted bin mean minus reference-bin mean, on the coefficient vector over `days`
  rows <- wk_def[bin != REF_BIN]
  M <- t(sapply(rows$bin, function(b) {
    v <- numeric(length(days)); names(v) <- days
    dd <- day2bin[wk == b, day_index]; v[as.character(dd)] <- v[as.character(dd)] + 1 / length(dd)
    rd <- day2bin[wk == REF_BIN, day_index]; v[as.character(rd)] <- v[as.character(rd)] - 1 / length(rd)
    v
  }))
  dimnames(M) <- list(rows$label, days); M
}
weekly <- list(); weekly_fit <- list(); post_rows <- list()
d_wk <- merge(d_all, day2bin, by = "day_index")
for (o in c("D", "A")) for (cl in CLUSTERS) {
  e <- es(o, cl, "all"); C <- Cmat(e$days)
  cb <- drop(C %*% e$b); CV <- C %*% e$V %*% t(C)
  fit <- feols(as.formula(sprintf("%s ~ i(wk, ex, ref = %d) | uid + day_index", o, REF_BIN)), data = d_wk, weights = d_wk$w, cluster = as.formula(paste0("~", cl)),
               fixef.tol = 1e-11, fixef.iter = 10000, notes = FALSE)
  cf <- coef(fit); kk <- as.integer(sub("^wk::([0-9]+):ex$", "\\1", names(cf)))
  bw <- setNames(rep(NA_real_, nrow(C)), rownames(C)); bw[wk_def[match(kk, bin), label]] <- cf
  sw <- setNames(rep(NA_real_, nrow(C)), rownames(C)); sw[wk_def[match(kk, bin), label]] <- se(fit)[names(cf)]
  weekly[[length(weekly) + 1L]] <- data.table(outcome = o, cluster = cl, bin = wk_def[bin != REF_BIN, bin], label = rownames(C), n_days = wk_def[bin != REF_BIN, n_days],
                                              C_beta = cb, C_se = sqrt(diag(CV)), refit_beta = bw, refit_se = sw, diff_beta = cb - bw, diff_se = sqrt(diag(CV)) - sw, n_clusters = e$G)
  wcsv(data.table(outcome = o, cluster = cl, row = rep(rownames(C), each = nrow(C)), col = rep(rownames(C), times = nrow(C)), value = as.vector(t(CV))), sprintf("o3_weekly_CSigmaCt_%s_%s.csv", o, cl))
  pw <- wk_def[bin != REF_BIN & period == "post", n_days] / POST_N; ip <- which(wk_def[bin != REF_BIN, period] == "post")
  cvp <- numeric(nrow(C)); cvp[ip] <- pw
  pm <- sum(cvp * cb); pse <- sqrt(drop(t(cvp) %*% CV %*% cvp))
  direct <- lincom(e, cv_set(e$days, POST_DAYS, 53:59))   # 35-day mean minus reference-week (53..59) mean, on the daily coefficients
  assert_eq(sprintf("weekly_post_dayweighted_equals_35day_recentred_%s_%s_est", o, cl), pm, direct[["est"]], source = "internal")
  assert_eq(sprintf("weekly_post_dayweighted_equals_35day_recentred_%s_%s_se", o, cl), pse, direct[["se"]], source = "internal")
  post_rows[[length(post_rows) + 1L]] <- data.table(outcome = o, cluster = cl, post_dayweighted_mean_recentred = pm, se = pse, direct_35day_recentred = direct[["est"]], abs_diff = abs(pm - direct[["est"]]))
  add_num(sprintf("weekly_post_dayweighted_%s", o), o, cl, pm, pse, e$G - 1, definition = "day-count-weighted mean of the 5 post weekly bins (C beta) re-centred on the 1-7 Aug reference bin = 35-day post mean minus reference-week mean of the daily coefficients")
}
weekly <- rbindlist(weekly); wcsv(weekly, "o3_weekly_Cbeta.csv"); wcsv(rbindlist(post_rows), "o3_weekly_post_mean.csv")
wk_diag <- weekly[, .(max_abs_diff_beta = max(abs(diff_beta)), max_abs_diff_se = max(abs(diff_se))), by = .(outcome, cluster)]
wcsv(wk_diag, "o3_weekly_diagnostic.csv")

# ---- 8. clip incidence (capped outcomes: share STRICTLY beyond the own caps), arm x tail x period and arm x week -----------------
clip_pp <- clip_incidence(panel, POST_EVENT_DAY)
clip_pp[, period := fifelse(period == "baseline", "pre", period)]
cs <- signed_csv("post_cap_clip.csv")[table == "clip_incidence"]
cmp <- merge(clip_pp[, .(outcome, tail, arm, period = fifelse(period == "pre", "baseline", period), share)], cs[, .(outcome = a, tail = b, arm = c, period = d, signed = value)], by = c("outcome", "tail", "arm", "period"))
stopifnot(nrow(cmp) == nrow(clip_pp), nrow(cmp) == 16L)
clip_check <- max(abs(cmp$share - cmp$signed))
caps <- cap_values_all(panel, POST_EVENT_DAY)
dw <- merge(panel[day_index != POST_EVENT_DAY], day2bin, by = "day_index")
dw[, arm := fifelse(treat, "exposed", "control")]
clip_wk <- rbindlist(lapply(c("D", "A"), function(o) rbindlist(lapply(c("lower", "upper"), function(tl) {
  cp <- caps[[o]]; flag <- if (tl == "lower") dw[[o]] < cp[1] else dw[[o]] > cp[2]
  dw[, .(share = sum(w * flag[.I]) / sum(w), n_wallet_days = .N), by = .(arm, wk)][, `:=`(outcome = o, tail = tl)]
}))))
clip_wk <- merge(clip_wk, wk_def[, .(wk = bin, week = label, period)], by = "wk")[order(outcome, tail, arm, wk), .(outcome, tail, arm, wk, week, period, share, n_wallet_days)]
wcsv(clip_pp[order(outcome, tail, arm, period)], "o3_clip_incidence.csv")
wcsv(clip_wk, "o3_clip_by_week.csv")

# ---- 9. write remaining outputs --------------------------------------------------------------------------------------------------
numbers <- rbindlist(NUM)
wcsv(numbers, "o3_numbers.csv")
wcsv(hash_tab, "o3_input_hashes.csv")
wcsv(rbindlist(assertions), "o3_assertions.csv")
writeLines(c(sprintf("# %s", BANNER), capture.output(sessionInfo()), "", "# sha256 of sourced committed R code:",
             vapply(code_files, function(f) sprintf("%s  %s", sha256_file(f), sub(paste0(script_root, "/"), "", f, fixed = TRUE)), "")), file.path(outdir, "sessionInfo.txt"))
sm <- c(sprintf("# %s", BANNER), "",
        sprintf("O3 pre-trend script `exploratory/o3_pretrend.R` (post-results diagnostic for the Tornado-USDC pre-trend tribunal of 2026-10-28). Run metadata: date %s; runtime %.1f min.", format(Sys.Date()), as.numeric(difftime(Sys.time(), t0, units = "mins"))),
        "",
        sprintf("- Signed inputs: all %d input sha256 equal `post_trace.json` input_sha256 (o3_input_hashes.csv).", nrow(hash_tab)),
        { A <- rbindlist(assertions); ps <- A[, .(n = .N, tol = paste(sort(unique(format(tol))), collapse = "/")), by = source]
          sprintf("- Assertions (all passed): %d; per source (count, tolerance): %s. signed = pooled capped D/A beta+SE under uid and stratum and 59+35 event-study coefficients/SEs vs the signed post_estimates/post_pretrend/post_time_path; review = F1/F2 values from the tribunal review/refuter, regression guards not signed values; internal = weekly consistency (o3_assertions.csv, column `source`).", nrow(A), paste(sprintf("%s %d (tol %s)", ps$source, ps$n, ps$tol), collapse = "; ")) },
        sprintf("- Clip incidence reproduces the signed post_cap_clip.csv: max |diff| = %.3g.", clip_check),
        sprintf("- Max |pre coefficient, pre-only fit - pre+post fit|: D %.3g, A %.3g.", diff_pre_all[["D"]], diff_pre_all[["A"]]),
        sprintf("- Weekly diagnostic (C beta vs weekly-binned refit), max |diff beta| / |diff se|: %s.", paste(sprintf("%s %s %.4f/%.4f", wk_diag$outcome, wk_diag$cluster, wk_diag$max_abs_diff_beta, wk_diag$max_abs_diff_se), collapse = "; ")),
        "- All CIs/p-values: t with df = (number of clusters - 1); Wald F has df (59, G-1). Event-study = R/post.R po_event_study spec re-fit with vcov kept (`i(day_index, ex, ref = 59) | uid + day_index`, weights w, own caps, 8 Aug excluded).",
        "- Extrapolation, random-walk and sliding-contrast families are coefficient arithmetic with no inferential uncertainty.",
        "- Files: o3_numbers.csv (every transcript-only number, with definition), o3_vcov_*.rds / o3_vcov_long.csv / o3_coefficients.csv, o3_extrapolation.csv, o3_sliding15.csv, o3_weekly_*.csv, o3_clip_*.csv, MANIFEST.sha256 (all files except summary.md), sessionInfo.txt.",
        "- Nothing here is a confirmatory result or changes the H1 classification; any citation must label it exploratory and post-results.")
writeLines(sm, file.path(outdir, "summary.md"))
fl <- setdiff(sort(list.files(outdir)), c("summary.md", "MANIFEST.sha256"))
writeLines(vapply(fl, function(f) sprintf("%s  %s", sha256_file(file.path(outdir, f)), f), ""), file.path(outdir, "MANIFEST.sha256"))
cat("done\n")
