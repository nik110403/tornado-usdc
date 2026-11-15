# Results exhibits (Figures 1-2, Tables 1-4, captions, manifest). Base R only.
# Usage: Rscript paper/exhibits.R --out <dir> [--in <dir>]
# --in defaults to results/ at the repo root (the shipped aggregates; layout results/{post,o3,placebo}/).
args <- commandArgs(TRUE)
out <- if (length(i <- which(args == "--out"))) args[i + 1] else stop("--out required")
out <- path.expand(out); dir.create(out, recursive = TRUE, showWarnings = FALSE)
self <- sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE)[1])
repo <- normalizePath(file.path(dirname(self), ".."), mustWork = FALSE)
root <- path.expand(if (length(i <- which(args == "--in"))) args[i + 1] else file.path(repo, "results"))
P <- file.path(root, "post")
O3 <- file.path(root, "o3", "o3_numbers.csv")
PLC <- file.path(root, "placebo", "placebo_estimates_june_pre.csv")
rd <- function(f) read.csv(file.path(P, paste0(f, ".csv")), stringsAsFactors = FALSE)
inputs <- c(file.path(P, paste0(c("post_estimates","post_decisions","post_rematches","post_cap_robustness",
  "post_short_window","post_drdid","post_pretrend","post_time_path","post_cluster_sizes",
  "post_weight_concentration","post_h2"), ".csv")), O3, PLC)

MINUS <- "−"
f3 <- function(x) sub("-", MINUS, sprintf("%.3f", x), fixed = TRUE)
ci <- function(lo, hi) sprintf("[%s, %s]", f3(lo), f3(hi))
fi <- function(n) format(n, big.mark = ",", scientific = FALSE, trim = TRUE)
write_tab <- function(df, name, header = NULL, footer = NULL, readme = NULL) {
  if (length(readme)) writeLines(readme, file.path(out, paste0(name, "_README.txt")))
  csv <- df
  if (length(footer)) {
    fr <- as.data.frame(matrix("", length(footer), ncol(df)), stringsAsFactors = FALSE); names(fr) <- names(df)
    fr[[1]] <- footer; csv <- rbind(df, fr)
  }
  write.csv(csv, file.path(out, paste0(name, ".csv")), row.names = FALSE, fileEncoding = "UTF-8")
  md <- c(if (length(header)) c(header, ""),
          paste0("| ", paste(names(df), collapse = " | "), " |"),
          paste0("|", paste(rep("---", ncol(df)), collapse = "|"), "|"),
          apply(df, 1, function(r) paste0("| ", paste(r, collapse = " | "), " |")),
          if (length(footer)) c("", paste0("- ", footer)))
  writeLines(md, file.path(out, paste0(name, ".md")), useBytes = TRUE)
}

## ---------- Table 1 ----------
est <- rd("post_estimates"); dec <- rd("post_decisions")
g <- function(o, cap, cl) est[est$outcome == o & est$cap == cap & est$cluster == cl, ]
dv <- function(k) dec$value[dec$item == k]
row1 <- function(lab, o, cap, label) {
  w <- g(o, cap, "uid"); s <- g(o, cap, "stratum")
  c(lab, f3(w$beta), ci(w$lo, w$hi), ci(s$lo, s$hi), label)
}
t1 <- as.data.frame(rbind(
  row1("H1 capped D", "D", "own", sprintf("%s (case %s wallet, %s stratum)", dv("h1_status"), dv("capped_case_uid"), dv("capped_case_stratum"))),
  row1("H1a capped A", "A", "own", sprintf("%s (wallet and stratum)", dv("h1a_combined"))),
  row1("Uncapped D (veto)", "D", "none", sprintf("case %s wallet, %s stratum; veto %s", dv("uncapped_case_uid"), dv("uncapped_case_stratum"), ifelse(dv("h1_veto") == "FALSE", "not fired", "fired"))),
  row1("Uncapped A", "A", "none", sprintf("%s (wallet and stratum)", dv("h1a_uncapped_uid")))), stringsAsFactors = FALSE)
names(t1) <- c("Row", "β", "95% CI, wallet-clustered", "95% CI, stratum-clustered", "Decision")
cs <- rd("post_cluster_sizes"); cu <- cs[cs$cluster == "uid", ]; cst <- cs[cs$cluster == "stratum", ]
wc <- rd("post_weight_concentration"); ceff <- wc$effective_n[wc$arm == "control" & wc$scope == "arm"]
rr <- read.csv(file.path(P, "post_reporting_rows.csv"), stringsAsFactors = FALSE)
clip <- sub(".*clip_flag=([A-Za-z]+).*", "\\1", rr$reason[rr$row_id == "h1_pooled_35d"])
stopifnot(length(clip) == 1, clip %in% c("TRUE", "FALSE"))
foot1 <- c(sprintf("Veto %s; clip flag %s (post_reporting_rows.csv, row h1_pooled_35d).", ifelse(dv("h1_veto") == "FALSE", "not fired", "fired"), clip),
sprintf("N exposed %s; controls %s; strata %s; wallet-days %s.", fi(cu$n_exposed_wallets), fi(cu$n_control_wallets), fi(cst$n_clusters), fi(cu$n_obs)),
           sprintf("Control effective n after weighting: %s.", fi(round(ceff))))
write_tab(t1, "tab1_headline", footer = foot1)

## ---------- Table 2 ----------
rm_ <- rd("post_rematches"); cr <- rd("post_cap_robustness"); sw <- rd("post_short_window"); dr <- rd("post_drdid")
rem <- function(lab, key) { r <- rm_[rm_$row == key, ]
  c(lab, f3(r$capped_beta_uid), ci(r$capped_lo_uid, r$capped_hi_uid), ci(r$capped_lo_stratum, r$capped_hi_stratum),
    sprintf("%s / %s", r$capped_case_uid, r$h1_status), "post_rematches.csv") }
crr <- function(lab, cap) { w <- cr[cr$outcome == "D" & cr$cap == cap & cr$cluster == "uid", ]; s <- cr[cr$outcome == "D" & cr$cap == cap & cr$cluster == "stratum", ]
  c(lab, f3(w$beta), ci(w$lo, w$hi), ci(s$lo, s$hi), sprintf("%s%s", w$decision, ifelse(w$differs_from_primary_case, " (differs from headline)", "")), "post_cap_robustness.csv") }
sww <- sw[sw$outcome == "D" & sw$cap == "own" & sw$cluster == "uid", ]; sws <- sw[sw$outcome == "D" & sw$cap == "own" & sw$cluster == "stratum", ]
d1 <- dr[dr$row == "capped_D", ]
# June placebo of record (fake event 9 Jul): capped D = usdc_mode three_bins, cap own
pl0 <- read.csv(PLC, stringsAsFactors = FALSE); pl0 <- pl0[pl0$usdc_mode == "three_bins" & pl0$cap == "own", ]
stopifnot(nrow(pl0) == 1, pl0$case_D_wallet == pl0$case_D_stratum)
t2 <- as.data.frame(rbind(
  rem("No USDC-share matching (5.6)", "no_usdc_share"),
  rem("USDC-share matching, exposed quartiles (5.6)", "quartile_share"),
  rem("Drop wallets frozen 8 Aug-12 Sep (5.9) †", "drop_later_freeze"),
  crr("One cap for both legs (5.8)", "leg_pooled"),
  crr("Uncapped (5.8)", "none"),
  c("Shorter window, through day 87 (5.4)", f3(sww$beta), ci(sww$lo, sww$hi), ci(sws$lo, sws$hi), "1", "post_short_window.csv"),
  c("Doubly robust DiD, unmatched (5.5)", f3(d1$att), ci(d1$lo, d1$hi), "n/a", as.character(d1$decision_case), "post_drdid.csv"),
  c("June placebo, fake event 9 Jul (5.2, signed record; placebo of record, case 3)", f3(pl0$beta_D), ci(pl0$lo_D_wallet, pl0$hi_D_wallet), ci(pl0$lo_D_stratum, pl0$hi_D_stratum), sprintf("%s both (pass)", pl0$case_D_wallet), "placebo_estimates_june_pre.csv (prespec section 5 item 2)")),
  stringsAsFactors = FALSE)
names(t2) <- c("Check (prespec ref)", "β capped D", "95% CI, wallet", "95% CI, stratum", "Case / H1", "Source")
write_tab(t2, "tab2_robustness", footer = "† Veto directory self-authenticating; independent flipped-endpoint re-run not executed (S13).")

## ---------- Table 3 ----------
o3 <- read.csv(O3, stringsAsFactors = FALSE)
oo <- function(id, o, cl) o3[o3$id == sprintf("postmean_minus_base_%s_%s", id, o) & o3$cluster == cl, ]
lab3 <- c(last10 = "29 Jul - 7 Aug (last 10 days)", `7aug_only` = "7 Aug only", last5 = "Last 5 days", last20 = "Last 20 days",
          last40 = "Last 40 days", from_1jul = "From 1 Jul", from_8jul = "From 8 Jul", all60 = "All 60 days (= headline)")
t3 <- t(sapply(names(lab3), function(k) { w <- oo(k, "D", "uid"); s <- oo(k, "D", "stratum"); a <- oo(k, "A", "uid")
  c(lab3[[k]], f3(w$estimate), ci(w$lo, w$hi), ci(s$lo, s$hi), f3(a$estimate)) }))
t3 <- as.data.frame(t3, stringsAsFactors = FALSE, row.names = NULL)
names(t3) <- c("Baseline", "D", "95% CI, wallet", "95% CI, stratum", "A")
write_tab(t3, "tab3_late_baseline", header = "**Exploratory, post-results, dated 28 Oct 2026** (source: o3_numbers.csv, rows postmean_minus_base_*; not confirmatory; does not reclassify H1)",
  readme = "Exploratory, post-results (matches the header of tab3_late_baseline.md; the CSV carries data rows only).")

## ---------- Table 4 ----------
h2 <- rd("post_h2")
h2r <- function(g_) { w <- h2[h2$group == g_ & h2$cluster == "uid", ]; s <- h2[h2$group == g_ & h2$cluster == "stratum", ]
  c(paste("H2:", g_, "prior Tornado use"), fi(w$n_exposed_wallets), f3(w$beta), ci(w$lo, w$hi), ci(s$lo, s$hi)) }
pl <- function(lab, key) { r <- rm_[rm_$row == key, ]
  c(lab, fi(r$n_exposed_retained), f3(r$capped_beta_uid), ci(r$capped_lo_uid, r$capped_hi_uid), ci(r$capped_lo_stratum, r$capped_hi_stratum)) }
t4 <- as.data.frame(rbind(h2r("low"), h2r("middle"), h2r("high"),
  pl("USDC-pool depositors (5.10)", "usdc_pool_flagged"), pl("Other exposed wallets (5.10)", "usdc_pool_unflagged")), stringsAsFactors = FALSE)
names(t4) <- c("Group", "N exposed wallets", "β capped D", "95% CI, wallet", "95% CI, stratum")
write_tab(t4, "tab4_h2_pool", header = "**Exploratory, pattern only; never determines H1** (sources: post_h2.csv, post_rematches.csv)",
  readme = "Exploratory, pattern only (matches the header of tab4_h2_pool.md; the CSV carries data rows only).")

## ---------- Figures ----------
pre <- rd("post_pretrend"); tp <- rd("post_time_path")
fam <- if (any(grepl("^Aptos:|/Aptos-Regular|Aptos\\.ttf", system("fc-list", intern = TRUE)))) "Aptos" else "Helvetica"
ev <- as.Date("2022-08-08"); frz <- as.Date("2022-06-30")
fig <- function(o, delta, lab, name, dlab) {
  a <- pre[pre$outcome == o, ]; b <- tp[tp$outcome == o, ]
  seg1 <- rbind(a[, c("date","coef","lo","hi")], b[b$status == "reference", c("date","coef","lo","hi")])
  seg2 <- b[b$status == "estimated", c("date","coef","lo","hi")]
  seg1$date <- as.Date(seg1$date); seg2$date <- as.Date(seg2$date)
  draw <- function() {
    par(family = fam, mar = c(3.6, 4.4, 0.8, 0.8), mgp = c(2.6, 0.7, 0), cex.axis = 0.8, cex.lab = 0.9, las = 1)
    yl <- range(c(seg1$lo, seg1$hi, seg2$lo, seg2$hi, -delta)) + c(-0.02, 0.02)
    plot(NA, xlim = range(c(seg1$date, seg2$date)), ylim = yl, xaxt = "n", yaxt = "n", xlab = "Date (2022)", ylab = lab, bty = "l")
    for (s in list(seg1, seg2)) polygon(c(s$date, rev(s$date)), c(s$lo, rev(s$hi)), col = "grey85", border = NA)
    abline(h = 0, col = "grey50", lwd = 1); abline(h = -delta, lty = 3, col = "black")
    abline(v = ev, lty = 2, col = "black")
    for (s in list(seg1, seg2)) lines(s$date, s$coef, col = "black", lwd = 1.3)
    ax <- seq(as.Date("2022-06-09"), as.Date("2022-09-12"), by = "14 days")
    axis(1, at = ax, labels = format(ax, "%d %b"))
    yt <- axTicks(2); axis(2, at = yt, labels = sub("-", MINUS, format(yt, trim = TRUE), fixed = TRUE))
    usr <- par("usr")
    text(usr[1] + 0.01 * diff(usr[1:2]), -delta, dlab, adj = c(0, -0.5), cex = 0.75)
    text(ev, usr[4], "8 Aug (event)", adj = c(-0.05, 1.2), cex = 0.75)
    segments(frz, usr[3], frz, usr[3] + 0.03 * diff(usr[3:4]))
    text(frz, usr[3] + 0.035 * diff(usr[3:4]), "30 Jun (Tether freeze)", adj = c(0.5, 0), cex = 0.7)
  }
  if (fam == "Aptos") cairo_pdf(file.path(out, paste0(name, ".pdf")), width = 6.5, height = 4) else pdf(file.path(out, paste0(name, ".pdf")), width = 6.5, height = 4)
  draw(); dev.off()
  png(file.path(out, paste0(name, ".png")), width = 6.5, height = 4, units = "in", res = 300, type = "cairo"); draw(); dev.off()
}
fig("D", 0.10, "Capped D, relative to 7 Aug 2022", "fig1_event_study_D", paste0(MINUS, "δ = ", MINUS, "0.10"))
fig("A", 0.05, "Capped A, relative to 7 Aug 2022", "fig2_event_study_A", expression(paste("\u2212", delta[A], " = \u22120.05")))

## ---------- Captions ----------
cap <- c("# Caption stubs (data description only)", "",
 "**Figure 1.** Daily event-study coefficients for capped D = (USDC - USDT) / B, exposed vs matched control wallets, 9 Jun - 12 Sep 2022, relative to 7 Aug 2022 (reference day = 0). Sample: 4,487 exposed and 11,495 control wallets in 692 strata, 1,518,290 wallet-days. Shaded band: 95% wallet-clustered interval (stratum-clustered intervals are not in the daily files). Dashed vertical line: 8 Aug 2022 (event; not in the panel). Solid grey line: 0. Dotted line: delta = -0.10. Tick: 30 Jun 2022 (Tether freeze). Sources: post_pretrend.csv (9 Jun - 6 Aug, pre-event days), post_time_path.csv (7 Aug reference and 9 Aug - 12 Sep).",
 "",
 "**Figure 2.** As Figure 1 for capped A = (USDC + USDT) / B; dotted line: delta_a = -0.05. Sources: post_pretrend.csv, post_time_path.csv (outcome A rows). 95% intervals, wallet-clustered.",
 "",
 "**Table 1.** Difference-in-differences estimates beta for capped D (H1), capped A (H1a), uncapped D (veto check) and uncapped A. 95% intervals, wallet-clustered and stratum-clustered. Decision labels as recorded in post_decisions.csv. Footer: sample counts from post_cluster_sizes.csv; control effective n from post_weight_concentration.csv. Sources: post_estimates.csv, post_decisions.csv.",
 "",
 "**Table 2.** Robustness estimates of beta for capped D under alternative matching, capping, window and estimator choices; 95% intervals, wallet-clustered (stratum-clustered alongside). Sources: post_rematches.csv, post_cap_robustness.csv, post_short_window.csv, post_drdid.csv; the June placebo row is taken from the signed prespec record (section 5 item 2), not from any output CSV.",
 "",
 "**Table 3.** Exploratory, post-results, dated 28 Oct 2026. Mean of the 35 post-event daily coefficients minus the mean of the pre coefficients over the stated baseline window, for D (95% wallet- and stratum-clustered intervals) and A (point estimate). Source: o3_numbers.csv (rows postmean_minus_base_*), script exploratory/o3_pretrend.R.",
 "",
 "**Table 4.** Exploratory. Capped D beta by prior Tornado use (low/middle/high; post_h2.csv) and for USDC-pool depositors versus other exposed wallets (post_rematches.csv, rows usdc_pool_flagged / usdc_pool_unflagged). 95% intervals, wallet-clustered and stratum-clustered.")
writeLines(cap, file.path(out, "captions.md"))

## ---------- Manifest ----------
outs <- setdiff(list.files(out, full.names = TRUE), file.path(out, "MANIFEST.sha256"))
writeLines(system2("sha256sum", c(outs, inputs), stdout = TRUE), file.path(out, "MANIFEST.sha256"))
cat("done:", out, "font:", fam, "\n")
