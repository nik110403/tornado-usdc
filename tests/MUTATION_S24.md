# S24 mutation evidence (rule disabled in `tools/s24_package.py` -> specific test red -> restored)

Harness: each row's edit is applied to the source (condition forced false, or the action neutralised), `python3 -m pytest -q -x tests/test_s24_package.py -k <expr>` is run, the file is restored.
All 16 rows are RED; every test is green on the unmutated tree. Several rules are defended twice (the assembler's own refusal AND g5's `check_r_half` / `check_config` run on the emitted bytes by
`check_unsigned`), so the tests pin the assembler-level message (`match=`); otherwise g5's second layer would hide the removal of the first. (First pass: the "pinned R package missing" row and the
"check_unsigned skipped" row stayed GREEN for exactly that reason; the first got a message-specific `match`, the second a dedicated test that injects an assembler bug, and both are RED below.)

| rule (requirement 4 / 5 / 6) | mutated in `tools/s24_package.py` | `-k` | first red test |
|---|---|---|---|
| g0 keys but no `eligible_frame_coverage` (roll-in r2) | `if g0 and not inputs.get("eligible_frame_coverage")` forced False | `g0_without` | test_refuse_g0_without_eligible_frame_coverage |
| refused post-window keys (later_freeze, outflow_evidence) | `if refused:` forced False | `refused_post_window` | test_refuse_refused_post_window_keys[later_freeze] |
| unknown key | `if unknown:` forced False | `unknown_key` | test_refuse_unknown_key |
| required key missing | `if missing:` forced False | `missing_required` | test_refuse_missing_required_key[pre/covariates.csv] |
| NaN / Inf | `_finite` condition forced False | `non_finite` | test_refuse_non_finite_values[nan] |
| input missing / symlink / not regular | `_regular` islink/isfile check forced False | `missing_unreadable` | test_refuse_missing_unreadable_or_symlinked_input |
| input unreadable | `except OSError` neutralised | `missing_unreadable` | test_refuse_missing_unreadable_or_symlinked_input |
| partial g0 set | all-or-none `if` forced False | `partial_g0` | test_refuse_partial_g0_set[keep0] |
| DRDID installed != R/drdid.R freeze | version comparison forced False | `drdid` | test_refuse_when_installed_drdid_differs_from_the_freeze |
| fixest / data.table pin missing or malformed | installed-version check forced False | `pinned_r_package` | test_refuse_when_a_pinned_r_package_is_missing_or_malformed |
| producer file != HEAD blob | `g5._committed_bytes` replaced by a plain worktree read | `differs_from_head` | test_refuse_producer_file_that_differs_from_head |
| non-empty outdir | guard forced False | `non_empty_outdir` | test_refuse_non_empty_outdir_and_leave_it_alone |
| round trip: config emitted without `post_input_paths` | `"post_input_paths": {}` | `roundtrip` | test_roundtrip_g5_chain_accepts_under_a_fixture_signature |
| round trip: fixture chain not actually run | `g5.verify_signature(...)` replaced by a stub | `roundtrip` | test_roundtrip_detects_a_broken_package |
| unsigned g5 checks gate the build | `check_unsigned(pkg, installed)` call removed | `refuses_its_own_output` | test_build_refuses_its_own_output_when_g5_would_deny_it |
| explicit "UNSIGNED" line in the summary | closing line removed | `assembles_strict` | test_assembles_strict_deterministic_unsigned_package |

"No output written on refusal" is structural: `build_package` is pure and `write_package` (sibling temp dir, then rename) runs last; every refusal test also asserts the outdir does not exist
(and the CLI variant that no `.s24-*` temp dir remains).
The R half of the round trip (`test_roundtrip_real_installed_r_accepts_in_python_and_in_r`: real installed fixest/data.table/DRDID, `verify_post_signature` + `verify_post_inputs` in R on the
fixture-signed temp repo) is green on the unmutated tree; it shares the `post_input_paths`-empty mutation with the row above (that run stops at the first red, the Python round trip).

## Fix round 1 (tribune chg-s24-trib + security chg-s24-sec FIX-FIRST) -- revert-red rows

Same harness: the rule is reverted in the source named, the `-k` selection is run (`-x`), the file is restored. All 25 rows RED; everything is green on the unmutated tree.
(First pass: the "eligible_frame_coverage pop/gen" row was a false RED -- its `-k` matched no test and exit code 5 looked like a failure; the `-k` was corrected and the row re-run RED on the named test. Two rows
-- "R.version.string unreadable" and "git HEAD failure" -- first stayed GREEN because g5's `check_r_half` / the `git show` branch produced an equivalent refusal; their tests now pin the assembler-level message.)

| # | rule | reverted in | `-k` | first red test |
|---|---|---|---|---|
| 1 | analysis_parameters ids == config populations | `tools/s24_package.py` `semantic_checks` id comparison -> `if False` | `analysis_parameters_population_differs` | test_refuse_analysis_parameters_population_differs_from_config |
| 2 | eligible_frame_coverage pop/gen == params (R reader, post.R:454) | `tools/s24_dryrun.R` condition -> `FALSE` | `eligible_frame_coverage_with_other_population` | test_refuse_eligible_frame_coverage_with_other_population_than_params |
| 3 | matched ⊆ retained (R reader, post.R:460) | `tools/s24_dryrun.R` `setdiff(...)` -> `FALSE` | `matched_not_subset_of_retained` | test_refuse_matched_not_subset_of_retained |
| 4 | g5 eligible set == coverage retained set | `semantic_checks` `set(retained) != frame` -> `False` | `g5_eligible_set_differs` | test_refuse_g5_eligible_set_differs_from_retained_set |
| 5 | matched ⊆ g5 frame when no coverage file | `elif not matched <= frame` -> `False` | `without_coverage_file` | test_refuse_matched_not_in_g5_frame_without_coverage_file |
| 6 | R's own readers run and a stop is a refusal | `r_dry_run` returncode check -> `False` | `rs_own_readers` | test_refuse_inputs_that_rs_own_readers_refuse[cov] |
| 7 | duplicate (pid, gid) | `len(set(keys)) != len(keys)` -> `False` | `duplicate_population` | test_refuse_duplicate_population_and_more_than_one_population |
| 8 | more than one population | `len(keys) != 1` -> `False` | `more_than_one_population` | same test (second assertion) |
| 9 | unfilled prespec blanks | `if not allow_blanks` -> `if False` | `production_refuses_unfilled or blank_flag_is_cli` | test_production_refuses_unfilled_prespec_blanks[...] |
| 10 | pre-amendment trimming sentence | `_OLD_TRIM_RE.search` -> `False` | `pre_amendment_trimming` | test_refuse_pre_amendment_trimming_sentence[False] |
| 11 | DRDID + imports + jsonlite pinned (Py) | `gates/g5_post.py` POST_PINNED_PACKAGES shrunk | `drdid_and_its_imports or r_half_fails...` (both files) | test_drdid_and_its_imports_and_jsonlite_are_pinned_and_r_version_recorded |
| 12 | pin list mirror in R | `R/post_gate.R` POST_PINNED_PACKAGES shrunk | `mirror` (tests/test_g5_post.py) | test_code_lists_include_run_all_and_every_imported_gate_and_mirror_the_r_gate |
| 13 | signed r_version == running R (Py) | `gates/g5_post.py` `if have_r != rv` -> `False` | `r_half_fails_before_any_access` | test_inconsistent_r_half_fails_before_any_access[...differs from the signed r_version] |
| 14 | signed r_version == running R (R, hard deny) | `R/post_gate.R` `identical(r_version, rv)` check -> `FALSE` | `Rscript run_all.R --stage test` | R test "post: every pinned package + R.version.string must EQUAL the signed values" |
| 15 | R.version.string unreadable refused | `_r_string` guard -> `False` | `r_version_string_is_unavailable` | test_refuse_when_r_version_string_is_unavailable_or_differs |
| 16 | git HEAD hard-fails, no "unknown" | `_git_head` returncode/format check -> `False` | `git_head_hard_fails` | test_git_head_hard_fails_and_never_says_unknown |
| 17 | assembler == its HEAD blob | `f.read() != b.stdout` -> `False` | `assembler_differs_from_its_head_blob` | test_refuse_when_the_assembler_differs_from_its_head_blob |
| 18 | summary forging chars (control, backtick, `|`) | `_clean` regex test removed | `forging` | test_refuse_forging_characters_in_paths[a\nb] |
| 19 | dates fullmatch `[0-9]{4}-[0-9]{2}-[0-9]{2}` | regex test removed | `non_ymd_dates` | test_refuse_non_ymd_dates[post_start_date-20220809] |
| 20 | event < bridge < post < merge | order check -> `False` | `outside_event_bridge_post_merge_order` | test_refuse_blocks_outside_event_bridge_post_merge_order[15302549-15523155] |
| 21 | blocks consistent with date window (tolerance now 3,000 blocks, see fix round 2 row 17) | `abs(blk - exp) > BLOCK_TOLERANCE` -> `False` | `inconsistent_with_the_date_window` | test_refuse_blocks_inconsistent_with_the_date_window[...] |
| 22 | non-empty `--g5-out-dir` refused | guard -> `False` | `existing_non_empty_g5_out_dir` | test_refuse_existing_non_empty_g5_out_dir |
| 23 | no rmdir-then-rename window | `os.rmdir(outdir)` re-added before `os.rename` | `never_rmdirs or loses_the_race` | test_write_package_never_rmdirs_the_outdir_and_replaces_an_empty_one |
| 24 | summary names the g0 post-cutoff material | g0 section emptied | `g0_post_cutoff_material` | test_summary_and_docstring_name_the_g0_post_cutoff_material |
| 25 | fixture commit ignores a global gpgsign | `-c commit.gpgsign=false` removed | `gpgsign` | test_fixture_commit_ignores_a_global_gpgsign_config |

Tolerance choice (row 21): the declared bridge/post_end blocks must be within 7,200 blocks (about one day at ~12 s) of the block expected for 23:59:59 UTC of the date, interpolated linearly between the pinned
`event_block` and `merge_block` timestamps (config/blocks.json). Fixture values 15,304,565 / 15,523,155 are those interpolations. Residual: the g0 files and the post panel are not read by the dry run (the
dry run covers the pre-window readers of R/post.R:29-41 and :452-466 plus the pool/deposit readers).

## Fix round 2 (final) -- revert-red rows

Same harness (rule reverted in the source named, `python3 -m pytest -q -x tests/test_s24_package.py -k <expr>`, file restored). All 23 rows RED; everything green on the unmutated tree
(first pass: row 23 was a no-op edit that kept "Matrix" and stayed GREEN; the mutation was corrected to drop the seven other packages and is RED).

| # | rule | reverted in | `-k` | first red test |
|---|---|---|---|---|
| 1 | matched wallet with B <= 0 stops in the dry run | `R/post.R` `po_pre_stage` uniqueN check -> `FALSE` | `baseline_B_le_0` | test_refuse_matched_wallet_with_baseline_B_le_0_in_the_dry_run |
| 2 | recomputed D/A caps == frozen cap_values in the dry run | `R/post.R` `po_pre_stage` cap loop disabled | `recomputed_caps_that_differ` | test_refuse_recomputed_caps_that_differ_from_the_frozen_cap_values_in_the_dry_run |
| 3 | 60 baseline days (po_frame) in the dry run | `R/post.R` `po_pre_stage` frame call -> `NULL` | `60_baseline_days_in_the_dry_run` | test_refuse_eligible_wallets_without_the_60_baseline_days_in_the_dry_run |
| 4 | dry run calls the shared function, no copied logic | `tools/s24_dryrun.R` copy of a stop text added | `one_shared_pre_function` | test_post_stage_and_dry_run_call_the_one_shared_pre_function_no_copied_logic |
| 5 | docs name the remaining post-fetch stops | `tools/s24_dryrun.R` header sentence reworded | `docs_claim_only` | test_docs_claim_only_what_is_checked_and_name_the_remaining_post_fetch_stops |
| 6 | trimming: code rule printed next to the prespec text | `tools/s24_package.py` summary code line blanked | `trimming_text` | test_trimming_text_and_code_are_printed_side_by_side_and_the_old_sentence_is_not_refused |
| 7 | "Signed: / Date:" line exempt | `_unsigned_text` exemption removed | `signed_date_line` | test_the_signed_date_line_is_exempt_but_other_blanks_on_the_same_text_are_not |
| 8 | extended blank patterns (TBD, XXX, `\_`, em-dash) | `_BLANK_RE` `TBD` alternative removed | `production_refuses_unfilled` | test_production_refuses_unfilled_prespec_blanks[Cut-points: D [TBD, TBD]] |
| 9 | filled cap values == frozen cap_values | `author_check_caps` mismatch test -> `False` | `differ_from_the_frozen_cap` | test_refuse_filled_cap_values_that_differ_from_the_frozen_cap_values |
| 10 | test-only build writes no SHA256 | `build_package` SHA256 guard forced True | `test_only_build` | test_test_only_build_has_no_sha256_marks_the_manifest_and_prints_a_loud_line |
| 11 | test-only build marks manifest `test_only: true` | marker assignment removed | `test_only_build` | same test |
| 12 | g5 `check_r_half` refuses `test_only` | `gates/g5_post.py` check -> `False` | `g5_refuses_a_test_only` | test_g5_refuses_a_test_only_manifest_in_check_r_half_and_check_config |
| 13 | g5 `check_config` refuses `test_only` | `gates/g5_post.py` check -> `False` | `g5_refuses_a_test_only` | same test (second assertion) |
| 14 | R `verify_post_signature` refuses `test_only` (Py/R parity) | `R/post_gate.R` check -> `FALSE` | `r_verify_post_signature_refuses` | test_r_verify_post_signature_refuses_a_test_only_manifest_python_r_parity |
| 15 | tools/s24_dryrun.R == its HEAD blob | `_git_head` blob comparison -> `False` | `dry_run_script_differs` | test_refuse_when_the_dry_run_script_differs_from_its_head_blob |
| 16 | dry-run sha256 recorded in manifest | `dryrun_sha256=` removed | `dry_run_hash_is_recorded` | test_dry_run_hash_is_recorded_in_manifest_and_summary |
| 17 | block tolerance 3,000 | `BLOCK_TOLERANCE` -> 7200 | `blocks_inside_the_tolerance` | test_blocks_inside_the_tolerance_are_accepted |
| 18 | length-1 matched/retained stay JSON arrays | `tools/s24_dryrun.R` `I()` removed | `length_one_vectors` | test_dry_run_json_keeps_length_one_vectors_as_arrays |
| 19 | assembler refuses a non-array dry-run result | `semantic_checks` guard disabled | `not_an_array` | test_assembler_refuses_a_dry_run_result_that_is_not_an_array |
| 20 | `_BAD_TEXT` covers C1, U+2028/9, bidi | regex shrunk to `\x7f` | `forging_characters_in_paths` | test_refuse_forging_characters_in_paths[a\x85b] |
| 21 | ids printed via json.dumps | summary population line back to backticks | `population_ids_via_json` | test_summary_prints_population_ids_via_json_dumps |
| 22 | transitive R deps pinned (Py) | `POST_PINNED_PACKAGES` shrunk to the 8 old | `transitive_numeric` | test_transitive_numeric_dependencies_are_pinned |
| 23 | transitive R deps pinned (R mirror) | `R/post_gate.R` mirror shrunk | `transitive_numeric` | same test (R text check; parity with Py also by test_g5_post mirror test) |

Block tolerance (row 17): 3,000 blocks (~10 h at ~12 s) around the linear interpolation between the pinned event_block and merge_block timestamps; a date wrong by one day (~7,200 blocks) is refused,
interpolation error (a few hundred blocks) passes. Fixture values 15,304,565 / 15,523,155 still pass.
Residual (documented, not covered by the dry run): post panel reader stops, g0 provenance (pg_fail), the rematch cut-point/hash downgrade, malformed g0 receipts.
