# Pre-event placebo stage (real data, pre-period only). HARD RULE: nothing after 2022-08-07 is read.
# Fake event 2022-07-09 (day_index 30, excluded like the real 8 Aug); post = 10 Jul..7 Aug (29 days);
# matching on June data only (day_index 0..21 = 9..30 Jun).
PLACEBO_EVENT_DAY <- as.integer(as.Date("2022-07-09") - PRE_PERIOD_START)   # 30
JUNE_DAYS <- 0:as.integer(as.Date("2022-06-30") - PRE_PERIOD_START)         # 0..21
PRE_LAST_DAY <- as.integer(LAST_ALLOWED_DATE - PRE_PERIOD_START)            # 59

# S8 output schema (ground truth: gates/g4_controls.py cov_cols / panel_cols, ~l.770-850). Both arms' panel AND
# covariates come from g4 only: one wallet_id space (dense by sorted address over controls + exposed floor-passers,
# g4_controls.py:639; panel written for all `passers` of both arms, :832). Every column here is REQUIRED; none is optional.
# Blacklist flags (prespec §1 baseline filter) and `address` (CEM tie-break) are required too. The June population
# (out/recovery/june/<generation>) must carry the SAME columns; its blacklist flags are as of 30 Jun.
G4_COVARIATE_COLS <- c("wallet_id", "address", "arm", "transfer_count_total", "eth_balance_wei_units",
                       "wallet_age_blocks", "issuer_mint_redeem_flag",
                       "blacklisted_usdc_at_pre_end", "blacklisted_usdt_at_pre_end")
G4_PANEL_COLS <- c("wallet_id", "coin", "date", "day_index", "balance_units")
G4_ARMS <- c("control", "withdrawal", "deposit", "both")
as_flag <- function(x) as.character(x) %in% c("True", "TRUE", "true", "1")

blocked_on_s8 <- function(msg) stop(paste0(
  "\n*** PRE STAGE BLOCKED ON S8 (gate 4 control panel + covariates) ***\n", msg,
  "\nNothing was fabricated; nothing was read. Run the routed `python3 run_all.py --gate g4 --generation NAME --baseline-blacklist-dir DIR --input-dir ROOT` once S8 lands, then re-run this stage.\n"),
  call. = FALSE)

read_wallet_coin_day <- function(path, what) {
  if (!file.exists(path)) blocked_on_s8(sprintf("missing %s (%s)", path, what))
  hdr <- names(fread(path, nrows = 0))
  miss <- setdiff(G4_PANEL_COLS, hdr)
  if (length(miss)) stop(sprintf("%s lacks columns: %s", path, paste(miss, collapse = ", ")))
  d <- fread(path, select = G4_PANEL_COLS, colClasses = c(balance_units = "numeric"))
  assert_no_post_data(d, path)
  if (anyNA(d$balance_units)) stop(sprintf("%s: empty/NA values in balance_units", path))
  if (anyDuplicated(d, by = c("wallet_id", "coin", "day_index")))
    stop(sprintf("%s: duplicate (wallet_id, coin, day_index) rows", path))
  d
}

to_wide <- function(cd) {
  w <- dcast(cd[, .(uid = wallet_id, day_index, coin, balance_units)],
             uid + day_index ~ coin, value.var = "balance_units", fill = 0)
  setnames(w, c("USDC", "USDT"), c("usdc_units", "usdt_units"))
  w
}

load_pre_inputs <- function(g4dir) {
  cov_path <- file.path(g4dir, "covariates.csv")
  if (!file.exists(cov_path)) blocked_on_s8(sprintf("missing %s", cov_path))
  hdr <- names(fread(cov_path, nrows = 0))
  miss <- setdiff(G4_COVARIATE_COLS, hdr)
  if (length(miss)) stop(sprintf("%s lacks required columns: %s (S8 schema, see G4_COVARIATE_COLS in R/pre.R)",
                                 cov_path, paste(miss, collapse = ", ")))
  # wei can exceed integer64 range in principle: read as text, convert explicitly
  g4cov <- fread(cov_path, select = G4_COVARIATE_COLS,
                 colClasses = c(eth_balance_wei_units = "character", issuer_mint_redeem_flag = "character",
                                blacklisted_usdc_at_pre_end = "character", blacklisted_usdt_at_pre_end = "character",
                                wallet_age_blocks = "numeric"))
  if (anyDuplicated(g4cov$wallet_id)) stop("covariates.csv: duplicate wallet_id")
  bad <- setdiff(unique(g4cov$arm), G4_ARMS)
  if (length(bad)) stop(sprintf("covariates.csv: unknown arm value(s): %s", paste(bad, collapse = ", ")))
  if (!any(g4cov$arm != "control") || !any(g4cov$arm == "control")) stop("covariates.csv needs both control and exposed arms")
  for (cn in c("transfer_count_total", "eth_balance_wei_units", "issuer_mint_redeem_flag",
              "blacklisted_usdc_at_pre_end", "blacklisted_usdt_at_pre_end"))
    if (anyNA(g4cov[[cn]]) || any(g4cov[[cn]] == "")) stop(sprintf("covariates.csv: empty values in %s", cn))
  panel <- read_wallet_coin_day(file.path(g4dir, "wallet_coin_day_pre.csv"), "g4 baseline panel, both arms")
  # single id space: covariate ids and panel ids must coincide exactly
  only_cov <- setdiff(g4cov$wallet_id, panel$wallet_id); only_pan <- setdiff(panel$wallet_id, g4cov$wallet_id)
  if (length(only_cov) || length(only_pan))
    stop(sprintf("wallet_id mismatch between covariates.csv and wallet_coin_day_pre.csv: %d covariate ids without panel rows, %d panel ids without covariates",
                 length(only_cov), length(only_pan)))
  list(g4cov = g4cov, wide = to_wide(panel))
}

# Placebo population routing (SPEC S10c, S20a). `june` = June-only rebuild, manifest-verified before any CSV row is read
# (no default directory, no `current` pointer, no fallback to the look-ahead g4 population). `g4` is an explicit
# development diagnostic only.
PLACEBO_POPS <- c("june", "g4")
JUNE_POPULATION_ID <- "june_placebo"
JUNE_BASELINE_START <- "2022-06-09"; JUNE_BASELINE_END <- "2022-06-30"
JUNE_OUTPUT_FILES <- c("covariates.csv", "wallet_coin_day_pre.csv")   # row_counts are required for these
JUNE_REQUIRED_FILES <- c(JUNE_OUTPUT_FILES, "meta.json")                # every one is hashed against the manifest
PRE_R_ROOT <- normalizePath(getwd())                                    # repo root (run_all.R / run_tests.R setwd there before sourcing)
MANIFEST_NAME <- "population_manifest.json"
LEGACY_RULE_LABEL <- "none (legacy, not recorded at production)"          # recorded in R outputs when an unrecorded-rule generation is accepted under --participation-rule none
JUNE_PANEL_SCHEMA_FILE <- "wallet_coin_day_pre.schema.json"             # panel schema: hashed, tied to its csv, rule note cross-checked
JUNE_SCHEMA_FILE <- "covariates.schema.json"                            # carries `participation_rule=<rule>` in its notes (as g1's cohort.schema.json); hashed + cross-checked
PARTICIPATION_RULES <- c("baseline_transfer_required", "none")          # = gates/g1_cohort.PARTICIPATION_RULES (shared by the June route and the main presign)

pop_fail <- function(...) stop(paste0("\n*** JUNE POPULATION REJECTED (before any CSV load): ", sprintf(...), " ***\n"), call. = FALSE)
is_hex <- function(x, n) is.character(x) && length(x) == 1L && !is.na(x) && grepl(sprintf("^[0-9a-f]{%d}$", n), x)
as_whole <- function(x) if (is.numeric(x) && length(x) == 1L && !is.na(x) && x == floor(x)) x else NA_real_

# June block bound from config/blocks.json (pinned june_end). Absent file/key is a hard stop, never a default.
pinned_june_end <- function(blocks_path = file.path(PRE_R_ROOT, "config", "blocks.json")) {
  if (!file.exists(blocks_path)) pop_fail("pinned blocks file %s missing (cannot attest the upper block)", blocks_path)
  b <- jsonlite::fromJSON(blocks_path, simplifyVector = FALSE)
  je <- as_whole(b$june_end$block)
  if (is.na(je)) pop_fail("%s has no numeric june_end.block", blocks_path)
  je
}

# Rejects an input dir that is not an explicit immutable generation: any component that is a symlink, `..`, or named
# `current` (checked on the UNRESOLVED absolute path, so a pointer anywhere in the chain is caught).
check_generation_path <- function(input_dir) {
  ap <- path.expand(sub("/+$", "", input_dir))
  if (!startsWith(ap, "/")) ap <- file.path(getwd(), ap)
  comps <- strsplit(ap, "/")[[1]]; comps <- comps[nzchar(comps) & comps != "."]
  if (".." %in% comps) pop_fail("%s contains `..`", input_dir)
  if (tail(comps, 1) == "current" || "current" %in% comps) pop_fail("%s goes through a `current` pointer; pass the immutable generation directory", input_dir)
  for (i in seq_along(comps)) if (nzchar(Sys.readlink(paste0("/", paste(comps[1:i], collapse = "/")))))
    pop_fail("%s: component /%s is a symlink; pass the real immutable generation directory", input_dir, paste(comps[1:i], collapse = "/"))
  invisible(ap)
}

# Copies `name` from the generation into the private dir `priv` (regular non-symlink file only) and returns the copy.
private_copy <- function(input_dir, name, priv) {
  src <- file.path(input_dir, name)
  if (!file.exists(src)) pop_fail("%s missing", src)
  if (nzchar(Sys.readlink(src)) || !isTRUE(file.info(src)$isdir == FALSE)) pop_fail("%s is not a regular (non-symlink) file", src)
  dst <- file.path(priv, name)
  if (!file.copy(src, dst, overwrite = FALSE)) pop_fail("could not copy %s", src)
  dst
}

# Legacy admission is a PIN, not evidence-by-absence: the one generation produced before the participation rule was recorded is the
# S21 generation (gen-20261023-s21). A manifest without `participation_rule` is accepted ONLY if it is exactly that generation.
S21_LEGACY_PIN <- list(producing_commit = "2608449912e462d294397fb33d8865da38921318",
                       producer_source_sha256 = "237370ca815fb06b",
                       manifest_sha256 = "d432b6b6769efc3e4a7c0b097db87b6aa0d1d0ddddbadbc900431b2a8b489db6")   # sha256 of its population_manifest.json

# Strict signed-JSON read (W2: R/post_gate.R pg_read_json / pg_dupkeys): UTF-8, no BOM, no NaN/Inf, no duplicate key at any depth; must be an object.
pop_read_json <- function(path, what) {
  x <- tryCatch(pg_read_json(path, what), error = function(e) pop_fail("%s unreadable: %s", what, trimws(gsub("\\*+|POST STAGE DENIED:", "", conditionMessage(e)))))
  if (!is.list(x) || is.null(names(x))) pop_fail("%s is not a JSON object", what)
  if (pg_dupkeys(x)) pop_fail("%s repeats a JSON key", what)
  x
}

# Exact participation window the producer (gates/g4_june.py participation_window) records: blocks (pre_period_start, june_end], day indexes
# [0, 22) = 9..30 June. Derived from the pinned blocks file + the June constants here, never hand-typed.
expected_participation_window <- function(june_end, blocks_path = file.path(PRE_R_ROOT, "config", "blocks.json")) {
  if (!file.exists(blocks_path)) pop_fail("pinned blocks file %s missing (cannot derive the participation window)", blocks_path)
  ps <- as_whole(jsonlite::fromJSON(blocks_path, simplifyVector = FALSE)[["pre_period_start"]][["block"]])
  if (is.na(ps)) pop_fail("%s has no numeric pre_period_start.block", blocks_path)
  list(after_block_exclusive = ps, to_block_inclusive = june_end, first_date = JUNE_BASELINE_START, last_date = JUNE_BASELINE_END, day_index_lt = length(JUNE_DAYS))
}
same_window <- function(a, b) {
  if (!is.list(a) || !is.list(b) || is.null(names(a)) || is.null(names(b)) || anyDuplicated(names(a)) || !setequal(names(a), names(b))) return(FALSE)
  all(vapply(names(b), function(k) {
    x <- a[[k, exact = TRUE]]; y <- b[[k, exact = TRUE]]
    if (is.numeric(y)) is.numeric(x) && length(x) == 1L && !is.na(as_whole(x)) && as_whole(x) == y else identical(x, y)
  }, TRUE))
}

# Verifies the June population manifest in `input_dir`. TOCTOU-safe: the manifest and every listed file are copied ONCE
# into a private temp dir (`$dir`, removed by the caller via unlink), hashed THERE, compared with the manifest, and the
# caller must read only those copies. No CSV body is parsed here. Contract: DATA-CONTRACT `population_manifest`; every
# field is required, absence is rejection, never an omitted check. `legacy_pin` is injectable for tests only.
verify_population_manifest <- function(input_dir, population_id = JUNE_POPULATION_ID, june_end = pinned_june_end(), participation_rule = NULL,
                                       legacy_pin = S21_LEGACY_PIN) {
  if (!is.character(participation_rule) || length(participation_rule) != 1L || !participation_rule %in% PARTICIPATION_RULES)
    pop_fail("--participation-rule must be one of %s (unresolved design decision; required, never defaulted)", paste(PARTICIPATION_RULES, collapse = ", "))
  if (missing(input_dir) || !is.character(input_dir) || length(input_dir) != 1L || is.na(input_dir) || !nzchar(input_dir))
    pop_fail("input dir must be given explicitly (no current/default population)")
  if (!identical(population_id, JUNE_POPULATION_ID)) pop_fail("population_id must be '%s' (got '%s')", JUNE_POPULATION_ID, population_id)
  if (!dir.exists(input_dir)) pop_fail("input dir %s does not exist", input_dir)
  check_generation_path(input_dir)
  priv <- tempfile("june-verified-"); dir.create(priv, mode = "0700")
  ok <- FALSE; on.exit(if (!ok) unlink(priv, recursive = TRUE), add = TRUE)
  mpath <- private_copy(input_dir, MANIFEST_NAME, priv)
  m <- pop_read_json(mpath, MANIFEST_NAME)
  need <- c("population_id", "generation_id", "producing_commit", "producer_source_sha256", "baseline_start_date", "baseline_end_date",
            "excluded_dates", "cutoff_block", "input_file_sha256", "output_file_sha256", "row_counts", "schema_version")
  legacy <- !"participation_rule" %in% names(m)   # generation produced before the rule was recorded: admitted only as the pinned S21 generation (below)
  if (legacy && !identical(participation_rule, "none"))
    pop_fail("manifest records no participation_rule (legacy generation); it is accepted ONLY with --participation-rule none, got '%s'; run stopped", participation_rule)
  miss <- setdiff(need, names(m))
  if (length(miss)) pop_fail("manifest lacks: %s", paste(miss, collapse = ", "))
  if (!identical(m[["population_id"]], population_id)) pop_fail("manifest population_id '%s' != requested '%s'", format(m[["population_id"]]), population_id)
  gen <- m[["generation_id"]]
  if (!is.character(gen) || length(gen) != 1L || !nzchar(gen)) pop_fail("manifest generation_id empty")
  if (!identical(gen, basename(sub("/+$", "", input_dir)))) pop_fail("manifest generation_id '%s' != input dir name '%s'", gen, basename(sub("/+$", "", input_dir)))
  if (!is_hex(m[["producing_commit"]], 40)) pop_fail("producing_commit %s is not a 40-hex commit sha", format(m[["producing_commit"]]))
  if (!is_hex(m[["producer_source_sha256"]], 16)) pop_fail("producer_source_sha256 %s is not 16-hex", format(m[["producer_source_sha256"]]))
  if (is.na(as_whole(m[["schema_version"]]))) pop_fail("schema_version missing/non-numeric")
  if (!identical(m[["baseline_start_date"]], JUNE_BASELINE_START) || !identical(m[["baseline_end_date"]], JUNE_BASELINE_END))
    pop_fail("baseline window must be %s..%s (got %s..%s)", JUNE_BASELINE_START, JUNE_BASELINE_END, format(m[["baseline_start_date"]]), format(m[["baseline_end_date"]]))
  cb <- as_whole(m[["cutoff_block"]])
  if (is.na(cb)) pop_fail("cutoff_block missing/non-integer")
  if (cb != june_end) pop_fail("cutoff_block %.0f != pinned june_end %.0f (upper-block attestation: must be <= june_end and is required to equal it)", cb, june_end)
  if (legacy) {
    if (!identical(m[["producing_commit"]], legacy_pin$producing_commit) || !identical(m[["producer_source_sha256"]], legacy_pin$producer_source_sha256) ||
        !identical(sha256_file(mpath), legacy_pin$manifest_sha256))
      pop_fail("manifest records no participation_rule and is not the pinned S21 generation (producing_commit / producer_source_sha256 / manifest sha256); legacy admission refused; run stopped")
    if ("participation_window" %in% names(m)) pop_fail("manifest has participation_window but no participation_rule; not a legacy generation; run stopped")
  } else {
    if (!identical(m[["participation_rule"]], participation_rule))
      pop_fail("participation rule mismatch: --participation-rule '%s' != '%s' recorded in the manifest; run stopped", participation_rule, format(m[["participation_rule"]]))
    exp_w <- expected_participation_window(june_end)
    if (!same_window(m[["participation_window"]], exp_w))
      pop_fail("manifest participation_window is absent or differs from the pinned June pre window (%s); run stopped", jsonlite::toJSON(exp_w, auto_unbox = TRUE))
  }
  hashes <- function(x, what) {
    if (!is.list(x) || !length(x) || is.null(names(x)) || any(!nzchar(names(x)))) pop_fail("%s must be a non-empty name -> sha256 map", what)
    h <- vapply(x, function(v) if (is_hex(v, 64)) v else NA_character_, "")
    if (anyNA(h)) pop_fail("%s has non-sha256 entries: %s", what, paste(names(h)[is.na(h)], collapse = ", "))
    h
  }
  in_h <- hashes(m[["input_file_sha256"]], "input_file_sha256")
  out_h <- hashes(m[["output_file_sha256"]], "output_file_sha256")
  if (anyDuplicated(names(out_h)) || any(grepl("/", names(out_h), fixed = TRUE) | names(out_h) %in% c(".", "..", MANIFEST_NAME)))
    pop_fail("output_file_sha256 has an invalid or duplicate entry name")
  if (length(setdiff(c(JUNE_REQUIRED_FILES, JUNE_SCHEMA_FILE, JUNE_PANEL_SCHEMA_FILE), names(out_h))))
    pop_fail("output_file_sha256 lacks %s", paste(setdiff(c(JUNE_REQUIRED_FILES, JUNE_SCHEMA_FILE, JUNE_PANEL_SCHEMA_FILE), names(out_h)), collapse = ", "))
  unlisted <- setdiff(list.files(input_dir, all.files = TRUE, no.. = TRUE), c(names(out_h), MANIFEST_NAME))
  if (length(unlisted)) pop_fail("generation holds files not listed in output_file_sha256: %s", paste(unlisted, collapse = ", "))
  for (f in names(out_h)) {
    cp <- private_copy(input_dir, f, priv)
    if (!identical(sha256_file(cp), unname(out_h[[f]]))) pop_fail("%s sha256 differs from manifest output_file_sha256", f)
  }
  # meta.json (hashed above) must carry the same rule + window as the manifest; a legacy generation carries neither
  meta <- pop_read_json(file.path(priv, "meta.json"), "meta.json")
  if (legacy) {
    if (any(c("participation_rule", "participation_window") %in% names(meta))) pop_fail("manifest records no participation_rule but meta.json does; inconsistent generation; run stopped")
  } else {
    if (!identical(meta[["participation_rule"]], m[["participation_rule"]]))
      pop_fail("meta.json participation_rule '%s' != manifest '%s'; run stopped", format(meta[["participation_rule"]]), format(m[["participation_rule"]]))
    if (!same_window(meta[["participation_window"]], m[["participation_window"]]))
      pop_fail("meta.json participation_window is absent or differs from the manifest's; run stopped")
  }
  # participation rule: CLI == manifest == both schemas' notes (the producer recorded it in each); each schema must belong to its csv
  check_schema <- function(sf, csv) {
    sc <- pop_read_json(file.path(priv, sf), sf)
    if (!identical(sc[["sha256"]], unname(out_h[[csv]]))) pop_fail("%s sha256 does not match %s; run stopped", sf, csv)
    if (!is.character(sc[["notes"]]) || length(sc[["notes"]]) != 1L) pop_fail("%s carries no `notes` string; participation_rule cannot be read", sf)
    rec <- unique(regmatches(sc[["notes"]], gregexpr("\\bparticipation_rule=[A-Za-z_-]+", sc[["notes"]]))[[1]])
    if (legacy) {   # nothing may be recorded anywhere for a legacy generation
      if (length(rec)) pop_fail("manifest records no participation_rule but %s notes do (%s); inconsistent generation; run stopped", sf, paste(rec, collapse = ", "))
    } else {
      if (length(rec) != 1L) pop_fail("%s notes must record exactly one participation_rule=<rule>", sf)
      rec <- sub("^participation_rule=", "", rec)
      if (!identical(rec, participation_rule))
        pop_fail("participation rule mismatch: --participation-rule '%s' != '%s' recorded in %s; run stopped", participation_rule, rec, sf)
    }
  }
  check_schema(JUNE_SCHEMA_FILE, "covariates.csv")
  check_schema(JUNE_PANEL_SCHEMA_FILE, "wallet_coin_day_pre.csv")
  if (legacy) { m[["participation_rule"]] <- LEGACY_RULE_LABEL; m[["participation_window"]] <- LEGACY_RULE_LABEL }
  rc <- m[["row_counts"]]
  rcn <- vapply(JUNE_OUTPUT_FILES, function(f) if (is.list(rc) && !is.null(rc[[f, exact = TRUE]])) as_whole(rc[[f, exact = TRUE]]) else NA_real_, 0)
  if (anyNA(rcn)) pop_fail("row_counts must give a whole number for %s", paste(JUNE_OUTPUT_FILES, collapse = ", "))
  ok <- TRUE
  list(manifest = m, dir = priv, input_sha256 = in_h, output_sha256 = out_h[c(JUNE_REQUIRED_FILES, JUNE_SCHEMA_FILE, JUNE_PANEL_SCHEMA_FILE)], row_counts = rcn,
       manifest_sha256 = sha256_file(mpath), june_end = june_end)
}

# Explicit, immutable output directory: must be given, and must not already hold files.
check_pre_outdir <- function(outdir) {
  if (missing(outdir) || !is.character(outdir) || length(outdir) != 1L || is.na(outdir) || !nzchar(outdir)) stop("outdir must be given explicitly", call. = FALSE)
  if (dir.exists(outdir) && length(list.files(outdir, all.files = TRUE, no.. = TRUE)))
    stop(sprintf("outdir %s is not empty; June placebo outputs are immutable (use a new directory)", outdir), call. = FALSE)
  invisible(TRUE)
}
# June frame from the panel + non-holdings covariates (prespec §1, §5 item 2).
#  - holdings, USDC share (ratio of baseline sums over the June days) recomputed from the June panel days;
#  - June floors (mean >= $100, >= 6 of 22 nonzero June days): applied here; for the June population they must already
#    hold (gate applies them) -> any violation STOPS the run (strict_floors);
#  - baseline blacklist filter and zero-address issuer screen: drop flagged wallets in BOTH groups, counts per group.
#  - blacklist_any: NULL = use the covariates' end-of-baseline flags (June population: its own any-time-June flags);
#    otherwise a character vector of lower-case addresses blacklisted at any time in [14,929,560, 15,298,282]
#    (blacklisted_any_in_window), which REPLACES the flags (main Jun-Aug population).
pre_match_frame <- function(inp, drop_issuer = TRUE, strict_floors = FALSE, drop_blacklisted = TRUE, blacklist_any = NULL) {
  w <- inp$wide[day_index %in% JUNE_DAYS]
  june <- w[, .(usdc_sum = sum(usdc_units), usdt_sum = sum(usdt_units),
                mean_hold = mean(usdc_units + usdt_units) / UNITS_PER_DOLLAR,
                nz_days = sum(usdc_units + usdt_units > 0), n_days = .N), by = uid]
  if (any(june$n_days != length(JUNE_DAYS))) stop("pre_match_frame: panel lacks the 22 June days for some wallet")
  # USDC share = ratio of baseline sums. Exactly 0 / 1 only when a leg sum is exactly zero (bins classified before rounding).
  june[, usdc_share := usdc_sum / pmax(usdc_sum + usdt_sum, 1)]
  june[usdt_sum == 0 & usdc_sum > 0, usdc_share := 1]
  june[usdc_sum == 0, usdc_share := 0]
  june[usdc_sum > 0 & usdt_sum > 0, usdc_share := pmin(pmax(usdc_share, 1e-15), 1 - 1e-15)]
  cv <- copy(inp$g4cov); setnames(cv, "wallet_id", "uid")
  f <- merge(cv, june, by = "uid")
  if (nrow(f) != nrow(cv)) stop("pre_match_frame: covariate wallets lost in merge with June panel")
  f[, `:=`(treat = arm != "control", issuer = as_flag(issuer_mint_redeem_flag),
           blacklisted = if (is.null(blacklist_any)) as_flag(blacklisted_usdc_at_pre_end) | as_flag(blacklisted_usdt_at_pre_end)
                         else tolower(address) %in% blacklist_any)]
  cnt <- function(x) c(exposed = sum(x & f$treat), control = sum(x & !f$treat))
  fun <- list(input = c(exposed = sum(f$treat), control = sum(!f$treat)))
  bad_floor <- !(f$mean_hold >= 100 & f$nz_days >= 6)
  fun$june_floor_violations <- cnt(bad_floor)
  if (strict_floors && any(bad_floor))
    stop(sprintf("June population violates the June floors the gate must apply (mean >= $100, >= 6/22 nonzero days): %d wallets",
                 sum(bad_floor)), call. = FALSE)
  f <- f[!bad_floor]
  cnt <- function(x) c(exposed = sum(x & f$treat), control = sum(x & !f$treat))
  fun$blacklisted_dropped <- if (drop_blacklisted) cnt(f$blacklisted) else c(exposed = 0L, control = 0L)
  if (drop_blacklisted) f <- f[blacklisted == FALSE]
  cnt <- function(x) c(exposed = sum(x & f$treat), control = sum(x & !f$treat))
  fun$issuer_flagged <- cnt(f$issuer)
  fun$issuer_dropped <- if (drop_issuer) fun$issuer_flagged else c(exposed = 0L, control = 0L)
  if (drop_issuer) f <- f[issuer == FALSE]
  fun$final <- c(exposed = sum(f$treat), control = sum(!f$treat))
  cat(sprintf("  frame funnel (exposed/control): input %d/%d | June-floor violations dropped %d/%d | baseline blacklist dropped %d/%d | zero-address issuer screen %s dropped %d/%d | final %d/%d\n",
              fun$input[1], fun$input[2], fun$june_floor_violations[1], fun$june_floor_violations[2],
              fun$blacklisted_dropped[1], fun$blacklisted_dropped[2], if (drop_issuer) "ON" else "OFF",
              fun$issuer_dropped[1], fun$issuer_dropped[2], fun$final[1], fun$final[2]))
  # wallet_age_blocks NA = never sent by pre_period_end -> its own CEM bin (coarsen_q maps NA to -1)
  f[, `:=`(transfers = as.numeric(transfer_count_total), eth = as.numeric(eth_balance_wei_units),
           age = as.numeric(wallet_age_blocks))]
  attr(f, "funnel") <- fun
  f[]
}

CEM_VARS <- c("mean_hold", "transfers", "age", "eth")

fmt_res <- function(r, delta) {
  dc <- classify_ci(r$lo, r$hi, delta)
  sprintf("beta=%.4f se=%.4f CI=[%.4f, %.4f] -> case %s", r$beta, r$se, r$lo, r$hi, dc$label)
}
fmt_a <- function(r) sprintf("beta=%.4f se=%.4f CI=[%.4f, %.4f] -> H1a %s", r$beta, r$se, r$lo, r$hi, classify_h1a(r$lo, r$hi))

sha256_file <- function(path) {
  if (is.null(path) || !is.character(path) || length(path) != 1L || is.na(path) || !nzchar(path)) stop("sha256_file: path must be a single non-empty character string")
  out <- system2("sha256sum", shQuote(path), stdout = TRUE)
  strsplit(out, " ")[[1]][1]
}

run_pre <- function(usdc_mode = "three_bins", g4dir, outdir, drop_issuer = TRUE, placebo_pop = "june",
                    g0dir = file.path("out", "g0"), participation_rule = NULL) {
  if (identical(placebo_pop, "g4_june")) stop("placebo_pop 'g4_june' (legacy, no manifest) is removed; use 'june' with a population_manifest.json", call. = FALSE)
  placebo_pop <- match.arg(placebo_pop, PLACEBO_POPS)
  dev_diag <- placebo_pop != "june"
  if (dev_diag && !is.null(participation_rule)) stop("participation_rule applies to the june placebo population only", call. = FALSE)
  pop_label <- if (dev_diag) "g4 (look-ahead population; DEVELOPMENT DIAGNOSTIC, not the prespecified placebo)"
               else "june_placebo (June-only rebuild, manifest-verified)"
  pop_ver <- NULL; load_dir <- g4dir
  if (placebo_pop == "june") {   # S20a: attest identity/hash/producer/upper block BEFORE any CSV row is read
    check_pre_outdir(outdir)
    pop_ver <- verify_population_manifest(g4dir, participation_rule = participation_rule)
    on.exit(unlink(pop_ver$dir, recursive = TRUE), add = TRUE)
    load_dir <- pop_ver$dir   # read ONLY the verified private copies (never re-open the original path)
  }
  cat("== Pre stage: placebo event 2022-07-09 (day_index", PLACEBO_EVENT_DAY, "), matching on June only ==\n")
  cat("  placebo population:", pop_label, "|", g4dir, "\n")
  inp <- load_pre_inputs(if (is.null(pop_ver)) g4dir else load_dir)
  if (!is.null(pop_ver) && (nrow(inp$g4cov) != pop_ver$row_counts[["covariates.csv"]] ||
                            2L * nrow(inp$wide) != pop_ver$row_counts[["wallet_coin_day_pre.csv"]]))
    stop("loaded row counts differ from the population manifest row_counts", call. = FALSE)
  stopifnot(max(inp$wide$day_index) <= PRE_LAST_DAY)
  cnt <- inp$wide[, .N, by = uid]
  if (uniqueN(cnt$N) != 1L) stop("panel is not balanced/dense across wallets; TWFE identity checks need a balanced panel")
  # main (g4) population: any-time-in-[9 Jun, 7 Aug] blacklist from the full event history; the June population
  # (g4_june) carries its own any-time-June flags in the covariates and keeps using them.
  bl_any <- if (placebo_pop == "g4") blacklisted_any_in_window(load_blacklist_events(g0dir)) else NULL
  if (!is.null(bl_any)) cat(sprintf("  blacklist filter: any time in blocks %d-%d (event replay): %d addresses (USDC %d, USDT %d)\n",
                                    PRE_START_BLOCK, PRE_END_BLOCK, length(bl_any),
                                    length(attr(bl_any, "by_coin")$USDC), length(attr(bl_any, "by_coin")$USDT)))
  frame <- pre_match_frame(inp, drop_issuer, strict_floors = !dev_diag, blacklist_any = bl_any)
  funnel <- attr(frame, "funnel")
  dir.create(outdir, showWarnings = FALSE, recursive = TRUE)
  ev <- PLACEBO_EVENT_DAY
  tab <- list(); prim <- NULL
  for (mode in unique(c(usdc_mode, "none", "continuous", "three_bins"))) {
    m <- cem_match(frame, vars = CEM_VARS, usdc_mode = mode, seed = SEED_PRIMARY_CEM)
    cem_report(m)
    # cut-points for caps: June days only (prespec §5 item 2), carried by the panel's baseline_days
    panel <- build_panel(inp$wide, m$matched, baseline_days = JUNE_DAYS)
    res <- list()
    for (cap in CAP_MODES) {
      r <- list(Dw = estimate_did(panel, ev, "D", "uid", cap), Ds = estimate_did(panel, ev, "D", "stratum", cap),
                Aw = estimate_did(panel, ev, "A", "uid", cap), As = estimate_did(panel, ev, "A", "stratum", cap))
      res[[cap]] <- r
      se_h1 <- max(r$Dw$se, r$Ds$se)
      cat(sprintf("  usdc_mode=%s cap=%s\n    D wallet : %s\n    D stratum: %s\n    A wallet : %s\n    power Phi(0.05/SE-1.96), SE=max(wallet,stratum)=%.4f: %.3f | MDE %.4f\n",
                  mode, cap, fmt_res(r$Dw, DELTA_D), fmt_res(r$Ds, DELTA_D), fmt_a(r$Aw), se_h1,
                  power_normal(se_h1), mde_h1(se_h1)))
      tab[[length(tab) + 1]] <- data.table(usdc_mode = mode, cap = cap, n_exposed = m$n_exposed - m$dropped_exposed,
        dropped_exposed = m$dropped_exposed, n_controls = m$n_controls_matched, n_strata = m$n_strata_kept,
        beta_D = r$Dw$beta, se_D_wallet = r$Dw$se, lo_D_wallet = r$Dw$lo, hi_D_wallet = r$Dw$hi,
        case_D_wallet = classify_ci(r$Dw$lo, r$Dw$hi, DELTA_D)$case,
        se_D_stratum = r$Ds$se, lo_D_stratum = r$Ds$lo, hi_D_stratum = r$Ds$hi,
        case_D_stratum = classify_ci(r$Ds$lo, r$Ds$hi, DELTA_D)$case,
        beta_A = r$Aw$beta, se_A_wallet = r$Aw$se, lo_A_wallet = r$Aw$lo, hi_A_wallet = r$Aw$hi,
        class_A_wallet = classify_h1a(r$Aw$lo, r$Aw$hi), se_A_stratum = r$As$se,
        se_h1 = se_h1, power_h1 = power_normal(se_h1), mde_h1 = mde_h1(se_h1),
        diag_power_vs_zero_null = power_zero_null(r$Dw$beta, r$Dw$se))
    }
    if (mode == usdc_mode) {
      v <- run_validations(panel, ev, "own", 1000L)
      stopifnot(v$ok)
      prim <- list(m = m, panel = panel, res = res,
                   legs = list(usdc = estimate_did(panel, ev, "usdc_B", "uid", "none"),
                               usdt = estimate_did(panel, ev, "usdt_B", "uid", "none")),
                   clip = clip_incidence(panel, ev), caps = cap_values_all(panel, ev))
    }
  }
  tab <- rbindlist(tab)
  fwrite(tab, file.path(outdir, "placebo_estimates.csv"))

  # ---- primary-specification decisions on the placebo -------------------------------------------------
  r <- prim$res; own <- r$own; unc <- r$none
  se_h1 <- max(own$Dw$se, own$Ds$se)
  pw <- power_normal(se_h1); mde <- mde_h1(se_h1)
  case_w <- classify_ci(own$Dw$lo, own$Dw$hi, DELTA_D)$case; case_s <- classify_ci(own$Ds$lo, own$Ds$hi, DELTA_D)$case
  placebo_pass <- case_w == 3L && case_s == 3L
  sup <- h1_support(c(own$Dw$lo, own$Dw$hi), c(own$Ds$lo, own$Ds$hi), c(unc$Dw$lo, unc$Dw$hi), c(unc$Ds$lo, unc$Ds$hi))
  cf <- clip_flag(prim$clip)
  enl <- if (dev_diag) list(decision = "fires", reason = "placebo population is the development diagnostic (g4), i.e. the rebuilt June placebo is unavailable")
         else if (pw < POWER_TARGET) list(decision = "fires", reason = sprintf("power %.3f < %.2f", pw, POWER_TARGET))
         else list(decision = "does_not_fire", reason = sprintf("power %.3f >= %.2f", pw, POWER_TARGET))
  cat(sprintf("\nPRIMARY (usdc_mode=%s, cap=own): power=%.3f MDE=%.4f (SE max(wallet %.4f, stratum %.4f)); placebo pass (both clusterings case 3): %s\n",
              usdc_mode, pw, mde, own$Dw$se, own$Ds$se, placebo_pass))
  cat(sprintf("  H1-support rule applied to placebo (information): %s | clip flag: %s | enlargement: %s (%s)\n",
              sup$status, cf$cap_sensitive_clip, enl$decision, enl$reason))
  print(cf$table, digits = 4)

  pm <- usdc_mode
  man <- list(
    stage = "pre", placebo_population = pop_label, development_diagnostic = dev_diag, input_dir = normalizePath(g4dir),
    r_version = R.version.string, rng_kind = RNG_KIND,
    packages = list(fixest = as.character(packageVersion("fixest")), `data.table` = as.character(packageVersion("data.table")),
                    jsonlite = as.character(packageVersion("jsonlite"))),
    seeds = list(primary_cem = SEED_PRIMARY_CEM, june_placebo_frame_used_by_gate = SEED_JUNE_FRAME, enlargement = SEED_ENLARGEMENT),
    pre_period_end_block = PRE_PERIOD_END_BLOCK, placebo_event_day_index = ev,
    input_sha256 = if (!is.null(pop_ver)) c(as.list(pop_ver$output_sha256), setNames(list(pop_ver$manifest_sha256), MANIFEST_NAME)) else {
      fs <- file.path(g4dir, c("covariates.csv", "wallet_coin_day_pre.csv", "meta.json"))
      fs <- fs[file.exists(fs)]; setNames(lapply(fs, sha256_file), basename(fs))
    },
    population = if (is.null(pop_ver)) NULL else list(population_id = pop_ver$manifest$population_id, generation_id = pop_ver$manifest$generation_id,
                                                      producing_commit = pop_ver$manifest$producing_commit, participation_rule = pop_ver$manifest$participation_rule, participation_window = pop_ver$manifest$participation_window, producer_source_sha256 = pop_ver$manifest$producer_source_sha256, cutoff_block = pop_ver$manifest$cutoff_block,
                                                      manifest_sha256 = pop_ver$manifest_sha256, output_file_sha256 = as.list(pop_ver$output_sha256)),
    funnel = lapply(funnel, as.list),
    cem = list(usdc_mode = usdc_mode, vars = CEM_VARS, cutpoints = prim$m$cutpoints,
               cutpoint_units = "mean_hold in USD; transfers count; age in blocks; eth in wei (as double)",
               n_exposed_eligible = prim$m$n_exposed, dropped_exposed = prim$m$dropped_exposed,
               matched_exposed = prim$m$m_T, matched_controls = prim$m$m_C, n_strata = prim$m$n_strata_kept,
               ratio = prim$m$ratio),
    caps = list(cap_days = "June days 9-30 Jun 2022 (day_index 0..21)", D = prim$caps$D, A = prim$caps$A,
                legs_pooled = prim$caps$legs, n_wallet_days = prim$caps$n_wallet_days),
    estimates = tab[tab$usdc_mode == pm],
    legs_uncapped = list(usdc_B = list(beta = prim$legs$usdc$beta, se = prim$legs$usdc$se),
                         usdt_B = list(beta = prim$legs$usdt$beta, se = prim$legs$usdt$se)),
    power = list(formula = "Phi(0.05/SE - 1.96); SE = max(wallet, stratum) clustered SE of capped (own) D; not rescaled",
                 se_wallet = own$Dw$se, se_stratum = own$Ds$se, se = se_h1, power = pw, mde = mde,
                 diag_power_vs_zero_null_labelled_diagnostic_only = power_zero_null(own$Dw$beta, own$Dw$se)),
    placebo = list(case_wallet = case_w, case_stratum = case_s, pass = placebo_pass,
                   h1_support_rule_info_only = sup$status),
    clip_incidence = list(shares = prim$clip, did = cf$table, cap_sensitive_clip = cf$cap_sensitive_clip),
    enlargement = enl)
  write_manifest(man, outdir)

  md <- c("# Pre-event placebo (event 2022-07-09, matching on June 9-30 only)", "",
          paste0("Placebo population: ", pop_label, "."), "No data after 2022-08-07 read.",
          if (dev_diag) "Transfer count, ether balance and wallet age in this population cover the full 60-day baseline, not June only.",
          sprintf("Zero-address issuer screen (both groups): %s. Blacklist filter: ON (both groups), %s.", if (drop_issuer) "ON" else "OFF",
                  if (is.null(bl_any)) "population's own flags" else "on either coin at any time in blocks 14,929,560-15,298,282 (event replay)"), "",
          "```", capture.output(print(tab, digits = 4)), "```")
  writeLines(md[!vapply(md, is.null, TRUE)], file.path(outdir, "summary.md"))
  cat("wrote", file.path(outdir, "summary.md"), "and prespec_manifest.{md,json}\n")
  setattr(tab, "manifest", man)
  invisible(tab)
}

write_manifest <- function(man, outdir) {
  jsonlite::write_json(man, file.path(outdir, "prespec_manifest.json"), auto_unbox = TRUE, pretty = TRUE, digits = NA, na = "string")
  f4 <- function(x) paste(sprintf("%.6g", x), collapse = ", ")
  cp <- man$cem$cutpoints
  L <- c("# Prespec manifest (R lane, pre stage)", "",
         sprintf("Placebo population: **%s**%s", man$placebo_population, if (man$development_diagnostic) " -- DEVELOPMENT DIAGNOSTIC, values below are not the signed placebo" else ""),
         sprintf("Input dir: `%s`", man$input_dir), "",
         "## Software and seeds",
         sprintf("- %s; RNG kind %s", man$r_version, paste(man$rng_kind, collapse = ", ")),
         sprintf("- packages: %s", paste(names(man$packages), unlist(man$packages), sep = " ", collapse = "; ")),
         sprintf("- seeds: primary CEM %d; June placebo frame %d (drawn by the gate, recorded); enlargement %d",
                 man$seeds$primary_cem, man$seeds$june_placebo_frame_used_by_gate, man$seeds$enlargement),
         sprintf("- pre_period_end block %d (no later block read)", man$pre_period_end_block), "",
         "## Input sha256", paste0("- ", names(man$input_sha256), ": `", unlist(man$input_sha256), "`"), "",
         "## Sample counts (exposed / control)",
         unlist(lapply(names(man$funnel), function(k) sprintf("- %s: %s", k, paste(names(man$funnel[[k]]), unlist(man$funnel[[k]]), sep = " ", collapse = " / ")))),
         sprintf("- CEM: eligible exposed %d, dropped (no control) %d, matched exposed %d, matched controls %d, strata %d, ratio <= %d:1, usdc_mode %s",
                 man$cem$n_exposed_eligible, man$cem$dropped_exposed, man$cem$matched_exposed, man$cem$matched_controls,
                 man$cem$n_strata, man$cem$ratio, man$cem$usdc_mode), "",
         "## CEM cut-points (type-7 25/50/75 among eligible exposed; duplicates removed; ties to higher bin)",
         paste0("- ", names(cp), ": ", vapply(cp, f4, "")), "",
         "## Cap values (type-7 1st/99th over all baseline wallet-days of the matched sample, pooled, unweighted)",
         sprintf("- D: [%s]", f4(man$caps$D)), sprintf("- A: [%s]", f4(man$caps$A)),
         sprintf("- legs pooled (USDC/B and USDT/B): [%s]", f4(man$caps$legs_pooled)),
         sprintf("- basis: %s; %d wallet-days", man$caps$cap_days, man$caps$n_wallet_days), "",
         "## Power (H1 planning proxy)",
         sprintf("- %s", man$power$formula),
         sprintf("- SE wallet %.5f, SE stratum %.5f, SE used %.5f -> power %.4f (target %.2f), MDE = 0.10 + 2.80 SE = %.4f",
                 man$power$se_wallet, man$power$se_stratum, man$power$se, man$power$power, POWER_TARGET, man$power$mde),
         sprintf("- diagnostic only (power against ZERO, old rule): %.4f", man$power$diag_power_vs_zero_null_labelled_diagnostic_only), "",
         "## Placebo",
         sprintf("- capped(own) D case: wallet %d, stratum %d -> pass (both case 3): %s", man$placebo$case_wallet, man$placebo$case_stratum, man$placebo$pass),
         sprintf("- legs (uncapped, wallet-clustered): USDC/B beta %.4f se %.4f; USDT/B beta %.4f se %.4f",
                 man$legs_uncapped$usdc_B$beta, man$legs_uncapped$usdc_B$se, man$legs_uncapped$usdt_B$beta, man$legs_uncapped$usdt_B$se), "",
         "## Clip incidence (weighted share of wallet-days strictly beyond the cap)",
         "```",
         "shares by arm x period are the columns; did = (exposed post - exposed baseline) - (control post - control baseline); flag if |did| > 0.01", "",
         capture.output(print(man$clip_incidence$did, digits = 4)), "```",
         sprintf("cap_sensitive_clip: %s", man$clip_incidence$cap_sensitive_clip), "",
         "## Enlargement decision",
         sprintf("- **%s** -- %s (enlargement itself is not implemented in this stage)", man$enlargement$decision, man$enlargement$reason), "",
         "## Estimates (primary usdc_mode, all cap modes)", "```", capture.output(print(man$estimates, digits = 4)), "```")
  writeLines(L, file.path(outdir, "prespec_manifest.md"))
}
