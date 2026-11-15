#!/usr/bin/env Rscript
# EXPLORATORY, POST-RESULTS, dated 2026-10-28; not confirmatory; does not reclassify H1.
# Synthetic-only tests of exploratory/o2_honestdid.R (never touches out/). Usage, from the repo root:
#   R_LIBS_USER=exploratory/rlib:<pinned-library> Rscript exploratory/tests/test_o2_synthetic.R
# Runtime ~10-12 min: HonestDiD is slow (one daily FLCI point ~1 min at 59 pre periods, ~10 s at 6; an RM point 20-60 s even on the 7-bin weekly calendar),
# so the daily checks use --pre-keep 6 (synthetic-only shortcut: the exact-linear pre-trend gives the same analytic answer for any number of pre periods)
# and the rule itself is tested against a mock evaluator.
suppressPackageStartupMessages(library(data.table))
if (!requireNamespace("HonestDiD", quietly = TRUE)) { cat("SKIPPED all: HonestDiD is not installed (exploratory library; see install_r_packages.R --exploratory)\n"); quit(status = 0) }
args <- commandArgs(FALSE); here <- dirname(sub("^--file=", "", grep("^--file=", args, value = TRUE)[1]))
root <- normalizePath(file.path(here, "..", ".."))
script <- file.path(root, "exploratory", "o2_honestdid.R")
source(file.path(root, "exploratory", "o2_lib.R"))
tmp <- tempfile("o2test"); dir.create(tmp)
fails <- 0L; npass <- 0L
check <- function(name, ok, info = "") { if (isTRUE(ok)) { npass <<- npass + 1L; cat("ok   ", name, "\n") } else { fails <<- fails + 1L; cat("FAIL ", name, info, "\n") }; flush.console() }
run <- function(...) { out <- suppressWarnings(system2("Rscript", c(shQuote(script), ...), stdout = TRUE, stderr = TRUE)); list(out = out, status = attr(out, "status") %||% 0L) }
rd <- function(d, f) fread(file.path(tmp, d, f))
q <- function(...) shQuote(file.path(tmp, ...))

# ---- the breakdown rule against a mock evaluator -----------------------------------------------------------------------------------------------------------
grid <- seq(0, 1, by = 0.1)
mk <- function(f) data.table(M = grid, hi = vapply(grid, f, 0), status = "ok")
f_nm <- function(M) if (M < 0.45 || M >= 0.85) -1 else 1
r <- scan_breakdown(mk(f_nm), 0, function(M) list(hi = f_nm(M), status = "ok"), 10L)
check("mock: non-monotone flagged, re-clearing point reported", r$nonmono && grepl("0.9", r$reclear) && r$last_clearing_M == 0.4 && r$first_losing_M == 0.5)
check("mock: 10 bisections, all inside [0.4,0.5], bracket width 0.1/1024, contains 0.45", length(r$bisection) == 10L && all(vapply(r$bisection, function(z) z$M > 0.4 && z$M < 0.5, TRUE)) && abs(r$bracket_hi - r$bracket_lo - 0.1 / 1024) < 1e-12 && r$bracket_lo <= 0.45 && 0.45 <= r$bracket_hi)
r <- scan_breakdown(mk(function(M) 1), 0, function(M) stop("must not bisect"), 10L)
check("mock: no initial clearance stops", r$status == "no initial clearance" && length(r$bisection) == 0L)
r <- scan_breakdown(mk(function(M) -1), 0, function(M) stop("must not bisect"), 10L)
check("mock: no crossing within the grid reports the largest grid value", r$status == "no crossing within the grid" && r$largest_grid_M == 1 && r$last_clearing_M == 1)
r <- scan_breakdown(mk(function(M) if (M < 0.45) -1 else 1), 0, function(M) list(hi = NA_real_, status = "failed: solver"), 10L)
check("mock: failure inside the bracket is reported where it failed, never clear or lost, flagged 'failure in bracket' (A2 item 3)", grepl("failed at M=0.45", r$note) && r$n_bis == 0L && r$bracket_lo == 0.4 && r$bracket_hi == 0.5 && identical(r$bracket_flag, "failure in bracket"))
r <- scan_breakdown(mk(function(M) if (M < 0.45) -1 else 1), 0, function(M) list(hi = if (M < 0.47) -1 else 1, status = "ok"), 3L)
check("mock: a bisection without failure carries no bracket flag", is.na(r$bracket_flag) && r$n_bis == 3L)
g2 <- mk(function(M) -1); g2[abs(M - 0.3) < 1e-9, `:=`(hi = NA_real_, status = "failed: x")]
r <- scan_breakdown(g2, 0, function(M) stop("no"), 10L)
check("mock: a failed grid point before the first loss is listed and makes the scan 'undetermined beyond M = m' (m = last point of the clearing prefix), never a clearance", grepl("^undetermined beyond M=0.2", r$status) && r$fails == "0.3" && r$last_clearing_M == 0.2 && length(r$bisection) == 0L)
g2b <- mk(function(M) if (M < 0.55) -1 else 1); g2b[abs(M - 0.2) < 1e-9, `:=`(hi = NA_real_, status = "timeout")]
r <- scan_breakdown(g2b, 0, function(M) stop("no"), 10L)
check("mock: a timed-out point before a later loss is also undetermined (no bracket is bisected across it)", grepl("^undetermined beyond M=0.1", r$status) && r$last_clearing_M == 0.1)
g2c <- mk(function(M) if (M < 0.45) -1 else 1); g2c[abs(M - 0.8) < 1e-9, `:=`(hi = NA_real_, status = "failed: x")]
r <- scan_breakdown(g2c, 0, function(M) list(hi = if (M < 0.45) -1 else 1, status = "ok"), 3L)
check("mock: a failure AFTER the first loss does not change the bracket (listed in the failure points only)", r$status == "breakdown bracketed" && r$fails == "0.8" && r$first_losing_M == 0.5)
g3 <- mk(function(M) -1); g3[abs(M - 0.6) < 1e-9, `:=`(status = "edge: hi_at_grid_edge")]
check("mock: an upper bound clipped by the RM grid edge is not a clearance", is.na(clears_hi("ok hi_at_grid_edge", -1, 0)) && grepl("0.6", scan_breakdown(g3, 0, function(M) stop("no"), 10L)$fails))
local({ union_grid <- function(...) sort(unique(round(unlist(list(...)), 12)))
  rm <- union_grid(seq(0, 0.30, by = 0.02), seq(0.1, 2, by = 0.1), seq(0.5, 3, by = 0.25)); sd <- union_grid(c(0, 0.000025, 0.00005), seq(0.0001, 0.001, by = 0.0001), c(0.005, 0.01, 0.025, 0.05))
  check("grids: |RM| = 40, |SD| = 17", length(rm) == 40L && length(sd) == 17L)
  src <- readLines(script); check("grids in the script are the same expressions", any(grepl("seq(0, 0.30, by = 0.02), seq(0.1, 2, by = 0.1), seq(0.5, 3, by = 0.25)", src, fixed = TRUE)) && any(grepl("c(0, 0.000025, 0.00005), seq(0.0001, 0.001, by = 0.0001), c(0.005, 0.01, 0.025, 0.05)", src, fixed = TRUE))) })

# ---- S32b fix round: lo-edge void, nominal vs conservative flags, joint rule, cache key, valve chain (all mock, no HonestDiD) -------------------------------------------------
check("mock: a hit at the LOWER grid edge voids the grid like the upper edge", is.na(clears_hi("edge: lo_at_grid_edge", -1, 0)) && is.na(clears_hi("edge: hi_at_grid_edge", -1, 0)) && is.na(clears_hi("edge: open_endpoint", -1, 0)) && is.na(clears_hi("ok lo_at_grid_edge", -1, 0)) && is.na(clears_hi("ok hi_at_grid_edge", -1, 0)) && is.na(clears_hi("ok open_endpoint", -1, 0)) && isTRUE(clears_hi("ok", -1, 0)))
check("mock: conservative clearance is upper bound + theta step < tau", isTRUE(clears_hi("ok", -0.04, 0, 0.03)) && isFALSE(clears_hi("ok", -0.04, 0, 0.05)) && isTRUE(clears_hi("ok", -0.04, 0, 0)))
local({
  gd <- seq(0, 1, by = 0.1); dth <- 0.05; hf <- function(M) M - 0.46      # nominal loses at M >= 0.46, conservative (hi + 0.05 < 0) at M >= 0.41
  g <- data.table(M = gd, hi = hf(gd), status = "ok", dtheta = dth)
  ev <- function(M, flag) list(hi = hf(M), status = "ok", dtheta = dth)
  sf <- scan_flags(g, 0, ev, 10L, TRUE)
  check("flags: nominal bracket contains 0.46, conservative bracket contains 0.41, each inside [0.4,0.5], ten bisections each", sf$nominal$bracket_lo <= 0.46 && 0.46 <= sf$nominal$bracket_hi && sf$conservative$bracket_lo <= 0.41 && 0.41 <= sf$conservative$bracket_hi &&
        sf$nominal$bracket_lo >= 0.4 && sf$conservative$bracket_hi <= 0.5 && length(sf$nominal$bisection) == 10L && length(sf$conservative$bisection) == 10L)
  check("flags: conservative breakdown is at or below the nominal one; headline = conservative", sf$conservative$bracket_lo <= sf$nominal$bracket_lo && sf$headline == "conservative" && sf$headline_status == sf$conservative$status)
  g0 <- data.table(M = gd, hi = gd * 0.1 - 0.02, status = "ok", dtheta = dth)   # hi(0) = -0.02 < 0 clears nominally; -0.02 + 0.05 > 0 does not clear conservatively
  sf0 <- scan_flags(g0, 0, function(M, flag) list(hi = M * 0.1 - 0.02, status = "ok", dtheta = dth), 10L, TRUE)
  check("flags: only the conservative flag fails at 0 -> 'no initial clearance (conservative); nominal clears at 0', headline 'no initial clearance'", grepl("^no initial clearance \\(conservative\\); nominal clears at 0$", sf0$conservative$status) && sf0$headline_status == "no initial clearance" && sf0$nominal$status != "no initial clearance")
  gs <- copy(g)[, dtheta := NULL]
  sfs <- scan_flags(gs, 0, function(M, flag) list(hi = hf(M), status = "ok"), 4L, FALSE)
  check("flags: SD/FLCI cells have one flag (no conservative scan)", is.null(sfs$conservative) && sfs$headline == "nominal" && length(sfs$nominal$bisection) == 4L)
})
check("cache key changes with M, method, gridPoints, grid.lb, grid.ub, alpha, bound, seed and restriction", local({
  k0 <- eval_key("RM", 0.5, "C-LF", 1000L, -1, 1, 0.05, "deviation from parallel trends", 0)
  ks <- c(eval_key("RM", 0.6, "C-LF", 1000L, -1, 1, 0.05, "deviation from parallel trends", 0), eval_key("RM", 0.5, "Conditional", 1000L, -1, 1, 0.05, "deviation from parallel trends", 0), eval_key("RM", 0.5, "C-LF", 200L, -1, 1, 0.05, "deviation from parallel trends", 0),
          eval_key("RM", 0.5, "C-LF", 1000L, -0.5, 1, 0.05, "deviation from parallel trends", 0), eval_key("RM", 0.5, "C-LF", 1000L, -1, 0.5, 0.05, "deviation from parallel trends", 0), eval_key("RM", 0.5, "C-LF", 1000L, -1, 1, 0.1, "deviation from parallel trends", 0),
          eval_key("RM", 0.5, "C-LF", 1000L, -1, 1, 0.05, "deviation from linear trend", 0), eval_key("RM", 0.5, "C-LF", 1000L, -1, 1, 0.05, "deviation from parallel trends", 1), eval_key("SD", 0.5, "C-LF", 1000L, -1, 1, 0.05, "deviation from parallel trends", 0))
  !any(ks == k0) && !anyDuplicated(c(k0, ks))
}))
check("script builds its cache key with eval_key (not an ad-hoc string)", any(grepl("key <- eval_key(", readLines(script), fixed = TRUE)))
check("script: no coarse:/continue/--max-seconds/--on-slow/combine-rule remnants", !any(grepl("coarse|on-slow|on_slow|max-seconds|max_seconds|combine|probe-mbar|probe_mbar", readLines(script))))
ja <- list(status = "breakdown bracketed", bracket_lo = 0.30, bracket_hi = 0.31, last_clearing_M = 0.3); jb <- list(status = "breakdown bracketed", bracket_lo = 0.20, bracket_hi = 0.21, last_clearing_M = 0.2)
j <- joint_breakdown(ja, jb)
check("joint: smaller of the two breakdowns, binding cluster named", j$status == "breakdown bracketed" && j$bracket_lo == 0.20 && j$bracket_hi == 0.21 && j$binding == "stratum")
check("joint: min, not max (either argument order gives the smaller)", joint_breakdown(jb, ja)$bracket_lo == 0.20 && joint_breakdown(ja, jb)$bracket_lo == 0.20)
nic <- list(status = "no initial clearance", bracket_lo = NA_real_, bracket_hi = NA_real_, last_clearing_M = NA_real_); nicc <- list(status = "no initial clearance (conservative); nominal clears at 0", bracket_lo = NA_real_, bracket_hi = NA_real_)
check("joint: either side without initial clearance -> 'no initial clearance' (no NA-on-one-side)", joint_breakdown(ja, nic)$status == "no initial clearance" && joint_breakdown(nic, ja)$status == "no initial clearance" && joint_breakdown(ja, nicc)$status == "no initial clearance" && is.na(joint_breakdown(ja, nic)$bracket_lo))
ncx <- list(status = "no crossing within the grid", bracket_lo = NA_real_, bracket_hi = NA_real_, last_clearing_M = 3)
check("joint: no crossing on one side takes the other side's breakdown; both no crossing stays no crossing", joint_breakdown(ja, ncx)$bracket_lo == 0.30 && joint_breakdown(ncx, ncx)$status == "no crossing within the grid" && joint_breakdown(ncx, ncx)$bracket_lo == 3)
und <- function(m) list(status = sprintf("undetermined beyond M=%g (failure before first loss)", m), bracket_lo = NA_real_, bracket_hi = NA_real_, last_clearing_M = m)
jj <- joint_breakdown(ja, und(0.2))
check("joint (T-F1): a failed side is never clearance - undetermined side bounds the joint by its m, qualifier 'undetermined', the other side's larger breakdown is NOT returned", startsWith(jj$status, "undetermined beyond M=0.2") && jj$bracket_lo == 0.2 && is.na(jj$bracket_hi) && jj$qualifier == "undetermined" && jj$binding == "stratum")
check("joint: undetermined side, either argument order; the smaller certified value bounds the joint", joint_breakdown(und(0.2), ja)$bracket_lo == 0.2 && joint_breakdown(und(0.6), ncx)$bracket_lo == 0.6 && joint_breakdown(und(0.6), ncx)$qualifier == "undetermined" && joint_breakdown(und(0.2), ncx)$bracket_lo == 0.2)
check("joint: undetermined beside a bracket that lies wholly at or below m -> that bracket (the undetermined side certainly clears through m)", joint_breakdown(und(0.6), jb)$status == "breakdown bracketed" && joint_breakdown(und(0.6), jb)$bracket_lo == 0.20 && is.na(joint_breakdown(und(0.6), jb)$qualifier))
check("joint: both undetermined -> min m, both binding; undetermined beside no-crossing is undetermined (not 'no crossing'); no initial clearance still wins", joint_breakdown(und(0.4), und(0.2))$bracket_lo == 0.2 && joint_breakdown(und(0.4), und(0.2))$binding == "uid+stratum" && startsWith(joint_breakdown(und(0.4), ncx)$status, "undetermined") && joint_breakdown(und(0.4), nic)$status == "no initial clearance")
jf <- c(ja, list(bracket_flag = "failure in bracket"))
check("joint: 'failure in bracket' of either side travels as the qualifier", joint_breakdown(jf, jb)$qualifier == "failure in bracket" && joint_breakdown(jb, jf)$qualifier == "failure in bracket" && is.na(joint_breakdown(ja, jb)$qualifier))
check("joint: not-implemented / failed statuses are not resolved in favour of either side", grepl("^not determined", joint_breakdown(ja, list(status = "initial point failed (not treated as clear or lost)"))$status) && joint_breakdown(list(status = "x"), list(status = "x"))$status == "x")

# valve chain with a scripted probe: ordering, re-check after each step, per-calendar, T = secs * 60 * 1.5, re-probe under the new setting
mkprobe <- function(f, st = function(cal, method, gp, k, center) "ok") { calls <- list(); list(fun = function(cal, method, gp, k, center) { calls[[length(calls) + 1L]] <<- list(cal = cal, method = method, gp = gp, k = k, center = center); list(secs = f(cal, method, gp, k), status = st(cal, method, gp, k, center)) }, calls = function() calls) }
acts <- function(v) unique(v$log[, .(iter, action)])$action
p <- mkprobe(function(cal, method, gp, k) if (cal == "weekly") 10 else if (method == "C-LF") 400 else 100)   # daily never gets fast enough (T = 9000 > 7200 even at the floor)
v <- valve_chain(c("daily", "weekly"), p$fun, 7200, 28800, 4L)
check("valve: ordered chain planned -> (i) -> (ii) -> (iii) -> (iv), nothing skipped or reordered", length(acts(v)) == 5L && acts(v)[1] == "planned" && grepl("^\\(i\\)", acts(v)[2]) && grepl("\\(ii\\)", acts(v)[3]) && grepl("\\(iii\\)", acts(v)[4]) && grepl("\\(iv\\)", acts(v)[5]), paste(acts(v), collapse = " | "))
check("valve: the trigger is re-checked after every step (one state row per calendar per iteration, showing the state after the step)", all(v$log[, .N, by = iter]$N == 2L) && v$log[iter == 2 & calendar == "daily", parallel] && !v$log[iter == 1 & calendar == "daily", parallel] && v$log[iter == 3 & calendar == "daily", method] == "Conditional" && v$log[iter == 4 & calendar == "daily", grid_points] == 200L && !v$log[iter == 5 & calendar == "daily", rm_computed])
dp <- v$plan[calendar == "daily"]; wp <- v$plan[calendar == "weekly"]
check("valve: (iv) drops daily RM, weekly RM stays on the planned method and grid (per calendar, not global)", !dp$rm_computed && wp$rm_computed && wp$rm_method == "C-LF" && wp$rm_grid_points == 1000L && wp$rm_grid_k == 20 && dp$rm_grid_points == 200L && dp$rm_grid_k == 8 && dp$rm_method == "Conditional")
check("valve: T = probe seconds x 60 x 1.5", all(abs(v$log[iter == 1, projected_T_secs] - v$log[iter == 1, probe_secs] * 90) < 1e-9) && v$log[iter == 1 & calendar == "daily", projected_T_secs] == 36000)
dcalls <- Filter(function(z) z$cal == "daily", p$calls())
check("valve: daily probe sequence = C-LF@1000, Conditional@1000 (after ii), Conditional@200 with k = 8 (after iii); none after (i) or (iv)", length(dcalls) == 3L && dcalls[[1]]$method == "C-LF" && dcalls[[1]]$gp == 1000L && dcalls[[2]]$method == "Conditional" && dcalls[[2]]$gp == 1000L && dcalls[[3]]$gp == 200L && dcalls[[3]]$k == 8 && dcalls[[1]]$center == "zero" && dcalls[[2]]$center == "zero" && dcalls[[3]]$center == "estimate")
check("valve (T-F2): the (iii) setting is centred on the estimate in the plan and the log; the planned and (ii) settings stay centred at 0", dp$rm_grid_center == "estimate" && wp$rm_grid_center == "zero" && v$log[iter == 3 & calendar == "daily", grid_center] == "zero" && v$log[iter == 4 & calendar == "daily", grid_center] == "estimate")
check("valve: fallback label lists the steps in order", grepl("^\\(i\\) parallel.*\\(ii\\) method Conditional.*\\(iii\\) gridPoints 200.*\\(iv\\) daily RM not computed", dp$fallback) && wp$fallback == "(i) parallel: one process per cell")
p <- mkprobe(function(cal, method, gp, k) if (cal == "weekly") 10 else if (method == "C-LF") 400 else 40)   # (ii) is enough: T = 3600
v <- valve_chain(c("daily", "weekly"), p$fun, 7200, 28800, 4L)
check("valve: stops as soon as the rule passes ((ii) enough -> no (iii), no (iv))", length(acts(v)) == 3L && v$plan[calendar == "daily", rm_grid_points] == 1000L && v$plan[calendar == "daily", rm_computed] && !v$final_trigger)
p <- mkprobe(function(cal, method, gp, k) 100)   # each cell T = 9000 (< cell limit 20000); sum = 8 x 9000 = 72000 > 28800 sequentially, 18000 with 4 procs
v <- valve_chain(c("daily", "weekly"), p$fun, 20000, 28800, 4L)
check("valve: a sum-over-cells trigger is cured by (i) parallel alone", length(acts(v)) == 2L && v$log[iter == 1, trigger][1] && !v$final_trigger && all(v$plan$rm_method == "C-LF"))
p <- mkprobe(function(cal, method, gp, k) 1); v <- valve_chain(c("daily", "weekly"), p$fun, 7200, 28800, 4L)
check("valve: no trigger -> no step, fallback 'none'", length(acts(v)) == 1L && all(v$plan$fallback == "none") && !any(v$plan$parallel))
p <- mkprobe(function(cal, method, gp, k) if (cal == "daily") 1 else 400); v <- valve_chain(c("daily", "weekly"), p$fun, 7200, 28800, 4L)
check("valve: a weekly calendar still over the limit after (iii) keeps RM (only daily RM is ever dropped), reported as exhausted", v$plan[calendar == "weekly", rm_computed] && grepl("exhausted", v$plan[calendar == "weekly", fallback]) && v$plan[calendar == "weekly", exhausted] && v$final_trigger)
# A2 item 6: an exhausted weekly calendar's time is excluded from the sum, so it cannot push daily RM down the chain
p <- mkprobe(function(cal, method, gp, k) if (cal == "daily") 20 else 400); v <- valve_chain(c("daily", "weekly"), p$fun, 7200, 28800, 4L)
check("valve (T-F6): weekly exhausted does not push daily down the chain (daily T = 1800 per cell; weekly would put the sum far over 8 h)", v$plan[calendar == "daily", rm_method] == "C-LF" && v$plan[calendar == "daily", rm_computed] && v$plan[calendar == "daily", rm_grid_points] == 1000L && v$plan[calendar == "weekly", exhausted] &&
      !any(grepl("^daily", unlist(strsplit(acts(v), "; ")))) && v$plan[calendar == "daily", fallback] == "(i) parallel: one process per cell" && sum(grepl("^daily|; daily", acts(v))) == 0L, paste(acts(v), collapse = " | "))
# A2 item 4: a failed / timed-out / edge probe trips the valve for its calendar and its seconds feed no T
for (bad_class in c("failed", "timeout", "edge")) {
  p <- mkprobe(function(cal, method, gp, k) 1, function(cal, method, gp, k, center) if (cal == "weekly" && method == "C-LF") bad_class else "ok"); v <- valve_chain(c("daily", "weekly"), p$fun, 7200, 28800, 4L)
  check(sprintf("valve (T-F4): a %s probe trips the valve for that calendar only, T is NA (the 1 s is not used), the chain re-probes under the next setting", bad_class),
        v$log[iter == 1 & calendar == "weekly", probe_trip] && is.na(v$log[iter == 1 & calendar == "weekly", projected_T_secs]) && !v$log[iter == 1 & calendar == "daily", probe_trip] && v$log[iter == 1 & calendar == "daily", projected_T_secs] == 90 && v$log[iter == 1, trigger][1] &&
          v$plan[calendar == "weekly", rm_method] == "Conditional" && v$plan[calendar == "weekly", rm_grid_points] == 1000L && v$plan[calendar == "daily", rm_method] == "C-LF" && !v$final_trigger && v$plan[calendar == "weekly", projected_T_secs] == 90)
}
p <- mkprobe(function(cal, method, gp, k) 1, function(cal, method, gp, k, center) if (cal == "weekly") (if (center == "estimate") "edge" else "failed") else "ok"); v <- valve_chain(c("daily", "weekly"), p$fun, 7200, 28800, 4L)
check("valve: an edge probe under (iii) (grid centred on the estimate) keeps tripping -> weekly exhausted with RM kept and T NA", v$plan[calendar == "weekly", exhausted] && v$plan[calendar == "weekly", rm_computed] && is.na(v$plan[calendar == "weekly", projected_T_secs]) && v$plan[calendar == "weekly", probe_status] == "edge" && v$final_trigger && v$plan[calendar == "daily", fallback] == "(i) parallel: one process per cell")
p <- mkprobe(function(cal, method, gp, k) 1, function(cal, method, gp, k, center) if (cal == "daily") "failed" else "ok"); v <- valve_chain(c("daily", "weekly"), p$fun, 7200, 28800, 4L)
check("valve: a daily probe that fails at every setting ends in (iv) daily RM not computed", !v$plan[calendar == "daily", rm_computed] && v$plan[calendar == "weekly", rm_computed])
# A2 item 5: T = secs x planned evaluations (per cell) x 1.5 + SD time; the per-cell maximum drives the cell trigger
p <- mkprobe(function(cal, method, gp, k) 10); v <- valve_chain(c("daily", "weekly"), p$fun, 7200, 28800, 4L, cell_evals = list(daily = c(80, 100, 80, 100), weekly = c(80, 100, 80, 100)), cell_sd = list(daily = c(500, 500, 500, 500), weekly = rep(0, 4)))
check("valve (T): T = probe secs x planned evaluations x 1.5 + SD time (daily 10 x 100 x 1.5 + 500 = 2000; weekly 1500), per-cell maximum", v$log[iter == 1 & calendar == "daily", projected_T_secs] == 2000 && v$log[iter == 1 & calendar == "weekly", projected_T_secs] == 1500)
p <- mkprobe(function(cal, method, gp, k) 10); v <- valve_chain(c("daily"), p$fun, 1900, 28800, 4L, cell_evals = list(daily = c(80, 100, 80, 100)), cell_sd = list(daily = rep(500, 4)))
check("valve (T): the SD time counts toward the cell trigger (2000 > 1900 trips, planned 1500 alone would not)", v$log[iter == 1, cell_trigger] && v$log[iter == 1, projected_T_secs] == 2000)
# A2 item 5: sum trigger -> calendars with cells over the cell limit, else the largest contributor
p <- mkprobe(function(cal, method, gp, k) if (cal == "daily") if (method == "C-LF") 60 else 5 else 50)
v <- valve_chain(c("daily", "weekly"), p$fun, 7200, 5000, 4L)
check("valve (sum trigger): no cell over the limit, sum over -> chain goes to the largest contributor (daily 21600 vs weekly 18000), not to every calendar", grepl("^\\(i\\)", acts(v)[2]) && grepl("^daily \\(ii\\)", acts(v)[3]) && v$plan[calendar == "weekly", rm_method] == "C-LF", paste(acts(v), collapse = " | "))

# out guard: whitelist out/exploratory/<dir> only (function extracted from the committed script, evaluated on paths; no file system access)
local({
  e <- new.env(); e$main_root <- root; e$SYNTHETIC <- FALSE
  for (x in parse(text = readLines(script), keep.source = FALSE)) if (is.call(x) && is.symbol(x[[1]]) && as.character(x[[1]]) %in% c("<-", "=") && as.character(x[[2]])[1] %in% c("lex_clean", "out_guard")) eval(x, e)
  ok <- function(p) !inherits(try(e$out_guard(e$lex_clean(p)), silent = TRUE), "try-error")
  check("out guard accepts out/exploratory/<dir> (and below)", ok(file.path(root, "out", "exploratory", "o2-x")) && ok(file.path(root, "out", "exploratory", "o2-x", "cells", "daily_uid_D")))
  check("out guard rejects everything else inside the repo: bare out/exploratory, out/recovery, out/g4, R/, exploratory/, the root", !ok(file.path(root, "out", "exploratory")) && !ok(file.path(root, "out", "recovery", "x")) && !ok(file.path(root, "out", "g4", "x")) && !ok(file.path(root, "out", "x")) &&
        !ok(file.path(root, "R", "x")) && !ok(file.path(root, "exploratory", "x")) && !ok(root) && !ok(file.path(root, "outx", "exploratory", "y")))
  check("out guard rejects traversal out of out/exploratory/<dir>", !ok(file.path(root, "out", "exploratory", "..", "recovery", "x")) && !ok(file.path(root, "out", "exploratory", "x", "..", "..", "g4", "y")) && !ok(file.path(root, "out", "exploratory", "..")) && !ok(file.path(root, "out", "exploratory", "x", "..", "..", "..", "R", "z")))
  check("out guard: outside the repo only for synthetic runs", !ok(file.path(tempdir(), "x")) && {e$SYNTHETIC <- TRUE; ok(file.path(tempdir(), "x"))} && !ok(file.path(root, "out", "recovery", "x")))
})

# ---- hd_eval / rm_setup extracted from the committed script, with a stubbed HonestDiD call (no solver): warnings recorded not muffled, edge hits, failures, grid centring (A2 items 1, 2, 7) ----------------
local({
  e <- new.env(); e$CFG <- list(point_seconds = Inf, seed = 0, alpha = 0.05, rm_bound = "deviation from parallel trends", sd_method = "FLCI"); e$`%||%` <- `%||%`
  for (x in parse(text = readLines(script), keep.source = FALSE)) if (is.call(x) && is.symbol(x[[1]]) && as.character(x[[1]]) %in% c("<-", "=") && as.character(x[[2]])[1] %in% c("HD_RM", "HD_SD", "hd_eval", "rm_setup")) eval(x, e)
  cl <- list(b = c(0, 0), S = diag(2), npre = 1L, npost = 1L, l_vec = 1, target = -0.1, sdT = 0.02)
  rmset <- list(method = "C-LF", gp = 11L, glb = -1, gub = 1)
  stub <- function(lb, ub, warns = character()) function(args) { for (w in warns) warning(w, call. = FALSE); data.frame(lb = lb, ub = ub) }
  seen <- character()
  ev <- function(f, rm = rmset) { e$HD_RM <- f; seen <<- character(); withCallingHandlers(e$hd_eval("RM", cl, 0, rm), warning = function(w) { seen <<- c(seen, conditionMessage(w)); invokeRestart("muffleWarning") }) }
  r <- ev(stub(-0.3, 0.4, c("LP for eta did not converge", "something else")))
  check("hd_eval (T-F5): package warnings are recorded per evaluation AND still propagate (not muffled); 'LP for eta did not converge' sets eval_flag; the interval is unaltered", r$status == "ok" && r$lo == -0.3 && r$hi == 0.4 &&
        grepl("LP for eta did not converge", r$warnings) && grepl("something else", r$warnings) && identical(r$eval_flag, "LP for eta did not converge") && setequal(seen, c("LP for eta did not converge", "something else")), paste(r$warnings, "|", paste(seen, collapse = ";")))
  r0 <- ev(stub(-0.3, 0.4))
  check("hd_eval: no warnings -> empty record, no eval_flag", r0$warnings == "" && is.na(r0$eval_flag) && r0$status == "ok")
  re <- ev(stub(-0.3, 1))
  check("hd_eval (A2 item 2): an upper-grid-edge hit is status 'edge: ...' (not ok), the interval is void (clears NA) and the probe class is 'edge'", startsWith(re$status, "edge:") && grepl("hi_at_grid_edge", re$status) && is.na(clears_hi(re$status, re$hi, 0, re$dtheta)) && probe_class(re$status) == "edge")
  rl <- ev(stub(-1, 0.4)); ro <- ev(stub(-0.3, 0.4, "CI is open at one of the endpoints"))
  check("hd_eval: lower-edge hit and the open-endpoint warning are edge too; each voids only its own interval (a neighbouring ok interval still clears)", startsWith(rl$status, "edge:") && startsWith(ro$status, "edge:") && grepl("open_endpoint", ro$status) && is.na(clears_hi(ro$status, ro$hi, 1, 0)) && isTRUE(clears_hi(r0$status, r0$hi, 1, 0)))
  rf <- ev(stub(NA_real_, NA_real_))
  check("hd_eval: an empty / non-finite interval is 'failed', probe class 'failed' (trips the valve)", startsWith(rf$status, "failed") && probe_class(rf$status) == "failed" && probe_class("timeout") == "timeout" && probe_class("ok") == "ok")
  e$HD_RM <- function(args) stop("solver exploded"); rr <- e$hd_eval("RM", cl, 0, rmset)
  check("hd_eval: an error is 'failed: <msg>'", startsWith(rr$status, "failed: solver exploded"))
  s0 <- e$rm_setup(cl, "C-LF", 1000L, 20); s3 <- e$rm_setup(cl, "Conditional", 200L, 8, "estimate")
  check("rm_setup (T-F2): planned grid = +-20 sd around 0; fallback (iii) grid = [l'beta_post - 8 sd, l'beta_post + 8 sd] with >= 200 points", isTRUE(all.equal(c(s0$glb, s0$gub), c(-0.4, 0.4))) && isTRUE(all.equal(c(s3$glb, s3$gub), c(-0.1 - 0.16, -0.1 + 0.16))) && s3$gp >= 200L)
  s4 <- e$rm_setup(cl, "Conditional", 200L, 8, "estimate", -1, 1)
  check("rm_setup: explicit synthetic grid bounds override the centring", s4$glb == -1 && s4$gub == 1)
})

# ---- isolated library: no signed package is shadowed, the signed gate's package check still passes ------------------------------------------------------------------
if (!dir.exists(file.path(root, "exploratory", "rlib"))) cat("skip  isolated-library checks (exploratory/rlib not present in this tree; see PROVENANCE.md)\n") else local({
  ex <- parse(text = readLines(file.path(root, "R", "post_gate.R")), keep.source = FALSE); e <- new.env()
  for (x in ex) if (is.call(x) && as.character(x[[1]]) %in% c("<-", "=") && as.character(x[[2]])[1] %in% c("POST_PINNED_PACKAGES", "pg_fail", "verify_post_package_versions")) eval(x, e)   # committed code, unmodified
  rl <- file.path(root, "exploratory", "rlib")
  check("rlib contains none of the signed POST_PINNED_PACKAGES", length(intersect(e$POST_PINNED_PACKAGES, list.files(rl))) == 0L)
  man <- list(r_package_versions = setNames(lapply(e$POST_PINNED_PACKAGES, function(p) as.character(utils::packageVersion(p))), e$POST_PINNED_PACKAGES), r_version = R.version.string)
  check("verify_post_package_versions passes with rlib on the path", isTRUE(e$verify_post_package_versions(man)))
  code <- sprintf("cat(paste(vapply(c(%s), function(p) as.character(packageVersion(p)), ''), collapse=' '))", paste(sprintf("'%s'", e$POST_PINNED_PACKAGES), collapse = ","))
  out <- system2("Rscript", c("-e", shQuote(code)), stdout = TRUE, env = paste0("R_LIBS_USER=", Sys.getenv("R_LIBS_USER")))
  mine <- paste(vapply(e$POST_PINNED_PACKAGES, function(p) as.character(utils::packageVersion(p)), ""), collapse = " ")
  check("pinned package versions identical with and without rlib", identical(out, mine), paste(out, "|", mine))
})

# ---- synthetic O3-layout inputs ---------------------------------------------------------------------------------------------------------------------------
ALL_DAYS <- c(0:59, 61:95)
write_input <- function(dir, outcome, cluster, beta, sd, ref_shift = 0, sd_post = sd) {   # sd: per-day sd of pre days (and the reference row stays 0); sd_post for days 61..95
  dir.create(dir, showWarnings = FALSE, recursive = TRUE)
  b <- setNames(beta, ALL_DAYS); b[["59"]] <- ref_shift
  V <- diag(ifelse(ALL_DAYS > 59, sd_post, sd)^2, length(ALL_DAYS)); V[ALL_DAYS == 59, ] <- 0; V[, ALL_DAYS == 59] <- 0
  saveRDS(list(banner = "synthetic", outcome = outcome, cluster = cluster, window = "all", day_index = ALL_DAYS, date = NA, coef = b, vcov = V, n_clusters = 700L, df = 699L), file.path(dir, sprintf("o3_vcov_%s_%s_all.rds", outcome, cluster)))
}
lin_beta <- function(s, tau) c(s * (0:58 - 59), 0, tau + s * (1:35))      # exact linear pre-trend through the reference; post_t = tau + s*t, t = 1..35
S_ <- 0.005; TAU <- -0.2; SD_ <- 1e-5
din <- file.path(tmp, "in_lin"); write_input(din, "D", "uid", lin_beta(S_, TAU), SD_)

# ---- daily smoothness (analytic: ID set tau +- 222 M, 222 = mean_t t(t+1)/2 over t = 1..35) ------------------------------------------------------------------
res <- run("--synthetic", "--cell", "daily_uid_D", "--in", shQuote(din), "--out", q("o_sd"), "--pre-keep", "6", "--restrictions", "SD", "--sd-grid", "0,0.0005,0.001", "--bisections", "4",
           "--thresholds", shQuote("D=0,-0.10;A=0"))
check("daily SD cell runs", res$status == 0L, paste(tail(res$out, 5), collapse = "\n"))
if (res$status == 0L) {
  brk <- rd("o_sd", "o2_breakdown.csv"); ev <- rd("o_sd", "o2_evals.csv")
  check("banner is SYNTHETIC in every csv", all(grepl("^SYNTHETIC", c(brk$banner, ev$banner))))
  check("MANIFEST, sessionInfo, timing written", all(file.exists(file.path(tmp, "o_sd", c("MANIFEST.sha256", "sessionInfo.txt", "o2_timing.csv", "o2_original.csv", "o2_input_hashes.csv")))))
  for (th in c(0, -0.10)) {
    b <- brk[restriction == "SD" & threshold == th]; ms <- (th - TAU) / 222
    check(sprintf("SD breakdown for tau = %g brackets the analytic M* = %.8f (one flag: SD/FLCI has no theta grid)", th, ms), nrow(b) == 1L && b$flag == "single" && b$headline && b$status == "breakdown bracketed" && b$bracket_lo - 2e-6 <= ms && ms <= b$bracket_hi + 2e-6 && b$n_bisections_done == 4L, paste(capture.output(print(b)), collapse = " "))
  }
  check("bisection rows carry their threshold; grid rows reported", all(!is.na(ev[kind == "bisect", threshold])) && ev[kind == "grid", .N] == 3L && all(c("clears_0_nominal", "clears_m0.1_nominal") %in% names(ev)))
  check("per-cell timing logged", rd("o_sd", "o2_timing.csv")$cell_seconds > 0 && all(ev$secs >= 0))
  check("original interval table has normal and t intervals", with(rd("o_sd", "o2_original.csv"), t_df == 699 && t_hi > normal_hi))
}
# ---- weekly relative magnitudes (analytic: raw weekly post mean + Mbar*s*weighted mean of bin index) --------------------------------------------------------------
wk_from <- c(0, 11, 18, 25, 32, 39, 46, 53, 61, 67, 74, 81, 88); wk_to <- c(10, 17, 24, 31, 38, 45, 52, 59, 66, 73, 80, 87, 95)
w <- c(6, 7, 7, 7, 8) / 35
wk_beta <- function(SW, TW) { bin_val <- c(SW * (1:7 - 8), 0, TW + SW * (1:5)); v <- numeric(length(ALL_DAYS)); for (k in 1:13) v[ALL_DAYS >= wk_from[k] & ALL_DAYS <= wk_to[k]] <- bin_val[k]; v }
SW <- 0.03; TW <- -0.2
SDP <- 0.03   # weekly: exact pre bins (so the RM scale is analytic) but post noise wide enough for the 300-point RM test grid to resolve (spacing 0.0027)
dw <- file.path(tmp, "in_wk"); write_input(dw, "D", "uid", wk_beta(SW, TW), SD_, sd_post = SDP)
res <- run("--synthetic", "--cell", "weekly_uid_D", "--in", shQuote(dw), "--out", q("o_wk"), "--rm-grid", "0,1,1.5", "--bisections", "3", "--rm-grid-points", "300", "--rm-grid-lb", "-0.4", "--rm-grid-ub", "0.4",
           "--thresholds", shQuote("D=0;A=0"))
check("weekly cell runs", res$status == 0L, paste(tail(res$out, 5), collapse = "\n"))
if (res$status == 0L) {
  brk <- rd("o_wk", "o2_breakdown.csv"); orig <- rd("o_wk", "o2_original.csv"); ev <- rd("o_wk", "o2_evals.csv")
  check("weekly: 7 pre + 5 post bins, day-count weights (6,7,7,7,8)/35", orig$n_pre == 7L && orig$n_post == 5L && all(abs(as.numeric(strsplit(orig$target_weights, " ")[[1]]) - w) < 1e-9))
  check("weekly: target equals the analytic day-count-weighted mean, recentred once", abs(orig$target - (TW + SW * sum(w * 1:5))) < 1e-9)
  check("weekly SD reported as not implemented, no number", all(brk[restriction == "SD", status] == "not implemented (no calendar-aware mapping)") && all(is.na(brk[restriction == "SD", bracket_lo])))
  ms <- (0 - TW - 1.96 * orig$se) / (SW * sum(w * 1:5)) - 1; b <- brk[restriction == "RM" & threshold == 0 & flag == "nominal"]; bc <- brk[restriction == "RM" & threshold == 0 & flag == "conservative"]   # post noise shifts the upper bound by about 1.96 se
  check(sprintf("weekly RM nominal breakdown brackets the analytic Mbar* = %.4f (tolerance = RM test-grid spacing)", ms), nrow(b) == 1L && b$status == "breakdown bracketed" && b$bracket_lo - 0.1 <= ms && ms <= b$bracket_hi + 0.1, paste(capture.output(print(b)), collapse = " "))
  check("weekly RM: conservative flag reported beside the nominal one, breakdown at or below it, headline = conservative", nrow(bc) == 1L && bc$headline && !b$headline && (grepl("^no initial clearance", bc$status) || bc$bracket_lo <= b$bracket_lo))
  check("RM grid points listed with their theta step; no per-cell probe row (the valve times once per calendar)", ev[kind == "probe", .N] == 0L && ev[kind == "grid" & restriction == "RM", .N] == 3L && all(abs(ev[kind == "grid" & restriction == "RM", dtheta] - 0.8 / 299) < 1e-12))
}
# ---- no initial clearance / no crossing (weekly RM, small grids) ------------------------------------------------------------------------------------------------------------
dpos <- file.path(tmp, "in_pos"); write_input(dpos, "D", "stratum", wk_beta(SW, 0.2), SD_, sd_post = SDP)
res <- run("--synthetic", "--cell", "weekly_stratum_D", "--in", shQuote(dpos), "--out", q("o_pos"), "--rm-grid", "0,0.5", "--rm-grid-points", "200", "--rm-grid-lb", "-0.6", "--rm-grid-ub", "0.8", "--thresholds", shQuote("D=0;A=0"))
bp <- if (res$status == 0L) rd("o_pos", "o2_breakdown.csv") else NULL
check("no initial clearance reported, no bisection", !is.null(bp) && all(bp[restriction == "RM", status] == "no initial clearance") && all(bp[restriction == "RM", headline_status] == "no initial clearance") && !any(rd("o_pos", "o2_evals.csv")$kind == "bisect"))
dneg <- file.path(tmp, "in_neg"); write_input(dneg, "D", "uid", wk_beta(0.001, -0.5), SD_, sd_post = SDP)
res <- run("--synthetic", "--cell", "weekly_uid_D", "--in", shQuote(dneg), "--out", q("o_neg"), "--rm-grid", "0,0.5", "--rm-grid-points", "200", "--rm-grid-lb", "-1", "--rm-grid-ub", "0.8", "--thresholds", shQuote("D=0;A=0"))
bn <- if (res$status == 0L) rd("o_neg", "o2_breakdown.csv") else NULL
check("no crossing within the grid, with the largest grid value", !is.null(bn) && all(bn[restriction == "RM", status] == "no crossing within the grid") && all(bn[restriction == "RM", largest_grid_M] == 0.5), paste(tail(res$out, 3), collapse = " "))
# ---- input already recentred ------------------------------------------------------------------------------------------------------------------------------------------------
dbad <- file.path(tmp, "in_bad"); write_input(dbad, "D", "uid", lin_beta(S_, TAU), SD_, ref_shift = 0.01)
res <- run("--synthetic", "--cell", "daily_uid_D", "--in", shQuote(dbad), "--out", q("o_bad"))
check("input with a nonzero reference coefficient is refused, not re-recentred", res$status != 0L && any(grepl("not recentred", res$out)))
guard_parent <- file.path(root, "out", "exploratory", sprintf("o2test-%d", Sys.getpid()))   # inside the repo's own whitelisted tree (gitignored); removed at the end
fo <- function(d) shQuote(file.path(guard_parent, d))
res <- run("--cell", "daily_uid_D", "--in", shQuote(din), "--out", fo("o_rr"), "--repo-root", shQuote(tmp))
check("--repo-root is rejected without --synthetic (verifier F2)", res$status != 0L && any(grepl("repo-root", res$out)) && !dir.exists(file.path(guard_parent, "o_rr")))
res <- run("--cell", "daily_uid_D", "--in", shQuote(din), "--out", fo("o_real"))
check("real (non-synthetic) inputs need --signed-time-path", res$status != 0L && any(grepl("signed-time-path", res$out)))
stp <- file.path(tmp, "signed.csv"); fwrite(data.table(outcome = "D", day_index = ALL_DAYS, coef = lin_beta(S_, TAU)), stp)
res <- run("--cell", "daily_uid_D", "--in", shQuote(din), "--out", fo("o_real3"), "--signed-time-path", shQuote(stp))
check("real inputs need --valve-plan (the valve outcome is shared by uid and stratum)", res$status != 0L && any(grepl("valve-plan", res$out)))
res <- run("--cell", "daily_uid_D", "--in", shQuote(din), "--out", q("o_real4"))
check("a non-synthetic run cannot write outside the repo's out/exploratory/<dir>", res$status != 0L && any(grepl("refusing to write", res$out)) && !dir.exists(file.path(tmp, "o_real4")))
res <- run("--cell", "daily_uid_D", "--in", shQuote(din), "--out", fo("o_real2"), "--pre-keep", "6")
check("--pre-keep is refused without --synthetic", res$status != 0L && any(grepl("pre-keep", res$out)))
# ---- valve end to end (real probes on synthetic weekly cells), shared plan, fallbacks (ii)/(iii)/(iv), default grid, timeout label -----------------------------------------------------
dpl <- file.path(tmp, "in_plan"); write_input(dpl, "D", "uid", wk_beta(0.001, -0.02), SD_, sd_post = SDP); write_input(dpl, "D", "stratum", wk_beta(0.001, -0.02), SD_, sd_post = SDP)
res <- run("--synthetic", "--valve", "--calendars", "weekly", "--in", shQuote(dpl), "--out", q("valve"), "--rm-grid-points", "80", "--valve-cell-seconds", "0.5", "--valve-gp-floor", "40")
check("valve mode runs", res$status == 0L, paste(tail(res$out, 4), collapse = "\n"))
if (res$status == 0L) {
  vplan <- rd("valve", "o2_valve_plan.csv"); vlog <- rd("valve", "o2_valve_log.csv"); vpr <- rd("valve", "o2_valve_probes.csv")
  check("valve (real probes): chain ran (i) -> (ii) -> (iii) in order, weekly RM kept (exhausted), method Conditional, gridPoints = floor, grid +-8 sd", identical(sub(" .*", "", unique(vlog[, .(iter, action)])$action)[1:4], c("planned", "(i)", "weekly", "weekly")) &&
        grepl("\\(ii\\) method Conditional", vplan$fallback) && grepl("\\(iii\\) gridPoints 40", vplan$fallback) && vplan$rm_method == "Conditional" && vplan$rm_grid_points == 40L && vplan$rm_grid_k == 8 && vplan$rm_computed && grepl("exhausted", vplan$fallback), paste(vplan$fallback))
  check("valve: three probes (planned, after ii, after iii); probes file carries seconds and a status class only, never an interval", nrow(vpr) == 3L && identical(setdiff(names(vpr), "banner"), c("calendar", "restriction", "probed_cell", "method", "grid_points", "grid_k", "grid_center", "secs", "status_class")) && all(vpr$status_class == "ok") && all(vpr$probed_cell == "weekly_uid_D") && identical(vpr$grid_center, c("zero", "zero", "estimate")) &&
        !any(c("lo", "hi") %in% unlist(lapply(c("o2_valve_plan.csv", "o2_valve_log.csv", "o2_valve_probes.csv"), function(f) names(rd("valve", f))))))
  check("valve: projected T = probe seconds x planned evaluations (40 grid + 3 thresholds x 2 flags x 10 bisections = 100 for the A cells) x 1.5 in the plan", abs(vplan$projected_T_secs - vplan$probe_secs * 150) < 1e-9 && vplan$rm_grid_center == "estimate" && file.exists(file.path(tmp, "valve", "MANIFEST.sha256")))
  dir.create(file.path(tmp, "cells_rm"))
  okc <- TRUE
  for (cl_ in c("uid", "stratum")) { r_ <- run("--synthetic", "--cell", sprintf("weekly_%s_D", cl_), "--in", shQuote(dpl), "--out", q("cells_rm", sprintf("weekly_%s_D", cl_)), "--valve-plan", q("valve", "o2_valve_plan.csv"), "--restrictions", "RM", "--rm-grid", "0,0.5,1", "--bisections", "3", "--thresholds", shQuote("D=0,-0.006;A=0")); okc <- okc && r_$status == 0L }
  check("weekly uid and stratum cells run under the shared valve plan", okc)
  if (okc) {
    for (cl_ in c("uid", "stratum")) {
      cd <- sprintf("cells_rm/weekly_%s_D", cl_); ev <- rd(cd, "o2_evals.csv"); og <- rd(cd, "o2_original.csv"); tm <- rd(cd, "o2_timing.csv"); br <- rd(cd, "o2_breakdown.csv")
      rmv <- ev[restriction == "RM"]; okr <- rmv[startsWith(status, "ok")]
      check(sprintf("[%s] evals csv carries the per-evaluation warnings, eval_flag and the 'valve exhausted' label (weekly was exhausted in this run)", cl_), all(c("warnings", "eval_flag", "valve_label") %in% names(ev)) && all(rmv$valve_label == "valve exhausted") && all(c("bracket_flag", "valve_label") %in% names(br)) && all(br[restriction == "RM", valve_label] == "valve exhausted"))
      check(sprintf("[%s] fallback (ii)+(iii) in force: method Conditional, gridPoints 40, theta grid = l'beta_post +-8 sd (centred on the estimate, T-F2), step printed beside every interval", cl_), all(rmv$method == "Conditional") && all(rmv$grid_points == 40L) && all(abs(rmv$grid_lb - (og$target - 8 * og$se)) < 1e-12) && all(abs(rmv$grid_ub - (og$target + 8 * og$se)) < 1e-12) &&
            all(abs(rmv$dtheta - 16 * og$se / 39) < 1e-12) && nrow(okr) > 0L)
      check(sprintf("[%s] conservative flag = (upper + theta step < tau), implies nominal; both in the long csv", cl_), all(c("clears_0_nominal", "clears_0_conservative", "clears_m0.006_nominal", "clears_m0.006_conservative") %in% names(ev)) &&
            all((okr$hi + okr$dtheta < 0) == okr$clears_0_conservative) && !any(ev$clears_0_conservative %in% TRUE & ev$clears_0_nominal %in% FALSE, na.rm = TRUE) && !any(ev$clears_m0.006_conservative %in% TRUE & ev$clears_m0.006_nominal %in% FALSE, na.rm = TRUE))
      b6 <- br[threshold == -0.006]; h0 <- rmv[kind == "grid" & M == 0]; nom0 <- h0$hi < -0.006; con0 <- h0$hi + h0$dtheta < -0.006
      check(sprintf("[%s] both brackets reported (nominal + conservative rows), headline = conservative, status follows the flags at M = 0", cl_), b6[, .N] == 2L && setequal(b6$flag, c("nominal", "conservative")) && b6[flag == "conservative", headline] && !b6[flag == "nominal", headline] &&
            (con0 || b6[flag == "conservative", status] == (if (nom0) "no initial clearance (conservative); nominal clears at 0" else "no initial clearance")) && (con0 || b6[flag == "conservative", headline_status] == "no initial clearance"))
      check(sprintf("[%s] timing records the plan (fallback, probe seconds) and the setting actually used", cl_), tm$fallback == vplan$fallback && tm$rm_grid_points == 40L && tm$rm_method == "Conditional" && tm$rm_grid_k == 8 && tm$rm_grid_center == "estimate" && tm$valve_exhausted && abs(tm$valve_probe_secs - vplan$probe_secs) < 1e-9 && rd(cd, "o2_input_hashes.csv")$valve_plan_sha256 != "")
    }
    tu <- rd("cells_rm/weekly_uid_D", "o2_timing.csv"); ts <- rd("cells_rm/weekly_stratum_D", "o2_timing.csv")
    check("uid and stratum cells of the calendar ran with the same method and grid", identical(tu[, .(rm_method, rm_grid_points, rm_grid_k, rm_computed)], ts[, .(rm_method, rm_grid_points, rm_grid_k, rm_computed)]))
    res <- run("--synthetic", "--collect", "--cells-dir", q("cells_rm"), "--out", q("col_rm"), "--allow-missing", "--valve-dir", q("valve"))
    check("collect merges the two weekly RM cells (+ valve files)", res$status == 0L && all(file.exists(file.path(tmp, "col_rm", c("o2_joint.csv", "o2_valve_plan.csv", "o2_valve_log.csv", "o2_valve_probes.csv")))), paste(tail(res$out, 3), collapse = "\n"))
    if (res$status == 0L) {
      ce <- rd("col_rm", "o2_evals.csv"); cb <- rd("col_rm", "o2_breakdown.csv"); cj <- rd("col_rm", "o2_joint.csv")
      jcols <- grep("^joint_clears_", names(ce), value = TRUE)
      check("long csv has a joint clears column per threshold and flag", setequal(jcols, c("joint_clears_0_nominal", "joint_clears_0_conservative", "joint_clears_m0.006_nominal", "joint_clears_m0.006_conservative")))
      gu <- ce[cluster == "uid" & kind == "grid"]; gs <- ce[cluster == "stratum" & kind == "grid"]
      okj <- all(vapply(jcols, function(jc) { col <- sub("^joint_", "", jc); all(vapply(seq_len(nrow(gu)), function(i) { a <- gu[[col]][i]; bb <- gs[[col]][gs$M == gu$M[i]]; exp <- if (isFALSE(a) || isFALSE(bb)) FALSE else if (isTRUE(a) && isTRUE(bb)) TRUE else NA; identical(gu[[jc]][i], exp) && identical(gs[[jc]][gs$M == gu$M[i]], exp) }, TRUE)) }, TRUE))
      check("joint per-point clears: TRUE only if both clusterings clear, FALSE if either loses, NA otherwise", okj)
      ok2 <- TRUE
      for (k in seq_len(nrow(cj))) { r <- cj[k]; a <- as.list(cb[cluster == "uid" & restriction == r$restriction & outcome == r$outcome & threshold == r$threshold & flag == r$flag]); b_ <- as.list(cb[cluster == "stratum" & restriction == r$restriction & outcome == r$outcome & threshold == r$threshold & flag == r$flag]); j <- joint_breakdown(a, b_)
        ok2 <- ok2 && identical(r$joint_status, j$status) && isTRUE(all.equal(r$joint_bracket_lo, j$bracket_lo)) }
      check("joint breakdown file = smaller of uid and stratum per flag, 'no initial clearance' if either has none", nrow(cj) == 4L && ok2 && setequal(cj$flag, c("nominal", "conservative")))
    }
  }
}
# edge-hit probe, end to end (T-F4 / A2 item 4): a grid whose upper end lies inside the interval at Mbar = 0 -> every probe is an edge hit -> the valve trips on it and T is never projected from its seconds
res <- run("--synthetic", "--valve", "--calendars", "weekly", "--in", shQuote(dpl), "--out", q("valve_edge"), "--rm-grid-points", "60", "--valve-gp-floor", "40", "--rm-grid-lb", "-0.2", "--rm-grid-ub", "-0.015")
check("valve with an edge-hit probe runs", res$status == 0L, paste(tail(res$out, 4), collapse = "\n"))
if (res$status == 0L) {
  ep <- rd("valve_edge", "o2_valve_probes.csv"); epl <- rd("valve_edge", "o2_valve_plan.csv"); elg <- rd("valve_edge", "o2_valve_log.csv")
  check("edge-hit probes: status_class 'edge' in the probes file, the valve trips on them, T is NA, chain runs to 'valve exhausted' with RM kept", nrow(ep) == 3L && all(ep$status_class == "edge") && elg[iter == 1, probe_trip] && is.na(elg[iter == 1, projected_T_secs]) && is.na(epl$projected_T_secs) &&
        epl$exhausted && epl$rm_computed && epl$rm_grid_center == "estimate" && epl$probe_status == "edge" && grepl("exhausted", epl$fallback), paste(capture.output(print(ep)), collapse = " "))
}
# default (no valve) setting: C-LF, theta grid +-20 sd centred at 0, step printed
res <- run("--synthetic", "--cell", "weekly_uid_D", "--in", shQuote(dpl), "--out", q("o_def"), "--restrictions", "RM", "--rm-grid", "0", "--bisections", "0", "--rm-grid-points", "60", "--thresholds", shQuote("D=0;A=0"))
check("no valve: default method C-LF with the theta grid +-20 sd of l'beta_post centred at 0 and its step printed", res$status == 0L && local({ ev <- rd("o_def", "o2_evals.csv")[restriction == "RM"]; og <- rd("o_def", "o2_original.csv"); all(ev$method == "C-LF") && all(abs(ev$grid_lb + 20 * og$se) < 1e-12) && all(abs(ev$grid_ub - 20 * og$se) < 1e-12) && all(abs(ev$dtheta - 40 * og$se / 59) < 1e-12) }), paste(tail(res$out, 3), collapse = "\n"))
# fallback (iv): daily RM not computed, an explicit row says so, SD still runs
plan4 <- file.path(tmp, "plan_iv.csv"); fwrite(data.table(calendar = "daily", rm_method = "Conditional", rm_grid_points = 200L, rm_grid_k = 8, rm_grid_center = "estimate", rm_computed = FALSE, exhausted = FALSE, parallel = TRUE, fallback = "(i) parallel; (ii); (iii); (iv) daily RM not computed", probe_secs = 100, projected_T_secs = 9000), plan4)
res <- run("--synthetic", "--cell", "daily_uid_D", "--in", shQuote(din), "--out", q("o_iv"), "--pre-keep", "6", "--sd-grid", "0", "--bisections", "1", "--valve-plan", shQuote(plan4), "--thresholds", shQuote("D=0;A=0"))
check("valve (iv): daily RM not computed row in breakdown (both flags) and evals; SD kept; timing says rm_computed FALSE", res$status == 0L && local({ br <- rd("o_iv", "o2_breakdown.csv"); ev <- rd("o_iv", "o2_evals.csv"); tm <- rd("o_iv", "o2_timing.csv")
  br[restriction == "RM", .N] == 2L && all(br[restriction == "RM", status] == "daily RM not computed (valve iv)") && any(ev$kind == "valve" & ev$status == "daily RM not computed (valve iv)") && !any(ev$restriction == "RM" & ev$kind %in% c("grid", "bisect")) &&
    br[restriction == "SD", .N] == 1L && br[restriction == "SD", status] != "daily RM not computed (valve iv)" && !tm$rm_computed }), paste(tail(res$out, 3), collapse = "\n"))
res <- run("--synthetic", "--cell", "weekly_uid_D", "--in", shQuote(dpl), "--out", q("o_pcomb"), "--valve-plan", q("valve", "o2_valve_plan.csv"), "--rm-method", "Conditional")
check("--valve-plan cannot be used together with --rm-method/--rm-grid-points", res$status != 0L && any(grepl("valve-plan", res$out)))
# timeout label (F4): HonestDiD swallows the time-limit error; the harness must say 'timeout', never 'failed: empty'
res <- run("--synthetic", "--cell", "weekly_uid_D", "--in", shQuote(dw), "--out", q("o_to"), "--restrictions", "RM", "--rm-grid", "0,1", "--rm-grid-points", "100", "--rm-grid-lb", "-0.4", "--rm-grid-ub", "0.4", "--point-seconds", "0.3", "--thresholds", shQuote("D=0;A=0"))
check("evaluation time limit is labelled 'timeout' (not 'failed: empty'), never clear or lost", res$status == 0L && local({ ev <- rd("o_to", "o2_evals.csv")[restriction == "RM"]; br <- rd("o_to", "o2_breakdown.csv")[restriction == "RM"]
  all(ev$status == "timeout") && !any(grepl("empty", ev$status)) && all(is.na(ev$clears_0_nominal)) && all(is.na(ev$clears_0_conservative)) && all(grepl("^initial point failed", br$status)) }), paste(tail(res$out, 3), collapse = "\n"))
# ---- guards ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------
res <- run("--synthetic", "--cell", "daily_uid_D", "--in", shQuote(din), "--out", shQuote(file.path(root, "out", "recovery", "x")))
check("refuses to write under out/recovery", res$status != 0L && !dir.exists(file.path(root, "out", "recovery", "x")))
res <- run("--synthetic", "--cell", "daily_uid_D", "--in", shQuote(din), "--out", q("o_sd"))
check("refuses a non-empty --out", res$status != 0L)

# ---- collect (8 cheap cells) ---------------------------------------------------------------------------------------------------------------------------------------------------
cells <- file.path(tmp, "cells"); dir.create(cells)
ok_all <- TRUE
for (cal in c("daily", "weekly")) for (cl in c("uid", "stratum")) for (oc in c("D", "A")) {
  d <- file.path(tmp, sprintf("in_%s_%s_%s", cal, cl, oc)); write_input(d, oc, cl, if (cal == "daily") lin_beta(S_, TAU) else wk_beta(SW, TW), SD_, sd_post = if (cal == "daily") SD_ else SDP)
  extra <- if (cal == "daily") c("--pre-keep", "6", "--restrictions", "SD", "--sd-grid", "0") else c("--restrictions", "SD")
  r_ <- run("--synthetic", "--cell", sprintf("%s_%s_%s", cal, cl, oc), "--in", shQuote(d), "--out", shQuote(file.path(cells, sprintf("%s_%s_%s", cal, cl, oc))), extra)
  ok_all <- ok_all && r_$status == 0L
}
check("8 synthetic cells ran", ok_all)
res <- run("--collect", "--synthetic", "--cells-dir", shQuote(cells), "--out", q("collected"))
check("collect runs", res$status == 0L, paste(tail(res$out, 4), collapse = "\n"))
if (res$status == 0L) {
  co <- file.path(tmp, "collected"); mf <- readLines(file.path(co, "MANIFEST.sha256"))
  sums <- vapply(mf, function(l) { f <- sub("^[0-9a-f]{64}  ", "", l); h <- strsplit(system2("sha256sum", shQuote(file.path(co, f)), stdout = TRUE), " ")[[1]][1]; identical(h, sub("  .*$", "", l)) }, TRUE)
  check("collected MANIFEST verifies and lists sessionInfo", all(sums) && any(grepl("sessionInfo.txt$", mf)) && length(mf) >= 14L)
  bb <- fread(file.path(co, "o2_breakdown.csv")); dd <- fread(file.path(co, "o2_disagreement.csv"))
  check("collected breakdown has all 8 cells", uniqueN(bb[, .(calendar, cluster, outcome)]) == 8L)
  check("disagreement table built (per flag), joint file written, banner kept", nrow(dd) > 0 && "flag" %in% names(dd) && !"combined_rule" %in% names(dd) && file.exists(file.path(co, "o2_joint.csv")) && grepl("^SYNTHETIC", dd$banner[1]))
  check("summary carries the banner", grepl("SYNTHETIC", readLines(file.path(co, "summary.md"))[1]))
}
unlink(file.path(cells, "weekly_stratum_A"), recursive = TRUE)
res <- run("--collect", "--synthetic", "--cells-dir", shQuote(cells), "--out", q("collected2"))
check("collect refuses a missing cell", res$status != 0L && any(grepl("missing cell", res$out)))
cat("tamper\n", file = file.path(cells, "daily_uid_D", "o2_breakdown.csv"), append = TRUE)
res <- run("--collect", "--synthetic", "--cells-dir", shQuote(cells), "--out", q("collected3"), "--allow-missing")
check("collect refuses a cell whose file no longer matches its MANIFEST", res$status != 0L && any(grepl("does not match its MANIFEST", res$out)))

cat(sprintf("\n%d passed, %d failed\n", npass, fails))
unlink(tmp, recursive = TRUE); unlink(guard_parent, recursive = TRUE); try(file.remove(file.path(root, "out", "exploratory")), silent = TRUE)   # file.remove leaves a non-empty directory alone
quit(save = "no", status = if (fails) 1L else 0L)
