# S59 §5.9 later-freeze derivation + veto tests (R/later_freeze.R). Fixture-only: tmp dirs, synthetic events; no network, nothing under out/ is read.
if (!exists("classify_ci")) {
  a <- grep("^--file=", commandArgs(FALSE), value = TRUE)
  setwd(normalizePath(file.path(dirname(sub("^--file=", "", a[1])), "..", "..")))
  for (f in c("utils", "decide", "cem", "estimate", "validate", "synth", "pre", "blacklist", "presign", "post_gate", "later_freeze")) source(file.path("R", paste0(f, ".R")))
}
for (f in c("drdid", "post")) if (!exists(c(drdid = "drdid_estimate", post = "run_post")[[f]])) source(file.path("R", paste0(f, ".R")))
if (!exists("test")) {
  .t_total <- 0L
  test <- function(name, expr) {
    r <- try(force(expr), silent = TRUE)
    if (inherits(r, "try-error")) stop(sprintf("TEST FAILED: %s\n%s", name, conditionMessage(attr(r, "condition"))), call. = FALSE)
    .t_total <<- .t_total + 1L; cat(sprintf("  ok  %s\n", name))
  }
  standalone <- TRUE
} else standalone <- FALSE
source(file.path("tests", "R", "lf_fixtures.R"), local = TRUE)
fails <- function(expr, pat) { e <- try(expr, silent = TRUE); inherits(e, "try-error") && grepl(pat, conditionMessage(attr(e, "condition"))) }

local({
tmpd <- tempfile("lf"); dir.create(tmpd)
frame_addr <- lfx_addr(100:140)                           # the eligible frame
E <- function(i, block, unit) data.table(addr = lfx_addr(i), block = as.integer(block), unit = unit)
CB <- "circle_blacklisted"; TB <- "tether_added_blacklist"
# the selection fixture: boundary and window edges, both tokens, a non-frame wallet, a designation wallet that is also a frame wallet
ev_main <- rbind(E(101, LF_PRE_END + 1L, CB),            # first block in range: IN
                 E(102, LF_PRE_END, CB),                 # the boundary block itself: OUT
                 E(103, LFX_POST_END, TB),               # post_end: IN
                 E(104, LFX_POST_END + 1L, TB),          # one past post_end: OUT
                 E(105, 15400000L, TB),                  # tether-only wallet: IN
                 E(106, 15401000L, CB),                  # circle-only wallet: IN
                 E(500, 15402000L, CB))                  # not a frame wallet: OUT
ev_veto <- ev_main[block <= LFX_POST_END & block > LF_PRE_END]    # the veto walk only sees (pre_end, post_end]
frame2 <- c(frame_addr, LFX_FIXED38[1:3])                  # three designation-day addresses ARE frame wallets: must still be excluded
g0main <- mk_g0(file.path(tmpd, "g0"), ev_main[, .(addr, block, token = ifelse(unit == CB, "circle", "tether"))])
vd <- mk_veto(file.path(tmpd, "veto"), ev_veto)
vres <- lfx_verify(vd)
inp <- function(g0 = g0main, v = vres) list(circle_events = g0$circle, usdt_events = g0$usdt, g0_receipts = g0$receipts, post_end_block = LFX_POST_END, veto = v)

test("config constants mirror config/blocks.json; unit table mirrors the Python producer's", {
  b <- jsonlite::fromJSON(file.path("config", "blocks.json"))
  stopifnot(b$pre_period_end$block == LF_PRE_END, b$event_block$block == LF_EVENT_BLOCK, b$circle_cluster_end$block == LF_CLUSTER_END)
})
test("the veto output verifies on fixtures (positive control)", {
  if (!isTRUE(vres$ok)) cat("REASON:", vres$reason, "\n"); stopifnot(isTRUE(vres$ok), vres$n_ranges == 6L)
})
test("derivation selection: in-range kept, boundary 15,298,282 excluded, post_end included, post_end+1 excluded, 38 excluded, non-frame excluded, both tokens", {
  r <- lf_later_freeze(inp(), frame2); if (!identical(r$status, "derived")) cat("REASON:", r$reason, "\n")
  stopifnot(identical(r$status, "derived"), setequal(r$dropped, lfx_addr(c(101, 103, 105, 106))), length(r$vetoed) == 0L, length(r$discrepancy) == 0L,
            !any(LFX_FIXED38 %in% r$dropped), r$n_fixed38 == 38L, r$n_g0_derived == 4L)
  stopifnot(!lfx_addr(102) %in% r$dropped, !lfx_addr(104) %in% r$dropped, !lfx_addr(500) %in% r$dropped)
})
test("UnBlacklisted / RemovedBlackList / DestroyedBlackFunds decoys never select a wallet", {
  g <- mk_g0(file.path(tmpd, "g0-decoy"), data.table(addr = character(), block = integer(), token = character()), receipts = function(r) { r[[6]]$logCount <- r[[6]]$logCount + 1L; r })   # +1: the row added below
  d <- fread(g$usdt, colClasses = "character"); d <- rbind(d, data.table(address = lfx_addr(110), event = "RemovedBlackList", block = "15400000", date = "x", tx_hash = "0x1", log_index = "1"))
  fwrite(d, g$usdt)
  r <- lf_later_freeze(inp(g, lfx_verify(mk_veto(file.path(tmpd, "veto-decoy"), ev_veto[0]))), frame_addr)   # a veto walk that also saw no in-window freeze (F1: veto-only wallets would block the row)
  stopifnot(identical(r$status, "derived"), length(r$dropped) == 0L)    # full coverage + veto coverage prove "none"
})
test("header-only / empty / truncated g0 files are never a verified 'none'", {
  for (nm in c("circle_header_only", "usdt_header_only")) {
    g <- mk_g0(file.path(tmpd, paste0("g0-", nm)), data.table(addr = character(), block = integer(), token = character()), circle_header_only = nm == "circle_header_only", usdt_header_only = nm == "usdt_header_only")
    r <- lf_later_freeze(inp(g), frame_addr)
    stopifnot(identical(r$status, "not estimable"), grepl("no rows|never a verified", r$reason))
  }
  g <- mk_g0(file.path(tmpd, "g0-trunc"), data.table(addr = character(), block = integer(), token = character()), cover = FALSE)   # nothing after post_end
  r <- lf_later_freeze(inp(g), frame_addr); stopifnot(identical(r$status, "not estimable"), grepl("coverage not proven", r$reason))
  g <- mk_g0(file.path(tmpd, "g0-n37"), data.table(addr = character(), block = integer(), token = character()), n_fixed = 37L)
  stopifnot(fails(lf_later_freeze(inp(g), frame_addr), "g0 source provenance.*designation-day"))               # F2: not exactly 38 is HARD (same signed files feed the primary sample)
  stopifnot(is.null(lf_later_freeze(NULL, frame_addr)))                                                       # nothing supplied: the caller reports 'not supplied', not 'none'
  r <- lf_later_freeze(inp(v = list(ok = FALSE, reason = "x")), frame_addr); stopifnot(identical(r$status, "not estimable"), grepl("not verified: x", r$reason))
  r <- lf_later_freeze(inp(v = NULL), frame_addr); stopifnot(identical(r$status, "not estimable"))
  vn <- lfx_verify(mk_veto(file.path(tmpd, "veto-no38"), ev_veto, fixed38 = FALSE))                            # a veto walk that misses the designation cluster proves no coverage
  r <- lf_later_freeze(inp(v = vn), frame_addr); stopifnot(isTRUE(vn$ok), identical(r$status, "not estimable"), grepl("38 designation-day", r$reason))
})
test("the veto may never shrink the list: a g0-derived wallet without its same-unit veto log => NOT ESTIMABLE (row-only) with the vetoed list; veto-only wallets are reported, never added", {
  v2 <- lfx_verify(mk_veto(file.path(tmpd, "veto-diff"), rbind(ev_veto[addr != lfx_addr(105)], E(120, 15410000L, CB))))   # 105 missing from the veto; 120 only in the veto
  r <- lf_later_freeze(inp(v = v2), frame_addr)
  stopifnot(identical(r$status, "not estimable"), grepl("never silently shrink", r$reason), identical(r$vetoed, lfx_addr(105)), identical(r$discrepancy, lfx_addr(120)), is.null(r$dropped))
  v3 <- lfx_verify(mk_veto(file.path(tmpd, "veto-empty"), ev_veto[0]))                                   # an empty veto (only the 38) vetoes everything: not estimable
  r3 <- lf_later_freeze(inp(v = v3), frame_addr); stopifnot(identical(r3$status, "not estimable"), length(r3$vetoed) == 4L, is.null(r3$dropped))
  vd2 <- lfx_verify(mk_veto(file.path(tmpd, "veto-disc"), rbind(ev_veto, E(120, 15410000L, CB))))        # a veto-only wallet (a relabelled / moved g0 row) blocks the row; never added
  rd <- lf_later_freeze(inp(v = vd2), frame_addr); stopifnot(identical(rd$status, "not estimable"), grepl("veto-only", rd$reason), identical(rd$discrepancy, lfx_addr(120)), is.null(rd$dropped), length(rd$vetoed) == 0L)   # S59 F1: a veto-only wallet means g0 shrank (g0 receipts bind count + span, not content): NOT ESTIMABLE, reported
  # per unit: 105 is a Tether event in g0; the veto logging it only as a CIRCLE event does not vouch for it
  vu <- lfx_verify(mk_veto(file.path(tmpd, "veto-unit"), rbind(ev_veto[addr != lfx_addr(105)], E(105, 15400000L, CB))))
  ru <- lf_later_freeze(inp(v = vu), frame_addr); stopifnot(identical(ru$status, "not estimable"), identical(ru$vetoed, lfx_addr(105)))
})
test("S59 fix round 2 F1: a relabelled / moved g0 row (derived list shrinks, veto still sees it) => row NOT ESTIMABLE, not hard, wallet reported", {
  g <- mk_g0(file.path(tmpd, "g0-moved"), ev_main[addr != lfx_addr(106), .(addr, block, token = ifelse(unit == CB, "circle", "tether"))])   # same row count, 106 gone from g0
  r <- lf_later_freeze(inp(g), frame_addr)
  stopifnot(identical(r$status, "not estimable"), identical(r$discrepancy, lfx_addr(106)), is.null(r$dropped))
})
test("S59 fix round 2 F2: a truncated signed g0 file that contradicts its own receipts is HARD, whichever row-only check it also trips", {
  none <- data.table(addr = character(), block = integer(), token = character())
  g <- mk_g0(file.path(tmpd, "g0-truncrc"), none)                                           # receipts claim every row
  d <- fread(g$circle, colClasses = "character"); fwrite(d[as.numeric(block) < LF_PRE_END], g$circle)   # truncated: no longer spans; receipts still count all rows
  stopifnot(fails(lf_later_freeze(inp(g), frame_addr), "g0 source provenance.*file not bound"))
  g2 <- mk_g0(file.path(tmpd, "g0-emptyrc"), none, receipts = function(r) { r[[6]]$logCount <- 5; r })   # header-only usdt file, receipts claim logs
  d <- fread(g2$usdt, colClasses = "character"); fwrite(d[0], g2$usdt)
  stopifnot(fails(lf_later_freeze(inp(g2), frame_addr), "g0 source provenance"))
})
test("S59 fix round 2 F3/F4: the veto comparison is per (wallet, unit), Circle side and wallets frozen by both issuers included", {
  g <- mk_g0(file.path(tmpd, "g0-both"), rbind(ev_main, E(107, 15403000L, CB), E(107, 15403001L, TB), E(108, 15404000L, CB))[, .(addr, block, token = ifelse(unit == CB, "circle", "tether"))])
  evb <- rbind(ev_veto, E(107, 15403000L, CB), E(108, 15404000L, TB))                      # 107: Tether event unconfirmed (Circle one is); 108: g0 Circle, veto only as Tether
  v <- lfx_verify(mk_veto(file.path(tmpd, "veto-both"), evb))
  r <- lf_later_freeze(inp(g, v), frame_addr)
  stopifnot(identical(r$status, "not estimable"), setequal(r$vetoed, lfx_addr(c(107, 108))))
  v1 <- lfx_verify(mk_veto(file.path(tmpd, "veto-both-ok"), rbind(ev_veto, E(107, 15403000L, CB), E(107, 15403001L, TB), E(108, 15404000L, CB))))
  r1 <- lf_later_freeze(inp(g, v1), frame_addr); stopifnot(identical(r1$status, "derived"), setequal(r1$dropped, lfx_addr(c(101, 103, 105, 106, 107, 108))))   # positive control
  g8 <- mk_g0(file.path(tmpd, "g0-c8"), rbind(ev_main, E(108, 15404000L, CB))[, .(addr, block, token = ifelse(unit == CB, "circle", "tether"))])
  v8 <- lfx_verify(mk_veto(file.path(tmpd, "veto-c8"), rbind(ev_veto, E(108, 15404000L, TB))))                                # Circle g0 event vouched only by a Tether log
  r8 <- lf_later_freeze(inp(g8, v8), frame_addr); stopifnot(identical(r8$status, "not estimable"), identical(r8$vetoed, lfx_addr(108)))
})
test("S59 fix round 2 LOW-1: an NA / non-numeric / null veto receipt field makes the row not estimable (row-only), not a generic abort", {
  for (nm in c("fromBlock", "logCount", "sha256")) for (val in c('"abc"', "null")) if (!(nm == "sha256" && val == '"abc"')) {   # a string sha256 is legal (it fails the second-endpoint comparison instead)
    t <- mk_veto(file.path(tmpd, paste0("v-na-", nm, nchar(val))), ev_veto, tamper = function(d) { f <- file.path(d, "receipts.jsonl"); l <- readLines(f); l[1] <- sub(sprintf('"%s":("[^"]*"|[0-9]+)', nm), sprintf('"%s":%s', nm, val), l[1]); writeLines(l, f) })
    r <- lfx_verify(t); stopifnot(isFALSE(r$ok), grepl("non-numeric|missing", r$reason))
  }
  rr <- lf_later_freeze(inp(v = lfx_verify(mk_veto(file.path(tmpd, "v-na-x"), ev_veto, tamper = function(d) { f <- file.path(d, "reverify.jsonl"); l <- readLines(f); l[2] <- sub('"toBlock":[0-9]+', '"toBlock":"zz"', l[2]); writeLines(l, f) }))), frame_addr)
  stopifnot(identical(rr$status, "not estimable"), grepl("non-numeric|missing", rr$reason))
})
test("veto verification failure => ONLY the row is not estimable: tiling gap / overlap / short end / missing walk", {
  rm_line <- function(nm, k) mk_veto(file.path(tmpd, nm), ev_veto, tamper = function(d) { f <- file.path(d, "receipts.jsonl"); l <- readLines(f); writeLines(l[-k], f) })
  r <- lfx_verify(rm_line("v-gap", 2L)); stopifnot(isFALSE(r$ok), grepl("gap", r$reason))                              # a hole in the middle of a unit
  r <- lfx_verify(rm_line("v-short", 3L)); stopifnot(isFALSE(r$ok), grepl("cover up to", r$reason))                    # the unit stops short of post_end
  r <- lfx_verify(rm_line("v-unit", 6L)); stopifnot(isFALSE(r$ok))                                                     # last range of the second unit
  ov <- mk_veto(file.path(tmpd, "v-overlap"), ev_veto, tamper = function(d) { f <- file.path(d, "receipts.jsonl"); l <- readLines(f); writeLines(c(l, sub('"fromBlock":15350001', '"fromBlock":15349000', l[2])), f) })
  r <- lfx_verify(ov); stopifnot(isFALSE(r$ok))
})
test("g0 receipts: a tiling gap / overlap / short span / count mismatch / missing receipts / non-single-address or multi-endpoint fetch is a HARD denial (F2); the overlapping cluster span is ignored", {
  none <- data.table(addr = character(), block = integer(), token = character())
  hard <- function(nm, f, pat) { g <- mk_g0(file.path(tmpd, nm), none, receipts = f); stopifnot(fails(lf_later_freeze(inp(g), frame_addr), paste0("g0 source provenance.*", pat))) }
  hard("rc-gap", function(r) r[-2], "do not tile: gap")                                                                                      # circle span lost its middle range
  hard("rc-overlap", function(r) { r[[2]]$fromBlock <- r[[2]]$fromBlock - 5; r }, "do not tile: overlap")
  hard("rc-usdt-gap", function(r) r[-5], "do not tile")
  hard("rc-short", function(r) r[-3], "not \\(15298282")     # circle span stops at 12,999,999, short of post_end
  hard("rc-count", function(r) { r[[3]]$logCount <- r[[3]]$logCount + 1L; r }, "file not bound")
  hard("rc-nousdt", function(r) r[-(4:6)], "no fetch of the usdt")
  hard("rc-addrcount", function(r) { r[[2]]$addressCount <- 2L; r }, "single-address")
  hard("rc-endpoints", function(r) { r[[2]]$endpoint <- "mevblocker"; r }, "single endpoint")
  hard("rc-reversed", function(r) { r[[2]]$toBlock <- r[[2]]$fromBlock - 1; r }, "malformed range")
  stopifnot(fails(lf_later_freeze(modifyList(inp(), list(g0_receipts = file.path(tmpd, "nope.jsonl"))), frame_addr), "g0 source provenance.*not supplied"))
  writeLines("{not json", rp <- file.path(tmpd, "rc-corrupt.jsonl")); stopifnot(fails(lf_later_freeze(modifyList(inp(), list(g0_receipts = rp)), frame_addr), "g0 source provenance"))
  stopifnot(fails(lf_later_freeze(inp(v = list(ok = FALSE, reason = "x"), g = mk_g0(file.path(tmpd, "rc-gap2"), none, receipts = function(r) r[-2])), frame_addr), "g0 source provenance"))   # hard even when the veto is also bad
  stopifnot(identical(lf_later_freeze(inp(), frame_addr)$status, "derived"))                       # the [Blacklisted]-only cluster receipt (overlapping span) did not break the tiling
})
test("sha256_file refuses NULL / zero-length / non-character / NA paths", {
  for (bad in list(NULL, character(0), 1, NA_character_, c("a", "b"))) stopifnot(fails(sha256_file(bad), "sha256_file"))
})
test("lf_tiles rejects a reversed range (to < from) as malformed", {
  stopifnot(grepl("malformed", lf_tiles(c(1, 5), c(4, 3), 1, 12)))
})
test("veto topic0 is matched by equality on the first '|' field (not startsWith)", {
  ext <- mk_veto(file.path(tmpd, "v-topic"), ev_veto, tamper = function(d) { L <- fread(file.path(d, "veto_logs.csv"), colClasses = "character"); L[1, topics := paste0(sub("\\|.*$", "", topics), "ab", sub("^[^|]*", "", topics))]; fwrite(L, file.path(d, "veto_logs.csv")) })
  r <- lfx_verify(ext); stopifnot(isFALSE(r$ok), grepl("is not", r$reason))
})
test("lf_verify_veto reads and hashes only the private COPY of veto_manifest.json", {
  src <- readLines(file.path("R", "later_freeze.R")); i <- grep("veto_manifest.json", src, fixed = TRUE)
  stopifnot(!any(grepl("file.path\\(veto_dir, \"veto_manifest.json\"\\)", src[i]) & !grepl("src_m", src[i])), any(grepl("file.copy\\(src_m, mp", src)),
            any(grepl("^  mp <- file.path\\(priv, \"veto\", \"veto_manifest.json\"\\)", src)), any(grepl("jsonlite::fromJSON(mp,", src, fixed = TRUE)), any(grepl("manifest_sha = sha256_file(mp)", src, fixed = TRUE)))
  v <- mk_veto(file.path(tmpd, "v-copy"), ev_veto); priv <- tempfile("lfp"); dir.create(priv)
  r <- lf_verify_veto(list(signed_hashes = LFX_SIG), v, priv, repo_root = getwd(), pre_end = LF_PRE_END, post_end = LFX_POST_END)
  stopifnot(isTRUE(r$ok), identical(r$manifest_sha, sha256_file(file.path(priv, "veto", "veto_manifest.json"))), file.exists(file.path(priv, "veto", "veto_manifest.json")))
})
test("tiling: gap, overlap and short coverage are named", {
  stopifnot(grepl("gap", lf_tiles(c(1, 10), c(4, 12), 1, 12)), grepl("overlap", lf_tiles(c(1, 3), c(4, 12), 1, 12)), grepl("cover up to", lf_tiles(c(1, 5), c(4, 9), 1, 12)), isTRUE(lf_tiles(c(5, 1), c(12, 4), 1, 12)))
})
test("a tampered receipt file (hash no longer matches the manifest) is a row-only failure; the row is reported not estimable", {
  t1 <- mk_veto(file.path(tmpd, "v-tamper"), ev_veto)
  writeLines(sub('"logCount":1', '"logCount":0', readLines(file.path(t1, "receipts.jsonl"))), file.path(t1, "receipts.jsonl"))
  r <- lfx_verify(t1); stopifnot(isFALSE(r$ok), grepl("tampered", r$reason))
  rr <- lf_later_freeze(inp(v = r), frame_addr); stopifnot(identical(rr$status, "not estimable"), grepl("tampered", rr$reason))
  t2 <- mk_veto(file.path(tmpd, "v-tamper-logs"), ev_veto); cat("x\n", file = file.path(t2, "veto_logs.csv"), append = TRUE)
  stopifnot(isFALSE(lfx_verify(t2)$ok))
  t3 <- mk_veto(file.path(tmpd, "v-missing"), ev_veto); unlink(file.path(t3, "reverify.jsonl")); stopifnot(grepl("missing", lfx_verify(t3)$reason))
  stopifnot(grepl("missing", lfx_verify(file.path(tmpd, "no-such-dir"))$reason))
})
test("a SELF-CONSISTENT forgery (manifest re-hashed) is still caught by recomputing from the raw logs", {
  # drop one raw log row, keep receipts/manifest consistent with the new bytes: the receipt's logCount / rows_sha256 no longer reproduce
  f1 <- mk_veto(file.path(tmpd, "v-forge1"), ev_veto, tamper = function(d) { L <- fread(file.path(d, "veto_logs.csv"), colClasses = "character"); L <- L[!(unit == CB & block == as.character(LF_PRE_END + 1L))]; fwrite(L, file.path(d, "veto_logs.csv")) })
  r <- lfx_verify(f1); stopifnot(isFALSE(r$ok), grepl("raw logs hold|do not reproduce", r$reason))
  f2 <- mk_veto(file.path(tmpd, "v-forge2"), ev_veto, tamper = function(d) { l <- readLines(file.path(d, "reverify.jsonl")); l[1] <- sub('"logCount":[0-9]+', '"logCount":999', l[1]); writeLines(l, file.path(d, "reverify.jsonl")) })
  r2 <- lfx_verify(f2); stopifnot(isFALSE(r2$ok), grepl("disagrees", r2$reason))
  f3 <- mk_veto(file.path(tmpd, "v-forge3"), ev_veto, tamper = function(d) { L <- fread(file.path(d, "veto_logs.csv"), colClasses = "character"); L[1, address := lfx_addr(7)]; fwrite(L, file.path(d, "veto_logs.csv")) })
  stopifnot(isFALSE(lfx_verify(f3)$ok))
})
test("shared acquisition provenance failures are HARD (stop), not row-only", {
  stopifnot(fails(lfx_verify(mk_veto(file.path(tmpd, "h-sig"), ev_veto, hashes = modifyList(LFX_SIG, list(prespec_text = strrep("9", 64))))), "CURRENT signature"))
  stopifnot(fails(lfx_verify(mk_veto(file.path(tmpd, "h-commit"), ev_veto, commit = strrep("0", 40))), "not an ancestor"))
  stopifnot(fails(lfx_verify(mk_veto(file.path(tmpd, "h-ep"), ev_veto, replay = "mevblocker")), "two different known endpoints"))
  stopifnot(fails(lfx_verify(mk_veto(file.path(tmpd, "h-chain"), ev_veto, chain_id = 5L)), "chain attestation"))
  stopifnot(fails(lfx_verify(mk_veto(file.path(tmpd, "h-post"), ev_veto, manifest_mut = function(m) { m$post_end_block <- LFX_POST_END - 1L; m })), "signed pre_end / post_end"))
  stopifnot(fails(lfx_verify(mk_veto(file.path(tmpd, "h-recep"), ev_veto, tamper = function(d) NULL, walk = "tenderly", replay = "mevblocker", manifest_mut = function(m) { m$walk_endpoint <- "mevblocker"; m$replay_endpoint <- "tenderly"; m })), "attestation"))
  stopifnot(fails(lfx_verify(mk_veto(file.path(tmpd, "h-rv"), ev_veto, manifest_mut = function(m) { m$replay_endpoint <- "mevblocker"; m })), "two different known endpoints"))
  # re-verification that did not run WALK -> REPLAY (receipt-level attestation): hard as well
  stopifnot(fails(lfx_verify(mk_veto(file.path(tmpd, "h-rv2"), ev_veto, tamper = function(d) { l <- readLines(file.path(d, "reverify.jsonl")); writeLines(sub('"endpoint":"tenderly"', '"endpoint":"mevblocker"', l), file.path(d, "reverify.jsonl")) })), "re-verification records"))
})
test("run_post is the ONLY consumer and has no hand-supplied list; run_post_stage still refuses a hand-supplied later_freeze csv", {
  stopifnot(!"later_freeze_path" %in% names(formals(run_post)), "later_freeze_inputs" %in% names(formals(run_post)), "later_freeze" %in% POST_REFUSED_KEYS)
  stopifnot(all(c("g0_circle_events", "g0_usdt_events") %in% POST_OPTIONAL_KEYS), "R/later_freeze.R" %in% POST_R_CODE_FILES, "gates/g5_veto.py" %in% POST_R_CODE_FILES)
  src <- paste(readLines(file.path("R", "later_freeze.R")), collapse = "\n")
  stopifnot(!grepl("system2\\(\"(curl|wget)|httr|curl::|url\\(", src), !grepl("verify_post_panel", sub("^(#[^\n]*\n)+", "", src)))
})
})
if (standalone) cat(sprintf("ALL %d LATER-FREEZE TESTS PASSED\n", .t_total))
