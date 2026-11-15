# Crosswalk: internal labels in the frozen files

The signed and hashed files (`prespec/`, `addenda/`, the 35 files in `prespec/code_manifest.txt`) were written during the project and are shipped unchanged. They use the project's working labels. This page says what each kind means.

| Label | Example | Meaning |
|---|---|---|
| S + number | S10b, S24 | an item in the private project plan (S10b = signing; S24 = assembling the signing package) |
| g + number | g0, g1b, g5 | a pipeline gate in `gates/`; `run_all.py --gate <name>` runs it |
| H1, H2 | H1 | the pre-specified hypotheses (`prespec/prespec.md` §2-§3) |
| case 1-5 | case 3 | the decision cases for an interval against the threshold, coded in `R/decide.R` |
| A + number | A2, A7 | an amendment to the pre-specification made before signing (A1-A3), or a dated addendum after it (`addenda/`, `exploratory/*.addenda.md`) |
| O + number | O1-O4 | exploratory analyses planned after signing, each in a dated, hashed plan (`exploratory/`, `addenda/`); they never change the H1 decision |
| gen-M, gen-… | gen-M | one generation of output records from a frozen code state; `out/recovery/main/gen-M/summary.md` is the hash-bound record of the main baseline |
| `chg-` + 8 hex | chg-e5801bb3 | the id of one review run, used to find its report in the private project record |
| tribunal, verifier, refuter, tribune | "tribunal chg-5b19994e" | reviews run by AI models (several models in separate roles, reading the same frozen material and arguing in writing; a refuter tries to disprove one finding). They are not human referees. The author made or ratified every decision they informed |
| Fable, Opus, Sonnet, GPT, Grok | "the 21 Sep Fable decisions" | names of AI models used for drafting, coding and review |
| absolute paths | in `prespec/g5_config.json` | paths on the author's machine. They are part of the signed bytes and therefore stay; the release does not need them except for the signed post-event stage, which cannot be rerun elsewhere (see `PROVENANCE.md`) |
