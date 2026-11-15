# Decision rule (draft 1, "Decision Rule and Pre-Specification"): pure CI classifier.
# Cases are checked in this order:
#  1 CI entirely below -delta          -> meaningful negative response (supports H1)
#  2 CI contains -delta                -> inconclusive
#  3 CI entirely inside [-delta,delta] -> negligible response
#  4 CI entirely above +delta          -> meaningful positive response
#  5 anything else                     -> rules out meaningful negative, size of positive open
DECISION_LABELS <- c(
  "1 meaningful negative (supports H1)",
  "2 inconclusive",
  "3 negligible",
  "4 meaningful positive",
  "5 no meaningful negative; positive size open")

classify_ci <- function(lo, hi, delta) {
  stopifnot(is.numeric(lo), is.numeric(hi), length(lo) == 1, length(hi) == 1,
            is.finite(lo), is.finite(hi), lo <= hi, is.numeric(delta), delta > 0)
  case <- if (hi < -delta) 1L
          else if (lo <= -delta && -delta <= hi) 2L
          else if (lo >= -delta && hi <= delta) 3L
          else if (lo > delta) 4L
          else 5L
  list(case = case, label = DECISION_LABELS[case])
}

# ---- H1a classifier (prespec §3): its OWN four-way rule, not the H1 one ------------------------------
#  wholly inside [-da, da] -> "negligible"; wholly below -da -> "decline"; wholly above da -> "increase"; else "inconclusive"
classify_h1a <- function(lo, hi, delta_a = DELTA_A) {
  stopifnot(is.numeric(lo), is.numeric(hi), length(lo) == 1, length(hi) == 1,
            is.finite(lo), is.finite(hi), lo <= hi, is.numeric(delta_a), delta_a > 0)
  if (lo >= -delta_a && hi <= delta_a) "negligible"
  else if (hi < -delta_a) "decline"
  else if (lo > delta_a) "increase"
  else "inconclusive"
}

# ---- H1 support (prespec §2 "Uncapped veto" + "Inference") ---------------------------------------------
# Each interval is c(lo, hi). Returns status:
#  supported            capped D case 1 under BOTH clusterings and uncapped D not case 3/4/5 under EITHER
#  cap_sensitive        capped case 1 under both, but a veto fires (an uncapped interval lies wholly above -delta)
#  dependence_sensitive exactly one clustering has capped case 1 (wallet/stratum disagree on support)
#  not_supported        neither clustering has capped case 1
#  not_estimable        any needed interval missing / non-finite
h1_support <- function(cap_w, cap_s, unc_w, unc_s, delta = DELTA_D) {
  ok <- function(i) is.numeric(i) && length(i) == 2 && all(is.finite(i)) && i[1] <= i[2]
  if (!all(vapply(list(cap_w, cap_s, unc_w, unc_s), ok, TRUE)))
    return(list(status = "not_estimable", cases = rep(NA_integer_, 4), veto = NA))
  cs <- c(cap_w = classify_ci(cap_w[1], cap_w[2], delta)$case, cap_s = classify_ci(cap_s[1], cap_s[2], delta)$case,
          unc_w = classify_ci(unc_w[1], unc_w[2], delta)$case, unc_s = classify_ci(unc_s[1], unc_s[2], delta)$case)
  veto <- any(cs[c("unc_w", "unc_s")] %in% 3:5)
  n1 <- sum(cs[c("cap_w", "cap_s")] == 1L)
  status <- if (n1 == 2L) { if (veto) "cap_sensitive" else "supported" }
            else if (n1 == 1L) "dependence_sensitive"
            else "not_supported"
  list(status = status, cases = cs, veto = veto)
}

# ---- Clip incidence flag (prespec §2) ----------------------------------------------------------------
# tab: data.table(outcome, tail, arm ("exposed"/"control"), period ("baseline"/"post"), share).
# did = (exposed post - exposed baseline) - (control post - control baseline) per outcome x tail;
# cap_sensitive_clip if any |did| > CLIP_FLAG_PP (absolute value: prespec says the difference "exceeds 1 percentage
# point"; ruling 2026-10-16: conservative, either direction flags).
clip_flag <- function(tab, thresh = CLIP_FLAG_PP) {
  w <- dcast(as.data.table(tab), outcome + tail ~ arm + period, value.var = "share")
  w[, did := (exposed_post - exposed_baseline) - (control_post - control_baseline)]
  w[, exceeds := abs(did) > thresh]
  list(table = w[], cap_sensitive_clip = any(w$exceeds))
}

# ---- Reallocation wording (prespec §3), all four conditions -----------------------------------------
REALLOC_SENTENCE <- "consistent with relative reallocation toward USDT on the observed addresses"
reallocation_label <- function(h1_status, a_cap, a_unc, cap_sensitive_clip, delta_a = DELTA_A) {
  c1 <- identical(h1_status, "supported")                                  # both clusterings, veto included
  c2 <- classify_h1a(a_cap[1], a_cap[2], delta_a) == "negligible"          # capped A wholly inside band
  c3 <- !(a_unc[2] < -delta_a || a_unc[1] > delta_a)                       # uncapped A not wholly outside band
  c4 <- !isTRUE(cap_sensitive_clip) && !identical(h1_status, "cap_sensitive")
  list(allowed = c1 && c2 && c3 && c4, conditions = c(h1_supported = c1, a_cap_inside = c2,
                                                       a_unc_not_outside = c3, not_cap_sensitive = c4),
       label = if (c1 && c2 && c3 && c4) REALLOC_SENTENCE else NA_character_)
}
