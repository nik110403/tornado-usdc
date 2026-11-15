#!/usr/bin/env Rscript
# EXPLORATORY, POST-HOC VARIANT, dated 2026-10-30; disclosed under plan 3.2 beside the as-planned row; not confirmatory; does not reclassify H1.
# Post-hoc variants of the O1 age-restricted secondary row (plan 2.5). exploratory/o1_estimate.R is NOT edited: the as-planned run stands.
# Two findings of the Lens-B verifier are addressed:
#   F1: o1_estimate.R feeds cem_match_fixed() the cut-points read from analysis_parameters.json, rounded to 15 digits; the exact type-7 cut-points
#       (R/post.R po_primary_cuts, hashed at %.17g) move some exposed wallets to another stratum. Both variants here use the EXACT cut-points and
#       STOP unless their hash equals params$cutpoints_hash.
#   F4: the as-planned row re-draws controls 3:1 inside the restricted set and drops admitted controls.
# V1 exact cuts          : cem_match_fixed(fr, CEM_VARS, exact cuts, "three_bins"), 3:1 redraw, default seed (the prespec formula).
# V2 exact cuts, no redraw: strata as assigned with the exact cuts; every admitted control of a stratum with an exposed wallet is kept; IKP weights
#                           (m_C/m_T) * (n_exposed / n_ctrl_sel) with n_ctrl_sel = ALL admitted controls in the stratum; exposed wallets without a control dropped.
# Estimation, estimands and tie-break call the same o1_analyse / o1_flatten / o1_tiebreak as o1_estimate.R (sourced as a library, O1_NO_MAIN).
#
# Usage: Rscript exploratory/o1_restricted_posthoc.R --repo-root <tree holding out/recovery> --o1-dir <o1_extract.py --out dir> --outdir <new dir under out/exploratory/>
#        [--high-ge 0.20] [--low-le 0.10] [--stable-tol 0.05] [--disclose-looser-threshold "<text>"]
# Same argument handling and output guard as o1_estimate.R. Single-threaded. Sourcing with PH_NO_MAIN <- TRUE defines the functions only (tests/test_o1_restricted_posthoc.R).
PH_DATE <- "2026-10-30"
PH_BANNER <- sprintf("EXPLORATORY, POST-HOC VARIANT, dated %s; disclosed under plan 3.2 beside the as-planned row; not confirmatory; does not reclassify H1", PH_DATE)

if (!exists("o1_analyse")) {
  .a <- grep("^--file=", commandArgs(FALSE), value = TRUE)
  PH_ROOT <- normalizePath(file.path(dirname(sub("^--file=", "", .a[1])), ".."))
  setwd(PH_ROOT)
  for (f in file.path(PH_ROOT, "R", paste0(c("utils", "decide", "cem", "estimate", "pre", "presign", "drdid", "post"), ".R"))) source(f, local = FALSE)
  O1_NO_MAIN <- TRUE
  source(file.path(PH_ROOT, "exploratory", "o1_estimate.R"))
}

# ---- exact cut-points, guarded -----------------------------------------------------------------------------------------------------------------
# Recomputes the exact type-7 cut-points from the eligible frame; stops unless they reproduce the frozen hash (and the seed is the primary one).
ph_exact_cuts <- function(frame, params) {
  pc <- po_primary_cuts(frame)
  if (!identical(pc$hash, params$cutpoints_hash)) stop("exact cut-points recomputed from the frame do not reproduce params$cutpoints_hash: stop (this is not the frozen frame)", call. = FALSE)
  if (!is.null(params$seed) && as.integer(params$seed) != SEED_PRIMARY_CEM) stop("params$seed differs from SEED_PRIMARY_CEM, the as-planned default: stop", call. = FALSE)
  pc
}
# bin key per wallet (address order) and stratum ids exactly as cem_match_fixed numbers them (three_bins USDC share, first-appearance ids)
ph_keys <- function(frame, cuts) {
  cov <- copy(as.data.table(frame)); setorderv(cov, "address")
  keys <- lapply(CEM_VARS, function(v) {
    if (anyNA(cov[[v]]) && v != "age") stop(sprintf("CEM: missing values in '%s'; run stopped, no imputation", v), call. = FALSE)
    b <- findInterval(cov[[v]], cuts[[v]]); b[is.na(cov[[v]])] <- -1L; b })
  keys$usdc_share <- coarsen_usdc_share(cov$usdc_share, NULL, "three_bins", 3L)
  cov[, key := do.call(paste, c(unname(lapply(keys, as.integer)), sep = "|"))]
  cov[, stratum := match(key, unique(key))]
  cov[, .(uid, treat, key, stratum)]
}
ph_strata <- function(frame, cuts) ph_keys(frame, cuts)[, .(uid, treat, stratum)]
# cut_frame: the frame the frozen cut-points were built on (the whole eligible frame); frame may be the restricted subset, which must not re-derive cuts.
ph_v1 <- function(frame, params, cut_frame = frame) {
  pc <- ph_exact_cuts(cut_frame, params)
  res <- cem_match_fixed(frame, CEM_VARS, pc$cuts, "three_bins")
  res$cuts <- pc$cuts; res
}
ph_v2 <- function(frame, params, cut_frame = frame) {
  pc <- ph_exact_cuts(cut_frame, params)
  s <- ph_strata(frame, pc$cuts)
  st <- s[, .(n_exposed = sum(treat), n_ctrl_sel = sum(!treat)), by = stratum]       # n_ctrl_sel = ALL admitted controls of the stratum
  st[, kept := n_exposed > 0 & n_ctrl_sel > 0]
  m_T <- st[kept == TRUE, sum(n_exposed)]; m_C <- st[kept == TRUE, sum(n_ctrl_sel)]
  if (!(m_T > 0 && m_C > 0)) stop("CEM: no stratum holds both arms", call. = FALSE)
  st[, ctrl_weight := ifelse(kept, (m_C / m_T) * (n_exposed / n_ctrl_sel), 0)]
  m <- merge(s, st[, .(stratum, kept, ctrl_weight)], by = "stratum")[kept == TRUE, .(uid, treat, stratum, w = fifelse(treat, 1, ctrl_weight))]
  setorder(m, uid)
  list(matched = m, n_exposed = sum(s$treat), dropped_exposed = sum(st[kept == FALSE, n_exposed]), n_controls_matched = sum(!m$treat), strata = st, m_T = m_T, m_C = m_C, cuts = pc$cuts)
}

# one variant -> list(res, analysis, ok, dropped, counts) or list(error)
ph_run_variant <- function(builder, fr, params, wide_ext, ok_uid, caps, jumps, params_o1, matched_signed, cut_frame = fr) {
  tryCatch({
    res <- builder(fr, params, cut_frame)
    pan <- po_panel(wide_ext[uid %in% res$matched$uid], res$matched)
    ok_r <- intersect(ok_uid, res$matched$uid)
    a <- o1_analyse(pan, ok_r, caps, jumps, params_o1)
    arm <- function(x) c(exposed = sum(x$treat), control = sum(!x$treat))
    ar <- arm(fr); mr <- arm(res$matched); ok_t <- res$matched[uid %in% ok_r]; okr <- arm(ok_t)
    counts <- data.table(arm = names(ar), n_admitted_in_frame = as.integer(ar), n_matched = as.integer(mr), n_matched_with_o1_path = as.integer(okr),
                         n_admitted_not_matched = as.integer(ar - mr), n_signed_matched = as.integer(arm(matched_signed)))
    list(res = res, analysis = a, ok = ok_r, dropped = o1_dropped_share(matched_signed, res$matched$uid), counts = counts)
  }, error = function(e) list(error = conditionMessage(e)))
}

# ================================================= main =====================================================================================
if (!exists("PH_NO_MAIN")) local({
  argv <- commandArgs(TRUE)
  opt <- function(name, default = NULL) {
    i <- match(paste0("--", name), argv)
    if (is.na(i) || i == length(argv) || startsWith(argv[i + 1L], "--")) { if (is.null(default)) stop(sprintf("--%s is required", name), call. = FALSE); return(default) }
    argv[i + 1L]
  }
  known <- c("--repo-root", "--o1-dir", "--outdir", "--high-ge", "--low-le", "--stable-tol", "--disclose-looser-threshold")
  if (length(bad <- setdiff(argv[startsWith(argv, "--")], known))) stop("unknown argument(s): ", paste(bad, collapse = " "), call. = FALSE)
  main_root <- normalizePath(opt("repo-root"), mustWork = TRUE)
  lex_clean <- function(p) {
    p <- path.expand(p); if (!startsWith(p, "/")) p <- file.path(getwd(), p)
    parts <- strsplit(p, "/+")[[1]]; out <- character()
    for (q in parts[nzchar(parts)]) if (q == "..") out <- head(out, -1L) else if (q != ".") out <- c(out, q)
    paste0("/", paste(out, collapse = "/"))
  }
  out_guard <- function(p) if (grepl("(^|/)out/recovery(/|$)", p) || (startsWith(p, file.path(main_root, "out")) && !startsWith(p, file.path(main_root, "out", "exploratory", "")))) stop("refusing to write under out/ except out/exploratory/<dir> (never into out/recovery/)", call. = FALSE)
  outdir <- lex_clean(opt("outdir")); out_guard(outdir)
  if (dir.exists(outdir) && length(list.files(outdir, all.files = TRUE, no.. = TRUE))) stop("outdir must be new or empty: ", outdir, call. = FALSE)
  local({ a <- outdir; rest <- character(); while (!file.exists(a) && a != "/") { rest <- c(basename(a), rest); a <- dirname(a) }
          out_guard(do.call(file.path, as.list(c(normalizePath(a, mustWork = TRUE), rest)))) })
  o1_dir <- normalizePath(opt("o1-dir"), mustWork = TRUE)
  dir.create(outdir, recursive = TRUE, showWarnings = FALSE); outdir <- normalizePath(outdir, mustWork = TRUE); out_guard(outdir)
  params_o1 <- O1_DEFAULTS
  params_o1$high_ge <- as.numeric(opt("high-ge", params_o1$high_ge)); params_o1$low_le <- as.numeric(opt("low-le", params_o1$low_le))
  params_o1$stable_tol <- as.numeric(opt("stable-tol", params_o1$stable_tol))

  script_root <- PH_ROOT
  script_path <- file.path(script_root, "exploratory", "o1_restricted_posthoc.R")
  stopifnot(requireNamespace("jsonlite", quietly = TRUE)); library(jsonlite)
  fixest::setFixest_nthreads(1L); data.table::setDTthreads(1L)
  wcsv <- function(dt, f) fwrite(cbind(data.table(banner = PH_BANNER), dt), file.path(outdir, f))

  # ---- extraction output must be complete and untampered (as o1_estimate.R)
  man <- readLines(file.path(o1_dir, "MANIFEST.sha256")); mh <- sub("  .*$", "", man); mn <- sub("^[0-9a-f]+  ", "", man)
  o1_check_manifest(mn)
  for (i in seq_along(man)) if (!identical(sha256_file(file.path(o1_dir, mn[i])), mh[i])) stop("o1 extraction file differs from its MANIFEST: ", mn[i], call. = FALSE)
  o1sum <- fromJSON(file.path(o1_dir, "o1_summary.json"), simplifyVector = FALSE)
  if (isTRUE(o1sum$aborted)) stop("o1 extraction was aborted; no estimation", call. = FALSE)
  setting_dev <- o1_check_extract_settings(o1sum$params, opt("disclose-looser-threshold", ""))

  # ---- signed inputs (as o1_estimate.R)
  REC <- file.path(main_root, "out", "recovery")
  inp_paths <- c(pre_covariates = file.path(REC, "main/gen-M/covariates.csv"), pre_panel = file.path(REC, "main/gen-M/wallet_coin_day_pre.csv"),
                 post_panel = file.path(REC, "post/gen-M-post/post_wallet_coin_day.csv"), match_membership = file.path(REC, "presign/main-gen-M-20261027/match_membership.csv"),
                 analysis_parameters = file.path(REC, "presign/main-gen-M-20261027/analysis_parameters.json"),
                 eligible_frame_coverage = file.path(REC, "presign/main-gen-M-20261027/eligible_frame_coverage.csv"))
  SIGNED <- file.path(REC, "post/r-gen-M-post")
  trace <- fromJSON(file.path(SIGNED, "post_trace.json"), simplifyVector = FALSE)
  o1_check_input_hashes(o1sum$params$input_hashes, trace$input_sha256)
  hash_tab <- rbindlist(lapply(names(inp_paths), function(k) data.table(input = k, sha256 = sha256_file(inp_paths[[k]]), signed_sha256 = trace$input_sha256[[k]])))
  hash_tab[, ok := sha256 == signed_sha256]; if (!all(hash_tab$ok)) stop("input sha256 differs from post_trace.json input_sha256: stop", call. = FALSE)
  wcsv(hash_tab, "ph_input_hashes.csv")
  params <- po_read_params(inp_paths[["analysis_parameters"]]); po_check_population(params$population_id, params$generation_id)
  mem <- po_read_membership(inp_paths[["match_membership"]], params)
  pre <- po_pre_stage(dirname(inp_paths[["pre_covariates"]]), params, mem, inp_paths[["eligible_frame_coverage"]])

  # ---- step 1: exact cut-points must reproduce the frozen hash, before anything heavy
  pc <- ph_exact_cuts(pre$frame, params)
  cat("exact cut-points reproduce params$cutpoints_hash:", pc$hash, "\n")
  cut_diff <- rbindlist(lapply(names(pc$cuts), function(v) data.table(var = v, k = seq_along(pc$cuts[[v]]), exact = sprintf("%.17g", pc$cuts[[v]]), json_rounded = sprintf("%.17g", params$cutpoints[[v]][seq_along(pc$cuts[[v]])]))))
  cut_diff[, identical := exact == json_rounded]; wcsv(cut_diff, "ph_cutpoints_exact_vs_json.csv")
  # F1 diagnostic: wallets of the admitted-eligible frame whose stratum key differs between exact and JSON-rounded cut-points
  k_ex <- ph_keys(pre$frame, pc$cuts); k_js <- ph_keys(pre$frame, params$cutpoints)
  mv <- merge(k_ex[, .(uid, treat, key_exact = key)], k_js[, .(uid, key_json = key)], by = "uid")[key_exact != key_json]
  wcsv(rbind(mv[, .(n_moved = .N), by = .(arm = fifelse(treat, "exposed", "control"))], data.table(arm = c("exposed", "control"), n_moved = 0L))[, .(n_moved = sum(n_moved)), by = arm], "ph_cutpoint_rounding_moved_wallets.csv")

  # ---- panel, caps, full-sample paths (as o1_estimate.R)
  post <- po_read_post_panel(inp_paths[["post_panel"]], params, required_addresses = pre$need_addr, exact_addresses = pre$retained)
  wide_signed <- rbind(pre$wide_pre[uid %in% pre$need_addr], post$wide)
  op <- fread(file.path(o1_dir, "o1_panel.csv"), select = c("address", "coin", "day_index", "balance_units"), colClasses = c(address = "character", coin = "character", day_index = "integer", balance_units = "character"))
  op[, bal := as.numeric(balance_units)]
  ow <- dcast(op[, .(uid = address, day_index, coin, bal)], uid + day_index ~ coin, value.var = "bal"); setnames(ow, c("USDC", "USDT"), c("usdc_units", "usdt_units"))
  if (any(ow$day_index >= 0L)) stop("o1 panel carries non-negative day_index rows: refused", call. = FALSE)
  if (length(bad <- setdiff(unique(ow$uid), mem$matched$uid))) stop(length(bad), " o1 panel wallets are not in the signed matched sample", call. = FALSE)
  ok_uid <- unique(ow$uid)
  wide_ext <- rbind(wide_signed, ow[, names(wide_signed), with = FALSE])
  rm(op, ow, wide_signed); invisible(gc())
  panel <- po_panel(wide_ext, mem$matched)
  caps_signed <- list(D = as.numeric(params$cap_values$D), A = as.numeric(params$cap_values$A))
  caps_re <- cap_values_all(panel, POST_EVENT_DAY)
  for (k in c("D", "A")) if (!isTRUE(all.equal(caps_re[[k]], caps_signed[[k]], tolerance = 1e-9))) stop("recomputed ", k, " caps differ from the frozen cap_values", call. = FALSE)
  tp <- fread(file.path(SIGNED, "post_time_path.csv"))
  jumps <- lapply(c(D = "D", A = "A"), function(o) { r <- tp[outcome == o & day_index == 61L]; stopifnot(nrow(r) == 1L, r$status == "estimated"); r$coef })
  matched <- mem$matched
  full <- o1_analyse(panel, ok_uid, caps_signed, jumps, params_o1)
  rm(panel); invisible(gc())
  flat_full <- o1_flatten(full$estimands, "full")
  wcsv(flat_full, "full_estimands.csv")

  # ---- age predicate and admitted set (as o1_estimate.R)
  pin_block <- o1sum$params$pin
  pb <- fread(file.path(o1_dir, "o1_pin_balances.csv"), select = c("address", "coin", "balance_units"), colClasses = "character")
  pinw <- dcast(pb[, .(address, coin, v = suppressWarnings(as.numeric(balance_units)))], address ~ coin, value.var = "v"); setnames(pinw, c("USDC", "USDT"), c("usdc", "usdt"))
  cov_fs <- fread(inp_paths[["pre_covariates"]], select = c("address", "first_send_block"), colClasses = c(address = "character", first_send_block = "character"))
  pred <- o1_age_predicate(cov_fs[address %in% matched$uid], pinw, pin_block)
  arm_of <- setNames(fifelse(matched$treat, "exposed", "control"), matched$uid)
  wcsv(o1_admissions(copy(pred), arm_of), "ph_restricted_admissions.csv")
  fr <- pre$frame[uid %in% pred[admitted == TRUE, address]]

  # ---- the two variants
  variants <- list(V1_exact_cuts_3to1 = ph_v1, V2_exact_cuts_no_redraw = ph_v2)
  summ <- character(); vsum <- list()
  for (vn in names(variants)) {
    tag <- tolower(substr(vn, 1, 2))
    r <- ph_run_variant(variants[[vn]], fr, params, wide_ext, ok_uid, caps_signed, jumps, params_o1, matched, cut_frame = pre$frame)
    if (!is.null(r$error)) {
      cat(vn, "not estimable:", r$error, "\n")
      wcsv(o1_dropped_share(matched, character()), sprintf("%s_dropped_weight.csv", tag)); wcsv(data.table(status = "restricted row not estimable", reason = r$error), sprintf("%s_status.csv", tag))
      flat_r <- NULL
    } else {
      wcsv(r$dropped, sprintf("%s_dropped_weight.csv", tag)); wcsv(r$counts, sprintf("%s_wallet_counts.csv", tag)); wcsv(r$analysis$paths, sprintf("%s_paths.csv", tag))
      wcsv(r$res$matched[, .(n_exposed = sum(treat), n_ctrl_sel = sum(!treat), ctrl_weight = if (any(!treat)) w[!treat][1] else NA_real_), by = stratum][order(stratum)], sprintf("%s_strata.csv", tag))
      for (nm in names(r$analysis$fits)) if (is.null(r$analysis$fits[[nm]]$error))
        saveRDS(list(banner = PH_BANNER, variant = vn, fit = r$analysis$fits[[nm]]), file.path(outdir, sprintf("%s_fit_%s.rds", tag, gsub("[|]", "_", nm))))   # fit$V = restricted covariance
      flat_r <- o1_flatten(r$analysis$estimands, paste0("restricted_", vn))
      wcsv(flat_r, sprintf("%s_estimands.csv", tag))
    }
    tb <- rbindlist(lapply(full$estimands, function(e) {
      if (!identical(e$status, "estimated") || e$variant != "capped") return(NULL)
      rr <- if (is.null(r$error)) Find(function(x) identical(x$status, "estimated") && x$variant == "capped" && x$outcome == e$outcome && x$cluster == e$cluster, r$analysis$estimands) else NULL
      t <- o1_tiebreak(e$est, if (is.null(rr)) NULL else rr$est)
      data.table(variant = vn, outcome = e$outcome, cluster = e$cluster, e3_band_final = t$e3_band, stable_final = t$stable, reading = t$reading, row = t$row,
                 downgraded = t$downgraded, e4_trigger = t$e4_trigger, note = t$note)
    }))
    wcsv(tb, sprintf("%s_tiebreak.csv", tag))
    vsum[[vn]] <- list(r = r, tb = tb)
  }

  # ---- summary.md
  fmt_dt <- function(dt) c(paste(names(dt), collapse = " | "), vapply(seq_len(nrow(dt)), function(i) paste(vapply(dt[i], function(x) format(x, digits = 6), ""), collapse = " | "), ""))
  L <- c(PH_BANNER, "",
         sprintf("Post-hoc variants of the O1 age-restricted row. Exact type-7 cut-points reproduce params$cutpoints_hash (%s); the as-planned row (o1_estimate.R) used the 15-digit JSON cut-points.", pc$hash),
         "V1: exact cuts, 3:1 control redraw (prespec formula, default seed). V2: exact cuts, no redraw (all admitted controls of a stratum with an exposed wallet kept; weights (m_C/m_T)*(n_exposed/n_ctrl_sel)).",
         "Signed caps stay in force; tie-break against the signed full-sample row (o1_tiebreak, capped variant). Parameters as o1_estimate.R defaults unless given on the command line.",
         if (length(setting_dev)) paste0("DISCLOSED looser extraction settings: ", paste(setting_dev, collapse = "; ")) else "Extraction settings: plan defaults.", "")
  for (vn in names(vsum)) {
    r <- vsum[[vn]]$r
    L <- c(L, sprintf("## %s", vn), "")
    if (!is.null(r$error)) { L <- c(L, paste("not estimable:", r$error), ""); next }
    L <- c(L, "Wallet counts per arm:", "", fmt_dt(r$counts), "", "Dropped (relative to the signed matched sample; count and weight share by arm):", "", fmt_dt(r$dropped), "", "Tie-break against the signed full-sample row:", "", fmt_dt(vsum[[vn]]$tb[, .(outcome, cluster, e3_band_final, stable_final, reading, downgraded, e4_trigger)]), "")
  }
  L <- c(L, "Files: v1_* / v2_* (paths, estimands, tiebreak, dropped_weight, wallet_counts, strata, fit rds with the restricted vcov), full_estimands.csv, ph_*.csv, PROVENANCE.txt, MANIFEST.sha256.")
  writeLines(L, file.path(outdir, "summary.md"))

  # ---- PROVENANCE.txt and MANIFEST.sha256 (manifest covers every file including summary.md and PROVENANCE.txt, except itself)
  gitq <- function(...) tryCatch(system2("git", c("-C", shQuote(script_root), ...), stdout = TRUE, stderr = FALSE), error = function(e) "git unavailable")
  code_paths <- c("R", "exploratory", "config", "prespec", "gates", "run_all.py")
  code_paths <- code_paths[file.exists(file.path(script_root, code_paths))]
  hs <- function(p) sprintf("%s  %s", sha256_file(p), sub(paste0("^", script_root, "/"), "", p))
  prov <- c(PH_BANNER, "", paste("git HEAD:", gitq("rev-parse", "HEAD")), "", "git status --porcelain (code files):", gitq("status", "--porcelain", "--", code_paths), "(end of status)", "",
            "sha256 of code:", hs(script_path), hs(file.path(script_root, "exploratory", "o1_estimate.R")), hs(file.path(script_root, "R", "post.R")), "",
            "sha256 of inputs (the six signed inputs, equal to post_trace.json input_sha256):", sprintf("%s  %s", hash_tab$sha256, hash_tab$input), "",
            "o1 extraction MANIFEST.sha256 (verified):", man, "", paste("cutpoints_hash (exact == frozen):", pc$hash))
  writeLines(prov, file.path(outdir, "PROVENANCE.txt"))
  fl <- sort(setdiff(list.files(outdir), "MANIFEST.sha256"))
  writeLines(sprintf("%s  %s", vapply(file.path(outdir, fl), sha256_file, ""), fl), file.path(outdir, "MANIFEST.sha256"))
  cat("done:", outdir, "\n")
})
