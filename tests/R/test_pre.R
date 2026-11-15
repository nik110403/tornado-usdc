tmp <- tempfile("pre"); dir.create(tmp)
g4 <- file.path(tmp, "g4"); dir.create(g4)
test("pre stage fails loudly (blocked on S8) when g4 outputs are absent", {
  e <- try(run_pre("none", g4, file.path(tmp, "o"), placebo_pop = "g4", g0dir = file.path(tmp, "g0x")), silent = TRUE)
  stopifnot(inherits(e, "try-error"), grepl("BLOCKED ON S8", conditionMessage(attr(e, "condition"))))
})
test("hard rule: any date after 2022-08-07 aborts", {
  bad <- data.table(wallet_id = 0L, coin = "USDC", date = "2022-08-08", day_index = 60L, balance_units = 1)
  e <- try(assert_no_post_data(bad, "x"), silent = TRUE)
  stopifnot(inherits(e, "try-error"), grepl("HARD RULE", conditionMessage(attr(e, "condition"))))
  stopifnot(isTRUE(assert_no_post_data(data.table(date = "2022-08-07", day_index = 59L))))
})
# Fake S8-shaped inputs (test fixtures only; never written under out/). Mirrors gates/g4_controls.py:
# ONE dense wallet_id space over controls + exposed, ids assigned by sorted address, so arms are INTERLEAVED
# (exposed ids are NOT 0..n-1, controls are NOT offset) -- the original g1/g4 mixed-id bug would mis-join here.
set.seed(5)
mk_panel <- function(ids, base) {
  g <- CJ(wallet_id = ids, coin = c("USDC", "USDT"), day_index = 0:59)
  g[, date := format(PRE_PERIOD_START + day_index)]
  g[, balance_units := round(base[match(wallet_id, ids)] * runif(.N, 0.9, 1.1) * 1e6)]   # ~base per wallet-coin
  g[]
}
N <- 1200L; ids <- 0:(N - 1L)
arm <- sample(c(rep("control", 900L), rep(c("withdrawal", "deposit", "both"), 100L)))   # interleaved
base <- exp(rnorm(N, 9, 1)) ; base[arm == "control"] <- base[arm == "control"] * 0.8
issuer <- ids %% 50L == 0L                                  # both groups
bl_usdc <- ids %% 37L == 0L; bl_usdt <- ids %% 41L == 5L     # both groups
mk_cov <- function() data.table(wallet_id = ids, address = sprintf("0x%040x", ids), arm = arm,
  transfer_count_total = rpois(N, 8), eth_balance_wei_units = format(1e18 * rexp(N), scientific = FALSE),
  first_send_block = 1, wallet_age_blocks = ifelse(runif(N) < .1, NA, 1.2e7 - rexp(N, 1e-6)),
  issuer_mint_redeem_flag = ifelse(issuer, "True", "False"),
  blacklisted_usdc_at_pre_end = ifelse(bl_usdc, "True", "False"), blacklisted_usdt_at_pre_end = ifelse(bl_usdt, "True", "False"),
  exchange_share = NA)
fwrite(mk_panel(ids, base), file.path(g4, "wallet_coin_day_pre.csv"))
fwrite(mk_cov(), file.path(g4, "covariates.csv"))
# The June route needs the producer's population manifest (hashes of both CSVs, pinned june_end); fixture dir name = generation id.
fwrite_manifest <- function(d, ..., meta = list(participation_rule = "baseline_transfer_required", participation_window = expected_participation_window(pinned_june_end())),
                            note = "participation_rule=baseline_transfer_required (fixture)") {
  writeLines(jsonlite::toJSON(meta, auto_unbox = TRUE), file.path(d, "meta.json"))
  for (sf in c(JUNE_SCHEMA_FILE, JUNE_PANEL_SCHEMA_FILE))
    writeLines(jsonlite::toJSON(list(sha256 = sha256_file(file.path(d, sub("\\.schema\\.json$", ".csv", sf))), notes = note), auto_unbox = TRUE), file.path(d, sf))
  m <- list(population_id = "june_placebo", generation_id = basename(d), producing_commit = strrep("a", 40), producer_source_sha256 = strrep("e", 16), schema_version = 1L,
            baseline_start_date = "2022-06-09", baseline_end_date = "2022-06-30", excluded_dates = list(), cutoff_block = pinned_june_end(),
            input_file_sha256 = list(`manifest.csv` = strrep("b", 64)),
            output_file_sha256 = setNames(lapply(file.path(d, c(JUNE_REQUIRED_FILES, JUNE_SCHEMA_FILE, JUNE_PANEL_SCHEMA_FILE)), sha256_file), c(JUNE_REQUIRED_FILES, JUNE_SCHEMA_FILE, JUNE_PANEL_SCHEMA_FILE)),
            participation_rule = "baseline_transfer_required", participation_window = expected_participation_window(pinned_june_end()),
            row_counts = list(`covariates.csv` = nrow(fread(file.path(d, "covariates.csv"))),
                              `wallet_coin_day_pre.csv` = nrow(fread(file.path(d, "wallet_coin_day_pre.csv")))))
  jsonlite::write_json(modifyList(m, list(...)), file.path(d, MANIFEST_NAME), auto_unbox = TRUE, pretty = TRUE)
}
test("pre stage end-to-end on S8-shaped fixtures (placebo beta ~ 0, outputs written)", {
  fwrite_manifest(g4)
  tab <- run_pre("three_bins", g4, file.path(tmp, "o"), participation_rule = "baseline_transfer_required")
  stopifnot(nrow(tab) == 9, file.exists(file.path(tmp, "o", "summary.md")),
            all(abs(tab$beta_D) < 1), all(tab$power_h1 >= 0), setequal(tab$cap, CAP_MODES),
            file.exists(file.path(tmp, "o", "prespec_manifest.md")), file.exists(file.path(tmp, "o", "prespec_manifest.json")))
})
# --- June manifest verification: participation window, meta.json, pinned legacy admission, schemas, unlisted files, strict JSON ---
mk_gen <- function(name, ..., mutate = function(d) NULL) {
  d <- file.path(tmp, name); dir.create(d); file.copy(file.path(g4, JUNE_OUTPUT_FILES), d)
  fwrite_manifest(d, ...); mutate(d); d
}
refused <- function(d, pat, rule = "baseline_transfer_required", pin = S21_LEGACY_PIN) {
  e <- try(verify_population_manifest(d, participation_rule = rule, legacy_pin = pin), silent = TRUE)
  stopifnot(inherits(e, "try-error"), grepl(pat, conditionMessage(attr(e, "condition")), fixed = TRUE))
}
accepted <- function(d, rule = "baseline_transfer_required", pin = S21_LEGACY_PIN) {
  v <- verify_population_manifest(d, participation_rule = rule, legacy_pin = pin); unlink(v$dir, recursive = TRUE); v
}
win <- expected_participation_window(pinned_june_end())
test("expected participation window is derived from config/blocks.json + June constants", {
  stopifnot(win$after_block_exclusive == 14929560, win$to_block_inclusive == pinned_june_end(), identical(win$first_date, "2022-06-09"),
            identical(win$last_date, "2022-06-30"), win$day_index_lt == 22)
  accepted(mk_gen("gw-ok"))
})
test("MED-1: manifest without participation_window (intermediate-commit generation) is refused under a recorded rule", {
  refused(mk_gen("gw-none", participation_window = NULL), "manifest participation_window is absent or differs")
})
test("MED-1: manifest participation_window must equal the pinned June pre window, field by field", {
  for (k in names(win)) {
    w <- win; w[[k]] <- if (is.numeric(win[[k]])) win[[k]] + 1 else "2022-01-01"
    refused(mk_gen(paste0("gw-bad-", k), participation_window = w), "manifest participation_window is absent or differs")
  }
  refused(mk_gen("gw-extra", participation_window = c(win, list(extra = 1))), "manifest participation_window is absent or differs")
})
test("MED-1: meta.json must carry the manifest's participation_rule and participation_window", {
  refused(mk_gen("gm-norule", meta = list(participation_window = win)), "meta.json participation_rule")
  refused(mk_gen("gm-badrule", meta = list(participation_rule = "none", participation_window = win)), "meta.json participation_rule")
  refused(mk_gen("gm-nowin", meta = list(participation_rule = "baseline_transfer_required")), "meta.json participation_window")
  w <- win; w$to_block_inclusive <- w$to_block_inclusive + 1
  refused(mk_gen("gm-badwin", meta = list(participation_rule = "baseline_transfer_required", participation_window = w)), "meta.json participation_window")
})
test("MED-1: the window is written into the prespec manifest's population block", {
  fwrite_manifest(g4)
  run_pre("three_bins", g4, file.path(tmp, "o-win"), participation_rule = "baseline_transfer_required")
  mj <- jsonlite::fromJSON(file.path(tmp, "o-win", "prespec_manifest.json"))
  stopifnot(identical(mj$population$participation_window$first_date, "2022-06-09"), mj$population$participation_window$day_index_lt == 22,
            mj$population$participation_window$to_block_inclusive == pinned_june_end())
})
legacy_gen <- function(name, window = NULL, meta = list(frame_sha256 = "x")) mk_gen(name, participation_rule = NULL, participation_window = window, meta = meta, note = "legacy notes")
pin_of <- function(d) modifyList(S21_LEGACY_PIN, list(producing_commit = strrep("a", 40), producer_source_sha256 = strrep("e", 16), manifest_sha256 = sha256_file(file.path(d, MANIFEST_NAME))))
test("MED-2: legacy admission is pinned to the S21 generation: a stripped (non-S21) manifest is refused even under --participation-rule none", {
  d <- legacy_gen("gl-stripped")
  refused(d, "not the pinned S21 generation", rule = "none")                      # real pin: this fixture is not S21
  v <- accepted(d, rule = "none", pin = pin_of(d))                                 # same fixture, pin matching it: only the pin separates the two
  stopifnot(identical(v$manifest$participation_rule, LEGACY_RULE_LABEL))
  refused(d, "legacy generation", rule = "baseline_transfer_required", pin = pin_of(d))
  for (k in c("producing_commit", "producer_source_sha256", "manifest_sha256"))
    refused(d, "not the pinned S21 generation", rule = "none", pin = modifyList(pin_of(d), setNames(list(strrep("0", nchar(S21_LEGACY_PIN[[k]]))), k)))
})
test("MED-2: S21 pin constants are the real S21 generation values", {
  stopifnot(identical(S21_LEGACY_PIN$producing_commit, "2608449912e462d294397fb33d8865da38921318"), identical(S21_LEGACY_PIN$producer_source_sha256, "237370ca815fb06b"),
            identical(S21_LEGACY_PIN$manifest_sha256, "d432b6b6769efc3e4a7c0b097db87b6aa0d1d0ddddbadbc900431b2a8b489db6"))
})
test("MED-2: a legacy generation may carry neither participation_window (manifest) nor participation_rule/window (meta.json)", {
  d <- legacy_gen("gl-win", window = win)
  refused(d, "participation_window but no participation_rule", rule = "none", pin = pin_of(d))
  d <- legacy_gen("gl-meta", meta = list(participation_rule = "none"))
  refused(d, "meta.json does", rule = "none", pin = pin_of(d))
  d <- legacy_gen("gl-meta2", meta = list(participation_window = win))
  refused(d, "meta.json does", rule = "none", pin = pin_of(d))
})
test("LOW-1: duplicate JSON keys (manifest, output_file_sha256 names, meta) are refused", {
  d <- mk_gen("gd-man", mutate = function(d) {
    p <- file.path(d, MANIFEST_NAME); t <- readLines(p)
    i <- grep('"covariates.csv": "[0-9a-f]{64}"', t)[1]; t <- append(t, t[i], after = i); writeLines(t, p) })
  refused(d, "repeats a JSON key")
  d <- mk_gen("gd-meta", mutate = function(d) writeLines('{"participation_rule": "none", "participation_rule": "baseline_transfer_required"}', file.path(d, "meta.json")))
  refused(d, "sha256 differs")                                                      # meta edited after hashing: caught by hash; strict parse is exercised directly:
  writeLines('{"a": 1, "a": 2}', f <- tempfile()); e <- try(pop_read_json(f, "x"), silent = TRUE)
  stopifnot(inherits(e, "try-error"), grepl("repeats a JSON key", conditionMessage(attr(e, "condition"))))
  writeLines('{"a": NaN}', f); stopifnot(inherits(try(pop_read_json(f, "x"), silent = TRUE), "try-error"))
})
test("LOW-2: wallet_coin_day_pre.schema.json is hashed, tied to its csv and carries the rule note", {
  refused(mk_gen("gs-nofile", mutate = function(d) {
    p <- file.path(d, MANIFEST_NAME); m <- jsonlite::fromJSON(p, simplifyVector = FALSE); m$output_file_sha256[[JUNE_PANEL_SCHEMA_FILE]] <- NULL
    jsonlite::write_json(m, p, auto_unbox = TRUE) }), paste("output_file_sha256 lacks", JUNE_PANEL_SCHEMA_FILE))
  refused(mk_gen("gs-hash", mutate = function(d) writeLines('{"sha256": "x", "notes": "n"}', file.path(d, JUNE_PANEL_SCHEMA_FILE))), "sha256 differs from manifest")
  refused(mk_gen("gs-csv", mutate = function(d) {
    sf <- file.path(d, JUNE_PANEL_SCHEMA_FILE); writeLines(jsonlite::toJSON(list(sha256 = strrep("0", 64), notes = "participation_rule=baseline_transfer_required"), auto_unbox = TRUE), sf)
    p <- file.path(d, MANIFEST_NAME); m <- jsonlite::fromJSON(p, simplifyVector = FALSE); m$output_file_sha256[[JUNE_PANEL_SCHEMA_FILE]] <- sha256_file(sf)
    jsonlite::write_json(m, p, auto_unbox = TRUE) }), paste(JUNE_PANEL_SCHEMA_FILE, "sha256 does not match wallet_coin_day_pre.csv"))
  refused(mk_gen("gs-rule", mutate = function(d) {
    sf <- file.path(d, JUNE_PANEL_SCHEMA_FILE); writeLines(jsonlite::toJSON(list(sha256 = sha256_file(file.path(d, "wallet_coin_day_pre.csv")), notes = "participation_rule=none"), auto_unbox = TRUE), sf)
    p <- file.path(d, MANIFEST_NAME); m <- jsonlite::fromJSON(p, simplifyVector = FALSE); m$output_file_sha256[[JUNE_PANEL_SCHEMA_FILE]] <- sha256_file(sf)
    jsonlite::write_json(m, p, auto_unbox = TRUE) }), paste("recorded in", JUNE_PANEL_SCHEMA_FILE))
})
test("LOW-2: files in the generation dir that the manifest does not list are refused", {
  refused(mk_gen("gu-extra", mutate = function(d) writeLines("x", file.path(d, "stray.txt"))), "not listed in output_file_sha256: stray.txt")
})
test("single id space: panel and covariates join on the right wallet (interleaved ids)", {
  inp <- load_pre_inputs(g4)
  f <- pre_match_frame(inp)
  # panel balance per wallet is ~base_i USDC + ~base_i USDT (+-10% noise) -> mean_hold == 2*base_i, per wallet_id
  stopifnot(max(abs(f$mean_hold / (2 * base[f$uid + 1L]) - 1)) < 0.1,
            identical(f$treat, arm[f$uid + 1L] != "control"))
})
test("blacklist filter and zero-address issuer screen drop flagged wallets in BOTH groups (on by default), counted", {
  inp <- load_pre_inputs(g4)
  raw <- pre_match_frame(inp, drop_issuer = FALSE, drop_blacklisted = FALSE)
  full <- pre_match_frame(inp)                                    # defaults: both screens ON
  nb_e <- raw[treat == TRUE & blacklisted == TRUE, .N]; nb_c <- raw[treat == FALSE & blacklisted == TRUE, .N]
  after_bl <- raw[blacklisted == FALSE]
  ni_e <- after_bl[treat == TRUE & issuer == TRUE, .N]; ni_c <- after_bl[treat == FALSE & issuer == TRUE, .N]
  fn <- attr(full, "funnel")
  stopifnot(nb_e > 0, nb_c > 0, ni_e > 0, ni_c > 0,
            fn$blacklisted_dropped[["exposed"]] == nb_e, fn$blacklisted_dropped[["control"]] == nb_c,
            fn$issuer_dropped[["exposed"]] == ni_e, fn$issuer_dropped[["control"]] == ni_c,
            !any(full$blacklisted), !any(full$issuer), nrow(raw) - nrow(full) == nb_e + nb_c + ni_e + ni_c,
            nrow(pre_match_frame(inp, drop_issuer = FALSE)) == nrow(raw) - nb_e - nb_c)
})
test("single id space still holds after screens (interleaved ids)", {
  f <- pre_match_frame(load_pre_inputs(g4))
  stopifnot(max(abs(f$mean_hold / (2 * base[f$uid + 1L]) - 1)) < 0.1)
})
test("USDC share = ratio of sums; exact 0 / 1 only when a leg sum is zero", {
  w <- rbind(CJ(uid = 1:3, day_index = JUNE_DAYS)[, `:=`(usdc_units = c(1e9, 0, 5e8)[uid], usdt_units = c(0, 1e9, 5e8)[uid])])
  w[uid == 3 & day_index == 0, `:=`(usdc_units = 1e12, usdt_units = 1)]   # mean of daily ratios would differ from ratio of sums
  cvr <- data.table(wallet_id = 1:3, address = c("a", "b", "c"), arm = c("withdrawal", "control", "control"),
    transfer_count_total = 1, eth_balance_wei_units = "1", wallet_age_blocks = 1, issuer_mint_redeem_flag = "False",
    blacklisted_usdc_at_pre_end = "False", blacklisted_usdt_at_pre_end = "False")
  f <- pre_match_frame(list(g4cov = cvr, wide = w))
  su <- setNames(f$usdc_share, f$uid)
  stopifnot(su[["1"]] == 1, su[["2"]] == 0, su[["3"]] > 0, su[["3"]] < 1,
            abs(su[["3"]] - sum(w[uid == 3, usdc_units]) / sum(w[uid == 3, usdc_units + usdt_units])) < 1e-9)
})
test("June floors asserted for the June population (strict) and applied for the dev population", {
  inp <- load_pre_inputs(g4)
  inp2 <- list(g4cov = inp$g4cov, wide = copy(inp$wide))
  inp2$wide[uid == 3L & day_index %in% JUNE_DAYS, `:=`(usdc_units = 0, usdt_units = 0)]
  e <- try(pre_match_frame(inp2, strict_floors = TRUE), silent = TRUE)
  stopifnot(inherits(e, "try-error"), grepl("June floors", conditionMessage(attr(e, "condition"))))
  f <- pre_match_frame(inp2, strict_floors = FALSE)
  stopifnot(!(3L %in% f$uid), attr(f, "funnel")$june_floor_violations[["exposed"]] + attr(f, "funnel")$june_floor_violations[["control"]] == 1)
})
test("placebo population: legacy g4_june and manifest-less June fail loudly (no fallback to g4); g4 only on explicit request", {
  e1 <- try(run_pre("none", g4, file.path(tmp, "o-leg"), placebo_pop = "g4_june"), silent = TRUE)
  stopifnot(inherits(e1, "try-error"), grepl("legacy", conditionMessage(attr(e1, "condition"))))
  nm <- file.path(tmp, "gen-nomanifest"); dir.create(nm); file.copy(file.path(g4, JUNE_OUTPUT_FILES), nm)
  e2 <- try(run_pre("none", nm, file.path(tmp, "o-nm"), participation_rule = "baseline_transfer_required"), silent = TRUE)
  stopifnot(inherits(e2, "try-error"), grepl("population_manifest.json missing", conditionMessage(attr(e2, "condition"))), !dir.exists(file.path(tmp, "o-nm")))
})
test("manifest: dev population labelled, enlargement fires, cut-points/caps/seeds/hashes recorded", {
  g0 <- file.path(tmp, "g0"); dir.create(g0)    # synthetic event history: one USDC listing inside the window
  ev0 <- data.table(coin = "USDC", address = sprintf("0x%040x", 3L), event = "Blacklisted", block = PRE_START_BLOCK + 1L, log_index = 0L)
  fwrite(ev0, file.path(g0, "blacklist_events_predeploy_ext.csv"))
  fwrite(ev0[0], file.path(g0, "circle_events.csv")); fwrite(ev0[0], file.path(g0, "usdt_events.csv"))
  res <- run_pre("three_bins", g4, file.path(tmp, "o2"), placebo_pop = "g4", g0dir = g0)
  mj <- jsonlite::fromJSON(file.path(tmp, "o2", "prespec_manifest.json"))
  stopifnot(isTRUE(mj$development_diagnostic), mj$enlargement$decision == "fires",
            mj$seeds$primary_cem == 20261015, mj$seeds$june_placebo_frame_used_by_gate == 20251015, mj$seeds$enlargement == 20251016,
            identical(mj$rng_kind, c("Mersenne-Twister", "Inversion", "Rejection")),
            nchar(mj$input_sha256$covariates.csv) == 64, length(mj$caps$D) == 2, length(mj$caps$A) == 2, length(mj$caps$legs_pooled) == 2,
            all(c("mean_hold", "transfers", "age", "eth") %in% names(mj$cem$cutpoints)),
            abs(mj$power$power - power_normal(mj$power$se)) < 1e-9, mj$power$se >= max(mj$power$se_wallet, mj$power$se_stratum) - 1e-12)
  txt <- paste(readLines(file.path(tmp, "o2", "prespec_manifest.md")), collapse = "\n")
  stopifnot(grepl("DEVELOPMENT DIAGNOSTIC", txt), grepl("Enlargement decision", txt))
})
test("guard: covariate ids without panel rows (and vice versa) abort", {
  d2 <- file.path(tmp, "g4b"); dir.create(d2)
  p <- fread(file.path(g4, "wallet_coin_day_pre.csv"))
  fwrite(p[wallet_id != 7L], file.path(d2, "wallet_coin_day_pre.csv"))
  fwrite(mk_cov(), file.path(d2, "covariates.csv"))
  e <- try(load_pre_inputs(d2), silent = TRUE)
  stopifnot(inherits(e, "try-error"), grepl("wallet_id mismatch", conditionMessage(attr(e, "condition"))))
  fwrite(p, file.path(d2, "wallet_coin_day_pre.csv")); fwrite(mk_cov()[wallet_id != 9L], file.path(d2, "covariates.csv"))
  e <- try(load_pre_inputs(d2), silent = TRUE)
  stopifnot(inherits(e, "try-error"), grepl("wallet_id mismatch", conditionMessage(attr(e, "condition"))))
})
test("required covariate column missing (incl. issuer flag, blacklist flags) fails loudly", {
  d2 <- file.path(tmp, "g4c"); dir.create(d2)
  file.copy(file.path(g4, "wallet_coin_day_pre.csv"), d2)
  fwrite(mk_cov()[, !"issuer_mint_redeem_flag"], file.path(d2, "covariates.csv"))
  e <- try(load_pre_inputs(d2), silent = TRUE)
  stopifnot(inherits(e, "try-error"), grepl("issuer_mint_redeem_flag", conditionMessage(attr(e, "condition"))))
  fwrite(mk_cov()[, !"blacklisted_usdc_at_pre_end"], file.path(d2, "covariates.csv"))
  e <- try(load_pre_inputs(d2), silent = TRUE)
  stopifnot(inherits(e, "try-error"), grepl("blacklisted_usdc_at_pre_end", conditionMessage(attr(e, "condition"))))
})
test("panel guards: duplicate (wallet_id, coin, day_index) key and empty balance_units abort", {
  d2 <- file.path(tmp, "g4d"); dir.create(d2)
  fwrite(mk_cov(), file.path(d2, "covariates.csv"))
  p <- fread(file.path(g4, "wallet_coin_day_pre.csv"))
  fwrite(rbind(p, p[1]), file.path(d2, "wallet_coin_day_pre.csv"))
  e <- try(load_pre_inputs(d2), silent = TRUE)
  stopifnot(inherits(e, "try-error"), grepl("duplicate", conditionMessage(attr(e, "condition"))))
  p2 <- copy(p); p2[5, balance_units := NA_real_]
  fwrite(p2, file.path(d2, "wallet_coin_day_pre.csv"))
  e <- try(load_pre_inputs(d2), silent = TRUE)
  stopifnot(inherits(e, "try-error"), grepl("balance_units", conditionMessage(attr(e, "condition"))))
})

test("blacklist window replaces end-of-baseline flags in pre_match_frame; NULL keeps the population's own flags", {
  inp <- load_pre_inputs(g4)
  a <- pre_match_frame(inp, drop_issuer = FALSE, drop_blacklisted = FALSE)
  keep <- a[blacklisted == FALSE][1, address]
  b <- pre_match_frame(inp, drop_issuer = FALSE, drop_blacklisted = FALSE, blacklist_any = keep)
  stopifnot(identical(b[blacklisted == TRUE, address], keep))
})
