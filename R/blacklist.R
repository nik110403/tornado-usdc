# "On the USDC or USDT blacklist at any time in the main window" (prespec §1 baseline filter, main Jun-Aug population).
# Replays the full add/remove event history (deployment-extension file + g0 events) per (coin, address):
# state at PRE_START_BLOCK (events with block <= start applied in (block, log_index) order) OR any add event in
# [PRE_START_BLOCK, PRE_END_BLOCK]. Events after PRE_END_BLOCK are never used (hard rule).
PRE_START_BLOCK <- 14929560L
PRE_END_BLOCK <- 15298282L
BL_ADD <- c("Blacklisted", "AddedBlackList")
BL_REMOVE <- c("UnBlacklisted", "RemovedBlackList")

# events: data.table(coin, address, event, block, log_index). Returns lower-case addresses flagged on EITHER coin;
# attr "by_coin" = list(USDC = addrs, USDT = addrs). Coins are replayed independently.
blacklisted_any_in_window <- function(events, start_block = PRE_START_BLOCK, end_block = PRE_END_BLOCK) {
  ev <- as.data.table(events)[event %in% c(BL_ADD, BL_REMOVE) & block <= end_block]
  ev[, address := tolower(address)]
  setorder(ev, coin, address, block, log_index)
  ev[, is_add := event %in% BL_ADD]
  by_coin <- lapply(setNames(nm = c("USDC", "USDT")), function(cn) {
    e <- ev[coin == cn]
    if (!nrow(e)) return(character(0))
    on_at_start <- e[block <= start_block, .(on = is_add[.N]), by = address][on == TRUE, address]
    added_in <- e[block >= start_block & is_add == TRUE, unique(address)]
    union(on_at_start, added_in)
  })
  out <- unique(unlist(by_coin, use.names = FALSE))
  if (is.null(out)) out <- character(0)
  attr(out, "by_coin") <- by_coin
  out
}

# Load g0 event files: out/g0/blacklist_events_predeploy_ext.csv (deployment -> 8,999,999) + circle_events.csv +
# usdt_events.csv (census_start -> apr2025). All three are required (fail loudly, never a partial history).
load_blacklist_events <- function(g0dir) {
  fs <- file.path(g0dir, c("blacklist_events_predeploy_ext.csv", "circle_events.csv", "usdt_events.csv"))
  miss <- fs[!file.exists(fs)]
  if (length(miss)) stop(sprintf("blacklist history incomplete, missing: %s", paste(miss, collapse = ", ")), call. = FALSE)
  ext <- fread(fs[1], select = c("coin", "address", "event", "block", "log_index"))
  cc <- fread(fs[2], select = c("address", "event", "block", "log_index")); cc[, coin := "USDC"]
  ut <- fread(fs[3], select = c("address", "event", "block", "log_index")); ut[, coin := "USDT"]
  ev <- rbind(ext, cc, ut, use.names = TRUE)
  unique(ev, by = c("coin", "block", "log_index", "address", "event"))
}
