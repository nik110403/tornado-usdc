#!/usr/bin/env Rscript
# EXPLORATORY, POST-RESULTS, dated 2026-10-28; not confirmatory; does not reclassify H1.
# O2 (exploratory/PLAN_o1_o2_2026-10-28.md section 1 + PLAN_o1_o2_2026-10-28.addenda.md A1; SPEC S32): HonestDiD sensitivity of the capped event-study post mean, one CELL per call.
#
#   Rscript exploratory/o2_honestdid.R --valve --in <dir with o3_vcov_*_all.rds> --out <new or empty dir> [--calendars daily,weekly] [options]       # runtime valve, once, BEFORE any cell
#   Rscript exploratory/o2_honestdid.R --cell <daily|weekly>_<uid|stratum>_<D|A> --in <dir> --out <new or empty dir> --valve-plan <valve dir>/o2_valve_plan.csv [options]
#   Rscript exploratory/o2_honestdid.R --collect --cells-dir <dir holding the 8 cell dirs> --out <new or empty dir> [--valve-dir <valve dir>] [options]
#
# Needs HonestDiD from the isolated library:  R_LIBS_USER=exploratory/rlib:$HOME/R/x86_64-pc-linux-gnu-library/4.3  (rlib FIRST, signed lib second;
# the signed library is never written to). Reads only the O3 vcov .rds layout (list: coef, vcov, day_index, n_clusters, df, outcome, cluster, window);
# nothing from the signed R/ code is sourced, so nothing signed can be edited or shadowed by this script. Inside the repo, writes only to out/exploratory/<dir>
# (whitelist; never anything else under the repo, never out/recovery/); outside the repo only in --synthetic test mode.
#
# Runtime valve (plan 1.5 step 5 + addendum A1 item 6): --valve times ONE relative-magnitudes call at Mbar = 0 per calendar (on that calendar's uid/D cell, planned method and gridPoints),
# discards its interval unread (only seconds and an ok/edge/failed/timeout class are written; a probe that is not ok trips the valve for its calendar and its seconds feed no T), projects
# T = seconds x (planned grid points + bisection evaluations, per threshold x flag) x 1.5 + the SD time and, if T > 2 h for any cell or the sum over cells > 8 h wall-clock after parallelism, walks the ordered chain
# (i) parallel (one process per cell) -> (ii) method Conditional -> (iii) gridPoints 200 with the theta grid centred on l'beta_post +-8 sd -> (iv) daily RM not computed, re-checking the
# trigger (re-probing where the setting changed) after each step, per calendar (addendum A2). The outcome is o2_valve_plan.csv; every cell of a calendar (uid AND stratum) reads the same row via --valve-plan.
#
# Options (all optional; defaults are the plan):
#   --alpha 0.05                    --bisections 10
#   --rm-method C-LF                --sd-method FLCI            --rm-grid-points <n>  (planned gridPoints for RM; default 1000 = HonestDiD's)
#   --thresholds D=0,-0.10;A=0,-0.05,-0.10     (clears tau <=> upper bound strictly < tau (nominal) / upper bound + theta step < tau (conservative, RM; addendum A1))
#   --rm-grid-lb / --rm-grid-ub     SYNTHETIC TESTS ONLY: absolute theta-grid bounds (real runs use +-20 sd of l'beta_post centred at 0, or +-8 sd centred on l'beta_post under valve step (iii))
#   --rm-grid a,b,c / --sd-grid a,b,c          (override the union grids)
#   --point-seconds <s>             per-evaluation wall-clock limit (an evaluation over it is reported as status timeout, never clear/lost)
#   --signed-time-path <csv>        assert coefficients equal this signed post_time_path.csv (outcome, day_index, coef) within 1e-9 (required for real inputs)
#   --restrictions RM,SD            which restrictions to run (default both; a cell run with one of them has no rows for the other)
#   --pre-keep K                    synthetic tests only (refused otherwise): keep only the last K daily pre periods (the full 59 take ~1 min per smoothness point)
#   --synthetic                     test mode: banner says SYNTHETIC, plan-checks that need real data are skipped
#   --repo-root <dir>               SYNTHETIC TESTS ONLY (refused otherwise): the tree whose out/exploratory/ the out guard whitelists
#   valve only: --calendars daily,weekly  --valve-cell-seconds 7200  --valve-total-seconds 28800  --valve-procs 4  --valve-gp-floor 200 (the floor is synthetic-test only)
#   cell only:  --valve-plan <o2_valve_plan.csv> (required for real inputs; replaces --rm-method / --rm-grid-points, which are refused beside it)
#   collect only: --allow-missing, --valve-dir <dir>
t_script <- Sys.time()
EXPLORATORY_DATE <- "2026-10-28"
BANNER_REAL <- sprintf("EXPLORATORY, POST-RESULTS, dated %s; not confirmatory; does not reclassify H1", EXPLORATORY_DATE)
BANNER_SYN <- "SYNTHETIC TEST DATA, NOT RESULTS; exploratory harness test"

# ---- CFG: everything the final plan may change ------------------------------------------------------------------------
union_grid <- function(...) sort(unique(round(unlist(list(...)), 12)))
CFG <- list(
  alpha = 0.05, bisections = 10L, seed = 0,
  rm_method = "C-LF", sd_method = "FLCI", rm_bound = "deviation from parallel trends", rm_grid_points = NULL, rm_grid_lb = NA_real_, rm_grid_ub = NA_real_,
  rm_grid_points_default = 1000L, rm_grid_k_default = 20,                       # HonestDiD defaults: gridPoints 1000, theta grid = +-20 sdTheta centred at 0
  valve_cell_seconds = 7200, valve_total_seconds = 28800, valve_procs = 4L, valve_gp_floor = 200L, valve_k = 8,
  rm_grid = union_grid(seq(0, 0.30, by = 0.02), seq(0.1, 2, by = 0.1), seq(0.5, 3, by = 0.25)),            # 40 points
  sd_grid = union_grid(c(0, 0.000025, 0.00005), seq(0.0001, 0.001, by = 0.0001), c(0.005, 0.01, 0.025, 0.05)),  # 17 points
  thresholds = list(D = c(0, -0.10), A = c(0, -0.05, -0.10)),
  point_seconds = Inf,
  ref_day = 59L, pre_days = 0:58, post_days = 61:95,
  honestdid_version = "0.2.7",                                  # pinned (0.2.8 needs CVXR>=1.8 -> Matrix>=1.7 + newer Rust, not buildable here without shadowing signed Matrix); a different installed version stops the run
  weekly_sd = "not implemented (no calendar-aware mapping)",   # plan 1.3: weekly smoothness is never computed
  dropped_rm = "daily RM not computed (valve iv)", restrictions = c("RM", "SD"))
CALENDARS <- c("daily", "weekly"); CLUSTERS <- c("uid", "stratum"); OUTCOMES <- c("D", "A")
ALL_CELLS <- as.vector(outer(CALENDARS, as.vector(outer(CLUSTERS, OUTCOMES, paste, sep = "_")), paste, sep = "_"))

# ---- args -------------------------------------------------------------------------------------------------------------
argv <- commandArgs(TRUE)
FLAGS <- c("synthetic", "collect", "allow-missing", "valve")
VALUED <- c("cell", "in", "out", "cells-dir", "alpha", "bisections", "rm-method", "sd-method", "rm-grid-points", "rm-grid-lb", "rm-grid-ub", "thresholds", "rm-grid", "sd-grid",
            "pre-keep", "restrictions", "point-seconds", "signed-time-path", "repo-root", "valve-plan", "valve-dir", "calendars", "valve-cell-seconds", "valve-total-seconds", "valve-procs", "valve-gp-floor")
bad <- setdiff(sub("=.*", "", argv[startsWith(argv, "--")]), paste0("--", c(FLAGS, VALUED)))
if (length(bad)) stop("unknown argument(s): ", paste(bad, collapse = " "), call. = FALSE)
has <- function(name) paste0("--", name) %in% argv
opt <- function(name, default = NULL) {
  i <- match(paste0("--", name), argv)
  if (is.na(i)) { if (is.null(default)) stop(sprintf("--%s is required (no default)", name), call. = FALSE) else return(default) }
  if (i == length(argv) || startsWith(argv[i + 1L], "--")) stop(sprintf("--%s needs a value", name), call. = FALSE)
  argv[i + 1L]
}
num_list <- function(s) { v <- as.numeric(strsplit(s, ",")[[1]]); if (anyNA(v)) stop("not a number list: ", s, call. = FALSE); v }
SYNTHETIC <- has("synthetic")
BANNER <- if (SYNTHETIC) BANNER_SYN else BANNER_REAL
if (has("alpha")) CFG$alpha <- as.numeric(opt("alpha"))
if (has("bisections")) CFG$bisections <- as.integer(opt("bisections"))
if (has("rm-method")) CFG$rm_method <- opt("rm-method")
if (has("sd-method")) CFG$sd_method <- opt("sd-method")
if (has("rm-grid-points")) CFG$rm_grid_points <- as.integer(opt("rm-grid-points"))
if (has("repo-root") && !SYNTHETIC) stop("--repo-root is a synthetic-test shortcut and is refused for real inputs (real runs write below the script's own repo only)", call. = FALSE)
if (has("rm-grid-lb") || has("rm-grid-ub") || has("valve-gp-floor")) { if (!SYNTHETIC) stop("--rm-grid-lb/--rm-grid-ub/--valve-gp-floor are synthetic-test shortcuts and are refused for real inputs (the grid is centred at 0, +-20 sd, or centred on l'beta_post +-8 sd under valve step iii)", call. = FALSE) }
if (has("rm-grid-lb")) CFG$rm_grid_lb <- as.numeric(opt("rm-grid-lb"))
if (has("rm-grid-ub")) CFG$rm_grid_ub <- as.numeric(opt("rm-grid-ub"))
if (has("valve-gp-floor")) CFG$valve_gp_floor <- as.integer(opt("valve-gp-floor"))
if (has("rm-grid")) CFG$rm_grid <- union_grid(num_list(opt("rm-grid")))
if (has("sd-grid")) CFG$sd_grid <- union_grid(num_list(opt("sd-grid")))
if (has("point-seconds")) CFG$point_seconds <- as.numeric(opt("point-seconds"))
if (has("valve-cell-seconds")) CFG$valve_cell_seconds <- as.numeric(opt("valve-cell-seconds"))
if (has("valve-total-seconds")) CFG$valve_total_seconds <- as.numeric(opt("valve-total-seconds"))
if (has("valve-procs")) CFG$valve_procs <- as.integer(opt("valve-procs"))
if (has("restrictions")) { CFG$restrictions <- strsplit(opt("restrictions"), ",")[[1]]; if (!all(CFG$restrictions %in% c("RM", "SD"))) stop("--restrictions must be RM, SD or RM,SD", call. = FALSE) }
if (has("pre-keep")) { if (!SYNTHETIC) stop("--pre-keep is a synthetic-test shortcut and is refused for real inputs", call. = FALSE); CFG$pre_days <- tail(CFG$pre_days, as.integer(opt("pre-keep"))) }
if (has("thresholds")) {   # D=0,-0.10;A=0,-0.05
  parts <- strsplit(opt("thresholds"), ";")[[1]]
  CFG$thresholds <- setNames(lapply(parts, function(p) num_list(sub("^[A-Za-z]+=", "", p))), sub("=.*$", "", parts))
  if (!setequal(names(CFG$thresholds), OUTCOMES)) stop("--thresholds needs both D= and A=", call. = FALSE)
}
if (has("valve-plan") && (has("rm-method") || has("rm-grid-points") || has("rm-grid-lb") || has("rm-grid-ub"))) stop("--valve-plan fixes the RM method and grid; it cannot be used together with --rm-method/--rm-grid-points/--rm-grid-lb/--rm-grid-ub", call. = FALSE)
if (!is.finite(CFG$alpha) || CFG$alpha <= 0 || CFG$alpha >= 1 || CFG$bisections < 0L) stop("bad --alpha / --bisections", call. = FALSE)

# ---- paths, out/ guard, helpers ---------------------------------------------------------------------------------------
script_root <- local({
  a <- grep("^--file=", commandArgs(FALSE), value = TRUE)
  normalizePath(file.path(dirname(sub("^--file=", "", a[1])), ".."))
})
main_root <- if (has("repo-root")) normalizePath(opt("repo-root"), mustWork = TRUE) else script_root
lex_clean <- function(p) {
  p <- path.expand(p); if (!startsWith(p, "/")) p <- file.path(getwd(), p)
  parts <- strsplit(p, "/+")[[1]]; out <- character()
  for (q in parts[nzchar(parts)]) if (q == "..") out <- head(out, -1L) else if (q != ".") out <- c(out, q)
  paste0("/", paste(out, collapse = "/"))
}
# Whitelist (not blacklist): inside the repo the ONLY writable place is out/exploratory/<dir>[/...]; any other repo path, bare out/exploratory, out/recovery, out/g4, ... and any ".." component is refused.
# Outside the repo (temp dirs) only --synthetic test runs may write.
out_guard <- function(p) {
  in_repo <- p == main_root || startsWith(p, paste0(main_root, "/"))
  pre <- file.path(main_root, "out", "exploratory", "")
  ok <- if (in_repo) startsWith(p, pre) && nzchar(sub("/.*$", "", substring(p, nchar(pre) + 1L))) && !grepl("(^|/)\\.\\.(/|$)", p) else SYNTHETIC && !grepl("(^|/)out/recovery(/|$)", p)
  if (!ok) stop("refusing to write: only out/exploratory/<dir> inside the repo (nothing else under the repo, never out/recovery/); outside the repo only --synthetic runs", call. = FALSE)
}
make_outdir <- function(p) {
  p <- lex_clean(p); out_guard(p)
  if (dir.exists(p) && length(list.files(p, all.files = TRUE, no.. = TRUE))) stop("--out must be new or empty: ", p, call. = FALSE)
  a <- p; rest <- character()
  while (!file.exists(a) && a != "/") { rest <- c(basename(a), rest); a <- dirname(a) }
  out_guard(do.call(file.path, as.list(c(normalizePath(a, mustWork = TRUE), rest))))   # parent-symlink guard before creating anything
  dir.create(p, recursive = TRUE, showWarnings = FALSE)
  p <- normalizePath(p, mustWork = TRUE); out_guard(p); p
}
suppressPackageStartupMessages(library(data.table))
source(file.path(script_root, "exploratory", "o2_lib.R"))
data.table::setDTthreads(1L)
sha256_file <- function(path) strsplit(system2("sha256sum", shQuote(path), stdout = TRUE), " ")[[1]][1]
outdir <- NULL
wcsv <- function(dt, f) fwrite(cbind(data.table(banner = BANNER), dt), file.path(outdir, f))
clabel <- function(tau) gsub("-", "m", format(tau, scientific = FALSE, trim = TRUE))
write_manifest <- function(exclude = "summary.md") {
  fl <- setdiff(sort(list.files(outdir)), c(exclude, "MANIFEST.sha256"))
  writeLines(vapply(fl, function(f) sprintf("%s  %s", sha256_file(file.path(outdir, f)), f), ""), file.path(outdir, "MANIFEST.sha256"))
}
verify_manifest <- function(d, what) {
  for (ln in readLines(file.path(d, "MANIFEST.sha256"))) {
    h <- sub("  .*$", "", ln); f <- sub("^[0-9a-f]{64}  ", "", ln)
    if (!identical(sha256_file(file.path(d, f)), h)) stop(sprintf("%s: %s does not match its MANIFEST", what, f), call. = FALSE)
  }
}
load_honestdid <- function() {
  if (!requireNamespace("HonestDiD", quietly = TRUE)) stop("HonestDiD is not installed; use R_LIBS_USER=exploratory/rlib:<signed lib>", call. = FALSE)
  v <- as.character(utils::packageVersion("HonestDiD"))
  if (!identical(v, CFG$honestdid_version)) stop(sprintf("HonestDiD %s installed but %s is pinned (CFG$honestdid_version)", v, CFG$honestdid_version), call. = FALSE)
  # seed = 0 is passed explicitly in every call; postPeriodMomentsOnly is NOT an argument of createSensitivityResults*, so the pinned package default (TRUE) is what runs: assert it.
  f <- formals(utils::getFromNamespace("computeConditionalCS_DeltaRM", "HonestDiD"))
  if (!isTRUE(f$postPeriodMomentsOnly) || !identical(f$seed, 0)) stop("HonestDiD defaults changed: postPeriodMomentsOnly must be TRUE and seed 0", call. = FALSE)
  v
}

# =====================================================================================================================
# COLLECT MODE
# =====================================================================================================================
if (has("collect")) {
  cells_dir <- normalizePath(opt("cells-dir"), mustWork = TRUE)
  outdir <- make_outdir(opt("out"))
  have <- ALL_CELLS[dir.exists(file.path(cells_dir, ALL_CELLS))]
  miss <- setdiff(ALL_CELLS, have)
  if (length(miss) && !has("allow-missing")) stop("missing cell dir(s): ", paste(miss, collapse = ", "), " (use --allow-missing to merge anyway; they are listed as MISSING)", call. = FALSE)
  rd <- function(cell, f) { x <- fread(file.path(cells_dir, cell, f)); x[, banner := NULL]; x }
  banners <- character()
  for (cell in have) {   # verify each cell's own manifest before merging anything
    verify_manifest(file.path(cells_dir, cell), paste("cell", cell))
    banners <- c(banners, fread(file.path(cells_dir, cell, "o2_timing.csv"), nrow = 1L)$banner)
  }
  if (length(unique(banners)) > 1L) stop("cells mix real and synthetic banners", call. = FALSE)
  if (length(banners)) BANNER <- banners[1]
  stk <- function(f) rbindlist(lapply(have, function(cell) rd(cell, f)), fill = TRUE)
  evals <- stk("o2_evals.csv"); brk <- stk("o2_breakdown.csv"); orig <- stk("o2_original.csv"); tim <- stk("o2_timing.csv"); inh <- stk("o2_input_hashes.csv")
  if (uniqueN(tim$honestdid_version) > 1L) stop("cells ran with different HonestDiD versions", call. = FALSE)
  if (uniqueN(tim$r_version) > 1L) stop("cells ran with different R versions", call. = FALSE)
  # uid and stratum of one calendar x restriction must have run under the same method and grid (the valve outcome is shared across both clusterings)
  setup <- unique(tim[, .(calendar, cluster, rm_method, rm_grid_points, rm_grid_k, rm_grid_center, rm_computed)])
  if (nrow(setup) && any(setup[, .(n = uniqueN(.SD[, -"cluster"])), by = calendar]$n > 1L)) stop("uid and stratum cells of one calendar ran with different RM method/grid (the valve outcome must be shared)", call. = FALSE)
  # joint per-point clears (plan 1.4): TRUE only if both clusterings clear at the same grid point; FALSE if either loses; NA otherwise (incl. points only one clustering evaluated)
  jk <- c("calendar", "restriction", "outcome", "M")
  for (col in grep("^clears_", names(evals), value = TRUE)) {
    u <- evals[cluster == "uid" & kind == "grid", c(jk, col), with = FALSE]; s <- evals[cluster == "stratum" & kind == "grid", c(jk, col), with = FALSE]
    setnames(u, col, "a"); setnames(s, col, "b"); jt <- merge(u, s, by = jk)
    jt[, j := fifelse(a %in% FALSE | b %in% FALSE, FALSE, fifelse(a %in% TRUE & b %in% TRUE, TRUE, NA))]
    jt[, id := paste(calendar, restriction, outcome, M)]
    jc <- paste0("joint_", col); evals[, (jc) := NA]
    evals[kind == "grid", (jc) := jt$j[match(paste(calendar, restriction, outcome, M), jt$id)]]
  }
  # joint breakdown per calendar x restriction x outcome x threshold x flag: smaller of the two clusterings' breakdowns; either without initial clearance -> "no initial clearance"
  keys <- unique(brk[, .(calendar, restriction, outcome, threshold, flag, headline)])
  joint <- rbindlist(lapply(seq_len(nrow(keys)), function(i) {
    k <- keys[i]; a <- brk[calendar == k$calendar & restriction == k$restriction & outcome == k$outcome & threshold == k$threshold & flag == k$flag]
    ua <- a[cluster == "uid"]; sa <- a[cluster == "stratum"]
    if (nrow(ua) != 1L || nrow(sa) != 1L) return(NULL)
    j <- joint_breakdown(as.list(ua), as.list(sa))
    cbind(k, data.table(joint_status = j$status, joint_bracket_lo = j$bracket_lo, joint_bracket_hi = j$bracket_hi, joint_qualifier = j$qualifier, binding_cluster = j$binding, uid_status = ua$status, stratum_status = sa$status))
  }))
  if (is.null(joint) || !nrow(joint)) joint <- data.table(note = "no cell pair")
  dis <- merge(brk[cluster == "uid"], brk[cluster == "stratum"], by = c("calendar", "restriction", "outcome", "threshold", "flag"), suffixes = c("_uid", "_stratum"))
  if (nrow(dis)) {
    dis[, status_differs := status_uid != status_stratum]
    dis[, brackets_overlap := !is.na(bracket_lo_uid) & !is.na(bracket_lo_stratum) & pmax(bracket_lo_uid, bracket_lo_stratum) <= pmin(bracket_hi_uid, bracket_hi_stratum)]
    dis[, agree := !status_differs & (is.na(bracket_lo_uid) | brackets_overlap)]
    dis <- dis[, .(calendar, restriction, outcome, threshold, flag, status_uid, status_stratum, bracket_lo_uid, bracket_hi_uid, bracket_lo_stratum, bracket_hi_stratum, status_differs, brackets_overlap, agree)]
  }
  wcsv(evals, "o2_evals.csv"); wcsv(brk, "o2_breakdown.csv"); wcsv(joint, "o2_joint.csv"); wcsv(orig, "o2_original.csv"); wcsv(tim, "o2_timing.csv"); wcsv(inh, "o2_input_hashes.csv"); wcsv(dis, "o2_disagreement.csv")
  if (has("valve-dir")) {
    vd <- normalizePath(opt("valve-dir"), mustWork = TRUE); verify_manifest(vd, "valve dir")
    for (f in c("o2_valve_plan.csv", "o2_valve_log.csv", "o2_valve_probes.csv")) file.copy(file.path(vd, f), file.path(outdir, f))
  }
  for (cell in have) file.copy(file.path(cells_dir, cell, "sessionInfo.txt"), file.path(outdir, sprintf("sessionInfo_%s.txt", cell)))
  writeLines(c(sprintf("# %s", BANNER), "# collect step", capture.output(sessionInfo()),
               sprintf("# o2_honestdid.R sha256 %s", sha256_file(file.path(script_root, "exploratory", "o2_honestdid.R")))), file.path(outdir, "sessionInfo.txt"))
  hl <- if ("headline" %in% names(joint)) joint[headline == TRUE] else joint[0]
  sm <- c(sprintf("# %s", BANNER), "", sprintf("O2 collect, %d of %d cells merged%s.", length(have), length(ALL_CELLS), if (length(miss)) paste0("; MISSING: ", paste(miss, collapse = ", ")) else ""), "",
          sprintf("- HonestDiD %s, %s.", tim$honestdid_version[1], tim$r_version[1]),
          sprintf("- Cell wall-clock seconds: %s.", paste(sprintf("%s %.0f", tim$cell, tim$cell_seconds), collapse = "; ")),
          sprintf("- Breakdown rows: %d (headline = conservative flag for RM; SD has a single flag); statuses: %s.", nrow(brk), paste(sprintf("%s=%d", names(table(brk$status)), as.integer(table(brk$status))), collapse = "; ")),
          sprintf("- Joint (smaller of uid and stratum) headline rows: %d; %s.", if ("joint_status" %in% names(hl)) nrow(hl) else 0L, if ("joint_status" %in% names(hl) && nrow(hl)) paste(sprintf("%s=%d", names(table(hl$joint_status)), as.integer(table(hl$joint_status))), collapse = "; ") else "none"),
          sprintf("- uid vs stratum: %d comparisons, %d disagree (both are always shown; the joint file takes the weaker).", nrow(dis), if (nrow(dis)) sum(!dis$agree) else 0L),
          "- Files: o2_evals.csv (with joint_clears_* columns), o2_breakdown.csv, o2_joint.csv, o2_original.csv, o2_timing.csv, o2_input_hashes.csv, o2_disagreement.csv, sessionInfo*.txt, MANIFEST.sha256 (all files except summary.md).")
  writeLines(sm, file.path(outdir, "summary.md")); write_manifest()
  cat(sprintf("collected %d cells into %s\n", length(have), outdir)); quit(save = "no", status = 0)
}

# =====================================================================================================================
# shared by VALVE and CELL mode: input loading, calendars, one HonestDiD evaluation
# =====================================================================================================================
PRE_START <- as.Date("2022-06-09")
ALL_DAYS <- c(0:59, 61:95)
load_cell <- function(cell) {
  m <- regmatches(cell, regexec("^(daily|weekly)_(uid|stratum)_(D|A)$", cell))[[1]]
  if (length(m) != 4L) stop("--cell must be <daily|weekly>_<uid|stratum>_<D|A>", call. = FALSE)
  CAL <- m[2]; CL <- m[3]; OUT <- m[4]
  in_dir <- normalizePath(opt("in"), mustWork = TRUE)
  in_file <- file.path(in_dir, sprintf("o3_vcov_%s_%s_all.rds", OUT, CL))
  if (!file.exists(in_file)) stop("input not found: ", in_file, call. = FALSE)
  x <- readRDS(in_file)
  stopifnot(is.list(x), all(c("coef", "vcov", "day_index", "outcome", "cluster", "window") %in% names(x)))
  if (!identical(x$outcome, OUT) || !identical(x$cluster, CL) || !identical(x$window, "all")) stop("input file is not the outcome/cluster/window 'all' asked for", call. = FALSE)
  if (!identical(as.integer(x$day_index), ALL_DAYS)) stop("input day_index is not 0..59, 61..95", call. = FALSE)
  beta_all <- setNames(as.numeric(x$coef), ALL_DAYS); V_all <- unname(x$vcov); dimnames(V_all) <- list(ALL_DAYS, ALL_DAYS)
  if (!all(is.finite(beta_all)) || !all(is.finite(V_all))) stop("input has non-finite coefficient or covariance", call. = FALSE)
  if (max(abs(V_all - t(V_all))) > 1e-12 * max(1, max(abs(V_all)))) stop("input covariance is not symmetric", call. = FALSE)
  # NOT re-recentred: the input is already centred on the reference day. A reference coefficient other than exactly 0 (or a nonzero reference
  # row/column) means the file is not in the O3 recentred layout; subtracting it again would shift every coefficient, so refuse instead of guessing.
  rf <- as.character(CFG$ref_day)
  if (beta_all[[rf]] != 0 || any(V_all[rf, ] != 0) || any(V_all[, rf] != 0)) stop("input is not recentred on the reference day (coef/vcov of day 59 must be exactly 0): refusing to re-recentre", call. = FALSE)
  ev <- eigen(V_all, symmetric = TRUE, only.values = TRUE)$values
  if (min(ev) < -1e-10 * max(ev)) stop("input covariance is not positive semi-definite", call. = FALSE)
  n_clusters <- if (!is.null(x$n_clusters)) as.integer(x$n_clusters) else NA_integer_
  df_t <- if (!is.null(x$df)) as.numeric(x$df) else n_clusters - 1
  if (!SYNTHETIC && (is.na(n_clusters) || is.na(df_t) || df_t != n_clusters - 1)) stop("input lacks n_clusters / df = n_clusters - 1", call. = FALSE)
  if (has("signed-time-path")) {
    st <- fread(opt("signed-time-path")); stopifnot(all(c("outcome", "day_index", "coef") %in% names(st)))
    s <- st[outcome == OUT]
    for (k in CFG$post_days) { r <- s[day_index == k]; stopifnot(nrow(r) == 1L); if (abs(r$coef - beta_all[[as.character(k)]]) > 1e-9) stop(sprintf("coefficient day %d differs from the signed time path by %.3g (> 1e-9)", k, abs(r$coef - beta_all[[as.character(k)]])), call. = FALSE) }
    SIGNED_CHECK <- sprintf("equals signed time path on %d post days within 1e-9", length(CFG$post_days))
  } else SIGNED_CHECK <- "not checked (no --signed-time-path)"
  # the signed-record equality check is the real run's job; a synthetic run says so
  if (!SYNTHETIC && !has("signed-time-path")) stop("real inputs need --signed-time-path (plan 1.1: coefficients must equal the signed post_time_path.csv within 1e-9)", call. = FALSE)

  # ---- calendars ------------------------------------------------------------------------------------------------------
  if (CAL == "daily") {
    keep_pre <- as.character(CFG$pre_days); keep_post <- as.character(CFG$post_days)
    b <- beta_all[c(keep_pre, keep_post)]; S <- V_all[c(keep_pre, keep_post), c(keep_pre, keep_post)]
    npre <- length(keep_pre); npost <- length(keep_post); l_vec <- rep(1 / npost, npost)
    labels <- c(keep_pre, keep_post); Cmat <- NULL
  } else {
    wk <- data.table(
      bin = 1:13, label = c("9-19 Jun", "20-26 Jun", "27 Jun-3 Jul", "4-10 Jul", "11-17 Jul", "18-24 Jul", "25-31 Jul", "1-7 Aug (ref)", "9-14 Aug", "15-21 Aug", "22-28 Aug", "29 Aug-4 Sep", "5-12 Sep"),
      from = as.Date(c("2022-06-09", "2022-06-20", "2022-06-27", "2022-07-04", "2022-07-11", "2022-07-18", "2022-07-25", "2022-08-01", "2022-08-09", "2022-08-15", "2022-08-22", "2022-08-29", "2022-09-05")),
      to = as.Date(c("2022-06-19", "2022-06-26", "2022-07-03", "2022-07-10", "2022-07-17", "2022-07-24", "2022-07-31", "2022-08-07", "2022-08-14", "2022-08-21", "2022-08-28", "2022-09-04", "2022-09-12")))
    wk[, `:=`(day_from = as.integer(from - PRE_START), day_to = as.integer(to - PRE_START))][, n_days := day_to - day_from + 1L][, period := fifelse(bin <= 8L, "pre", "post")]
    REF_BIN <- 8L
    stopifnot(sum(wk[period == "post", n_days]) == 35L, sum(wk[period == "pre", n_days]) == 60L, identical(wk$n_days[REF_BIN], 7L), wk$day_from[1] == 0L, wk$day_to[13] == 95L)
    day2bin <- rbindlist(lapply(seq_len(nrow(wk)), function(i) data.table(day = wk$day_from[i]:wk$day_to[i], bin = wk$bin[i])))
    rows <- wk[bin != REF_BIN]   # 7 pre bins then 5 post bins, chronological
    Cmat <- t(vapply(rows$bin, function(bb) {
      v <- setNames(numeric(length(ALL_DAYS)), ALL_DAYS)
      dd <- day2bin[bin == bb, day]; v[as.character(dd)] <- 1 / length(dd)
      rr <- day2bin[bin == REF_BIN, day]; v[as.character(rr)] <- v[as.character(rr)] - 1 / length(rr)   # recentre ONCE, on the reference bin
      v
    }, numeric(length(ALL_DAYS))))
    dimnames(Cmat) <- list(rows$label, ALL_DAYS)
    stopifnot(max(abs(rowSums(Cmat))) < 1e-12)   # every row is a contrast: the reference bin is subtracted exactly once
    b <- drop(Cmat %*% beta_all); S <- Cmat %*% V_all %*% t(Cmat); S <- (S + t(S)) / 2
    npre <- sum(rows$period == "pre"); npost <- sum(rows$period == "post"); labels <- rows$label
    l_vec <- rows[period == "post", n_days] / sum(rows[period == "post", n_days])   # day-count weights (6,7,7,7,8)/35; equal bin weights are forbidden
    stopifnot(identical(as.integer(rows[period == "post", n_days]), c(6L, 7L, 7L, 7L, 8L)), npre == 7L, npost == 5L)
    # identity: day-count-weighted bin post mean == 35-day mean minus the 1-7 Aug mean
    id_gap <- abs(sum(l_vec * b[(npre + 1L):(npre + npost)]) - (mean(beta_all[as.character(CFG$post_days)]) - mean(beta_all[as.character(53:59)])))
    if (id_gap > 1e-12) stop(sprintf("weekly target identity failed (%.3g)", id_gap), call. = FALSE)
  }
  names(b) <- labels; dimnames(S) <- list(labels, labels)
  stopifnot(length(b) == npre + npost, abs(sum(l_vec) - 1) < 1e-12)
  pidx <- (npre + 1L):(npre + npost)
  target <- sum(l_vec * b[pidx]); target_se <- sqrt(drop(t(l_vec) %*% S[pidx, pidx] %*% l_vec))   # target_se = sdTheta of deltarm.R: sqrt(l' Sigma_post l)
  list(cell = cell, CAL = CAL, CL = CL, OUT = OUT, in_file = in_file, b = b, S = S, npre = npre, npost = npost, l_vec = l_vec, target = target, sdT = target_se, df_t = df_t,
       n_clusters = n_clusters, SIGNED_CHECK = SIGNED_CHECK)
}

# One HonestDiD evaluation. restr "RM": rm = list(method, gp, glb, gub) with the theta grid [glb, gub]; "SD": FLCI (no grid). Every call passes seed = 0 explicitly and parallel = FALSE
# (valve step (i) parallelises across processes, never inside a call); postPeriodMomentsOnly is not an argument of these wrappers, the pinned default TRUE runs (asserted in load_honestdid).
# An evaluation over --point-seconds is labelled "timeout": HonestDiD's .CustomErrorHandling swallows the time-limit error into a printed message and returns NULL, so the message is captured
# (and the elapsed time checked) to tell it from an empty interval. HonestDiD's "CI is open at one of the endpoints" warning, like a lo/hi equal to a grid edge, makes the status "edge: ..." and voids
# THAT interval only (addendum A2 item 2). Warnings are recorded, never muffled (A2 item 7): `warnings` holds every distinct package warning of the evaluation; `eval_flag` is set when HonestDiD
# reported "LP for eta did not converge" (the evaluation is otherwise unaltered).
HD_RM <- function(args) do.call(HonestDiD::createSensitivityResults_relativeMagnitudes, args)
HD_SD <- function(args) do.call(HonestDiD::createSensitivityResults, args)
hd_eval <- function(restr, cl, M, rm = NULL) {
  t0 <- proc.time()[["elapsed"]]; msgs <- character(); warns <- character(); errtxt <- character()
  on.exit(setTimeLimit(elapsed = Inf), add = TRUE)
  out <- tryCatch({
    if (is.finite(CFG$point_seconds)) setTimeLimit(elapsed = CFG$point_seconds, transient = TRUE)
    withCallingHandlers({
      if (restr == "RM") HD_RM(list(betahat = cl$b, sigma = cl$S, numPrePeriods = cl$npre, numPostPeriods = cl$npost, bound = CFG$rm_bound,
        method = rm$method, Mbarvec = M, l_vec = cl$l_vec, alpha = CFG$alpha, gridPoints = rm$gp, grid.lb = rm$glb, grid.ub = rm$gub, parallel = FALSE, seed = CFG$seed))
      else HD_SD(list(betahat = cl$b, sigma = cl$S, numPrePeriods = cl$npre, numPostPeriods = cl$npost, method = CFG$sd_method, Mvec = M, l_vec = cl$l_vec,
        alpha = CFG$alpha, seed = CFG$seed))
    }, message = function(m) { msgs <<- c(msgs, conditionMessage(m)); invokeRestart("muffleMessage") },
       warning = function(w) { warns <<- c(warns, conditionMessage(w)) })   # recorded, NOT muffled
  }, error = function(e) { errtxt <<- conditionMessage(e); NULL })
  setTimeLimit(elapsed = Inf)
  secs <- proc.time()[["elapsed"]] - t0
  lo <- as.numeric(out$lb); hi <- as.numeric(out$ub)
  bad <- length(lo) != 1L || length(hi) != 1L || !is.finite(lo) || !is.finite(hi) || lo > hi
  timed_out <- any(grepl("time limit|elapsed", c(msgs, errtxt), ignore.case = TRUE)) || (bad && is.finite(CFG$point_seconds) && secs >= 0.98 * CFG$point_seconds)
  res <- list(lo = NA_real_, hi = NA_real_, secs = secs, dtheta = NA_real_, glb = NA_real_, gub = NA_real_, gp = NA_integer_, method = if (restr == "RM") rm$method else CFG$sd_method,
              warnings = if (length(warns)) gsub("[\r\n,]+", " ", paste(unique(warns), collapse = " | ")) else "", eval_flag = if (any(grepl("LP for eta did not converge", warns))) "LP for eta did not converge" else NA_character_)
  if (restr == "RM") { res$glb <- rm$glb; res$gub <- rm$gub; res$gp <- as.integer(rm$gp); res$dtheta <- (rm$gub - rm$glb) / (rm$gp - 1) }
  if (timed_out) { res$status <- "timeout"; return(res) }
  if (bad) { res$status <- paste0("failed: ", if (length(errtxt) || any(grepl("error:", msgs))) gsub("[\r\n,]+", " ", paste(c(errtxt, grep("error:", msgs, value = TRUE)), collapse = " ")) else "empty, infinite or non-finite interval"); return(res) }
  edge <- ""
  if (restr == "RM") edge <- paste0(if (abs(lo - rm$glb) <= 1e-9 * max(1, abs(rm$glb))) " lo_at_grid_edge" else "", if (abs(hi - rm$gub) <= 1e-9 * max(1, abs(rm$gub))) " hi_at_grid_edge" else "",
                                    if (any(grepl("open at one of the endpoints", warns))) " open_endpoint" else "")
  res$lo <- lo; res$hi <- hi; res$status <- if (nzchar(edge)) paste0("edge:", edge) else "ok"
  res
}
# theta grid [c0 - k sdT, c0 + k sdT]; c0 = 0 for the planned setting (package default, +-20 sd), c0 = l'beta_post (cl$target) under valve step (iii) (addendum A2 item 1)
rm_setup <- function(cl, method, gp, k, center = "zero", glb = NA_real_, gub = NA_real_) {
  c0 <- if (identical(center, "estimate")) cl$target else 0
  list(method = method, gp = as.integer(gp), glb = if (is.finite(glb)) glb else c0 - k * cl$sdT, gub = if (is.finite(gub)) gub else c0 + k * cl$sdT)
}

# =====================================================================================================================
# VALVE MODE: one timing probe per calendar, ordered fallback chain, plan shared by every cell of the calendar
# =====================================================================================================================
if (has("valve")) {
  outdir <- make_outdir(opt("out"))
  HD_VERSION <- load_honestdid()
  cals <- strsplit(opt("calendars", "daily,weekly"), ",")[[1]]
  if (!all(cals %in% CALENDARS)) stop("--calendars must be daily and/or weekly", call. = FALSE)
  cls <- setNames(lapply(cals, function(cal) load_cell(paste0(cal, "_uid_D"))), cals)   # the real uid/D cell stands for its calendar (the four cells of a calendar share size and scale)
  PROBES <- list()
  probe <- function(cal, method, gp, k, center) {   # ONE RM call at Mbar = 0; the interval is discarded unread: only seconds and a status class leave this function
    r <- hd_eval("RM", cls[[cal]], 0, rm_setup(cls[[cal]], method, gp, k, center, CFG$rm_grid_lb, CFG$rm_grid_ub))
    cls_ <- probe_class(r$status)
    PROBES[[length(PROBES) + 1L]] <<- data.table(calendar = cal, restriction = "RM", probed_cell = paste0(cal, "_uid_D"), method = method, grid_points = as.integer(gp), grid_k = k, grid_center = center, secs = r$secs, status_class = cls_)
    list(secs = r$secs, status = cls_)
  }
  planned_gp <- CFG$rm_grid_points %||% CFG$rm_grid_points_default
  # planned evaluations per cell (uid_D, uid_A, stratum_D, stratum_A): RM grid points + bisections, each threshold x flag (nominal, conservative) counted separately
  cell_evals <- setNames(lapply(cals, function(cal) unlist(lapply(CLUSTERS, function(cc) vapply(OUTCOMES, function(o) as.numeric(length(CFG$rm_grid) + length(CFG$thresholds[[o]]) * 2L * CFG$bisections), 0)))), cals)
  # SD time of a cell = one measured FLCI point at M = 0 (daily only; weekly smoothness is not computed) x that cell's planned SD evaluations (grid + bisections, one flag). The cells have not run at valve time,
  # so the measured time is projected from this one point; its interval is discarded unread like the RM probe's. A failed SD probe contributes 0 (its status is in the probes file).
  cell_sd <- setNames(lapply(cals, function(cal) rep(0, 4L)), cals)
  if ("daily" %in% cals && "SD" %in% CFG$restrictions) {
    r <- hd_eval("SD", cls[["daily"]], 0)
    PROBES[[length(PROBES) + 1L]] <- data.table(calendar = "daily", restriction = "SD", probed_cell = "daily_uid_D", method = CFG$sd_method, grid_points = NA_integer_, grid_k = NA_real_, grid_center = NA_character_, secs = r$secs, status_class = probe_class(r$status))
    if (startsWith(r$status, "ok")) cell_sd[["daily"]] <- unlist(lapply(CLUSTERS, function(cc) vapply(OUTCOMES, function(o) r$secs * (length(CFG$sd_grid) + length(CFG$thresholds[[o]]) * CFG$bisections), 0)))
  }
  v <- valve_chain(cals, probe, CFG$valve_cell_seconds, CFG$valve_total_seconds, CFG$valve_procs, cell_evals = cell_evals, cell_sd = cell_sd,
                   planned_method = CFG$rm_method, planned_gp = planned_gp, planned_k = CFG$rm_grid_k_default, gp_floor = CFG$valve_gp_floor, k_fallback = CFG$valve_k)
  wcsv(v$plan, "o2_valve_plan.csv"); wcsv(v$log, "o2_valve_log.csv"); wcsv(rbindlist(PROBES), "o2_valve_probes.csv")
  writeLines(c(sprintf("# %s", BANNER), capture.output(sessionInfo()), "", sprintf("# HonestDiD %s from %s", HD_VERSION, find.package("HonestDiD")),
               sprintf("# o2_honestdid.R sha256 %s", sha256_file(file.path(script_root, "exploratory", "o2_honestdid.R")))), file.path(outdir, "sessionInfo.txt"))
  write_manifest(exclude = character())
  cat(sprintf("valve done: %s%s\n", paste(sprintf("%s: %s", v$plan$calendar, v$plan$fallback), collapse = " | "), if (v$final_trigger) " | TRIGGER STILL FIRES after the last fallback (reported)" else ""))
  quit(save = "no", status = 0)
}

# =====================================================================================================================
# CELL MODE
# =====================================================================================================================
cell <- opt("cell")
outdir <- make_outdir(opt("out"))
HD_VERSION <- load_honestdid()
cl <- load_cell(cell)
CAL <- cl$CAL; CL <- cl$CL; OUT <- cl$OUT; TAUS <- CFG$thresholds[[OUT]]
in_file <- cl$in_file; b <- cl$b; S <- cl$S; npre <- cl$npre; npost <- cl$npost; l_vec <- cl$l_vec; df_t <- cl$df_t

# ---- RM setting in force: the valve plan (shared by uid and stratum of this calendar), else the defaults (method C-LF, gridPoints 1000, +-20 sd) ----------------------------------------------------
PLAN <- NULL
if (has("valve-plan")) {
  pl <- fread(opt("valve-plan")); PLAN <- pl[calendar == CAL]
  if (nrow(PLAN) != 1L) stop("valve plan has no unique row for calendar ", CAL, call. = FALSE)
} else if (!SYNTHETIC) stop("real inputs need --valve-plan (run --valve first; the plan is shared by the uid and stratum cells of a calendar)", call. = FALSE)
RM_METHOD <- if (!is.null(PLAN)) PLAN$rm_method else CFG$rm_method
RM_GP <- if (!is.null(PLAN)) as.integer(PLAN$rm_grid_points) else (CFG$rm_grid_points %||% CFG$rm_grid_points_default)
RM_K <- if (!is.null(PLAN)) PLAN$rm_grid_k else if (is.finite(CFG$rm_grid_lb) || is.finite(CFG$rm_grid_ub)) NA_real_ else CFG$rm_grid_k_default
RM_DROPPED <- !is.null(PLAN) && !isTRUE(as.logical(PLAN$rm_computed))
if (!is.null(PLAN) && !all(c("rm_grid_center", "exhausted") %in% names(PLAN))) stop("valve plan lacks rm_grid_center / exhausted (written by --valve of this version)", call. = FALSE)
RM_CENTER <- if (!is.null(PLAN)) PLAN$rm_grid_center else "zero"
VALVE_EXHAUSTED <- !is.null(PLAN) && isTRUE(as.logical(PLAN$exhausted))
VALVE_LABEL <- if (VALVE_EXHAUSTED) "valve exhausted" else NA_character_
RMS <- rm_setup(cl, RM_METHOD, RM_GP, RM_K, RM_CENTER, CFG$rm_grid_lb, CFG$rm_grid_ub)
FALLBACK <- if (!is.null(PLAN)) PLAN$fallback else "none (no valve plan; synthetic test)"

# ---- one evaluation, cached on everything that changes the result ----------------------------------------------------------------------------------
CACHE <- new.env(); EVALS <- list()
add_eval <- function(kind, restr, M, r, status, secs, threshold = NA_real_, flag = NA_character_) {
  EVALS[[length(EVALS) + 1L]] <<- data.table(kind = kind, flag = flag, restriction = restr, M = M, lo = r$lo, hi = r$hi, status = status, secs = secs, threshold = threshold, method = r$method,
                                             grid_points = r$gp, grid_lb = r$glb, grid_ub = r$gub, dtheta = r$dtheta, warnings = r$warnings %||% "", eval_flag = r$eval_flag %||% NA_character_)
}
eval_point <- function(restr, M, kind, threshold = NA_real_, flag = NA_character_) {
  rm <- if (restr == "RM") RMS else NULL
  key <- eval_key(restr, M, if (restr == "RM") RM_METHOD else CFG$sd_method, if (restr == "RM") RMS$gp else NA, if (restr == "RM") RMS$glb else NA_real_, if (restr == "RM") RMS$gub else NA_real_, CFG$alpha, CFG$rm_bound, CFG$seed)
  if (!is.null(r <- CACHE[[key]])) { add_eval(kind, restr, M, r, paste0(r$status, " (cached)"), 0, threshold, flag); return(r) }
  r <- hd_eval(restr, cl, M, rm)
  CACHE[[key]] <- r; add_eval(kind, restr, M, r, r$status, r$secs, threshold, flag)
  r
}

# ---- original (unrestricted) intervals, normal vs t -----------------------------------------------------------------------
orig <- tryCatch(suppressMessages(HonestDiD::constructOriginalCS(betahat = b, sigma = S, numPrePeriods = npre, numPostPeriods = npost, l_vec = l_vec, alpha = CFG$alpha)),
                 error = function(e) NULL)
zc <- qnorm(1 - CFG$alpha / 2); tc <- if (is.finite(df_t)) qt(1 - CFG$alpha / 2, df_t) else NA_real_
target <- cl$target; target_se <- cl$sdT
orig_dt <- data.table(calendar = CAL, cluster = CL, outcome = OUT, target = target, se = target_se, normal_lo = target - zc * target_se, normal_hi = target + zc * target_se,
                      t_df = df_t, t_lo = target - tc * target_se, t_hi = target + tc * target_se,
                      honestdid_original_lo = if (is.null(orig)) NA_real_ else as.numeric(orig$lb), honestdid_original_hi = if (is.null(orig)) NA_real_ else as.numeric(orig$ub),
                      n_pre = npre, n_post = npost, target_weights = paste(sprintf("%.10g", l_vec), collapse = " "))

# ---- grids ------------------------------------------------------------------------------------------------------------------
t_cell0 <- proc.time()[["elapsed"]]
RUN_RM <- "RM" %in% CFG$restrictions && !RM_DROPPED; RUN_SD <- "SD" %in% CFG$restrictions
restrictions <- list()
if (RUN_RM) restrictions$RM <- CFG$rm_grid
if (RUN_SD && CAL == "daily") restrictions$SD <- CFG$sd_grid
GRID <- list()
for (rs in names(restrictions)) GRID[[rs]] <- rbindlist(lapply(restrictions[[rs]], function(M) { r <- eval_point(rs, M, "grid"); data.table(M = M, lo = r$lo, hi = r$hi, status = r$status, dtheta = r$dtheta) }))

# ---- breakdown per restriction x threshold x flag (addendum A1: nominal + conservative for RM, headline = conservative; SD/FLCI: one flag) --------------------------------
BRK <- list()
brk_row <- function(rs, tau, flag, headline, hs, status, d = list()) {
  data.table(calendar = CAL, cluster = CL, outcome = OUT, restriction = rs, threshold = tau, flag = flag, headline = headline, headline_status = hs, status = status,
             first_grid_M = d$first_grid_M %||% NA_real_, largest_grid_M = d$largest_grid_M %||% NA_real_,
             last_clearing_M = d$last_clearing_M %||% NA_real_, first_losing_M = d$first_losing_M %||% NA_real_,
             bracket_lo = d$bracket_lo %||% NA_real_, bracket_hi = d$bracket_hi %||% NA_real_, n_bisections_done = d$n_bis %||% 0L,
             nonmonotone = d$nonmono %||% NA, reclear_points = d$reclear %||% NA_character_, failure_points = d$fails %||% NA_character_,
             note = d$note %||% NA_character_, bracket_flag = d$bracket_flag %||% NA_character_, valve_label = VALVE_LABEL)
}
for (rs in CFG$restrictions) for (tau in TAUS) {
  if (rs == "SD" && CAL == "weekly") { BRK[[length(BRK) + 1L]] <- brk_row(rs, tau, "single", TRUE, CFG$weekly_sd, CFG$weekly_sd); next }
  if (rs == "RM" && RM_DROPPED) {
    for (fl in c("nominal", "conservative")) BRK[[length(BRK) + 1L]] <- brk_row(rs, tau, fl, fl == "conservative", CFG$dropped_rm, CFG$dropped_rm)
    next
  }
  sf <- scan_flags(GRID[[rs]], tau, function(M, flag) eval_point(rs, M, "bisect", tau, flag), CFG$bisections, theta_grid = rs == "RM")
  if (rs == "RM") {
    BRK[[length(BRK) + 1L]] <- brk_row(rs, tau, "nominal", FALSE, sf$headline_status, sf$nominal$status, sf$nominal)
    BRK[[length(BRK) + 1L]] <- brk_row(rs, tau, "conservative", TRUE, sf$headline_status, sf$conservative$status, sf$conservative)
  } else BRK[[length(BRK) + 1L]] <- brk_row(rs, tau, "single", TRUE, sf$headline_status, sf$nominal$status, sf$nominal)
}
brk <- rbindlist(BRK)
if (CAL == "weekly") brk[restriction == "SD", `:=`(bracket_lo = NA_real_, bracket_hi = NA_real_)]

# ---- outputs ------------------------------------------------------------------------------------------------------------------
if (RM_DROPPED && "RM" %in% CFG$restrictions) add_eval("valve", "RM", NA_real_, list(lo = NA_real_, hi = NA_real_, method = RM_METHOD, gp = NA_integer_, glb = NA_real_, gub = NA_real_, dtheta = NA_real_), CFG$dropped_rm, 0)
if (!length(EVALS)) add_eval("none", "none", NA_real_, list(lo = NA_real_, hi = NA_real_, method = NA_character_, gp = NA_integer_, glb = NA_real_, gub = NA_real_, dtheta = NA_real_), "no evaluation requested", 0)
evs <- rbindlist(EVALS)
for (tau in TAUS) {
  set(evs, j = paste0("clears_", clabel(tau), "_nominal"), value = mapply(function(s, h) clears_hi(s, h, tau), evs$status, evs$hi, USE.NAMES = FALSE))   # for SD/FLCI rows this single column is the one flag
  set(evs, j = paste0("clears_", clabel(tau), "_conservative"), value = mapply(function(s, h, d, r) if (r == "RM") clears_hi(s, h, tau, d) else NA, evs$status, evs$hi, evs$dtheta, evs$restriction, USE.NAMES = FALSE))
}
evs[, valve_label := fifelse(restriction == "RM", VALVE_LABEL, NA_character_)]
evs <- cbind(data.table(calendar = CAL, cluster = CL, outcome = OUT), evs)
if (CAL == "weekly" && RUN_SD) evs <- rbind(evs, data.table(calendar = CAL, cluster = CL, outcome = OUT, kind = "grid", restriction = "SD", M = NA_real_, status = CFG$weekly_sd), fill = TRUE)
wcsv(evs, "o2_evals.csv"); wcsv(brk, "o2_breakdown.csv"); wcsv(orig_dt, "o2_original.csv")
n_rm_run <- sum(evs$restriction == "RM" & evs$kind %in% c("grid", "bisect") & !grepl("cached", evs$status))
timing <- data.table(calendar = CAL, cluster = CL, outcome = OUT, cell = cell, rm_method = RM_METHOD, rm_grid_points = RM_GP, rm_grid_k = RM_K, rm_grid_center = RM_CENTER, rm_computed = !RM_DROPPED, valve_exhausted = VALVE_EXHAUSTED, fallback = FALLBACK,
                     valve_probe_secs = if (!is.null(PLAN)) PLAN$probe_secs else NA_real_, valve_projected_T_secs = if (!is.null(PLAN)) PLAN$projected_T_secs else NA_real_,
                     rm_points_run = n_rm_run, cell_seconds = proc.time()[["elapsed"]] - t_cell0, honestdid_version = HD_VERSION, r_version = R.version.string, pid = Sys.getpid())
wcsv(timing, "o2_timing.csv")
wcsv(data.table(input = "o3_vcov", path = if (SYNTHETIC) basename(in_file) else in_file, sha256 = sha256_file(in_file), recentred_input_check = "coef/vcov of day 59 exactly 0; not re-recentred",
                signed_time_path_check = cl$SIGNED_CHECK, valve_plan_sha256 = if (has("valve-plan")) sha256_file(opt("valve-plan")) else NA_character_, honestdid_lib = dirname(find.package("HonestDiD"))), "o2_input_hashes.csv")
writeLines(c(sprintf("# %s", BANNER), capture.output(sessionInfo()), "",
             sprintf("# HonestDiD %s from %s", HD_VERSION, find.package("HonestDiD")),
             sprintf("# o2_honestdid.R sha256 %s", sha256_file(file.path(script_root, "exploratory", "o2_honestdid.R")))), file.path(outdir, "sessionInfo.txt"))
write_manifest(exclude = character())
cat(sprintf("cell %s done in %.1fs (%d RM evaluations)\n", cell, as.numeric(difftime(Sys.time(), t_script, units = "secs")), n_rm_run))
