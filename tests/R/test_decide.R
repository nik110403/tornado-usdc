cc <- function(lo, hi, d = 0.10) classify_ci(lo, hi, d)$case
test("case 1: entirely below -delta", stopifnot(cc(-0.30, -0.11) == 1L))
test("case 2: contains -delta (incl. endpoints)", stopifnot(cc(-0.20, -0.05) == 2L, cc(-0.10, -0.05) == 2L,
     cc(-0.30, -0.10) == 2L, cc(-0.30, 0.30) == 2L))
test("case 3: inside [-delta, delta]", stopifnot(cc(-0.05, 0.05) == 3L, cc(-0.09, 0.10) == 3L, cc(0.0, 0.0) == 3L))
test("case 4: entirely above +delta", stopifnot(cc(0.11, 0.40) == 4L))
test("case 5: other (rules out negative, positive open)", stopifnot(cc(-0.05, 0.30) == 5L, cc(0.05, 0.30) == 5L,
     cc(0.10, 0.30) == 5L))
test("order: case 2 wins over 3 at the -delta boundary", stopifnot(cc(-0.10, 0.0) == 2L))
test("delta_A = 0.05 applied", stopifnot(cc(-0.04, 0.04, DELTA_A) == 3L, cc(-0.04, 0.06, DELTA_A) == 5L,
     cc(-0.2, -0.06, DELTA_A) == 1L))
test("symmetry: every interval maps to exactly one case 1..5", {
  g <- seq(-0.4, 0.4, by = 0.01)
  for (lo in g) for (hi in g[g >= lo]) stopifnot(cc(lo, hi) %in% 1:5)
})
test("input validation", {
  stopifnot(inherits(try(classify_ci(0.2, 0.1, 0.1), silent = TRUE), "try-error"),
            inherits(try(classify_ci(0, 1, -1), silent = TRUE), "try-error"))
})
test("H1a classifier: four-way rule (own rule, not the H1 one)", {
  h <- function(lo, hi) classify_h1a(lo, hi, 0.05)
  stopifnot(h(-0.05, 0.05) == "negligible", h(-0.02, 0.03) == "negligible",
            h(-0.20, -0.06) == "decline", h(0.06, 0.20) == "increase",
            h(-0.06, 0.02) == "inconclusive", h(-0.2, 0.2) == "inconclusive", h(0.04, 0.06) == "inconclusive",
            h(-0.05, -0.05) == "negligible")
  # differs from the H1 classifier: [-0.04, 0.06] is H1 case 5 but H1a inconclusive; [-0.2,-0.06] H1 case 1 (decline for H1a too)
  stopifnot(classify_ci(-0.04, 0.06, 0.05)$case == 5L, h(-0.04, 0.06) == "inconclusive",
            inherits(try(classify_h1a(0.1, 0.0), silent = TRUE), "try-error"))
})
test("H1 support: both clusterings case 1 and no veto -> supported", {
  s <- h1_support(c(-0.30, -0.15), c(-0.28, -0.12), c(-0.40, -0.20), c(-0.35, -0.11))
  stopifnot(s$status == "supported", !s$veto)
})
test("H1 veto: uncapped interval wholly above -delta (case 3/4/5) under EITHER clustering -> cap_sensitive", {
  cw <- c(-0.30, -0.15); cs <- c(-0.28, -0.12)
  stopifnot(h1_support(cw, cs, c(-0.05, 0.05), c(-0.4, -0.2))$status == "cap_sensitive",   # case 3, wallet
            h1_support(cw, cs, c(-0.4, -0.2), c(0.11, 0.3))$status == "cap_sensitive",     # case 4, stratum
            h1_support(cw, cs, c(-0.4, -0.2), c(-0.05, 0.3))$status == "cap_sensitive",    # case 5
            h1_support(cw, cs, c(-0.4, 0.0), c(-0.4, -0.2))$status == "supported")         # case 2 does not veto
})
test("H1 support: wallet/stratum disagreement -> dependence_sensitive; neither -> not_supported; bad CI -> not_estimable", {
  u <- c(-0.4, -0.2)
  stopifnot(h1_support(c(-0.3, -0.15), c(-0.3, 0.0), u, u)$status == "dependence_sensitive",
            h1_support(c(-0.3, 0.0), c(-0.3, -0.15), u, u)$status == "dependence_sensitive",
            h1_support(c(-0.3, 0.0), c(-0.3, 0.0), u, u)$status == "not_supported",
            h1_support(c(-0.3, -0.15), c(NA, NA), u, u)$status == "not_estimable")
})
test("clip flag: exposed-minus-control change in tail share > 1 pp at any tail of either cap", {
  mk <- function(ep) data.table(
    outcome = rep(c("D", "A"), each = 8), tail = rep(rep(c("lower", "upper"), each = 4), 2),
    arm = rep(c("exposed", "exposed", "control", "control"), 4), period = rep(c("baseline", "post"), 8),
    share = c(.01, .01, .01, .01,  .01, .01 + ep, .01, .01,   .01, .01, .01, .01,  .01, .01, .01, .01))
  stopifnot(clip_flag(mk(0.02))$cap_sensitive_clip, !clip_flag(mk(0.01))$cap_sensitive_clip,   # exactly 1 pp does not exceed
            !clip_flag(mk(0))$cap_sensitive_clip,
            clip_flag(mk(-0.02))$cap_sensitive_clip, !clip_flag(mk(-0.01))$cap_sensitive_clip)   # absolute value: a fall also flags
  f <- clip_flag(mk(0.02))$table; stopifnot(f[outcome == "D" & tail == "upper", exceeds], sum(f$exceeds) == 1)
})
test("reallocation wording needs all four conditions", {
  ok <- reallocation_label("supported", c(-0.03, 0.03), c(-0.2, 0.04), FALSE)
  stopifnot(ok$allowed, ok$label == REALLOC_SENTENCE)
  stopifnot(!reallocation_label("cap_sensitive", c(-0.03, 0.03), c(-0.2, 0.04), FALSE)$allowed,
            !reallocation_label("dependence_sensitive", c(-0.03, 0.03), c(-0.2, 0.04), FALSE)$allowed,
            !reallocation_label("supported", c(-0.08, 0.03), c(-0.2, 0.04), FALSE)$allowed,       # capped A not inside band
            !reallocation_label("supported", c(-0.03, 0.03), c(-0.5, -0.06), FALSE)$allowed,      # uncapped A wholly outside
            !reallocation_label("supported", c(-0.03, 0.03), c(0.06, 0.5), FALSE)$allowed,
            !reallocation_label("supported", c(-0.03, 0.03), c(-0.2, 0.04), TRUE)$allowed)        # clip flag
})
