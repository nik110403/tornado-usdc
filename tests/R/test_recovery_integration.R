# S20a: June population attestation before CSV load + explicit CLI routing. Fixtures in tempdir only; never reads out/.
rt <- tempfile("recint"); dir.create(rt)
JE <- 15053225L
PR <- "baseline_transfer_required"
mk_schema <- function(d, rule = PR) for (sf in c(JUNE_SCHEMA_FILE, JUNE_PANEL_SCHEMA_FILE))
  writeLines(jsonlite::toJSON(list(sha256 = sha256_file(file.path(d, sub("\\.schema\\.json$", ".csv", sf))),
    notes = if (is.null(rule)) "legacy fixture notes, no rule" else sprintf("participation_rule=%s (recorded, not defaulted; fixture)", rule)), auto_unbox = TRUE), file.path(d, sf))
WINDOW <- expected_participation_window(JE)
# Legacy (S21-shaped) admission is pinned; fixtures are not S21, so tests inject a pin that names the fixture (the pin, not the absence, is what admits it).
with_pin <- function(d, expr) {
  old <- get("S21_LEGACY_PIN", globalenv()); on.exit(assign("S21_LEGACY_PIN", old, globalenv()))
  assign("S21_LEGACY_PIN", list(producing_commit = strrep("a", 40), producer_source_sha256 = strrep("e", 16), manifest_sha256 = sha256_file(file.path(d, MANIFEST_NAME))), globalenv())
  force(expr)
}
mk_man <- function(d, ..., schema_rule = PR) {
  gen <- basename(d)
  ov <- list(...); legacy <- "participation_rule" %in% names(ov) && is.null(ov$participation_rule)
  writeLines(jsonlite::toJSON(if (legacy) list(frame_sha256 = "x") else list(participation_rule = PR, participation_window = WINDOW), auto_unbox = TRUE), file.path(d, "meta.json"))
  mk_schema(d, schema_rule)
  m <- list(population_id = "june_placebo", generation_id = gen, producing_commit = strrep("a", 40), producer_source_sha256 = strrep("e", 16), schema_version = 1L,
            baseline_start_date = "2022-06-09", baseline_end_date = "2022-06-30", excluded_dates = list(),
            cutoff_block = JE, input_file_sha256 = list(`manifest.csv` = strrep("b", 64)),
            output_file_sha256 = setNames(lapply(file.path(d, c(JUNE_REQUIRED_FILES, JUNE_SCHEMA_FILE, JUNE_PANEL_SCHEMA_FILE)), sha256_file), c(JUNE_REQUIRED_FILES, JUNE_SCHEMA_FILE, JUNE_PANEL_SCHEMA_FILE)),
            participation_rule = PR, participation_window = WINDOW,
            row_counts = list(`covariates.csv` = nrow(fread(file.path(d, "covariates.csv"))),
                              `wallet_coin_day_pre.csv` = nrow(fread(file.path(d, "wallet_coin_day_pre.csv")))))
  m <- modifyList(m, ov, keep.null = TRUE); m <- m[!vapply(m, is.null, TRUE)]
  if (legacy) m$participation_window <- NULL
  jsonlite::write_json(m, file.path(d, MANIFEST_NAME), auto_unbox = TRUE, pretty = TRUE)
  invisible(m)
}
set.seed(11)
Nw <- 500L; wid <- 0:(Nw - 1L)
j_arm <- sample(c(rep("control", 350L), rep(c("withdrawal", "deposit", "both"), 50L)))
j_base <- pmax(exp(rnorm(Nw, 9, 0.6)), 400)
mk_june_dir <- function(name) {
  d <- file.path(rt, name); dir.create(d)
  g <- CJ(wallet_id = wid, coin = c("USDC", "USDT"), day_index = 0:59)
  g[, `:=`(date = format(PRE_PERIOD_START + day_index), balance_units = round(j_base[match(wallet_id, wid)] * runif(.N, 0.9, 1.1) * 1e6))]
  writeLines("{}", file.path(d, "meta.json"))
  fwrite(g, file.path(d, "wallet_coin_day_pre.csv"))
  fwrite(data.table(wallet_id = wid, address = sprintf("0x%040x", wid), arm = j_arm, transfer_count_total = rpois(Nw, 8),
                    eth_balance_wei_units = format(1e18 * rexp(Nw), scientific = FALSE), first_send_block = 1,
                    wallet_age_blocks = ifelse(runif(Nw) < .1, NA, 1.2e7 - rexp(Nw, 1e-6)), issuer_mint_redeem_flag = "False",
                    blacklisted_usdc_at_pre_end = "False", blacklisted_usdt_at_pre_end = "False", exchange_share = NA),
         file.path(d, "covariates.csv"))
  d
}
rejects <- function(expr, pat) {
  e <- try(expr, silent = TRUE)
  inherits(e, "try-error") && grepl(pat, conditionMessage(attr(e, "condition")))
}

test("June manifest verifies on a consistent fixture (no CSV body parsed)", {
  d <- file.path(rt, "gen-ok"); dir.create(d)
  for (f in JUNE_REQUIRED_FILES) writeLines(c("x", "y"), file.path(d, f))   # not parseable as the panel: only hashes are read
  mk_man(d, row_counts = list(`covariates.csv` = 1L, `wallet_coin_day_pre.csv` = 1L))
  v <- verify_population_manifest(d, june_end = JE, participation_rule = PR)
  stopifnot(v$manifest$generation_id == "gen-ok", v$june_end == JE, all(JUNE_OUTPUT_FILES %in% names(v$output_sha256)))
})
test("missing / wrong population, hash, producer, upper block are rejected before load", {
  for (case in list(list("population_id", "main", "population_id"), list("population_id", NULL, "lacks"),
                    list("producing_commit", "HEAD", "40-hex"), list("producing_commit", NULL, "lacks"), list("producer_source_sha256", NULL, "lacks"), list("producer_source_sha256", "xyz", "16-hex"),
                    list("cutoff_block", JE + 1L, "cutoff_block"), list("cutoff_block", JE - 1L, "cutoff_block"), list("cutoff_block", NULL, "lacks"),
                    list("input_file_sha256", NULL, "lacks"), list("input_file_sha256", list(a = "zz"), "non-sha256"),
                    list("generation_id", "other", "generation_id"), list("baseline_end_date", "2022-08-07", "baseline window"))) {
    d <- file.path(rt, paste0("gen-r", case[[1]], if (is.null(case[[2]])) "NULL" else case[[2]])); dir.create(d)
    for (f in JUNE_REQUIRED_FILES) writeLines("garbage \001", file.path(d, f))
    mk_man(d, row_counts = list(`covariates.csv` = 1L, `wallet_coin_day_pre.csv` = 1L))
    mm <- jsonlite::fromJSON(file.path(d, MANIFEST_NAME), simplifyVector = FALSE)
    mm[[case[[1]]]] <- case[[2]]; mm <- mm[!vapply(mm, is.null, TRUE)]
    jsonlite::write_json(mm, file.path(d, MANIFEST_NAME), auto_unbox = TRUE)
    stopifnot(rejects(run_pre("none", d, file.path(rt, "o-never"), placebo_pop = "june", participation_rule = PR), "before any CSV load"),
              rejects(verify_population_manifest(d, june_end = JE, participation_rule = PR), case[[3]]), !dir.exists(file.path(rt, "o-never")))
  }
})
test("June participation rule: required, recorded in manifest AND schema notes, mismatch refused before any CSV load", {
  d <- file.path(rt, "gen-rule"); dir.create(d)
  for (f in JUNE_REQUIRED_FILES) writeLines(c("x", "y"), file.path(d, f))
  mk_man(d, row_counts = list(`covariates.csv` = 1L, `wallet_coin_day_pre.csv` = 1L))
  v <- verify_population_manifest(d, june_end = JE, participation_rule = PR)
  stopifnot(identical(v$manifest$participation_rule, PR), JUNE_SCHEMA_FILE %in% names(v$output_sha256))
  unlink(v$dir, recursive = TRUE)
  stopifnot(rejects(verify_population_manifest(d, june_end = JE), "must be one of"),                       # never defaulted
            rejects(verify_population_manifest(d, june_end = JE, participation_rule = "maybe"), "must be one of"),
            rejects(verify_population_manifest(d, june_end = JE, participation_rule = "none"), "participation rule mismatch"),
            rejects(run_pre("none", d, file.path(rt, "o-rule"), placebo_pop = "june", participation_rule = "none"), "participation rule mismatch"),
            rejects(run_pre("none", d, file.path(rt, "o-rule"), placebo_pop = "june"), "must be one of"),
            !dir.exists(file.path(rt, "o-rule")))
  e <- file.path(rt, "gen-rule-schema"); dir.create(e)                                                    # schema notes disagree with manifest
  for (f in JUNE_REQUIRED_FILES) writeLines(c("x", "y"), file.path(e, f))
  mk_man(e, row_counts = list(`covariates.csv` = 1L, `wallet_coin_day_pre.csv` = 1L), schema_rule = "none")
  stopifnot(rejects(verify_population_manifest(e, june_end = JE, participation_rule = PR), "participation rule mismatch"))
  g <- file.path(rt, "gen-rule-absent"); dir.create(g)                                                    # manifest without the field (pre-rule generation)
  for (f in JUNE_REQUIRED_FILES) writeLines(c("x", "y"), file.path(g, f))
  mk_man(g, row_counts = list(`covariates.csv` = 1L, `wallet_coin_day_pre.csv` = 1L), participation_rule = NULL, schema_rule = NULL)
  stopifnot(rejects(verify_population_manifest(g, june_end = JE, participation_rule = PR), "legacy generation"))   # unrecorded + baseline_transfer_required: refused
  stopifnot(rejects(verify_population_manifest(g, june_end = JE, participation_rule = "none"), "not the pinned S21 generation"))   # stripped, not S21: refused even under none
  vl <- with_pin(g, verify_population_manifest(g, june_end = JE, participation_rule = "none"))             # unrecorded + none + pin naming it: explicit legacy path
  stopifnot(identical(vl$manifest$participation_rule, LEGACY_RULE_LABEL), LEGACY_RULE_LABEL == "none (legacy, not recorded at production)")
  unlink(vl$dir, recursive = TRUE)
  gs <- file.path(rt, "gen-rule-absent-schema"); dir.create(gs)                                           # legacy manifest but schema notes DO carry a rule: inconsistent
  for (f in JUNE_REQUIRED_FILES) writeLines(c("x", "y"), file.path(gs, f))
  mk_man(gs, row_counts = list(`covariates.csv` = 1L, `wallet_coin_day_pre.csv` = 1L), participation_rule = NULL)
  stopifnot(rejects(with_pin(gs, verify_population_manifest(gs, june_end = JE, participation_rule = "none")), "inconsistent generation"))
})
test("legacy generation (no recorded rule) end-to-end: refused unless --participation-rule none; outputs label it legacy", {
  d <- mk_june_dir("gen-legacy"); mk_man(d, participation_rule = NULL, schema_rule = NULL)
  stopifnot(rejects(run_pre("none", d, file.path(rt, "o-leg-bad"), placebo_pop = "june", participation_rule = PR), "legacy generation"), !dir.exists(file.path(rt, "o-leg-bad")))
  stopifnot(rejects(run_pre("none", d, file.path(rt, "o-leg-notS21"), placebo_pop = "june", participation_rule = "none"), "not the pinned S21 generation"))
  with_pin(d, run_pre("none", d, file.path(rt, "o-leg-ok"), placebo_pop = "june", participation_rule = "none"))
  mj <- jsonlite::fromJSON(file.path(rt, "o-leg-ok", "prespec_manifest.json"), simplifyVector = FALSE)
  stopifnot(identical(mj$population$participation_rule, "none (legacy, not recorded at production)"))
  d2 <- mk_june_dir("gen-rec"); mk_man(d2)                                                                 # recorded rule must match the CLI exactly
  stopifnot(rejects(run_pre("none", d2, file.path(rt, "o-rec-bad"), placebo_pop = "june", participation_rule = "none"), "participation rule mismatch"))
})
test("tampered output file, absent manifest, symlinked / `current` input dir are rejected", {
  d <- file.path(rt, "gen-tamper"); dir.create(d)
  for (f in JUNE_REQUIRED_FILES) writeLines("a", file.path(d, f))
  mk_man(d, row_counts = list(`covariates.csv` = 1L, `wallet_coin_day_pre.csv` = 1L))
  writeLines("b", file.path(d, "covariates.csv"))
  stopifnot(rejects(verify_population_manifest(d, june_end = JE, participation_rule = PR), "sha256 differs"))
  e <- file.path(rt, "gen-nomanifest"); dir.create(e)
  stopifnot(rejects(verify_population_manifest(e, june_end = JE, participation_rule = PR), "population_manifest.json missing"))
  stopifnot(rejects(verify_population_manifest(NA_character_, june_end = JE, participation_rule = PR), "explicitly"))
  ln <- file.path(rt, "ptr"); file.symlink(d, ln)
  stopifnot(rejects(verify_population_manifest(ln, june_end = JE, participation_rule = PR), "symlink"))
  cur <- file.path(rt, "x", "current"); dir.create(cur, recursive = TRUE)
  stopifnot(rejects(verify_population_manifest(cur, june_end = JE, participation_rule = PR), "current"))
  # a symlinked COMPONENT anywhere in the unresolved path is rejected, and so is a symlinked required file
  real <- file.path(rt, "real-parent"); dir.create(file.path(real, "gen-under"), recursive = TRUE)
  for (f in JUNE_REQUIRED_FILES) writeLines("a", file.path(real, "gen-under", f))
  mk_man(file.path(real, "gen-under"), row_counts = list(`covariates.csv` = 1L, `wallet_coin_day_pre.csv` = 1L))
  lp <- file.path(rt, "link-parent"); file.symlink(real, lp)
  stopifnot(rejects(verify_population_manifest(file.path(lp, "gen-under"), june_end = JE, participation_rule = PR), "symlink"))
  fl <- file.path(rt, "gen-filelink"); dir.create(fl)
  for (f in JUNE_REQUIRED_FILES) writeLines("a", file.path(fl, f))
  mk_man(fl, row_counts = list(`covariates.csv` = 1L, `wallet_coin_day_pre.csv` = 1L))
  file.remove(file.path(fl, "meta.json")); file.symlink(file.path(fl, "covariates.csv"), file.path(fl, "meta.json"))
  stopifnot(rejects(verify_population_manifest(fl, june_end = JE, participation_rule = PR), "regular|sha256"))
  fm <- file.path(rt, "gen-nometa"); dir.create(fm)
  for (f in JUNE_OUTPUT_FILES) writeLines("a", file.path(fm, f))
  mk_man(fm, row_counts = list(`covariates.csv` = 1L, `wallet_coin_day_pre.csv` = 1L)) |> try(silent = TRUE)
  m0 <- jsonlite::fromJSON(file.path(real, "gen-under", MANIFEST_NAME), simplifyVector = FALSE)
  m0$generation_id <- "gen-nometa"; m0$output_file_sha256$meta.json <- NULL
  jsonlite::write_json(m0, file.path(fm, MANIFEST_NAME), auto_unbox = TRUE)
  stopifnot(rejects(verify_population_manifest(fm, june_end = JE, participation_rule = PR), "lacks meta.json"))
  # verified private copies are what gets read: the copy hashes equal the manifest even if the original changes afterwards
  v <- verify_population_manifest(file.path(real, "gen-under"), june_end = JE, participation_rule = PR)
  writeLines("changed after verification", file.path(real, "gen-under", "covariates.csv"))
  stopifnot(identical(sha256_file(file.path(v$dir, "covariates.csv")), unname(v$output_sha256[["covariates.csv"]])))
  unlink(v$dir, recursive = TRUE)
  stopifnot(rejects(pinned_june_end(file.path(rt, "nope.json")), "missing"))
})
test("outdir must be new/empty for the June route", {
  full <- file.path(rt, "full"); dir.create(full); writeLines("x", file.path(full, "f"))
  stopifnot(rejects(check_pre_outdir(full), "not empty"), isTRUE(check_pre_outdir(file.path(rt, "fresh"))))
})
test("June fixture end-to-end through the CLI with explicit input/output (config/blocks.json june_end attested)", {
  d <- mk_june_dir("gen-e2e"); mk_man(d)
  od <- file.path(rt, "out-e2e")
  out <- suppressWarnings(system2("Rscript", c("run_all.R", "--stage", "pre", "--placebo-pop", "june", "--input-dir", shQuote(d),
                                               "--outdir", shQuote(od), "--participation-rule", PR, "--usdc-mode", "none"), stdout = TRUE, stderr = TRUE))
  st <- attr(out, "status"); stopifnot(is.null(st) || st == 0L)
  stopifnot(file.exists(file.path(od, "summary.md")), file.exists(file.path(od, "prespec_manifest.json")))
  mj <- jsonlite::fromJSON(file.path(od, "prespec_manifest.json"), simplifyVector = FALSE)
  stopifnot(mj$population$population_id == "june_placebo", mj$population$generation_id == "gen-e2e",
            !isTRUE(mj$development_diagnostic), !is.null(mj$input_sha256[[MANIFEST_NAME]]))
  # a second run into the same (now non-empty) outdir is refused: outputs are immutable
  out2 <- suppressWarnings(system2("Rscript", c("run_all.R", "--stage", "pre", "--placebo-pop", "june", "--input-dir", shQuote(d),
                                                "--outdir", shQuote(od), "--participation-rule", PR), stdout = TRUE, stderr = TRUE))
  stopifnot(!is.null(attr(out2, "status")), any(grepl("not empty", out2)))
})
test("row-count mismatch against the manifest is rejected after load", {
  d <- mk_june_dir("gen-rc"); mk_man(d, row_counts = list(`covariates.csv` = 499L, `wallet_coin_day_pre.csv` = 60000L))
  stopifnot(rejects(run_pre("none", d, file.path(rt, "o-rc"), placebo_pop = "june", participation_rule = PR), "row counts"))
})

# ---- S20b: main presign attestation (verify_main_manifest / the presign stage live in run_all.R, so these run under
# `Rscript run_all.R --stage test`; the full CLI matrix + end-to-end are in tests/test_recovery_integration.py) ----
MAIN_FILES <- c("covariates.csv", "wallet_coin_day_pre.csv", "meta.json")
MAIN_RT <- file.path(rt, "mainroot"); dir.create(MAIN_RT)
HEAD_SHA <- trimws(system2("git", c("rev-parse", "HEAD"), stdout = TRUE))   # a commit that exists (verify_main_manifest runs git cat-file)
MAIN_IN <- if (exists("MAIN_INPUT_KEYS")) setNames(as.list(rep(strrep("b", 64), 4)), MAIN_INPUT_KEYS)   # MAIN_INPUT_KEYS lives in run_all.R; absent under standalone run_tests.R (the guarded block below is then skipped)
mk_main <- function(name, ...) {
  d <- file.path(MAIN_RT, name); dir.create(d)
  for (f in MAIN_FILES) writeLines("garbage \001", file.path(d, f))
  m <- list(population_id = "main", generation_id = name, producing_commit = HEAD_SHA, producer_source_sha256 = strrep("e", 16), schema_version = 1L,
            baseline_start_date = "2022-06-09", baseline_end_date = "2022-08-07", excluded_dates = list("2022-08-08"), cutoff_block = 15298282L,
            input_file_sha256 = MAIN_IN, output_file_sha256 = setNames(lapply(file.path(d, MAIN_FILES), sha256_file), MAIN_FILES),
            row_counts = list(`covariates.csv` = 1L, `wallet_coin_day_pre.csv` = 1L))
  m <- modifyList(m, list(...), keep.null = TRUE); m <- m[!vapply(m, is.null, TRUE)]
  jsonlite::write_json(m, file.path(d, MANIFEST_NAME), auto_unbox = TRUE, pretty = TRUE)
  d
}
if (exists("verify_main_manifest")) {
  test("main manifest verifies on a consistent fixture (no CSV body parsed) and yields private copies", {
    v <- verify_main_manifest(mk_main("main-ok"), main_root = MAIN_RT)
    stopifnot(v$manifest$population_id == "main", v$manifest$cutoff_block == 15298282L, all(file.exists(file.path(v$dir, MAIN_FILES))))
    unlink(v$dir, recursive = TRUE)
  })
  test("main manifest: June / smoke population, wrong block, window, commit or tampered file are rejected", {
    for (case in list(list(population_id = "june_placebo"), list(population_id = "main_smoke"), list(cutoff_block = 15053225L), list(cutoff_block = 15298283L),
                      list(baseline_end_date = "2022-06-30"), list(excluded_dates = "2022-08-09"), list(producing_commit = "HEAD"), list(producer_source_sha256 = "xyz"),
                      list(generation_id = "other"), list(input_file_sha256 = list(a = "zz")))) {
      d <- do.call(mk_main, c(list(paste0("main-bad", sample.int(1e6, 1))), case))
      stopifnot(rejects(verify_main_manifest(d, main_root = MAIN_RT), "MAIN POPULATION REJECTED"))
    }
    d <- mk_main("main-tamper"); writeLines("changed", file.path(d, "covariates.csv"))
    stopifnot(rejects(verify_main_manifest(d, main_root = MAIN_RT), "sha256 differs"), rejects(verify_main_manifest(NA_character_), "explicitly"),
              rejects(verify_main_manifest(file.path(rt, "nope")), "does not exist"))
  })
  test("S20b fix1: exact input key set, pinned schema_version, existing commit, every output entry hashed, unlisted files rejected, containment", {
    bad <- function(pat, ...) {   # fields are replaced wholesale (modifyList in mk_main would merge nested lists)
      d <- mk_main(paste0("main-f", sample.int(1e6, 1))); m <- jsonlite::fromJSON(file.path(d, MANIFEST_NAME), simplifyVector = FALSE)
      for (k in names(list(...))) m[[k]] <- list(...)[[k]]
      jsonlite::write_json(m, file.path(d, MANIFEST_NAME), auto_unbox = TRUE); stopifnot(rejects(verify_main_manifest(d, main_root = MAIN_RT), pat))
    }
    bad("keys must be exactly", input_file_sha256 = MAIN_IN[-1])
    bad("keys must be exactly", input_file_sha256 = c(MAIN_IN, list(extra = strrep("b", 64))))
    bad("schema_version", schema_version = 2L)
    bad("not a commit", producing_commit = strrep("b", 40))
    d <- mk_main("main-unl"); writeLines("x", file.path(d, "extra.csv"))
    stopifnot(rejects(verify_main_manifest(d, main_root = MAIN_RT), "not listed"))
    d <- mk_main("main-lst"); writeLines("x", file.path(d, "summary.md"))
    m <- jsonlite::fromJSON(file.path(d, MANIFEST_NAME), simplifyVector = FALSE); m$output_file_sha256$summary.md <- strrep("c", 64)
    jsonlite::write_json(m, file.path(d, MANIFEST_NAME), auto_unbox = TRUE)
    stopifnot(rejects(verify_main_manifest(d, main_root = MAIN_RT), "summary.md sha256 differs"))
    stopifnot(rejects(verify_main_manifest(mk_main("main-root"), main_root = file.path(rt, "elsewhere")), "directly under the main root"))
  })
  test("S20b fix1: outdir / side-file path checks", {
    stopifnot(rejects(check_presign_outdir(file.path(MAIN_RT, "new"), MAIN_RT), "inside the immutable"),
              rejects(check_presign_outdir(file.path(rt, "a", "..", "b"), MAIN_RT), "`..`"),
              rejects(check_presign_outdir(file.path(rt, "current"), MAIN_RT), "current"),
              isTRUE(is.character(check_presign_outdir(file.path(rt, "fresh-out"), MAIN_RT))))
    f <- file.path(rt, "afile"); writeLines("x", f)
    stopifnot(rejects(check_presign_outdir(f, MAIN_RT), "not a directory"), rejects(check_side_file(file.path(rt, "nofile"), "x"), "not a regular file"),
              rejects(check_side_file(file.path(PRE_R_ROOT, "out", "g0", "usdt_events.csv"), "x"), "historical out/"), is.character(check_side_file(f, "x")))
    ln <- file.path(rt, "alink"); file.symlink(f, ln)
    stopifnot(rejects(check_side_file(ln, "x"), "symlink"))
  })
  test("S20b fix2: relative not-yet-existing outdirs are resolved before containment (N1/N2)", {
    # cwd is the repo root here; none of these paths exists, none is created
    for (rel in c("out/recovery/main/newgen-fix2", "out/recovery/june/x-fix2"))
      stopifnot(rejects(check_presign_outdir(rel, MAIN_RT), "inside the immutable"), !dir.exists(rel))
    stopifnot(rejects(check_presign_outdir("out/g4/x-fix2", MAIN_RT), "historical out/"),
              rejects(check_main_path("out/g4/x-fix2", "x"), "historical out/"),
              identical(resolve_path("out/recovery/main/newgen-fix2"), file.path(normalizePath(PRE_R_ROOT), "out/recovery/main/newgen-fix2")),
              isTRUE(is.character(check_presign_outdir("fix2-fresh-out-never-made", MAIN_RT))))
    # a relocated main_root never unlocks the production roots
    stopifnot(rejects(check_presign_outdir(file.path(MAIN_ROOT, "g"), file.path(rt, "elsewhere")), "inside the immutable"),
              rejects(check_presign_outdir(file.path(dirname(MAIN_ROOT), "june", "g"), file.path(rt, "elsewhere")), "inside the immutable"))
  })
  test("S20b fix2: side files are snapshotted once and the recorded hash is of the snapshot (N3)", {
    f <- file.path(rt, "snap-src"); writeLines("original", f); sd <- file.path(rt, "snapdir"); dir.create(sd)
    s <- snapshot_side_files(c(a_file = f), sd)
    h <- s$sha256[["a_file"]]
    writeLines("edited after snapshot", f)
    stopifnot(identical(readLines(s$files[["a_file"]]), "original"), identical(sha256_file(s$files[["a_file"]]), h), !identical(sha256_file(f), h), s$files[["a_file"]] != f)
  })
  test("B: the real g1 cohort.schema.json path is admissible for --cohort-schema-file only; other historical out/ paths stay refused", {
    stopifnot(is.character(check_main_path(G1_SCHEMA_PATH, "cohort_schema_file")),
              rejects(check_main_path(G1_SCHEMA_PATH, "candidates_file"), "historical out/"),
              rejects(check_main_path(file.path(PRE_R_ROOT, "out", "g1", "cohort.csv"), "cohort_schema_file"), "historical out/"),
              rejects(check_main_path(file.path(PRE_R_ROOT, "out", "g1b", "cohort.schema.json"), "cohort_schema_file"), "historical out/"))
  })
  test("presign stage CLI: every flag required, no default rule, June manifest never satisfies it", {
    run <- function(...) suppressWarnings(system2("Rscript", c("run_all.R", "--stage", "presign", ...), stdout = TRUE, stderr = TRUE, env = "TORNADO_TEST_MAIN_ROOT=1"))
    o <- run(); stopifnot(!is.null(attr(o, "status")), any(grepl("requires --population-id", o)))
    sf <- file.path(rt, "sidefile"); writeLines("x", sf)
    csf <- file.path(rt, "cohort.schema.json"); writeLines('{"sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "notes": "participation_rule=none (UNRESOLVED)"}', csf)
    d <- mk_main("main-cli"); od <- file.path(rt, "o-presign-never")
    base <- c("--population-id", "main", "--input-dir", shQuote(d), "--outdir", shQuote(od), "--direct-freeze-file", shQuote(sf), "--candidates-file", shQuote(sf),
              "--original-windows-file", shQuote(sf), "--blacklist-any-file", shQuote(sf), "--cohort-schema-file", shQuote(csf), "--main-root", shQuote(MAIN_RT))
    o <- run(base); stopifnot(!is.null(attr(o, "status")), any(grepl("requires --participation-rule", o)))
    o <- run(base, "--participation-rule", "maybe"); stopifnot(!is.null(attr(o, "status")), any(grepl("must be one of", o)))
    d2 <- mk_main("main-cli2", population_id = "june_placebo")
    base[4] <- shQuote(d2)
    o <- run(base, "--participation-rule", "none"); stopifnot(!is.null(attr(o, "status")), any(grepl("MAIN POPULATION REJECTED", o)), !dir.exists(od))
  })
}

# ---- S20: `--stage post` is fail-closed: signature + signed code + signed pre-signature input mapping before ANY input is opened. Fixture repos in tempdir only. ----
if (!exists("verify_post_signature")) source(file.path("R", "post_gate.R"))
pg_run <- function(args, ...) suppressWarnings(system2("git", c("-C", shQuote(args[[1]]), "-c", "user.name=t", "-c", "user.email=t@t", args[-1]), stdout = TRUE, stderr = TRUE, ...))
pg_sha <- function(path) sha256_file(path)
FIX_CODE <- c("R/stub.R", "config/stub.json")      # stand-ins for POST_R_CODE_FILES inside the throwaway repos
pg_commit <- function(root) { pg_run(c(root, "add", "-A")); pg_run(c(root, "commit", "-q", "-m", "x")) }
# A throwaway git repo whose prespec/ is signed over four role files (incl. the code manifest). `mutate(root)` may alter files BEFORE hashing.
mk_signed_repo <- function(name, input_sha = NULL, mutate = NULL, commit = TRUE, code = TRUE, pkg_ver = TRUE) {
  root <- file.path(rt, name); dir.create(file.path(root, "prespec"), recursive = TRUE); dir.create(file.path(root, "R")); dir.create(file.path(root, "config"))
  pg_run(c(root, "init", "-q"))
  writeLines("# estimator stand-in", file.path(root, "R", "stub.R")); writeLines("{}", file.path(root, "config", "stub.json"))
  writeLines("signed text", file.path(root, "prespec", "prespec.md"))
  jsonlite::write_json(list(post_input_file_sha256 = input_sha, r_version = R.version.string, r_package_versions = if (pkg_ver) setNames(lapply(POST_PINNED_PACKAGES, function(p) as.character(packageVersion(p))), POST_PINNED_PACKAGES)),
                       file.path(root, "prespec", "manifest.json"), auto_unbox = TRUE)
  writeLines("{}", file.path(root, "prespec", "config.json"))
  writeLines(if (code) vapply(FIX_CODE, function(f) sprintf("%s  %s", pg_sha(file.path(root, f)), f), "") else "", file.path(root, "prespec", "code.txt"))
  if (!is.null(mutate)) mutate(root)
  nm <- c("prespec.md", "manifest.json", "config.json", "code.txt")
  writeLines(vapply(nm, function(n) sprintf("%s  %s", pg_sha(file.path(root, "prespec", n)), n), ""), file.path(root, "prespec", "SHA256"))
  sign_it(root)
  if (commit) pg_commit(root)
  root
}
sign_it <- function(root, date = "2026-11-01") {
  jsonlite::write_json(list(signed_by = "Nik", signed_date = date, sha256_list_sha256 = pg_sha(file.path(root, "prespec", "SHA256")),
                            files = list(prespec_text = "prespec.md", numerical_manifest = "manifest.json", executable_config = "config.json", code_manifest = "code.txt")),
                       file.path(root, "prespec", "signing.json"), auto_unbox = TRUE)
}
vsig <- function(root) verify_post_signature(root, FIX_CODE)
denied <- function(expr) rejects(expr, "POST STAGE DENIED")
# pre-signature inputs + their signed hashes (NO post-window file here: none exists at signing time)
pin <- file.path(rt, "post-in"); dir.create(pin)
PIN <- c(`pre/covariates.csv` = "covariates.csv", `pre/wallet_coin_day_pre.csv` = "wallet_coin_day_pre.csv", match_membership = "mm.csv", analysis_parameters = "par.json")
for (k in names(PIN)) writeLines(paste("body", k), file.path(pin, PIN[[k]]))
PIN_PATHS <- setNames(file.path(pin, PIN), names(PIN))
PIN_SHA <- lapply(PIN_PATHS, pg_sha)
# what g5 writes AFTER signing: panel + schema bound to the signature in force
mk_panel_dir <- function(name, sig, body = "panel body", hashes = sig$signed_hashes, sha = NULL, root = NULL, commit = NULL) {
  d <- file.path(rt, name); dir.create(d)
  if (is.null(commit)) commit <- if (is.null(root)) strrep("0", 40) else trimws(pg_run(c(root, "rev-parse", "HEAD")))
  writeLines(body, file.path(d, "post_wallet_coin_day.csv"))
  jsonlite::write_json(list(schema_version = 1, sha256 = if (is.null(sha)) pg_sha(file.path(d, "post_wallet_coin_day.csv")) else sha, signed_file_sha256 = hashes, producing_commit = commit),
                       file.path(d, "post_wallet_coin_day.schema.json"), auto_unbox = TRUE)
  file.path(d, "post_wallet_coin_day.csv")
}

test("post: missing / blank / unbound / tampered / untracked / unsigned-list signatures are denied", {
  stopifnot(denied(verify_post_signature(file.path(rt, "no-such-repo"))))
  r0 <- mk_signed_repo("sig-ok", PIN_SHA)
  sig <- vsig(r0)
  stopifnot(identical(sig$signed_by, "Nik"), length(sig$role_files) == 4L, setequal(names(sig$signed_hashes), c(POST_ROLES, "signing_json")))
  r_unsigned <- mk_signed_repo("sig-none", PIN_SHA); unlink(file.path(r_unsigned, "prespec", "signing.json")); pg_commit(r_unsigned)
  stopifnot(denied(vsig(r_unsigned)))
  r_blank <- mk_signed_repo("sig-blank", PIN_SHA)
  rec <- jsonlite::fromJSON(file.path(r_blank, "prespec", "signing.json"), simplifyVector = FALSE); rec$signed_by <- " "
  jsonlite::write_json(rec, file.path(r_blank, "prespec", "signing.json"), auto_unbox = TRUE); pg_commit(r_blank)
  stopifnot(denied(vsig(r_blank)))
  r_unbound <- mk_signed_repo("sig-unbound", PIN_SHA)   # SHA256 edited after signing.json bound it
  cat("\n", file = file.path(r_unbound, "prespec", "SHA256"), append = TRUE); pg_commit(r_unbound)
  stopifnot(denied(vsig(r_unbound)))
  r_tamper <- mk_signed_repo("sig-tamper", PIN_SHA)     # role file changed after it was hashed (and committed)
  writeLines("edited", file.path(r_tamper, "prespec", "prespec.md")); pg_commit(r_tamper)
  stopifnot(denied(vsig(r_tamper)))
  r_dirty <- mk_signed_repo("sig-dirty", PIN_SHA)       # worktree differs from HEAD (uncommitted edit of a signed file)
  writeLines("edited", file.path(r_dirty, "prespec", "config.json"))
  stopifnot(denied(vsig(r_dirty)))
  stopifnot(denied(vsig(mk_signed_repo("sig-uncommitted", PIN_SHA, commit = FALSE))))   # signed files exist but are not committed at all
  r_dup <- mk_signed_repo("sig-dup", PIN_SHA)
  rec <- jsonlite::fromJSON(file.path(r_dup, "prespec", "signing.json"), simplifyVector = FALSE); rec$files$executable_config <- "prespec.md"
  jsonlite::write_json(rec, file.path(r_dup, "prespec", "signing.json"), auto_unbox = TRUE); pg_commit(r_dup)
  stopifnot(denied(vsig(r_dup)))
})

test("post: the signed code manifest binds estimator + gate code; unchecked extra SHA256 lines are refused", {
  stopifnot(denied(vsig(mk_signed_repo("code-none", PIN_SHA, code = FALSE))))                                  # manifest omits required files
  r <- mk_signed_repo("code-tamper", PIN_SHA)
  writeLines("# edited after signing", file.path(r, "R", "stub.R")); pg_commit(r)                                # committed, but not what was signed
  stopifnot(rejects(vsig(r), "does not match the signed code manifest"))
  r2 <- mk_signed_repo("code-dirty", PIN_SHA); writeLines("# dirty", file.path(r2, "config", "stub.json"))        # uncommitted
  stopifnot(rejects(vsig(r2), "HEAD blob"))
  r3 <- mk_signed_repo("code-extra-sha", PIN_SHA)
  cat(sprintf("%s  stray.txt\n", strrep("a", 64)), file = file.path(r3, "prespec", "SHA256"), append = TRUE); sign_it(r3); pg_commit(r3)
  stopifnot(rejects(vsig(r3), "unchecked"))
  r4 <- mk_signed_repo("code-role-missing", PIN_SHA)
  rec <- jsonlite::fromJSON(file.path(r4, "prespec", "signing.json"), simplifyVector = FALSE); rec$files$code_manifest <- NULL
  jsonlite::write_json(rec, file.path(r4, "prespec", "signing.json"), auto_unbox = TRUE); pg_commit(r4)
  stopifnot(denied(vsig(r4)))
  stopifnot(all(c("R/post.R", "R/post_gate.R", "R/drdid.R", "run_all.R") %in% POST_R_CODE_FILES))              # the real list covers the estimator + this gate
})

test("post: pre-signature inputs must equal the signed mapping exactly; post-window inputs are refused", {
  sig <- vsig(mk_signed_repo("map-ok", PIN_SHA))
  v <- verify_post_inputs(sig, PIN_PATHS)
  stopifnot(all(file.exists(v[names(PIN_PATHS)])), identical(unname(vapply(names(PIN_PATHS), function(k) pg_sha(v[[k]]), "")), unname(unlist(PIN_SHA))), !v[["priv"]] %in% dirname(PIN_PATHS))
  unlink(v[["priv"]], recursive = TRUE)
  bad <- PIN_PATHS; bad[["match_membership"]] <- file.path(rt, "mm-edited.csv"); writeLines("edited after signing", bad[["match_membership"]])
  stopifnot(denied(verify_post_inputs(sig, bad)))
  stopifnot(denied(verify_post_inputs(sig, PIN_PATHS[setdiff(names(PIN_PATHS), "match_membership")])))              # a signed input not given
  stopifnot(denied(verify_post_inputs(sig, c(PIN_PATHS, eligible_frame_coverage = PIN_PATHS[["match_membership"]]))))  # an input the signature does not list
  for (k in c("later_freeze", "outflow_evidence")) stopifnot(rejects(verify_post_inputs(sig, c(PIN_PATHS, setNames(PIN_PATHS[["match_membership"]], k))), "no signature-bound producer"))
  stopifnot(denied(verify_post_inputs(sig, c(PIN_PATHS, post_panel = PIN_PATHS[["match_membership"]]))))             # the panel is never a signed-hash input
  stopifnot(denied(verify_post_inputs(vsig(mk_signed_repo("map-none", NULL)), PIN_PATHS)))                           # manifest carries no mapping
  lnk <- file.path(rt, "post-link.csv"); file.symlink(PIN_PATHS[["match_membership"]], lnk)
  sym <- PIN_PATHS; sym[["match_membership"]] <- lnk
  stopifnot(denied(verify_post_inputs(sig, sym)))
  stopifnot(denied(verify_post_inputs(list(), PIN_PATHS)))                                                            # no verification result at all
})

test("post: TIME ORDER -- sign first, g5 produces the panel afterwards, then the stage opens; panel is verified by chain", {
  called <- character(); assign("run_post", function(pre_dir, post_panel_path, ...) { called <<- c(called, post_panel_path); "RAN" }, envir = globalenv())
  on.exit(rm("run_post", envir = globalenv()), add = TRUE)
  mk_opt <- function(vals) function(name, default) if (!is.null(vals[[name]])) vals[[name]] else default
  root <- mk_signed_repo("time-order", PIN_SHA)                     # 1. signature exists; NO post-window file exists yet
  sig <- vsig(root)
  panel <- mk_panel_dir("time-order-panel", sig, root = root)                    # 2. g5 runs under that signature and writes panel + manifest
  vals <- list(`pre-dir` = pin, `post-panel` = panel, `match-membership` = PIN_PATHS[["match_membership"]], `analysis-parameters` = PIN_PATHS[["analysis_parameters"]], outdir = file.path(rt, "o-time"))
  r <- run_post_stage(mk_opt(vals), repo_root = root, source_estimator = FALSE, code_files = FIX_CODE)   # 3. the stage opens
  stopifnot(identical(r, "RAN"), length(called) == 1L, grepl("post-verified-", called), !identical(called, panel))
  # a panel from another signature (re-signed prespec) / a tampered panel / a missing manifest are all denied, run_post never reached
  n_before <- length(called)
  r_other <- mk_signed_repo("time-order-other", PIN_SHA); sign_it(r_other, "2026-11-09"); pg_commit(r_other)   # re-signed: same role files, new signing.json
  other <- vsig(r_other)
  stopifnot(denied(run_post_stage(mk_opt(modifyList(vals, list(`post-panel` = mk_panel_dir("p-other", other, root = root)))), root, FALSE, FIX_CODE)))
  stopifnot(denied(run_post_stage(mk_opt(modifyList(vals, list(`post-panel` = mk_panel_dir("p-tamper", sig, sha = strrep("0", 64), root = root)))), root, FALSE, FIX_CODE)))
  stopifnot(denied(run_post_stage(mk_opt(modifyList(vals, list(`post-panel` = mk_panel_dir("p-short", sig, hashes = sig$signed_hashes[c("prespec_text", "signing_json")], root = root)))), root, FALSE, FIX_CODE)))
  lone <- file.path(rt, "lone"); dir.create(lone); writeLines("x", file.path(lone, "post_wallet_coin_day.csv"))
  stopifnot(denied(run_post_stage(mk_opt(modifyList(vals, list(`post-panel` = file.path(lone, "post_wallet_coin_day.csv")))), root, FALSE, FIX_CODE)))
  stopifnot(denied(run_post_stage(mk_opt(modifyList(vals, list(`post-panel` = PIN_PATHS[["match_membership"]]))), root, FALSE, FIX_CODE)))
  stopifnot(length(called) == n_before)
})

test("post stage: denial happens before any input is opened; run_post is reached only after all checks pass", {
  called <- character(); assign("run_post", function(pre_dir, ...) { called <<- c(called, pre_dir); "RAN" }, envir = globalenv())
  on.exit(rm("run_post", envir = globalenv()), add = TRUE)
  mk_opt <- function(vals) function(name, default) if (!is.null(vals[[name]])) vals[[name]] else default
  vals <- list(`pre-dir` = file.path(rt, "nonexistent-pre"), `post-panel` = file.path(rt, "nonexistent-post.csv"), `match-membership` = file.path(rt, "nonexistent-mm.csv"),
               `analysis-parameters` = file.path(rt, "nonexistent-par.json"), outdir = file.path(rt, "o-post-never"))
  # no signature at all: the denial names the signature, NOT the (nonexistent) inputs -> nothing was opened
  e <- try(run_post_stage(mk_opt(vals), repo_root = file.path(rt, "sig-none"), source_estimator = FALSE, code_files = FIX_CODE), silent = TRUE)
  stopifnot(inherits(e, "try-error"), grepl("POST STAGE DENIED", conditionMessage(attr(e, "condition"))), grepl("signing|SHA256", conditionMessage(attr(e, "condition"))), !length(called), !dir.exists(vals$outdir))
  root <- mk_signed_repo("post-ok", PIN_SHA); sig <- vsig(root)
  vals2 <- list(`pre-dir` = pin, `post-panel` = mk_panel_dir("post-ok-panel", sig, root = root), `match-membership` = PIN_PATHS[["match_membership"]], `analysis-parameters` = PIN_PATHS[["analysis_parameters"]], outdir = file.path(rt, "o-post"))
  writeLines("x", file.path(rt, "mm-bad.csv")); vals_bad <- vals2; vals_bad$`match-membership` <- file.path(rt, "mm-bad.csv")
  stopifnot(denied(run_post_stage(mk_opt(vals_bad), repo_root = root, source_estimator = FALSE, code_files = FIX_CODE)), !length(called))
  vals_lf <- c(vals2, list(`later-freeze` = PIN_PATHS[["match_membership"]]))
  stopifnot(rejects(run_post_stage(mk_opt(vals_lf), repo_root = root, source_estimator = FALSE, code_files = FIX_CODE), "no signature-bound producer"), !length(called))
  r <- run_post_stage(mk_opt(vals2), repo_root = root, source_estimator = FALSE, code_files = FIX_CODE)
  stopifnot(identical(r, "RAN"), length(called) == 1L, !identical(called, pin), grepl("post-verified-", called))
})

test("post CLI: missing / non-git signature is denied before the (nonexistent) inputs are touched; no outputs", {
  out <- suppressWarnings(system2("Rscript", c("run_all.R", "--stage", "post", "--pre-dir", shQuote(file.path(rt, "nx-pre")), "--post-panel", shQuote(file.path(rt, "nx.csv")),
                                               "--match-membership", shQuote(file.path(rt, "nx-mm.csv")), "--analysis-parameters", shQuote(file.path(rt, "nx.json")),
                                               "--outdir", shQuote(file.path(rt, "o-post-cli"))), stdout = TRUE, stderr = TRUE))
  stopifnot(!is.null(attr(out, "status")), any(grepl("POST STAGE DENIED", out)), !any(grepl("No such file|cannot open|does not exist|must exist", out)), !dir.exists(file.path(rt, "o-post-cli")))
  out <- suppressWarnings(system2("Rscript", c("run_all.R", "--stage", "post"), stdout = TRUE, stderr = TRUE))
  stopifnot(!is.null(attr(out, "status")), any(grepl("requires --pre-dir", out)))
})

test("post: every pinned package + R.version.string must EQUAL the signed values (fail closed)", {
  allv <- setNames(as.list(sprintf("%d.0", seq_along(POST_PINNED_PACKAGES))), POST_PINNED_PACKAGES)
  man <- list(r_package_versions = allv, r_version = "R version 9.9.9 (test)")
  inst <- function(v) function(p) v[[p]]
  rv <- "R version 9.9.9 (test)"
  stopifnot(isTRUE(verify_post_package_versions(man, inst(allv), rv)))
  stopifnot(all(c("DRDID", "trust", "BMisc", "fastglm", "Rcpp", "jsonlite") %in% POST_PINNED_PACKAGES))
  for (p in POST_PINNED_PACKAGES) {
    other <- allv; other[[p]] <- "99.9"
    stopifnot(rejects(verify_post_package_versions(man, inst(other), rv), "differs from the signed version"))
    gone <- allv; gone[[p]] <- NA_character_
    stopifnot(rejects(verify_post_package_versions(man, inst(gone), rv), "not installed"))
    unp <- man; unp$r_package_versions[[p]] <- NULL
    stopifnot(rejects(verify_post_package_versions(unp, inst(allv), rv), "does not pin"))
  }
  stopifnot(rejects(verify_post_package_versions(man, inst(allv), "R version 4.0.0 (other)"), "differs from the signed r_version"))
  stopifnot(rejects(verify_post_package_versions(list(r_package_versions = allv), inst(allv), rv), "does not record r_version"))
  stopifnot(rejects(verify_post_package_versions(list(), inst(list())), "no r_package_versions"))
  # end to end through the signature check: the fixture pins the installed versions; one without a record, or with a wrong one, is denied
  stopifnot(is.list(vsig(mk_signed_repo("ver-ok", PIN_SHA))))
  stopifnot(rejects(vsig(mk_signed_repo("ver-none", PIN_SHA, pkg_ver = FALSE)), "r_package_versions"))
  stopifnot(rejects(vsig(mk_signed_repo("ver-wrong", PIN_SHA, mutate = function(root) {
    jsonlite::write_json(list(post_input_file_sha256 = PIN_SHA, r_version = R.version.string, r_package_versions = modifyList(setNames(lapply(POST_PINNED_PACKAGES, function(p) as.character(packageVersion(p))), POST_PINNED_PACKAGES), list(fixest = "0.0.1"))),
                         file.path(root, "prespec", "manifest.json"), auto_unbox = TRUE) })), "differs from the signed version"))
})

test("post: panel chain needs a producing_commit that is an ancestor of HEAD (accident guard)", {
  root <- mk_signed_repo("pc-ok", PIN_SHA); sig <- vsig(root)
  priv <- file.path(rt, "pc-priv"); dir.create(priv)
  stopifnot(file.exists(verify_post_panel(sig, mk_panel_dir("pc-good", sig, root = root), priv, root)))
  stopifnot(denied(verify_post_panel(sig, mk_panel_dir("pc-zero", sig), priv, root)))                                 # not an ancestor
  stopifnot(denied(verify_post_panel(sig, mk_panel_dir("pc-bad", sig, commit = "unknown"), priv, root)))              # g4._git_commit() failure value
})

# ---- S59: the §5.9 later-freeze path through run_post_stage (derive from signed g0 inputs + g5_veto output; hand-supplied list stays refused) ----
if (!exists("mk_veto")) source(file.path("tests", "R", "lf_fixtures.R"))
test("post stage S59: --veto-dir needs BOTH g0 files (and vice versa) before anything opens; veto failures are row-only, shared-provenance failures are hard; a hand-supplied list stays refused", {
  called <- list(); assign("run_post", function(pre_dir, post_panel_path, ..., later_freeze_inputs = NULL) { called[[length(called) + 1L]] <<- later_freeze_inputs; "RAN" }, envir = globalenv())
  on.exit(rm("run_post", envir = globalenv()), add = TRUE)
  mk_opt <- function(vals) function(name, default) if (!is.null(vals[[name]])) vals[[name]] else default
  g0d <- file.path(rt, "s59-g0"); g0 <- mk_g0(g0d, data.table(addr = character(), block = integer(), token = character()))
  sha_all <- c(PIN_SHA, list(g0_circle_events = pg_sha(g0$circle), g0_usdt_events = pg_sha(g0$usdt), g0_receipts = pg_sha(g0$receipts)))
  root <- mk_signed_repo("s59-ok", sha_all, mutate = function(r) {
    jsonlite::write_json(list(post_input_file_sha256 = sha_all, post_end_block = LFX_POST_END, r_version = R.version.string, r_package_versions = setNames(lapply(POST_PINNED_PACKAGES, function(p) as.character(packageVersion(p))), POST_PINNED_PACKAGES)),
                         file.path(r, "prespec", "manifest.json"), auto_unbox = TRUE) })
  sig <- vsig(root); head <- trimws(pg_run(c(root, "rev-parse", "HEAD")))
  base <- list(`pre-dir` = pin, `post-panel` = mk_panel_dir("s59-panel", sig, root = root), `match-membership` = PIN_PATHS[["match_membership"]],
               `analysis-parameters` = PIN_PATHS[["analysis_parameters"]], outdir = file.path(rt, "o-s59"))
  mkv <- function(nm, ...) mk_veto(file.path(rt, nm), data.table(addr = lfx_addr(101), block = 15400000L, unit = "circle_blacklisted"), hashes = sig$signed_hashes, commit = head, ...)
  run <- function(vals) run_post_stage(mk_opt(vals), repo_root = root, source_estimator = FALSE, code_files = FIX_CODE)
  both <- list(`g0-circle-events` = g0$circle, `g0-usdt-events` = g0$usdt, `g0-receipts` = g0$receipts)
  stopifnot(denied(run(c(base, list(`veto-dir` = mkv("s59-v0"))))), denied(run(c(base, both))), denied(run(c(base, both[1], list(`veto-dir` = mkv("s59-v0b"))))), !length(called))
  # S59 fix round: the xor rule in isolation. The signed mapping HAS the g0 keys and the files hash right, so only the "--veto-dir and ALL g0 files together" rule can deny these two
  stopifnot(rejects(run(c(base, both)), "given together or not at all"), rejects(run(c(base, list(`veto-dir` = mkv("s59-vx")))), "given together or not at all"), !length(called))
  r <- run(c(base, both, list(`veto-dir` = mkv("s59-v1"))))
  lfi <- called[[1]]
  stopifnot(identical(r, "RAN"), isTRUE(lfi$veto$ok), lfi$post_end_block == LFX_POST_END, grepl("post-verified-", lfi$circle_events), !identical(lfi$circle_events, g0$circle))   # private verified copies
  rb <- mkv("s59-vbad"); writeLines("tampered", file.path(rb, "receipts.jsonl"))
  run(c(base, both, list(`veto-dir` = rb))); stopifnot(isFALSE(called[[2]]$veto$ok), grepl("tampered", called[[2]]$veto$reason))              # row-only: the stage still runs
  n <- length(called)
  stopifnot(denied(run(c(base, both, list(`veto-dir` = mk_veto(file.path(rt, "s59-vh"), data.table(addr = lfx_addr(101), block = 15400000L, unit = "circle_blacklisted"), hashes = sig$signed_hashes, commit = strrep("0", 40)))))), length(called) == n)   # hard
  stopifnot(denied(run(c(base, both[1:2], list(`veto-dir` = mkv("s59-v1b"))))), length(called) == n)                                          # g0 receipts omitted while the event files are given: all or none
  sha_two <- c(PIN_SHA, list(g0_circle_events = pg_sha(g0$circle), g0_usdt_events = pg_sha(g0$usdt)))      # a (consistently) signed mapping WITHOUT the receipts hash: the g0 source is all-or-none
  root2 <- mk_signed_repo("s59-two", sha_two, mutate = function(r) {
    jsonlite::write_json(list(post_input_file_sha256 = sha_two, post_end_block = LFX_POST_END, r_version = R.version.string, r_package_versions = setNames(lapply(POST_PINNED_PACKAGES, function(p) as.character(packageVersion(p))), POST_PINNED_PACKAGES)),
                         file.path(r, "prespec", "manifest.json"), auto_unbox = TRUE) })
  sig2 <- vsig(root2); head2 <- trimws(pg_run(c(root2, "rev-parse", "HEAD")))
  run2 <- function(vals) run_post_stage(mk_opt(vals), repo_root = root2, source_estimator = FALSE, code_files = FIX_CODE)
  stopifnot(denied(run2(c(base[names(base) != "post-panel"], list(`post-panel` = mk_panel_dir("s59-panel2", sig2, root = root2)), both[1:2]))), length(called) == n)   # two g0 keys signed + given, receipts missing, no veto-dir: only verify_post_inputs rejects it
  rc_bak <- tempfile(); file.copy(g0$receipts, rc_bak); cat("\n", file = g0$receipts, append = TRUE)
  stopifnot(denied(run(c(base, both, list(`veto-dir` = mkv("s59-v1c"))))), length(called) == n)                                          # g0 receipts.jsonl no longer the signed bytes: HARD
  file.copy(rc_bak, g0$receipts, overwrite = TRUE); run(c(base, both, list(`veto-dir` = mkv("s59-v1d")))); stopifnot(length(called) == n + 1L, !is.null(called[[n + 1L]]$g0_receipts))   # restored: runs again
  n <- length(called)
  g0_edit <- g0; writeLines("edited after signing", file.path(g0d, "usdt_events.csv"))
  stopifnot(denied(run(c(base, both, list(`veto-dir` = mkv("s59-v2"))))), length(called) == n)                                            # g0 file no longer the signed bytes
  stopifnot(rejects(run(c(base, list(`later-freeze` = PIN_PATHS[["match_membership"]]))), "no signature-bound producer"))                     # the hand-supplied csv stays refused
})
