# EXPLORATORY, POST-RESULTS, dated 2026-10-28; not confirmatory; does not reclassify H1.
# Pure helpers of o2_honestdid.R (no HonestDiD, no I/O) so the breakdown rule, the runtime valve, the cache key and the joint rule can be tested with mocks.
`%||%` <- function(a, b) if (is.null(a)) b else a

# clearance (plan 1.4 + addendum A1): nominal = robust interval's upper bound strictly below tau; conservative = upper bound + theta step strictly below tau.
# A failed evaluation is NA (never clear, never lost); a hit at EITHER edge of the RM theta grid, or HonestDiD's "open at one of the endpoints" warning, voids the grid (NA).
clears_hi <- function(status, hi, tau, dtheta = 0) {
  if (!startsWith(status, "ok") || grepl("grid_edge|open_endpoint", status)) return(NA)
  hi + (if (is.na(dtheta)) 0 else dtheta) < tau
}

# cache key: everything that changes the HonestDiD result (restriction, M, method, theta grid, alpha, bound, seed)
eval_key <- function(restr, M, method, gp, glb, gub, alpha, bound, seed) {
  sprintf("%s|%.15g|%s|%s|%.15g|%.15g|%.15g|%s|%s", restr, M, method, if (is.null(gp) || is.na(gp)) "NA" else format(gp), glb %||% NA_real_, gub %||% NA_real_, alpha, bound %||% "NA", seed)
}

# Plan 1.5 steps 2-4, 6 + addendum A2 item 3. g: data.frame/data.table with columns M (ascending), hi, status, optional dtheta (the evaluated grid). eval_fun(M) -> list(hi=, status=, dtheta=) is called only for the
# bisection points. conservative = TRUE tests hi + dtheta < tau. A failed / timed-out / edge-voided point is NEVER a clearance: a failure before the first loss makes the scan "undetermined beyond M = m"
# (m = the last point of the contiguous clearing prefix, in last_clearing_M); a failure inside a bisection bracket stops that bisection and flags the bracket "failure in bracket".
# Returns list(status, fields..., bisection = list of (M, hi, status, clears)).
scan_breakdown <- function(g, tau, eval_fun, bisections = 10L, conservative = FALSE) {
  n <- nrow(g); dth <- if (conservative && "dtheta" %in% names(g)) g$dtheta else rep(0, n)
  cl <- vapply(seq_len(n), function(i) clears_hi(g$status[i], g$hi[i], tau, dth[i]), NA)
  out <- list(first_grid_M = g$M[1], largest_grid_M = g$M[n], bisection = list(),
              fails = if (any(is.na(cl))) paste(g$M[is.na(cl)], collapse = " ") else NA_character_)
  if (is.na(cl[1])) return(c(list(status = "initial point failed (not treated as clear or lost)"), out))
  if (!cl[1]) return(c(list(status = "no initial clearance"), out))            # step 2: stop for this cell
  j <- which(cl %in% FALSE)[1]; f <- which(is.na(cl))[1]
  if (!is.na(f) && (is.na(j) || f < j)) {                                         # A2 item 3: a failure before the first loss is never a clearance
    m <- g$M[f - 1L]
    return(c(list(status = sprintf("undetermined beyond M=%.10g (failure before first loss)", m), last_clearing_M = m), out))
  }
  if (is.na(j)) return(c(list(status = "no crossing within the grid", last_clearing_M = max(g$M[cl %in% TRUE])), out))
  ilo <- max(which(cl[seq_len(j - 1L)] %in% TRUE)); lo <- g$M[ilo]; hi <- g$M[j]
  re <- if (j < n) g$M[(j + 1L):n][cl[(j + 1L):n] %in% TRUE] else numeric()     # step 4: reported, never searched
  nb <- 0L; note <- NA_character_; bflag <- NA_character_
  while (nb < bisections) {                                                      # step 3: inside [last clearing, first losing] only
    mid <- (lo + hi) / 2; r <- eval_fun(mid); c1 <- clears_hi(r$status, r$hi, tau, if (conservative) (r$dtheta %||% 0) else 0)
    out$bisection[[length(out$bisection) + 1L]] <- list(M = mid, hi = r$hi, status = r$status, clears = c1)
    if (is.na(c1)) { note <- sprintf("bisection evaluation failed at M=%.10g (%s); bracket reported as it stood", mid, r$status); bflag <- "failure in bracket"; break }   # step 6 / A2 item 3
    if (c1) lo <- mid else hi <- mid
    nb <- nb + 1L
  }
  c(list(status = if (length(re)) "breakdown bracketed; NON-MONOTONE (clears again later)" else "breakdown bracketed",
         last_clearing_M = g$M[ilo], first_losing_M = g$M[j], bracket_lo = lo, bracket_hi = hi, n_bis = nb,
         nonmono = length(re) > 0L, reclear = if (length(re)) paste(re, collapse = " ") else NA_character_, note = note, bracket_flag = bflag), out)
}

# Addendum A1 items 2-3: both flags from the same grid evaluations, each bisected inside its own bracket (eval_fun is cached by the caller, so a shared bracket shares evaluations).
# theta_grid = FALSE (SD / FLCI): the two flags coincide, one scan is run. headline = conservative status; "no initial clearance (conservative); nominal clears at 0" when only the conservative flag fails at 0.
scan_flags <- function(g, tau, eval_fun, bisections = 10L, theta_grid = TRUE) {
  nom <- scan_breakdown(g, tau, function(M) eval_fun(M, "nominal"), bisections, FALSE)
  if (!theta_grid) return(list(nominal = nom, conservative = NULL, headline = "nominal", headline_status = nom$status))
  con <- scan_breakdown(g, tau, function(M) eval_fun(M, "conservative"), bisections, TRUE)
  hs <- con$status
  if (hs == "no initial clearance" && !(nom$status %in% c("no initial clearance") || startsWith(nom$status, "initial point failed"))) con$status <- "no initial clearance (conservative); nominal clears at 0"
  list(nominal = nom, conservative = con, headline = "conservative", headline_status = if (startsWith(con$status, "no initial clearance")) "no initial clearance" else con$status)
}

# Plan 1.4 joint rule + addendum A2 item 3: the smaller of the two clusterings' breakdowns; either without initial clearance -> "no initial clearance". A clustering that is
# "undetermined beyond M = m" (a failed point before its first loss) is never resolved as clear: the joint is then bounded by m (or by the other side's certified value, whichever is smaller)
# and carries the qualifier "undetermined" - unless the other side's breakdown bracket lies wholly at or below m, where the joint is that bracket. a, b: one-row lists/data.tables with status,
# bracket_lo, bracket_hi, last_clearing_M, first_grid_M, largest_grid_M, bracket_flag (as written by the breakdown csv). Returns list(status, bracket_lo, bracket_hi, binding, qualifier).
joint_breakdown <- function(a, b, name_a = "uid", name_b = "stratum") {
  kind <- function(r) { s <- r$status; if (is.na(s)) "other" else if (startsWith(s, "no initial clearance")) "nic" else if (startsWith(s, "breakdown bracketed")) "br" else if (startsWith(s, "no crossing")) "nc" else if (startsWith(s, "undetermined")) "und" else "other" }
  qf <- function(r) { v <- r[["bracket_flag"]]; if (is.null(v) || is.na(v)) NA_character_ else v }
  ka <- kind(a); kb <- kind(b)
  res <- function(status, lo = NA_real_, hi = NA_real_, binding = NA_character_, qualifier = NA_character_) list(status = status, bracket_lo = lo, bracket_hi = hi, binding = binding, qualifier = qualifier)
  if (ka == "nic" || kb == "nic") return(res("no initial clearance", binding = paste(c(name_a, name_b)[c(ka, kb) == "nic"], collapse = "+")))
  if (ka == "other" || kb == "other") return(res(if (identical(a$status, b$status)) a$status else sprintf("not determined (%s: %s; %s: %s)", name_a, a$status, name_b, b$status)))
  if (ka == "und" || kb == "und") {
    if (ka == "und" && kb == "br" && b$bracket_hi <= a$last_clearing_M) return(res(b$status, b$bracket_lo, b$bracket_hi, name_b, qf(b)))
    if (kb == "und" && ka == "br" && a$bracket_hi <= b$last_clearing_M) return(res(a$status, a$bracket_lo, a$bracket_hi, name_a, qf(a)))
    cert <- function(r, k) if (k == "br") r$bracket_lo else r$last_clearing_M
    m <- min(cert(a, ka), cert(b, kb))
    return(res(sprintf("undetermined beyond M=%.10g", m), lo = m, binding = paste(c(name_a, name_b)[c(ka, kb) == "und"], collapse = "+"), qualifier = "undetermined"))
  }
  qq <- unique(stats::na.omit(c(qf(a), qf(b)))); qual <- if (length(qq)) paste(qq, collapse = "; ") else NA_character_
  if (ka == "nc" && kb == "nc") return(res("no crossing within the grid", lo = min(a$last_clearing_M, b$last_clearing_M), binding = "both"))
  if (ka == "nc") return(res(b$status, b$bracket_lo, b$bracket_hi, name_b, qual))
  if (kb == "nc") return(res(a$status, a$bracket_lo, a$bracket_hi, name_a, qual))
  pick_a <- a$bracket_lo < b$bracket_lo || (a$bracket_lo == b$bracket_lo && a$bracket_hi <= b$bracket_hi)
  w <- if (pick_a) a else b
  res(w$status, w$bracket_lo, w$bracket_hi, if (a$bracket_lo == b$bracket_lo && a$bracket_hi == b$bracket_hi) "both" else if (pick_a) name_a else name_b, qual)
}

# timing-probe status class (addendum A2 items 2, 4): ok / edge (a grid-edge or open-endpoint hit) / timeout / failed. Anything but "ok" trips the valve for that calendar.
probe_class <- function(status) if (identical(status, "timeout")) "timeout" else if (startsWith(status, "edge")) "edge" else if (startsWith(status, "ok")) "ok" else "failed"

# Plan 1.5 step 5 + addendum A1 item 6 + A2 items 1, 2, 4-6: the runtime valve, an ORDERED chain (i) parallel -> (ii) method Conditional -> (iii) gridPoints floor + theta grid centred on l'beta_post
# +-8 sd -> (iv) drop daily RM, with the trigger re-checked after every step, applied per calendar (never per grid point).
# probe(cal, method, gp, k, center) -> list(secs, status): the timing call at Mbar = 0 whose interval the caller discards unread; status "ok" or probe_class() != "ok". A probe that is not "ok" (failed,
# timeout, edge) trips the valve for its calendar and its seconds are NOT used for T (T is NA until a re-probe under a later step succeeds).
# T of a cell = probe secs * (planned evaluations of that cell: grid points + bisection evaluations, each threshold x flag counted separately; cell_evals[[cal]], one entry per cell) * 1.5 + the cell's
# SD time (cell_sd[[cal]]). Trigger: T > cell_limit for any cell, or the sum over cells (wall-clock after parallelism) > total_limit. Sum trigger: the chain goes to calendars with cells over the
# cell limit, else to the largest contributor. A calendar still tripping after (iii): daily -> (iv); weekly -> "valve exhausted", RM kept, and its time is excluded from the sum (A2 item 6).
valve_chain <- function(cals, probe, cell_limit = 7200, total_limit = 28800, procs = 4L,
                        cell_evals = setNames(lapply(cals, function(.) rep(60, 4)), cals), cell_sd = setNames(lapply(cals, function(.) rep(0, 4)), cals),
                        planned_method = "C-LF", planned_gp = 1000L, planned_k = 20, gp_floor = 200L, k_fallback = 8) {
  st <- lapply(setNames(cals, cals), function(cal) list(method = planned_method, gp = planned_gp, k = planned_k, center = "zero", level = 0L, dropped = FALSE, exhausted = FALSE, label = character(), secs = NA_real_, pstatus = NA_character_))
  parallel <- FALSE; LOG <- list(); iter <- 0L
  do_probe <- function(cal) { s <- st[[cal]]; r <- probe(cal, s$method, s$gp, s$k, s$center); st[[cal]]$secs <<- r$secs; st[[cal]]$pstatus <<- r$status }
  state <- function() {
    ptrip <- vapply(cals, function(cal) !st[[cal]]$dropped && !identical(st[[cal]]$pstatus, "ok"), TRUE)
    Tcell <- lapply(cals, function(cal) if (st[[cal]]$dropped || ptrip[[cal]]) numeric() else st[[cal]]$secs * cell_evals[[cal]] * 1.5 + cell_sd[[cal]])
    names(Tcell) <- cals
    Tmax <- vapply(cals, function(cal) if (ptrip[[cal]]) NA_real_ else if (length(Tcell[[cal]])) max(Tcell[[cal]]) else 0, 0)
    Tsum <- vapply(cals, function(cal) sum(Tcell[[cal]]), 0)
    cell_trip <- ptrip | (!is.na(Tmax) & Tmax > cell_limit)
    inc <- cals[!vapply(cals, function(cal) st[[cal]]$exhausted, TRUE) & !ptrip]                        # exhausted weekly time is excluded from the sum (A2 item 6)
    tot <- sum(Tsum[inc]); wall <- if (parallel) max(c(Tmax[inc], tot / procs, 0)) else tot
    list(ptrip = ptrip, Tmax = Tmax, Tsum = Tsum, cell_trip = cell_trip, wall = wall, sum_trip = wall > total_limit)
  }
  check <- function(action, detail = "") {
    s <- state(); iter <<- iter + 1L
    ex <- vapply(cals, function(cal) st[[cal]]$exhausted, TRUE)
    LOG[[length(LOG) + 1L]] <<- data.table::rbindlist(lapply(cals, function(cal) data.table::data.table(iter = iter, action = action, detail = detail, calendar = cal, method = st[[cal]]$method, grid_points = st[[cal]]$gp,
      grid_k = st[[cal]]$k, grid_center = st[[cal]]$center, rm_computed = !st[[cal]]$dropped, exhausted = st[[cal]]$exhausted, parallel = parallel, probe_secs = st[[cal]]$secs, probe_status = st[[cal]]$pstatus,
      probe_trip = s$ptrip[[cal]], projected_T_secs = s$Tmax[[cal]], cell_trigger = s$cell_trip[[cal]], sum_wall_secs = s$wall, sum_trigger = s$sum_trip, trigger = any(s$cell_trip) || s$sum_trip)))
    s$active <- any(s$cell_trip[!ex]) || s$sum_trip; s$any <- any(s$cell_trip) || s$sum_trip
    s
  }
  for (cal in cals) do_probe(cal)
  tr <- check("planned")
  while (tr$active) {
    if (!parallel) { parallel <- TRUE; tr <- check("(i) parallel: one process per cell"); next }
    cand <- cals[!vapply(cals, function(cal) st[[cal]]$dropped || st[[cal]]$exhausted, TRUE)]
    aff <- cand[tr$cell_trip[cand]]
    if (!length(aff) && tr$sum_trip && length(cand)) aff <- cand[which.max(tr$Tsum[cand])]
    if (!length(aff)) { tr <- check("exhausted: no further fallback applies", paste(cals[tr$cell_trip], collapse = ",")); break }
    acts <- character()
    for (cal in aff) {
      s <- st[[cal]]
      if (s$level == 0L) {
        if (s$method == "Conditional") acts <- c(acts, sprintf("%s (ii) skipped: already Conditional", cal)) else { st[[cal]]$method <- "Conditional"; acts <- c(acts, sprintf("%s (ii) method Conditional", cal)); st[[cal]]$label <- c(st[[cal]]$label, "(ii) method Conditional") }
        st[[cal]]$level <- 1L; if (s$method != "Conditional") do_probe(cal)
      } else if (s$level == 1L) {
        st[[cal]]$gp <- as.integer(gp_floor); st[[cal]]$k <- k_fallback; st[[cal]]$center <- "estimate"; st[[cal]]$level <- 2L; do_probe(cal)
        acts <- c(acts, sprintf("%s (iii) gridPoints %d, grid l'beta_post +-%g sd", cal, gp_floor, k_fallback)); st[[cal]]$label <- c(st[[cal]]$label, sprintf("(iii) gridPoints %d, grid centred on l'beta_post +-%g sd", gp_floor, k_fallback))
      } else {
        if (cal == "daily") { st[[cal]]$dropped <- TRUE; st[[cal]]$level <- 3L; acts <- c(acts, "daily (iv) daily RM not computed"); st[[cal]]$label <- c(st[[cal]]$label, "(iv) daily RM not computed") }
        else { st[[cal]]$exhausted <- TRUE; acts <- c(acts, sprintf("%s still over the limit after (iii); weekly RM stays (valve exhausted)", cal)); st[[cal]]$label <- c(st[[cal]]$label, "valve exhausted after (iii), RM kept") }
      }
    }
    tr <- check(paste(acts, collapse = "; "), paste(aff, collapse = ","))
  }
  s <- state()
  plan <- data.table::rbindlist(lapply(cals, function(cal) { x <- st[[cal]]; data.table::data.table(calendar = cal, rm_method = x$method, rm_grid_points = as.integer(x$gp), rm_grid_k = x$k, rm_grid_center = x$center, rm_computed = !x$dropped, exhausted = x$exhausted,
    parallel = parallel, fallback = if (parallel || length(x$label)) paste(c(if (parallel) "(i) parallel: one process per cell", x$label), collapse = "; ") else "none", probe_secs = x$secs, probe_status = x$pstatus, projected_T_secs = s$Tmax[[cal]]) }))
  list(plan = plan, log = data.table::rbindlist(LOG), final_trigger = any(s$cell_trip) || s$sum_trip)
}
