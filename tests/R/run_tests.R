# Base-R test runner (testthat is not installed). Sourced from run_all.R (--stage test) or run directly:
#   Rscript tests/R/run_tests.R      (UPDATE_GOLDEN=1 rewrites the golden table)
if (!exists("classify_ci")) {
  a <- grep("^--file=", commandArgs(FALSE), value = TRUE)
  setwd(normalizePath(file.path(dirname(sub("^--file=", "", a[1])), "..", "..")))
  for (f in c("utils", "decide", "cem", "estimate", "validate", "synth", "pre", "blacklist", "presign", "post_gate", "later_freeze")) source(file.path("R", paste0(f, ".R")))
}
.t_total <- 0L
test <- function(name, expr) {
  r <- try(force(expr), silent = TRUE)
  if (inherits(r, "try-error")) stop(sprintf("TEST FAILED: %s\n%s", name, conditionMessage(attr(r, "condition"))), call. = FALSE)
  .t_total <<- .t_total + 1L
  cat(sprintf("  ok  %s\n", name))
}
cat("== R unit tests ==\n")
for (f in sort(list.files(file.path("tests", "R"), "^test_.*\\.R$", full.names = TRUE))) {
  cat(basename(f), "\n"); source(f, local = TRUE)
}
cat(sprintf("ALL %d R TESTS PASSED\n", .t_total))
