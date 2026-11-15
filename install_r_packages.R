#!/usr/bin/env Rscript
# Installs the pinned R packages for this repository from CRAN archive sources. Nothing is guessed: every
# version below is taken from a record in the repository or the private run that produced it (column `evidence`).
#
#   Rscript install_r_packages.R --dry-run                       # list what would be installed
#   Rscript install_r_packages.R --lib <dir>                     # signed pipeline library (R 4.3.3 expected)
#   Rscript install_r_packages.R --exploratory --lib <otherdir>  # exploratory packages, SEPARATE library
#
# Matrix, nlme and lattice are R "recommended" packages: they ship with R 4.3.3 (Ubuntu: r-recommended) and are
# only checked, never installed. The signed list is verified against prespec/manifest.json by the pipeline itself
# (R/post_gate.R verify_post_package_versions); this script is a convenience, not an authority.
args <- commandArgs(TRUE)
dry <- "--dry-run" %in% args; expl <- "--exploratory" %in% args
lib <- if (length(i <- which(args == "--lib"))) args[i + 1] else NA_character_
if (!dry && is.na(lib)) stop("--lib <dir> required (or --dry-run)")

S <- "signed: prespec/manifest.json r_package_versions"
O <- "observed in the library of the signed run (closure of the 16 signed packages; not itself signed)"
signed <- data.frame(stringsAsFactors = FALSE, check.names = FALSE,
  package = c("fixest", "data.table", "DRDID", "trust", "BMisc", "fastglm", "Rcpp", "jsonlite", "RcppEigen", "sandwich", "dreamerr", "numDeriv", "stringmagic", "bigmemory",
              "BH", "bigmemory.sri", "Formula", "RcppArmadillo", "uuid", "zoo"),
  version = c("0.14.2", "1.18.4", "1.4.0", "0.1-9", "1.4.10", "0.1.2", "1.1.2", "2.0.0", "0.3.4.0.2", "3.1-2", "1.5.0", "2016.8-1.1", "1.2.0", "4.6.6",
              "1.90.0-1", "0.1.8", "1.2-5", "15.4.2-1", "1.2-2", "1.8-15"),
  evidence = c(rep(S, 14), rep(O, 6)))
recommended <- data.frame(package = c("Matrix", "nlme", "lattice"), version = c("1.6-5", "3.1-164", "0.22-5"),
  evidence = c(S, S, O))   # ship with R 4.3.3; checked only

# Exploratory-only (O2/O4). Versions: DESCRIPTION files of the private exploratory/rlib used for the dated runs
# (HonestDiD 0.2.7 and CVXR 1.0-15 are pinned nowhere else). None of the signed packages is in this list, by design.
E <- "DESCRIPTION of exploratory/rlib in the private working copy"
expl_pk <- data.frame(stringsAsFactors = FALSE, check.names = FALSE,
  package = c("HonestDiD", "CVXR", "ECOSolveR", "Rglpk", "Rmpfr", "TruncatedNormal", "alabama", "gmp", "highs", "latex2exp", "lpSolveAPI", "matrixStats", "mvtnorm", "nleqslv",
              "osqp", "pracma", "qrng", "scs", "slam", "spacefillr"),
  version = c("0.2.7", "1.0-15", "0.6.2", "0.6-5.1", "1.1-3", "2.3", "2025.1.0", "0.7-5.1", "1.15.1-1", "0.9.8", "5.5.2.0-17.15", "1.5.0", "1.4-2", "3.3.7",
              "1.0.0", "2.4.6", "0.0-11", "3.2.7", "0.1-56", "0.4.0"),
  evidence = E)

pk <- if (expl) expl_pk else signed
cat(sprintf("%s library: %d packages to install, R %s\n", if (expl) "exploratory" else "signed pipeline", nrow(pk), getRversion()))
print(pk, row.names = FALSE)
if (!expl) { cat("\nshipped with R (checked only):\n"); print(recommended, row.names = FALSE) }
if (dry) quit(status = 0)

if (!requireNamespace("remotes", quietly = TRUE)) stop("install 'remotes' first: install.packages('remotes')")
if (!expl && getRversion() != "4.3.3") warning("the signed run used R 4.3.3; this is ", as.character(getRversion()))
dir.create(lib, recursive = TRUE, showWarnings = FALSE)
.libPaths(c(lib, .libPaths()))
for (k in seq_len(nrow(pk)))
  remotes::install_version(pk$package[k], version = pk$version[k], lib = lib, repos = "https://cloud.r-project.org",
                           dependencies = FALSE, upgrade = "never", quiet = TRUE)
bad <- character()
for (k in seq_len(nrow(pk))) {
  got <- tryCatch(as.character(packageVersion(pk$package[k], lib.loc = lib)), error = function(e) NA_character_)
  if (!identical(got, gsub("-", ".", pk$version[k], fixed = TRUE)) && !identical(got, pk$version[k])) bad <- c(bad, sprintf("%s: want %s got %s", pk$package[k], pk$version[k], got))
}
if (!expl) for (k in seq_len(nrow(recommended))) {
  got <- as.character(packageVersion(recommended$package[k]))
  if (!identical(got, gsub("-", ".", recommended$version[k], fixed = TRUE))) bad <- c(bad, sprintf("%s (ships with R): want %s got %s", recommended$package[k], recommended$version[k], got))
}
if (length(bad)) { cat("VERSION MISMATCH:\n", paste(" ", bad, collapse = "\n"), "\n"); quit(status = 1) }
cat("all pinned versions present in", lib, "\n")
