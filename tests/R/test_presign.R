# S17 presign tests: fixture-only (tmp dirs, synthetic data; nothing under out/ is read). Runnable standalone:
#   Rscript tests/R/test_presign.R
# or via tests/R/run_tests.R once R/presign.R is in its source list (S20b owns that entrypoint edit).
if (!exists("classify_ci")) {
  a <- grep("^--file=", commandArgs(FALSE), value = TRUE)
  setwd(normalizePath(file.path(dirname(sub("^--file=", "", a[1])), "..", "..")))
  for (f in c("utils", "decide", "cem", "estimate", "validate", "synth", "pre", "blacklist")) source(file.path("R", paste0(f, ".R")))
}
if (!exists("run_presign")) source(file.path("R", "presign.R"))
if (!exists("test")) {
  .t_total <- 0L
  test <- function(name, expr) {
    r <- try(force(expr), silent = TRUE)
    if (inherits(r, "try-error")) stop(sprintf("TEST FAILED: %s\n%s", name, conditionMessage(attr(r, "condition"))), call. = FALSE)
    .t_total <<- .t_total + 1L; cat(sprintf("  ok  %s\n", name))
  }
  standalone <- TRUE
} else standalone <- FALSE
fails <- function(expr, pat) { e <- try(expr, silent = TRUE); inherits(e, "try-error") && grepl(pat, conditionMessage(attr(e, "condition"))) }

tmpd <- tempfile("presign"); dir.create(tmpd)
inp_dir <- file.path(tmpd, "in"); dir.create(inp_dir)
set.seed(11)
N <- 1500L; ids <- 0:(N - 1L)
arm <- sample(c(rep("control", 1200L), rep(c("withdrawal", "deposit", "both"), 100L)))
addr <- sprintf("0x%040x", ids * 7919L + 1L)
base <- exp(rnorm(N, 8, 0.8))
usd_frac <- ifelse(ids %% 5L == 0L, 1, ifelse(ids %% 7L == 0L, 0, runif(N)))   # exact 0 / 1 / between
g <- CJ(wallet_id = ids, coin = c("USDC", "USDT"), day_index = 0:59)
g[, date := format(PRE_PERIOD_START + day_index)]
g[, share := usd_frac[wallet_id + 1L]]
g[, balance_units := round(base[wallet_id + 1L] * runif(.N, .9, 1.1) * 1e6 * ifelse(coin == "USDC", share, 1 - share) * 2)]
g[, share := NULL]
low <- ids %in% c(2L, 3L, 4L, 5L)                          # fail the mean-holdings floor
g[wallet_id %in% ids[low], balance_units := 1000]
sparse <- 10L; g[wallet_id == sparse & day_index >= 8, balance_units := 0]       # < 14 nonzero days (mean also low-ish: keep large early balances)
g[wallet_id == sparse & day_index < 8, balance_units := 1e10]
cov <- data.table(wallet_id = ids, address = addr, arm = arm, transfer_count_total = rpois(N, 8),
  eth_balance_wei_units = format(1e18 * rexp(N), scientific = FALSE), wallet_age_blocks = ifelse(runif(N) < .1, NA, 1.2e7 - rexp(N, 1e-6)),
  issuer_mint_redeem_flag = ifelse(ids %% 53L == 1L, "True", "False"), blacklisted_usdc_at_pre_end = "False", blacklisted_usdt_at_pre_end = "False")
fwrite(g, file.path(inp_dir, "wallet_coin_day_pre.csv")); fwrite(cov, file.path(inp_dir, "covariates.csv"))
bl <- addr[c(20L, 21L, 22L)]
dfz <- addr[101:138]                                         # 38-address direct-freeze fixture
dff <- file.path(tmpd, "freeze.csv"); writeLines(c("address", dfz), dff)
gp <- function(...) modifyList(list(status = "genuine", population_id = "june_placebo", outcome = "capped_D", trace_hash = strrep("a", 64)), list(...))
man <- list(population_id = "main", generation_id = "gen-test-1", cutoff_block = 15298282L, baseline_start_date = "2022-06-09",
            baseline_end_date = "2022-08-07", excluded_dates = c("2022-08-08"), input_file_sha256 = list(`g1/cohort.csv` = strrep("c", 64)))
mk_sch <- function(rule, sha = strrep("c", 64)) { f <- tempfile(fileext = ".json"); writeLines(jsonlite::toJSON(list(sha256 = sha, notes = sprintf("participation_rule=%s (UNRESOLVED)", rule)), auto_unbox = TRUE), f); f }
scs <- strrep("c", 64)   # side manifest g1/cohort.csv sha256 (required)
sch_x <- mk_sch("x"); sch_u <- mk_sch("UNRESOLVED-recorded")
cands <- data.table(address = c(addr, sprintf("0x%040x", 900000L + 0:19)), source_membership = c(rep("g4", N), rep("frame_extra", 20L)))
ow <- data.table(start_block = PRE_START_BLOCK + 1000L * (0:29), end_block = PRE_START_BLOCK + 1000L * (0:29) + 399L)
out1 <- file.path(tmpd, "out1")
pr <- run_presign(inp_dir, man, "main", out1, bl, cands, ow, transfer_participation_rule = "UNRESOLVED-recorded", direct_freeze_file = dff, cohort_schema_file = sch_u, side_cohort_sha256 = scs)

test("distinct main outputs: membership / parameters / coverage / quartiles / A1 / hashes all written", {
  for (f in c("match_membership.csv", "analysis_parameters.json", "eligible_frame_coverage.csv", "eligible_frame_funnel.csv",
              "exposed_quartiles.csv", "a1_diagnostics.json", "cohort_hashes.json", "enlargement_request.json", "enlargement_request_windows.csv"))
    stopifnot(file.exists(file.path(out1, f)))
  mm <- fread(file.path(out1, "match_membership.csv"))
  stopifnot(identical(names(mm), c("population_id", "generation_id", "address", "arm", "stratum", "matched", "weight", "match_rank", "cutpoints_hash", "seed")),
            !anyDuplicated(mm$address), all(mm[arm == "exposed" & matched == TRUE, weight] == 1), all(mm[matched == FALSE, weight] == 0),
            all(mm$seed == SEED_PRIMARY_CEM), nchar(mm$cutpoints_hash[1]) == 64,
            mm[arm == "control" & matched == TRUE, .N, by = stratum][, max(N)] > 0)
  mx <- merge(mm[arm == "exposed" & matched == TRUE, .N, by = stratum], mm[arm == "control" & matched == TRUE, .N, by = stratum], by = "stratum")
  stopifnot(all(mx$N.y <= 3 * mx$N.x))
  pj <- jsonlite::fromJSON(file.path(out1, "analysis_parameters.json"))
  stopifnot(pj$transfer_participation_rule == "UNRESOLVED-recorded", pj$seed == SEED_PRIMARY_CEM, length(pj$cap_values$D) == 2, length(pj$cap_values$A) == 2,
            nchar(pj$cohort_hashes$matched_exposed) == 64, nchar(pj$input_sha256[["covariates.csv"]]) == 64)
})
test("eligibility: floors, any-time blacklist and issuer screen applied; funnel counts reconcile; every candidate has a coverage row", {
  cvg <- fread(file.path(out1, "eligible_frame_coverage.csv"))
  stopifnot(setequal(cvg$address, cands$address), !anyDuplicated(cvg$address),
            all(cvg[address %in% bl, screen_reason] == "baseline_blacklist"),
            cvg[address == addr[sparse + 1L], screen_reason] == "floor_nonzero_days",
            all(cvg[address %in% addr[low], screen_reason] == "floor_mean_holdings"),
            all(cvg[address %in% sprintf("0x%040x", 900000L + 0:19), screen_reason] == "not_in_pre_population"),
            any(cvg$screen_reason == "issuer_zero_address"))
  f <- pr$funnel; stopifnot(sum(f$exposed) + sum(f$control) == N)
  agg <- fread(file.path(out1, "eligible_frame_funnel.csv")); stopifnot(sum(agg$N) == nrow(cands))
})
test("original exposed quartiles are type-7 over eligible exposed and match the recorded CEM cut-points", {
  el <- main_match_frame(load_pre_inputs(inp_dir), bl, dfz)$eligible
  q <- pr$quartiles[variable == "mean_hold"]
  stopifnot(isTRUE(all.equal(c(q$q25, q$q50, q$q75), unname(quantile(el[treat == TRUE, mean_hold], c(.25, .5, .75), type = 7)))),
            isTRUE(all.equal(pr$match$cutpoints$mean_hold, unique(c(q$q25, q$q50, q$q75)))))
})
test("fixed cut-points / seed: result is identical on rerun and the cut-point hash is stable", {
  pr2 <- run_presign(inp_dir, man, "main", file.path(tmpd, "out2"), bl, cands, ow, "UNRESOLVED-recorded", dff, sch_u, scs)
  stopifnot(identical(pr2$hashes, pr$hashes), identical(pr2$membership, pr$membership), identical(pr2$params$cap_values, pr$params$cap_values))
  stopifnot(pr$params$seed == 20261015L, pr$params$usdc_mode == "three_bins")
})
test("main caps come from the final main match (type-7 1/99 over baseline wallet-days), not from any June value", {
  inp <- load_pre_inputs(inp_dir); el <- main_match_frame(inp, bl, dfz)$eligible
  res <- cem_match(el[, .(uid, treat, address, mean_hold, transfers, age, eth, usdc_share)], CEM_VARS, "three_bins", seed = SEED_PRIMARY_CEM)
  p <- build_panel(inp$wide, res$matched, 0:59)
  stopifnot(identical(pr$caps$D, quantile(p$D, c(.01, .99), type = 7, names = FALSE)), pr$caps$n_wallet_days == nrow(p), nrow(p) == nrow(res$matched) * 60L)
})
test("A1(a)/(b) are descriptive, documented, and select nothing", {
  a <- pr$a1; stopifnot(is.finite(a$a$difference), a$a$n_exposed > 0, grepl("no test", a$note), grepl("0..29 vs 30..59", a$b$half_split))
  if (a$a_differs) stopifnot(is.finite(a$b$slope_change_on_first_half), is.finite(a$b$correlation), a$b$n_used > 0)
  stopifnot(!any(grepl("p_value|alpha|selected", names(unlist(a)))))
  # (b) is computed even when (a) shows no difference
  same <- copy(main_match_frame(load_pre_inputs(inp_dir), bl, dfz)$eligible); same[, usdc_share := 0.5]
  z <- a1_diagnostics(same, load_pre_inputs(inp_dir)$wide); stopifnot(!z$a_differs, z$b$computed)
})
test("A1 diagnostics selecting nothing: matching is bit-identical whatever A1 reports", {
  el <- main_match_frame(load_pre_inputs(inp_dir), bl, dfz)$eligible
  r1 <- cem_match(el[, .(uid, treat, address, mean_hold, transfers, age, eth, usdc_share)], CEM_VARS, "three_bins", seed = SEED_PRIMARY_CEM)
  invisible(a1_diagnostics(el, load_pre_inputs(inp_dir)$wide))
  r2 <- cem_match(el[, .(uid, treat, address, mean_hold, transfers, age, eth, usdc_share)], CEM_VARS, "three_bins", seed = SEED_PRIMARY_CEM)
  stopifnot(identical(r1$matched, r2$matched), r1$usdc_mode == "three_bins")
})
test("missing required data fails before any read; a post-event decoy file is never consulted", {
  d <- file.path(tmpd, "inc"); dir.create(d); writeLines("not,a,csv\n\"unterminated", file.path(d, "post_wallet_coin_day.csv"))
  fwrite(cov, file.path(d, "covariates.csv"))      # panel missing
  stopifnot(fails(run_presign(d, man, "main", file.path(tmpd, "o3"), bl, cands, ow, "x", dff, sch_x, scs), "required pre-event input missing.*wallet_coin_day_pre.csv.*no post-event"),
            !dir.exists(file.path(tmpd, "o3")))
  stopifnot(fails(run_presign(inp_dir, man, "main", file.path(tmpd, "o4"), NULL, cands, ow, "x", dff, sch_x, scs), "blacklist_any is required"))
  stopifnot(fails(run_presign(inp_dir, man, "main", file.path(tmpd, "o5"), bl, cands[-1], ow, "x", dff, sch_x, scs), "candidates incomplete"))
})
test("guards: no default/live output paths, immutable generation, population label, transfer rule required", {
  stopifnot(fails(run_presign(inp_dir, man, "main", file.path(tmpd, "out", "r_pre"), bl, cands, ow, "x", dff, sch_x, scs), "r_pre"),
            fails(run_presign(inp_dir, man, "main", out1, bl, cands, ow, "x", dff, sch_x, scs), "immutable"),
            fails(run_presign(inp_dir, modifyList(man, list(population_id = "june_placebo")), "main", file.path(tmpd, "o6"), bl, cands, ow, "x", dff, sch_x, scs), "not the requested main"),
            fails(run_presign(inp_dir, modifyList(man, list(cutoff_block = 15298283L)), "main", file.path(tmpd, "o6"), bl, cands, ow, "x", dff, sch_x, scs), "cutoff_block"),
            fails(run_presign(inp_dir, man, "main", file.path(tmpd, "o6"), bl, cands, ow, "", dff, sch_x, scs), "transfer_participation_rule is required"))
})

# ---- enlargement ----------------------------------------------------------------------------------------
test("exactly 30 additional non-overlapping 400-block windows, seed 20251016, deterministic, none overlap originals", {
  w1 <- draw_enlargement_windows(ow); w2 <- draw_enlargement_windows(ow)
  stopifnot(identical(w1, w2), nrow(w1) == 30L, all(w1$end_block - w1$start_block == 399), SEED_ENLARGEMENT == 20251016L,
            all(w1$start_block >= PRE_START_BLOCK), all(w1$end_block <= PRE_END_BLOCK), isTRUE(validate_enlargement_windows(w1, ow)))
  stopifnot(identical(pr$request$windows, w1), identical(freeze_window_request(ow)$request_hash, pr$request$request_hash))
  set.seed(1); w3 <- draw_enlargement_windows(ow); stopifnot(identical(w3, w1))     # ambient RNG state irrelevant
})
test("overlap rejection: drawn windows avoid originals; validator rejects overlapping / wrong-count / wrong-size sets", {
  dense <- data.table(start_block = PRE_START_BLOCK + 400L * (0:600), end_block = PRE_START_BLOCK + 400L * (0:600) + 399L)  # tile most of the range
  w <- draw_enlargement_windows(dense)
  stopifnot(!any(vapply(seq_len(nrow(w)), function(i) any(windows_overlap(w$start_block[i], w$end_block[i], dense$start_block, dense$end_block)), TRUE)))
  bad <- copy(draw_enlargement_windows(ow)); bad[2, `:=`(start_block = bad$start_block[1] + 100, end_block = bad$start_block[1] + 499)]
  stopifnot(fails(validate_enlargement_windows(bad, ow), "overlap each other"),
            fails(validate_enlargement_windows(draw_enlargement_windows(ow)[1:29], ow), "exactly 30"),
            fails(validate_enlargement_windows({x <- copy(draw_enlargement_windows(ow)); x[1, end_block := end_block + 1]; x}, ow), "exactly 400"),
            fails(validate_enlargement_windows({x <- copy(draw_enlargement_windows(ow)); x[1, `:=`(start_block = ow$start_block[1] + 10, end_block = ow$start_block[1] + 409)]; x}, ow), "overlaps an original"))
  full <- data.table(start_block = PRE_START_BLOCK, end_block = PRE_END_BLOCK)
  stopifnot(fails(draw_enlargement_windows(full), "could not draw exactly 30"))
})
test("trigger: genuine low power (larger of the two clustered SEs) triggers; power >= 0.80 does not", {
  se_hi <- 0.0301                                       # power_normal(0.0301) < 0.8
  d <- decide_enlargement(gp(se_wallet = 0.010, se_stratum = se_hi))
  stopifnot(d$trigger, d$real_branch_active, d$se == se_hi, abs(d$power - power_normal(se_hi)) < 1e-12, d$power < POWER_TARGET)
  d2 <- decide_enlargement(gp(se_wallet = se_hi, se_stratum = 0.010)); stopifnot(d2$trigger, d2$se == se_hi)
  lo <- decide_enlargement(gp(se_wallet = 0.0149, se_stratum = 0.0150))
  stopifnot(!lo$trigger, lo$real_branch_active, lo$power >= POWER_TARGET, lo$se == 0.0150)
  # the smaller SE alone would pass; the larger one fails => larger SE decides
  stopifnot(power_normal(0.012) >= POWER_TARGET, decide_enlargement(gp(se_wallet = 0.012, se_stratum = 0.05))$trigger)
})
test("trigger: terminal-unavailable placebo triggers even with no SE; development / non-terminal unavailable never activate a real branch", {
  t <- decide_enlargement(list(status = "terminal_unavailable")); stopifnot(t$trigger, t$real_branch_active, is.na(t$power))
  for (s in c("development", "unavailable_nonterminal")) {
    d <- decide_enlargement(list(status = s, se_wallet = 0.5, se_stratum = 0.5))     # low-power-looking SEs are ignored
    stopifnot(is.na(d$trigger), d$disposition == "undecidable", !d$real_branch_active,
              fails(enlargement_disposition(d), "undecidable"), fails(adopt_enlargement(d, pr$request, ow), "undecidable"))
  }
})
test("missing / invalid placebo input stops (never silently no-trigger)", {
  for (bad in list(NULL, list(), list(status = "bogus"), list(status = "genuine"), gp(se_wallet = NA_real_, se_stratum = 0.02),
                   gp(se_wallet = 0.02, se_stratum = -1), gp(se_wallet = 0.02, se_stratum = Inf), gp(se_wallet = 0.02)))
    stopifnot(fails(decide_enlargement(bad), "placebo"))
})
test("adoption exactly once; adopted even if enlarged power falls; no-trigger cannot adopt", {
  dec <- decide_enlargement(gp(se_wallet = 0.02, se_stratum = 0.04))
  ad <- adopt_enlargement(dec, pr$request, ow, enlarged_power = dec$power - 0.2)
  stopifnot(ad$adopted, ad$primary_sample == "enlarged", ad$power_fell, ad$n_windows_added == 30L, ad$request_hash == pr$request$request_hash)
  ad2 <- adopt_enlargement(decide_enlargement(list(status = "terminal_unavailable")), pr$request, ow)
  stopifnot(ad2$adopted, !ad2$power_fell, is.na(ad2$enlarged_power))
  stopifnot(fails(adopt_enlargement(dec, pr$request, ow, prior_adoption = ad), "exactly once"))
  nt <- decide_enlargement(gp(se_wallet = 0.01, se_stratum = 0.012))
  stopifnot(!nt$trigger, fails(adopt_enlargement(nt, pr$request, ow), "not triggered"))
})
test("enlargement invariants: cut-point / seed / eligibility / threshold / window-rule / cap-rule drift is rejected; cap VALUES may change", {
  o <- pr$params; e <- o
  stopifnot(isTRUE(check_enlargement_invariants(o, e)))
  e$cap_values <- list(D = c(-9, 9), A = c(0, 9), legs_pooled = c(0, 9)); stopifnot(isTRUE(check_enlargement_invariants(o, e)))
  for (k in c("cutpoints_hash", "seed", "windows_rule", "eligibility", "thresholds", "cap_rule", "usdc_mode", "ratio")) {
    e <- o; e[[k]] <- if (is.numeric(o[[k]])) o[[k]] + 1 else if (is.list(o[[k]])) c(o[[k]], list(drift = 1)) else paste0(o[[k]], "x")
    stopifnot(fails(check_enlargement_invariants(o, e), k))
  }
})
test("fix1/2: adoption re-validates against ORIGINAL windows and the request hash; freeze accepts no overrides", {
  dec <- decide_enlargement(gp(se_wallet = 0.02, se_stratum = 0.04))
  mv <- pr$request; mv$windows <- copy(mv$windows)
  mv$windows[1, `:=`(start_block = ow$start_block[3] + 5, end_block = ow$start_block[3] + 404)]
  stopifnot(fails(adopt_enlargement(dec, mv, ow), "overlaps"))
  tp <- pr$request; tp$windows <- copy(tp$windows); tp$windows[1, `:=`(start_block = start_block + 1000000 %% 7, end_block = end_block + 1000000 %% 7)]
  tp$windows <- copy(pr$request$windows); tp$windows[5, `:=`(start_block = start_block + 1, end_block = end_block + 1)]
  if (!isTRUE(try(validate_enlargement_windows(tp$windows, ow), silent = TRUE))) tp$windows <- pr$request$windows[order(-start_block)]   # reordered under the old hash
  stopifnot(fails(adopt_enlargement(dec, tp, ow), "frozen request hash"))
  ow2 <- copy(ow); ow2[1, `:=`(start_block = start_block + 7L, end_block = end_block + 7L)]
  stopifnot(fails(adopt_enlargement(dec, pr$request, ow2), "frozen request hash"))
  stopifnot(fails(freeze_window_request(ow, seed = 1L), "unused argument"), fails(freeze_window_request(ow, n = 5L), "unused argument"))
})
test("fix3/5: genuine placebo needs june population, capped_D outcome, trace hash; development ids rejected", {
  for (bad in list(list(status = "genuine", se_wallet = .02, se_stratum = .02),
                   gp(se_wallet = .02, se_stratum = .02, population_id = "g4_development"),
                   gp(se_wallet = .02, se_stratum = .02, outcome = "uncapped_D"), gp(se_wallet = .02, se_stratum = .02, trace_hash = "abc")))
    stopifnot(fails(decide_enlargement(bad), "genuine placebo must carry"))
  stopifnot(decide_enlargement(gp(se_wallet = .02, se_stratum = .02))$disposition == "enlarge", enlargement_disposition(decide_enlargement(gp(se_wallet = .001, se_stratum = .001))) == "no_enlarge")
})
test("fix4/6/7/8: direct-freeze set (38) excluded in both arms and hashed; blacklist hashed; A1(b) unconditional; params record choices", {
  cvg <- fread(file.path(out1, "eligible_frame_coverage.csv"))
  stopifnot(all(cvg[address %in% dfz, screen_reason] == "direct_freeze_8aug"), pr$funnel[reason == "direct_freeze_8aug", exposed + control] == 38L)
  pj <- jsonlite::fromJSON(file.path(out1, "analysis_parameters.json"))
  stopifnot(nchar(pj$cohort_hashes$direct_freeze) == 64, nchar(pj$cohort_hashes$blacklist_any) == 64, pj$blacklist_any_n == 3L,
            grepl("noise", pj$a1b_definition), grepl("50,000", pj$window_draw), grepl("14929560", pj$window_draw), grepl("no non-terminal", pj$enlargement_note))
  stopifnot(pr$a1$b$computed)
  bad38 <- file.path(tmpd, "f37.csv"); writeLines(dfz[1:37], bad38)
  stopifnot(fails(run_presign(inp_dir, man, "main", file.path(tmpd, "o7"), bl, cands, ow, "x", bad38, sch_x, scs), "exactly 38"),
            fails(run_presign(inp_dir, man, "main", file.path(tmpd, "o7"), bl, cands, ow, "x", file.path(tmpd, "nope.csv"), sch_x, scs), "direct_freeze_file"),
            fails(run_presign(inp_dir, man, "main", file.path(tmpd, "o7"), c(bl, "0xABC"), cands, ow, "x", dff, sch_x, scs), "blacklist_any"))
  w <- writeLines(c(dfz[1:37], toupper(dfz[38])), bad38); stopifnot(fails(read_direct_freeze(bad38), "lowercase"))
})

test("S24 finding B: participation rule must equal the rule recorded in g1 cohort.schema.json (fail closed)", {
  sj <- tempfile(fileext = ".json")
  wr <- function(notes) writeLines(jsonlite::toJSON(list(sha256 = strrep("c", 64), notes = notes), auto_unbox = TRUE), sj)
  wr("participation_rule=baseline_transfer_required (UNRESOLVED upstream, S14; recorded, not defaulted). eligibility cutoff block 1")
  stopifnot(identical(check_cohort_rule(sj, "baseline_transfer_required", strrep("c", 64), strrep("c", 64)), "baseline_transfer_required"),
            tryCatch({ check_cohort_rule(sj, "none", strrep("c", 64), strrep("c", 64)); FALSE }, error = function(e) grepl("participation rule mismatch", conditionMessage(e))))
  wr("no rule recorded here")
  stopifnot(tryCatch({ check_cohort_rule(sj, "none", strrep("c", 64), strrep("c", 64)); FALSE }, error = function(e) grepl("exactly one participation_rule", conditionMessage(e))))
  wr("participation_rule=none and participation_rule=baseline_transfer_required")
  stopifnot(tryCatch({ check_cohort_rule(sj, "none", strrep("c", 64), strrep("c", 64)); FALSE }, error = function(e) grepl("exactly one participation_rule", conditionMessage(e))))
})

test("S24 finding B: schema sha256 must bind to the verified g1/cohort.csv hash; bad schema files stop", {
  sj <- mk_sch("none")
  stopifnot(identical(check_cohort_rule(sj, "none", strrep("c", 64), strrep("c", 64)), "none"),
            fails(check_cohort_rule(mk_sch("none", strrep("d", 64)), "none", strrep("c", 64), strrep("c", 64)), "does not match the verified g1/cohort.csv"),
            fails(check_cohort_rule(sj, "none", strrep("c", 64), strrep("e", 64)), "side manifest"),
            fails(check_cohort_rule(sj, "none", strrep("c", 64), NULL), "no valid g1/cohort.csv sha256"),
            fails(check_cohort_rule(sj, "none", strrep("c", 64), "xyz"), "no valid g1/cohort.csv sha256"),
            fails(run_presign(inp_dir, man, "main", file.path(tmpd, "o9"), bl, cands, ow, "x", dff, sch_x), "argument \"side_cohort_sha256\" is missing"),
            fails(check_cohort_rule(sj, "none", NULL, strrep("c", 64)), "cannot be bound"),
            fails(run_presign(inp_dir, man, "main", file.path(tmpd, "o8"), bl, cands, ow, "x", dff, mk_sch("x", strrep("d", 64)), scs), "does not match the verified"))
  nosha <- tempfile(fileext = ".json"); writeLines('{"notes": "participation_rule=none"}', nosha)
  stopifnot(fails(check_cohort_rule(nosha, "none", strrep("c", 64), strrep("c", 64)), "does not match"))
  bad <- tempfile(fileext = ".json"); writeLines("{not json", bad)
  stopifnot(fails(check_cohort_rule(bad, "none", strrep("c", 64), strrep("c", 64)), "unreadable"),
            fails(check_cohort_rule(file.path(tmpd, "absent.json"), "none", strrep("c", 64), strrep("c", 64)), "unreadable"))
  arr <- tempfile(fileext = ".json"); writeLines("[1,2]", arr)
  stopifnot(fails(check_cohort_rule(arr, "none", strrep("c", 64), strrep("c", 64)), "not a JSON object|does not match"))
  nn <- tempfile(fileext = ".json"); writeLines(sprintf('{"sha256": "%s", "notes": 5}', strrep("c", 64)), nn)
  stopifnot(fails(check_cohort_rule(nn, "none", strrep("c", 64), strrep("c", 64)), "no `notes` string"))
  an <- tempfile(fileext = ".json"); writeLines(sprintf('{"sha256": "%s", "notes": "xparticipation_rule=none"}', strrep("c", 64)), an)
  stopifnot(fails(check_cohort_rule(an, "none", strrep("c", 64), strrep("c", 64)), "exactly one participation_rule"))
})
if (standalone) cat(sprintf("ALL %d PRESIGN TESTS PASSED\n", .t_total))
