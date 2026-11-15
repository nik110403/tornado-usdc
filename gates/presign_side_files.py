"""S22 prep -- the four caller-supplied side files of `Rscript run_all.R --stage presign --population-id main`.

Offline and deterministic (no RPC, no chain-id call): reads only already-produced gate outputs from an explicit --input-dir
(g0/circle_events.csv, g1/cohort.csv, g1b/frame_survivors.csv, optionally g1b/receipts.jsonl) and the accepted baseline
blacklist generation (--baseline-blacklist-dir, verified by g0_baseline_blacklist.load_baseline for consumer `main`).
Every input is copied once into a private 0700 temp dir; only the copies are hashed and read (symlinks in any path component
from the input root down are refused). Writes, into a NEW/EMPTY --outdir strictly under out/recovery/presign_side/ (temp
sibling + one rename), together with side_manifest.json:

  direct_freeze.txt     `address` header + the 38 addresses with a Blacklisted event dated 2022-08-08 UTC in circle_events.csv
                        (prespec section 1; already-disclosed prior access, prespec section 0). Only rows with event_block <= block <= pinned circle_cluster_end are parsed (see direct_freeze).
  blacklist_any.txt     `address` header + addresses on the USDC or USDT blacklist at any time in the main baseline, with
                        EXACTLY the semantics of R `blacklisted_any_in_window` (R/blacklist.R); baseline generation only.
  candidates.csv        address,source_membership -- full pre-screen universe, mirroring g4_controls (exposed = cohort rows with
                        floor_pass == "True"; controls = frame minus exposed): a cohort address that is also a frame survivor is
                        `g1_<arm>` when floor_pass is True and `g1b_frame` when it is not; a cohort address not in the frame is
                        `g1_<arm>`; every other frame survivor is `g1b_frame`. cohort.csv must carry a floor_pass column.
  original_windows.csv  start_block,end_block -- the 30 g1b windows, recomputed via g1b_frame.original_windows; cross-checked
                        against g1b/receipts.jsonl tiling when that file exists.
"""
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict

from gates import g0_baseline_blacklist, g1b_frame
from lib import blocks, rpc

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

PRE_START_BLOCK = 14929560  # R/blacklist.R PRE_START_BLOCK
PRE_END_BLOCK = 15298282    # R/blacklist.R PRE_END_BLOCK
EVENT_BLOCK = 15302549  # config/blocks.json event_block (first block of the 2022-08-08 freeze window)
CLUSTER_END_BLOCK = 15302766  # config/blocks.json circle_cluster_end (end of the 2022-08-08 freeze cluster)
FREEZE_DATE = "2022-08-08"
FREEZE_N = 38
COINS = ("USDC", "USDT")
CIRCLE_COLS = ["address", "event", "block", "date", "tx_hash", "log_index"]
INPUT_FILES = (("g0", "circle_events.csv"), ("g1", "cohort.csv"), ("g1b", "frame_survivors.csv"))
MANIFEST = "side_manifest.json"
OUTPUTS = ("direct_freeze.txt", "blacklist_any.txt", "candidates.csv", "original_windows.csv")
PRODUCER_FILES = tuple(os.path.join(_ROOT, p) for p in (
    "gates/presign_side_files.py", "gates/g1b_frame.py", "gates/g0_baseline_blacklist.py", "lib/rpc.py", "lib/blocks.py", "lib/keccak.py", "run_all.py"))
OUT_ROOT = os.path.join(_ROOT, "out", "recovery", "presign_side")   # the only place an --outdir may live (tests relocate it)
_MAIN_ROOT = os.path.join(_ROOT, "out", "recovery", "main")
_JUNE_ROOT = os.path.join(_ROOT, "out", "recovery", "june")
_SOURCE_DIRS = tuple(os.path.join(_ROOT, d) for d in ("gates", "lib", "R", "config", "tests"))
_ADDR = re.compile(r"^0x[0-9a-f]{40}$")
_SOURCE_G1 = "g1_{}"
_SOURCE_G1B = "g1b_frame"


class SideFileError(RuntimeError):
    pass


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _data_rows(path):
    with open(path, "rb") as f:
        return max(sum(1 for _ in f) - 1, 0)


def _producer_dirty():
    """Producer files that differ from HEAD, are untracked, or are missing -- any of these makes the identity unreproducible."""
    bad = set()
    for p in PRODUCER_FILES:
        rel = os.path.relpath(p, _ROOT)
        if not os.path.isfile(p):
            bad.add(f"{rel} (missing)")
        elif subprocess.run(["git", "ls-files", "--error-unmatch", "--", rel], cwd=_ROOT, capture_output=True).returncode != 0:
            bad.add(f"{rel} (untracked)")
    out = subprocess.check_output(["git", "status", "--porcelain", "--", *PRODUCER_FILES], cwd=_ROOT, text=True)
    bad.update(ln[3:] for ln in out.splitlines())
    out = subprocess.check_output(["git", "diff", "--name-only", "HEAD", "--", *PRODUCER_FILES], cwd=_ROOT, text=True)
    bad.update(out.split())
    return sorted(bad)


def _addr(a, what):
    a = a.strip().lower()
    if not _ADDR.match(a):
        raise SideFileError(f"{what}: {a!r} is not a 0x + 40 hex address")
    return a


def _regular(path):
    if os.path.islink(path) or not os.path.isfile(path):
        raise SideFileError(f"{path} is missing, not a regular file, or a symlink")
    return path


def _no_symlinks_below(root, path):
    """Refuse a symlink at `root` or at any component between `root` and `path` (path must be under root)."""
    root, path = os.path.abspath(root), os.path.abspath(path)
    if os.path.commonpath([root, path]) != root:
        raise SideFileError(f"{path} is not under {root}")
    cur = root
    for comp in [""] + [c for c in os.path.relpath(path, root).split(os.sep) if c not in ("", ".")]:
        cur = os.path.join(cur, comp) if comp else cur
        if os.path.islink(cur):
            raise SideFileError(f"{cur} is a symlink; input path components must be real directories/files")


# --- 1. direct freeze ----------------------------------------------------------------------------------------------------

def direct_freeze(circle_events_csv, pinned=None, stats=None):
    """Addresses of Blacklisted events dated FREEZE_DATE, read only from rows with event_block <= block <= circle_cluster_end.

    circle_events.csv is sorted by (tx_hash, log_index), NOT by block, so it cannot be read up to a cutoff and stopped. Per data
    line ONLY the leading `address,event,block` prefix is split off (line.split(",", 3)) and ONLY the block column is interpreted
    first: it must be ASCII digits, else SideFileError (the message never echoes row content). A row outside [event_block, circle_cluster_end] (config/blocks.json) is skipped on that number alone: its
    address / event / date / tx_hash / log_index are never validated, interpreted or stored (the line's bytes are read from disk by
    the line iterator, nothing more). Only in-window rows get the quote check and full csv parse. `stats`, if given, is filled with
    rows_skipped_out_of_window / max_block_parsed / the window, for the side manifest."""
    pinned = pinned if pinned is not None else blocks.load_pinned()
    lo, bound = pinned["event_block"]["block"], pinned["circle_cluster_end"]["block"]
    out, skipped, max_blk = set(), 0, None
    with open(_regular(circle_events_csv), newline="") as f:
        header = next(csv.reader([f.readline()]), None)
        if header != CIRCLE_COLS:
            raise SideFileError(f"circle_events.csv header {header} != {CIRCLE_COLS}")
        for line in f:
            head = line.split(",", 3)
            if len(head) < 4:
                raise SideFileError("circle_events.csv: malformed row (cannot locate the block column)")
            tok = head[2]
            if not (tok and tok.isascii() and tok.isdigit()):
                raise SideFileError("circle_events.csv: non-integer block")
            if len(tok) > 19:
                raise SideFileError("circle_events.csv: block column too long")
            blk = int(tok)
            if blk < lo or blk > bound:
                skipped += 1
                continue   # outside [event_block, circle_cluster_end]: nothing but the block column is interpreted
            if '"' in head[0] or '"' in head[1]:
                raise SideFileError("circle_events.csv: malformed in-window row (quote before the block column)")
            max_blk = blk if max_blk is None else max(max_blk, blk)
            r = next(csv.DictReader([line], fieldnames=CIRCLE_COLS))
            if r["event"] == "Blacklisted" and r["date"] == FREEZE_DATE:
                out.add(_addr(r["address"], "circle_events.csv"))
    if stats is not None:
        stats.update({"direct_freeze_block_window": [lo, bound], "rows_skipped_out_of_window": skipped, "max_block_parsed": max_blk})
    if len(out) != FREEZE_N:
        raise SideFileError(f"expected exactly {FREEZE_N} addresses Blacklisted on {FREEZE_DATE}, found {len(out)}")
    return sorted(out)


# --- 2. blacklist at any time in the main baseline (R `blacklisted_any_in_window` semantics) ------------------------------

def blacklist_any(events, start=PRE_START_BLOCK, end=PRE_END_BLOCK):
    """events: {coin: {address: [(block, log_index, kind)]}}, kind in add|remove. Per coin: address is flagged iff the last
    event with block <= start (in (block, log_index) order) is an add, OR it has any add with block >= start (NOT > start:
    R uses >=, which differs from g0_baseline_blacklist.any_time when an add and a remove share block == start).
    Events with block > end are never used. Coins are replayed independently; the result is their union."""
    flagged = set()
    for coin in COINS:
        for a, evs in events.get(coin, {}).items():
            ev = sorted(e for e in evs if e[0] <= end)
            on = False
            for b, _, kind in ev:
                if b <= start:
                    on = kind == "add"
            if on or any(kind == "add" and b >= start for b, _, kind in ev):
                flagged.add(a.lower())
    return sorted(flagged)


# --- 3. candidates -------------------------------------------------------------------------------------------------------

def candidates(cohort_csv, survivors_csv):
    """-> (sorted [(address, label)], n_cohort, n_frame, overlap_floor_pass, overlap_non_floor). Same rule as g4_controls.run():
    exposed = cohort rows with floor_pass == "True"; a frame survivor that is exposed is labelled by its arm, a frame survivor
    that is a cohort row without floor_pass is a control (g1b_frame); cohort rows outside the frame keep g1_<arm>."""
    rows, floor = {}, {}
    with open(_regular(cohort_csv), newline="") as f:
        rdr = csv.DictReader(f)
        if not rdr.fieldnames or not {"address", "arm", "floor_pass"} <= set(rdr.fieldnames):
            raise SideFileError(f"cohort.csv needs address,arm,floor_pass columns, has {rdr.fieldnames}")
        for r in rdr:
            a = _addr(r["address"], "cohort.csv")
            if r["arm"] not in ("withdrawal", "deposit", "both"):
                raise SideFileError(f"cohort.csv: unknown arm {r['arm']!r} for {a}")
            if a in rows:
                raise SideFileError(f"cohort.csv: duplicate address {a}")
            rows[a] = _SOURCE_G1.format(r["arm"])
            floor[a] = r["floor_pass"] == "True"
    n_cohort = len(rows)
    seen, ov_floor, ov_non = set(), 0, 0
    with open(_regular(survivors_csv), newline="") as f:
        rdr = csv.DictReader(f)
        if not rdr.fieldnames or "address" not in rdr.fieldnames:
            raise SideFileError(f"frame_survivors.csv needs an address column, has {rdr.fieldnames}")
        for r in rdr:
            a = _addr(r["address"], "frame_survivors.csv")
            if a in seen:
                raise SideFileError(f"frame_survivors.csv: duplicate address {a}")
            seen.add(a)
            if a in rows:
                if floor[a]:       # exposed in g4 (floor_pass == "True"): the exposed label overrides the control label
                    ov_floor += 1
                    continue
                ov_non += 1        # not exposed in g4: a control, labelled by the frame
            rows[a] = _SOURCE_G1B
    return sorted(rows.items()), n_cohort, len(seen), ov_floor, ov_non


# --- 4. original windows -------------------------------------------------------------------------------------------------

def check_window_receipts(windows, receipts_jsonl):
    """Every window must be exactly tiled by g1b receipt spans (no gap, no overlap within the tiling). g1b draws its windows
    independently, so windows may overlap one another: a span belonging to a neighbouring window is ignored, and the tiling is
    searched among spans that start exactly where the previous one ended and end inside the window."""
    spans = set()
    with open(_regular(receipts_jsonl)) as f:
        for line in f:
            line = line.strip()
            if line:
                rec = json.loads(line)
                spans.add((rec["fromBlock"], rec["toBlock"]))
    by_start = defaultdict(list)
    for a, b in spans:
        by_start[a].append(b)
    for s, e in windows:
        reach, todo = {s}, [s]
        while todo:
            cur = todo.pop()
            for b in by_start.get(cur, ()):
                if b <= e and b + 1 not in reach:
                    reach.add(b + 1)
                    todo.append(b + 1)
        if e + 1 not in reach:
            raise SideFileError(f"window [{s},{e}]: no exact tiling by g1b receipt spans (recomputed window differs from the fetched one, or gap)")
    return f"ok: all {len(windows)} windows exactly tiled by g1b receipts"


# --- driver --------------------------------------------------------------------------------------------------------------

def _write_lines(path, header, lines):
    with open(path, "w", newline="") as f:
        f.write(header + "\n")
        for ln in lines:
            f.write(ln + "\n")
        f.flush()
        os.fsync(f.fileno())


def _shares(a, b):
    return os.path.commonpath([a, b]) in (a, b)


def _copy_nofollow(src, dest):
    """Copy src to dest from an fd opened with O_NOFOLLOW (a symlink swapped in after the check is refused)."""
    try:
        fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        raise SideFileError(f"cannot open {src} without following symlinks: {e}")
    with os.fdopen(fd, "rb") as fin, open(dest, "wb") as fout:
        shutil.copyfileobj(fin, fout)


def check_outdir(outdir, input_dir=None, baseline_dir=None):
    """Validate the RAW --outdir argument and return its absolute path. It must (a) not contain `..` or be a `current`
    pointer, (b) have no symlink component, (c) resolve strictly under OUT_ROOT (out/recovery/presign_side), (d) share no path
    with the input g0/g1/g1b subdirs (not the input root), the baseline dir, the main/june population roots or the source tree, (e) be absent or empty."""
    if ".." in outdir.split(os.sep):
        raise SideFileError(f"--outdir {outdir!r} must not contain ..")
    out = os.path.abspath(outdir)
    if os.path.basename(out) == "current" or "current" in out.split(os.sep):
        raise SideFileError(f"--outdir {outdir!r} must not go through a `current` pointer")
    p = out
    while p != os.path.dirname(p):
        if os.path.islink(p):
            raise SideFileError(f"--outdir path component {p} is a symlink")
        p = os.path.dirname(p)
    real, root = os.path.realpath(out), os.path.realpath(OUT_ROOT)
    if real == root or os.path.commonpath([real, root]) != root:
        raise SideFileError(f"--outdir {out} must be a new directory strictly under {OUT_ROOT}")
    in_subs = [] if input_dir is None else [os.path.join(input_dir, d) for d in ("g0", "g1", "g1b")]
    others = [*(("input dir", d) for d in in_subs), ("baseline dir", baseline_dir), ("main root", _MAIN_ROOT), ("june root", _JUNE_ROOT),
              *(("source tree", d) for d in _SOURCE_DIRS)]
    for what, d in others:
        if d is not None and _shares(real, os.path.realpath(d)):
            raise SideFileError(f"--outdir {out} shares a path with the {what} {d}")
    if os.path.lexists(out) and (not os.path.isdir(out) or os.listdir(out)):
        raise SideFileError(f"--outdir {out} exists and is not an empty directory; side files are immutable")
    return out


def produce(input_dir, baseline_dir, outdir, pinned=None, skip_window_receipts_check=False):
    pinned = pinned if pinned is not None else blocks.load_pinned()
    if pinned["pre_period_start"]["block"] != PRE_START_BLOCK or pinned["pre_period_end"]["block"] != PRE_END_BLOCK:
        raise SideFileError("pinned pre_period_start/end differ from the constants in R/blacklist.R; refusing")
    if pinned["event_block"]["block"] != EVENT_BLOCK:
        raise SideFileError(f"pinned event_block must be {EVENT_BLOCK}; refusing")
    if pinned["circle_cluster_end"]["block"] != CLUSTER_END_BLOCK or pinned["circle_cluster_end"]["block"] < pinned["event_block"]["block"]:
        raise SideFileError(f"pinned circle_cluster_end must be {CLUSTER_END_BLOCK} and >= event_block; refusing")
    out = check_outdir(outdir, input_dir, baseline_dir)
    identity = rpc.producer_identity(PRODUCER_FILES)
    head, _, src_hash = identity.partition(":")
    dirty = _producer_dirty()
    if dirty:
        raise SideFileError(f"producer sources differ from HEAD, are untracked or missing ({', '.join(dirty)}); refusing to write unreproducible side files")

    receipts = os.path.join(input_dir, "g1b", "receipts.jsonl")
    sources = {"g0/circle_events.csv": os.path.join(input_dir, "g0", "circle_events.csv"),
               "g1/cohort.csv": os.path.join(input_dir, "g1", "cohort.csv"),
               "g1b/frame_survivors.csv": os.path.join(input_dir, "g1b", "frame_survivors.csv")}
    if os.path.lexists(receipts):
        sources["g1b/receipts.jsonl"] = receipts
    elif not skip_window_receipts_check:
        raise SideFileError("g1b/receipts.jsonl is absent, so the original windows cannot be cross-checked; pass --skip-window-receipts-check to proceed without it")
    for k, p in sources.items():
        _no_symlinks_below(input_dir, p)
        _regular(p)
    bsources = {"baseline_blacklist/manifest.json": os.path.join(baseline_dir, "manifest.json"),
                "baseline_blacklist/events.csv": os.path.join(baseline_dir, "events.csv")}
    if os.path.lexists(os.path.join(baseline_dir, "receipts.jsonl")):
        bsources["baseline_blacklist/receipts.jsonl"] = os.path.join(baseline_dir, "receipts.jsonl")
    for k, p in bsources.items():
        _no_symlinks_below(baseline_dir, p)
        _regular(p)

    priv = tempfile.mkdtemp(prefix="presign-side-in-")   # mode 0700; every read below is from these copies
    try:
        inputs, in_hash = {}, {}
        for k, src in {**sources, **bsources}.items():
            dest = os.path.join(priv, k)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            _copy_nofollow(src, dest)
            inputs[k] = dest
            in_hash[k] = _sha256_file(dest)
        circle, cohort, surv = (inputs["g0/circle_events.csv"], inputs["g1/cohort.csv"], inputs["g1b/frame_survivors.csv"])
        bcopy = os.path.join(priv, "baseline_blacklist")

        fstats = {}
        freeze = direct_freeze(circle, pinned, fstats)
        base = g0_baseline_blacklist.load_baseline(bcopy, "main", pinned=pinned)  # verified; main cutoff = pre_period_end
        if base.manifest_sha256 != in_hash["baseline_blacklist/manifest.json"]:
            raise SideFileError("baseline manifest changed while it was being verified")
        bl = blacklist_any(base._ev)  # Baseline keeps the verified, cutoff-clipped events in `_ev`
        cands, n_cohort, n_surv, ov_floor, ov_non = candidates(cohort, surv)
        wins = g1b_frame.original_windows(pinned)
        if len(wins) != g1b_frame.N_WINDOWS or any(s < PRE_START_BLOCK or e > PRE_END_BLOCK for s, e in wins):
            raise SideFileError("recomputed original windows are not 30 windows inside the main baseline range")
        if "g1b/receipts.jsonl" in inputs:
            check = check_window_receipts(wins, inputs["g1b/receipts.jsonl"])
        else:
            check = "skipped: --skip-window-receipts-check (g1b/receipts.jsonl not present)"

        parent = os.path.dirname(out)
        os.makedirs(parent, exist_ok=True)   # check_outdir proved `out` lies strictly inside OUT_ROOT, so this only creates within it
        stage = tempfile.mkdtemp(dir=parent, prefix=os.path.basename(out) + ".tmp-")
        try:
            _write_lines(os.path.join(stage, "direct_freeze.txt"), "address", freeze)
            _write_lines(os.path.join(stage, "blacklist_any.txt"), "address", bl)
            _write_lines(os.path.join(stage, "candidates.csv"), "address,source_membership", [f"{a},{m}" for a, m in cands])
            _write_lines(os.path.join(stage, "original_windows.csv"), "start_block,end_block", [f"{s},{e}" for s, e in wins])
            if {k: _sha256_file(p) for k, p in inputs.items()} != in_hash:
                raise SideFileError("a private input copy changed during the run; refusing to publish")
            if rpc.producer_identity(PRODUCER_FILES) != identity or _producer_dirty():
                raise SideFileError("producer identity changed during the run; refusing to publish")
            man = {"producing_commit": head, "producer_source_sha256": src_hash,
                   "inputs": {k: {"sha256": in_hash[k], "rows": _data_rows(inputs[k])} for k in sorted(inputs)},
                   "baseline_manifest_sha256": in_hash["baseline_blacklist/manifest.json"],
                   "outputs": {n: {"sha256": _sha256_file(os.path.join(stage, n)), "rows": _data_rows(os.path.join(stage, n))} for n in OUTPUTS},
                   "pre_start_block": PRE_START_BLOCK, "pre_end_block": PRE_END_BLOCK, "freeze_date": FREEZE_DATE,
                   "source_membership_labels": {"cohort": "g1_<arm>", "frame": _SOURCE_G1B},
                   "counts": {"direct_freeze": len(freeze), "blacklist_any": len(bl), "candidates": len(cands),
                              "cohort": n_cohort, "frame_survivors": n_surv,
                              "cohort_frame_overlap_floor_pass": ov_floor, "cohort_frame_overlap_non_floor": ov_non,
                              "original_windows": len(wins)},
                   **fstats,
                   "window_receipts_check": check, "window_receipts_check_skipped": "g1b/receipts.jsonl" not in inputs}
            with open(os.path.join(stage, MANIFEST), "w") as f:
                json.dump(man, f, indent=2, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            if os.path.lexists(out):
                if os.listdir(out):
                    raise SideFileError(f"--outdir {out} was filled during the run")
                os.rmdir(out)
            os.rename(stage, out)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
    finally:
        shutil.rmtree(priv, ignore_errors=True)
    print(f"[presign_side] wrote {out}: freeze {len(freeze)}, blacklist_any {len(bl)}, candidates {len(cands)}, windows {len(wins)}; {check}")
    return man


def run(cfg):
    try:
        produce(cfg["input_root"], cfg["baseline_blacklist_dir"], cfg["output_dir"], pinned=cfg.get("blocks") or None,
                skip_window_receipts_check=cfg.get("skip_window_receipts_check", False))
    except (SideFileError, g0_baseline_blacklist.BaselineError) as e:
        print(f"[presign_side] FAILED: {e}", file=sys.stderr)
        return 1
    except Exception as e:   # fail closed without a traceback (which could echo input content)
        print(f"[presign_side] FAILED: unexpected {type(e).__name__}", file=sys.stderr)
        return 1
    return 0
