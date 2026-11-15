#!/usr/bin/env Rscript
# EXPLORATORY, DESCRIPTIVE, computed 2026-11-05 after the signed results; no test, no estimate, selects nothing.
# Baseline balance of the signed gen-M matched sample (exposed vs CEM-weighted controls). Baseline (9 Jun-7 Aug 2022) data only.
# Usage: Rscript exploratory/balance_table.R [--outdir out/exploratory/balance-2026-11-05]
suppressMessages(library(data.table))
argv <- commandArgs(TRUE)
outdir <- if (length(argv) >= 2 && argv[1] == "--outdir") argv[2] else "out/exploratory/balance-2026-11-05"
stopifnot(startsWith(normalizePath(outdir, mustWork = FALSE), normalizePath("out/exploratory", mustWork = FALSE)))
dir.create(outdir, recursive = TRUE, showWarnings = FALSE)
MEM <- "out/recovery/presign/main-gen-M-20261027/match_membership.csv"
COV <- "out/recovery/main/gen-M/covariates.csv"
PRE <- "out/recovery/main/gen-M/wallet_coin_day_pre.csv"
SIGNED_CAP_D <- c(-3.0927, 4.4768); SIGNED_CAP_A <- c(0, 5.3170); SIGNED_WD <- 958920L

mem <- fread(MEM); cov <- fread(COV, colClasses = list(character = c("eth_balance_wei_units")))
x <- merge(mem[, .(address, arm, matched, weight, stratum)], cov, by = "address", suffixes = c("", "_cov"))
stopifnot(nrow(x) == nrow(mem), nrow(x) == 76280L)
x[, group := fifelse(arm == "exposed" & matched, "exposed_matched",
             fifelse(arm == "control" & matched, "control_matched",
             fifelse(arm == "control", "control_unmatched", "exposed_unmatched")))]
# baseline wallet-day panel (pre file only)
pre <- dcast(fread(PRE, select = c("wallet_id", "coin", "day_index", "balance_units"))[
  wallet_id %in% x$wallet_id], wallet_id + day_index ~ coin, value.var = "balance_units")
pre[, tot := (USDC + USDT) / 1e6]
B <- pre[, .(B = mean(tot)), by = wallet_id]
pre <- merge(pre, B, by = "wallet_id")[B > 0]
pre[, `:=`(D = (USDC - USDT) / 1e6 / B, A = tot / B)]
# signed cap rule (prespec s2): 1/99 type-7 over all baseline wallet-days of the matched sample, both groups pooled, unweighted
mids <- x[matched == TRUE, wallet_id]
bm <- pre[wallet_id %in% mids]
capD <- quantile(bm$D, c(.01, .99), type = 7, names = FALSE); capA <- quantile(bm$A, c(.01, .99), type = 7, names = FALSE)
stopifnot(nrow(bm) == SIGNED_WD, max(abs(round(capD, 4) - SIGNED_CAP_D)) < 1e-9, max(abs(round(capA, 4) - SIGNED_CAP_A)) < 1e-9)
pre[, `:=`(Dc = pmin(pmax(D, capD[1]), capD[2]), Ac = pmin(pmax(A, capA[1]), capA[2]))]
w <- pre[, .(base_D = mean(Dc), base_A = mean(Ac), B_panel = B[1]), by = wallet_id]
x <- merge(x, w, by = "wallet_id", all.x = TRUE)
x[, `:=`(hold_usd = as.numeric(mean_total_units) / 1e6, usdc_share = usdc_share_ppm / 1e6, transfers = transfer_count_total,
         age_blocks = wallet_age_blocks, eth = as.numeric(eth_balance_wei_units) / 1e18, never_sent = as.numeric(is.na(wallet_age_blocks)))]
stopifnot(x[matched == TRUE, !anyNA(B_panel) && max(abs(B_panel - hold_usd) / hold_usd) < 1e-6])
vars <- c(hold_usd = "Mean holdings B (USD)", usdc_share = "USDC share", transfers = "Transfer count (baseline)",
          age_blocks = "Wallet age (blocks; sent-wallets only)", never_sent = "Never-sent (share)",
          eth = "ETH balance (ETH)", base_D = "Baseline D (capped)", base_A = "Baseline A (capped)")
src <- c(hold_usd = "covariates.csv:mean_total_units/1e6", usdc_share = "covariates.csv:usdc_share_ppm/1e6",
         transfers = "covariates.csv:transfer_count_total", age_blocks = "covariates.csv:wallet_age_blocks",
         never_sent = "covariates.csv:wallet_age_blocks is NA", eth = "covariates.csv:eth_balance_wei_units/1e18",
         base_D = "wallet_coin_day_pre.csv:balance_units(USDC,USDT); capped D mean over 60 days",
         base_A = "wallet_coin_day_pre.csv:balance_units(USDC,USDT); capped A mean over 60 days")
wm <- function(v, ww) { k <- !is.na(v); v <- v[k]; ww <- ww[k]; m <- sum(ww * v) / sum(ww); c(m, sqrt(sum(ww * (v - m)^2) / sum(ww)), length(v)) }
x[, wt := fifelse(matched, weight, 1)]   # unmatched controls: unweighted
rows <- rbindlist(lapply(names(vars), function(v) {
  g <- function(gr) wm(x[group == gr, get(v)], x[group == gr, wt])
  e <- g("exposed_matched"); cm <- g("control_matched"); u <- g("control_unmatched")
  data.table(variable = vars[[v]], source = src[[v]],
    exposed_mean = e[1], exposed_sd = e[2], exposed_n = e[3],
    control_mean = cm[1], control_sd = cm[2], control_n = cm[3],
    smd = (e[1] - cm[1]) / sqrt((e[2]^2 + cm[2]^2) / 2),
    unmatched_control_mean = u[1], unmatched_control_n = u[3])
}))
fwrite(rows, file.path(outdir, "balance.csv"))
nE <- x[group == "exposed_matched", .N]; nC <- x[group == "control_matched", .N]; sw <- x[group == "control_matched", sum(weight)]
nU <- x[group == "control_unmatched", .N]
stopifnot(nE == 4487L, nC == 11495L)
cat(sprintf("N exposed matched %d; controls matched %d; sum control weights %.4f; unmatched controls %d; exposed unmatched %d\n", nE, nC, sw, nU, x[group == "exposed_unmatched", .N]))
f3 <- function(v) gsub("-", "−", formatC(v, format = "f", digits = 3, big.mark = ","), fixed = TRUE)
tb <- rows[, sprintf("| %s | %s | %s | %s | %s | %s | %s | %s |", variable, f3(exposed_mean), f3(exposed_sd), f3(control_mean), f3(control_sd), f3(smd), f3(unmatched_control_mean), "")]
tb <- sub(" \\|  \\|$", " |", tb)
md <- c("**Table A6. Baseline balance, matched sample (descriptive, computed 5 November 2026 after the signed results)**", "",
 "| Variable | Exposed mean | Exposed SD | Matched control mean | Matched control SD | SMD | Unmatched control mean |",
 "|---|---|---|---|---|---|---|", tb, "",
 sprintf("*Note.* Matched exposed N = %s; matched controls N = %s (weights sum to %s); unmatched control-frame wallets N = %s (unweighted, for reference). Exposed wallets carry weight 1; each matched control carries its signed CEM stratum weight (prespec §1; `match_membership.csv`). Means and SDs are weighted (SD uses the weight-normalised variance, divisor Σw). Baseline window 9 Jun – 7 Aug 2022 (60 days; 8 Aug excluded). B is the baseline mean combined USDC + USDT holdings in USD. Baseline D and A are each wallet's 60-day mean of the capped daily D = (USDC − USDT)/B and A = (USDC + USDT)/B, with the signed caps (D [−3.093, 4.477], A [0, 5.317]). Wallet age is for wallets with at least one send; the never-sent row gives the share without a send. Five CEM matching traits: B, USDC share (three bins), transfer count, wallet age, ETH balance; D, A and never-sent are not matched on directly. SMD = (exposed mean − matched control mean) / √((SD² exposed + SD² control)/2). Descriptive; no test; selects nothing.",
   format(nE, big.mark = ","), format(nC, big.mark = ","), f3(sw), format(nU, big.mark = ",")))
writeLines(md, file.path(outdir, "tab_balance.md"))
cat(md, sep = "\n")
