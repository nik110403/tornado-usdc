# Main-sample pre-signing stage (S17). ADDITIVE: reuses cem_match / build_panel / cap_values_all / power_normal /
# load_pre_inputs; replaces nothing in the June estimator (R/pre.R). Pre-event data only: reads exactly two files
# (covariates.csv, wallet_coin_day_pre.csv) from an EXPLICIT input directory, writes to an EXPLICIT new output
# directory. No default to out/r_pre or any live path; no RPC; no post-event file is ever opened.
#
# Produces (DATA-CONTRACT): match_membership, analysis_parameters, eligible_frame_coverage (+ aggregate funnel),
# original exposed quartiles, main cut-points / weights / caps, A1(a)/(b) descriptive diagnostics, cohort hashes, and the
# frozen one-time enlargement candidate-window request. The enlargement TRIGGER/ADOPTION logic is pure and takes the
# genuine June placebo result as input (S21 produces it); development output never activates a real branch.

MAIN_BASELINE_DAYS <- 0:as.integer(LAST_ALLOWED_DATE - PRE_PERIOD_START)   # 0..59 = 9 Jun .. 7 Aug
MAIN_FLOOR_MEAN_USD <- 100                      # prespec §1 filters (both groups)
MAIN_FLOOR_NZ_DAYS <- 14L
MAIN_EVENT_DAY <- 60L                           # 8 Aug, excluded; only the cap_values_all() fallback signature uses it
MAIN_RATIO <- 3L
A1_HALF_SPLIT <- list(first = 0:29, second = 30:59)   # 30 + 30 baseline days; descriptive, chosen here, no test
ENLARGE_N <- 30L
ENLARGE_WINDOW_BLOCKS <- 400L
PLACEBO_STATUSES <- c("genuine", "terminal_unavailable", "development", "unavailable_nonterminal")
CAP_RULE_TEXT <- "D and A each at own type-7 1st/99th percentile over all baseline wallet-days of the final matched sample, both groups pooled, unweighted"
DIRECT_FREEZE_N <- 38L                          # prespec §1: addresses Circle froze on 8 Aug 2022 (direct-freeze set)
REQUIRED_PRE_FILES <- c("covariates.csv", "wallet_coin_day_pre.csv")

ps_sha256_text <- function(lines) {
  f <- tempfile("ps_h"); on.exit(unlink(f))
  writeBin(charToRaw(paste0(paste(lines, collapse = "\n"), "\n")), f)
  sha256_file(f)
}
ps_hash_set <- function(x) ps_sha256_text(sort(unique(as.character(x)), method = "radix"))
ps_check_address <- function(a, what) {
  if (anyNA(a) || !all(grepl("^0x[0-9a-f]{40}$", a))) stop(sprintf("%s: address must be lowercase 0x + 40 hex", what), call. = FALSE)
  if (anyDuplicated(a)) stop(sprintf("%s: duplicate addresses", what), call. = FALSE)
  invisible(TRUE)
}

# ---- population manifest guard --------------------------------------------------------------------------
# population_id must equal the explicitly requested id; June-placebo / development labels are never a main population.
check_main_manifest <- function(man, population_id) {
  need <- c("population_id", "generation_id", "cutoff_block", "baseline_start_date", "baseline_end_date", "excluded_dates")
  miss <- setdiff(need, names(man))
  if (length(miss)) stop(sprintf("population manifest lacks: %s", paste(miss, collapse = ", ")), call. = FALSE)
  if (!is.character(population_id) || length(population_id) != 1 || !nzchar(population_id)) stop("population_id must be given explicitly", call. = FALSE)
  if (grepl("june|development|diagnostic|enlarg", man$population_id, ignore.case = TRUE) || !identical(man$population_id, population_id))
    stop(sprintf("population manifest '%s' is not the requested main population '%s' (June/development/enlarged populations never satisfy the main presign)",
                 man$population_id, population_id), call. = FALSE)
  if (!nzchar(man$generation_id)) stop("population manifest: empty generation_id", call. = FALSE)
  if (as.integer(man$cutoff_block) != PRE_PERIOD_END_BLOCK) stop("population manifest: cutoff_block != 15,298,282", call. = FALSE)
  if (as.character(man$baseline_start_date) != as.character(PRE_PERIOD_START) || as.character(man$baseline_end_date) != as.character(LAST_ALLOWED_DATE) ||
      !("2022-08-08" %in% as.character(man$excluded_dates)))
    stop("population manifest: baseline window must be 2022-06-09..2022-08-07 with 2022-08-08 excluded", call. = FALSE)
  invisible(TRUE)
}

# Direct-freeze set: caller-supplied text/csv file, one lowercase address per line (optional header "address").
# Exactly 38 unique addresses; returns list(addresses, sha256). Never read from out/ by default.
read_direct_freeze <- function(path) {
  if (!is.character(path) || length(path) != 1 || !nzchar(path) || !file.exists(path)) stop("direct_freeze_file is required and must exist", call. = FALSE)
  a <- trimws(readLines(path, warn = FALSE)); a <- a[nzchar(a)]
  if (length(a) && a[1] == "address") a <- a[-1]
  ps_check_address(a, "direct_freeze_file")
  if (length(a) != DIRECT_FREEZE_N) stop(sprintf("direct_freeze_file must hold exactly %d unique addresses (got %d)", DIRECT_FREEZE_N, length(a)), call. = FALSE)
  list(addresses = a, sha256 = sha256_file(path))
}

# ---- main frame: baseline filters + funnel -------------------------------------------------------------
# inp = load_pre_inputs(); blacklist_any = lower-case addresses on EITHER list at any time in the main baseline
# (blacklisted_any_in_window; REQUIRED, no fallback to the covariates' end-of-baseline flags).
# Order of reasons (first match): direct_freeze_8aug, floor_mean_holdings, floor_nonzero_days, baseline_blacklist, issuer_zero_address.
main_match_frame <- function(inp, blacklist_any, direct_freeze) {
  if (is.null(blacklist_any)) stop("blacklist_any is required (accepted baseline-only any-time blacklist history); no fallback", call. = FALSE)
  w <- inp$wide[day_index %in% MAIN_BASELINE_DAYS]
  base <- w[, .(usdc_sum = sum(usdc_units), usdt_sum = sum(usdt_units),
                mean_hold = mean(usdc_units + usdt_units) / UNITS_PER_DOLLAR,
                nz_days = sum(usdc_units + usdt_units > 0), n_days = .N), by = uid]
  if (any(base$n_days != length(MAIN_BASELINE_DAYS))) stop("main_match_frame: panel lacks the 60 baseline days for some wallet", call. = FALSE)
  base[, usdc_share := usdc_share_ratio(usdc_sum, usdt_sum)]
  cv <- copy(inp$g4cov); setnames(cv, "wallet_id", "uid")
  f <- merge(cv, base, by = "uid")
  if (nrow(f) != nrow(cv)) stop("main_match_frame: covariate wallets lost in merge with baseline panel", call. = FALSE)
  if (is.null(direct_freeze)) stop("direct_freeze addresses are required (prespec §1: 38 addresses frozen 8 Aug 2022)", call. = FALSE)
  ps_check_address(f$address, "covariates.csv")
  ps_check_address(unique(blacklist_any), "blacklist_any")
  f[, `:=`(treat = arm != "control", issuer = as_flag(issuer_mint_redeem_flag), blacklisted = address %in% tolower(blacklist_any))]
  f[, screen_reason := fcase(address %in% direct_freeze, "direct_freeze_8aug",
                             !(mean_hold >= MAIN_FLOOR_MEAN_USD), "floor_mean_holdings",
                             !(nz_days >= MAIN_FLOOR_NZ_DAYS), "floor_nonzero_days",
                             blacklisted, "baseline_blacklist",
                             issuer, "issuer_zero_address",
                             default = "retained")]
  f[, `:=`(transfers = as.numeric(transfer_count_total), eth = as.numeric(eth_balance_wei_units), age = as.numeric(wallet_age_blocks))]
  all_f <- copy(f)
  reasons <- c("direct_freeze_8aug", "floor_mean_holdings", "floor_nonzero_days", "baseline_blacklist", "issuer_zero_address", "retained")
  fun <- rbindlist(lapply(reasons, function(r) data.table(
    reason = r, exposed = sum(f$screen_reason == r & f$treat), control = sum(f$screen_reason == r & !f$treat))))
  list(eligible = f[screen_reason == "retained"], all = all_f, funnel = fun)
}

# USDC share = ratio of baseline sums; exactly 0 / 1 only when a leg sum is exactly zero (prespec §1a; same rule as pre.R).
usdc_share_ratio <- function(usdc_sum, usdt_sum) {
  s <- usdc_sum / pmax(usdc_sum + usdt_sum, 1)
  s <- ifelse(usdt_sum == 0 & usdc_sum > 0, 1, s)
  s <- ifelse(usdc_sum == 0, 0, s)
  ifelse(usdc_sum > 0 & usdt_sum > 0, pmin(pmax(s, 1e-15), 1 - 1e-15), s)
}

# ---- match membership (one row per eligible address; rank/stratum re-derived and cross-checked against cem_match) ----
membership_table <- function(el, res, pop, gen, cut_hash) {
  cov <- el[, .(uid, treat, address, mean_hold, transfers, age, eth, usdc_share)]
  setorderv(cov, "address")
  keys <- lapply(CEM_VARS, function(v) { b <- findInterval(cov[[v]], res$cutpoints[[v]]); b[is.na(cov[[v]])] <- -1L; b })
  keys$usdc_share <- ifelse(cov$usdc_share <= 0, 0L, ifelse(cov$usdc_share >= 1, 2L, 1L))
  st <- do.call(paste, c(unname(lapply(keys, as.integer)), sep = "|"))
  cov[, stratum := match(st, unique(st))]
  set_rng_kind(); set.seed(res$seed)
  cov[, rnd := runif(.N)][, ord := .I]
  cov[treat == FALSE, match_rank := frankv(.SD, cols = c("rnd", "ord"), ties.method = "first"), by = stratum, .SDcols = c("rnd", "ord")]
  cov[treat == TRUE, match_rank := 0L]
  mm <- merge(cov, res$matched[, .(uid, stratum_cem = stratum, w)], by = "uid", all.x = TRUE)
  if (!identical(mm[!is.na(stratum_cem), stratum], mm[!is.na(stratum_cem), stratum_cem]))
    stop("membership_table: re-derived strata disagree with cem_match", call. = FALSE)
  mm[, matched := !is.na(w)][is.na(w), w := 0]
  # rank cross-check: controls selected by the re-derived rank (rank <= min(avail, ratio * exposed) in strata with both arms)
  # must be exactly the controls cem_match selected
  sn <- cov[, .(ne = sum(treat), na = sum(!treat)), by = stratum][, nsel := pmin(na, res$ratio * ne)][ne > 0 & nsel > 0]
  der <- merge(cov[treat == FALSE], sn, by = "stratum")[match_rank <= nsel, uid]
  if (!setequal(der, mm[matched == TRUE & treat == FALSE, uid]))
    stop("membership_table: re-derived match_rank selects different controls than cem_match", call. = FALSE)
  mm[matched == FALSE, match_rank := ifelse(treat, 0L, match_rank)]
  setorderv(mm, "address")
  mm[, .(population_id = pop, generation_id = gen, address, arm = ifelse(treat, "exposed", "control"), stratum,
         matched, weight = w, match_rank, cutpoints_hash = cut_hash, seed = res$seed)]
}

# ---- A1 descriptive diagnostics (NO test, NO threshold; neither selects anything; USDC share is a fixed 3-bin covariate) ----
# (a) mean wallet-level baseline USDC share, eligible exposed vs eligible control frame (difference exposed - frame), plus the
#     fraction exactly 0 / exactly 1.  (b) computed unconditionally: baseline split into days 0..29 and
#     30..59; among eligible FRAME (control) wallets with a defined share in both halves, the unweighted OLS slope and Pearson
#     correlation of (second-half share - first-half share) on first-half share. Wallets with zero holdings in a half are
#     excluded from (b) and counted.
a1_diagnostics <- function(el, wide) {
  fr <- el[treat == FALSE]; ex <- el[treat == TRUE]
  a <- list(exposed_mean_share = mean(ex$usdc_share), frame_mean_share = mean(fr$usdc_share),
            difference = mean(ex$usdc_share) - mean(fr$usdc_share),
            exposed_frac_all_usdc = mean(ex$usdc_share >= 1), frame_frac_all_usdc = mean(fr$usdc_share >= 1),
            exposed_frac_no_usdc = mean(ex$usdc_share <= 0), frame_frac_no_usdc = mean(fr$usdc_share <= 0),
            n_exposed = nrow(ex), n_frame = nrow(fr))
  differs <- is.finite(a$difference) && a$difference != 0     # reported only; (b) is computed regardless (prespec §1a)
  half <- function(days) {
    h <- wide[uid %in% fr$uid & day_index %in% days, .(u = sum(usdc_units), t = sum(usdt_units)), by = uid]
    h[, s := ifelse(u + t > 0, u / (u + t), NA_real_)]
    h[, .(uid, s)]
  }
  b <- list(computed = TRUE, half_split = "baseline days 0..29 vs 30..59 (30+30), descriptive only")
  {
    h1 <- half(A1_HALF_SPLIT$first); h2 <- half(A1_HALF_SPLIT$second)
    m <- merge(h1, h2, by = "uid", suffixes = c("1", "2"))
    ok <- m[!is.na(s1) & !is.na(s2)]
    b$n_frame_wallets <- nrow(fr); b$n_used <- nrow(ok); b$n_excluded_undefined_half <- nrow(fr) - nrow(ok)
    if (nrow(ok) >= 3 && sd(ok$s1) > 0) {
      ch <- ok$s2 - ok$s1
      b$slope_change_on_first_half <- unname(coef(lm(ch ~ ok$s1))[2]); b$correlation <- cor(ok$s1, ch)
    } else { b$slope_change_on_first_half <- NA_real_; b$correlation <- NA_real_ }
  }
  list(a = a, a_differs = differs, b = b, note = "descriptive; no test statistic, alpha or threshold; selects no matching rule")
}

# ---- enlargement: deterministic candidate-window request, trigger rule, one adoption -----------------------
windows_overlap <- function(s1, e1, s2, e2) s1 <= e2 & s2 <= e1

# original_windows: data.table(start_block, end_block) of the original frame windows (block ranges recorded by the gate).
# Draws starts uniformly without replacement from admissible starts (window wholly inside [start_block, end_block]),
# rejecting any overlap with an original window or an already accepted one, until exactly `n` windows are accepted.
draw_enlargement_windows <- function(original_windows, start_block = PRE_START_BLOCK, end_block = PRE_END_BLOCK,
                                     n = ENLARGE_N, window_blocks = ENLARGE_WINDOW_BLOCKS, seed = SEED_ENLARGEMENT) {
  ow <- as.data.table(original_windows)
  if (!all(c("start_block", "end_block") %in% names(ow)) || !nrow(ow)) stop("original_windows needs rows with start_block,end_block", call. = FALSE)
  if (anyNA(ow$start_block) || anyNA(ow$end_block) || any(ow$end_block < ow$start_block) ||
      any(ow$start_block < start_block) || any(ow$end_block > end_block))
    stop("original_windows invalid or outside the main baseline block range", call. = FALSE)
  n_starts <- as.numeric(end_block) - start_block - window_blocks + 2
  set_rng_kind(); set.seed(seed)
  cand <- start_block - 1 + sample.int(n_starts, size = min(n_starts, 50000))
  acc_s <- numeric(0)
  for (s in cand) {
    e <- s + window_blocks - 1
    if (any(windows_overlap(s, e, ow$start_block, ow$end_block))) next
    if (length(acc_s) && any(windows_overlap(s, e, acc_s, acc_s + window_blocks - 1))) next
    acc_s <- c(acc_s, s)
    if (length(acc_s) == n) break
  }
  if (length(acc_s) != n) stop(sprintf("could not draw exactly %d non-overlapping windows (got %d)", n, length(acc_s)), call. = FALSE)
  out <- data.table(draw_order = seq_len(n), start_block = as.numeric(acc_s), end_block = acc_s + window_blocks - 1)
  validate_enlargement_windows(out, ow, n, window_blocks)
  out
}

validate_enlargement_windows <- function(new, original, n = ENLARGE_N, window_blocks = ENLARGE_WINDOW_BLOCKS) {
  new <- as.data.table(new); original <- as.data.table(original)
  if (nrow(new) != n) stop(sprintf("enlargement must add exactly %d windows (got %d)", n, nrow(new)), call. = FALSE)
  if (any(new$end_block - new$start_block + 1 != window_blocks)) stop("enlargement windows must be exactly 400 blocks", call. = FALSE)
  for (i in seq_len(nrow(new))) {
    if (any(windows_overlap(new$start_block[i], new$end_block[i], original$start_block, original$end_block)))
      stop("enlargement window overlaps an original window", call. = FALSE)
    j <- setdiff(seq_len(nrow(new)), i)
    if (any(windows_overlap(new$start_block[i], new$end_block[i], new$start_block[j], new$end_block[j])))
      stop("enlargement windows overlap each other", call. = FALSE)
  }
  invisible(TRUE)
}

# The frozen request (written BEFORE any acquisition). S23 adopts it only on a genuine trigger.
# No seed / count / size / range overrides are accepted: the frozen constants are the only possible request.
request_hash_of <- function(w, original_windows) {
  ps_sha256_text(c(sprintf("seed=%d;n=%d;window_blocks=%d;range=%d-%d", SEED_ENLARGEMENT, ENLARGE_N, ENLARGE_WINDOW_BLOCKS, PRE_START_BLOCK, PRE_END_BLOCK),
                   sprintf("orig=%s", ps_sha256_text(sprintf("%.0f,%.0f", original_windows$start_block, original_windows$end_block))),
                   sprintf("%.0f,%.0f", w$start_block, w$end_block)))
}
freeze_window_request <- function(original_windows) {
  ow <- as.data.table(original_windows)
  w <- draw_enlargement_windows(ow)
  list(windows = w, seed = SEED_ENLARGEMENT, n = ENLARGE_N, window_blocks = ENLARGE_WINDOW_BLOCKS,
       original_windows_hash = ps_sha256_text(sprintf("%.0f,%.0f", ow$start_block, ow$end_block)),
       request_hash = request_hash_of(w, ow), status = "frozen_not_adopted")
}

# placebo: list(status = one of PLACEBO_STATUSES, se_wallet, se_stratum). se_* required only for "genuine".
#   genuine              -> trigger iff power < POWER_TARGET, power = Phi(0.05/SE - 1.96), SE = max(wallet, stratum) (not rescaled)
#   terminal_unavailable -> trigger (the timing criterion for "terminal" is NOT defined here; the caller supplies the status)
#   development / unavailable_nonterminal -> never trigger, no real branch
# Missing / unknown / non-finite genuine inputs stop (never read as "no trigger").
decide_enlargement <- function(placebo) {
  if (is.null(placebo) || !is.list(placebo) || is.null(placebo$status) || !(placebo$status %in% PLACEBO_STATUSES))
    stop("placebo status missing or invalid (must be one of: ", paste(PLACEBO_STATUSES, collapse = ", "), ")", call. = FALSE)
  st <- placebo$status
  base <- list(status = st, trigger = NA, disposition = "undecidable", real_branch_active = FALSE, power = NA_real_, se = NA_real_, reason = NA_character_)
  if (st == "genuine") {
    if (!identical(placebo$population_id, "june_placebo") || !identical(placebo$outcome, "capped_D") ||
        !is.character(placebo$trace_hash) || length(placebo$trace_hash) != 1 || !grepl("^[0-9a-f]{64}$", placebo$trace_hash))
      stop("genuine placebo must carry population_id 'june_placebo', outcome 'capped_D' and a 64-hex trace_hash (development populations never qualify)", call. = FALSE)
    se <- c(placebo$se_wallet, placebo$se_stratum)
    if (length(se) != 2 || !is.numeric(se) || any(!is.finite(se)) || any(se <= 0))
      stop("genuine placebo needs finite positive se_wallet and se_stratum", call. = FALSE)
    base$se <- max(se); base$power <- power_normal(base$se)
    base$trigger <- base$power < POWER_TARGET; base$real_branch_active <- TRUE
    base$trace_hash <- placebo$trace_hash
    base$reason <- if (base$trigger) "genuine June power below 0.80" else "genuine June power at or above 0.80: no enlargement"
  } else if (st == "terminal_unavailable") {
    base$trigger <- TRUE; base$real_branch_active <- TRUE; base$reason <- "genuine placebo terminally unavailable"
  } else {
    base$reason <- if (st == "development") "development output never activates a real branch: undecidable" else "placebo not terminally unavailable: undecidable (prespec has no non-terminal state)"
  }
  base$disposition <- if (is.na(base$trigger)) "undecidable" else if (base$trigger) "enlarge" else "no_enlarge"
  base
}

# Consumers must go through this: NA (undecidable) stops, it is never read as "no enlargement".
enlargement_disposition <- function(decision) {
  if (is.null(decision$trigger) || is.na(decision$trigger)) stop("enlargement decision is undecidable (development / non-terminal placebo): no real branch", call. = FALSE)
  if (decision$trigger) "enlarge" else "no_enlarge"
}

# Adoption is exactly once and unconditional on later power: the enlarged sample becomes primary even if power falls.
adopt_enlargement <- function(decision, window_request, original_windows, prior_adoption = NULL, enlarged_power = NA_real_) {
  if (!is.null(prior_adoption)) stop("enlargement already adopted: exactly once", call. = FALSE)
  if (enlargement_disposition(decision) != "enlarge" || !isTRUE(decision$real_branch_active)) stop("enlargement not triggered by a genuine decision", call. = FALSE)
  ow <- as.data.table(original_windows)
  validate_enlargement_windows(window_request$windows, ow, ENLARGE_N, ENLARGE_WINDOW_BLOCKS)
  if (!identical(request_hash_of(window_request$windows, ow), window_request$request_hash))
    stop("adopted windows do not match the frozen request hash (tampered or for different original windows)", call. = FALSE)
  list(adopted = TRUE, primary_sample = "enlarged", request_hash = window_request$request_hash, n_windows_added = nrow(window_request$windows),
       june_power = decision$power, enlarged_power = enlarged_power,
       power_fell = is.finite(decision$power) && is.finite(enlarged_power) && enlarged_power < decision$power,
       note = "enlarged sample is primary even if its estimated power falls; shortfall and MDE are reported, nothing else changes")
}

# Reject drift between the original main parameters and the enlarged ones. Caps VALUES may differ (recomputed from the final
# enlarged match under the unchanged rule); everything else must be identical.
check_enlargement_invariants <- function(orig, enl) {
  for (k in c("cutpoints_hash", "seed", "windows_rule", "eligibility", "thresholds", "cap_rule", "usdc_mode", "ratio"))
    if (!identical(orig[[k]], enl[[k]])) stop(sprintf("enlargement drift in '%s': cut-points/rules/seed/thresholds must stay unchanged", k), call. = FALSE)
  invisible(TRUE)
}

# ---- entry point ----------------------------------------------------------------------------------------
# Fail-closed cross-check (tribunal finding B): the participation rule given on the CLI must equal the rule g1 recorded in its
# cohort.schema.json `notes` ("participation_rule=<rule> (UNRESOLVED ..."). The rule is otherwise hand-typed in several places.
cohort_schema_read <- function(schema_path) {
  sc <- tryCatch(jsonlite::fromJSON(schema_path, simplifyVector = FALSE), error = function(e) stop(sprintf("cohort schema %s unreadable: %s", schema_path, conditionMessage(e)), call. = FALSE))
  if (!is.list(sc)) stop("cohort.schema.json is not a JSON object", call. = FALSE)
  sc
}
cohort_schema_rule <- function(schema_path) {
  notes <- cohort_schema_read(schema_path)$notes
  if (!is.character(notes) || length(notes) != 1L) stop("cohort.schema.json carries no `notes` string; g1's participation_rule cannot be read", call. = FALSE)
  m <- regmatches(notes, gregexpr("\\bparticipation_rule=[A-Za-z_-]+", notes))[[1]]
  if (length(unique(m)) != 1L) stop("cohort.schema.json notes must record exactly one participation_rule=<rule>", call. = FALSE)
  sub("^participation_rule=", "", unique(m))
}
# The schema must be the one g1 wrote for the cohort.csv already verified for this presign: its `sha256` field must equal the
# population manifest's input_file_sha256[["g1/cohort.csv"]] (and the side manifest's, when one is supplied).
check_cohort_rule <- function(schema_path, rule, cohort_sha256, side_cohort_sha256) {
  if (!is.character(cohort_sha256) || length(cohort_sha256) != 1L || !grepl("^[0-9a-f]{64}$", cohort_sha256))
    stop("population manifest carries no valid input_file_sha256[['g1/cohort.csv']]; cohort schema cannot be bound", call. = FALSE)
  sha <- cohort_schema_read(schema_path)$sha256
  if (!is.character(sha) || length(sha) != 1L || !identical(sha, cohort_sha256))
    stop(sprintf("cohort schema sha256 does not match the verified g1/cohort.csv hash %s; run stopped", cohort_sha256), call. = FALSE)
  if (!is.character(side_cohort_sha256) || length(side_cohort_sha256) != 1L || !grepl("^[0-9a-f]{64}$", side_cohort_sha256))
    stop("side manifest carries no valid g1/cohort.csv sha256; run stopped", call. = FALSE)
  if (!identical(side_cohort_sha256, cohort_sha256))
    stop("side manifest g1/cohort.csv hash differs from the population manifest's; run stopped", call. = FALSE)
  rec <- cohort_schema_rule(schema_path)
  if (!identical(rec, rule)) stop(sprintf("participation rule mismatch: --participation-rule '%s' != '%s' recorded in g1 cohort.schema.json; run stopped", rule, rec), call. = FALSE)
  invisible(rec)
}

# input_dir: explicit dir holding covariates.csv + wallet_coin_day_pre.csv (main pre population). population_manifest: list (see
# check_main_manifest). candidates: data.table(address, source_membership) = the full candidate universe (screened or not).
# original_windows: data.table(start_block, end_block). transfer_participation_rule: REQUIRED recorded metadata (the baseline
# Transfer-participation design is unresolved; nothing here applies or defaults it).
run_presign <- function(input_dir, population_manifest, population_id, output_dir, blacklist_any, candidates, original_windows,
                        transfer_participation_rule, direct_freeze_file, cohort_schema_file, side_cohort_sha256) {
  if (!is.character(input_dir) || !is.character(output_dir) || length(input_dir) != 1 || length(output_dir) != 1 || !nzchar(input_dir) || !nzchar(output_dir))
    stop("input_dir and output_dir are required explicit paths", call. = FALSE)
  if (grepl("(^|/)r_pre(/|$)", input_dir) || grepl("(^|/)r_pre(/|$)", output_dir)) stop("out/r_pre is never an input or output of the presign stage", call. = FALSE)
  if (!is.character(transfer_participation_rule) || !nzchar(transfer_participation_rule))
    stop("transfer_participation_rule is required (unresolved design; recorded verbatim, never defaulted)", call. = FALSE)
  check_main_manifest(population_manifest, population_id)
  check_cohort_rule(cohort_schema_file, transfer_participation_rule, population_manifest$input_file_sha256[["g1/cohort.csv"]], side_cohort_sha256)
  miss <- REQUIRED_PRE_FILES[!file.exists(file.path(input_dir, REQUIRED_PRE_FILES))]
  if (length(miss)) stop(sprintf("required pre-event input missing: %s; run stopped (no post-event file was consulted)", paste(miss, collapse = ", ")), call. = FALSE)
  if (file.exists(file.path(output_dir, "match_membership.csv"))) stop("output_dir already holds a generation (immutable); use a new directory", call. = FALSE)
  cand <- as.data.table(candidates)
  if (!all(c("address", "source_membership") %in% names(cand))) stop("candidates needs address,source_membership", call. = FALSE)
  ps_check_address(cand$address, "candidates")

  dfz <- read_direct_freeze(direct_freeze_file)
  if (is.null(blacklist_any)) stop("blacklist_any is required (accepted baseline-only any-time blacklist history); no fallback", call. = FALSE)
  inp <- load_pre_inputs(input_dir)
  mf <- main_match_frame(inp, blacklist_any, dfz$addresses)
  el <- mf$eligible
  if (!any(el$treat) || !any(!el$treat)) stop("no eligible exposed or control wallet after baseline filters", call. = FALSE)
  unk <- setdiff(mf$all$address, cand$address)
  if (length(unk)) stop(sprintf("candidates incomplete: %d population addresses are not in the candidate universe", length(unk)), call. = FALSE)

  res <- cem_match(el[, .(uid, treat, address, mean_hold, transfers, age, eth, usdc_share)], CEM_VARS, "three_bins",
                   ratio = MAIN_RATIO, seed = SEED_PRIMARY_CEM)
  cuts <- res$cutpoints
  cut_txt <- unlist(lapply(names(cuts), function(k) sprintf("%s:%s", k, paste(sprintf("%.17g", cuts[[k]]), collapse = ","))))
  cut_hash <- ps_sha256_text(cut_txt)
  pop <- population_manifest$population_id; gen <- population_manifest$generation_id
  mm <- membership_table(el, res, pop, gen, cut_hash)

  panel <- build_panel(inp$wide, res$matched, baseline_days = MAIN_BASELINE_DAYS)
  caps <- cap_values_all(panel, MAIN_EVENT_DAY)       # from the FINAL main match; never June caps

  # coverage: every candidate, including those outside the g4 population, excluded and unmatched
  cv <- merge(cand, mf$all[, .(address, screen_reason)], by = "address", all.x = TRUE)
  cv <- merge(cv, mm[, .(address, matched)], by = "address", all.x = TRUE)
  cv[, `:=`(screen_status = fifelse(is.na(screen_reason), "excluded", fifelse(screen_reason == "retained", "retained", "excluded")),
            screen_reason = fifelse(is.na(screen_reason), "not_in_pre_population", screen_reason))]
  cv[screen_status == "retained" & !matched, screen_reason := "retained_unmatched"]
  cov_out <- cv[, .(population_id = pop, generation_id = gen, address, source_membership, screen_status, screen_reason)]
  setorderv(cov_out, "address")
  agg <- merge(cv, rbind(mf$all[, .(address, arm)], data.table(address = setdiff(cand$address, mf$all$address), arm = NA_character_)), by = "address")
  agg_out <- agg[, .N, by = .(arm = fifelse(is.na(arm), "not_in_population", ifelse(arm == "control", "control", "exposed")), screen_reason)][order(arm, screen_reason)]

  ex <- el[treat == TRUE]
  qt <- rbindlist(lapply(c(CEM_VARS, "usdc_share"), function(v) {
    q <- quantile(ex[[v]], c(.25, .5, .75), type = 7, na.rm = TRUE, names = FALSE)
    data.table(variable = v, q25 = q[1], q50 = q[2], q75 = q[3], n_na = sum(is.na(ex[[v]])))
  }))
  a1 <- a1_diagnostics(el, inp$wide[day_index %in% MAIN_BASELINE_DAYS])
  req <- freeze_window_request(as.data.table(original_windows))

  hashes <- list(exposed_eligible = ps_hash_set(el[treat == TRUE, address]), control_eligible = ps_hash_set(el[treat == FALSE, address]),
                 matched_exposed = ps_hash_set(mm[matched & arm == "exposed", address]), matched_control = ps_hash_set(mm[matched & arm == "control", address]),
                 candidates = ps_hash_set(cand$address), blacklist_any = ps_hash_set(blacklist_any), direct_freeze = dfz$sha256, cutpoints = cut_hash, enlargement_request = req$request_hash)
  params <- list(population_id = pop, generation_id = gen, population_manifest = population_manifest,
    windows = list(baseline = "2022-06-09..2022-08-07 (60 days)", post = "2022-08-09..2022-09-12", excluded_dates = "2022-08-08"),
    windows_rule = sprintf("%d additional non-overlapping %d-block windows, seed %d, if triggered", ENLARGE_N, ENLARGE_WINDOW_BLOCKS, SEED_ENLARGEMENT),
    eligibility = list(mean_holdings_usd_min = MAIN_FLOOR_MEAN_USD, nonzero_days_min = MAIN_FLOOR_NZ_DAYS, baseline_days = 60L,
                       blacklist = "either issuer, any time 2022-06-09..2022-08-07", issuer_zero_address_screen = TRUE),
    thresholds = list(delta_d = DELTA_D, delta_a = DELTA_A, power_target = POWER_TARGET, clip_flag_pp = CLIP_FLAG_PP),
    usdc_mode = "three_bins", ratio = MAIN_RATIO, seed = SEED_PRIMARY_CEM, rng_kind = RNG_KIND,
    cutpoints = cuts, cutpoints_hash = cut_hash,
    cap_rule = CAP_RULE_TEXT, cap_basis = "final main match, baseline days 0..59", cap_values = list(D = caps$D, A = caps$A, legs_pooled = caps$legs),
    cap_n_wallet_days = caps$n_wallet_days,
    n_exposed_eligible = res$n_exposed, n_exposed_dropped_no_control = res$dropped_exposed, n_controls_matched = res$n_controls_matched,
    transfer_participation_rule = transfer_participation_rule,
    blacklist_any_n = length(unique(blacklist_any)), direct_freeze_n = DIRECT_FREEZE_N,
    a1b_definition = paste("per-half USDC share = ratio of sums over baseline days 0..29 / 30..59; eligible control frame wallets, pre-match;",
      "unweighted OLS slope and Pearson r of (s2 - s1) on s1; wallets with zero holdings in either half dropped and counted; NA if n < 3.",
      "slope = b(s2|s1) - 1, so measurement noise in s1 biases it negative. Computed unconditionally and descriptive; selects nothing."),
    window_draw = sprintf("admissible start %d..%d-%d+1 (window wholly inside); R Mersenne-Twister/Inversion/Rejection, set.seed(%d), sample.int prefix of 50,000 candidates, sequential rejection of overlaps with original and accepted windows",
                          PRE_START_BLOCK, PRE_END_BLOCK, ENLARGE_WINDOW_BLOCKS, SEED_ENLARGEMENT),
    enlargement_note = "The prespec defines no non-terminal placebo state and the terminal-unavailability timing criterion is unresolved; status is a caller-supplied enum. development / unavailable_nonterminal are undecidable (trigger NA), never no-trigger.",
    versions = list(R = R.version.string, data.table = as.character(packageVersion("data.table")), fixest = as.character(packageVersion("fixest")),
                    jsonlite = as.character(packageVersion("jsonlite"))),
    input_sha256 = setNames(lapply(file.path(input_dir, REQUIRED_PRE_FILES), sha256_file), REQUIRED_PRE_FILES),
    enlargement_request_hash = req$request_hash, cohort_hashes = hashes)

  dir.create(output_dir, recursive = TRUE, showWarnings = FALSE)
  fwrite(mm, file.path(output_dir, "match_membership.csv"))
  fwrite(cov_out, file.path(output_dir, "eligible_frame_coverage.csv"))
  fwrite(agg_out, file.path(output_dir, "eligible_frame_funnel.csv"))
  fwrite(qt, file.path(output_dir, "exposed_quartiles.csv"))
  fwrite(req$windows, file.path(output_dir, "enlargement_request_windows.csv"))
  wj <- function(x, f) jsonlite::write_json(x, file.path(output_dir, f), auto_unbox = TRUE, pretty = TRUE, digits = NA, na = "string")
  wj(params, "analysis_parameters.json"); wj(a1, "a1_diagnostics.json"); wj(hashes, "cohort_hashes.json")
  wj(req[setdiff(names(req), "windows")], "enlargement_request.json")
  invisible(list(params = params, membership = mm, coverage = cov_out, funnel = mf$funnel, quartiles = qt, a1 = a1,
                 request = req, caps = caps, hashes = hashes, match = res))
}
