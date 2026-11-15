# §5.9 later-freeze drop list (S59). Signed R code; 0 RPC. Defines functions only; the entry points are lf_verify_veto() (called by
# run_post_stage after the signature passed) and lf_later_freeze() (called by run_post).
#
# SELECTION (tribunal 2026-10-26 row F): the drop list is DERIVED here, from the g0 lifetime freeze files (circle_events.csv, usdt_events.csv:
# pre-signature, hash-bound inputs) -- never read from a pre-made list. A frame wallet is selected when it has a Circle `Blacklisted` or a Tether
# `AddedBlackList` event in the block range (PRE_END, post_end], EXCLUDING the 38 designation-day addresses (the distinct Circle `Blacklisted`
# addresses in [EVENT_BLOCK, CLUSTER_END]; exactly 38 or the row is not estimable). PRE_END itself is excluded, post_end included.
#
# VETO (rows G, H, I): the separate g5_veto walk (gates/g5_veto.py) re-fetches the same two event types over (PRE_END, post_end] on the walk
# endpoint, re-verified on a second one. R recomputes the comparison from that walk's RAW LOGS (never from a summary or an "agree" flag) and
# asserts that the walk's ranges tile (PRE_END, post_end] exactly. Equality rule, per frame wallet (after removing the 38):
#   in g0 AND in the veto logs  -> stays dropped
#   in g0 only                  -> VETOED: removed from the drop list, reported with counts
#   in the veto logs only       -> reported as a discrepancy, NEVER added
# "none" is only ever a conclusion drawn from COVERAGE: header-only / empty / truncated g0 files (no rows before PRE_END or none after post_end,
# or not exactly 38 designation addresses) and a veto walk without the 38 designation addresses in its Circle logs are `not estimable`,
# never "no wallet was frozen". An absent veto dir is `not estimable` as well.
#
# VETO MAY NEVER SHRINK THE LIST (tribune F1): if ANY g0-derived frame wallet lacks its same-unit veto log (n_vetoed > 0) the row is `not estimable`
# (row-only), the vetoed wallets reported in post_later_freeze.csv. The veto dir is self-authenticating (see README); a forged one can only force that.
#
# F1 (fix round 2): g0 receipts bind count + span, not content, so a relabelled / moved g0 row would shrink the list silently; a veto-only wallet (per unit)
# therefore also makes the row `not estimable` (row-only), reported as veto_only_discrepancy_not_added. The comparison is per (wallet, unit). A g0 file that
# contradicts its own receipts is HARD even when it also trips a row-only check (the receipts are checked first).
#
# FAILURE RULE (row F/G + dissent) -- g0 SOURCE provenance (receipts not tiling / not reaching post_end / logCount != file rows / not exactly 38
# designation addresses) is HARD too (tribune F2): the same signed files feed the primary sample. Header-only / no later event stay row-only.
#
# (row F/G + dissent): a veto-output verification failure (missing / tampered file, hash mismatch, receipts that do not tile, a
# count or digest that the raw logs do not reproduce, an unreproduced second-endpoint check) makes ONLY the §5.9 row `not estimable`, with the
# reason. A failure in SHARED acquisition provenance -- the signature chain (signed_file_sha256), the producing commit, the chain-id / endpoint
# attestation, the pinned blocks -- is a hard denial (pg_fail -> non-zero exit), exactly like the main panel's chain. This file never uses the
# post_panel sidecar pattern: nothing here trusts a hash that is merely copied next to the data.

LF_PRE_END <- 15298282L           # config/blocks.json pre_period_end (tests/R/test_later_freeze.R diffs these constants against the config)
LF_EVENT_BLOCK <- 15302549L       # config/blocks.json event_block        (first Circle designation Blacklisted log)
LF_CLUSTER_END <- 15302766L       # config/blocks.json circle_cluster_end (last one)
LF_N_DESIGNATED <- 38L
# MIRROR of gates/g5_veto.py UNITS (tests/test_g5_veto.py diffs them): unit -> emitting contract and topic0 (lowercase)
LF_UNITS <- list(circle_blacklisted = list(address = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", topic0 = "0xffa4e6181777692565cf28528fc88fd1516ea86b56da075235fa575af6a4b855", event = "Blacklisted"),
                 tether_added_blacklist = list(address = "0xdac17f958d2ee523a2206206994597c13d831ec7", topic0 = "0x42e160154868087d6bfdc0ca23d96a1c1cfa32f1b72ba9ba27b69b98a0d819dc", event = "AddedBlackList"))
# g0's lifetime fetches in receipts.jsonl, told apart by their topic0 group (the receipts store no address): circle_events.csv = Blacklisted + UnBlacklisted,
# usdt_events.csv = the five USDT events. g0 also fetched a [Blacklisted]-only cluster span that overlaps the circle history, so it must NOT be selected.
LF_G0_TOPICS <- list(circle = c("0xffa4e6181777692565cf28528fc88fd1516ea86b56da075235fa575af6a4b855", "0x117e3210bb9aa7d9baff172026820255c6f6c30ba8999d1c2fd88e2848137c4e"),
                     usdt = c("0x42e160154868087d6bfdc0ca23d96a1c1cfa32f1b72ba9ba27b69b98a0d819dc", "0xd7e9ec6e6ecd65492dce6bf513cd6867560d49544421d0783ddf06e76c24470c",
                              "0x61e6e66b0d6339b2980aecc6ccc0039736791f0ccde9ed512e789a7fbdd698c6", "0xcb8241adb0c3fdb35b70c24ce35c5eb0c17af7431c99f827d44a445ca624176a",
                              "0x702d5967f45f6513a38ffc42d6ba9bf230bd40e8f53b16363c7eb4fd2deb9a44"))
LF_ENDPOINTS <- c("mevblocker", "tenderly")
LF_LOG_COLS <- c("unit", "block", "log_index", "tx_hash", "block_hash", "address", "topics", "data", "block_timestamp")
LF_FILES <- c("veto_logs.csv", "receipts.jsonl", "reverify.jsonl")

lf_row_fail <- function(...) stop(structure(class = c("lf_row_failure", "error", "condition"), list(message = sprintf(...), call = NULL)))

lf_sha_text <- function(lines) {          # == gates/g5_veto.rows_sha256: lines joined by "\n" plus a trailing "\n" (empty -> one newline)
  f <- tempfile("lf_h"); on.exit(unlink(f))
  writeBin(charToRaw(paste0(paste(lines, collapse = "\n"), "\n")), f)
  sha256_file(f)
}

# the nine canonical text fields, '|'-joined, sorted by (block, log_index): the digest gates/g5_veto.py writes into every receipt
lf_rows_sha <- function(d) {
  if (!nrow(d)) return(lf_sha_text(character(0)))
  d <- d[order(as.numeric(block), as.numeric(log_index))]
  lf_sha_text(do.call(paste, c(unname(as.list(d[, ..LF_LOG_COLS])), sep = "|")))
}

# wallet address of a raw Blacklisted / AddedBlackList log (mirror of g0_manifest._decode_address_event): indexed -> topic 1, else data word 0
lf_decode_wallet <- function(topics, data) {
  vapply(seq_along(topics), function(i) {
    tp <- strsplit(topics[i], "|", fixed = TRUE)[[1]]
    if (length(tp) >= 2L) return(paste0("0x", substr(tp[2], nchar(tp[2]) - 39L, nchar(tp[2]))))
    d <- sub("^0x", "", data[i])
    if (nchar(d) < 64L) NA_character_ else paste0("0x", substr(d, 25L, 64L))
  }, "")
}

# ---- source binding: the g0 receipts.jsonl behind the two g0 event files ---------------------------------------------------------
# The receipts file is hash-bound like the event files (verify_post_inputs: a differing hash is a HARD denial before this runs). Here, per file, the g0
# receipts of that file's fetch must tile one contiguous span (no gap, no overlap: lf_tiles, the R mirror of lib.rpc.assert_tiling), the span must reach
# from PRE_END + 1 to post_end, and their logCounts must add up to the file's rows. Returns NULL (ok) or a reason string (row-only).
lf_check_g0_receipts <- function(receipts_path, n_rows, post_end) {
  # every string returned here is a provenance failure: lf_read_g0 turns it into a HARD denial
  if (!is.character(receipts_path) || length(receipts_path) != 1L || is.na(receipts_path) || !file.exists(receipts_path)) return("g0 receipts.jsonl not supplied")
  rs <- tryCatch(lf_read_jsonl(receipts_path, "g0 receipts.jsonl"), lf_row_failure = function(e) conditionMessage(e), error = function(e) "g0 receipts.jsonl is unreadable")
  if (is.character(rs)) return(rs)
  rs <- Filter(function(r) identical(r$gate, "g0") && !is.null(r$fromBlock) && !is.null(r$toBlock) && !is.null(r$logCount) && is.list(r$topics), rs)
  for (u in names(LF_G0_TOPICS)) {
    sel <- Filter(function(r) length(r$topics) == 1L && setequal(tolower(unlist(r$topics[[1]])), LF_G0_TOPICS[[u]]), rs)
    if (!length(sel)) return(sprintf("g0 receipts hold no fetch of the %s events: source coverage not proven", u))
    if (!all(vapply(sel, function(r) identical(as.numeric(r$addressCount), 1) && length(r$endpoint) == 1L && is.character(r$endpoint), TRUE)) || length(unique(vapply(sel, function(r) r$endpoint, ""))) != 1L)
      return(sprintf("g0 %s receipts must each be a single-address fetch (addressCount == 1) on one single endpoint", u))
    from <- vapply(sel, function(r) as.numeric(r$fromBlock), 0); to <- vapply(sel, function(r) as.numeric(r$toBlock), 0); n <- vapply(sel, function(r) as.numeric(r$logCount), 0)
    if (anyNA(c(from, to, n))) return(sprintf("g0 %s receipts have a non-numeric block / logCount", u))
    t <- lf_tiles(from, to, min(from), max(to))
    if (!isTRUE(t)) return(sprintf("g0 %s receipts do not tile: %s", u, t))
    if (min(from) > LF_PRE_END + 1 || max(to) < post_end) return(sprintf("g0 %s receipts cover blocks %.0f..%.0f, not (%d, %.0f]: coverage not proven", u, min(from), max(to), LF_PRE_END, post_end))
    if (sum(n) != n_rows[[u]]) return(sprintf("g0 %s receipts count %.0f logs, the event file holds %d rows: file not bound to its receipts", u, sum(n), n_rows[[u]]))
  }
  NULL
}

# ---- source: the g0 lifetime freeze files ---------------------------------------------------------------------------------------
# list(ok, reason, circle = addresses (Blacklisted, block in (PRE_END, post_end]), usdt = addresses (AddedBlackList, same), fixed38)
lf_read_g0 <- function(circle_path, usdt_path, receipts_path, post_end) {
  bad <- function(why, hard = FALSE) list(ok = FALSE, reason = why, hard = hard)
  load1 <- function(path, what, ev_name) {
    d <- tryCatch(fread(path, colClasses = "character"), error = function(e) NULL)
    if (is.null(d) || length(setdiff(c("address", "event", "block"), names(d)))) return(list(why = sprintf("%s unreadable or lacks address/event/block", what)))
    r <- list(n = nrow(d))                              # the row count is known from here on: the receipts check must see it BEFORE any row-only verdict (F2)
    if (!nrow(d)) return(c(r, list(why = sprintf("%s has no rows (header-only / empty is never a verified 'none')", what))))
    if (!all(grepl("^[0-9]+$", d$block))) return(c(r, list(why = sprintf("%s has a non-integer block", what))))
    blk <- as.numeric(d$block)
    if (min(blk) > LF_PRE_END || max(blk) <= post_end)
      return(c(r, list(why = sprintf("%s does not span the window (blocks %.0f..%.0f must straddle %d..%.0f): coverage not proven", what, min(blk), max(blk), LF_PRE_END, post_end))))
    sel <- d$event == ev_name
    if (any(sel & !grepl("^0x[0-9a-f]{40}$", d$address))) return(c(r, list(why = sprintf("%s has a %s row with a malformed address", what, ev_name))))
    c(r, list(addr = d$address[sel], blk = blk[sel]))
  }
  ci <- load1(circle_path, "g0 circle_events", "Blacklisted"); us <- load1(usdt_path, "g0 usdt_events", "AddedBlackList")
  if (is.null(ci$n)) return(bad(ci$why)); if (is.null(us$n)) return(bad(us$why))   # unreadable: no row count to compare, row-only
  why <- lf_check_g0_receipts(receipts_path, list(circle = ci$n, usdt = us$n), post_end); if (!is.null(why)) return(bad(why, hard = TRUE))   # the same signed files feed the primary sample: a source-provenance failure (incl. a file contradicting its receipts) is a hard denial, checked BEFORE the row-only verdicts
  if (!is.null(ci$why)) return(bad(ci$why)); if (!is.null(us$why)) return(bad(us$why))
  fixed <- unique(ci$addr[ci$blk >= LF_EVENT_BLOCK & ci$blk <= LF_CLUSTER_END])
  if (length(fixed) != LF_N_DESIGNATED) return(bad(sprintf("g0 circle_events holds %d distinct designation-day addresses in [%d, %d], expected %d: source coverage not proven", length(fixed), LF_EVENT_BLOCK, LF_CLUSTER_END, LF_N_DESIGNATED), hard = TRUE))
  in_rng <- function(a, b) unique(a[b > LF_PRE_END & b <= post_end])
  list(ok = TRUE, circle = in_rng(ci$addr, ci$blk), usdt = in_rng(us$addr, us$blk), fixed38 = fixed)
}

# ---- the veto output ---------------------------------------------------------------------------------------------------------------
lf_tiles <- function(from, to, lo, hi) {            # TRUE or a reason string; ranges must tile [lo, hi] exactly
  o <- order(from); from <- from[o]; to <- to[o]; cursor <- lo
  if (any(to < from)) return("malformed range: to < from")
  for (i in seq_along(from)) {
    if (from[i] > cursor) return(sprintf("gap: expected block %.0f, next range starts at %.0f", cursor, from[i]))
    if (from[i] < cursor) return(sprintf("overlap: range [%.0f,%.0f] overlaps coverage up to %.0f", from[i], to[i], cursor - 1))
    cursor <- to[i] + 1
  }
  if (cursor - 1 != hi) return(sprintf("ranges cover up to %.0f, expected %.0f", cursor - 1, hi))
  TRUE
}

lf_read_jsonl <- function(path, what) {
  lines <- readLines(path, warn = FALSE); lines <- lines[nzchar(trimws(lines))]
  lapply(lines, function(l) {
    r <- tryCatch(jsonlite::fromJSON(l, simplifyVector = FALSE), error = function(e) lf_row_fail("%s has a corrupt line", what))
    if (!is.list(r)) lf_row_fail("%s has a non-object line", what)
    r
  })
}

# Verifies the g5_veto output dir against the CURRENT signature and RECOMPUTES it from the raw logs. Copies every file into `priv` first and works
# on the copies. Hard (pg_fail) = shared acquisition provenance; row-only = list(ok = FALSE, reason). ok: list(ok = TRUE, logs, manifest_sha).
lf_verify_veto <- function(sig, veto_dir, priv, repo_root = PRE_R_ROOT, pre_end = LF_PRE_END, post_end) {
  if (!is.list(sig) || is.null(sig$signed_hashes)) pg_fail("signature verification result required before the veto output is opened")
  tryCatch(lf_verify_veto_inner(sig, veto_dir, priv, repo_root, pre_end, post_end), lf_row_failure = function(e) list(ok = FALSE, reason = conditionMessage(e)))
}

lf_verify_veto_inner <- function(sig, veto_dir, priv, repo_root, pre_end, post_end) {
  isfile <- function(p) !(pg_islink(p) || !file.exists(p) || isTRUE(file.info(p)$isdir))
  if (!is.character(veto_dir) || length(veto_dir) != 1L || pg_islink(veto_dir) || !dir.exists(veto_dir)) lf_row_fail("veto dir is missing or a symlink")
  src_m <- file.path(veto_dir, "veto_manifest.json")
  if (!isfile(src_m)) lf_row_fail("veto_manifest.json is missing or a symlink")
  dir.create(file.path(priv, "veto"), showWarnings = FALSE)
  mp <- file.path(priv, "veto", "veto_manifest.json")           # parse and hash ONLY this private copy (no re-read of the mutable source)
  if (!file.copy(src_m, mp, overwrite = FALSE)) lf_row_fail("veto_manifest.json could not be copied")
  m <- tryCatch(jsonlite::fromJSON(mp, simplifyVector = FALSE), error = function(e) lf_row_fail("veto_manifest.json is not valid JSON"))
  if (!is.list(m) || is.null(names(m)) || pg_dupkeys(m)) lf_row_fail("veto_manifest.json is not a JSON object with unique keys")
  if (!identical(m[["gate"]], "g5_veto") || !identical(m[["schema_version"]], 1L)) lf_row_fail("veto_manifest.json is not a g5_veto schema 1 manifest")
  # --- SHARED PROVENANCE (hard) ---
  have <- m[["signed_file_sha256"]]
  if (!is.list(have) || !setequal(names(have), names(sig$signed_hashes)) || !all(vapply(names(sig$signed_hashes), function(k) identical(have[[k]], sig$signed_hashes[[k]]), TRUE)))
    pg_fail("the veto walk was not produced under the CURRENT signature (signed_file_sha256 differs)")
  pc <- m[["producing_commit"]]
  if (!is.character(pc) || length(pc) != 1L || !is_hex(pc, 40)) pg_fail("veto manifest carries no valid producing_commit")
  if (!identical(as.integer(pg_git(repo_root, c("merge-base", "--is-ancestor", shQuote(pc), "HEAD"))), 0L)) pg_fail("veto producing_commit %s is not an ancestor of HEAD", pc)
  walk <- m[["walk_endpoint"]]; replay <- m[["replay_endpoint"]]
  ok_ep <- function(x) is.character(x) && length(x) == 1L && x %in% LF_ENDPOINTS
  if (!ok_ep(walk) || !ok_ep(replay) || identical(walk, replay)) pg_fail("veto endpoint attestation: walk and replay must be two different known endpoints")
  if (!identical(m[["chain_id"]], 1L) || !setequal(unlist(m[["chain_asserted_endpoints"]]), c(walk, replay))) pg_fail("veto chain attestation: chain id 1 must be asserted on exactly the walk and replay endpoints")
  if (!identical(as.integer(m[["pre_end_block"]]), as.integer(pre_end)) || !identical(as.integer(m[["post_end_block"]]), as.integer(post_end)))
    pg_fail("veto walk blocks differ from the signed pre_end / post_end")
  # --- the files (row-only) ---
  fl <- m[["files"]]
  if (!is.list(fl) || !setequal(names(fl), LF_FILES) || !all(vapply(fl, function(v) is_hex(v, 64), TRUE))) lf_row_fail("veto manifest does not hash exactly %s", paste(LF_FILES, collapse = ", "))
  cp <- list()
  for (n in LF_FILES) {
    src <- file.path(veto_dir, n)
    if (!isfile(src)) lf_row_fail("%s is missing or a symlink", n)
    dest <- file.path(priv, "veto", n)
    if (!file.copy(src, dest, overwrite = FALSE)) lf_row_fail("%s could not be copied", n)
    if (!identical(sha256_file(dest), fl[[n]])) lf_row_fail("%s does not match the sha256 in the veto manifest (tampered or damaged)", n)
    cp[[n]] <- dest
  }
  rec <- Filter(function(r) identical(r$gate, "g5_veto") && identical(r$kind, "getLogs"), lf_read_jsonl(cp[["receipts.jsonl"]], "receipts.jsonl"))
  rvr <- Filter(function(r) identical(r$gate, "g5_veto") && identical(r$kind, "reverify"), lf_read_jsonl(cp[["reverify.jsonl"]], "reverify.jsonl"))
  if (!length(rec)) lf_row_fail("receipts.jsonl holds no getLogs receipt")
  keys <- c("unit", "endpoint", "fromBlock", "toBlock", "logCount", "sha256", "rows_sha256")
  rt <- function(rs) { for (r in rs) if (any(!keys %in% names(r)) || !is.character(r$unit) || !is.character(r$endpoint)) lf_row_fail("a receipt lacks %s", paste(keys, collapse = "/"))
    num <- function(f) vapply(rs, function(r) { v <- r[[f]]; if (is.null(v) || length(v) != 1L || is.list(v)) NA_real_ else suppressWarnings(as.numeric(v)) }, 0)
    chr <- function(f) vapply(rs, function(r) { v <- r[[f]]; if (is.character(v) && length(v) == 1L && !is.na(v)) v else NA_character_ }, "")
    d <- data.table(unit = vapply(rs, `[[`, "", "unit"), endpoint = vapply(rs, `[[`, "", "endpoint"), from = num("fromBlock"), to = num("toBlock"), n = num("logCount"), sha = chr("sha256"), rows = chr("rows_sha256"))
    if (anyNA(d)) lf_row_fail("a receipt has a missing / non-numeric fromBlock / toBlock / logCount or a missing sha256 / rows_sha256")   # row-only: never a generic abort of the post stage
    d }
  R1 <- rt(rec)
  # endpoint attestation (hard): every acquisition range on the attested walk endpoint, every re-verification WALK -> REPLAY
  if (!all(R1$endpoint == walk)) pg_fail("veto endpoint attestation: acquisition receipts are not all on the walk endpoint %s", walk)
  if (!length(rvr) || !all(vapply(rvr, function(r) identical(r$source, walk) && identical(r$endpoint, replay), TRUE))) {
    if (length(rvr)) pg_fail("veto endpoint attestation: re-verification records are not all %s -> %s", walk, replay)
    lf_row_fail("reverify.jsonl holds no re-verification record")
  }
  R2 <- rt(rvr)
  # --- units and tiling of (pre_end, post_end] ---
  if (!setequal(R1$unit, names(LF_UNITS))) lf_row_fail("receipted units {%s} differ from the expected {%s}", paste(sort(unique(R1$unit)), collapse = ","), paste(sort(names(LF_UNITS)), collapse = ","))
  for (u in names(LF_UNITS)) { t <- lf_tiles(R1[unit == u, from], R1[unit == u, to], pre_end + 1, post_end); if (!isTRUE(t)) lf_row_fail("unit %s does not tile (%d, %d]: %s", u, pre_end, post_end, t) }
  # --- recompute from the raw logs ---
  L <- tryCatch(fread(cp[["veto_logs.csv"]], colClasses = "character"), error = function(e) lf_row_fail("veto_logs.csv is unreadable"))
  if (!identical(names(L), LF_LOG_COLS)) lf_row_fail("veto_logs.csv columns are not %s", paste(LF_LOG_COLS, collapse = ","))
  if (nrow(L)) {
    if (!all(grepl("^[0-9]+$", L$block) & grepl("^[0-9]+$", L$log_index) & grepl("^[0-9]+$", L$block_timestamp))) lf_row_fail("veto_logs.csv has a non-integer block / log_index / timestamp")
    if (!all(L$unit %in% names(LF_UNITS))) lf_row_fail("veto_logs.csv has a row of an unknown unit")
    if (anyDuplicated(L[, .(unit, block_hash, log_index)])) lf_row_fail("veto_logs.csv has a duplicate (block_hash, log_index)")
    for (u in names(LF_UNITS)) {
      x <- L[unit == u]
      if (nrow(x) && (!all(x$address == LF_UNITS[[u]]$address) || !all(vapply(strsplit(x$topics, "|", fixed = TRUE), function(tp) length(tp) >= 1L && identical(tp[1], LF_UNITS[[u]]$topic0), TRUE)))) lf_row_fail("unit %s: a log is not %s from %s", u, LF_UNITS[[u]]$event, LF_UNITS[[u]]$address)
      if (nrow(x) && (any(as.numeric(x$block) <= pre_end) || any(as.numeric(x$block) > post_end))) lf_row_fail("unit %s: a log lies outside (%d, %d]", u, pre_end, post_end)
    }
  }
  L[, `:=`(blk = as.numeric(block))]
  total <- 0
  for (i in seq_len(nrow(R1))) {
    r <- R1[i]; sub <- L[unit == r$unit & blk >= r$from & blk <= r$to]; total <- total + nrow(sub)
    if (nrow(sub) != r$n) lf_row_fail("%s [%.0f,%.0f]: receipt says %.0f logs, the raw logs hold %d", r$unit, r$from, r$to, r$n, nrow(sub))
    if (!identical(lf_rows_sha(sub), r$rows)) lf_row_fail("%s [%.0f,%.0f]: the raw logs do not reproduce the receipt's rows_sha256", r$unit, r$from, r$to)
    m2 <- R2[unit == r$unit & from == r$from & to == r$to]
    if (nrow(m2) != 1L) lf_row_fail("%s [%.0f,%.0f]: not re-verified on the second endpoint (%d records)", r$unit, r$from, r$to, nrow(m2))
    if (m2$n != r$n || m2$sha != r$sha || m2$rows != r$rows) lf_row_fail("%s [%.0f,%.0f]: the second endpoint's record disagrees with the receipt", r$unit, r$from, r$to)
  }
  if (total != nrow(L)) lf_row_fail("veto_logs.csv holds %d logs outside every receipted range", nrow(L) - total)
  if (nrow(R2) != nrow(R1)) lf_row_fail("reverify.jsonl has %d records for %d receipted ranges", nrow(R2), nrow(R1))
  L[, wallet := lf_decode_wallet(topics, data)]
  if (nrow(L) && !all(grepl("^0x[0-9a-f]{40}$", L$wallet))) lf_row_fail("veto_logs.csv has a log whose wallet address cannot be decoded")
  list(ok = TRUE, logs = L[, .(unit, block = blk, wallet)], manifest_sha = sha256_file(mp), n_logs = nrow(L), n_ranges = nrow(R1))
}

# ---- the derivation + comparison ---------------------------------------------------------------------------------------------------
# inputs: list(circle_events, usdt_events, g0_receipts, veto = lf_verify_veto() result, post_end_block). Returns NULL (nothing supplied) or
# list(status = "derived", dropped, vetoed, discrepancy, counts...) or list(status = "not estimable", reason).
lf_later_freeze <- function(inputs, frame_addr) {
  if (is.null(inputs)) return(NULL)
  ne <- function(why) list(status = "not estimable", reason = why)
  v <- inputs$veto
  pe <- inputs$post_end_block
  g0 <- lf_read_g0(inputs$circle_events, inputs$usdt_events, inputs$g0_receipts, pe)     # read first: a hard g0 failure must stop even when the veto is also bad
  if (!isTRUE(g0$ok) && isTRUE(g0$hard)) pg_fail("g0 source provenance failed (the same signed files feed the primary sample): %s", g0$reason)
  if (!is.list(v) || !isTRUE(v$ok)) return(ne(sprintf("g5_veto output not verified: %s", if (is.list(v) && !is.null(v$reason)) v$reason else "absent")))
  if (!isTRUE(g0$ok)) return(ne(g0$reason))
  L <- v$logs
  vc <- L[unit == "circle_blacklisted"]
  if (!all(g0$fixed38 %in% vc[block >= LF_EVENT_BLOCK & block <= LF_CLUSTER_END, wallet]))
    return(ne("veto walk lacks the 38 designation-day addresses in its Circle logs: its coverage is not proven"))
  frame_addr <- unique(frame_addr)
  g0_list <- setdiff(intersect(frame_addr, union(g0$circle, g0$usdt)), g0$fixed38)
  # per unit: a Circle event is only matched by a Circle veto log, a Tether event by a Tether one (an address frozen by one issuer is not vouched by the other)
  veto_c <- setdiff(intersect(frame_addr, L[unit == "circle_blacklisted", wallet]), g0$fixed38)
  veto_t <- setdiff(intersect(frame_addr, L[unit == "tether_added_blacklist", wallet]), g0$fixed38)
  veto_list <- union(veto_c, veto_t)
  g0_c <- setdiff(intersect(frame_addr, g0$circle), g0$fixed38); g0_t <- setdiff(intersect(frame_addr, g0$usdt), g0$fixed38)
  vetoed <- union(setdiff(g0_c, veto_c), setdiff(g0_t, veto_t))                            # per (wallet, unit): a g0 event without its same-unit veto log
  dropped <- setdiff(g0_list, vetoed)                                                      # every one of its g0 events confirmed by the same unit
  disc <- union(setdiff(veto_c, g0_c), setdiff(veto_t, g0_t))                              # veto only (per unit): reported, NEVER added
  # F1: g0 receipts bind count + span, not content, so a relabelled / moved g0 row shrinks g0_list and shows up here as a veto-only wallet: not estimable too
  if (length(vetoed) || length(disc)) return(c(ne(sprintf("%d g0-derived wallet-unit(s) are not confirmed by the veto walk and %d veto-only wallet-unit(s) are absent from g0: the veto may never silently shrink the drop list and g0 may not be silently incomplete, so the row is not estimable", length(vetoed), length(disc))),
                               list(vetoed = sort(vetoed), discrepancy = sort(disc), n_g0_derived = length(g0_list), n_veto = length(veto_list), veto_manifest_sha = v$manifest_sha)))
  list(status = "derived", dropped = sort(dropped), vetoed = character(0), discrepancy = sort(disc), n_g0_derived = length(g0_list), n_veto = length(veto_list),
       n_fixed38 = length(g0$fixed38), post_end_block = pe, veto_manifest_sha = v$manifest_sha)
}
