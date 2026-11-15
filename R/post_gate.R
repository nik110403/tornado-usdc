# Fail-closed signature gate for `Rscript run_all.R --stage post` (S20). Mirrors gates/g5_post.verify_signature (Python) for the R lane.
# Defines functions only; nothing runs at source time. Order is the contract: verify_post_signature() touches ONLY git and the
# committed prespec/ files; verify_post_inputs() is the first function allowed to open an input file, and it opens it only after the
# signature passed. Anything else -> stop() with "POST STAGE DENIED"; run_post() is never reached.
#
# Signed layout (same files the g5 gate reads): <repo>/prespec/{signing.json, SHA256, <the FOUR role files>}; signing.json and SHA256 are
# git-tracked and byte-equal to HEAD, and SHA256 lists exactly the role files (an unchecked extra line is refused). The roles are the signed
# prespec text, numerical manifest, executable config and CODE MANIFEST (`<sha256>  <repo-relative path>` lines): every file in
# POST_R_CODE_FILES must be listed there, and every listed file must be tracked, equal to HEAD and match its hash -- the estimator and
# this gate's own code are bound by the signature, checked before any data access.
#
# TIME ORDER (the signature exists BEFORE any post-window file does): the signed numerical manifest's `post_input_file_sha256` map holds
# ONLY pre-signature inputs and its key set must equal exactly the pre-signature inputs given on the command line:
#   pre/covariates.csv, pre/wallet_coin_day_pre.csv, match_membership, analysis_parameters     (always)
#   eligible_frame_coverage, usdc_pool_flag, deposit_counts                                    (only when that flag is given; pre-window data)
# The post-window panel is verified BY CHAIN instead: it must sit next to g5's `post_wallet_coin_day.schema.json`, whose
# `signed_file_sha256` must equal the CURRENT signature's hashes (roles + signing.json) and whose `sha256` must equal the panel's hash.
# `later_freeze` (a hand-supplied 8 Aug-12 Sep list) and `outflow_evidence` are post-window data with no signature-bound producer: they are REFUSED.
# The §5.9 drop list is instead DERIVED in signed R code (R/later_freeze.R) from two pre-signature, hash-bound inputs (`g0_circle_events`,
# `g0_usdt_events`: g0's lifetime freeze files; `g0_receipts`: the g0 receipts.jsonl they came from, hash-bound and tiling-checked in R) and checked against g5_veto's raw-log output (`--veto-dir`, verified here by chain AND recomputed
# by R). A shared-provenance failure of the veto output (signature chain, producing commit, chain/endpoint attestation, pinned blocks) is a hard
# denial like the panel's; any other veto-output failure only makes the §5.9 row `not estimable` (R/later_freeze.R lf_verify_veto).
# Inputs are copied once into a private dir and the COPIES are hashed and then analysed (no edit-after-check window).

POST_REQUIRED_KEYS <- c("pre/covariates.csv", "pre/wallet_coin_day_pre.csv", "match_membership", "analysis_parameters")
POST_OPTIONAL_KEYS <- c("eligible_frame_coverage", "usdc_pool_flag", "deposit_counts", "g0_circle_events", "g0_usdt_events", "g0_receipts")
POST_REFUSED_KEYS <- c("later_freeze", "outflow_evidence")
POST_G0_KEYS <- c("g0_circle_events", "g0_usdt_events", "g0_receipts")   # the §5.9 source: the two g0 event files + the g0 receipts.jsonl they came from (all or none)
POST_ROLES <- c("prespec_text", "numerical_manifest", "executable_config", "code_manifest")
POST_PINNED_PACKAGES <- c("fixest", "data.table", "DRDID", "trust", "BMisc", "fastglm", "Rcpp", "jsonlite",
                          "Matrix", "RcppEigen", "sandwich", "nlme", "dreamerr", "numDeriv", "stringmagic", "bigmemory")   # MIRROR of gates/g5_post.py POST_PINNED_PACKAGES; signed in the numerical manifest as `r_package_versions`; installed versions must EQUAL them
# MIRROR of gates/g5_post.py POST_R_CODE_FILES (g5 runs the R-half checks first; tests/test_g5_post.py diffs the two lists)
POST_R_CODE_FILES <- c("run_all.R", file.path("R", c("utils.R", "decide.R", "cem.R", "estimate.R", "validate.R", "synth.R", "pre.R", "blacklist.R",
                                                   "presign.R", "drdid.R", "post.R", "post_gate.R", "later_freeze.R")),
                       "run_all.py", file.path("config", c("blocks.json", "endpoints.json")),
                       file.path("gates", c("g5_post.py", "g4_controls.py", "g1_cohort.py", "g1b_frame.py", "__init__.py", "g0_baseline_blacklist.py",
                                            "g0_blacklist_ext.py", "g0_manifest.py", "g1c_funnel.py", "g2_integrity.py", "g3_mde.py", "g4_june.py",
                                            "presign_side_files.py", "g5_veto.py")),
                       file.path("lib", c("rpc.py", "keccak.py", "blocks.py", "__init__.py")))

pg_fail <- function(...) stop(paste0("\n*** POST STAGE DENIED: ", sprintf(...), " ***\n"), call. = FALSE)

pg_islink <- function(p) { lk <- Sys.readlink(p); !is.na(lk) && nzchar(lk) }   # NA (unreadable) is not a link: the missing-file check handles it

pg_git <- function(root, args, stdout = FALSE) {
  saved <- Sys.getenv(grep("^GIT_", names(Sys.getenv()), value = TRUE), names = TRUE)   # a stray GIT_DIR / GIT_WORK_TREE must not redirect the check
  if (length(saved)) Sys.unsetenv(names(saved))
  on.exit(if (length(saved)) do.call(Sys.setenv, as.list(saved)), add = TRUE)
  suppressWarnings(system2("git", c("-C", shQuote(root), args), stdout = stdout, stderr = FALSE))
}

# Bytes of repo-relative `rel`: a regular non-symlink file, git-tracked, byte-equal to the HEAD blob (not `git diff`, which
# assume-unchanged / skip-worktree fool). Returns the worktree path.
pg_committed_file <- function(root, rel) {
  p <- file.path(root, rel)
  for (q in c(file.path(root, dirname(rel)), p)) if (pg_islink(q)) pg_fail("%s is a symlink", rel)
  if (!file.exists(p) || isTRUE(file.info(p)$isdir)) pg_fail("%s is missing", rel)
  if (!identical(as.integer(pg_git(root, c("ls-files", "--error-unmatch", "--", shQuote(rel)))), 0L)) pg_fail("%s is not git-tracked", rel)
  blob <- tempfile("pg-blob-"); on.exit(unlink(blob), add = TRUE)
  if (!identical(as.integer(pg_git(root, c("cat-file", "blob", shQuote(paste0("HEAD:", rel))), stdout = blob)), 0L)) pg_fail("%s has no HEAD blob", rel)
  if (!identical(sha256_file(blob), sha256_file(p))) pg_fail("%s differs from the HEAD blob", rel)
  p
}

# TRUE when any JSON object (at any depth) repeats a key. jsonlite keeps duplicates in the parsed list and `$` silently reads the FIRST, while
# Python's json keeps the LAST, so a duplicated key would let the two halves read different signed values: refused (mirror of g5_post._no_dup_pairs).
pg_dupkeys <- function(x) {
  if (!is.list(x)) return(FALSE)
  nm <- names(x)
  if (!is.null(nm) && anyDuplicated(nm)) return(TRUE)
  any(vapply(x, pg_dupkeys, TRUE))
}

# The signed-list grammar knows exactly four whitespace characters (' ', \t, \r, \n): trimws() default, and gates/g5_post.py _WS. A line made of
# any other (unicode) whitespace is NOT blank and must match the line grammar (so it is denied); invalid UTF-8 is denied. readLines splits on
# LF / CRLF / CR, mirrored by g5_post._lines.
pg_read_list <- function(path, what) {
  lines <- readLines(path, warn = FALSE)
  if (!all(validUTF8(lines))) pg_fail("%s is not valid UTF-8", what)
  lines[nzchar(trimws(lines))]
}

# Strict JSON read of a signed-package document, mirroring g5_post._loads: raw bytes must be UTF-8 with NO byte-order mark (jsonlite would
# silently strip one; Python refuses it), then parsed from that text (NaN / Infinity / UTF-16 / malformed -> jsonlite errors -> denial).
pg_read_json <- function(path, what) {
  raw <- readBin(path, "raw", file.info(path)$size)
  if (length(raw) >= 3L && identical(raw[1:3], as.raw(c(0xef, 0xbb, 0xbf)))) pg_fail("%s starts with a UTF-8 byte-order mark", what)
  txt <- tryCatch(rawToChar(raw), error = function(e) pg_fail("%s is not valid text", what))
  if (!validUTF8(txt)) pg_fail("%s is not valid UTF-8", what)
  tryCatch(jsonlite::fromJSON(txt, simplifyVector = FALSE), error = function(e) pg_fail("%s is not valid JSON", what))
}

verify_post_code_manifest <- function(path, repo_root, code_files) {
  lines <- pg_read_list(path, "code manifest")
  m <- regmatches(lines, regexec("^([0-9a-f]{64})  ([^ \t\r\n].*)$", lines))
  if (any(lengths(m) != 3L)) pg_fail("code manifest has a line that is not '<sha256>  <repo-relative path>'")
  h <- setNames(vapply(m, `[`, "", 2), vapply(m, `[`, "", 3))
  if (anyDuplicated(names(h)) || any(startsWith(names(h), "/") | grepl("(^|/)\\.\\.(/|$)", names(h)))) pg_fail("code manifest has a duplicate or unsafe path")
  miss <- setdiff(code_files, names(h))
  if (length(miss)) pg_fail("code manifest does not bind: %s", paste(miss, collapse = ", "))
  for (rel in names(h)) if (!identical(sha256_file(pg_committed_file(repo_root, rel)), unname(h[[rel]]))) pg_fail("code file %s does not match the signed code manifest", rel)
  invisible(TRUE)
}

# The estimator's version-sensitive packages (H1 uses fixest::feols) must be EXACTLY the versions the signature records (fail closed:
# a missing record, a missing package or any difference is a denial). `installed` is injectable for tests.
verify_post_package_versions <- function(man, installed = function(p) if (requireNamespace(p, quietly = TRUE)) as.character(utils::packageVersion(p)) else NA_character_,
                                         r_version = R.version.string) {
  pk <- man[["r_package_versions"]]
  if (!is.list(pk)) pg_fail("signed manifest carries no r_package_versions")
  for (p in POST_PINNED_PACKAGES) {
    want <- pk[[p]]
    if (!is.character(want) || length(want) != 1L || !nzchar(trimws(want))) pg_fail("signed r_package_versions does not pin %s", p)
    have <- installed(p)
    if (is.na(have)) pg_fail("package %s is not installed (signed %s)", p, want)
    if (!identical(have, want)) pg_fail("installed %s %s differs from the signed version %s", p, have, want)
  }
  rv <- man[["r_version"]]   # hard deny like the packages: the signed R.version.string must EQUAL the running R
  if (!is.character(rv) || length(rv) != 1L || !nzchar(trimws(rv))) pg_fail("signed manifest does not record r_version (R.version.string)")
  if (!identical(r_version, rv)) pg_fail("running R '%s' differs from the signed r_version '%s'", r_version, rv)
  invisible(TRUE)
}

# Returns list(manifest, listed, role_files, signed_hashes). Opens nothing but prespec/ files; no input path is needed or accepted.
verify_post_signature <- function(repo_root = PRE_R_ROOT, code_files = POST_R_CODE_FILES) {
  top <- suppressWarnings(tryCatch(system2("git", c("-C", shQuote(repo_root), "rev-parse", "--show-toplevel"), stdout = TRUE, stderr = FALSE), error = function(e) character()))
  if (length(top) != 1L || !identical(normalizePath(top), normalizePath(repo_root))) pg_fail("repo root is not the top level of its git repository")
  rec_p <- pg_committed_file(repo_root, "prespec/signing.json"); list_p <- pg_committed_file(repo_root, "prespec/SHA256")
  rec <- pg_read_json(rec_p, "signing.json")
  if (pg_dupkeys(rec)) pg_fail("signing.json repeats a JSON key")
  signer <- rec[["signed_by"]]
  if (!is.list(rec) || !is.character(signer) || length(signer) != 1L || !nzchar(trimws(signer))) pg_fail("signing.json carries no signed_by")
  sd <- rec[["signed_date"]]
  if (!is.character(sd) || length(sd) != 1L || !grepl("^[0-9]{4}-[0-9]{2}-[0-9]{2}$", sd) || is.na(as.Date(sd, optional = TRUE))) pg_fail("signing.json carries no valid ISO signed_date")
  if (!identical(rec[["sha256_list_sha256"]], sha256_file(list_p))) pg_fail("signing.json does not bind the current SHA256 list")
  lines <- pg_read_list(list_p, "SHA256 list")
  m <- regmatches(lines, regexec("^([0-9a-f]{64}) [ *](.+)$", lines))
  if (!length(lines) || any(lengths(m) != 3L)) pg_fail("SHA256 list has a line that is not '<sha256>  <basename>'")
  listed <- setNames(vapply(m, `[`, "", 2), vapply(m, `[`, "", 3))
  if (anyDuplicated(names(listed)) || any(grepl("/", names(listed), fixed = TRUE) | names(listed) %in% c(".", ".."))) pg_fail("SHA256 list has a duplicate or non-basename entry")
  files <- rec[["files"]]
  if (is.list(files) && !setequal(unname(unlist(files)), names(listed))) pg_fail("SHA256 lists entries that are not role files (unchecked), or omits a role file: {%s}", paste(setdiff(names(listed), unname(unlist(files))), collapse = ", "))
  if (!is.list(files) || !setequal(names(files), POST_ROLES) || anyDuplicated(names(files))) pg_fail("signing.json files must be exactly {%s}", paste(POST_ROLES, collapse = ", "))
  nm <- vapply(POST_ROLES, function(r) if (is.character(files[[r]]) && length(files[[r]]) == 1L) files[[r]] else NA_character_, "")
  if (anyNA(nm) || anyDuplicated(nm)) pg_fail("role files must be four distinct basenames")
  role_files <- character()
  for (r in POST_ROLES) {
    if (grepl("/", nm[[r]], fixed = TRUE) || !nm[[r]] %in% names(listed)) pg_fail("role %s: '%s' is not a basename listed in SHA256", r, nm[[r]])
    p <- file.path(repo_root, "prespec", nm[[r]])
    if (pg_islink(p) || !file.exists(p) || isTRUE(file.info(p)$isdir)) pg_fail("role %s: signed file %s is missing or a symlink", r, nm[[r]])
    if (file.info(p)$size == 0) pg_fail("role %s: %s is empty", r, nm[[r]])
    if (!identical(sha256_file(p), unname(listed[[nm[[r]]]]))) pg_fail("role %s: %s does not match its SHA256 entry", r, nm[[r]])
    role_files[[r]] <- p
  }
  verify_post_code_manifest(role_files[["code_manifest"]], repo_root, code_files)
  man <- pg_read_json(role_files[["numerical_manifest"]], "numerical manifest")
  if (!is.list(man)) pg_fail("numerical manifest is not a JSON object")
  if (pg_dupkeys(man)) pg_fail("numerical manifest repeats a JSON key")
  if ("test_only" %in% names(man)) pg_fail("manifest carries test_only: a test-only package build can never be verified or signed")   # MIRROR of gates/g5_post.py check_r_half
  verify_post_package_versions(man)
  signed_hashes <- c(setNames(lapply(POST_ROLES, function(r) unname(listed[[basename(role_files[[r]])]])), POST_ROLES), list(signing_json = sha256_file(rec_p)))
  list(manifest = man, listed = listed, role_files = role_files, signed_by = signer, signed_date = sd, signed_hashes = signed_hashes)
}

# `inputs`: named character vector key -> path (only the keys actually supplied). Call ONLY with a verify_post_signature() result.
# Returns key -> path of the private, hash-verified copies.
verify_post_inputs <- function(sig, inputs) {
  if (!is.list(sig) || is.null(sig$manifest)) pg_fail("signature verification result required before any input is opened")
  want <- sig$manifest[["post_input_file_sha256"]]
  if (!is.list(want) || !length(want) || is.null(names(want)) || anyDuplicated(names(want)) ||
      !all(vapply(want, function(v) is_hex(v, 64), TRUE))) pg_fail("signed manifest has no valid post_input_file_sha256 map")
  keys <- names(inputs)
  if (any(keys %in% POST_REFUSED_KEYS)) pg_fail("input(s) %s are post-window data with no signature-bound producer: refused (the §5.9 list is derived only from --g0-circle-events/--g0-usdt-events + --veto-dir)", paste(intersect(keys, POST_REFUSED_KEYS), collapse = ", "))
  if (anyDuplicated(keys) || !all(keys %in% c(POST_REQUIRED_KEYS, POST_OPTIONAL_KEYS))) pg_fail("unknown or duplicate input key")
  if (length(setdiff(POST_REQUIRED_KEYS, keys))) pg_fail("required input(s) not given: %s", paste(setdiff(POST_REQUIRED_KEYS, keys), collapse = ", "))
  if (length(intersect(keys, POST_G0_KEYS)) %in% 1:2) pg_fail("the g0 source inputs %s are given all together or not at all", paste(POST_G0_KEYS, collapse = ", "))
  if (!setequal(keys, names(want))) pg_fail("inputs given {%s} differ from the signed input mapping {%s}", paste(sort(keys), collapse = ", "), paste(sort(names(want)), collapse = ", "))
  priv <- tempfile("post-verified-"); dir.create(file.path(priv, "pre"), recursive = TRUE, mode = "0700")
  ok <- FALSE; on.exit(if (!ok) unlink(priv, recursive = TRUE), add = TRUE)
  out <- character()
  for (k in keys) {
    src <- inputs[[k]]
    if (!is.character(src) || length(src) != 1L || is.na(src) || !nzchar(src) || pg_islink(src) || !file.exists(src) || isTRUE(file.info(src)$isdir))
      pg_fail("input %s: not a regular, non-symlink file", k)
    dest <- file.path(priv, if (startsWith(k, "pre/")) k else paste0(k, if (k == "analysis_parameters") ".json" else if (k == "g0_receipts") ".jsonl" else ".csv"))
    if (!file.copy(src, dest, overwrite = FALSE)) pg_fail("input %s could not be copied", k)
    if (!identical(sha256_file(dest), want[[k]])) pg_fail("input %s does not match the signed sha256", k)
    out[[k]] <- dest
  }
  ok <- TRUE
  c(out, priv = priv)
}

# ---- post stage (S20): signature + signed input mapping BEFORE any data access, then the S19 estimator on verified private copies ------
run_post_stage <- function(opt, repo_root = PRE_R_ROOT, source_estimator = TRUE, code_files = POST_R_CODE_FILES) {
  flag <- function(name) { v <- opt(name, NA_character_); if (is.na(v) || !nzchar(v) || startsWith(v, "--")) NA_character_ else v }
  req <- function(name) { v <- flag(name); if (is.na(v)) stop(sprintf("--stage post requires --%s (no default; explicit routing)", name), call. = FALSE); v }
  pre_dir <- req("pre-dir"); outdir <- req("outdir")
  paths <- c(`pre/covariates.csv` = file.path(pre_dir, "covariates.csv"), `pre/wallet_coin_day_pre.csv` = file.path(pre_dir, "wallet_coin_day_pre.csv"),
             match_membership = req("match-membership"), analysis_parameters = req("analysis-parameters"))
  post_panel <- req("post-panel")
  optional <- c(eligible_frame_coverage = "eligible-frame-coverage", later_freeze = "later-freeze", usdc_pool_flag = "usdc-pool-flag",
                deposit_counts = "deposit-counts", outflow_evidence = "outflow-evidence", g0_circle_events = "g0-circle-events", g0_usdt_events = "g0-usdt-events", g0_receipts = "g0-receipts")
  for (k in names(optional)) { v <- flag(optional[[k]]); if (!is.na(v)) paths[[k]] <- v }
  veto_dir <- flag("veto-dir")                      # g5_veto output (a directory of post-window files): opened only after the signature passed
  if (xor(is.na(veto_dir), !all(POST_G0_KEYS %in% names(paths)))) pg_fail("--veto-dir and ALL of --g0-circle-events/--g0-usdt-events/--g0-receipts are given together or not at all (the §5.9 list needs source and veto)")
  sig <- verify_post_signature(repo_root, code_files)          # touches the repo's committed prespec/ only; any failure stops here
  vin <- verify_post_inputs(sig, paths)            # first point an input file is opened (copy + hash)
  on.exit(unlink(vin[["priv"]], recursive = TRUE), add = TRUE)
  vin[["post_panel"]] <- verify_post_panel(sig, post_panel, vin[["priv"]], repo_root)   # by chain: schema bound to the current signature, panel bound to the schema
  lf_inputs <- NULL
  if (!is.na(veto_dir)) {                           # shared-provenance failures stop here (hard); any other veto failure becomes veto$ok = FALSE (row-only)
    pe <- sig$manifest[["post_end_block"]]
    if (!is.numeric(pe) || length(pe) != 1L || is.na(pe) || pe != round(pe) || pe <= LF_PRE_END) pg_fail("signed manifest carries no valid post_end_block")
    lf_inputs <- list(circle_events = vin[["g0_circle_events"]], usdt_events = vin[["g0_usdt_events"]], g0_receipts = vin[["g0_receipts"]], post_end_block = as.integer(pe),
                      veto = lf_verify_veto(sig, veto_dir, vin[["priv"]], repo_root, LF_PRE_END, as.integer(pe)))
  }
  if (source_estimator) for (f in c("drdid", "post")) source(file.path(PRE_R_ROOT, "R", paste0(f, ".R")))
  g <- function(k) if (k %in% names(vin)) vin[[k]] else NULL
  invisible(run_post(dirname(vin[["pre/covariates.csv"]]), vin[["post_panel"]], vin[["match_membership"]], vin[["analysis_parameters"]], outdir,
                     eligible_frame_coverage_path = g("eligible_frame_coverage"), later_freeze_inputs = lf_inputs, usdc_pool_flag_path = g("usdc_pool_flag"),
                     deposit_counts_path = g("deposit_counts"), outflow_evidence_path = g("outflow_evidence")))
}

# The post-window panel is verified by chain, not by a pre-signature hash: sibling g5 schema bound to the CURRENT signature, panel bound to the schema.
# THE CHAIN IS AN ACCIDENT GUARD, NOT A SECURITY BOUNDARY: the schema is g5's own unsigned output, so anyone who can write the panel
# directory can forge schema + panel together. It catches a panel from another/previous signature or a hand-edited CSV; the
# `producing_commit` it records must at least be an ancestor of the repo's HEAD (a panel produced by code outside this history is refused).
# Returns the path of a private verified copy (inside `priv`).
verify_post_panel <- function(sig, panel_path, priv, repo_root = PRE_R_ROOT) {
  if (!is.list(sig) || is.null(sig$signed_hashes)) pg_fail("signature verification result required before the post panel is opened")
  if (!identical(basename(panel_path), "post_wallet_coin_day.csv")) pg_fail("post panel must be g5's post_wallet_coin_day.csv (got %s)", basename(panel_path))
  sp <- file.path(dirname(panel_path), "post_wallet_coin_day.schema.json")
  for (q in c(panel_path, sp)) if (pg_islink(q) || !file.exists(q) || isTRUE(file.info(q)$isdir)) pg_fail("%s is missing, a directory or a symlink", basename(q))
  sc <- tryCatch(jsonlite::fromJSON(sp, simplifyVector = FALSE), error = function(e) pg_fail("g5 schema is not valid JSON"))
  have <- sc$signed_file_sha256
  if (!is.list(have) || !setequal(names(have), names(sig$signed_hashes)) ||
      !all(vapply(names(sig$signed_hashes), function(k) identical(have[[k]], sig$signed_hashes[[k]]), TRUE)))
    pg_fail("the panel's g5 manifest was not produced under the CURRENT signature (signed_file_sha256 differs)")
  if (!is_hex(sc$sha256, 64)) pg_fail("g5 schema carries no panel sha256")
  pc <- sc$producing_commit
  if (!is.character(pc) || length(pc) != 1L || !is_hex(pc, 40)) pg_fail("g5 schema carries no valid producing_commit")
  if (!identical(as.integer(pg_git(repo_root, c("merge-base", "--is-ancestor", shQuote(pc), "HEAD"))), 0L)) pg_fail("g5 producing_commit %s is not an ancestor of HEAD", pc)
  dest <- file.path(priv, "post_panel.csv")
  if (!file.copy(panel_path, dest, overwrite = FALSE)) pg_fail("post panel could not be copied")
  if (!identical(sha256_file(dest), sc$sha256)) pg_fail("post panel does not match the sha256 in its g5 manifest")
  dest
}
