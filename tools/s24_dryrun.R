#!/usr/bin/env Rscript
# S24 assembler dry run: calls the SAME pre-window function the post stage calls (R/post.R po_pre_stage: coverage-file checks, required wallets,
# matched wallets with baseline B <= 0, recomputed cap values vs the frozen cap_values, 60 baseline days per eligible wallet) plus the pool/deposit
# readers, on the real candidate inputs. Reads pre-window inputs only; opens no post panel; writes nothing but stdout.
# It does NOT cover the stops that need post data or the g0 files: post panel reader, g0 provenance (pg_fail), the rematch cut-point/hash downgrade,
# and malformed g0 receipts (see the semantic_checks docstring in tools/s24_package.py).
# Usage: Rscript tools/s24_dryrun.R <cov.csv> <wcd.csv> <match_membership> <analysis_parameters> <eligible_frame_coverage|-> <usdc_pool_flag|-> <deposit_counts|->
# Output (one JSON object on the last stdout line): population_id, generation_id, matched / retained (always arrays, or retained null), cap_values
# (recomputed), drdid_trimming (DRDID_FREEZE$trimming), r_version.
args <- commandArgs(TRUE)
stopifnot(length(args) == 7L)
for (f in c("utils", "decide", "cem", "estimate", "validate", "synth", "pre", "blacklist", "presign", "drdid", "post")) source(file.path("R", paste0(f, ".R")))
opt <- function(x) if (identical(x, "-")) NULL else x
params <- po_read_params(args[4]); po_check_population(params$population_id, params$generation_id)
mem <- po_read_membership(args[3], params)
pre_dir <- tempfile("s24-pre-"); dir.create(pre_dir)
file.copy(args[1], file.path(pre_dir, "covariates.csv")); file.copy(args[2], file.path(pre_dir, "wallet_coin_day_pre.csv"))
st <- po_pre_stage(pre_dir, params, mem, opt(args[5]))
# PRE-SIGN coverage (tribunal chg-e5801bb3): deposit_counts must cover every MATCHED exposed wallet with non-negative integers, usdc_pool_flag every
# ELIGIBLE exposed wallet (all arm == exposed rows) with TRUE/FALSE. A gap would make the post stage refuse after post data was read.
if (!is.null(opt(args[6]))) {
  fl <- po_read_addr_file(args[6], c("address", "usdc_pool_depositor"), "usdc_pool_flag")
  elig <- mem$all[arm == "exposed", address]
  if (anyDuplicated(fl$address) || !all(fl$usdc_pool_depositor %in% c("TRUE", "FALSE"))) po_stop("usdc_pool_flag: duplicate address or value not TRUE/FALSE")
  if (length(setdiff(elig, fl$address))) po_stop("usdc_pool_flag lacks %d eligible exposed wallet(s)", length(setdiff(elig, fl$address)))
  if (length(setdiff(fl$address, elig)) || nrow(fl) != length(elig)) po_stop("usdc_pool_flag is not exactly the eligible exposed set (%d rows, %d eligible, %d unexpected address(es))", nrow(fl), length(elig), length(setdiff(fl$address, elig)))
}
if (!is.null(opt(args[7]))) {
  dc <- po_read_addr_file(args[7], c("address", "deposit_count"), "deposit_counts")
  if (anyDuplicated(dc$address) || !all(grepl("^[0-9]+$", dc$deposit_count))) po_stop("deposit_counts: duplicate address or deposit_count not a non-negative integer")
  mexp <- mem$matched[treat == TRUE, uid]
  if (length(setdiff(mexp, dc$address))) po_stop("deposit_counts lacks %d matched exposed wallet(s)", length(setdiff(mexp, dc$address)))
  if (length(setdiff(dc$address, mexp)) || nrow(dc) != length(mexp)) po_stop("deposit_counts is not exactly the matched exposed set (%d rows, %d matched exposed, %d unexpected address(es))", nrow(dc), length(mexp), length(setdiff(dc$address, mexp)))
}
arr <- function(x) if (is.null(x)) NULL else I(as.character(x))     # I(): a length-1 vector stays a JSON array under auto_unbox
cat(jsonlite::toJSON(list(population_id = params$population_id, generation_id = params$generation_id, matched = arr(mem$matched$uid), retained = arr(st$retained),
                          cap_values = list(D = I(st$caps$D), A = I(st$caps$A), legs_pooled = I(st$caps$legs)), drdid_trimming = DRDID_FREEZE$trimming,
                          r_version = R.version.string), auto_unbox = TRUE, null = "null", digits = NA), "\n")
