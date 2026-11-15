# S59 mutation evidence (check disabled -> specific test red -> reverted)

Each row: the named check was disabled in the source (the condition forced false, or the `pg_fail(...)` made a no-op `invisible(...)`), the stated test command was run,
the result recorded, and the source restored. All 17 original rows are RED; the fix round (tribune F1/F2/F5 + LOW fixes) adds the 18 rows of the second table, all RED (harness: each row's edit applied, the stated suite run, the file restored; every suite is green on the unmutated tree). (A first pass of three rows used `FALSE && a || b`, which does not disable `b`; those were redone by
neutralising the action instead, and a row that stayed green led to two test fixes: the R all-or-none row and the Python walk row, see the end.)

R tests: `Rscript tests/R/test_later_freeze.R` (LF) or `Rscript run_all.R --stage test` (R-all). Python: `python3 -m pytest -q -x <file> -k <expr>`.

| rule | mutated | run | first red |
|---|---|---|---|
| signed sha mismatch (veto not produced under the current signature), HARD | `R/later_freeze.R` `pg_fail("the veto walk was not produced...` -> `invisible(` | LF | shared acquisition provenance failures are HARD |
| producing_commit not an ancestor of HEAD, HARD | `R/later_freeze.R` ancestor check forced FALSE | LF | shared acquisition provenance failures are HARD |
| endpoint attestation (walk != replay, known endpoints), HARD | `R/later_freeze.R` `pg_fail("veto endpoint attestation: walk and replay...` -> `invisible(` | LF | shared acquisition provenance failures are HARD |
| chain-id attestation, HARD | `R/later_freeze.R` chain-id condition forced FALSE | LF | shared acquisition provenance failures are HARD |
| block bounds vs signed pre_end / post_end, HARD | `R/later_freeze.R` `pg_fail("veto walk blocks differ...` -> `invisible(` | LF | shared acquisition provenance failures are HARD |
| exactly 38 designation addresses | `R/later_freeze.R` `length(fixed) != LF_N_DESIGNATED` forced FALSE | LF | header-only / empty / truncated g0 files are never a verified 'none' |
| header-only g0 file -> not estimable | `R/later_freeze.R` `!nrow(d)` branch forced FALSE | LF | header-only / empty / truncated g0 files are never a verified 'none' |
| veto walk tiling gap (row-only) | `R/later_freeze.R` veto `lf_tiles` rejection forced FALSE | LF | veto verification failure => ONLY the row is not estimable: tiling gap... |
| g0 receipts tiling gap / overlap (new; HARD since the fix round) | `R/later_freeze.R` `g0 %s receipts do not tile` forced FALSE | LF | g0 receipts: a tiling gap / overlap / short span / count mismatch... |
| g0 receipts reach (pre_end, post_end] (new; HARD since the fix round) | `R/later_freeze.R` span-reach condition forced FALSE | LF | g0 receipts: a tiling gap / overlap / short span / count mismatch... |
| g0 receipts logCount == event-file rows (new; HARD since the fix round) | `R/later_freeze.R` count condition forced FALSE | LF | g0 receipts: a tiling gap / overlap / short span / count mismatch... |
| 2nd-endpoint disagreement, R recompute | `R/later_freeze.R` `m2$n != r$n \|\| m2$sha != ...` forced FALSE | LF | a SELF-CONSISTENT forgery (manifest re-hashed) is still caught... |
| 2nd-endpoint disagreement, Python walk | `gates/g5_veto.py` reverify-mismatch `if` forced False | `-k reverify_mismatch` tests/test_g5_veto.py | DID NOT RAISE ReverifyError |
| g0 receipts hash mismatch, R (new, HARD) | `R/post_gate.R` `sha256_file(dest) != want[[k]]` forced FALSE | R-all | post: pre-signature inputs must equal the signed mapping exactly... |
| g0 receipts hash mismatch, Python pre-access (new, HARD) | `gates/g5_post.py` InputMismatch `if` forced False | `-k g0_receipts` tests/test_g5_veto.py | test_g0_receipts_hash_is_bound_like_the_other_signed_inputs... |
| g0 keys all-or-none, R (new) | `R/post_gate.R` `length(intersect(keys, POST_G0_KEYS)) %in% 1:2` forced FALSE | R-all | post stage S59... (the `run2` case: two g0 keys signed, receipts missing) |
| g0 keys all-or-none, Python (new) | `gates/g5_post.py` `check_r_half` all-or-none `if` forced False | `-k inconsistent_r_half` tests/test_g5_post.py | `pytest.raises(SignatureError, match="all of .* or none")` |

Two corrections made because a mutation stayed green: (1) the R all-or-none test first used a signed mapping with all three keys, where the signed-mapping equality check
rejected the input anyway; it now signs only two g0 keys and passes no `--veto-dir`, so only the all-or-none rule can deny it. (2) the Python walk row's first mutation had the
same `a or b` precedence slip; the harness now wraps the whole condition.

## Fix round 1 (ONE) -- rows added/redone (first red = the test that went red)

Run with `/scratchpad/mut.py`-style harness (edit, run, restore). LF = `Rscript tests/R/test_later_freeze.R`, R-all = `Rscript run_all.R --stage test`.

| rule | mutated | run | first red |
|---|---|---|---|
| post_trace hashes the g0 receipts (`$g0_receipts`) | `R/post.R` `later_freeze_inputs$g0_receipts` -> `$receipts` (the original bug) | R-all | hard error in `sha256_file(NULL)` (the fail-closed `sha256_file`); `test_post.R` additionally asserts `input_sha256.g0_receipts == sha256(receipts file)` |
| `sha256_file` stops on NULL / zero-length / non-character / NA | `stop(` -> `invisible(` | LF | sha256_file refuses NULL / zero-length / non-character / NA paths |
| manifest PARSED from the private copy | `fromJSON(mp,` -> `fromJSON(src_m,` | LF | lf_verify_veto reads and hashes only the private COPY... |
| manifest HASHED from the private copy | `sha256_file(mp)` -> `sha256_file(src_m)` | LF | lf_verify_veto reads and hashes only the private COPY... |
| veto-intersect line (dropped = g0 AND same-unit veto) | `dropped <- union(intersect(g0_c, veto_c), ...)` -> `dropped <- g0_list` | LF | the veto may never shrink the list... |
| n_vetoed > 0 => row not estimable | `if (length(vetoed)) return(` -> `if (FALSE && ...` | LF | the veto may never shrink the list... |
| 38 designation addresses in the veto Circle logs (coverage) | `if (!all(g0$fixed38 %in% vc[...` forced FALSE | LF | header-only / empty / truncated g0 files are never a verified 'none' (veto-no38 case) |
| per-unit comparison | `L[unit == "tether_added_blacklist", wallet]` -> `L$wallet` | LF | the veto may never shrink the list... (veto-unit case) |
| g0 source failures are HARD | `pg_fail("g0 source provenance failed` -> `if (FALSE) pg_fail(` | LF | header-only / empty / truncated g0 files... (the n37 case is now a hard denial) |
| g0 receipts tiling / reach / logCount (HARD) | the three conditions forced FALSE (3 runs) | LF | g0 receipts: a tiling gap / overlap / short span / count mismatch... HARD denial |
| exactly 38 designation addresses, g0 (HARD) | `length(fixed) != LF_N_DESIGNATED` forced FALSE | LF | header-only / empty / truncated g0 files... (n37) |
| receipts `addressCount == 1` | condition -> `TRUE &&` | LF | g0 receipts: a tiling gap / ... (rc-addrcount) |
| receipts single endpoint per topic group | `!= 1L` -> `< 0L` | LF | g0 receipts: ... (rc-endpoints) |
| `lf_tiles` rejects `to < from` | `if (any(to < from)) return(` forced FALSE | LF | g0 receipts: ... (rc-reversed) |
| veto topic0 equality on first `\|` field | `identical(tp[1], topic0)` -> `startsWith(tp[1], topic0)` | LF | veto topic0 is matched by equality on the first '\|' field |
| `--veto-dir` xor g0 keys (isolated: signed mapping WITH g0 keys, files hash right) | `R/post_gate.R` `if (xor(...` forced FALSE | R-all | post stage S59: ... (the new "given together or not at all" assertions) |

Corrections during the round: the first per-unit mutation hit the Circle line, which no test could see (a Tether g0 event vouched by a Circle log needs the TETHER side loosened); it was moved to the Tether line and the row went red.

## Fix round 2 (ONE) -- rows added (first red = the test that went red; each mutation applied, suite run, file restored; harness = edit/run/restore)

LF = `Rscript tests/R/test_later_freeze.R`, POST = `Rscript tests/R/test_post.R` (also inside R-all). All suites green on the unmutated tree; the whole round's R changes reverted together (git stash of `R/`) turn LF and POST red.

| rule | mutated | run | first red |
|---|---|---|---|
| F1: veto-only wallet (disc > 0) => row NOT ESTIMABLE | `R/later_freeze.R` `if (length(vetoed) \|\| length(disc)) return(` -> `if (length(vetoed)) return(` | LF | the veto may never shrink the list... (veto-disc case, status must be not estimable) |
| F2: g0 receipts checked BEFORE the row-only file verdicts (contradiction = HARD) | `R/later_freeze.R` `if (is.null(ci$n)) return(...)` -> the old order (row-only `why` first) | LF | S59 fix round 2 F2: a truncated signed g0 file that contradicts its own receipts is HARD |
| F3: per (wallet, unit) comparison (wallet frozen by both issuers) | `vetoed <- union(setdiff(g0_c, veto_c), setdiff(g0_t, veto_t))` -> the old per-wallet `setdiff(g0_list, union(intersect...))` | LF | S59 fix round 2 F3/F4 |
| F4: Circle-side unit check | `veto_c <- ... L[unit == "circle_blacklisted", wallet]` -> `L$wallet` | LF | derivation selection (a Tether log vouching a Circle event changes the selection fixture first); the F3/F4 test also holds the 108 case |
| LOW-1: NA / non-numeric / null veto receipt field => row-only | `if (anyNA(d)) lf_row_fail(` -> `if (FALSE) lf_row_fail(` | LF | S59 fix round 2 LOW-1 |
| LOW-2: later_freeze_inputs without eligible_frame_coverage fails closed | `R/post.R` `po_stop(` -> `if (FALSE) po_stop(` | POST | fix round 1: later-freeze rematch ... (the `o_nocov` assertion) |
| csv reports veto-only wallets of a not-estimable row | `R/post.R` `\|\| length(lf$discrepancy)` removed | POST | same test (`o_vx` assertion) |
| pg_fail message no longer claims "before any data load" | `R/post_gate.R` text reverted | POST | same test (message assertion) |
