# Provenance

This repository is a single release commit built from a private working repository. This file says what the release contains, which bytes were fixed before the post-event data were loaded, how to check them, and what those checks cannot show.

## The pre-specification

The analysis was pre-specified, signed and hashed privately on 28 October 2026, before any USDC or USDT transfer, balance or freeze data from after block 15,298,282 (7 August 2022, 23:59:57 UTC) were loaded. The signed package is `prespec/`:

| File | Role | SHA-256 |
|---|---|---|
| `prespec/prespec.md` | the pre-specification text | `e598ada76b03e7474be179e085ba8d6f2975602f975ecee8b70db6d0b80d07cc` |
| `prespec/manifest.json` | numerical manifest | `16eac9633cb0e627b0a4419a3b8daa0d6a43dc2bddf53023d72c0e3c061a1b6a` |
| `prespec/g5_config.json` | executable configuration of the post-event stage | `6341f75b8929b48bd54f0f6af1d01ccdf12b938c3ae901420577730550877e56` |
| `prespec/code_manifest.txt` | SHA-256 of the 35 code and config files the estimates depend on | `29cfe506b10ccf5377a29a45f997d300133edba93f95b22a83d4cb85902c4f88` |

`prespec/SHA256` lists these four hashes. Its own hash, `6bc01a5c9132beceec8289e015ef3393ed83b581074259d6c1e44e8cc1a6516e`, is recorded in `prespec/signing.json`.

**Check it (T0, no data, no network):** `python3 tools/verify_release.py`. It recomputes all four hashes and all 35 entries of the code manifest against the files in this tree.

What a pass shows: the files here are byte for byte the files that were hashed in the signing record. What it does not show: when they were hashed (the hashes are not externally timestamped as of the signing date; a later OpenTimestamps record proves only that they existed by its own date), and that the analysis is correct.

## Why some files look unfinished

The signed files were frozen as they were at signing and are shipped unchanged, because editing a single byte would break the hashes above.

- `prespec/prespec.md` line 1 reads "DRAFT (unsigned)" and line 7 has a blank signature line. The author signed the package through the signing record (`prespec/signing.json`, signed 28 October 2026) and the SHA-256 list, not by editing the text; the heading is the drafting heading the file carried when it was hashed.
- Several frozen files mention absolute paths on the author's machine, internal item labels (S10b, A3, O1) and review identifiers (`chg-xxxxxxxx`). `docs/CROSSWALK.md` explains each kind.

## What the release can and cannot reproduce

| Tier | What | Who can run it | Status |
|---|---|---|---|
| T0 | hash check of the signed files | anyone; no data | runs in CI |
| T1 | unit and fixture tests; Tables 1-4 and Figures 1-2 rebuilt from the shipped aggregates in `results/` | anyone; Python 3 and R 4.3.3 with the pinned packages (`install_r_packages.R`) | Python tests run in CI; the R tests run locally (164/164) because compiling the R packages in CI is slow |
| T2 | an unsigned rebuild of the chain from public Ethereum endpoints | anyone with time (about a day) | not yet run; numbers comparable, hashes not |

The signed post-event stage itself cannot be rerun outside the author's machine: its configuration contains absolute paths that are part of the signed bytes, and it refuses symlinks and environment overrides by design. The receipts of the extraction carry wall-clock times, so a rebuild cannot match them byte for byte.

## History

This public repository has its own short history: a 15 October 2026 construction snapshot (three commits and a dated notice) and this release commit. The development history stays private. Commit ids printed in the paper or in frozen files (for example `87c33de`, the signing commit of 28 October 2026 16:18 CET) refer to that private repository; a git bundle of it is available on request. Tests that check private history are skipped here with a reason. Four frozen scripts also check private commits when they run (`run_all.R` lines 64-65, `R/post_gate.R` line 243, `R/later_freeze.R` line 175, `tools/s24_package.py` lines 112-118): they ask git about the private commit history (for example, whether the commit that produced an input is an ancestor of the current commit) and stop when the answer does not match. In this public history those checks fail by design. They are part of the signed bytes and therefore stay as they are; they belong to the signed post-event stage, which cannot be rerun here anyway (see above).

## Data

No file in this release holds a wallet address of a studied user. The only addresses in the tree are public contracts (token, pool and issuer contracts), each listed with its name in the author's pre-release address check. Wallet-level data are not released; they can be regenerated from public Ethereum data by the code here.
