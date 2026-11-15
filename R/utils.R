# Shared constants and guards for the R estimation lane (S9).
suppressPackageStartupMessages({
  library(data.table)
  library(fixest)
})

UNITS_PER_DOLLAR <- 1e6                   # USDC/USDT: 6 decimals; convert ONLY inside R
PRE_PERIOD_START <- as.Date("2022-06-09") # day_index 0
LAST_ALLOWED_DATE <- as.Date("2022-08-07")# HARD RULE: never read data after this date
DELTA_D <- 0.10                           # decision-rule bound for D
DELTA_A <- 0.05                           # decision-rule bound for A

# Pre-specification constants (prespec_draft.md §1, §3, §5; SPEC S10c).
PRE_PERIOD_END_BLOCK <- 15298282L         # last block any input may reach (7 Aug 2022, 23:59:57 UTC)
SEED_PRIMARY_CEM <- 20261015L             # primary CEM draw (also the R placebo CEM draw)
SEED_JUNE_FRAME  <- 20251015L             # June placebo frame windows (drawn by gates/g4_june.py; recorded here)
SEED_ENLARGEMENT <- 20251016L             # one-time frame enlargement (not implemented in R; recorded)
POWER_TARGET <- 0.80
CLIP_FLAG_PP <- 0.01                      # clip-incidence flag threshold (1 percentage point)
RNG_KIND <- c("Mersenne-Twister", "Inversion", "Rejection")
set_rng_kind <- function() suppressWarnings(RNGkind(RNG_KIND[1], RNG_KIND[2], RNG_KIND[3]))

to_dollars <- function(units) as.numeric(units) / UNITS_PER_DOLLAR

# Fail loudly if any date/day_index in a loaded table is past the hard cutoff.
assert_no_post_data <- function(dt, what = "table") {
  if ("date" %in% names(dt)) {
    mx <- max(as.Date(dt$date))
    if (mx > LAST_ALLOWED_DATE)
      stop(sprintf("HARD RULE violated: %s contains date %s > %s", what, mx, LAST_ALLOWED_DATE))
  }
  if ("day_index" %in% names(dt) && !"date" %in% names(dt)) {
    lim <- as.integer(LAST_ALLOWED_DATE - PRE_PERIOD_START)
    if (max(dt$day_index) > lim && !isTRUE(getOption("tusdc.synthetic")))
      stop(sprintf("HARD RULE violated: %s has day_index %d > %d", what, max(dt$day_index), lim))
  }
  invisible(TRUE)
}

repo_root <- function() {
  a <- grep("^--file=", commandArgs(FALSE), value = TRUE)
  if (length(a)) normalizePath(dirname(sub("^--file=", "", a[1]))) else getwd()
}
