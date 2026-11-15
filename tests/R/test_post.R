# S19 post-estimator tests: fixture-only (tmp dirs, synthetic data; nothing under out/ is read; no network). Runnable standalone:
#   Rscript tests/R/test_post.R
if (!exists("classify_ci")) {
  a <- grep("^--file=", commandArgs(FALSE), value = TRUE)
  setwd(normalizePath(file.path(dirname(sub("^--file=", "", a[1])), "..", "..")))
  for (f in c("utils", "decide", "cem", "estimate", "validate", "synth", "pre", "blacklist")) source(file.path("R", paste0(f, ".R")))
}
for (f in c("presign", "drdid", "post", "post_gate", "later_freeze")) if (!exists(c(presign = "run_presign", drdid = "drdid_estimate", post = "run_post", post_gate = "pg_fail", later_freeze = "lf_later_freeze")[[f]])) source(file.path("R", paste0(f, ".R")))
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

local({
# ---- fixture: a pre population, a real S17 presign run on it, and a synthetic post panel with a known effect -------------
tmpd <- tempfile("post"); dir.create(tmpd)
inp_dir <- file.path(tmpd, "in"); dir.create(inp_dir)
set.seed(11)
N <- 1500L; ids <- 0:(N - 1L)
arm <- sample(c(rep("control", 1200L), rep(c("withdrawal", "deposit", "both"), 100L)))
addr <- sprintf("0x%040x", ids * 7919L + 1L)
base <- exp(rnorm(N, 8, 0.8))
usd_frac <- ifelse(ids %% 5L == 0L, 1, ifelse(ids %% 7L == 0L, 0, runif(N)))
g <- CJ(wallet_id = ids, coin = c("USDC", "USDT"), day_index = 0:59)
g[, date := format(PRE_PERIOD_START + day_index)]
g[, share := usd_frac[wallet_id + 1L]]
g[, balance_units := round(base[wallet_id + 1L] * runif(.N, .9, 1.1) * 1e6 * ifelse(coin == "USDC", share, 1 - share) * 2)]
g[, share := NULL]
cov <- data.table(wallet_id = ids, address = addr, arm = arm, transfer_count_total = rpois(N, 8),
  eth_balance_wei_units = format(1e18 * rexp(N), scientific = FALSE), wallet_age_blocks = ifelse(runif(N) < .1, NA, 1.2e7 - rexp(N, 1e-6)),
  issuer_mint_redeem_flag = ifelse(ids %% 53L == 1L, "True", "False"), blacklisted_usdc_at_pre_end = "False", blacklisted_usdt_at_pre_end = "False")
fwrite(g, file.path(inp_dir, "wallet_coin_day_pre.csv")); fwrite(cov, file.path(inp_dir, "covariates.csv"))
dfz <- addr[101:138]; dff <- file.path(tmpd, "freeze.csv"); writeLines(c("address", dfz), dff)
man <- list(population_id = "main", generation_id = "gen-test-1", cutoff_block = 15298282L, baseline_start_date = "2022-06-09", baseline_end_date = "2022-08-07", excluded_dates = "2022-08-08", input_file_sha256 = list(`g1/cohort.csv` = strrep("c", 64)))
cands <- data.table(address = addr, source_membership = "g4")
ow <- data.table(start_block = PRE_START_BLOCK + 1000L * (0:29), end_block = PRE_START_BLOCK + 1000L * (0:29) + 399L)
pre_out <- file.path(tmpd, "presign")
pr <- run_presign(inp_dir, man, "main", pre_out, character(0), cands, ow, transfer_participation_rule = "UNRESOLVED-recorded", direct_freeze_file = dff,
                  cohort_schema_file = { f <- tempfile(fileext = ".json"); writeLines(paste0('{"sha256": "', strrep("c", 64), '", "notes": "participation_rule=UNRESOLVED-recorded"}'), f); f },
                  side_cohort_sha256 = strrep("c", 64))
mem_path <- file.path(pre_out, "match_membership.csv"); par_path <- file.path(pre_out, "analysis_parameters.json"); cov_path <- file.path(pre_out, "eligible_frame_coverage.csv")
cvg <- fread(cov_path); retained <- cvg[screen_status == "retained", address]

# post panel for the full retained frame: exposed USDC x0.6, USDT x1.4 (true beta_D about -0.4), others unchanged
mb <- g[, .(m = mean(balance_units)), by = .(wallet_id, coin)]
mk_post <- function(addrs, exposed_mult = c(USDC = .6, USDT = 1.4), seed = 3) {
  set.seed(seed)
  p <- CJ(wallet_id = ids[match(addrs, addr)], coin = c("USDC", "USDT"), day_index = 0:34)
  p <- merge(p, mb, by = c("wallet_id", "coin")); p[, address := addr[wallet_id + 1L]]
  ex <- arm[match(p$address, addr)] != "control"
  p[, balance_units := sprintf("%.0f", round(m * runif(.N, .9, 1.1) * ifelse(ex, exposed_mult[coin], 1)))]
  p[, `:=`(date = format(as.Date("2022-08-09") + day_index), arm = "g4", nonzero = balance_units != "0", population_id = "main", generation_id = "gen-test-1",
           source_file_sha256 = strrep("b", 64), source_range_id = "15298283-15500000/bridge_end=15299000", source_receipt_sha256 = strrep("c", 64))]
  p[, m := NULL]; setcolorder(p, POST_PANEL_COLS); p[]
}
post_dt <- mk_post(retained)
post_path <- file.path(tmpd, "post_wallet_coin_day.csv"); fwrite(post_dt, post_path)
source(file.path("tests", "R", "lf_fixtures.R"), local = TRUE)
# §5.9: g0 lifetime freeze files + a verified g5_veto output (two retained wallets frozen in-window: one Circle, one Tether)
lf_ev <- data.table(addr = retained[c(3, 4)], block = c(15400000L, 15401000L), unit = c("circle_blacklisted", "tether_added_blacklist"))
lf_g0 <- mk_g0(file.path(tmpd, "lf_g0"), lf_ev[, .(addr, block, token = ifelse(unit == "circle_blacklisted", "circle", "tether"))])
lf_vres <- lfx_verify(mk_veto(file.path(tmpd, "lf_veto"), lf_ev))
lfi <- list(circle_events = lf_g0$circle, usdt_events = lf_g0$usdt, g0_receipts = lf_g0$receipts, post_end_block = LFX_POST_END, veto = lf_vres)
ex_ret <- retained[arm[match(retained, addr)] != "control"]
flag_path <- file.path(tmpd, "pool_flag.csv"); fwrite(data.table(address = ex_ret, usdc_pool_depositor = rep(c("TRUE", "FALSE"), length.out = length(ex_ret))), flag_path)
mm <- fread(mem_path); mex <- mm[matched == TRUE & arm == "exposed", address]
dep_path <- file.path(tmpd, "deposits.csv"); fwrite(data.table(address = mm[matched == TRUE, address], deposit_count = rep(1:9, length.out = sum(mm$matched))), dep_path)

run <- function(out, ...) run_post(inp_dir, post_path, mem_path, par_path, out, ...)
out_a <- file.path(tmpd, "out_a")
ra <- run(out_a, eligible_frame_coverage_path = cov_path, later_freeze_inputs = lfi, usdc_pool_flag_path = flag_path, deposit_counts_path = dep_path)
out_b <- file.path(tmpd, "out_b")
rb <- run(out_b)                                              # no optional manifests

test("all result tables written with population/generation/trace identity; trace file hashes every output", {
  fs <- c("post_estimates.csv", "post_decisions.csv", "post_cluster_sizes.csv", "post_weight_concentration.csv", "post_descriptive_legs.csv", "post_cap_clip.csv",
          "post_pretrend.csv", "post_time_path.csv", "post_short_window.csv", "post_rematches.csv", "post_h2.csv", "post_outflow.csv", "post_drdid.csv",
          "post_reporting_rows.csv", "post_analysis_parameters.json", "post_trace.json")
  stopifnot(all(file.exists(file.path(out_a, fs))))
  e <- fread(file.path(out_a, "post_estimates.csv"))
  tj <- jsonlite::fromJSON(file.path(out_a, "post_trace.json"))
  stopifnot(all(e$population_id == "main"), all(e$generation_id == "gen-test-1"), all(e$input_trace_hash == ra$trace_hash), tj$input_trace_hash == ra$trace_hash,
            nchar(tj$input_sha256$post_panel) == 64, tj$input_sha256$match_membership == sha256_file(mem_path), tj$input_sha256$analysis_parameters == sha256_file(par_path))
  for (f in setdiff(fs, "post_trace.json")) stopifnot(tj$output_sha256[[f]] == sha256_file(file.path(out_a, f)))
  pj <- jsonlite::fromJSON(file.path(out_a, "post_analysis_parameters.json"))
  stopifnot(pj$drdid$function_name == "drdid_imp_panel", pj$drdid$version == "1.4.0", pj$drdid$trim_level == 0.99, pj$transfer_participation_rule == "UNRESOLVED-recorded",
            pj$post_panel_provenance$source_receipt_sha256 == strrep("c", 64), isTRUE(pj$primary_match_rederived_from_frame))
  # run B (no optional manifests) has a different trace hash because inputs differ
  stopifnot(rb$trace_hash != ra$trace_hash)
})
test("population and weight provenance: matched sample = frozen membership; clusters/counts/weights reproduce it", {
  m <- mm[matched == TRUE]
  cl <- ra$cluster
  stopifnot(cl[cluster == "uid", n_clusters] == nrow(m), cl[cluster == "stratum", n_clusters] == uniqueN(m$stratum), cl[cluster == "uid", n_obs] == nrow(m) * 95L,
            cl[cluster == "uid", n_exposed_wallets] == sum(m$arm == "exposed"), cl[cluster == "uid", obs_per_cluster_min] == 95L,
            cl[cluster == "stratum", clusters_with_both_arms] == uniqueN(m$stratum))
  wc <- ra$weights
  ex <- wc[arm == "exposed" & scope == "arm"]; ct <- wc[arm == "control" & scope == "arm"]
  stopifnot(ex$sum_w == sum(m$arm == "exposed"), abs(ct$sum_w - sum(m[arm == "control", weight])) < 1e-9, abs(ct$effective_n - sum(m[arm == "control", weight])^2 / sum(m[arm == "control", weight]^2)) < 1e-9,
            ct$max_w == max(m[arm == "control", weight]), abs(sum(ra$panel[day_index == 0, w]) - sum(m$weight)) < 1e-9, abs(ct$sum_w - sum(m$arm == "control")) < 1e-6)   # IKP: control weights sum to m_C
  stopifnot(setequal(unique(ra$panel$uid), m$address), ra$primary_rederived)
})
test("H1 pooled 35-day estimate: capped D equals an independent cell-mean DiD (1e-10) and recovers the planted effect", {
  b <- ra$est[outcome == "D" & cap == "own" & cluster == "uid", beta]
  stopifnot(abs(b - cellmean_did(ra$panel, POST_EVENT_DAY, "D", "own")) < 1e-10, b < -0.25, b > -0.55,
            ra$est[outcome == "D" & cap == "own" & cluster == "uid", n_obs] == nrow(mm[matched == TRUE]) * 95L,
            all(ra$est$status == "estimated"), nrow(ra$est) == 16L)
  stopifnot(ra$est[outcome == "D" & cap == "own" & cluster == "stratum", se] > 0, ra$est[outcome == "D" & cap == "own" & cluster == "uid", se] > 0)
  stopifnot(ra$decision$capped_case_uid == 1L, ra$decision$capped_case_stratum == 1L, ra$decision$h1_status %in% c("supported", "cap_sensitive"))
})
test("caps are the frozen S17 values (cross-checked), same for every day; tampered caps are a hard stop", {
  pj <- jsonlite::fromJSON(par_path)
  stopifnot(isTRUE(all.equal(ra$caps$D, pj$cap_values$D)), isTRUE(all.equal(ra$caps$A, pj$cap_values$A)))
  bad <- jsonlite::fromJSON(par_path, simplifyVector = FALSE); bad$cap_values$D[[1]] <- bad$cap_values$D[[1]] - 0.5
  bp <- file.path(tmpd, "bad_params.json"); jsonlite::write_json(bad, bp, auto_unbox = TRUE, digits = NA)
  stopifnot(fails(run_post(inp_dir, post_path, mem_path, bp, file.path(tmpd, "o_bad")), "differ from the frozen"), !dir.exists(file.path(tmpd, "o_bad")))
})
test("decision boundaries: H1 cases, both-clustering conjunction, uncapped veto, dependence sensitivity, missing => not estimable", {
  mk <- function(cap_w, cap_s, unc_w, unc_s, a = list(cap = c(-.01, .01), unc = c(-.02, .02))) {
    r <- function(o, cp, cl, i) data.table(outcome = o, cap = cp, cluster = cl, status = if (is.null(i)) "not_estimable" else "estimated", lo = if (is.null(i)) NA_real_ else i[1], hi = if (is.null(i)) NA_real_ else i[2])
    rbind(r("D", "own", "uid", cap_w), r("D", "own", "stratum", cap_s), r("D", "none", "uid", unc_w), r("D", "none", "stratum", unc_s),
          r("A", "own", "uid", a$cap), r("A", "own", "stratum", a$cap), r("A", "none", "uid", a$unc), r("A", "none", "stratum", a$unc))
  }
  s <- c(-.30, -.12); h <- c(-.30, -.10)                     # hi == -delta exactly is NOT case 1 (case 2: contains -delta)
  stopifnot(po_decide(mk(s, s, s, s))$h1_status == "supported", po_decide(mk(h, s, s, s))$h1_status == "dependence_sensitive",
            po_decide(mk(h, h, s, s))$h1_status == "not_supported", po_decide(mk(h, h, s, s))$capped_case_uid == 2L,
            po_decide(mk(s, s, c(-.09, -.05), s))$h1_status == "cap_sensitive", po_decide(mk(s, s, s, c(-.09, -.05)))$h1_status == "cap_sensitive",
            po_decide(mk(s, s, c(-.3, -.05), s))$h1_status == "supported",    # uncapped case 2 (contains -delta) does not veto; only cases 3-5 do
            po_decide(mk(s, s, NULL, s))$h1_status == "not_estimable", po_decide(mk(NULL, s, s, s))$h1_status == "not_estimable")
  stopifnot(po_decide(mk(c(-.30, -.1000001), s, s, s))$capped_case_uid == 1L, po_decide(mk(c(-.1, .1), s, s, s))$capped_case_uid == 2L, po_decide(mk(c(-.0999999, .1), s, s, s))$capped_case_uid == 3L,
            po_decide(mk(c(-.0999999, .1000001), s, s, s))$capped_case_uid == 5L, po_decide(mk(c(.1000001, .3), s, s, s))$capped_case_uid == 4L)
  d <- po_decide(mk(s, s, s, s, a = list(cap = c(-.05, .05), unc = c(-.06, .06))), cap_sensitive_clip = FALSE)
  stopifnot(d$h1a_combined == "negligible", isTRUE(d$reallocation_wording_allowed))
  stopifnot(isFALSE(po_decide(mk(s, s, s, s, a = list(cap = c(-.05, .05), unc = c(-.06, .06))), cap_sensitive_clip = TRUE)$reallocation_wording_allowed))
  stopifnot(po_decide(mk(s, s, s, s, a = list(cap = c(-.2, -.0501), unc = c(-.3, -.1))))$h1a_combined == "decline",
            po_decide(mk(s, s, s, s, a = list(cap = c(-.2, -.05), unc = c(-.3, -.1))))$h1a_combined == "inconclusive",
            po_decide(mk(s, s, s, s, a = list(cap = c(.0501, .2), unc = c(.06, .1))))$h1a_combined == "increase",
            po_decide(mk(s, s, s, s, a = list(cap = NULL, unc = c(-.3, -.1))))$h1a_combined == "not_estimable")
  d2 <- po_decide(rbind(mk(s, s, s, s)[outcome == "D"], data.table(outcome = "A", cap = c("own", "own"), cluster = c("uid", "stratum"), status = "estimated", lo = c(-.01, -.2), hi = c(.01, -.1)),
                        data.table(outcome = "A", cap = c("none", "none"), cluster = c("uid", "stratum"), status = "estimated", lo = c(-.01, -.2), hi = c(.01, -.1))))
  stopifnot(d2$h1a_combined == "dependence_sensitive")
})
test("uncapped veto / clip flag: the clip flag is computed from the weighted incidence by arm, tail and period (|DiD| > 1 pp, either direction)", {
  cf <- ra$clip; stopifnot(nrow(cf$table) == 4L, is.logical(cf$cap_sensitive_clip))
  ci <- clip_incidence(ra$panel, POST_EVENT_DAY); stopifnot(nrow(ci) == 16L, all(ci$share >= 0 & ci$share <= 1))
  t0 <- copy(ci); t0[, share := 0.02]; stopifnot(!clip_flag(t0)$cap_sensitive_clip)
  t1 <- copy(t0); t1[outcome == "D" & tail == "lower" & arm == "exposed" & period == "post", share := 0.0301]; stopifnot(clip_flag(t1)$cap_sensitive_clip)
  t2 <- copy(t0); t2[outcome == "A" & tail == "upper" & arm == "control" & period == "post", share := 0.0301]; stopifnot(clip_flag(t2)$cap_sensitive_clip)
  t3 <- copy(t0); t3[outcome == "D" & tail == "lower" & arm == "exposed" & period == "post", share := 0.03]; stopifnot(!clip_flag(t3)$cap_sensitive_clip)  # exactly 1 pp does not exceed
  cc <- fread(file.path(out_a, "post_cap_clip.csv")); stopifnot(all(c("caps", "clip_incidence", "clip_did") %in% cc$table), nrow(cc[table == "clip_incidence"]) == 16L)
})
test("descriptive uncapped legs: separate from capped estimands; beta_USDC = (bA+bD)/2 and beta_USDT = (bA-bD)/2 hold exactly when uncapped", {
  stopifnot(all(abs(unlist(ra$legs_identity[, .(identity_resid_usdc, identity_resid_usdt)])) < 1e-9))
  stopifnot(all(ra$est[outcome %in% c("usdc_B", "usdt_B"), cap] == "none"), nrow(ra$legs[outcome == "usdc_B"]) == 4L)
  capd <- ra$est[outcome == "D" & cap == "own" & cluster == "uid", beta]; uncd <- ra$est[outcome == "D" & cap == "none" & cluster == "uid", beta]
  usdc <- ra$est[outcome == "usdc_B" & cluster == "uid", beta]; usdt <- ra$est[outcome == "usdt_B" & cluster == "uid", beta]
  stopifnot(abs((usdc - usdt) - uncd) < 1e-9, capd != uncd)
})
test("pretrend covers all 60 baseline days; time path covers all 35 post days relative to 7 Aug; 8 Aug never appears", {
  pt <- ra$pretrend[outcome == "D"]; tp <- ra$time_path[outcome == "D"]
  stopifnot(nrow(pt) == 60L, setequal(pt$day_index, 0:59), pt[day_index == 59, status] == "reference", sum(pt$status == "estimated") == 59L,
            pt[day_index == 21, note] == "30 Jun 2022: Tether froze 46 addresses in one day", pt[day_index == 0, date] == "2022-06-09",
            nrow(tp) == 36L, setequal(tp$day_index, c(59L, 61:95)), !(60L %in% tp$day_index), tp[day_index == 61, date] == "2022-08-09", tp[day_index == 95, date] == "2022-09-12",
            sum(tp$status == "estimated") == 35L)
  stopifnot(mean(tp[day_index >= 61 & status == "estimated", coef]) < -0.25, abs(mean(pt[status == "estimated", coef])) < 0.1)
})
test("short window: through 4 Sep inclusive (27 post days), same population / weights / baseline B / caps", {
  s <- ra$short
  stopifnot(s$last_day_index == 87L, s$post_days == 27L, nrow(s$est) == 8L, !is.na(s$binance_sensitive), grepl("differs", s$definition))
  stopifnot(all(abs(s$est[outcome == "D" & cap == "own", beta] - ra$est[outcome == "D" & cap == "own" & cluster %in% c("uid", "stratum"), beta][c(1, 2)]) < 0.1))
  # direct recomputation on the truncated panel
  p2 <- ra$panel[day_index <= 87]; setattr(p2, "baseline_days", 0:59)
  stopifnot(abs(estimate_did(p2, 60L, "D", "uid", "own")$beta - s$est[outcome == "D" & cap == "own" & cluster == "uid", beta]) < 1e-12,
            isTRUE(all.equal(attr(prep_est(p2, 60L, "own"), "caps")$D, ra$caps$D)))
})
test("rematch machinery: cem_match_fixed with the primary cut-points reproduces cem_match exactly", {
  inp <- load_pre_inputs(inp_dir); wp <- po_pre_wide(inp); fr <- po_frame(inp, wp, retained)
  r1 <- cem_match(fr[, .(uid, treat, address, mean_hold, transfers, age, eth, usdc_share)], CEM_VARS, "three_bins", seed = SEED_PRIMARY_CEM)
  pc <- po_primary_cuts(fr)
  stopifnot(identical(pc$hash, jsonlite::fromJSON(par_path)$cutpoints_hash), identical(pc$cuts, r1$cutpoints[CEM_VARS]))   # exact cuts reproduce the frozen hash
  r2 <- cem_match_fixed(fr, CEM_VARS, r1$cutpoints[CEM_VARS], "three_bins")
  stopifnot(identical(r1$matched, r2$matched), r1$dropped_exposed == r2$dropped_exposed)
  r3 <- cem_match(fr[, .(uid, treat, address, mean_hold, transfers, age, eth, usdc_share)], CEM_VARS, "none", seed = SEED_PRIMARY_CEM)
  stopifnot(identical(r3$matched, cem_match_fixed(fr, CEM_VARS, r3$cutpoints[CEM_VARS], "none")$matched))
  q <- unique(quantile(fr[treat == TRUE, usdc_share], c(.25, .5, .75), type = 7, names = FALSE))
  r4 <- cem_match(fr[, .(uid, treat, address, mean_hold, transfers, age, eth, usdc_share)], CEM_VARS, "continuous", seed = SEED_PRIMARY_CEM)
  stopifnot(identical(r4$matched, cem_match_fixed(fr, CEM_VARS, r4$cutpoints[CEM_VARS], "continuous", q)$matched))
  # frozen membership is the same match
  stopifnot(setequal(r1$matched$uid, mm[matched == TRUE, address]))
})
test("all estimable rematches and subgroups run with primary cut-points; absent manifests are explicit 'not estimable'", {
  r <- ra$rematches
  stopifnot(setequal(r$row, names(PO_REMATCH_REFS)), all(r$status == "estimated"), all(r$n_exposed_retained > 0),
            r[row == "no_usdc_share", usdc_mode] == "none", r[row == "quartile_share", usdc_mode] == "continuous",
            r[row == "drop_later_freeze", n_exposed_eligible] <= sum(arm[match(retained, addr)] != "control"),
            all(r[row %in% c("usdc_pool_flagged", "usdc_pool_unflagged"), n_exposed_eligible] < r[row == "no_usdc_share", n_exposed_eligible]),
            all(!is.na(r$capped_case_uid)))
  r0 <- rb$rematches
  stopifnot(all(r0$status == "not estimable"), all(grepl("eligible_frame_coverage not supplied", r0$reason)))
  # partial flag manifest => both pool rows explicit not estimable
  pf <- fread(flag_path)[-1]
  inp <- load_pre_inputs(inp_dir); wp <- po_pre_wide(inp); fr <- po_frame(inp, wp, retained)
  wall <- rbind(wp[uid %in% retained], po_read_post_panel(post_path, jsonlite::fromJSON(par_path), retained, retained)$wide)
  rp <- po_rematches(fr, po_primary_cuts(fr)$cuts, wall, ra$est, NULL, pf, TRUE)
  stopifnot(rp[row == "usdc_pool_flagged", status] == "not estimable", grepl("never 'unflagged'", rp[row == "usdc_pool_flagged", reason]), rp[row == "drop_later_freeze", status] == "not estimable")
})
test("DRDID rows: frozen function on the pre-match eligible frame, both outcomes; no coverage => 'not estimated' with reason", {
  d <- ra$drdid
  stopifnot(nrow(d) == 2L, all(d$package == "DRDID"), all(d$fn == "drdid_imp_panel"), all(d$trim_level == 0.99), all(d$version == "1.4.0"),
            all(d$status %in% c("estimated", "not estimated")), all(d$n_exposed + d$n_control == length(retained) - d$n_dropped_B_le_0))
  stopifnot(all(rb$drdid$status == "not estimated"), all(grepl("eligible_frame_coverage not supplied", rb$drdid$reason)))
  if (d[row == "capped_D", status] == "estimated") stopifnot(d[row == "capped_D", att] < -0.2)
})
test("H2 terciles are exploratory: type-7 cuts among matched exposed, ties not split, empty groups reported; absent manifest not estimable", {
  h <- ra$h2$table
  stopifnot(all(h$exploratory == "pattern only; never determines H1"), setequal(h$group, c("low", "middle", "high")))
  dc <- fread(dep_path)[address %in% mex]; q <- unname(quantile(dc$deposit_count, c(1/3, 2/3), type = 7))
  stopifnot(isTRUE(all.equal(ra$h2$cuts, q)), sum(unique(h[, .(group, n_exposed_wallets)])$n_exposed_wallets) == length(mex),
            unique(h[group == "low", n_exposed_wallets]) == sum(dc$deposit_count <= q[1]), unique(h[group == "high", n_exposed_wallets]) == sum(dc$deposit_count > q[2]))
  stopifnot(all(rb$h2$table$status == "not estimable"), grepl("not supplied", rb$h2$table$reason[1]))
  same <- data.table(address = ra$panel[treat == TRUE, unique(uid)], deposit_count = 4L)        # all tied: nothing is split
  z <- po_h2(ra$panel, same)$table; stopifnot(z[group == "low", unique(n_exposed_wallets)] == length(mex), z[group == "middle", status[1]] == "empty group", z[group == "high", status[1]] == "empty group")
  stopifnot(po_h2(ra$panel, same[-1])$table$status[1] == "not estimable")                       # incomplete coverage is not zero
})
test("conditional outflow breakdown: only on H1a decline; only verified evidence; CEX unavailable; absent components not estimable, never invented", {
  stopifnot(all(ra$outflow[component %in% OUTFLOW_COMPONENTS, status] == c("not applicable", "not applicable", "not applicable")) || ra$decision$h1a_combined == "decline",
            ra$outflow[component == "cex_deposits", status] == "unavailable")
  stopifnot(all(po_outflow(ra$panel, "negligible", NULL)[component %in% OUTFLOW_COMPONENTS, status] == "not applicable"))
  stopifnot(all(po_outflow(ra$panel, "decline", NULL)[component %in% OUTFLOW_COMPONENTS, status] == "not estimable"), po_outflow(ra$panel, "decline", NULL)[component == "cex_deposits", status] == "unavailable")
  w <- unique(ra$panel[, .(uid, treat, w, B)])
  ev <- rbind(data.table(address = w$uid, component = "eth_dex", amount_units = sprintf("%.0f", w$B * 1e6 * ifelse(w$treat, 0.2, 0.05)), evidence_manifest_sha256 = strrep("d", 64)),
              data.table(address = w$uid[-1], component = "dai", amount_units = "5", evidence_manifest_sha256 = strrep("d", 64)))
  o <- po_outflow(ra$panel, "decline", ev)
  stopifnot(o[component == "eth_dex", status] == "estimated", abs(o[component == "eth_dex", exposed_weighted_mean_scaled] - 0.2) < 1e-6, abs(o[component == "eth_dex", control_weighted_mean_scaled] - 0.05) < 1e-6,
            abs(o[component == "eth_dex", difference] - 0.15) < 1e-6, o[component == "dai", status] == "not estimable", grepl("absence is not zero", o[component == "dai", reason]),
            o[component == "new_address", status] == "not estimable", grepl("absent from the verified", o[component == "new_address", reason]), o[component == "cex_deposits", status] == "unavailable")
  bad <- copy(ev); bad[1, evidence_manifest_sha256 := "x"]; stopifnot(po_outflow(ra$panel, "decline", bad)[component == "eth_dex", status] == "not estimable")
})
test("every prespec row is reported with a status; unavailable / unowned rows are explicit", {
  r <- ra$report
  stopifnot(!anyDuplicated(r$row_id), all(nzchar(r$status)),
            all(c("h1_pooled_35d", "h1a", "cluster_sizes", "cem_weight_concentration", "descriptive_uncapped_legs", "cap_clip_diagnostics", "h2_terciles_intensity", "outflow_breakdown",
                  "outflow_cex", "post_time_path", "pretrend_60_days", "placebo_cohort", "binance_shortened_window", "binance_flags_and_outflow_shares", "drdid_capped_D", "drdid_uncapped_D",
                  "no_usdc_share", "quartile_share", "deposit_address_reuse", "drop_later_freeze", "usdc_pool_flagged", "usdc_pool_unflagged", "uncapped_and_leg_cap", "owner_grouped_inference") %in% r$row_id),
            r[row_id == "placebo_cohort", status] == "not estimable", r[row_id == "outflow_cex", status] == "unavailable", r[row_id == "deposit_address_reuse", status] == "not estimable",
            all(nzchar(r[status %in% c("not estimable", "unavailable", "not estimated"), reason]) & !is.na(r[status %in% c("not estimable", "unavailable", "not estimated"), reason])))
  rb2 <- rb$report; stopifnot(all(rb2[row_id %in% c("no_usdc_share", "quartile_share", "drop_later_freeze", "usdc_pool_flagged"), status] == "not estimable"), rb2[row_id == "drdid_capped_D", status] == "not estimated")
  stopifnot(identical(fread(file.path(out_a, "post_reporting_rows.csv"))$row_id, r$row_id))
})
test("fix round 1: clip_flag is its own decision field; leg-pooled/uncapped rows carry decisions and differs_from_primary_case; NA never TRUE", {
  dd <- fread(file.path(out_a, "post_decisions.csv"))
  stopifnot("clip_flag" %in% dd$item, "h1_veto" %in% dd$item, dd[item == "clip_flag", value] == as.character(ra$clip$cap_sensitive_clip),
            grepl("clip_flag=", ra$report[row_id == "h1_pooled_35d", reason]))
  stopifnot(identical(po_decide(ra$est, TRUE)$clip_flag, TRUE), identical(po_decide(ra$est, FALSE)$clip_flag, FALSE))
  rob <- fread(file.path(out_a, "post_cap_robustness.csv"))
  stopifnot(nrow(rob) == 8L, setequal(rob$cap, c("none", "leg_pooled")), setequal(rob$outcome, c("D", "A")), all(c("decision", "primary_decision", "differs_from_primary_case") %in% names(rob)),
            all(!is.na(rob$decision)), all(rob[outcome == "D", decision] %in% as.character(1:5)), all(rob[outcome == "A", decision] %in% c("negligible", "decline", "increase", "inconclusive")))
  stopifnot(identical(po_differs(c(1L, 1L), c(1L, 1L)), FALSE), identical(po_differs(c(1L, 2L), c(1L, 1L)), TRUE), is.na(po_differs(c(NA, 1L), c(1L, 1L))), is.na(po_differs(c(1L, 1L), c(1L, NA))))
  e <- copy(ra$est); e[outcome == "D" & cap == "leg_pooled" & cluster == "uid", `:=`(status = "not_estimable", lo = NA_real_, hi = NA_real_)]
  r2 <- po_cap_robustness(e); stopifnot(is.na(r2[outcome == "D" & cap == "leg_pooled" & cluster == "uid", differs_from_primary_case]))
  e2 <- copy(ra$est); e2[outcome == "D" & cap == "leg_pooled" & cluster == "uid", `:=`(lo = -.05, hi = .02)]
  stopifnot(po_cap_robustness(e2)[outcome == "D" & cap == "leg_pooled" & cluster == "uid", differs_from_primary_case])
  pe <- copy(ra$est); pe[outcome == "D" & cap == "own" & cluster == "uid", `:=`(status = "not_estimable", lo = NA_real_, hi = NA_real_)]
  stopifnot(is.na(po_differs(c(NA, 1L), c(1L, 1L))), !isTRUE(po_cap_robustness(pe)[outcome == "D" & cap == "none" & cluster == "uid", differs_from_primary_case]))
})
test("fix round 1: later-freeze rematch reports per-arm dropped counts; absent list names S22; eligibility read from screen_reason like presign", {
  r <- ra$rematches[row == "drop_later_freeze"]
  stopifnot(r$n_later_freeze_dropped_exposed + r$n_later_freeze_dropped_control == 2L)
  inp <- load_pre_inputs(inp_dir); wp <- po_pre_wide(inp); fr <- po_frame(inp, wp, retained)
  wall <- rbind(wp[uid %in% retained], po_read_post_panel(post_path, jsonlite::fromJSON(par_path), retained, retained)$wide)
  rl <- po_rematches(fr, po_primary_cuts(fr)$cuts, wall, ra$est, NULL, NULL, TRUE)
  stopifnot(grepl("not supplied", rl[row == "drop_later_freeze", reason]), grepl("absence is not 'none'", rl[row == "drop_later_freeze", reason]))
  bv <- lf_later_freeze(modifyList(lfi, list(veto = list(ok = FALSE, reason = "tampered receipts"))), fr$address)
  rv <- po_rematches(fr, po_primary_cuts(fr)$cuts, wall, ra$est, bv, NULL, TRUE)
  stopifnot(rv[row == "drop_later_freeze", status] == "not estimable", grepl("tampered receipts", rv[row == "drop_later_freeze", reason]))   # row-only; nothing else changes
  stopifnot(file.exists(file.path(out_a, "post_later_freeze.csv")), setequal(fread(file.path(out_a, "post_later_freeze.csv"))[disposition == "dropped", address], retained[c(3, 4)]))
  stopifnot(!file.exists(file.path(out_b, "post_later_freeze.csv")))
  tr <- jsonlite::fromJSON(file.path(out_a, "post_trace.json"))$input_sha256               # S59 fix round: the trace binds the g0 receipts, not NA
  stopifnot(identical(tr$g0_receipts, sha256_file(lf_g0$receipts)), identical(tr$g0_circle_events, sha256_file(lf_g0$circle)), identical(tr$g0_usdt_events, sha256_file(lf_g0$usdt)),
            identical(jsonlite::fromJSON(file.path(out_b, "post_trace.json"))$input_sha256$g0_receipts, "NA"))
  stopifnot(fails(run_post(inp_dir, post_path, mem_path, par_path, file.path(tmpd, "o_nocov"), later_freeze_inputs = lfi), "later_freeze_inputs.*eligible_frame_coverage"), !dir.exists(file.path(tmpd, "o_nocov")))   # S59 LOW-2: g0 hashes traced but no frame => fail closed
  lf_vx <- lfx_verify(mk_veto(file.path(tmpd, "lf_veto_x"), rbind(lf_ev, data.table(addr = retained[5], block = 15402000L, unit = "circle_blacklisted"))))   # S59 F1: a veto-only wallet => row not estimable, reported in the csv
  rx <- run_post(inp_dir, post_path, mem_path, par_path, file.path(tmpd, "o_vx"), eligible_frame_coverage_path = cov_path, later_freeze_inputs = { l2 <- lfi; l2$veto <- lf_vx; l2 })
  cx <- fread(file.path(tmpd, "o_vx", "post_later_freeze.csv"))
  stopifnot(rx$rematches[row == "drop_later_freeze", status] == "not estimable", identical(cx[disposition == "veto_only_discrepancy_not_added", address], retained[5]), !"dropped" %in% cx$disposition)
  stopifnot(!grepl("before any data load", tryCatch(pg_fail("x"), error = function(e) conditionMessage(e))))        # message accuracy: the g0 hard fail is raised inside run_post
  bad <- copy(cvg); bad[screen_status == "retained"][1, screen_status := "excluded"]; bad[which(cvg$screen_status == "retained")[1], screen_status := "excluded"]
  bp <- file.path(tmpd, "cov_bad.csv"); fwrite(bad, bp)
  stopifnot(fails(run_post(inp_dir, post_path, mem_path, par_path, file.path(tmpd, "o_cb"), eligible_frame_coverage_path = bp), "disagrees with screen_reason"))
})
test("fix round 1: freeze metadata states trimming precisely and discloses every reporting convention", {
  fp <- file.path(tmpd, "freeze2.json"); write_post_freeze(fp); j <- jsonlite::fromJSON(fp)
  stopifnot(grepl("ONLY controls", j$drdid$trimming), grepl("REMAIN in the IPT propensity fit", j$drdid$trimming), grepl("pscore/\\(1-pscore\\)", j$drdid$trimming), grepl("never", j$drdid$trimming),
            all(c("h1a_combination", "reallocation_conjunction", "outflow_trigger", "outflow_metric", "caps", "event_study_inference", "binance_scope", "later_freeze") %in% names(j$spec$disclosures)),
            grepl("wallet-clustered only", j$spec$disclosures$event_study_inference), grepl("FROZEN main caps", j$spec$disclosures$caps))
  pj <- jsonlite::fromJSON(file.path(out_a, "post_analysis_parameters.json")); stopifnot(!is.null(pj$spec$disclosures$binance_scope))
})
test("failed rows are reported without changing models: a degenerate sample yields not_estimable rows, not a different estimator", {
  p <- copy(ra$panel); p[treat == TRUE, D := 0.1]; setattr(p, "baseline_days", 0:59)
  e <- po_fit_grid(p[0], data.table(outcome = "D", cap = "none", cluster = "uid"))
  stopifnot(e$status == "not_estimable", nzchar(e$reason), is.na(e$beta))
})
test("hard stops: missing / mixed-population / non-dense / 8-Aug / extra-address / wrong-window post panels; June and r_pre paths; immutability; explicit paths only", {
  chk <- function(dt, pat, nm) { f <- file.path(tmpd, paste0(nm, ".csv")); fwrite(dt, f)
    stopifnot(fails(run_post(inp_dir, f, mem_path, par_path, file.path(tmpd, paste0("o_", nm)), eligible_frame_coverage_path = cov_path), pat), !dir.exists(file.path(tmpd, paste0("o_", nm)))) }
  chk(post_dt[-1], "not dense", "dense")
  chk(copy(post_dt)[, population_id := "main2"], "population/generation differs", "pop")
  d8 <- copy(post_dt); d8[1, `:=`(date = "2022-08-08")]; chk(d8, "date does not equal", "d8")
  dw <- copy(post_dt); dw[1, `:=`(day_index = 40L, date = "2022-09-18")]; chk(dw, "outside 0..34", "win")
  chk(post_dt[address != retained[5]], "not dense|lacks", "miss")
  ex <- rbind(post_dt, copy(post_dt[address == retained[1]])[, address := sprintf("0x%040x", 123456789L)]); chk(ex, "differ from the retained eligible frame", "extra")
  chk(copy(post_dt)[1, balance_units := "1.5"] |> (\(x) rbind(x, post_dt[-1]))(), "decimal integer", "frac")
  chk(copy(post_dt)[, source_receipt_sha256 := ""], "provenance", "prov")
  stopifnot(fails(run_post(inp_dir, post_path, mem_path, par_path, file.path(tmpd, "out", "r_pre")), "r_pre"),
            fails(run_post(inp_dir, post_path, mem_path, par_path, out_a), "immutable"),
            fails(run_post(inp_dir, file.path(tmpd, "nope.csv"), mem_path, par_path, file.path(tmpd, "o_np")), "must exist"),
            fails(run_post(file.path(tmpd, "nodir"), post_path, mem_path, par_path, file.path(tmpd, "o_nd")), "required pre-event input missing"))
  jp <- jsonlite::fromJSON(par_path, simplifyVector = FALSE); jp$population_id <- "june_placebo"
  pj <- file.path(tmpd, "june.json"); jsonlite::write_json(jp, pj, auto_unbox = TRUE, digits = NA)
  stopifnot(fails(run_post(inp_dir, post_path, mem_path, pj, file.path(tmpd, "o_j")), "differs from analysis_parameters|June"))
  mm2 <- copy(mm); mm2[matched == TRUE & arm == "exposed", weight := 2]; mp <- file.path(tmpd, "mem_bad.csv"); fwrite(mm2, mp)
  stopifnot(fails(run_post(inp_dir, post_path, mp, par_path, file.path(tmpd, "o_m")), "weights violate"))
  for (nm in c("pre_dir", "post_panel_path", "match_membership_path", "analysis_parameters_path", "output_dir")) stopifnot(is.symbol(formals(run_post)[[nm]]))   # no defaults => no implicit live paths
})
test("pre-signing freeze: DRDID function/API/arguments/trimming + spec recorded without any data; immutable", {
  fp <- file.path(tmpd, "freeze.json"); h <- write_post_freeze(fp)
  j <- jsonlite::fromJSON(fp)
  stopifnot(nchar(h) == 64, j$drdid$package == "DRDID", j$drdid$function_name == "drdid_imp_panel", j$drdid$version == "1.4.0", j$drdid$installed_version == "1.4.0",
            j$drdid$arguments$trim.level == 0.99, grepl("0.99", j$drdid$trimming), j$spec$windows$short_window_last_date == "2022-09-04 inclusive (day_index 87)",
            grepl("1st/99th", j$spec$cap_rule), fails(write_post_freeze(fp), "immutable"))
})
test("no post data is read from out/: sources reference no live path and the pre loader keeps the hard date rule", {
  src <- paste(readLines(file.path("R", "post.R")), collapse = "\n")
  stopifnot(!grepl("file.path\\([^)]*\"out\"", src), !grepl("system2\\(\"(curl|wget)", src), !grepl("httr|curl::|url\\(", src))
  stopifnot(fails(assert_no_post_data(data.table(date = "2022-08-09"), "x"), "HARD RULE"))
})

})
if (standalone) cat(sprintf("ALL %d POST TESTS PASSED\n", .t_total))
