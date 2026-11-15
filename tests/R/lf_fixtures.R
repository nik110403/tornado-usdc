# Fixtures for the §5.9 later-freeze tests (sourced by test_later_freeze.R and test_post.R; not a test file). Synthetic only: nothing under out/ is read.
# mk_g0(): g0-style lifetime freeze files. mk_veto(): a g5_veto output dir written by R (digests via lf_rows_sha); tests/test_g5_veto.py checks
# that the PYTHON producer's output verifies in R as well (parity).
LFX_POST_END <- 15500000L
LFX_SIG <- list(prespec_text = strrep("1", 64), numerical_manifest = strrep("2", 64), executable_config = strrep("3", 64), code_manifest = strrep("4", 64), signing_json = strrep("5", 64))
lfx_addr <- function(i) sprintf("0x%040x", 1000L + i)
LFX_FIXED38 <- lfx_addr(1:38)
LFX_USDC <- LF_UNITS$circle_blacklisted$address
LFX_USDT <- LF_UNITS$tether_added_blacklist$address

# events: data.table(addr, block, token = "circle"|"tether") -> g0 files. Always adds the 38 designation rows, an early row, a late row (coverage)
# and decoys (UnBlacklisted / RemovedBlackList in range), unless cover = FALSE.
mk_g0 <- function(dir, events, cover = TRUE, circle_header_only = FALSE, usdt_header_only = FALSE, n_fixed = 38L, receipts = identity) {
  dir.create(dir, showWarnings = FALSE, recursive = TRUE)
  mk_row <- function(a, ev, b) data.table(address = a, event = ev, block = sprintf("%.0f", b), date = "2022-08-08", tx_hash = sprintf("0x%064x", b), log_index = "0")
  ci <- rbind(mk_row(lfx_addr(900), "Blacklisted", 12000000L),
              mk_row(LFX_FIXED38[seq_len(n_fixed)], "Blacklisted", LF_EVENT_BLOCK + seq_len(n_fixed)),
              if (cover) mk_row(lfx_addr(901), "Blacklisted", LFX_POST_END + 500L),
              mk_row(lfx_addr(902), "UnBlacklisted", LF_PRE_END + 5L),
              if (nrow(events[token == "circle"])) mk_row(events[token == "circle", addr], "Blacklisted", events[token == "circle", block]))
  us <- rbind(mk_row(lfx_addr(903), "AddedBlackList", 12000000L),
              if (cover) mk_row(lfx_addr(904), "AddedBlackList", LFX_POST_END + 500L),
              mk_row(lfx_addr(905), "RemovedBlackList", LF_PRE_END + 7L),
              mk_row(lfx_addr(906), "DestroyedBlackFunds", LF_PRE_END + 9L),
              if (nrow(events[token == "tether"])) mk_row(events[token == "tether", addr], "AddedBlackList", events[token == "tether", block]))
  cp <- file.path(dir, "circle_events.csv"); up <- file.path(dir, "usdt_events.csv")
  fwrite(if (circle_header_only) ci[0] else ci, cp); fwrite(if (usdt_header_only) us[0] else us, up)
  # g0 receipts.jsonl: per file the real shape (g0 fetches in 3 tiling spans from the census block, topics group of that file, logCounts summing to the rows), plus
  # the overlapping [Blacklisted]-only cluster span g0 also writes (it must be ignored). `receipts` can rewrite the lines before they are written.
  nci <- if (circle_header_only) 0L else nrow(ci); nus <- if (usdt_header_only) 0L else nrow(us)
  mk_rc <- function(topics, n, cuts = c(11000000, 13000000), hi = LFX_POST_END + 1000L) {
    b <- c(9000000, cuts, hi + 1); k <- length(b) - 1L; cn <- c(rep(0L, k - 1L), n)
    lapply(seq_len(k), function(i) list(addressCount = 1L, endpoint = "tenderly", fromBlock = b[i], gate = "g0", logCount = cn[i], sha256 = sprintf("%064x", i), t = 1, toBlock = b[i + 1L] - 1, topics = list(topics)))
  }
  rcs <- c(mk_rc(as.list(LF_G0_TOPICS$circle), nci), mk_rc(as.list(LF_G0_TOPICS$usdt), nus),
           list(list(addressCount = 1L, endpoint = "tenderly", fromBlock = LF_EVENT_BLOCK, gate = "g0", logCount = 38L, sha256 = strrep("a", 64), t = 1, toBlock = LF_CLUSTER_END, topics = list(list(LF_G0_TOPICS$circle[1])))))
  rcs <- receipts(rcs)
  rp <- file.path(dir, "receipts.jsonl")
  writeLines(vapply(rcs, function(r) as.character(jsonlite::toJSON(r, auto_unbox = TRUE)), ""), rp)
  list(circle = cp, usdt = up, receipts = rp)
}

lfx_log <- function(unit, addr, block, i) {
  tok <- if (unit == "circle_blacklisted") LFX_USDC else LFX_USDT
  pad <- paste0("0x", strrep("0", 24), sub("^0x", "", addr))
  data.table(unit = unit, block = sprintf("%.0f", block), log_index = sprintf("%.0f", i), tx_hash = sprintf("0x%064x", block), block_hash = sprintf("0x%064x", block + 1e6), address = tok,
             topics = if (unit == "circle_blacklisted") paste(LF_UNITS[[unit]]$topic0, pad, sep = "|") else LF_UNITS[[unit]]$topic0,
             data = if (unit == "circle_blacklisted") "0x" else pad, block_timestamp = sprintf("%.0f", 1660000000 + block %% 1000))
}

# events: data.table(addr, block, unit). The 38 are always present in the Circle unit. `cuts`: the interior range boundaries (default 2 interior cuts => 3 ranges per unit).
mk_veto <- function(dir, events, pre = LF_PRE_END, post = LFX_POST_END, cuts = c(15350000L, 15420000L), fixed38 = TRUE, hashes = LFX_SIG, commit = NULL,
                    walk = "mevblocker", replay = "tenderly", chain_id = 1L, tamper = function(d) NULL, manifest_mut = function(m) m) {
  dir.create(dir, showWarnings = FALSE, recursive = TRUE)
  ev <- copy(events)
  if (fixed38) ev <- rbind(ev, data.table(addr = LFX_FIXED38, block = LF_EVENT_BLOCK + seq_along(LFX_FIXED38), unit = "circle_blacklisted"))
  logs <- if (nrow(ev)) rbindlist(lapply(seq_len(nrow(ev)), function(i) lfx_log(ev$unit[i], ev$addr[i], ev$block[i], i))) else data.table(unit = character(), block = character(), log_index = character(),
            tx_hash = character(), block_hash = character(), address = character(), topics = character(), data = character(), block_timestamp = character())
  b <- c(pre, cuts, post); rec <- list(); rv <- list()
  for (u in names(LF_UNITS)) for (k in seq_len(length(b) - 1L)) {
    from <- b[k] + 1; to <- b[k + 1]
    sub <- logs[unit == u & as.numeric(block) >= from & as.numeric(block) <= to]
    base <- list(gate = "g5_veto", unit = u, fromBlock = from, toBlock = to, logCount = nrow(sub), sha256 = paste0("v2:", sprintf("%064x", k)), rows_sha256 = lf_rows_sha(sub), t = 1)
    rec[[length(rec) + 1L]] <- c(base[1], list(kind = "getLogs", endpoint = walk), base[-1])
    rv[[length(rv) + 1L]] <- c(base[1], list(kind = "reverify", endpoint = replay, source = walk), base[-1])
  }
  wl <- function(x, f) writeLines(vapply(x, function(r) as.character(jsonlite::toJSON(r, auto_unbox = TRUE)), ""), file.path(dir, f))
  wl(rec, "receipts.jsonl"); wl(rv, "reverify.jsonl"); fwrite(logs, file.path(dir, "veto_logs.csv"))
  tamper(dir)
  fl <- setNames(lapply(c("veto_logs.csv", "receipts.jsonl", "reverify.jsonl"), function(n) sha256_file(file.path(dir, n))), c("veto_logs.csv", "receipts.jsonl", "reverify.jsonl"))
  m <- list(schema_version = 1L, gate = "g5_veto", pre_end_block = pre, post_end_block = post, files = fl, signed_file_sha256 = hashes,
            producing_commit = if (is.null(commit)) trimws(system2("git", c("rev-parse", "HEAD"), stdout = TRUE)) else commit,
            walk_endpoint = walk, replay_endpoint = replay, chain_id = chain_id, chain_asserted_endpoints = c(walk, replay))
  m <- manifest_mut(m)
  jsonlite::write_json(m, file.path(dir, "veto_manifest.json"), auto_unbox = TRUE, pretty = TRUE)
  dir
}

lfx_verify <- function(dir, ...) {
  priv <- tempfile("lfpriv"); dir.create(priv)
  lf_verify_veto(list(signed_hashes = LFX_SIG), dir, priv, repo_root = getwd(), pre_end = LF_PRE_END, post_end = LFX_POST_END, ...)
}
