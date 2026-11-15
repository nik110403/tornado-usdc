# "Blacklisted at any time in [14,929,560, 15,298,282]" -- synthetic add/remove sequences, USDC and USDT independent.
S <- PRE_START_BLOCK; E <- PRE_END_BLOCK
ev <- function(coin, address, event, block, log_index = 0L) data.table(coin, address, event, block, log_index)
flagged <- function(...) as.vector(blacklisted_any_in_window(rbindlist(list(...))))
test("blacklist window: added before window, removed inside -> flagged", {
  stopifnot(identical(flagged(ev("USDC", "0xa", "Blacklisted", S - 1000L), ev("USDC", "0xa", "UnBlacklisted", S + 5L)), "0xa"))
})
test("blacklist window: added and removed before window -> not flagged", {
  stopifnot(length(flagged(ev("USDC", "0xa", "Blacklisted", S - 1000L), ev("USDC", "0xa", "UnBlacklisted", S - 10L))) == 0L)
})
test("blacklist window: added inside window (even if removed inside) -> flagged", {
  stopifnot(identical(flagged(ev("USDT", "0xb", "AddedBlackList", S + 10L), ev("USDT", "0xb", "RemovedBlackList", S + 20L)), "0xb"))
})
test("blacklist window: never blacklisted -> not flagged; events after the window are ignored", {
  stopifnot(length(flagged(ev("USDC", "0xc", "Blacklisted", E + 1L), ev("USDT", "0xd", "DestroyedBlackFunds", S + 1L))) == 0L)
})
test("blacklist window: re-add after removal before window replays in (block, log_index) order", {
  stopifnot(identical(flagged(ev("USDC", "0xa", "UnBlacklisted", S - 5L, 2L), ev("USDC", "0xa", "Blacklisted", S - 5L, 1L)), character(0)),
            identical(flagged(ev("USDC", "0xa", "UnBlacklisted", S - 5L, 1L), ev("USDC", "0xa", "Blacklisted", S - 5L, 2L)), "0xa"))
})
test("blacklist window: USDC and USDT histories are independent (a USDT removal does not clear a USDC listing)", {
  r <- blacklisted_any_in_window(rbindlist(list(ev("USDC", "0xa", "Blacklisted", S - 50L), ev("USDT", "0xa", "AddedBlackList", S - 60L),
                                                ev("USDT", "0xa", "RemovedBlackList", S - 40L), ev("USDT", "0xe", "AddedBlackList", S - 9L))))
  bc <- attr(r, "by_coin")
  stopifnot(identical(bc$USDC, "0xa"), identical(bc$USDT, "0xe"), setequal(r, c("0xa", "0xe")))
})
