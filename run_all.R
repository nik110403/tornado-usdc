#!/usr/bin/env Rscript
# R lane (sole estimator). Usage: Rscript run_all.R --stage synthetic|pre|presign|test|all [--sims N] [--usdc-mode none|continuous|three_bins] [--no-issuer-screen] --placebo-pop june|g4 --input-dir DIR [june: --participation-rule baseline_transfer_required|none, REQUIRED, must equal the rule recorded in the generation] --outdir DIR [--g0dir DIR (g4 only)]  (pre: all of --placebo-pop/--input-dir/--outdir are required; no defaults)
# Post (S20, FAIL-CLOSED): Rscript run_all.R --stage post --pre-dir DIR --post-panel F --match-membership F --analysis-parameters F --outdir DIR [--eligible-frame-coverage F] [--usdc-pool-flag F] [--deposit-counts F] [--g0-circle-events F --g0-usdt-events F --g0-receipts F --veto-dir D (the §5.9 list: derived from the two hash-bound g0 files, bound to the hash-bound g0 receipts, vetoed by the g5_veto output; all four or none)]   (a hand-supplied --later-freeze / --outflow-evidence are post-window data with no signature-bound producer: REFUSED; the committed prespec/ signature and the signed input-hash mapping are verified BEFORE any input file is opened; denial = nonzero exit, nothing read; not part of --stage all)
# Main presign (S20b): Rscript run_all.R --stage presign --population-id main --input-dir <main generation dir> --outdir <new dir> --participation-rule baseline_transfer_required|none --direct-freeze-file F --candidates-file F --original-windows-file F --blacklist-any-file F --cohort-schema-file <g1 cohort.schema.json; its recorded participation_rule must equal --participation-rule> [--main-root DIR, test-only: needs TORNADO_TEST_MAIN_ROOT]   (every other flag required, no defaults; main root is fixed at out/recovery/main; not part of --stage all)
root <- {
  a <- grep("^--file=", commandArgs(FALSE), value = TRUE)
  if (length(a)) normalizePath(dirname(sub("^--file=", "", a[1]))) else getwd()
}
setwd(root)
for (f in c("utils", "decide", "cem", "estimate", "validate", "synth", "pre", "blacklist", "presign", "post_gate", "later_freeze")) source(file.path("R", paste0(f, ".R")))

args <- commandArgs(TRUE)
opt <- function(name, default) { i <- match(paste0("--", name), args); if (is.na(i)) default else args[i + 1] }
stage <- opt("stage", "")
if (!stage %in% c("synthetic", "pre", "presign", "post", "test", "all")) stop("usage: Rscript run_all.R --stage synthetic|pre|presign|post|test|all")

# ---- main presign routing (S20b) ------------------------------------------------------------------------------------
MAIN_POPULATION_ID <- "main"
MAIN_BASELINE_START <- "2022-06-09"; MAIN_BASELINE_END <- "2022-08-07"
MAIN_REQUIRED_FILES <- c("covariates.csv", "wallet_coin_day_pre.csv", "meta.json")   # must be listed; EVERY listed entry is hashed, unlisted files are rejected
MAIN_INPUT_KEYS <- c("g0/usdt_events.csv", "g1/cohort.csv", "g1b/frame_survivors.csv", "baseline_blacklist_manifest")   # = run_all.py MAIN_INPUT_FILES + baseline manifest; exact set
MAIN_SCHEMA_VERSION <- 1L                                                              # = gates/g4_controls.SCHEMA_VERSION
MAIN_ROOT <- file.path(PRE_R_ROOT, "out", "recovery", "main")                          # generations live directly below it
main_fail <- function(...) stop(paste0("\n*** MAIN POPULATION REJECTED (before any CSV load): ", sprintf(...), " ***\n"), call. = FALSE)

# Absolute, `..`-free resolution of a path that may not exist yet: normalise the deepest EXISTING ancestor, re-append the rest
# (normalizePath(mustWork = FALSE) returns a not-yet-existing relative path unchanged, which would dodge every prefix check).
resolve_path <- function(path) {
  ap <- path.expand(sub("/+$", "", path))
  if (!startsWith(ap, "/")) ap <- file.path(getwd(), ap)
  comps <- strsplit(ap, "/")[[1]]; comps <- comps[nzchar(comps) & comps != "."]
  if (".." %in% comps) main_fail("path %s contains `..`", path)
  tail <- character()
  while (length(comps) && !file.exists(paste0("/", paste(comps, collapse = "/")))) { tail <- c(comps[length(comps)], tail); comps <- comps[-length(comps)] }
  base <- if (length(comps)) normalizePath(paste0("/", paste(comps, collapse = "/"))) else "/"
  if (length(tail)) paste(sub("/+$", "", base), paste(tail, collapse = "/"), sep = "/") else base
}

# Mirrors verify_population_manifest (June, R/pre.R) for population_id `main`: identity / producer / upper-block / hash
# attestation of a main generation dir BEFORE any CSV row is read; the verified private copies are what gets loaded.
verify_main_manifest <- function(input_dir, blocks_path = file.path(PRE_R_ROOT, "config", "blocks.json"), main_root = MAIN_ROOT) {
  if (!is.character(input_dir) || length(input_dir) != 1L || is.na(input_dir) || !nzchar(input_dir)) main_fail("input dir must be given explicitly (no current/default population)")
  if (!dir.exists(input_dir)) main_fail("input dir %s does not exist", input_dir)
  rewrap <- function(e) main_fail("%s", trimws(gsub("\\*\\*\\* JUNE POPULATION REJECTED \\(before any CSV load\\): | \\*\\*\\*", "", conditionMessage(e))))
  tryCatch(check_generation_path(input_dir), error = rewrap)
  rr <- resolve_path(main_root); ri <- resolve_path(input_dir)
  if (!identical(dirname(ri), rr)) main_fail("input dir %s is not a generation directly under the main root %s", input_dir, rr)
  priv <- tempfile("main-verified-"); dir.create(priv, mode = "0700")
  ok <- FALSE; on.exit(if (!ok) unlink(priv, recursive = TRUE), add = TRUE)
  cp <- function(name) tryCatch(private_copy(input_dir, name, priv), error = rewrap)
  mpath <- cp(MANIFEST_NAME)
  m <- tryCatch(jsonlite::fromJSON(mpath, simplifyVector = FALSE), error = function(e) main_fail("%s unreadable: %s", MANIFEST_NAME, conditionMessage(e)))
  need <- c("population_id", "generation_id", "producing_commit", "producer_source_sha256", "baseline_start_date", "baseline_end_date",
            "excluded_dates", "cutoff_block", "input_file_sha256", "output_file_sha256", "row_counts", "schema_version")
  miss <- setdiff(need, names(m))
  if (length(miss)) main_fail("manifest lacks: %s", paste(miss, collapse = ", "))
  if (!identical(m$population_id, MAIN_POPULATION_ID)) main_fail("manifest population_id '%s' != '%s' (June/development/smoke populations never satisfy the main presign)", format(m$population_id), MAIN_POPULATION_ID)
  gen <- m$generation_id
  if (!is.character(gen) || length(gen) != 1L || !nzchar(gen)) main_fail("manifest generation_id empty")
  if (!identical(gen, basename(sub("/+$", "", input_dir)))) main_fail("manifest generation_id '%s' != input dir name '%s'", gen, basename(sub("/+$", "", input_dir)))
  if (!is_hex(m$producing_commit, 40)) main_fail("producing_commit %s is not a 40-hex commit sha", format(m$producing_commit))
  if (!is_hex(m$producer_source_sha256, 16)) main_fail("producer_source_sha256 %s is not 16-hex", format(m$producer_source_sha256))
  if (!isTRUE(as_whole(m$schema_version) == MAIN_SCHEMA_VERSION)) main_fail("schema_version %s != pinned %d", format(m$schema_version), MAIN_SCHEMA_VERSION)
  git <- suppressWarnings(system2("git", c("-C", shQuote(PRE_R_ROOT), "cat-file", "-e", shQuote(paste0(m$producing_commit, "^{commit}"))), stdout = FALSE, stderr = FALSE))
  if (!identical(as.integer(git), 0L)) main_fail("producing_commit %s is not a commit of this repository (git cat-file -e failed)", m$producing_commit)
  if (!identical(m$baseline_start_date, MAIN_BASELINE_START) || !identical(m$baseline_end_date, MAIN_BASELINE_END))
    main_fail("baseline window must be %s..%s (got %s..%s)", MAIN_BASELINE_START, MAIN_BASELINE_END, format(m$baseline_start_date), format(m$baseline_end_date))
  if (!("2022-08-08" %in% unlist(m$excluded_dates))) main_fail("excluded_dates must contain 2022-08-08 (event day)")
  cb <- as_whole(m$cutoff_block)
  if (is.na(cb)) main_fail("cutoff_block missing/non-integer")
  pin <- if (file.exists(blocks_path)) as_whole(jsonlite::fromJSON(blocks_path, simplifyVector = FALSE)$pre_period_end$block) else NA_real_
  if (is.na(pin)) main_fail("pinned pre_period_end missing in %s (cannot attest the upper block)", blocks_path)
  if (cb != pin || cb != PRE_PERIOD_END_BLOCK) main_fail("cutoff_block %.0f != pinned pre_period_end %.0f / %d (upper-block attestation)", cb, pin, PRE_PERIOD_END_BLOCK)
  hashes <- function(x, what) {
    if (!is.list(x) || !length(x) || is.null(names(x)) || any(!nzchar(names(x)))) main_fail("%s must be a non-empty name -> sha256 map", what)
    h <- vapply(x, function(v) if (is_hex(v, 64)) v else NA_character_, "")
    if (anyNA(h)) main_fail("%s has non-sha256 entries: %s", what, paste(names(h)[is.na(h)], collapse = ", "))
    h
  }
  in_h <- hashes(m$input_file_sha256, "input_file_sha256"); out_h <- hashes(m$output_file_sha256, "output_file_sha256")
  if (!setequal(names(in_h), MAIN_INPUT_KEYS) || anyDuplicated(names(in_h)))
    main_fail("input_file_sha256 keys must be exactly {%s} (got {%s})", paste(MAIN_INPUT_KEYS, collapse = ", "), paste(names(in_h), collapse = ", "))
  if (length(setdiff(MAIN_REQUIRED_FILES, names(out_h)))) main_fail("output_file_sha256 lacks %s", paste(setdiff(MAIN_REQUIRED_FILES, names(out_h)), collapse = ", "))
  if (anyDuplicated(names(out_h)) || any(grepl("/", names(out_h), fixed = TRUE) | names(out_h) %in% c(".", "..", MANIFEST_NAME)))
    main_fail("output_file_sha256 has an invalid or duplicate entry name")
  unlisted <- setdiff(list.files(input_dir, all.files = TRUE, no.. = TRUE), c(names(out_h), MANIFEST_NAME))
  if (length(unlisted)) main_fail("generation holds files not listed in output_file_sha256: %s", paste(unlisted, collapse = ", "))
  for (f in names(out_h)) if (!identical(sha256_file(cp(f)), unname(out_h[[f]]))) main_fail("%s sha256 differs from manifest output_file_sha256", f)
  rcn <- vapply(JUNE_OUTPUT_FILES, function(f) if (is.list(m$row_counts) && !is.null(m$row_counts[[f]])) as_whole(m$row_counts[[f]]) else NA_real_, 0)
  if (anyNA(rcn)) main_fail("row_counts must give a whole number for %s", paste(JUNE_OUTPUT_FILES, collapse = ", "))
  ok <- TRUE
  list(manifest = m, dir = priv, row_counts = rcn, manifest_sha256 = sha256_file(mpath))
}

# One lowercase address per line (optional `address` header), as for the direct-freeze file; used for the any-time blacklist set.
read_address_file <- function(path, what) {
  if (!is.character(path) || length(path) != 1L || !nzchar(path) || !file.exists(path)) stop(sprintf("%s is required and must exist", what), call. = FALSE)
  a <- trimws(readLines(path, warn = FALSE)); a <- a[nzchar(a)]
  if (length(a) && a[1] == "address") a <- a[-1]
  ps_check_address(a, what)
  a
}

# Side inputs/outputs are explicit paths too: no `..`, no symlinked component, no `current` pointer, and nothing from the
# historical out/ tree (only out/recovery/* generations and files outside out/ are admissible).
G1_SCHEMA_PATH <- file.path(PRE_R_ROOT, "out", "g1", "cohort.schema.json")   # the one historical out/ file a presign may read
check_main_path <- function(path, what) {
  if (!is.character(path) || length(path) != 1L || is.na(path) || !nzchar(path)) main_fail("%s must be an explicit path", what)
  ap <- path.expand(sub("/+$", "", path))
  if (!startsWith(ap, "/")) ap <- file.path(getwd(), ap)
  comps <- strsplit(ap, "/")[[1]]; comps <- comps[nzchar(comps) & comps != "."]
  if (".." %in% comps) main_fail("%s %s contains `..`", what, path)
  if ("current" %in% comps) main_fail("%s %s goes through a `current` pointer", what, path)
  for (i in seq_along(comps)) {   # the path may not exist yet (outdir); Sys.readlink gives NA there
    lk <- Sys.readlink(paste0("/", paste(comps[1:i], collapse = "/")))
    if (!is.na(lk) && nzchar(lk)) main_fail("%s %s: component /%s is a symlink", what, path, paste(comps[1:i], collapse = "/"))
  }
  ap <- resolve_path(path)
  out_root <- file.path(normalizePath(PRE_R_ROOT), "out")
  if (identical(what, "cohort_schema_file") && identical(ap, resolve_path(G1_SCHEMA_PATH))) return(invisible(ap))   # exactly this file, nothing else under out/
  if (startsWith(ap, paste0(out_root, "/")) && !startsWith(ap, paste0(out_root, "/recovery/"))) main_fail("%s %s is under the historical out/ tree (only out/recovery/* generations are admissible)", what, path)
  invisible(ap)
}
check_side_file <- function(path, what) {
  check_main_path(path, what)
  if (!file.exists(path) || isTRUE(file.info(path)$isdir)) main_fail("%s %s is not a regular file", what, path)
  invisible(path)
}
snapshot_side_files <- function(paths, dir) {
  files <- character(); sha <- character()
  for (n in names(paths)) {
    dest <- file.path(dir, n)
    if (!file.copy(paths[[n]], dest, overwrite = FALSE)) main_fail("%s %s could not be copied", n, paths[[n]])
    files[[n]] <- dest; sha[[n]] <- sha256_file(dest)
  }
  list(files = files, sha256 = sha)
}
# side_manifest.json (presign_side_files output) is REQUIRED and binds the side files: g1/cohort.csv sha256 well-formed, every recorded
# output sha256 equals the hash of the snapshotted copy actually used. `m` = the snapshotted manifest path, `files` = named snapshot paths.
SIDE_MANIFEST_OUTPUTS <- c(direct_freeze.txt = "direct_freeze_file", candidates.csv = "candidates_file",
                           blacklist_any.txt = "blacklist_any_file", original_windows.csv = "original_windows_file")
check_side_manifest <- function(m, files) {
  man <- tryCatch(jsonlite::fromJSON(m, simplifyVector = FALSE), error = function(e) main_fail("side_manifest.json unreadable: %s", conditionMessage(e)))
  if (!is.list(man) || is.null(names(man))) main_fail("side_manifest.json is not a JSON object")
  is_sha <- function(x) is.character(x) && length(x) == 1L && grepl("^[0-9a-f]{64}$", x)
  csha <- man$inputs[["g1/cohort.csv"]]$sha256
  if (!is_sha(csha)) main_fail("side_manifest.json lacks a valid inputs[g1/cohort.csv].sha256")
  for (n in names(SIDE_MANIFEST_OUTPUTS)) {
    rec <- man$outputs[[n]]$sha256
    if (!is_sha(rec)) main_fail("side_manifest.json lacks a valid outputs[%s].sha256", n)
    if (!identical(rec, sha256_file(files[[SIDE_MANIFEST_OUTPUTS[[n]]]]))) main_fail("side file %s differs from side_manifest.json outputs[%s].sha256", SIDE_MANIFEST_OUTPUTS[[n]], n)
  }
  csha
}
check_presign_outdir <- function(outdir, main_root = MAIN_ROOT) {
  ap <- check_main_path(outdir, "outdir")
  # the production main root and its June sibling are forbidden whatever `main_root` is (a relocated guard must not unlock them)
  rr <- vapply(unique(c(MAIN_ROOT, main_root)), resolve_path, "")
  for (r in unique(c(rr, file.path(dirname(rr), "june"), file.path(dirname(resolve_path(MAIN_ROOT)), "june")))) if (identical(ap, r) || startsWith(ap, paste0(r, "/")))
    main_fail("outdir %s is inside the immutable population root %s", outdir, r)
  if (file.exists(outdir) && !isTRUE(file.info(outdir)$isdir)) main_fail("outdir %s exists and is not a directory", outdir)
  if (dir.exists(outdir) && length(list.files(outdir, all.files = TRUE, no.. = TRUE))) stop(sprintf("outdir %s is not empty; main presign outputs are immutable (use a new directory)", outdir), call. = FALSE)
  invisible(ap)
}

run_presign_stage <- function(opt, args) {
  req <- function(name) {
    v <- opt(name, NA_character_)
    if (is.na(v) || !nzchar(v) || startsWith(v, "--")) stop(sprintf("--stage presign requires --%s (no default; explicit immutable input/output routing)", name), call. = FALSE)
    v
  }
  pid <- req("population-id"); indir <- req("input-dir"); outdir <- req("outdir"); rule <- req("participation-rule")
  dff <- req("direct-freeze-file"); candf <- req("candidates-file"); owf <- req("original-windows-file"); blf <- req("blacklist-any-file"); csf <- req("cohort-schema-file")
  main_root <- MAIN_ROOT
  if (!is.na(opt("main-root", NA_character_))) {   # relocating the guard is a test-only affordance: production input must live under out/recovery/main
    if (!nzchar(Sys.getenv("TORNADO_TEST_MAIN_ROOT"))) stop("--main-root is test-only (set TORNADO_TEST_MAIN_ROOT); production input must be a generation under out/recovery/main", call. = FALSE)
    main_root <- opt("main-root", NA_character_)
  }
  if (!identical(pid, MAIN_POPULATION_ID)) stop("--population-id must be `main` (June / development / enlarged populations never feed the main presign)", call. = FALSE)
  if (!rule %in% PARTICIPATION_RULES) stop(sprintf("--participation-rule must be one of %s (unresolved design decision; recorded verbatim, never defaulted)", paste(PARTICIPATION_RULES, collapse = ", ")), call. = FALSE)
  check_presign_outdir(outdir, main_root)
  for (k in list(c(dff, "direct_freeze_file"), c(candf, "candidates_file"), c(owf, "original_windows_file"), c(blf, "blacklist_any_file"), c(csf, "cohort_schema_file"))) check_side_file(k[1], k[2])
  smf <- file.path(dirname(dff), "side_manifest.json"); check_side_file(smf, "side_manifest")
  # side files are copied ONCE into a private dir; only the copies are read and hashed (a later edit of the original cannot desync the recorded hash)
  sdir <- tempfile("presign-side-"); dir.create(sdir, mode = "0700"); on.exit(unlink(sdir, recursive = TRUE), add = TRUE)
  side <- snapshot_side_files(c(direct_freeze_file = dff, candidates_file = candf, original_windows_file = owf, blacklist_any_file = blf, cohort_schema_file = csf, side_manifest = smf), sdir)
  dff <- side$files[["direct_freeze_file"]]; candf <- side$files[["candidates_file"]]; owf <- side$files[["original_windows_file"]]; blf <- side$files[["blacklist_any_file"]]; csf <- side$files[["cohort_schema_file"]]
  side_cohort_sha <- check_side_manifest(side$files[["side_manifest"]], side$files)   # from the snapshot, never the original path
  ver <- verify_main_manifest(indir, main_root = main_root)   # BEFORE any CSV row is read
  on.exit(unlink(ver$dir, recursive = TRUE), add = TRUE)
  # loaded row counts vs the manifest, on the verified private copies, BEFORE run_presign writes anything
  for (f in JUNE_OUTPUT_FILES) if (nrow(fread(file.path(ver$dir, f), select = "wallet_id")) != ver$row_counts[[f]])
    main_fail("%s: loaded row count differs from the population manifest row_counts", f)
  bl <- read_address_file(blf, "blacklist_any_file")
  cand <- fread(candf, colClasses = "character")
  ow <- fread(owf)
  if (!all(c("start_block", "end_block") %in% names(ow))) stop("original_windows_file needs start_block,end_block", call. = FALSE)
  # write to a temp sibling and rename on success: a failed run never leaves a partial outdir
  outdir <- sub("/+$", "", outdir)
  dir.create(dirname(outdir), recursive = TRUE, showWarnings = FALSE)
  tmp <- paste0(outdir, ".tmp-", Sys.getpid())
  on.exit(unlink(tmp, recursive = TRUE), add = TRUE)
  r <- run_presign(ver$dir, ver$manifest, pid, tmp, bl, cand, ow, transfer_participation_rule = rule, direct_freeze_file = dff,
                  cohort_schema_file = csf, side_cohort_sha256 = side_cohort_sha)
  jsonlite::write_json(list(population_manifest_sha256 = ver$manifest_sha256, transfer_participation_rule = rule,
                            side_input_sha256 = as.list(side$sha256)),
                       file.path(tmp, "side_inputs.json"), auto_unbox = TRUE, pretty = TRUE)
  if (dir.exists(outdir)) {
    if (length(list.files(outdir, all.files = TRUE, no.. = TRUE))) stop(sprintf("outdir %s became non-empty during the run; refusing to overwrite", outdir), call. = FALSE)
    unlink(outdir, recursive = TRUE)
  }
  if (!file.rename(tmp, outdir)) stop(sprintf("could not move the finished presign output onto %s", outdir), call. = FALSE)
  cat(sprintf("wrote %s (population main, generation %s, participation rule %s)\n", outdir, ver$manifest$generation_id, rule))
  invisible(r)
}

options(tusdc.synthetic = FALSE)
ok <- TRUE
if (stage %in% c("test", "all")) {
  # own env: test files assign scratch names (e.g. `ok`) that must not leak into this script's state
  source(file.path("tests", "R", "run_tests.R"), local = new.env())
}
if (stage %in% c("synthetic", "all")) {
  options(tusdc.synthetic = TRUE)
  r <- run_synthetic(n_sims = as.integer(opt("sims", "1000")))
  options(tusdc.synthetic = FALSE)
  ok <- ok && r$ok
  cat(sprintf("\nSYNTHETIC STAGE: %s\n", if (r$ok) "PASS" else "FAIL"))
}
if (stage %in% c("pre", "all")) {
  # Explicit immutable routing (S20a): no default population, no `current` pointer, no out/ fallback.
  req <- function(name) {
    v <- opt(name, NA_character_)
    if (is.na(v) || !nzchar(v) || startsWith(v, "--")) stop(sprintf("--stage pre requires --%s (no default; explicit immutable input/output routing)", name), call. = FALSE)
    v
  }
  pop <- req("placebo-pop")
  if (!pop %in% c("june", "g4")) stop("--placebo-pop must be `june` (signed June placebo) or `g4` (development diagnostic); legacy `g4_june` and defaults are rejected", call. = FALSE)
  indir <- req("input-dir"); outdir <- req("outdir")
  g0dir <- if (pop == "g4") req("g0dir") else NULL
  if (pop == "june" && !is.na(opt("g0dir", NA_character_))) stop("--g0dir applies to --placebo-pop g4 only", call. = FALSE)
  prule <- NULL
  if (pop == "june") {   # the June population is produced under a recorded participation rule; the CLI must name the same one
    prule <- req("participation-rule")
    if (!prule %in% PARTICIPATION_RULES) stop(sprintf("--participation-rule must be one of %s (unresolved design decision; recorded verbatim, never defaulted)", paste(PARTICIPATION_RULES, collapse = ", ")), call. = FALSE)
  } else if (!is.na(opt("participation-rule", NA_character_))) stop("--participation-rule applies to --placebo-pop june only", call. = FALSE)
  run_pre(usdc_mode = opt("usdc-mode", "three_bins"), g4dir = indir, placebo_pop = pop,
          g0dir = g0dir, drop_issuer = !"--no-issuer-screen" %in% args, outdir = outdir, participation_rule = prule)
}
if (stage == "presign") run_presign_stage(opt, args)
if (stage == "post") run_post_stage(opt)
if (!ok) quit(status = 1)
