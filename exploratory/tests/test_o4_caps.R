#!/usr/bin/env Rscript
# EXPLORATORY, POST-RESULTS, dated 2026-11-12; not confirmatory; does not reclassify H1.
# Synthetic-only tests of exploratory/o4_lib.R and the o4_caps.R outdir guard (never reads out/recovery). Usage, from anywhere:
#   R_LIBS_USER=<pinned-library> Rscript exploratory/tests/test_o4_caps.R
suppressPackageStartupMessages({ library(data.table); library(fixest) })
args <- commandArgs(FALSE); here <- dirname(sub("^--file=", "", grep("^--file=", args, value = TRUE)[1]))
root <- normalizePath(file.path(here, "..", ".."))
source(file.path(root, "R", "estimate.R")); source(file.path(root, "exploratory", "o4_lib.R"))   # prep_est, cap_values_all, clampv (read-only)
fixest::setFixest_nthreads(1L); data.table::setDTthreads(1L)
fails <- 0L
check <- function(name, ok, info = "") { if (isTRUE(ok)) cat("ok   ", name, "\n") else { fails <<- fails + 1L; cat("FAIL ", name, info, "\n") } }

# balanced synthetic panel: 120 wallets x 12 days (event day 6 dropped by prep_est), heavy-tailed legs, wallet-constant weights
set.seed(42)
n <- 120L; days <- 0:11; EV <- 6L
w0 <- data.table(uid = 1:n, treat = rep(c(TRUE, FALSE), each = n / 2), w = runif(n, 0.5, 3), stratum = rep(1:12, length.out = n), B = exp(rnorm(n)))
p <- w0[rep(seq_len(n), each = length(days))][, day_index := rep(days, n)]
p[, `:=`(usdc_B = rlnorm(.N, 0, 1.5) * (1 + 0.5 * (treat & day_index > EV)), usdt_B = rlnorm(.N, 0, 1.5) * (1 + 0.3 * (!treat & day_index > EV)))]
p[, `:=`(D = usdc_B - usdt_B, A = usdc_B + usdt_B)]
setattr(p, "baseline_days", 0:5)

# 1. o4_caps at the signed pair equals cap_values_all; other pairs nest
c1 <- o4_caps(p, EV, c(.01, .99)); cs <- cap_values_all(p, EV)
check("o4_caps(.01,.99) == cap_values_all (D, A, legs, n)", identical(c1$D, cs$D) && identical(c1$A, cs$A) && identical(c1$legs, cs$legs) && c1$n_wallet_days == cs$n_wallet_days)
c5 <- o4_caps(p, EV, c(.05, .95))
check("wider cap pair is nested inside the narrower one", c5$legs[1] >= c1$legs[1] && c5$legs[2] <= c1$legs[2] && c5$D[1] >= c1$D[1] && c5$D[2] <= c1$D[2])
check("cut-points come from baseline days only (0..5)", abs(c1$n_wallet_days - n * 6L) == 0L)

# 2. clamp / leg identity: cu - ct = prep_est(leg_pooled) D, cu + ct = A; beta(cu) - beta(ct) = beta(Dleg)
d0 <- merge(prep_est(p, EV, "none"), unique(p[, .(uid, B)]), by = "uid", sort = FALSE)
dc <- o4_capped(d0, c1)
dl <- prep_est(p, EV, "leg_pooled")
setkey(dl, uid, day_index); setkey(dc, uid, day_index)
check("Dleg/Aleg equal the signed prep_est(leg_pooled) D/A exactly", isTRUE(all.equal(dc$Dleg, dl$D, tolerance = 0)) && isTRUE(all.equal(dc$Aleg, dl$A, tolerance = 0)))
do <- prep_est(p, EV, "own"); setkey(do, uid, day_index)
check("Down/Aown equal the signed prep_est(own) D/A exactly", isTRUE(all.equal(dc$Down, do$D, tolerance = 0)) && isTRUE(all.equal(dc$Aown, do$A, tolerance = 0)))
check("clamped legs stay inside the pooled cut-points and clipping occurred", all(dc$cu >= c1$legs[1] & dc$cu <= c1$legs[2]) && any(dc$cu != dc$usdc_B))
fu <- o4_fit(dc, "cu"); ft <- o4_fit(dc, "ct"); fD <- o4_fit(dc, "Dleg"); fA <- o4_fit(dc, "Aleg")
check("beta(cu) - beta(ct) = beta(Dleg) (both clusterings)", all(abs(fu$beta - ft$beta - fD$beta) < 1e-9))
check("beta(cu) + beta(ct) = beta(Aleg) (both clusterings)", all(abs(fu$beta + ft$beta - fA$beta) < 1e-9))
ref <- estimate_did(p, EV, "D", "uid", "leg_pooled")
check("o4_fit uid beta/se reproduce estimate_did(leg_pooled)", abs(fD[cluster == "uid"]$beta - ref$beta) < 1e-9 && abs(fD[cluster == "uid"]$se - ref$se) < 1e-9)
check("o4_fit CI uses t with clusters - 1 df", abs(fD[cluster == "stratum"]$hi - fD[cluster == "stratum"]$beta - qt(.975, 11) * fD[cluster == "stratum"]$se) < 1e-12)

# 3. contribution decomposition sums to the uncapped beta on a balanced panel (also with the 2-day subset-free full fit)
ct <- o4_contrib(d0, "D", EV)
b0 <- o4_fit(d0, "D")[cluster == "uid"]$beta
check("sum of per-wallet contributions = uncapped beta (balanced toy panel)", abs(sum(ct$contribution) - b0) < 1e-9, sprintf("sum %.12g beta %.12g", sum(ct$contribution), b0))
check("contributions: treated positive weights normalise, controls enter with minus sign", abs(sum(ct[treat == TRUE]$w * ct[treat == TRUE]$delta) / ct[treat == TRUE, sum(w)] - sum(ct[treat == TRUE]$contribution)) < 1e-12)
k <- ct[order(-abs(contribution))][1:5]$uid
dk <- d0[!uid %in% k]; ck <- o4_contrib(dk, "D", EV)
check("leave-out: refit beta equals the sum of contributions recomputed on the remaining wallets", abs(sum(ck$contribution) - o4_fit(dk, "D")[cluster == "uid"]$beta) < 1e-9)
x2 <- o4_fit(d0, "D", c(5L, 7L)); check("two-day subset (event-study style, days 5 and 7 around event day 6) fits and is finite", all(is.finite(x2$beta)) && all(x2$n_obs == 2L * n))

# 4. in-time contrast helper
b <- setNames(seq_len(100) * 1.0, 1:100); check("o4_contrast: mean(next 35) - mean(previous 60), fake day dropped", abs(o4_contrast(b, 61L) - (mean(62:96) - mean(1:60))) < 1e-12)

# 5. out/ guard (function and the CLI, which refuses before reading any input)
check("guard: out/recovery refused", inherits(try(o4_out_guard("/r/out/recovery/x", "/r"), silent = TRUE), "try-error"))
check("guard: other out/ dir refused", inherits(try(o4_out_guard("/r/out/foo", "/r"), silent = TRUE), "try-error"))
check("guard: out/exploratory/<dir> allowed", isTRUE(o4_out_guard("/r/out/exploratory/o4-x", "/r")))
tmp <- tempfile("o4t"); dir.create(tmp)
run <- function(...) { o <- suppressWarnings(system2("Rscript", c(shQuote(file.path(root, "exploratory", "o4_caps.R")), ...), stdout = TRUE, stderr = TRUE)); list(out = paste(o, collapse = "\n"), status = attr(o, "status") %||% 0L) }
`%||%` <- function(a, b) if (is.null(a)) b else a
r <- run("--repo-root", shQuote(root), "--outdir", shQuote(file.path(root, "out", "o4-guard-test")))
check("CLI: --outdir under out/ (not exploratory) refused, nothing created", r$status != 0L && grepl("refusing to write under out/", r$out) && !dir.exists(file.path(root, "out", "o4-guard-test")))
r <- run("--repo-root", shQuote(root), "--outdir", shQuote(file.path(root, "out", "exploratory", "..", "recovery", "x")))
check("CLI: ../ traversal into out/recovery refused", r$status != 0L && grepl("refusing to write under out/", r$out))
r <- run("--repo-root", shQuote(root)); check("CLI: --outdir is required", r$status != 0L && grepl("--outdir is required", r$out))
cat(if (fails == 0L) "ALL PASSED\n" else sprintf("%d FAILED\n", fails)); quit(status = fails > 0L)
