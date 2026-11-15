#!/usr/bin/env python3
"""Entry point: runs the gates in order, stops on first failure, exit code =
gate result.

`--gate none` runs nothing -- it only asserts chain id 1 and prints the
pinned blocks with their timestamps (provenance smoke test). `--gate all`
runs g0, g1, g1b, g1c, g2, g3 in order (g0ext, g0_baseline, g4 and g4_june are opt-in) and stops at the first non-zero return.

Recovery gates take explicit immutable routing (no `current` pointer, no out/ fallback):
  --gate g0_baseline --generation NAME
  --gate g4_june --generation NAME --baseline-blacklist-dir DIR --input-dir ROOT --participation-rule baseline_transfer_required|none   (ROOT holds g0/; outputs go to
      out/recovery/june/NAME/, which must not exist or be empty)
  --gate g4 --generation NAME --baseline-blacklist-dir DIR --input-dir ROOT   (main population; ROOT holds g0/, g1/, g1b/;
      outputs go to out/recovery/main/NAME/, new/empty only; population_manifest.json written last)
  --gate presign_side --input-dir ROOT --baseline-blacklist-dir DIR --outdir NEW   (offline, no RPC: writes the four presign side files +
      side_manifest.json into a new/empty NEW; ROOT holds g0/circle_events.csv, g1/cohort.csv, g1b/frame_survivors.csv [+ g1b/receipts.jsonl])
  --gate g5 --config PATH   (post-event extraction; FAIL-CLOSED: before any data/RPC the committed prespec/ signature, the signed executable
      config == PATH, the signed manifest and every signed input hash are verified, else exit 3 with nothing read or fetched; the
      receipts dir G4_OUT_DIR must be set to a g5-specific directory; opt-in, never part of --gate all)
  --gate g5_veto --config PATH --outdir NEW   (the separate §5.9 veto walk, ~60 requests: Circle Blacklisted + Tether AddedBlackList over (pre_end, post_end]
      on the walk endpoint, every range re-verified on the other endpoint, own receipts/lock; SAME fail-closed pre-access checks as g5 (Python AND R
      halves of the signed package, before any RPC); G4_OUT_DIR must be a work dir of its own, never g5's; opt-in, never part of --gate all.
      It can only VETO the g0-derived list in R, never add a wallet; see gates/g5_veto.py)
  --gate s24_package --outdir NEW ...   (offline, RPC-free: assembles the UNSIGNED S24 signing package; see tools/s24_package.py -- never signs)
  --gate h2_inputs --outdir NEW ...   (offline, RPC-free: deposit_counts.csv + usdc_pool_flag.csv from persisted chunks; see tools/h2_inputs.py)
  --gate wallet_map --covariates F --population-id ID --generation-id GEN --out NEW_FILE   (offline: projects a covariates.csv to the WALLET_MAP that s24_package --population takes; tools/wallet_map.py)
  --gate g1|all --participation-rule baseline_transfer_required|none   (unresolved design: required, no default, recorded)
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
from datetime import timedelta

from gates import g0_baseline_blacklist, g0_blacklist_ext, g0_manifest, g1_cohort, g1b_frame, g1c_funnel, g2_integrity, g3_mde, g4_controls, g4_june, g5_post, g5_veto, presign_side_files
from lib import blocks, rpc

GATES = [
    ("g0", g0_manifest),
    ("g0ext", g0_blacklist_ext),  # opt-in: one-off blacklist history deployment -> census_start-1 (mevblocker; R stage needs its CSV)
    ("g0_baseline", g0_baseline_blacklist),  # opt-in: baseline blacklist history, immutable generation (S15)
    ("g1", g1_cohort),
    ("g1b", g1b_frame),
    ("g1c", g1c_funnel),
    ("g2", g2_integrity),
    ("g3", g3_mde),
    ("g4", g4_controls),  # opt-in: not part of --gate all (multi-hour, resumable; see gates/g4_controls.py)
    ("g4_june", g4_june),  # opt-in: June-only placebo population (multi-hour, resumable; see gates/g4_june.py)
    ("presign_side", presign_side_files),  # opt-in, offline: S22 presign side files (see gates/presign_side_files.py)
]
ALL_GATES = [g for g in GATES if g[0] not in ("g0ext", "g0_baseline", "g4", "g4_june", "presign_side")]


def _print_provenance():
    chain_id = rpc.assert_chain_id("drpc")
    print(f"chain id {chain_id}")
    pinned = blocks.load_pinned()
    if not pinned:
        print("(no pinned blocks in config/blocks.json)")
        return
    for name in sorted(pinned):
        rec = pinned[name]
        print(f"{name}: block {rec['block']} @ {rec['timestamp']}")


_ROOT = os.path.dirname(os.path.abspath(__file__))
RECOVERY_JUNE_ROOT = os.path.join(_ROOT, "out", "recovery", "june")
RECOVERY_MAIN_ROOT = os.path.join(_ROOT, "out", "recovery", "main")  # distinct from the June root: populations never share a location
OUTPUT_ROOTS = {"g4_june": RECOVERY_JUNE_ROOT, "g4": RECOVERY_MAIN_ROOT}
_GENERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _validate_routing(parser, args):
    """Explicit immutable routing for the recovery gates; every violation is a parser error BEFORE any RPC."""
    routed = ("generation", "baseline_blacklist_dir", "input_dir")
    needs = {"g0_baseline": ("generation",), "g4_june": routed, "g4": routed,
             "presign_side": ("baseline_blacklist_dir", "input_dir", "outdir"), "g5": ("config",), "g5_veto": ("config", "outdir")}.get(args.gate, ())
    if args.gate in ("g1", "all", "g4_june"):
        if args.participation_rule is None:
            parser.error(f"--gate {args.gate} requires --participation-rule {{{','.join(g1_cohort.PARTICIPATION_RULES)}}} "
                         "(unresolved design decision: no default, recorded in the cohort schema and summary)")
    elif args.participation_rule is not None:
        parser.error(f"--participation-rule does not apply to --gate {args.gate}")
    given = {k for k in ("generation", "baseline_blacklist_dir", "input_dir", "outdir", "config") if getattr(args, k) is not None}
    for k in needs:
        if k not in given:
            parser.error(f"--gate {args.gate} requires --{k.replace('_', '-')} (no default, no `current` fallback)")
    for k in given - set(needs):
        parser.error(f"--{k.replace('_', '-')} does not apply to --gate {args.gate}")
    if args.generation is not None and (not _GENERATION_RE.match(args.generation) or args.generation == "current" or args.generation.endswith(".work")):
        parser.error(f"invalid --generation {args.generation!r}: letters/digits/._- only, not 'current', no .work suffix")
    for k in ("baseline_blacklist_dir", "input_dir"):
        d = getattr(args, k)
        if d is None:
            continue
        if os.path.basename(d.rstrip("/")) == "current" or os.path.islink(d.rstrip("/")):
            parser.error(f"--{k.replace('_', '-')} {d!r} is a `current` pointer/symlink; pass the immutable generation directory")
        if not os.path.isdir(d):
            parser.error(f"--{k.replace('_', '-')} {d!r} is not a directory")
    if args.gate == "presign_side":
        if not os.path.isfile(os.path.join(args.baseline_blacklist_dir, "manifest.json")):
            parser.error("--baseline-blacklist-dir has no manifest.json: not an accepted baseline generation")
        for d, f in presign_side_files.INPUT_FILES:
            if not os.path.isfile(os.path.join(args.input_dir, d, f)):
                parser.error(f"--input-dir must contain {d}/{f} (no out/ fallback)")
        try:
            presign_side_files.check_outdir(args.outdir, args.input_dir, args.baseline_blacklist_dir)   # raw argument: `..`, containment, empty
        except presign_side_files.SideFileError as e:
            parser.error(str(e))
    if args.gate in OUTPUT_ROOTS:
        if not os.path.isfile(os.path.join(args.baseline_blacklist_dir, "manifest.json")):
            parser.error("--baseline-blacklist-dir has no manifest.json: not an accepted baseline generation")
        out = os.path.join(OUTPUT_ROOTS[args.gate], args.generation)
        if os.path.lexists(out) and (os.path.islink(out) or not os.path.isdir(out) or os.listdir(out)):
            parser.error(f"output dir {out} exists and is not empty; generations are immutable (pick a new --generation)")
        if not os.path.isdir(os.path.join(args.input_dir, "g0")):
            parser.error("--input-dir must contain g0/ (manifest.csv, usdt_events.csv)")
        if args.gate == "g4":
            for d, f in MAIN_INPUT_FILES:
                if not os.path.isfile(os.path.join(args.input_dir, d, f)):
                    parser.error(f"--input-dir must contain {d}/{f} (the main population reads exactly these roots; no out/ fallback)")
            stale = _stale_g4_env()
            if stale:
                parser.error(f"environment {', '.join(stale)} must be unset for the routed main population (no env fallbacks)")


# --- main-population generation (g4 routed): immutable out/recovery/main/<NAME>/, manifest written last ---------------

MAIN_INPUT_FILES = (("g0", "usdt_events.csv"), ("g1", "cohort.csv"), ("g1b", "frame_survivors.csv"))
# every source whose behaviour shapes the published inputs/outputs (the g1/g1b producers of the inputs are bound by their hashes too)
MAIN_PRODUCER_FILES = tuple(os.path.join(_ROOT, p) for p in (
    "lib/rpc.py", "lib/keccak.py", "lib/blocks.py", "gates/g4_controls.py", "gates/g0_baseline_blacklist.py",
    "gates/g1_cohort.py", "gates/g1b_frame.py", "gates/g4_june.py", "run_all.py"))
# pinned blocks + endpoint caps shape every fetch: a .work resume under edited config is a different run
MAIN_CONFIG_FILES = tuple(os.path.join(_ROOT, "config", n) for n in ("blocks.json", "endpoints.json"))
MAIN_MANIFEST = "population_manifest.json"
MAIN_BINDING = "main_binding.json"  # in <output>.work: {producer identity, input hashes} the checkpoints were produced under


def _stale_g4_env():
    """Every G4_* variable (SAMPLE_CONTROLS, BASELINE_BLACKLIST_DIR, WALK_ENDPOINT, AUDIT_MAX, OUT_DIR, ...) silently changes
    g4_controls' behaviour or routing; the routed main population admits none of them."""
    return g4_june._stale_g4_env()


def _bind_work(work, identity, inputs):
    """Checkpoints in `work` are only valid for the producer identity + input hashes that wrote them (mirrors g4_june's
    check_meta / binding_ctx). First use writes the binding; a later resume under a different identity or inputs is refused;
    leftover checkpoints without a binding are refused too (cannot be attributed)."""
    want = {"producer_identity": identity, "inputs": dict(sorted(inputs.items())),
            "config": {os.path.basename(p): g4_controls._sha256_file(p) for p in MAIN_CONFIG_FILES}}
    path = os.path.join(work, MAIN_BINDING)
    if os.path.lexists(path):
        if os.path.islink(path):
            raise RuntimeError(f"{path} is a symlink; refusing to trust it")
        with open(path) as f:
            have = json.load(f)
        if have != want:
            raise RuntimeError(f"{path}: the .work checkpoints were produced under a different producer identity/inputs "
                               f"({have.get('producer_identity')} vs {identity}); move {work} aside to start fresh")
        return
    leftovers = sorted(n for n in os.listdir(work) if n not in (".lock", MAIN_BINDING))
    if leftovers:
        raise RuntimeError(f"{work} holds unbound checkpoints ({', '.join(leftovers[:5])}); cannot attribute them to this producer/inputs; "
                           "move it aside to start fresh")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(want, f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _main_inputs(root, baseline_dir=None):
    """sha256 of every input-root file the main g4 reads (+ the baseline manifest file when `baseline_dir` is given);
    missing/empty raises BEFORE any RPC."""
    out = {}
    for d, f in MAIN_INPUT_FILES:
        path = os.path.join(root, d, f)
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            raise RuntimeError(f"required main input {path} is missing or empty")
        out[f"{d}/{f}"] = g4_controls._sha256_file(path)
    if baseline_dir is not None:
        out["baseline_blacklist_manifest"] = g4_controls._sha256_file(os.path.join(baseline_dir, "manifest.json"))
    return out


def _producer_dirty():
    out = subprocess.check_output(["git", "diff", "--name-only", "HEAD", "--", *MAIN_PRODUCER_FILES], cwd=_ROOT, text=True)
    return out.split()


def _data_rows(path):
    with open(path) as f:
        return sum(1 for _ in f) - 1


def _publish_main(final_dir, stage, work_dir, inputs, cutoff, identity):
    """Same discipline as g4_june._publish for population_id `main`: identity re-checked, dirty producer refused, every
    other output hashed, population_manifest.json written LAST (atomic), then ONE rename onto the new/empty final dir."""
    head, _, src_hash = identity.partition(":")
    if not (re.fullmatch(r"[0-9a-f]{40}", head) and re.fullmatch(r"[0-9a-f]{16}", src_hash)):
        raise RuntimeError(f"cannot record producer identity: {identity!r}")
    now = rpc.producer_identity(MAIN_PRODUCER_FILES)
    if now != identity:
        raise RuntimeError(f"producer identity changed during the run ({identity} -> {now}); refusing to publish")
    dirty = _producer_dirty()
    if dirty:
        raise RuntimeError(f"producer sources differ from HEAD ({', '.join(dirty)}); refusing to publish an unreproducible generation")
    meta = os.path.join(work_dir, "meta.json")
    if not os.path.isfile(meta) or os.path.islink(meta):
        raise RuntimeError(f"{meta} missing: cannot bind the run's frame/inputs record")
    shutil.copyfile(meta, os.path.join(stage, "meta.json"))
    bl_path = os.path.join(stage, "baseline_blacklist.json")  # what g4_controls actually loaded (hash of the manifest it verified)
    if not os.path.isfile(bl_path):
        raise RuntimeError(f"{stage} lacks baseline_blacklist.json; the run did not use the routed baseline blacklist")
    with open(bl_path) as f:
        bl = json.load(f)
    if bl.get("source") != "cfg" or bl.get("manifest_sha256") != inputs.get("baseline_blacklist_manifest"):
        raise RuntimeError(f"baseline_blacklist.json (source {bl.get('source')!r}, manifest {bl.get('manifest_sha256')}) does not match the "
                           f"attested baseline manifest {inputs.get('baseline_blacklist_manifest')}; refusing to publish")
    walk_ep, replay_ep = g4_controls.assert_single_provider(work_dir)  # observed in the checkpoints, not copied from constants
    outputs = {n: g4_controls._sha256_file(os.path.join(stage, n)) for n in sorted(os.listdir(stage))
               if n != MAIN_MANIFEST and os.path.isfile(os.path.join(stage, n))}
    for n in ("covariates.csv", "wallet_coin_day_pre.csv"):
        if n not in outputs:
            raise RuntimeError(f"{stage} lacks {n}; refusing to publish")
    man = {"population_id": "main", "generation_id": os.path.basename(final_dir), "producing_commit": head,
           "producer_source_sha256": src_hash, "schema_version": g4_controls.SCHEMA_VERSION,
           "baseline_start_date": g4_controls.PRE_START_DATE.isoformat(),
           "baseline_end_date": (g4_controls.PRE_START_DATE + timedelta(days=g4_controls.N_DAYS - 1)).isoformat(),
           "walk_endpoint": walk_ep, "replay_endpoint": replay_ep,
           "excluded_dates": ["2022-08-08"], "cutoff_block": cutoff, "input_file_sha256": dict(sorted(inputs.items())),
           "output_file_sha256": outputs,
           "row_counts": {n: _data_rows(os.path.join(stage, n)) for n in ("covariates.csv", "wallet_coin_day_pre.csv")}}
    tmp = os.path.join(stage, MAIN_MANIFEST + ".tmp")
    with open(tmp, "w") as f:
        json.dump(man, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, os.path.join(stage, MAIN_MANIFEST))
    os.makedirs(os.path.dirname(final_dir), exist_ok=True)
    g4_june._check_target_free(final_dir)  # re-check right before the rename
    os.rename(stage, final_dir)


def run_g4_main(cfg):
    """Main population: g4_controls under explicit routing. Work files in <output_dir>.work; the finished generation
    (covariates/panel/schema/summary/baseline_blacklist/meta + population_manifest.json) appears by one rename."""
    final = os.path.abspath(cfg["output_dir"])
    if final.endswith(".work"):
        raise RuntimeError(f"output dir {final} must not end in .work (reserved for the work directory)")
    g4_june._check_target_free(final)
    work = final + ".work"
    if os.path.islink(work) or (os.path.lexists(work) and not os.path.isdir(work)):
        raise RuntimeError(f"{work} is a symlink or not a directory; refusing to use it as the work directory")
    stale = _stale_g4_env()
    if stale:
        raise RuntimeError(f"environment {', '.join(stale)} must be unset for the routed main population (no env fallbacks)")
    root = cfg["input_root"]
    bdir = cfg["baseline_blacklist_dir"]
    inputs = _main_inputs(root, bdir)
    base = g0_baseline_blacklist.load_baseline(bdir, "main", pinned=cfg["blocks"])  # verified before any RPC
    if base.manifest_sha256 != inputs["baseline_blacklist_manifest"]:
        raise RuntimeError("baseline manifest changed while it was being verified; refusing to start")
    cutoff = cfg["blocks"]["pre_period_end"]["block"]
    identity = rpc.producer_identity(MAIN_PRODUCER_FILES)  # captured ONCE; re-checked at publish
    os.makedirs(work, exist_ok=True)
    lock = g4_june.acquire_run_lock(work)
    saved = (g4_controls._G1_DIR, g4_controls._G1B_DIR)
    try:
        _bind_work(work, identity, inputs)  # refuses a .work resume made under another producer identity / other inputs
        g4_controls._G1_DIR, g4_controls._G1B_DIR = os.path.join(root, "g1"), os.path.join(root, "g1b")
        with g4_june._retarget("g4_main", work, root):
            rc = g4_controls.run({**cfg, "baseline_blacklist_dir": os.path.abspath(cfg["baseline_blacklist_dir"])})
        if rc != 0:
            return rc
        stage = os.path.join(work, os.readlink(os.path.join(work, "current")))
        if _main_inputs(root, bdir) != inputs:  # inputs were read in place for hours: re-hash before attesting them
            raise RuntimeError("an input file or the baseline manifest changed during the run; refusing to publish")
        _publish_main(final, stage, work, inputs, cutoff, identity)
        os.unlink(os.path.join(work, "current"))
        print(f"[g4_main] published {final}")
        return 0
    finally:
        g4_controls._G1_DIR, g4_controls._G1B_DIR = saved
        lock.close()


def run_g5(config_path):
    """Post gate, deny by default. Exit codes: 0 ok; 3 DENIED before any post data was read or any RPC was made (signature, signed
    config/code/inputs, binding, routing); 4 FAILED after data access or RPC started (nothing is published; the traceback goes to stderr)."""
    if not os.path.isfile(config_path):
        print(f"g5 DENIED (fail-closed, no post data read, no RPC): config {config_path!r} is not a file")
        return 3
    try:
        path, n, sha = g5_post.run(config_path)
    except Exception as e:  # every failure gets a defined exit code; the message says truthfully which phase it was
        if getattr(e, "pre_access", False) or isinstance(e, g5_post.PreAccessDenied):
            if getattr(e, "checkpoint_parsed", False):   # a resume: an earlier run's checkpoint (post data) was already read; only "no RPC" is true
                print(f"g5 DENIED (fail-closed, no RPC made by this run; an earlier run's checkpoint was already parsed): {type(e).__name__}: {e}")
            else:
                print(f"g5 DENIED (fail-closed, no post data read, no RPC): {type(e).__name__}: {e}")
            return 3
        traceback.print_exc()
        print(f"g5 FAILED after data access / RPC began (nothing published): {type(e).__name__}: {e}")
        return 4
    print(f"[g5] wrote {path} ({n} rows, sha256 {sha})")
    return 0


def run_g5_veto(config_path, outdir):
    """§5.9 veto walk, deny by default; same exit codes as run_g5 (3 = denied before any RPC, 4 = failed after RPC began, nothing published)."""
    if not os.path.isfile(config_path):
        print(f"g5_veto DENIED (fail-closed, no post data read, no RPC): config {config_path!r} is not a file")
        return 3
    try:
        path, n, sha = g5_veto.run(config_path, outdir)
    except Exception as e:
        if getattr(e, "pre_access", False) or isinstance(e, g5_post.PreAccessDenied):
            print(f"g5_veto DENIED (fail-closed, no post data read, no RPC): {type(e).__name__}: {e}")
            return 3
        traceback.print_exc()
        print(f"g5_veto FAILED after RPC began (nothing published): {type(e).__name__}: {e}")
        return 4
    print(f"[g5_veto] wrote {path} ({n} raw logs, veto_logs.csv sha256 {sha})")
    return 0


def main(argv=None):
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--gate")
    known, rest = pre.parse_known_args(argv)
    if known.gate == "s24_package":   # offline package assembler (tools/s24_package.py): own argument set; lazy import keeps it out of the signed module set
        from tools import s24_package
        return s24_package.main(rest)
    if known.gate == "h2_inputs":   # offline H2 / USDC-pool input producer (tools/h2_inputs.py): own argument set, no RPC
        from tools import h2_inputs
        return h2_inputs.main(rest)
    if known.gate == "wallet_map":   # offline covariates.csv -> WALLET_MAP projection (tools/wallet_map.py), input for s24_package --population
        from tools import wallet_map
        return wallet_map.main(rest)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gate",
        required=True,
        choices=["none", "g0", "g0ext", "g0_baseline", "g1", "g1b", "g1c", "g2", "g3", "g4", "g4_june", "presign_side", "g5", "g5_veto", "s24_package", "h2_inputs", "wallet_map", "all"],
        help="which gate to run (or 'none' for a provenance smoke test, 'all' to run every gate in order)",
    )
    parser.add_argument("--smoke", action="store_true",
                        help="g4_june only: 2 frame windows + a small exposed sample, written to the --generation dir, manifest population_id june_placebo_smoke")
    parser.add_argument("--generation", help="g0_baseline / g4_june / g4: immutable generation name (output routing)")
    parser.add_argument("--baseline-blacklist-dir", help="g4_june / g4: accepted out/recovery/blacklist_baseline/<generation> directory")
    parser.add_argument("--input-dir", help="g4_june: read-only input root holding g0/ (manifest.csv, usdt_events.csv); g4 additionally g1/cohort.csv, g1b/frame_survivors.csv")
    parser.add_argument("--outdir", help="g5_veto: NEW/empty output directory (disjoint from G4_OUT_DIR); presign_side: NEW/empty directory for direct_freeze.txt, blacklist_any.txt, candidates.csv, original_windows.csv, side_manifest.json")
    parser.add_argument("--config", help="g5: the signed executable config (must be byte-identical to the one named in prespec/signing.json)")
    parser.add_argument("--skip-window-receipts-check", action="store_true",
                        help="presign_side only: proceed when g1b/receipts.jsonl is absent (recorded in side_manifest.json); default is to fail")
    parser.add_argument("--participation-rule", choices=g1_cohort.PARTICIPATION_RULES,
                        help="g1 / all / g4_june: baseline-Transfer participation rule (unresolved; required, no default)")
    args = parser.parse_args(argv)
    _validate_routing(parser, args)
    if args.skip_window_receipts_check and args.gate != "presign_side":
        parser.error("--skip-window-receipts-check applies to --gate presign_side only")
    if args.smoke and args.gate != "g4_june":
        parser.error("--smoke applies to --gate g4_june only")

    if args.gate == "none":
        _print_provenance()
        return 0

    if args.gate == "presign_side":  # offline: reads finished gate outputs only, never touches the chain
        return presign_side_files.run({"blocks": blocks.load_pinned(), "input_root": os.path.abspath(args.input_dir),
                                       "baseline_blacklist_dir": os.path.abspath(args.baseline_blacklist_dir), "output_dir": os.path.abspath(args.outdir),
                                       "skip_window_receipts_check": args.skip_window_receipts_check})

    if args.gate == "g5":  # BEFORE any chain-id call: g5_post.run verifies signature + config + input hashes first, then touches data/RPC
        return run_g5(args.config)

    if args.gate == "g5_veto":  # same as g5: verification (signature, config, code, inputs) BEFORE the first RPC
        return run_g5_veto(args.config, os.path.abspath(args.outdir))

    rpc.assert_chain_id("drpc")
    cfg = {"blocks": blocks.load_pinned(), "endpoints": rpc.ENDPOINTS, "smoke": args.smoke}
    if args.generation:
        cfg["generation"] = args.generation
    if args.participation_rule:
        cfg["participation_rule"] = args.participation_rule
    if args.gate in OUTPUT_ROOTS:
        cfg["output_dir"] = os.path.join(OUTPUT_ROOTS[args.gate], args.generation)
        cfg["input_root"] = os.path.abspath(args.input_dir)
        cfg["baseline_blacklist_dir"] = os.path.abspath(args.baseline_blacklist_dir)

    if args.gate == "all":
        to_run = ALL_GATES
    else:
        to_run = [(name, mod) for name, mod in GATES if name == args.gate]

    for name, mod in to_run:
        print(f"=== gate {name} ===")
        result = run_g4_main(cfg) if name == "g4" else mod.run(cfg)
        if result != 0:
            print(f"gate {name} FAILED (exit {result})")
            return result
    return 0


if __name__ == "__main__":
    sys.exit(main())
