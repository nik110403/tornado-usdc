# Fixture-only tests for exploratory/o1_restricted_posthoc.R (exact cut-points guard, V1/V2, no-redraw weights). No real data.
# Run from the repo root:  Rscript exploratory/tests/test_o1_restricted_posthoc.R
a <- grep("^--file=", commandArgs(FALSE), value = TRUE)
setwd(normalizePath(file.path(dirname(sub("^--file=", "", a[1])), "..", "..")))
for (f in c("utils", "decide", "cem", "estimate", "pre", "presign", "drdid", "post")) source(file.path("R", paste0(f, ".R")))
O1_NO_MAIN <- TRUE; PH_NO_MAIN <- TRUE
source("exploratory/o1_estimate.R")
source("exploratory/o1_restricted_posthoc.R")
.n <- 0L
test <- function(name, expr) {
  r <- try(force(expr), silent = TRUE)
  if (inherits(r, "try-error")) stop(sprintf("TEST FAILED: %s\n%s", name, conditionMessage(attr(r, "condition"))), call. = FALSE)
  .n <<- .n + 1L; cat(sprintf("  ok  %s\n", name))
}
near <- function(x, y, tol = 1e-12) stopifnot(isTRUE(all.equal(x, y, tolerance = tol)))
mkfr <- function(mh, treat, tag) data.table(uid = sprintf("0x%s%02d", tag, seq_along(mh)), address = sprintf("0x%s%02d", tag, seq_along(mh)), treat = treat,
                                            mean_hold = mh, transfers = 1, age = 1, eth = 1, usdc_share = 0.5)
stopifnot(identical(banner_check <- grepl("POST-HOC VARIANT, dated 2026-10-30", PH_BANNER), TRUE), grepl("does not reclassify H1", PH_BANNER))

# ---- (a) rounding at 15 digits moves a value on the cut -------------------------------------------------------------------------------------
test("(a) 15-digit rounded cut-point differs from the exact one and moves a wallet to another stratum", {
  x <- 0.1 + 0.2                                             # 0.30000000000000004; the 25th percentile (type 7, n = 5) of the exposed values is exactly their 2nd value
  fr <- rbind(mkfr(c(0, x, 1, 2, 3), TRUE, "e"), mkfr(c(0.3, 0.3), FALSE, "c"))
  pc <- po_primary_cuts(fr); ex <- pc$cuts; stopifnot(ex$mean_hold[1] == x)
  rd <- lapply(ex, signif, digits = 15); stopifnot(rd$mean_hold[1] == 0.3, !identical(ex$mean_hold, rd$mean_hold))
  stopifnot(findInterval(0.3, ex$mean_hold)[1] == 0L, findInterval(0.3, rd$mean_hold)[1] == 1L)
  stopifnot(!identical(ps_sha256_text(unlist(lapply(names(rd), function(k) sprintf("%s:%s", k, paste(sprintf("%.17g", rd[[k]]), collapse = ","))))), pc$hash))   # the hash guard sees it
  s_ex <- ph_strata(fr, ex); s_rd <- ph_strata(fr, rd)
  st <- function(s, id) s[uid == id, stratum]
  stopifnot(st(s_ex, "0xc01") == st(s_ex, "0xe01"), st(s_rd, "0xc01") == st(s_rd, "0xe02"), st(s_ex, "0xc01") != st(s_ex, "0xe02"))   # exact: with the wallet at 0; rounded: with the wallet on the cut
})

# ---- tiny frame: A 2 exposed + 9 controls, B 1 + 2, C 1 + 0 (exposed dropped), D 0 + 3 (no exposed) ------------------------------------------
fr <- rbind(mkfr(c(10, 10, 20, 30), TRUE, "e"), mkfr(rep(10, 9), FALSE, "a"), mkfr(rep(20, 2), FALSE, "b"), mkfr(rep(5, 3), FALSE, "d"))
pc0 <- po_primary_cuts(fr); good <- list(cutpoints_hash = pc0$hash, seed = SEED_PRIMARY_CEM)
test("(b) V1 and V2 both stop on a wrong cutpoints hash (and on a wrong seed), run on the right one", {
  bad <- list(cutpoints_hash = "deadbeef", seed = SEED_PRIMARY_CEM)
  for (f in list(ph_v1, ph_v2, ph_exact_cuts)) {
    r <- tryCatch(f(fr, bad), error = function(e) conditionMessage(e)); stopifnot(is.character(r), grepl("cutpoints_hash", r, fixed = TRUE))
  }
  r <- tryCatch(ph_v2(fr, list(cutpoints_hash = pc0$hash, seed = 1L)), error = function(e) conditionMessage(e)); stopifnot(is.character(r), grepl("seed", r))
  stopifnot(is.list(ph_v1(fr, good)), is.list(ph_v2(fr, good)), identical(ph_v1(fr, good)$cuts, pc0$cuts))
})
test("(b2) a restricted subset uses the cut-points of the whole frame: without cut_frame the guard stops, with it both variants run on those cuts", {
  sub <- fr[!(uid %in% c("0xa01", "0xe04", "0xd01"))]
  stopifnot(!identical(po_primary_cuts(sub)$hash, pc0$hash))
  r <- tryCatch(ph_v1(sub, good), error = function(e) conditionMessage(e)); stopifnot(is.character(r), grepl("cutpoints_hash", r, fixed = TRUE))
  v1 <- ph_v1(sub, good, fr); v2 <- ph_v2(sub, good, fr)
  stopifnot(identical(v1$cuts, pc0$cuts), identical(v2$cuts, pc0$cuts), all(v2$matched$uid %in% sub$uid), all(v1$matched$uid %in% sub$uid))
})
test("(c) V2 keeps every admitted control of a kept stratum; IKP weights equal the hand calculation; V1 redraws 3:1", {
  r2 <- ph_v2(fr, good); m <- r2$matched
  # hand calculation: kept strata A (2 exposed, 9 ctrl) and B (1, 2); m_T = 3, m_C = 11; C (1 exposed, 0 ctrl) dropped; D (0 exposed, 3 ctrl) unmatched
  stopifnot(r2$m_T == 3, r2$m_C == 11, r2$dropped_exposed == 1L, r2$n_controls_matched == 11L, sum(m$treat) == 3L)
  stopifnot(all(sprintf("0xa%02d", 1:9) %in% m$uid), all(sprintf("0xb%02d", 1:2) %in% m$uid), !any(grepl("^0xd", m$uid)), !("0xe04" %in% m$uid))
  near(m[uid == "0xa01", w], (11 / 3) * (2 / 9)); near(m[uid == "0xb01", w], (11 / 3) * (1 / 2)); stopifnot(all(m[treat == TRUE, w] == 1))
  near(m[treat == FALSE, sum(w)], 11)                 # control weights sum to m_C (IKP)
  # V1 = cem_match_fixed 3:1: A keeps min(9, 6) = 6 controls
  r1 <- ph_v1(fr, good); stopifnot(r1$n_controls_matched == 8L, sum(grepl("^0xa", r1$matched$uid)) == 6L)
  # V2 = cem_match_fixed with an unbinding ratio (independent cross-check of the strata and weights)
  rx <- cem_match_fixed(fr, CEM_VARS, pc0$cuts, "three_bins", ratio = 1000L)
  stopifnot(identical(rx$matched$uid, m$uid), identical(rx$matched$stratum, m$stratum)); near(rx$matched$w, m$w)
})
test("(d) ph_run_variant end to end on a synthetic balanced panel: both variants estimate, counts per arm, V2 keeps more controls", {
  set.seed(11); nt <- 12L; nc <- 40L; uid <- sprintf("0x%s%02d", rep(c("e", "c"), c(nt, nc)), c(seq_len(nt), seq_len(nc)))
  treat <- rep(c(TRUE, FALSE), c(nt, nc)); mh <- c(rep(c(10, 20), each = nt / 2), rep(c(10, 20), c(30, 10)))
  frd <- data.table(uid = uid, address = uid, treat = treat, mean_hold = mh, transfers = 1, age = 1, eth = 1, usdc_share = 0.5)
  gd <- CJ(i = seq_along(uid), day_index = c(-100:59, 61:70)); gd[, `:=`(uid = uid[i], treat = treat[i])]
  base <- 1e6 * runif(length(uid), 50, 500)
  gd[, usdc_units := round(base[i] * (1 + 0.1 * rnorm(.N)) * (1 + ifelse(treat & day_index > 60, -0.09, 0)))]; gd[, usdt_units := round(base[i] * 0.5 * (1 + 0.05 * rnorm(.N)))]
  wide <- gd[, .(uid, day_index, usdc_units, usdt_units)]
  p <- list(cutpoints_hash = po_primary_cuts(frd)$hash, seed = SEED_PRIMARY_CEM)
  msig <- data.table(uid = uid, treat = treat, stratum = 1L, w = 1)
  caps <- list(D = c(-3, 4), A = c(0, 5)); jumps <- list(D = -0.09, A = -0.09)
  R1 <- ph_run_variant(ph_v1, frd, p, wide, uid, caps, jumps, O1_DEFAULTS, msig); R2 <- ph_run_variant(ph_v2, frd, p, wide, uid, caps, jumps, O1_DEFAULTS, msig)
  stopifnot(is.null(R1$error), is.null(R2$error))
  stopifnot(R2$counts[arm == "control", n_matched] == 40L, R1$counts[arm == "control", n_matched] == 28L, R1$counts[arm == "exposed", n_matched] == nt)
  stopifnot(!is.null(R2$analysis$fits[["capped|D|uid"]]$V), R2$dropped[arm == "control", n_dropped] == 0L, R1$dropped[arm == "control", n_dropped] == 12L)
  tbv <- o1_tiebreak(R2$analysis$estimands[[1]]$est, R2$analysis$estimands[[1]]$est); stopifnot(is.character(tbv$reading))
})
cat(sprintf("ALL %d O1 POST-HOC R TESTS PASSED\n", .n))
