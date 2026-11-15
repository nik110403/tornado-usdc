# Post-period estimator (S19). ADDITIVE and FIXTURE-ONLY until the signature gate (S10b) passes: this file defines the functions;
# it never runs at source time and has no default input path. Reuses R/estimate.R (build_panel, estimate_did, prep_est,
# clip_incidence), R/decide.R (classify_ci, classify_h1a, h1_support, clip_flag, reallocation_label), R/cem.R (cem_match) and, from
# R/presign.R (S17), ps_sha256_text / ps_check_address / usdc_share_ratio / MAIN_BASELINE_DAYS / CAP_RULE_TEXT. R/drdid.R holds the
# DRDID row. None of those files is edited. Source order: utils, decide, cem, estimate, pre, presign, drdid, post.
#
# Inputs are EXPLICIT paths: the pre population directory (covariates.csv, wallet_coin_day_pre.csv), the post panel
# (post_wallet_coin_day.csv, S18), match_membership.csv + analysis_parameters.json (S17) and, optionally, the eligible-frame
# coverage and four evidence manifests. Anything absent makes ONLY its rows "not estimable" (never zero, never a default).
#
# Combined day index: 0..59 = 9 Jun..7 Aug (baseline), 60 = 8 Aug (excluded), 61..95 = 9 Aug..12 Sep (post, 35 days).

POST_EVENT_DAY <- 60L
POST_DAY0 <- 61L
POST_N_DAYS <- 35L
POST_DAYS <- POST_DAY0:(POST_DAY0 + POST_N_DAYS - 1L)
POST_START_DATE <- as.Date("2022-08-09")
BINANCE_LAST_DAY <- as.integer(as.Date("2022-09-04") - PRE_PERIOD_START)    # 87: through 4 Sep inclusive
PRETREND_NOTE_DAY <- as.integer(as.Date("2022-06-30") - PRE_PERIOD_START)   # 21: Tether froze 46 addresses in one day
POST_PANEL_COLS <- c("wallet_id", "coin", "date", "day_index", "balance_units", "arm", "nonzero", "address", "population_id",
                     "generation_id", "source_file_sha256", "source_range_id", "source_receipt_sha256")
MEMBERSHIP_COLS <- c("population_id", "generation_id", "address", "arm", "stratum", "matched", "weight", "match_rank", "cutpoints_hash", "seed")
OUTFLOW_COMPONENTS <- c("eth_dex", "dai", "new_address")
SHA_RE <- "^[0-9a-f]{64}$"

po_stop <- function(...) stop(sprintf(...), call. = FALSE)

# ---- readers (every one validates before use; hard STOP on schema / key / population problems) --------------------
po_read_params <- function(path) {
  if (!is.character(path) || length(path) != 1 || !nzchar(path) || !file.exists(path)) po_stop("analysis_parameters path is required and must exist")
  p <- jsonlite::fromJSON(path, simplifyVector = TRUE)
  need <- c("population_id", "generation_id", "cutpoints_hash", "seed", "cutpoints", "cap_values")
  if (length(setdiff(need, names(p)))) po_stop("analysis_parameters lacks: %s", paste(setdiff(need, names(p)), collapse = ", "))
  for (k in c("D", "A", "legs_pooled")) {
    v <- p$cap_values[[k]]
    if (!is.numeric(v) || length(v) != 2 || !all(is.finite(v)) || v[1] >= v[2]) po_stop("analysis_parameters cap_values$%s invalid", k)
  }
  if (!all(vapply(CEM_VARS, function(v) is.numeric(p$cutpoints[[v]]) && length(p$cutpoints[[v]]) >= 1, TRUE))) po_stop("analysis_parameters cutpoints incomplete")
  p$.sha256 <- sha256_file(path)
  p
}

po_check_population <- function(population_id, generation_id) {
  if (!nzchar(population_id) || !nzchar(generation_id)) po_stop("empty population_id / generation_id")
  if (grepl("june|development|diagnostic", population_id, ignore.case = TRUE))
    po_stop("population '%s' is a June / development population: never a post-estimation population", population_id)
}

po_read_membership <- function(path, params) {
  if (!is.character(path) || length(path) != 1 || !nzchar(path) || !file.exists(path)) po_stop("match_membership path is required and must exist")
  m <- fread(path, colClasses = c(address = "character", population_id = "character", generation_id = "character", cutpoints_hash = "character", arm = "character"))
  if (length(setdiff(MEMBERSHIP_COLS, names(m)))) po_stop("match_membership lacks: %s", paste(setdiff(MEMBERSHIP_COLS, names(m)), collapse = ", "))
  ps_check_address(m$address, "match_membership")
  if (!identical(unique(m$population_id), params$population_id) || !identical(unique(m$generation_id), params$generation_id))
    po_stop("match_membership population/generation differs from analysis_parameters")
  if (!identical(unique(m$cutpoints_hash), params$cutpoints_hash) || any(m$seed != params$seed)) po_stop("match_membership cutpoints_hash / seed differ from analysis_parameters")
  if (!all(m$arm %in% c("exposed", "control"))) po_stop("match_membership: unknown arm")
  if (!is.logical(m$matched) || anyNA(m$matched) || anyNA(m$weight) || !all(is.finite(m$weight))) po_stop("match_membership: matched/weight must be logical/finite")
  if (any(m$weight < 0) || any(!m$matched & m$weight != 0) || any(m$matched & m$weight <= 0) || any(m$matched & m$arm == "exposed" & m$weight != 1))
    po_stop("match_membership: weights violate exposed 1 / matched control > 0 / unmatched 0")
  mt <- m[matched == TRUE]
  if (!any(mt$arm == "exposed") || !any(mt$arm == "control")) po_stop("match_membership has no matched exposed or no matched control wallet")
  s <- mt[, .(ne = sum(arm == "exposed"), nc = sum(arm == "control")), by = stratum]
  if (any(s$ne == 0 | s$nc == 0) || any(s$nc > 3 * s$ne)) po_stop("match_membership: a stratum lacks an arm or exceeds 3 controls per exposed wallet")
  list(all = m, matched = mt[, .(uid = address, treat = arm == "exposed", stratum = as.integer(stratum), w = weight)])
}

# post panel -> list(wide = data.table(uid = address, day_index = 61..95, usdc_units, usdt_units), provenance, n_addresses)
po_read_post_panel <- function(path, params, required_addresses, exact_addresses = NULL) {
  if (!is.character(path) || length(path) != 1 || !nzchar(path) || !file.exists(path)) po_stop("post_wallet_coin_day path is required and must exist")
  d <- fread(path, colClasses = "character")
  if (length(setdiff(POST_PANEL_COLS, names(d)))) po_stop("post panel lacks: %s", paste(setdiff(POST_PANEL_COLS, names(d)), collapse = ", "))
  if (!nrow(d)) po_stop("post panel is empty")
  ps_chk <- grepl("^0x[0-9a-f]{40}$", d$address)
  if (!all(ps_chk)) po_stop("post panel: address must be lowercase 0x + 40 hex")
  if (!identical(unique(d$population_id), params$population_id) || !identical(unique(d$generation_id), params$generation_id))
    po_stop("post panel population/generation differs from analysis_parameters (absent or mixed ids)")
  if (!all(d$coin %in% c("USDC", "USDT"))) po_stop("post panel: unknown coin")
  if (!all(grepl("^-?[0-9]+$", d$balance_units))) po_stop("post panel: balance_units must be decimal integer base units")
  di <- suppressWarnings(as.integer(d$day_index))
  if (anyNA(di) || any(di < 0L | di >= POST_N_DAYS)) po_stop("post panel: day_index outside 0..34 (window is 2022-08-09..2022-09-12)")
  if (!identical(d$date, format(POST_START_DATE + di))) po_stop("post panel: date does not equal 2022-08-09 + day_index (8 Aug and out-of-window dates rejected)")
  prov <- lapply(c("source_file_sha256", "source_range_id", "source_receipt_sha256"), function(k) unique(d[[k]]))
  names(prov) <- c("source_file_sha256", "source_range_id", "source_receipt_sha256")
  if (any(lengths(prov) != 1L) || any(!nzchar(unlist(prov))) || !grepl(SHA_RE, prov$source_file_sha256) || !grepl(SHA_RE, prov$source_receipt_sha256))
    po_stop("post panel: provenance columns must be single-valued, non-empty, sha256 where applicable")
  d[, `:=`(day_index = di, bal = as.numeric(balance_units))]
  if (anyDuplicated(d, by = c("address", "coin", "day_index"))) po_stop("post panel: duplicate (address, coin, day_index)")
  n_addr <- uniqueN(d$address)
  if (nrow(d) != n_addr * 2L * POST_N_DAYS) po_stop("post panel is not dense: %d rows for %d addresses (expected %d)", nrow(d), n_addr, n_addr * 2L * POST_N_DAYS)
  if (length(setdiff(required_addresses, d$address))) po_stop("post panel lacks %d required eligible-frame addresses (full coverage required)", length(setdiff(required_addresses, d$address)))
  if (!is.null(exact_addresses) && !setequal(exact_addresses, d$address))
    po_stop("post panel addresses differ from the retained eligible frame (%d extra)", length(setdiff(d$address, exact_addresses)))
  w <- dcast(d[, .(uid = address, day_index = POST_DAY0 + day_index, coin, bal)], uid + day_index ~ coin, value.var = "bal")
  setnames(w, c("USDC", "USDT"), c("usdc_units", "usdt_units"))
  list(wide = w, provenance = prov, n_addresses = n_addr)
}

po_read_addr_file <- function(path, cols, what) {
  if (!is.character(path) || length(path) != 1 || !nzchar(path) || !file.exists(path)) po_stop("%s: file must exist", what)
  d <- fread(path, colClasses = "character")
  if (length(setdiff(cols, names(d)))) po_stop("%s lacks: %s", what, paste(setdiff(cols, names(d)), collapse = ", "))
  if (nrow(d) && !all(grepl("^0x[0-9a-f]{40}$", d$address))) po_stop("%s: address must be lowercase 0x + 40 hex", what)
  d
}

# ---- pre inputs keyed by address -----------------------------------------------------------------------------------
po_pre_wide <- function(inp) {
  m <- inp$g4cov[, .(uid_id = wallet_id, uid = address)]
  ps_check_address(m$uid, "covariates.csv")
  w <- merge(inp$wide, m, by.x = "uid", by.y = "uid_id")
  w[, .(uid = uid.y, day_index, usdc_units, usdt_units)]
}

# Eligible (pre-match) frame with the matching covariates; same definitions as main_match_frame (S17).
po_frame <- function(inp, wide_pre, addresses) {
  base <- wide_pre[uid %in% addresses & day_index %in% MAIN_BASELINE_DAYS,
                   .(usdc_sum = sum(usdc_units), usdt_sum = sum(usdt_units), mean_hold = mean(usdc_units + usdt_units) / UNITS_PER_DOLLAR, n = .N), by = uid]
  if (nrow(base) != length(addresses) || any(base$n != length(MAIN_BASELINE_DAYS))) po_stop("po_frame: eligible wallets lack the 60 baseline days")
  base[, usdc_share := usdc_share_ratio(usdc_sum, usdt_sum)]
  cv <- inp$g4cov[address %in% addresses, .(uid = address, treat = arm != "control", address,
                                            transfers = as.numeric(transfer_count_total), eth = as.numeric(eth_balance_wei_units),
                                            age = as.numeric(wallet_age_blocks))]
  merge(cv, base[, .(uid, mean_hold, usdc_share)], by = "uid")
}

# CEM with SUPPLIED cut-points (the S17 primary cut-points stay unchanged in every rematch/subgroup; R/cem.R cannot take them, and
# is not edited). Same ordering, seed, random-number, rank and IKP-weight logic as cem_match(); the test proves it reproduces
# cem_match() exactly when given its cut-points. usdc_mode: none | three_bins | continuous (needs share_cuts).
cem_match_fixed <- function(cov, vars, cuts, usdc_mode = "three_bins", share_cuts = NULL, ratio = 3L, seed = SEED_PRIMARY_CEM, age_var = "age") {
  cov <- copy(as.data.table(cov)); setorderv(cov, "address")
  stopifnot(all(c("uid", "treat", vars) %in% names(cov)), anyDuplicated(cov$uid) == 0)
  bin <- function(x, br, na_ok, what) {
    if (anyNA(x) && !na_ok) po_stop("CEM: missing values in '%s'; run stopped, no imputation", what)
    b <- findInterval(x, br); b[is.na(x)] <- -1L; b
  }
  keys <- lapply(vars, function(v) bin(cov[[v]], cuts[[v]], v == age_var, v)); names(keys) <- vars
  if (usdc_mode == "three_bins") keys$usdc_share <- coarsen_usdc_share(cov$usdc_share, NULL, "three_bins", 3L)
  else if (usdc_mode == "continuous") keys$usdc_share <- bin(cov$usdc_share, share_cuts, FALSE, "usdc_share")
  else if (usdc_mode != "none") po_stop("usdc_mode must be none|three_bins|continuous")
  keys <- lapply(keys, as.integer)
  cov[, stratum := do.call(paste, c(unname(keys), sep = "|"))]
  cov[, stratum := match(stratum, unique(stratum))]
  set_rng_kind(); set.seed(seed)
  cov[, rnd := runif(.N)]; cov[, ord := .I]
  cov[treat == FALSE, rk := frankv(.SD, cols = c("rnd", "ord"), ties.method = "first"), by = stratum, .SDcols = c("rnd", "ord")]
  st <- cov[, .(n_exposed = sum(treat), n_ctrl_avail = sum(!treat)), by = stratum]
  st[, n_ctrl_sel := pmin(n_ctrl_avail, ratio * n_exposed)]
  st[, kept := n_exposed > 0 & n_ctrl_sel > 0]
  m_T <- st[kept == TRUE, sum(n_exposed)]; m_C <- st[kept == TRUE, sum(n_ctrl_sel)]
  if (!(m_T > 0 && m_C > 0)) po_stop("CEM: no stratum holds both arms")
  st[, ctrl_weight := ifelse(kept, (m_C / m_T) * (n_exposed / n_ctrl_sel), 0)]
  cov <- merge(cov, st[, .(stratum, n_ctrl_sel, kept, ctrl_weight)], by = "stratum", sort = FALSE)
  m <- cov[kept == TRUE & (treat == TRUE | rk <= n_ctrl_sel), .(uid, treat, stratum, w = fifelse(treat, 1, ctrl_weight))]
  setorder(m, uid)
  list(matched = m, n_exposed = sum(cov$treat), dropped_exposed = sum(st[kept == FALSE, n_exposed]), n_controls_matched = sum(!m$treat))
}

# ---- estimation on one matched sample -----------------------------------------------------------------------------
po_panel <- function(wide_all, matched) build_panel(wide_all, matched, MAIN_BASELINE_DAYS)

PO_GRID_MAIN <- CJ(outcome = c("D", "A"), cap = c("own", "none", "leg_pooled"), cluster = c("uid", "stratum"), sorted = FALSE)
PO_GRID_LEGS <- CJ(outcome = c("usdc_B", "usdt_B"), cap = "none", cluster = c("uid", "stratum"), sorted = FALSE)
PO_GRID_H1 <- CJ(outcome = c("D", "A"), cap = c("own", "none"), cluster = c("uid", "stratum"), sorted = FALSE)

# Failed / non-finite fits are reported as rows, never retried with another model.
po_fit_grid <- function(panel, grid) {
  rbindlist(lapply(seq_len(nrow(grid)), function(i) {
    g <- grid[i]
    r <- tryCatch(estimate_did(panel, POST_EVENT_DAY, g$outcome, g$cluster, g$cap), error = function(e) e)
    ok <- !inherits(r, "error") && all(is.finite(c(r$beta, r$se, r$lo, r$hi)))
    data.table(outcome = g$outcome, cap = g$cap, cluster = g$cluster, status = if (ok) "estimated" else "not_estimable",
               reason = if (ok) NA_character_ else if (inherits(r, "error")) conditionMessage(r) else "non-finite estimate or interval",
               beta = if (ok) r$beta else NA_real_, se = if (ok) r$se else NA_real_, lo = if (ok) r$lo else NA_real_, hi = if (ok) r$hi else NA_real_,
               n_obs = if (ok) r$n_obs else NA_integer_, n_clusters = if (ok) r$n_clusters else NA_integer_)
  }))
}

po_iv <- function(est, o, c, cl) { r <- est[outcome == o & cap == c & cluster == cl]; if (nrow(r) != 1L || r$status != "estimated") NULL else c(r$lo, r$hi) }

# Decision records from an estimate table (+ optional clip flag). Pure; used for the primary and every rematch.
po_decide <- function(est, cap_sensitive_clip = NA) {
  cw <- po_iv(est, "D", "own", "uid"); cs <- po_iv(est, "D", "own", "stratum")
  uw <- po_iv(est, "D", "none", "uid"); us <- po_iv(est, "D", "none", "stratum")
  h1 <- h1_support(cw, cs, uw, us)
  case_of <- function(i) if (is.null(i)) NA_integer_ else classify_ci(i[1], i[2], DELTA_D)$case
  h1a <- lapply(c(uid = "uid", stratum = "stratum"), function(cl) {
    a_cap <- po_iv(est, "A", "own", cl); a_unc <- po_iv(est, "A", "none", cl)
    lab <- function(i) if (is.null(i)) NA_character_ else classify_h1a(i[1], i[2])
    re <- if (is.null(a_cap) || is.null(a_unc) || is.na(cap_sensitive_clip)) list(allowed = NA, conditions = NULL)
          else reallocation_label(h1$status, a_cap, a_unc, cap_sensitive_clip)
    list(capped = lab(a_cap), uncapped = lab(a_unc), realloc_allowed = re$allowed)
  })
  agree <- !anyNA(c(h1a$uid$capped, h1a$stratum$capped)) && h1a$uid$capped == h1a$stratum$capped
  comb <- if (anyNA(c(h1a$uid$capped, h1a$stratum$capped))) "not_estimable" else if (agree) h1a$uid$capped else "dependence_sensitive"
  ra <- c(h1a$uid$realloc_allowed, h1a$stratum$realloc_allowed)
  list(h1_status = h1$status, h1_cases = h1$cases, h1_veto = h1$veto,
       capped_case_uid = case_of(cw), capped_case_stratum = case_of(cs), uncapped_case_uid = case_of(uw), uncapped_case_stratum = case_of(us),
       h1a_capped_uid = h1a$uid$capped, h1a_capped_stratum = h1a$stratum$capped, h1a_uncapped_uid = h1a$uid$uncapped, h1a_uncapped_stratum = h1a$stratum$uncapped,
       h1a_combined = comb,
       clip_flag = cap_sensitive_clip,      # clip-incidence flag (prespec §2), distinct from the veto-driven h1_status "cap_sensitive"
       reallocation_wording_allowed = if (anyNA(ra)) NA else all(ra),
       reallocation_rule = "allowed only if the four prespec conditions hold under BOTH clusterings (S19 convention; prespec fixes no single interval)")
}

po_cluster_table <- function(panel, ctx) {
  d <- prep_est(panel, POST_EVENT_DAY, "none")
  rbindlist(lapply(c("uid", "stratum"), function(cl) {
    s <- d[, .(obs = .N, wallets = uniqueN(uid), exposed_wallets = uniqueN(uid[treat]), control_wallets = uniqueN(uid[!treat])), by = cl]
    data.table(cluster = cl, n_clusters = nrow(s), n_obs = nrow(d), obs_per_cluster_min = min(s$obs), obs_per_cluster_median = median(s$obs), obs_per_cluster_max = max(s$obs),
               wallets_per_cluster_min = min(s$wallets), wallets_per_cluster_median = median(s$wallets), wallets_per_cluster_max = max(s$wallets),
               clusters_with_both_arms = sum(s$exposed_wallets > 0 & s$control_wallets > 0), n_exposed_wallets = uniqueN(d[treat == TRUE, uid]),
               n_control_wallets = uniqueN(d[treat == FALSE, uid]))
  }))
}

po_weight_concentration <- function(m) {
  m <- as.data.table(m)
  arms <- rbindlist(lapply(c(exposed = TRUE, control = FALSE), function(t) {
    w <- sort(m[treat == t, w], decreasing = TRUE)
    data.table(scope = "arm", n_wallets = length(w), sum_w = sum(w), effective_n = sum(w)^2 / sum(w^2), max_w = max(w), max_w_share = max(w) / sum(w),
               hhi = sum((w / sum(w))^2), top1pct_share = sum(head(w, max(1L, ceiling(0.01 * length(w))))) / sum(w), min_w = min(w))
  }), idcol = "arm")
  sw <- m[treat == FALSE, .(sw = sum(w)), by = stratum]
  arms[, `:=`(n_strata = NA_integer_, max_stratum_control_weight_share = NA_real_)]
  rbind(arms, data.table(arm = "control", scope = "stratum", n_wallets = NA_integer_, sum_w = sum(sw$sw), effective_n = sum(sw$sw)^2 / sum(sw$sw^2),
                         max_w = max(sw$sw), max_w_share = max(sw$sw) / sum(sw$sw), hhi = sum((sw$sw / sum(sw$sw))^2), top1pct_share = NA_real_, min_w = min(sw$sw),
                         n_strata = nrow(sw), max_stratum_control_weight_share = max(sw$sw) / sum(sw$sw)), fill = TRUE)
}

# Uncapped weighted arm x period means of the legs and of D, A (descriptive; distinct from the capped estimands).
po_descriptive_legs <- function(panel) {
  d <- panel[day_index != POST_EVENT_DAY]
  d[, period := fifelse(day_index < POST_EVENT_DAY, "baseline", "post")][, arm := fifelse(treat, "exposed", "control")]
  rbindlist(lapply(c("usdc_B", "usdt_B", "D", "A"), function(o)
    d[, .(mean_uncapped = sum(w * get(o)) / sum(w), n_wallet_days = .N), by = .(arm, period)][, outcome := o]))[, .(outcome, arm, period, mean_uncapped, n_wallet_days)]
}

# Event-study (descriptive, wallet-clustered). Coefficients relative to 7 Aug (day 59); 8 Aug excluded.
po_event_study <- function(panel, outcome, sample_days, report_days, ref_day = 59L, cap = "own") {
  d <- prep_est(panel, POST_EVENT_DAY, cap)[day_index %in% sample_days]
  d[, ex := as.numeric(treat)]
  out <- data.table(outcome = outcome, day_index = report_days, date = format(PRE_PERIOD_START + report_days), coef = NA_real_, se = NA_real_, lo = NA_real_, hi = NA_real_,
                    status = "not_estimable", reason = NA_character_)
  out[day_index == ref_day, `:=`(coef = 0, se = 0, lo = 0, hi = 0, status = "reference")]
  fit <- tryCatch(feols(as.formula(sprintf("%s ~ i(day_index, ex, ref = %d) | uid + day_index", outcome, ref_day)), data = d, weights = d$w, cluster = ~uid,
                        fixef.tol = 1e-11, fixef.iter = 10000, notes = FALSE), error = function(e) e)
  if (inherits(fit, "error")) { out[status == "not_estimable", reason := conditionMessage(fit)]; return(out[]) }
  cf <- coef(fit); sv <- se(fit); ci <- confint(fit, level = 0.95)
  k <- as.integer(sub("^day_index::(-?[0-9]+):ex$", "\\1", names(cf)))
  for (i in seq_along(cf)) if (k[i] %in% report_days && is.finite(cf[i]) && is.finite(sv[i]))
    out[day_index == k[i], `:=`(coef = unname(cf[i]), se = unname(sv[i]), lo = ci[i, 1], hi = ci[i, 2], status = "estimated")]
  out[status == "not_estimable", reason := "coefficient dropped (collinear) or non-finite"]
  out[, note := fifelse(day_index == PRETREND_NOTE_DAY, "30 Jun 2022: Tether froze 46 addresses in one day", NA_character_)]
  out[]
}

# Shortened window through 4 Sep inclusive: same population, baseline denominator B, caps and weights.
po_short_window <- function(panel, est_full) {
  p2 <- panel[day_index <= BINANCE_LAST_DAY]; setattr(p2, "baseline_days", attr(panel, "baseline_days"))
  est <- po_fit_grid(p2, PO_GRID_H1)
  dec <- po_decide(est)
  full <- po_decide(est_full)
  cases_short <- c(dec$capped_case_uid, dec$capped_case_stratum); cases_full <- c(full$capped_case_uid, full$capped_case_stratum)
  list(est = est, decision = dec, last_day_index = BINANCE_LAST_DAY, post_days = sum(unique(p2$day_index) > POST_EVENT_DAY),
       binance_sensitive = if (anyNA(c(cases_short, cases_full))) NA else any(cases_short != cases_full),
       definition = "Binance-sensitive iff the capped-D section 3 case under either clustering differs from the full 35-day case")
}

# H2 exploratory: deposit-count terciles among FINAL matched exposed wallets (type 7 at 1/3, 2/3; ties not split; empty groups reported).
po_h2 <- function(panel, deposit_counts) {
  ex <- unique(panel[treat == TRUE, .(uid)])
  nz <- function(reason) data.table(group = c("low", "middle", "high"), status = "not estimable", reason = reason)
  if (is.null(deposit_counts)) return(list(table = nz("deposit-count manifest not supplied"), cuts = c(NA_real_, NA_real_)))
  dc <- merge(ex, deposit_counts[, .(uid = address, n = suppressWarnings(as.numeric(deposit_count)))], by = "uid", all.x = TRUE)
  if (anyNA(dc$n) || any(dc$n < 0 | dc$n != round(dc$n))) return(list(table = nz("deposit-count manifest does not cover every matched exposed wallet with a non-negative integer"), cuts = c(NA_real_, NA_real_)))
  q <- unname(quantile(dc$n, c(1 / 3, 2 / 3), type = 7))
  dc[, grp := fifelse(n <= q[1], "low", fifelse(n <= q[2], "middle", "high"))]
  d <- merge(prep_est(panel, POST_EVENT_DAY, "own"), dc[, .(uid, grp)], by = "uid", all.x = TRUE)
  for (g in c("low", "middle", "high")) set(d, j = paste0("tp_", g), value = as.numeric(!is.na(d$grp) & d$grp == g & d$tp == 1))
  counts <- dc[, .N, by = grp]
  tab <- rbindlist(lapply(c("low", "middle", "high"), function(g) {
    n <- counts[grp == g, N]; if (!length(n)) n <- 0L
    base <- data.table(group = g, n_exposed_wallets = n, lower_cut = q[1], upper_cut = q[2])
    if (n == 0L) return(cbind(base, cluster = NA_character_, status = "empty group", reason = "no matched exposed wallet in this tercile", beta = NA_real_, se = NA_real_, lo = NA_real_, hi = NA_real_))
    rbindlist(lapply(c("uid", "stratum"), function(cl) {
      keep <- c("low", "middle", "high")[c("low", "middle", "high") %in% counts$grp]
      r <- tryCatch(feols(as.formula(sprintf("D ~ %s | uid + day_index", paste0("tp_", keep, collapse = " + "))), data = d, weights = d$w, cluster = as.formula(paste0("~", cl)),
                          fixef.tol = 1e-11, fixef.iter = 10000, notes = FALSE), error = function(e) e)
      nm <- paste0("tp_", g)
      if (inherits(r, "error") || !(nm %in% names(coef(r))) || !is.finite(se(r)[nm])) return(cbind(base, cluster = cl, status = "not estimable",
        reason = if (inherits(r, "error")) conditionMessage(r) else "coefficient dropped or non-finite", beta = NA_real_, se = NA_real_, lo = NA_real_, hi = NA_real_))
      ci <- confint(r, level = 0.95)[nm, ]
      cbind(base, cluster = cl, status = "estimated", reason = NA_character_, beta = unname(coef(r)[nm]), se = unname(se(r)[nm]), lo = ci[[1]], hi = ci[[2]])
    }))
  }))
  tab[, exploratory := "pattern only; never determines H1"]
  list(table = tab, cuts = q)
}

# Conditional outflow breakdown: only when H1a is a meaningful differential decline; ONLY frozen verified evidence. CEX is unavailable.
# evidence: data.table(address, component in OUTFLOW_COMPONENTS, amount_units = stablecoin-equivalent value in 6-dec base units of the verified
# outflow, evidence_manifest_sha256). A component must cover EVERY matched wallet (explicit zero rows); missing rows are never zero.
po_outflow <- function(panel, h1a_combined, evidence) {
  rows <- function(comp, st, why, ...) data.table(component = comp, status = st, reason = why, ...)
  cex <- rows("cex_deposits", "unavailable", "centralised-exchange deposits are not estimable without a verified label source")
  if (!identical(h1a_combined, "decline"))
    return(rbind(rows(OUTFLOW_COMPONENTS, "not applicable", sprintf("H1a is '%s', not a meaningful differential decline: breakdown not triggered", h1a_combined)), cex, fill = TRUE))
  if (is.null(evidence))
    return(rbind(rows(OUTFLOW_COMPONENTS, "not estimable", "verified evidence manifest absent: components are not inferred or labelled"), cex, fill = TRUE))
  W <- unique(panel[, .(uid, treat, w, B)])                # B = wallet baseline mean holdings in dollars (build_panel)
  out <- lapply(OUTFLOW_COMPONENTS, function(cm) {
    e <- evidence[component == cm]
    if (!nrow(e)) return(rows(cm, "not estimable", "component absent from the verified evidence manifest"))
    if (!all(grepl(SHA_RE, e$evidence_manifest_sha256)) || !all(grepl("^-?[0-9]+$", e$amount_units)) || anyDuplicated(e$address))
      return(rows(cm, "not estimable", "evidence rows malformed (manifest hash, integer units, unique addresses)"))
    if (length(setdiff(W$uid, e$address))) return(rows(cm, "not estimable", "evidence does not cover every matched wallet (absence is not zero)"))
    x <- merge(W, e[, .(uid = address, amt = as.numeric(amount_units) / UNITS_PER_DOLLAR)], by = "uid")[, y := amt / B]
    mx <- x[treat == TRUE, sum(w * y) / sum(w)]; mc <- x[treat == FALSE, sum(w * y) / sum(w)]
    rows(cm, "estimated", NA_character_, exposed_weighted_mean_scaled = mx, control_weighted_mean_scaled = mc, difference = mx - mc,
         n_exposed = sum(x$treat), n_control = sum(!x$treat), evidence_manifest_sha256 = paste(sort(unique(e$evidence_manifest_sha256)), collapse = ","))
  })
  rbind(rbindlist(out, fill = TRUE), cex, fill = TRUE)
}

# Exact primary cut-points (type 7, eligible exposed, duplicates removed) and their hash exactly as S17 hashes them.
po_primary_cuts <- function(frame) {
  cuts <- lapply(CEM_VARS, function(v) attr(coarsen_q(frame[[v]], frame[[v]][frame$treat], 4L, na_ok = v == "age", what = v), "cuts"))
  names(cuts) <- CEM_VARS
  list(cuts = cuts, hash = ps_sha256_text(unlist(lapply(names(cuts), function(k) sprintf("%s:%s", k, paste(sprintf("%.17g", cuts[[k]]), collapse = ","))))))
}

# ---- rematches / subgroups (all on the eligible frame, PRIMARY cut-points unchanged) -----------------------------------
po_rematch <- function(label, ref, frame, cuts, usdc_mode, share_cuts, wide_all, primary_est) {
  na_row <- function(status, why) data.table(row = label, prespec_ref = ref, status = status, reason = why)
  tryCatch({
    res <- cem_match_fixed(frame, CEM_VARS, cuts, usdc_mode, share_cuts)
    panel <- po_panel(wide_all, res$matched)
    est <- po_fit_grid(panel, PO_GRID_H1[outcome == "D"])
    dec <- po_decide(est)
    g <- function(cl, cp) est[outcome == "D" & cap == cp & cluster == cl]
    prim <- po_decide(primary_est)
    ok <- any(est$status == "estimated")
    cbind(na_row(if (ok) "estimated" else "not estimable", if (ok) NA_character_ else "no estimable interval"),
          data.table(n_exposed_eligible = res$n_exposed, n_exposed_dropped_no_control = res$dropped_exposed, n_exposed_retained = res$n_exposed - res$dropped_exposed,
                     n_controls_matched = res$n_controls_matched, usdc_mode = usdc_mode,
                     capped_beta_uid = g("uid", "own")$beta, capped_lo_uid = g("uid", "own")$lo, capped_hi_uid = g("uid", "own")$hi, capped_case_uid = dec$capped_case_uid,
                     capped_beta_stratum = g("stratum", "own")$beta, capped_lo_stratum = g("stratum", "own")$lo, capped_hi_stratum = g("stratum", "own")$hi,
                     capped_case_stratum = dec$capped_case_stratum, uncapped_case_uid = dec$uncapped_case_uid, uncapped_case_stratum = dec$uncapped_case_stratum,
                     h1_status = dec$h1_status,
                     differs_from_primary_case = po_differs(c(dec$capped_case_uid, dec$capped_case_stratum), c(prim$capped_case_uid, prim$capped_case_stratum)),
                     caps_note = "caps recomputed on this rematched sample under the unchanged section 2 rule"))
  }, error = function(e) na_row("not estimable", conditionMessage(e)))
}

# NA (a case that cannot be classified on either side) is "not comparable", never TRUE.
po_differs <- function(a, b) if (anyNA(c(a, b))) NA else any(a != b)

# Cap-robustness rows (§5.8): uncapped and leg-pooled caps, D (section 3 case) and A (H1a label), both clusterings, vs the primary own-cap row.
po_cap_robustness <- function(est) {
  rbindlist(lapply(c("none", "leg_pooled"), function(cp) rbindlist(lapply(c("D", "A"), function(o) rbindlist(lapply(c("uid", "stratum"), function(cl) {
    r <- est[outcome == o & cap == cp & cluster == cl]; p <- est[outcome == o & cap == "own" & cluster == cl]
    lab <- function(x) if (nrow(x) != 1L || x$status != "estimated") NA_character_ else if (o == "D") as.character(classify_ci(x$lo, x$hi, DELTA_D)$case) else classify_h1a(x$lo, x$hi)
    data.table(outcome = o, cap = cp, cluster = cl, status = if (nrow(r) == 1L) r$status else "not_estimable", beta = r$beta, lo = r$lo, hi = r$hi,
               decision = lab(r), primary_decision = lab(p), differs_from_primary_case = po_differs(lab(r), lab(p)))
  }))))))
}

PO_REMATCH_REFS <- c(no_usdc_share = "§5.6 / §1a no USDC share", quartile_share = "§5.6 / §1a exposed-quartile USDC share",
                     drop_later_freeze = "§5.9 drop wallets frozen 8 Aug-12 Sep", usdc_pool_flagged = "§5.10 USDC-pool depositors (exploratory)",
                     usdc_pool_unflagged = "§5.10 other exposed wallets (exploratory)")

po_rematches <- function(frame, cuts, wide_all, primary_est, later_freeze, pool_flag, rederived_ok) {
  skip <- function(id, why) data.table(row = id, prespec_ref = PO_REMATCH_REFS[[id]], status = "not estimable", reason = why)
  if (!rederived_ok) return(rbindlist(lapply(names(PO_REMATCH_REFS), skip, why = "eligible frame does not reproduce the frozen match_membership: rematch refused"), fill = TRUE))
  one <- function(id, fr, mode, sc = NULL) po_rematch(id, PO_REMATCH_REFS[[id]], fr, cuts, mode, sc, wide_all, primary_est)
  share_cuts <- unique(quantile(frame[treat == TRUE, usdc_share], c(.25, .5, .75), type = 7, names = FALSE))
  out <- list(one("no_usdc_share", frame, "none"), one("quartile_share", frame, "continuous", share_cuts))
  # `later_freeze` = lf_later_freeze() result (R/later_freeze.R): derived in signed R code from the g0 lifetime freeze files, vetoed (never extended) by
  # the g5_veto raw logs. NULL = inputs not supplied; status "not estimable" carries the reason (absence / failed coverage is never "none").
  out[[3]] <- if (is.null(later_freeze)) skip("drop_later_freeze", "later-freeze inputs (g0 lifetime freeze files + a verified g5_veto output) not supplied; absence is not 'none'")
              else if (!identical(later_freeze$status, "derived")) skip("drop_later_freeze", later_freeze$reason)
              else { r <- one("drop_later_freeze", frame[!(address %in% later_freeze$dropped)], "three_bins")
                     dropped <- frame[address %in% later_freeze$dropped]
                     r[, `:=`(n_later_freeze_dropped_exposed = sum(dropped$treat), n_later_freeze_dropped_control = sum(!dropped$treat),
                              n_later_freeze_g0_derived = later_freeze$n_g0_derived, n_later_freeze_vetoed = length(later_freeze$vetoed),
                              n_later_freeze_veto_discrepancy_not_added = length(later_freeze$discrepancy))]; r }
  ex_addr <- frame[treat == TRUE, address]
  if (is.null(pool_flag) || length(setdiff(ex_addr, pool_flag$address)) || !all(toupper(pool_flag$usdc_pool_depositor) %in% c("TRUE", "FALSE"))) {
    why <- "USDC-pool depositor flag manifest absent or not covering every eligible exposed wallet (missing is never 'unflagged')"
    out[[4]] <- skip("usdc_pool_flagged", why); out[[5]] <- skip("usdc_pool_unflagged", why)
  } else {
    fl <- pool_flag[toupper(usdc_pool_depositor) == "TRUE", address]
    out[[4]] <- one("usdc_pool_flagged", frame[treat == FALSE | address %in% fl], "three_bins")
    out[[5]] <- one("usdc_pool_unflagged", frame[treat == FALSE | !(address %in% fl)], "three_bins")
  }
  rbindlist(out, fill = TRUE)
}

# ---- freeze record (no data needed; written before signing and embedded in every run) ------------------------------------
po_spec_record <- function() list(
  windows = list(baseline = "2022-06-09..2022-08-07 (day_index 0..59)", excluded = "2022-08-08 (day_index 60)", post = "2022-08-09..2022-09-12 (day_index 61..95, 35 days)",
                 short_window_last_date = "2022-09-04 inclusive (day_index 87)"),
  estimator = "R/estimate.R estimate_did: weighted TWFE (fixest::feols), wallet + day FE, CEM weights; clustered by wallet and, separately, by CEM stratum",
  decision = list(delta_d = DELTA_D, delta_a = DELTA_A, clip_flag_pp = CLIP_FLAG_PP, h1 = "R/decide.R h1_support (both clusterings case 1, uncapped veto)",
                  h1a = "R/decide.R classify_h1a per clustering; combined = shared label else dependence_sensitive (S19 reporting convention)"),
  cap_rule = CAP_RULE_TEXT, cap_source = "frozen analysis_parameters cap_values, cross-checked against recomputation on the matched baseline (hard stop on mismatch)",
  h2 = "deposit-count terciles among final matched exposed (type 7, 1/3 and 2/3; low <= q1 < middle <= q2 < high), exploratory",
  outflow = "descriptive; only if H1a combined == decline; verified ETH/DEX/DAI/new-address manifest only; CEX unavailable",
  disclosures = list(
    h1a_combination = "H1a is classified per clustering; combined label = the shared label, else dependence_sensitive (S19 convention; prespec fixes no single interval)",
    reallocation_conjunction = "reallocation wording allowed only if all four prespec conditions hold under BOTH clusterings; clip_flag and veto (h1_status cap_sensitive) are separate fields",
    outflow_trigger = "outflow breakdown reported only when h1a_combined == decline (both clusterings agree on a meaningful differential decline)",
    outflow_metric = "weighted mean over matched wallets of verified component outflow (stablecoin-equivalent 6-dec base units, to dollars) divided by baseline B, by arm and difference; descriptive",
    caps = "rematch rows recompute caps on their own rematched sample under the unchanged section 2 rule; the DRDID capped-D row uses the FROZEN main caps from analysis_parameters",
    event_study_inference = "pretrend and time-path intervals are wallet-clustered only (descriptive, no stratum clustering, no test)",
    binance_scope = "Binance-sensitive compares the capped-D section 3 case only (both clusterings) between the 35-day and 4 Sep windows; uncapped, A and legs are not compared",
    later_freeze = "5.9 rematch reports per-arm counts of eligible wallets dropped, derived in signed R code from the g0 lifetime freeze files and checked (never extended) by the g5_veto raw logs; a g0-derived wallet the veto does not confirm, the g0 inputs or a verified veto output missing, or a coverage check failing makes the row not estimable (g0 source-provenance failures are a hard denial) (absence is not 'none')"),
  rematch_rule = "cem_match_fixed: primary cut-points (S17) unchanged, seed 20261015, ratio 3:1; share quartiles from eligible exposed for the quartile row",
  versions = list(R = R.version.string, data.table = as.character(packageVersion("data.table")), fixest = as.character(packageVersion("fixest")),
                  jsonlite = as.character(packageVersion("jsonlite"))))

write_post_freeze <- function(path) {
  if (file.exists(path)) po_stop("freeze file exists (immutable): %s", path)
  jsonlite::write_json(list(drdid = drdid_freeze_record(), spec = po_spec_record()), path, auto_unbox = TRUE, pretty = TRUE, digits = NA, na = "string")
  invisible(sha256_file(path))
}

# ---- shared pre-window stage: every hard stop that depends on PRE data only -------------------------------------------
# Called by run_post (before the post panel is read) AND by tools/s24_dryrun.R (the S24 assembler dry run), so a signed package cannot pass
# g5 and then po_stop here after post data was fetched. Stops: eligible_frame_coverage schema / ids / duplicates / status-vs-reason /
# matched subset; required wallets present in the pre population; matched wallets with baseline B <= 0; recomputed cap values (D, A, pooled
# legs; B and caps use baseline days 0..59 only) differing from the frozen cap_values; eligible wallets lacking the 60 baseline days (po_frame).
po_pre_stage <- function(pre_dir, params, mem, eligible_frame_coverage_path = NULL) {
  pop <- params$population_id; gen <- params$generation_id
  retained <- NULL; cov_sha <- NA_character_
  if (!is.null(eligible_frame_coverage_path)) {
    cv <- po_read_addr_file(eligible_frame_coverage_path, c("population_id", "generation_id", "address", "source_membership", "screen_status", "screen_reason"), "eligible_frame_coverage")
    if (!identical(unique(cv$population_id), pop) || !identical(unique(cv$generation_id), gen)) po_stop("eligible_frame_coverage population/generation differs from analysis_parameters")
    if (anyDuplicated(cv$address)) po_stop("eligible_frame_coverage: duplicate addresses")
    # same column presign uses for eligibility: screen_reason in retained / retained_unmatched (screen_status must agree)
    el_reason <- cv$screen_reason %in% c("retained", "retained_unmatched")
    if (!identical(el_reason, cv$screen_status == "retained")) po_stop("eligible_frame_coverage: screen_status disagrees with screen_reason about retained wallets")
    retained <- cv[el_reason, address]
    if (length(setdiff(mem$matched$uid, retained))) po_stop("matched wallets are not all in the retained eligible frame")
    cov_sha <- sha256_file(eligible_frame_coverage_path)
  }
  inp <- load_pre_inputs(pre_dir)                              # pre-event only (assert_no_post_data inside)
  wide_pre <- po_pre_wide(inp)
  need_addr <- if (is.null(retained)) mem$matched$uid else retained
  if (length(setdiff(need_addr, wide_pre$uid))) po_stop("required wallets are missing from the pre population")
  panel0 <- po_panel(wide_pre[uid %in% need_addr], mem$matched)   # B and the cap values read baseline days 0..59 only: identical to the post-stage panel's
  if (uniqueN(panel0$uid) != nrow(mem$matched)) po_stop("matched wallets lost in the panel (baseline holdings B <= 0)")
  caps <- cap_values_all(panel0, POST_EVENT_DAY)
  for (k in c("D", "A")) if (!isTRUE(all.equal(caps[[k]], as.numeric(params$cap_values[[k]]), tolerance = 1e-9)))
    po_stop("recomputed %s caps differ from the frozen analysis_parameters cap_values: hard stop", k)
  if (!isTRUE(all.equal(caps$legs, as.numeric(params$cap_values$legs_pooled), tolerance = 1e-9))) po_stop("recomputed pooled leg caps differ from the frozen cap_values: hard stop")
  frame <- if (is.null(retained)) NULL else po_frame(inp, wide_pre, retained)
  list(inp = inp, wide_pre = wide_pre, retained = retained, cov_sha = cov_sha, need_addr = need_addr, frame = frame, caps = caps)
}

first_or_na <- function(x) if (length(x)) as.character(x[1]) else NA_character_

# ---- entry point ----------------------------------------------------------------------------------------------------
# All paths explicit; no default to out/. Optional manifests absent => only their rows are "not estimable".
#  later_freeze_inputs list(circle_events, usdt_events, veto = lf_verify_veto(), post_end_block) -- the ONLY way the §5.9 row is estimated; there is no
#                      hand-supplied list (a header-only csv is never a verified "none")
#  usdc_pool_flag_path csv `address,usdc_pool_depositor` (TRUE/FALSE) for every eligible exposed wallet
#  deposit_counts_path csv `address,deposit_count` (deduplicated by tx hash + log index, up to 7 Aug)
#  outflow_evidence_path csv `address,component,amount_units,evidence_manifest_sha256` (verified, frozen)
run_post <- function(pre_dir, post_panel_path, match_membership_path, analysis_parameters_path, output_dir,
                     eligible_frame_coverage_path = NULL, later_freeze_inputs = NULL, usdc_pool_flag_path = NULL,
                     deposit_counts_path = NULL, outflow_evidence_path = NULL) {
  if (!is.character(pre_dir) || !is.character(output_dir) || length(pre_dir) != 1 || length(output_dir) != 1 || !nzchar(pre_dir) || !nzchar(output_dir))
    po_stop("pre_dir and output_dir are required explicit paths")
  if (grepl("(^|/)r_pre(/|$)", pre_dir) || grepl("(^|/)r_pre(/|$)", output_dir)) po_stop("out/r_pre is never an input or output of the post stage")
  if (file.exists(file.path(output_dir, "post_estimates.csv"))) po_stop("output_dir already holds a generation (immutable); use a new directory")
  miss <- REQUIRED_PRE_FILES[!file.exists(file.path(pre_dir, REQUIRED_PRE_FILES))]
  if (length(miss)) po_stop("required pre-event input missing: %s; run stopped", paste(miss, collapse = ", "))
  params <- po_read_params(analysis_parameters_path); po_check_population(params$population_id, params$generation_id)
  pop <- params$population_id; gen <- params$generation_id
  mem <- po_read_membership(match_membership_path, params)

  pre <- po_pre_stage(pre_dir, params, mem, eligible_frame_coverage_path)   # ALL pre-data-only hard stops (shared with tools/s24_dryrun.R)
  inp <- pre$inp; wide_pre <- pre$wide_pre; retained <- pre$retained; cov_sha <- pre$cov_sha; need_addr <- pre$need_addr; caps <- pre$caps
  post <- po_read_post_panel(post_panel_path, params, required_addresses = need_addr, exact_addresses = retained)
  wide_all <- rbind(wide_pre[uid %in% need_addr], post$wide)

  panel <- po_panel(wide_all, mem$matched)

  # --- primary estimates and decisions ---
  est <- rbind(po_fit_grid(panel, PO_GRID_MAIN), po_fit_grid(panel, PO_GRID_LEGS))
  clipt <- clip_incidence(panel, POST_EVENT_DAY); cf <- clip_flag(clipt)
  dec <- po_decide(est, cf$cap_sensitive_clip)
  robust <- po_cap_robustness(est)
  legs_id <- {
    g <- function(o, cl) est[outcome == o & cap == "none" & cluster == cl, beta]
    rbindlist(lapply(c("uid", "stratum"), function(cl) data.table(cluster = cl,
      identity_resid_usdc = g("usdc_B", cl) - (g("A", cl) + g("D", cl)) / 2, identity_resid_usdt = g("usdt_B", cl) - (g("A", cl) - g("D", cl)) / 2)))
  }
  cluster_t <- po_cluster_table(panel); weight_t <- po_weight_concentration(mem$matched)
  dleg <- po_descriptive_legs(panel)
  pre_t <- rbindlist(lapply(c("D", "A"), function(o) po_event_study(panel, o, MAIN_BASELINE_DAYS, MAIN_BASELINE_DAYS)))
  path_t <- rbindlist(lapply(c("D", "A"), function(o) po_event_study(panel, o, c(MAIN_BASELINE_DAYS, POST_DAYS), c(59L, POST_DAYS))))
  short <- po_short_window(panel, est[outcome %in% c("D", "A") & cap %in% c("own", "none")])
  dc <- if (is.null(deposit_counts_path)) NULL else po_read_addr_file(deposit_counts_path, c("address", "deposit_count"), "deposit_counts")
  h2 <- po_h2(panel, dc)
  ev <- if (is.null(outflow_evidence_path)) NULL else po_read_addr_file(outflow_evidence_path, c("address", "component", "amount_units", "evidence_manifest_sha256"), "outflow_evidence")
  outf <- po_outflow(panel, dec$h1a_combined, ev)

  # --- eligible-frame rows: rematches and DRDID ---
  # S59 LOW-2: the g0 hashes are traced whenever later_freeze_inputs is given, so the derivation must also be possible: without the eligible frame it is not -> fail closed
  if (!is.null(later_freeze_inputs) && is.null(retained)) po_stop("later_freeze_inputs given without eligible_frame_coverage: the §5.9 list is derived against the eligible frame; refusing a trace that hashes g0 inputs the derivation never read")
  lf <- NULL                                                   # set below once the eligible frame is known
  pf <- if (is.null(usdc_pool_flag_path)) NULL else po_read_addr_file(usdc_pool_flag_path, c("address", "usdc_pool_depositor"), "usdc_pool_flag")
  dr_na <- function(why) rbindlist(lapply(c("capped_D", "uncapped_D"), function(o) drdid_estimate(data.table(treat = logical(0)), o)[, `:=`(status = "not estimated", reason = why)]), fill = TRUE)
  if (is.null(retained)) {
    why <- "eligible_frame_coverage not supplied: the pre-match eligible frame is unknown"
    rem <- rbindlist(lapply(names(PO_REMATCH_REFS), function(id) data.table(row = id, prespec_ref = PO_REMATCH_REFS[[id]], status = "not estimable", reason = why)))
    dr <- dr_na(why); rederived <- NA
  } else {
    frame <- pre$frame
    # JSON stores cut-points at 15 significant digits, so the exact type-7 cut-points are recomputed from the frame and must reproduce
    # the frozen cutpoints_hash (which hashes them at %.17g); only then is the frame accepted as the frozen one.
    pc <- tryCatch(po_primary_cuts(frame), error = function(e) NULL)
    r0 <- if (!is.null(pc) && identical(pc$hash, params$cutpoints_hash))
            tryCatch(cem_match_fixed(frame, CEM_VARS, pc$cuts, "three_bins", ratio = 3L, seed = as.integer(params$seed)), error = function(e) NULL) else NULL
    rederived <- !is.null(r0) && nrow(r0$matched) == nrow(mem$matched) &&
      isTRUE(all.equal(setorder(copy(r0$matched), uid)[, .(uid, w)], setorder(copy(mem$matched), uid)[, .(uid, w)], tolerance = 1e-12, check.attributes = FALSE))
    lf <- lf_later_freeze(later_freeze_inputs, frame$address)
    rem <- po_rematches(frame, if (isTRUE(rederived)) pc$cuts else NULL, wide_all, est[outcome %in% c("D", "A") & cap %in% c("own", "none")], lf, pf, isTRUE(rederived))
    fr <- tryCatch(drdid_frame(wide_all, inp$g4cov, retained, as.numeric(params$cap_values$D)), error = function(e) e)
    dr <- if (inherits(fr, "error")) dr_na(conditionMessage(fr))
          else { r <- rbindlist(lapply(c("capped_D", "uncapped_D"), drdid_estimate, fr = fr)); r[, n_dropped_B_le_0 := attr(fr, "n_dropped_B")]; r }
  }

  # --- reporting-row index: every prespec row, with status and reason ---
  rr <- function(id, ref, status, reason = NA_character_) data.table(row_id = id, prespec_ref = ref, status = status, reason = as.character(reason))
  st_est <- function(ok) if (ok) "estimated" else "not estimable"
  report <- rbind(
    rr("h1_pooled_35d", "§3 sole confirmatory H1", st_est(dec$h1_status != "not_estimable"),
       sprintf("h1_status=%s; veto=%s; clip_flag=%s%s", dec$h1_status, dec$h1_veto, dec$clip_flag, if (dec$h1_status == "not_estimable") "; an interval is missing or non-finite" else "")),
    rr("h1a", "§3 H1a confirmatory-secondary", st_est(dec$h1a_combined != "not_estimable")),
    rr("cluster_sizes", "§2 cluster sizes/counts", "estimated"), rr("cem_weight_concentration", "§2", "estimated"),
    rr("descriptive_uncapped_legs", "§2", st_est(all(est[outcome %in% c("usdc_B", "usdt_B"), status] == "estimated"))),
    rr("cap_clip_diagnostics", "§2", "estimated"),
    rr("h2_terciles_intensity", "§3 exploratory", if (any(h2$table$status == "estimated")) "estimated (exploratory)" else "not estimable", first_or_na(h2$table[status != "estimated", reason])),
    rr("outflow_breakdown", "§3 conditional", if (any(outf$status == "estimated")) "estimated" else outf[component == "eth_dex", status], first_or_na(outf[component == "eth_dex", reason])),
    rr("outflow_cex", "§3", "unavailable", "no verified centralised-exchange label source"),
    rr("post_time_path", "§4 descriptive", st_est(any(path_t$status == "estimated"))),
    rr("pretrend_60_days", "§5.1", st_est(any(pre_t$status == "estimated"))),
    rr("june_placebo", "§5.2", "not owned by S19", "genuine June execution is S21"),
    rr("placebo_cohort", "§5.3", "not estimable", "verified frozen lending/exchange manifest absent; no substitute cohort"),
    rr("binance_shortened_window", "§5.4", st_est(short$decision$h1_status != "not_estimable")),
    rr("binance_flags_and_outflow_shares", "§5.4", "not estimable", "no verified Binance label source"),
    rr("drdid_capped_D", "§5.5", dr[row == "capped_D", status], dr[row == "capped_D", reason]),
    rr("drdid_uncapped_D", "§5.5", dr[row == "uncapped_D", status], dr[row == "uncapped_D", reason]),
    rem[, .(row_id = row, prespec_ref, status, reason = as.character(reason))],
    rr("deposit_address_reuse", "§5.7 / §1b", "not estimable", "no verified exchange-label source"),
    rr("uncapped_and_leg_cap", "§5.8", st_est(all(robust$status == "estimated")),
       if (any(robust$differs_from_primary_case, na.rm = TRUE)) "a robustness row differs from the primary case: reported, never overrides" else NA),
    rr("owner_grouped_inference", "§2", "not estimable", "no defensible owner partition"))

  # --- write ---
  inputs <- list(pre_covariates = sha256_file(file.path(pre_dir, "covariates.csv")), pre_panel = sha256_file(file.path(pre_dir, "wallet_coin_day_pre.csv")),
                 post_panel = sha256_file(post_panel_path), match_membership = sha256_file(match_membership_path), analysis_parameters = params$.sha256,
                 eligible_frame_coverage = cov_sha,
                 g0_circle_events = if (is.null(later_freeze_inputs)) NA_character_ else sha256_file(later_freeze_inputs$circle_events),
                 g0_usdt_events = if (is.null(later_freeze_inputs)) NA_character_ else sha256_file(later_freeze_inputs$usdt_events),
                 g0_receipts = if (is.null(later_freeze_inputs)) NA_character_ else sha256_file(later_freeze_inputs$g0_receipts),
                 g5_veto_manifest = if (is.null(later_freeze_inputs) || !isTRUE(later_freeze_inputs$veto$ok)) NA_character_ else later_freeze_inputs$veto$manifest_sha,
                 usdc_pool_flag = if (is.null(usdc_pool_flag_path)) NA_character_ else sha256_file(usdc_pool_flag_path),
                 deposit_counts = if (is.null(deposit_counts_path)) NA_character_ else sha256_file(deposit_counts_path),
                 outflow_evidence = if (is.null(outflow_evidence_path)) NA_character_ else sha256_file(outflow_evidence_path))
  iv <- unlist(inputs)
  trace_hash <- ps_sha256_text(sprintf("%s=%s", names(iv), ifelse(is.na(iv), "ABSENT", iv)))
  tag <- function(dt) cbind(data.table(population_id = pop, generation_id = gen, input_trace_hash = trace_hash), dt)
  dir.create(output_dir, recursive = TRUE, showWarnings = FALSE)
  w <- function(dt, f) fwrite(tag(dt), file.path(output_dir, f))
  decision_t <- data.table(item = names(dec), value = vapply(dec, function(x) paste(format(x), collapse = ";"), ""))
  caps_t <- data.table(table = "caps", a = c("D", "A", "legs_pooled"), b = NA_character_, c = NA_character_, d = NA_character_,
                       value = c(caps$D[1], caps$A[1], caps$legs[1]), value2 = c(caps$D[2], caps$A[2], caps$legs[2]),
                       frozen_lo = c(params$cap_values$D[1], params$cap_values$A[1], params$cap_values$legs_pooled[1]),
                       frozen_hi = c(params$cap_values$D[2], params$cap_values$A[2], params$cap_values$legs_pooled[2]))
  w(est, "post_estimates.csv"); w(decision_t, "post_decisions.csv"); w(cluster_t, "post_cluster_sizes.csv"); w(weight_t, "post_weight_concentration.csv")
  w(rbind(dleg[, .(table = "arm_period_means", outcome, arm, period, value = mean_uncapped)],
          legs_id[, .(table = "identity_residual_usdc", outcome = cluster, arm = NA_character_, period = NA_character_, value = identity_resid_usdc)],
          legs_id[, .(table = "identity_residual_usdt", outcome = cluster, arm = NA_character_, period = NA_character_, value = identity_resid_usdt)]), "post_descriptive_legs.csv")
  w(rbind(caps_t,
          clipt[, .(table = "clip_incidence", a = outcome, b = tail, c = arm, d = period, value = share, value2 = NA_real_, frozen_lo = NA_real_, frozen_hi = NA_real_)],
          cf$table[, .(table = "clip_did", a = outcome, b = tail, c = as.character(exceeds), d = NA_character_, value = did, value2 = NA_real_, frozen_lo = NA_real_, frozen_hi = NA_real_)]), "post_cap_clip.csv")
  w(robust, "post_cap_robustness.csv"); w(pre_t, "post_pretrend.csv"); w(path_t, "post_time_path.csv")
  w(cbind(short$est, data.table(last_day_index = short$last_day_index, binance_sensitive = short$binance_sensitive)), "post_short_window.csv")
  if (!is.null(lf) && (identical(lf$status, "derived") || length(lf$vetoed) || length(lf$discrepancy)))   # per-address disposition of the §5.9 derivation (audit; discrepancies are listed, never dropped)
    w(data.table(address = c(lf$dropped, lf$vetoed, lf$discrepancy),
                   disposition = rep(c("dropped", "vetoed_not_dropped", "veto_only_discrepancy_not_added"), lengths(list(lf$dropped, lf$vetoed, lf$discrepancy)))), "post_later_freeze.csv")
  w(rem, "post_rematches.csv"); w(h2$table, "post_h2.csv"); w(outf, "post_outflow.csv"); w(dr, "post_drdid.csv"); w(report, "post_reporting_rows.csv")
  jsonlite::write_json(list(population_id = pop, generation_id = gen, base_analysis_parameters_sha256 = params$.sha256, drdid = drdid_freeze_record(), spec = po_spec_record(),
                            transfer_participation_rule = if (is.null(params$transfer_participation_rule)) "not recorded in analysis_parameters" else params$transfer_participation_rule,
                            post_panel_provenance = post$provenance, primary_match_rederived_from_frame = rederived),
                       file.path(output_dir, "post_analysis_parameters.json"), auto_unbox = TRUE, pretty = TRUE, digits = NA, na = "string")
  outs <- sort(list.files(output_dir, full.names = TRUE))
  jsonlite::write_json(list(population_id = pop, generation_id = gen, input_sha256 = inputs, input_trace_hash = trace_hash,
                            output_sha256 = setNames(lapply(outs, sha256_file), basename(outs))),
                       file.path(output_dir, "post_trace.json"), auto_unbox = TRUE, pretty = TRUE, na = "string")
  invisible(list(est = est, decision = dec, clip = cf, cluster = cluster_t, weights = weight_t, legs = dleg, legs_identity = legs_id, pretrend = pre_t,
                 time_path = path_t, short = short, h2 = h2, outflow = outf, rematches = rem, drdid = dr, report = report, caps = caps,
                 trace_hash = trace_hash, inputs = inputs, panel = panel, robust = robust, primary_rederived = rederived))
}
