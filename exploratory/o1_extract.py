#!/usr/bin/env python3
"""O1 extraction -- extended pre-period 1 March .. 8 June 2022 (PLAN_o1_o2_2026-10-28.md section 2.2, final text).

EXPLORATORY, POST-RESULTS, dated 2026-10-28; not confirmatory; does not reclassify H1.

New unbound code: nothing signed-bound is edited. The balance machinery is IMPORTED from gates/g4_controls.py
(WALK_ARRAY, GapError via lib.rpc, the batched-post / windowed-getLogs helpers, strict log-shape checks, digests).
g4's checkpoint and receipt writers are NOT used (they write under out/g4); this file has its own, rooted in --out.

Rules (final plan 2.2):
  R9   There is no signed 8-Jun opening balance. The identity ties the walk to the signed pre-panel day_index 0
       (= end of 9 Jun) balances, so the walk runs from pin+1 to the last block of 9 Jun (--day0-block), 9-Jun flows included.
       Opening balance = balanceOf at the pinned last block before 2022-03-01 00:00:00 UTC (--pin-block, timestamp-verified).
  R12  Complete walk: both pin balances read and every Transfer / USDT DestroyedBlackFunds log range pin+1..day0 of every
       unit touching the wallet fetched without a GapError, on the primary endpoint or, after ONE retry, on the second
       endpoint (the retry is per unit: the whole pin+1..day0 range of that unit). DestroyedBlackFunds is fetched as log
       ranges (topic-only; the event has no indexed address), under the same rules; one failed range makes every wallet
       incomplete. Mismatch (identity inequality, negative reconstructed balance, absent signed day-0 row) is excluded and
       counted by arm; the run stops if mismatches exceed --max-mismatch-frac (default 0.005) of the COMPLETE wallets of
       EITHER arm. Failed archive reads are missing, never zero; a missing pin balance for EITHER coin -> `missing`.
  R10  Re-verify: seeded (808) 5% sample within arm of the ok wallets, re-read on the OTHER endpoint than the one that
       produced each record: both pin balances, the day-0 balance (vs the signed panel), the full log ranges (count +
       sha256 per wallet/coin/position, like g4 _reverify) and the DERIVED daily path (pin balance + flows + destroyed,
       all from the second endpoint) against the primary path. A wallet that differs gets a missing path (`missing`,
       reason reverify); the run stops if more than --max-reverify-frac (default 0.01) of the re-verified wallets of
       either arm differ. Zero tolerance.

Day convention: end-of-UTC-day state, day_index 0 = 2022-06-09 (as gates/g5_post.py); the O1 panel covers day_index
-100 (1 Mar) .. -1 (8 Jun). Day 0 is used for the identity only (the signed panel already holds it).

INTEGRITY: this module never reads out/ by itself except the three input paths you pass in (membership, signed pre
panel + covariates for day 0). Outputs go only to --out under out/exploratory/<dir> (or outside out/), same guard
semantics as o3_pretrend.R (repo root realpath'd). One acquisition slot: a single process, an flock on <out>/.slot.lock,
a resumable hash-checked jsonl checkpoint. Outputs are written temp-file + fsync + os.replace and opened O_NOFOLLOW; a
resume refuses an outdir holding a symlink.

Usage:
  python3 exploratory/o1_extract.py resolve-blocks --endpoint mevblocker          # non-sample RPC only
  python3 exploratory/o1_extract.py run --membership M.csv --signed-cov C.csv --signed-panel P.csv \\
      --pin-block N --day0-block N --out out/exploratory/o1-YYYY-MM-DD [--resume] [--max-mismatch-frac 0.005]
"""
import argparse
import contextlib
import csv
import fcntl
import hashlib
import json
import math
import os
import random
import re
import ssl
import sys
import threading
import time
import urllib.error
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from gates import g1_cohort as g1  # noqa: E402
from gates import g4_controls as g4  # noqa: E402
from lib import keccak, rpc  # noqa: E402

EXPLORATORY_DATE = "2026-10-28"
BANNER = f"EXPLORATORY, POST-RESULTS, dated {EXPLORATORY_DATE}; not confirmatory; does not reclassify H1"
SCHEMA = 2
COINS = g4.COINS
DAY0_DATE = date(2022, 6, 9)                 # day_index 0
FIRST_DATE = date(2022, 3, 1)
FIRST_DAY = (FIRST_DATE - DAY0_DATE).days    # -100
LAST_PANEL_DAY = -1                          # 8 Jun
IDENT_DAY = 0                                # end of 9 Jun: signed pre-panel day_index 0
WALK_ARRAY = g4.WALK_ARRAY                   # 1,000-address unit identity, imported
OPEN_BATCH = g4.BALANCE_BATCH
BALANCE_PACING = {"tenderly": (5, 1.0)}      # (calls per JSON-RPC batch, pause s): public gateway 429s every batch of 6+, 5 at 1 s sustained (probe 2026-10-30)
REVERIFY_SEED = 808                          # plan 2.2
REVERIFY_FRACTION = 0.05                     # plan 2.2: 5% within arm
MAX_MISMATCH_FRAC = 0.005                    # plan 2.2: mismatches > 0.5% of complete wallets in either arm stop the run
MAX_REVERIFY_FRAC = 0.01                     # plan 2.2: > 1% of re-verified wallets differing in either arm stops the run
EMPTY_DIGEST = g4._digest([], with_block=True)
DESTROYED_TOPIC = keccak.topic("DestroyedBlackFunds(address,uint256)")
DESTROYED_KEY = "destroyed"
_ADDR = re.compile(r"0x[0-9a-f]{40}")   # used with fullmatch: `$` would also accept a trailing newline
_HEX = re.compile(r"0x[0-9a-fA-F]+")
_HASH = re.compile(r"0x[0-9a-fA-F]{64}")
_DESTROYED_DATA = re.compile(r"0x0{24}[0-9a-fA-F]{104}")
PRODUCER_FILES = [os.path.join(REPO, *p.split("/")) for p in
                  ("exploratory/o1_extract.py", "gates/g4_controls.py", "gates/g1_cohort.py", "lib/rpc.py", "lib/keccak.py")]


class Abort(RuntimeError):
    """The run stops (mismatches over threshold, reverify failure, pin check failure)."""


# ---------------------------------------------------------------------------------------------------------------
# output guard (same semantics as exploratory/o3_pretrend.R) + file primitives
# ---------------------------------------------------------------------------------------------------------------

def assert_producers_clean(files, repo):
    """The producer files that stamp run_params.json must equal HEAD: `git status --porcelain` empty for them."""
    import subprocess
    r = subprocess.run(["git", "status", "--porcelain", "--"] + [os.path.relpath(f, repo) for f in files], cwd=repo,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise Abort(f"cannot check producer files with git: {r.stderr.strip()[:200]}")
    if r.stdout.strip():
        raise Abort(f"producer files have uncommitted changes (commit first, the producer hash must name committed code):\n{r.stdout}")


def check_resume_producer(stored, current, files, repo):
    """Resume: the producer identity is '<git HEAD>:<16-hex hash of producer files>'. HEAD may move (autopilot commits of
    unrelated files) but the file-hash half must be equal and no producer file may differ between the stored and the
    current HEAD. Returns True if HEAD moved (caller logs the resume), False if identical; raises Abort otherwise."""
    import subprocess
    if stored == current:
        return False
    m = re.fullmatch(r"([0-9a-f]{40}):([0-9a-f]{16})", stored if isinstance(stored, str) else "")
    n = re.fullmatch(r"([0-9a-f]{40}):([0-9a-f]{16})", current if isinstance(current, str) else "")
    if not (m and n) or m.group(2) != n.group(2):
        raise Abort("run_params.json differs from this invocation: refusing to resume a different run (producer files differ)")
    rel = [os.path.relpath(f, repo) for f in files]
    r = subprocess.run(["git", "cat-file", "-e", m.group(1) + "^{commit}"], cwd=repo, capture_output=True, text=True)
    if r.returncode != 0:
        raise Abort(f"stored producer commit {m.group(1)} is unknown to this repo: refusing to resume")
    r = subprocess.run(["git", "merge-base", "--is-ancestor", m.group(1), n.group(1)], cwd=repo, capture_output=True, text=True)
    if r.returncode != 0:
        raise Abort(f"stored producer commit {m.group(1)} is not an ancestor of HEAD {n.group(1)}: refusing to resume")
    r = subprocess.run(["git", "diff", "--quiet", m.group(1), n.group(1), "--"] + rel, cwd=repo, capture_output=True, text=True)
    if r.returncode != 0:
        raise Abort("producer files changed between the stored commit and HEAD: refusing to resume a different run")
    return True


def lex_clean(p, cwd=None):
    p = os.path.expanduser(p)
    if not p.startswith("/"):
        p = os.path.join(cwd or os.getcwd(), p)
    out = []
    for q in [s for s in p.split("/") if s]:
        if q == "..":
            out = out[:-1]
        elif q != ".":
            out.append(q)
    return "/" + "/".join(out)


def out_guard(p, repo=REPO):
    repo = os.path.realpath(repo)                      # '.' / symlinked roots must not blind the guard
    out_root = os.path.join(repo, "out")
    inside_out = p == out_root or p.startswith(out_root + "/")
    allowed = p.startswith(os.path.join(out_root, "exploratory") + "/")
    if re.search(r"(^|/)out/recovery(/|$)", p) or (inside_out and not allowed):
        raise Abort("refusing to write under out/ except out/exploratory/<dir> (never into out/recovery/)")


def refuse_symlinks(outdir):
    if os.path.islink(outdir):
        raise Abort(f"outdir is a symlink: {outdir}")
    if os.path.isdir(outdir):
        bad = sorted(n for n in os.listdir(outdir) if os.path.islink(os.path.join(outdir, n)))
        if bad:
            raise Abort(f"outdir holds symlink entries (refusing to resume/write): {bad}")


def prepare_outdir(arg, repo=REPO, resume=False):
    """Lexically clean, guard, symlink-resolve the deepest existing ancestor, guard again, create, guard the realpath.
    New-or-empty unless resume=True (then an existing dir must carry our run_params.json and no symlink entries)."""
    repo = os.path.realpath(repo)
    outdir = lex_clean(arg)
    out_guard(outdir, repo)
    exists_nonempty = os.path.isdir(outdir) and bool(os.listdir(outdir))
    if exists_nonempty and not (resume and os.path.isfile(os.path.join(outdir, "run_params.json"))):
        raise Abort(f"outdir must be new or empty (or --resume of an O1 run): {outdir}")
    if exists_nonempty:
        refuse_symlinks(outdir)
    a, rest = outdir, []
    while not os.path.exists(a) and a != "/":
        rest.insert(0, os.path.basename(a))
        a = os.path.dirname(a)
    out_guard(os.path.join(os.path.realpath(a), *rest), repo)
    os.makedirs(outdir, exist_ok=True)
    real = os.path.realpath(outdir)
    out_guard(real, repo)
    return real


def _open_fd(path, flags, mode=0o644):
    return os.open(path, flags | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), mode)


def _fsync_dir(d):
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def atomic_text(path):
    """Text writer to a temp file in the same directory; fsync + os.replace (+ dir fsync) on success; temp removed on failure."""
    d, n = os.path.split(path)
    tmp = os.path.join(d, f".{n}.tmp{os.getpid()}")
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp)
    f = os.fdopen(_open_fd(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "w", newline="")
    try:
        yield f
        f.flush()
        os.fsync(f.fileno())
        f.close()
        os.replace(tmp, path)
        _fsync_dir(d)
    except BaseException:
        with contextlib.suppress(Exception):
            f.close()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _read_bytes(path):
    fd = _open_fd(path, os.O_RDONLY)
    with os.fdopen(fd, "rb") as f:
        return f.read()


# ---------------------------------------------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------------------------------------------

def day_of_ts(ts):
    return (datetime.fromtimestamp(int(ts), tz=timezone.utc).date() - DAY0_DATE).days


def date_of_day(d):
    return (DAY0_DATE + timedelta(days=d)).isoformat()


def _ts_utc(d, h=0):
    return int(datetime(d.year, d.month, d.day, h, tzinfo=timezone.utc).timestamp())


def last_block_before(ts_target, get_ts, lo, hi):
    """Last block b in [lo, hi] with timestamp(b) < ts_target (timestamps non-decreasing). Bisection; non-sample RPC only."""
    if get_ts(lo) >= ts_target:
        raise Abort(f"block {lo} already at/after the target timestamp")
    if get_ts(hi) < ts_target:
        raise Abort(f"block {hi} is still before the target timestamp: raise hi")
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if get_ts(mid) < ts_target:
            lo = mid
        else:
            hi = mid - 1
    return lo


def check_pin_blocks(get_ts, pin, day0):
    """pin = last block before 2022-03-01 00:00:00 UTC; day0 = last block of 2022-06-09 (UTC). Returns the four timestamps."""
    t_pin, t_pin1, t_d0, t_d01 = get_ts(pin), get_ts(pin + 1), get_ts(day0), get_ts(day0 + 1)
    if not (t_pin < _ts_utc(FIRST_DATE) <= t_pin1):
        raise Abort(f"pin block {pin} is not the last block before 2022-03-01 00:00 UTC (ts {t_pin}, next {t_pin1})")
    end9 = _ts_utc(DAY0_DATE + timedelta(days=1))
    if not (t_d0 < end9 <= t_d01):
        raise Abort(f"day-0 block {day0} is not the last block of 2022-06-09 UTC (ts {t_d0}, next {t_d01})")
    return {"pin": t_pin, "pin+1": t_pin1, "day0": t_d0, "day0+1": t_d01}


def _hexint(x, what="field"):
    """Strict 0x-hex quantity (no whitespace, underscores, sign or bare digits, which int(x, 16) would accept)."""
    if not (isinstance(x, str) and _HEX.fullmatch(x)):
        raise AssertionError(f"{what} is not strict 0x-hex: {x!r}")
    return int(x, 16)


_RUN_BLOCKS = None     # run-wide {blockNumber: blockHash}, set by run_extraction (None = per-call check only)


def check_log_fields(logs):
    """Per log: strict hex blockNumber / blockTimestamp / logIndex, 32-byte blockHash, not a reorged (removed) log;
    across logs: exactly one blockHash per blockNumber."""
    by_blk = {}
    for l in logs:
        if l.get("removed") not in (None, False):
            raise AssertionError(f"removed log returned: tx={l.get('transactionHash')}")
        blk = _hexint(l.get("blockNumber"), "blockNumber")
        _hexint(l.get("blockTimestamp"), "blockTimestamp")
        _hexint(l.get("logIndex"), "logIndex")
        bh = l.get("blockHash")
        if not (isinstance(bh, str) and _HASH.fullmatch(bh)):
            raise AssertionError(f"blockHash is not a 32-byte hex: {bh!r}")
        if by_blk.setdefault(blk, bh.lower()) != bh.lower():
            raise AssertionError(f"block {blk} carries two different blockHashes")
        if _RUN_BLOCKS is not None and _RUN_BLOCKS.setdefault(blk, bh.lower()) != bh.lower():
            raise AssertionError(f"block {blk} carries two different blockHashes across units/endpoints of this run")


def _split_log(log, pos):
    """-> (wallet, other, sign, value) for the leg this unit position credits."""
    frm, to, value = g1._transfer_decode(log)
    return (frm, to, -1, value) if pos == "from" else (to, frm, 1, value)


def group_logs_by_wallet(logs, pos):
    """Strict shape checks + per-wallet log lists (a log belongs to exactly one wallet per position)."""
    check_log_fields(logs)
    g4.check_block_timestamps(logs)
    out = defaultdict(list)
    for l in logs:
        g4._strict_transfer_shape(l)
        w, _, _, _ = _split_log(l, pos)
        out[w].append(l)
    return out


def wallet_day_flows(wl, pos):
    """One wallet's logs -> {day: signed delta}; asserts every day is inside the O1 window."""
    days = defaultdict(int)
    for l in wl:
        _, _, sign, value = _split_log(l, pos)
        d = day_of_ts(_hexint(l["blockTimestamp"], "blockTimestamp"))
        if not (FIRST_DAY <= d <= IDENT_DAY):
            raise AssertionError(f"log day {d} outside the O1 window [{FIRST_DAY}, {IDENT_DAY}]: tx={l.get('transactionHash')}")
        days[d] += sign * value
    return dict(days)


def _check_unique_in_range(logs, pin, day0):
    uniq = set()
    for l in logs:
        k = (l.get("blockHash"), l.get("logIndex"))
        if k[0] is None or k[1] is None or k in uniq:
            raise AssertionError(f"missing or duplicate (blockHash, logIndex) {k}")
        uniq.add(k)
        blk = _hexint(l.get("blockNumber"), "blockNumber")
        if not (pin < blk <= day0):
            raise g4.PostPeriodRead(f"log block {blk} outside ({pin}, {day0}]")


def decode_unit(logs, pos, addr_set, pin, day0, token):
    """One unit's logs -> {addr: [[(day, delta), ...], n_logs, digest]} for wallets with >= 1 log.
    Same assertions as g4.decode_unit_logs (this token's 3-topic Transfer, unique (blockHash, logIndex), block in
    (pin, day0], tracked address in the right slot) plus strict hex fields, no removed logs, one blockHash per block,
    with the O1 day window FIRST_DAY..0."""
    token = token.lower()
    check_log_fields(logs)
    for l in logs:
        if str(l.get("address", "")).lower() != token:
            raise AssertionError(f"log from {l.get('address')}, expected token {token}")
        topics = l.get("topics") or []
        if len(topics) != 3 or str(topics[0]).lower() != g4.TRANSFER_TOPIC:
            raise AssertionError(f"not a 3-topic Transfer log: tx={l.get('transactionHash')}")
    _check_unique_in_range(logs, pin, day0)
    grouped = group_logs_by_wallet(logs, pos)
    out = {}
    for w, wl in grouped.items():
        if w not in addr_set:
            raise AssertionError(f"endpoint returned a log for untracked address {w}")
        out[w] = [sorted(wallet_day_flows(wl, pos).items()), len(wl), g4._digest(wl, with_block=True)]
    return out


def decode_destroyed(logs, addr_set, pin, day0):
    """USDT DestroyedBlackFunds(address,uint256) logs (topic-only query) -> {addr: [[(day, amount), ...], n, digest]} for the
    tracked wallets. Same strictness as decode_unit; the address is in the data word (the event has no indexed field)."""
    check_log_fields(logs)
    usdt = COINS["USDT"].lower()
    for l in logs:
        topics, data = l.get("topics") or [], l.get("data")
        if len(topics) != 1 or str(topics[0]).lower() != DESTROYED_TOPIC or str(l.get("address", "")).lower() != usdt:
            raise AssertionError(f"not a USDT DestroyedBlackFunds log: tx={l.get('transactionHash')}")
        if not (isinstance(data, str) and _DESTROYED_DATA.fullmatch(data)):
            raise AssertionError(f"DestroyedBlackFunds data is not 64 bytes: tx={l.get('transactionHash')}")
    _check_unique_in_range(logs, pin, day0)
    g4.check_block_timestamps(logs)
    per = defaultdict(list)
    for l in logs:
        addr = "0x" + l["data"][26:66].lower()
        if addr in addr_set:
            per[addr].append(l)
    out = {}
    for a, wl in per.items():
        days = defaultdict(int)
        for l in wl:
            d = day_of_ts(_hexint(l["blockTimestamp"], "blockTimestamp"))
            if not (FIRST_DAY <= d <= IDENT_DAY):
                raise AssertionError(f"DestroyedBlackFunds day {d} outside the O1 window")
            days[d] += int(l["data"][66:130], 16)
        out[a] = [sorted(days.items()), len(wl), g4._digest(wl, with_block=True)]
    return out


def build_path(open_bal, flows, dest_days):
    """Daily end-of-day balances FIRST_DAY..IDENT_DAY: opening + cumulative flows - destroyed."""
    bal, s = open_bal, {}
    for d in range(FIRST_DAY, IDENT_DAY + 1):
        bal += flows.get(d, 0) - dest_days.get(d, 0)
        s[d] = bal
    return s


def classify_wallets(arms, opening, flows, gap_wallets, destroyed, signed0, pin=None, day0=None):
    """Pure per-wallet classification.
      arms: {addr: arm}; opening: {(coin, addr): int | None}; flows: {(coin, addr): {day: delta}};
      gap_wallets: set of addrs with an incomplete walk; destroyed: [{address, day, amount}] (already range-checked);
      signed0: {(addr, coin): int} signed pre-panel day_index 0.
    Returns {addr: {"status": ok|mismatch|missing, "reason": str, "series": {coin: {day: bal}} | None}}.
    Order: gap -> missing; pin balance absent for either coin -> missing (never zero); negative reconstructed balance,
    absent signed day-0 row or identity inequality (tolerance 0) -> mismatch."""
    dest = defaultdict(dict)
    for r in destroyed:
        d = int(r["day"])
        if not (FIRST_DAY <= d <= IDENT_DAY):
            raise AssertionError(f"DestroyedBlackFunds day {d} outside the O1 window")
        a = r["address"].lower()
        dest[a][d] = dest[a].get(d, 0) + int(r["amount"])
    res = {}
    for a in arms:
        if a in gap_wallets:
            res[a] = {"status": "missing", "reason": "gap", "series": None}
            continue
        miss = [c for c in COINS if opening.get((c, a)) is None]
        if miss:
            res[a] = {"status": "missing", "reason": "pin_missing_" + "_".join(miss), "series": None}
            continue
        series, bad = {}, None
        for c in COINS:
            s = build_path(opening[(c, a)], flows.get((c, a), {}), dest.get(a, {}) if c == "USDT" else {})
            if bad is None:
                neg = next((d for d in range(FIRST_DAY, IDENT_DAY + 1) if s[d] < 0), None)
                if neg is not None:
                    bad = f"negative_balance_{c}_day{neg}"
            series[c] = s
        if bad:
            res[a] = {"status": "mismatch", "reason": bad, "series": None}
            continue
        for c in COINS:
            want = signed0.get((a, c))
            if want is None:
                bad = f"no_signed_day0_{c}"
            elif series[c][IDENT_DAY] != want:
                bad = f"identity_{c}_diff_{series[c][IDENT_DAY] - want}"
            if bad:
                break
        res[a] = {"status": "mismatch", "reason": bad, "series": None} if bad else {"status": "ok", "reason": "", "series": series}
    return res


def mismatch_stats(arms, res):
    """Per arm: complete wallets (ok or mismatch, i.e. walk-complete with both pin balances), mismatches among them, fraction.
    Negative-balance and absent-day-0 mismatches are counted (they are `mismatch`)."""
    out = {}
    for arm in sorted(set(arms.values())):
        ws = [a for a in arms if arms[a] == arm]
        comp = [a for a in ws if res[a]["status"] in ("ok", "mismatch")]
        mm = [a for a in comp if res[a]["status"] == "mismatch"]
        out[arm] = {"n_complete": len(comp), "n_mismatch": len(mm), "fraction": (len(mm) / len(comp)) if comp else 0.0}
    return out


def over_threshold(stats, frac):
    """Arms whose fraction exceeds `frac` (strictly)."""
    return sorted(arm for arm, s in stats.items() if s["fraction"] > frac)


def pick_reverify_sample(arms, ok_addrs, fraction=REVERIFY_FRACTION, seed=REVERIFY_SEED):
    """Seeded sample of ok wallets within arm: per arm ceil(fraction * n) (no minimum)."""
    rng = random.Random(seed)
    by_arm = defaultdict(list)
    for a in sorted(ok_addrs):
        by_arm[arms[a]].append(a)
    out = []
    for arm in sorted(by_arm):
        xs = by_arm[arm]
        out += rng.sample(xs, min(len(xs), math.ceil(fraction * len(xs))))
    return sorted(out)


def compare_reverify(stored, got):
    """stored/got: {(addr, coin, pos): (count, digest)}. Returns [{addr, coin, pos, stored_n, got_n, digest_equal}] for every difference."""
    bad = []
    for k in sorted(set(stored) | set(got)):
        s, g = stored.get(k, (0, EMPTY_DIGEST)), got.get(k, (0, EMPTY_DIGEST))
        if s != g:
            bad.append({"addr": k[0], "coin": k[1], "pos": k[2], "stored_n": s[0], "got_n": g[0], "digest_equal": s[1] == g[1]})
    return bad


# ---------------------------------------------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------------------------------------------

def _truthy(x):
    return str(x).strip().lower() in ("true", "t", "1", "yes")


def load_universe(membership_csv):
    """match_membership.csv -> {address: arm} for matched rows (arm exposed|control)."""
    arms = {}
    with open(membership_csv, newline="") as f:
        for r in csv.DictReader(f):
            if not _truthy(r["matched"]):
                continue
            a = r["address"].strip().lower()
            if not _ADDR.fullmatch(a):
                raise ValueError(f"membership address not lowercase 0x+40hex: {a!r}")
            if r["arm"] not in ("exposed", "control"):
                raise ValueError(f"unknown arm {r['arm']!r}")
            if a in arms:
                raise ValueError(f"duplicate membership address {a}")
            arms[a] = r["arm"]
    if not arms:
        raise ValueError("membership has no matched wallet")
    return arms


def load_signed_day0(cov_csv, panel_csv):
    """Signed pre panel (covariates.csv wallet_id->address + wallet_coin_day_pre.csv rows with day_index 0) -> {(addr, coin): int}."""
    wid = {}
    with open(cov_csv, newline="") as f:
        for r in csv.DictReader(f):
            wid[r["wallet_id"]] = r["address"].strip().lower()
    out = {}
    with open(panel_csv, newline="") as f:
        for r in csv.DictReader(f):
            if int(r["day_index"]) != IDENT_DAY:
                continue
            k = (wid[r["wallet_id"]], r["coin"])
            if k in out:
                raise ValueError(f"duplicate signed day-0 row {k}")
            out[k] = int(r["balance_units"])
    return out


# ---------------------------------------------------------------------------------------------------------------
# checkpoint + receipts (rooted in --out; g4's writers are not used)
# ---------------------------------------------------------------------------------------------------------------

def _canon(v):
    return json.dumps(v, sort_keys=True, separators=(",", ":"))


def _vhash(v):
    return hashlib.sha256(_canon(v).encode()).hexdigest()


class Ckpt:
    """Append-only jsonl {k, v, h}; h = sha256 of the canonical v, verified on load; fsync per write; opened O_NOFOLLOW.
    Only an unterminated FINAL line (torn write) may be dropped; a complete tail lacking its newline is kept and the
    newline restored before the next append; any other bad line raises."""

    def __init__(self, path):
        self.path, self.data = path, {}
        good = 0
        need_nl = False
        if os.path.exists(path) or os.path.islink(path):
            raw = _read_bytes(path)
            lines = raw.split(b"\n")
            tail = lines.pop()                       # b"" when the file ends with a newline
            off = 0
            for i, line in enumerate(lines):
                if line.strip():
                    self._load(line, f"{path}: corrupt line {i + 1}")
                off += len(line) + 1
            good = off
            if tail.strip():
                try:
                    self._load(tail, f"{path}: torn tail")
                    good, need_nl = off + len(tail), True
                except RuntimeError:
                    fd = _open_fd(path, os.O_WRONLY)
                    try:
                        os.ftruncate(fd, good)
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            elif tail:                               # whitespace-only tail: drop it too
                fd = _open_fd(path, os.O_WRONLY)
                try:
                    os.ftruncate(fd, good)
                finally:
                    os.close(fd)
        self._fh = os.fdopen(_open_fd(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT), "a")
        self._lock = threading.Lock()
        if need_nl:
            self._fh.write("\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def _load(self, line, what):
        try:
            rec = json.loads(line)
            k, v, h = rec["k"], rec["v"], rec["h"]
        except (ValueError, KeyError, TypeError) as e:
            raise RuntimeError(f"{what}: {e}") from e
        if h != _vhash(v):
            raise RuntimeError(f"{what}: record hash mismatch for key {k!r}")
        self.data[k] = v

    def put(self, k, v):
        line = json.dumps({"k": k, "v": v, "h": _vhash(v)}) + "\n"
        with self._lock:
            self._fh.write(line)
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self.data[k] = v

    def close(self):
        self._fh.close()


class Receipts:
    def __init__(self, path):
        self.path, self._lock = path, threading.Lock()

    def add(self, rec):
        line = json.dumps(dict(rec, t=time.time()), sort_keys=True) + "\n"
        with self._lock:
            fd = _open_fd(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
            with os.fdopen(fd, "a") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())


def assert_receipts(path, pin, day0):
    """Every receipt's to_block / block must be <= day0, except block-header reads, which must be marked timestamp_only
    and may touch only pin, pin+1, day0, day0+1 (the pin-check blocks). Returns the receipt count."""
    n = 0
    hdr_ok = {pin, pin + 1, day0, day0 + 1}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("kind") == "block_header":
                if rec.get("timestamp_only") is not True or rec.get("blockNumber") not in hdr_ok:
                    raise g4.PostPeriodRead(f"unexpected block-header receipt: {line[:200]}")
            else:
                top = rec.get("toBlock", rec.get("block"))
                if top is None or top > day0:
                    raise g4.PostPeriodRead(f"receipt beyond day-0 block {day0}: {line[:200]}")
            n += 1
    return n


# ---------------------------------------------------------------------------------------------------------------
# chain access (live implementation wraps the g4 helpers; tests inject a fake with the same three methods)
# ---------------------------------------------------------------------------------------------------------------

QUOTA_BACKOFF_BASE_S = 30.0   # rate-limit (429 / quota) backoff: exponential from here, capped per wait, bounded in total
QUOTA_BACKOFF_MAX_S = 300.0
QUOTA_BACKOFF_TOTAL_S = 900.0


_TRANSPORT_RE = re.compile(r"urlopen error|\[SSL:|CERTIFICATE_VERIFY_FAILED|hostname mismatch", re.I)


def _is_transport_error(e):
    """Transient transport failure (DNS/TLS/connection), never a data or validation error: a URLError (not an HTTPError,
    which carries a status and goes through rpc.classify_error) or ssl.SSLError, or the RuntimeError g4._batch_post
    raises after its retries, recognised by the wrapped urllib/ssl wording."""
    if isinstance(e, urllib.error.HTTPError):
        return False
    if isinstance(e, (urllib.error.URLError, ssl.SSLError)):
        return True
    return isinstance(e, RuntimeError) and bool(_TRANSPORT_RE.search(str(e)))


def with_quota_backoff(fn, sleep=None, rng=random.uniform):
    """Call fn(); g4._batch_post already gave up its short retries with a RuntimeError. If that error classifies as quota
    (HTTP 429 / rate-limit wording, rpc.classify_error) wait with jittered exponential backoff on the SAME endpoint and
    retry, until QUOTA_BACKOFF_TOTAL_S of waiting is spent, then re-raise (the run is resumable). Other errors propagate
    unchanged. Transient transport errors (URLError / ssl.SSLError, e.g. a gateway certificate hostname mismatch; see
    _is_transport_error) share the same bounded backoff; data and validation errors are never retried. Lives here, not in gates/g4_controls.py, which is signed-bound (test_no_edit_of_signed_bound_files)."""
    sleep = sleep or time.sleep
    n, waited = 0, 0.0
    while True:
        try:
            return fn()
        except (RuntimeError, urllib.error.URLError, ssl.SSLError) as e:
            if rpc.classify_error(e) != "quota" and not _is_transport_error(e):
                raise
            wait = min(QUOTA_BACKOFF_MAX_S, QUOTA_BACKOFF_BASE_S * (2 ** n)) * rng(0.75, 1.25)
            if waited + wait > QUOTA_BACKOFF_TOTAL_S:
                raise
            _log(f"rate-limited/transport ({str(e)[:120]}); backing off {wait:.0f}s ({waited:.0f}s spent)")
            n += 1
            waited += wait
            sleep(wait)


class LiveChain:
    def balance_of(self, endpoint, coin, addrs, block):
        """Strictly validated batched balanceOf at one pinned block -> [int]; raises RuntimeError on any failure
        (a rate limit is retried with a long bounded backoff first)."""
        token = COINS[coin]
        calls = [("eth_call", [{"to": token, "data": g1._balance_of_data(a)}, hex(block)]) for a in addrs]
        size, pause = BALANCE_PACING.get(endpoint, (max(len(calls), 1), 0.0))
        res = []
        for i in range(0, len(calls), size):
            if i and pause:
                time.sleep(pause)
            part = calls[i:i + size]
            res += with_quota_backoff(lambda part=part: g4._batch_post(endpoint, part, validate=g4._valid_word))
        return [g1._hex_to_int(r) for r in res]

    def logs(self, endpoint, coin, topics, start, end, guard):
        """[start, end] in windows with result-cap bisection -> (logs, [(s, e, count, digest, endpoint)]); raises rpc.GapError."""
        return g4._fetch_windowed(endpoint, COINS[coin], topics, start, end, guard, g4.REPLAY_WORKERS.get(endpoint, g4.WORKERS))

    def block_ts(self, endpoint, block):
        b = rpc.get_block_by_number(endpoint, block)
        if not b or "timestamp" not in b:
            raise RuntimeError(f"{endpoint} returned no header for block {block}")
        return _hexint(b["timestamp"], "block timestamp")


# ---------------------------------------------------------------------------------------------------------------
# acquisition
# ---------------------------------------------------------------------------------------------------------------

def _log(msg):
    print(f"[o1] {msg}", flush=True)


def _guard(block, day0):
    return g4._guard_block(block, day0)


def _nonneg_int(x):
    return isinstance(x, int) and not isinstance(x, bool) and x >= 0


def _check_open_rec(rec, chunk):
    """A stored pin-balance record: vals keyed exactly by the chunk, every value a non-negative int."""
    vals = rec.get("vals")
    if not isinstance(vals, dict) or set(vals) != set(chunk) or not all(_nonneg_int(v) for v in vals.values()):
        raise RuntimeError("checkpoint pin-balance record is not {address: non-negative int} for its chunk")


def _check_blocks_rec(rec):
    """Cached pin-check timestamps: exactly {pin, pin+1, day0, day0+1} -> non-negative ints."""
    if not (isinstance(rec, dict) and set(rec) == {"pin", "pin+1", "day0", "day0+1"} and all(_nonneg_int(v) for v in rec.values())):
        raise RuntimeError("checkpoint blocks record malformed")


def _check_rv_rec(rec, sample):
    """Cached re-verify result: sample_n + three mismatch lists of dicts whose `addr` lies in the sample."""
    sset = set(sample)
    if not (isinstance(rec, dict) and rec.get("sample_n") == len(sample)):
        raise RuntimeError("checkpoint reverify record malformed (sample_n)")
    for k in ("log_mismatches", "balance_mismatches", "path_mismatches"):
        lst = rec.get(k)
        if not (isinstance(lst, list) and all(isinstance(m, dict) and m.get("addr") in sset for m in lst)):
            raise RuntimeError(f"checkpoint reverify record malformed ({k})")


def _check_balances(vals, chunk):
    """Re-verify balanceOf result: one non-negative int per requested address."""
    if not (isinstance(vals, list) and len(vals) == len(chunk) and all(_nonneg_int(v) for v in vals)):
        raise RuntimeError("re-verify balanceOf returned a non-integer / negative / short result")


def _check_unit_rec(rec):
    """A stored OK walk/destroyed unit: ranges + per-wallet [[day, int delta]...], non-negative int count, v2 digest."""
    if not isinstance(rec.get("ranges"), list) or not isinstance(rec.get("wallets"), dict) or not _nonneg_int(rec.get("logs")):
        raise RuntimeError("checkpoint unit record malformed")
    for r in rec["ranges"]:
        if not (isinstance(r, list) and len(r) == 5 and _nonneg_int(r[0]) and _nonneg_int(r[1])):
            raise RuntimeError("checkpoint unit range malformed")
    for a, ent in rec["wallets"].items():
        if not (isinstance(ent, list) and len(ent) == 3 and _nonneg_int(ent[1]) and isinstance(ent[2], str) and ent[2].startswith("v2:")
                and isinstance(ent[0], list)):
            raise RuntimeError(f"checkpoint unit entry malformed for {a}")
        for dv in ent[0]:
            if not (isinstance(dv, list) and len(dv) == 2 and all(isinstance(x, int) and not isinstance(x, bool) for x in dv)
                    and FIRST_DAY <= dv[0] <= IDENT_DAY):
                raise RuntimeError(f"checkpoint day/delta malformed for {a}")


def acquire_openings(chain, ck, rcpt, universe, pin, day0, ep, ep2, retry_failed=True):
    """Opening balanceOf at the pin, both coins. A chunk that fails on BOTH endpoints is recorded missing (never zero)."""
    _guard(pin, day0)
    for coin in COINS:
        for bi in range(0, len(universe), OPEN_BATCH):
            key, chunk = f"open:{coin}:{bi}", universe[bi:bi + OPEN_BATCH]
            rec = ck.data.get(key)
            if rec is not None and "vals" in rec:
                _check_open_rec(rec, chunk)
                continue
            if rec is not None and rec.get("missing") and not retry_failed:
                continue
            err = None
            for e in (ep, ep2):
                try:
                    vals = chain.balance_of(e, coin, chunk, pin)
                    if len(vals) != len(chunk) or not all(_nonneg_int(v) for v in vals):
                        raise RuntimeError("balanceOf returned a non-integer / negative / short result")
                    ck.put(key, {"ep": e, "vals": dict(zip(chunk, vals))})
                    rcpt.add({"kind": "balanceOf", "endpoint": e, "coin": coin, "block": pin, "addressCount": len(chunk),
                              "sha256": hashlib.sha256(json.dumps(vals).encode()).hexdigest()})
                    break
                except RuntimeError as ex:
                    err = str(ex)[:300]
            else:
                ck.put(key, {"missing": True, "err": err})
                rcpt.add({"kind": "balanceOf_failed", "endpoint": f"{ep}+{ep2}", "coin": coin, "block": pin, "addressCount": len(chunk)})
                _log(f"  pin balances {coin} batch {bi}: unreadable on both endpoints -> {len(chunk)} wallets missing for this coin")


def _fetch_with_retry(chain, rcpt, key, coin, topics, pin, day0, ep, ep2):
    """One unit's whole pin+1..day0 range on `ep`; on a GapError ONE retry of the same range on `ep2`. -> (endpoint used, logs, ranges)."""
    last = None
    for e in (ep, ep2):
        try:
            logs, ranges = chain.logs(e, coin, topics, pin + 1, day0, day0)
            g4._assert_tiles([(s, en) for s, en, *_ in ranges], pin + 1, day0)
            return e, logs, ranges
        except rpc.GapError as ex:
            last = ex
            rcpt.add({"kind": "getLogs_failed", "unit": key, "endpoint": e, "fromBlock": pin + 1, "toBlock": day0})
    raise last


def _put_unit(ck, rcpt, key, used, logs, ranges, wallets, n_addr):
    ck.put(key, {"ok": True, "ep": used, "ranges": [list(r) for r in ranges], "logs": len(logs),
                 "wallets": {a: [[list(x) for x in v[0]], v[1], v[2]] for a, v in wallets.items()}})
    for s, e, cnt, dig, epn in ranges:
        rcpt.add({"kind": "getLogs", "unit": key, "endpoint": epn, "fromBlock": s, "toBlock": e,
                  "addressCount": n_addr, "logCount": cnt, "sha256": dig})


def acquire_walk(chain, ck, rcpt, universe, pin, day0, ep, ep2, retry_failed=True):
    units = [(c, p, bi) for c in COINS for p in ("from", "to") for bi in range(0, len(universe), WALK_ARRAY)]
    t0 = time.time()
    for n, (coin, pos, bi) in enumerate(units, 1):
        key = f"unit:{coin}:{pos}:{bi}"
        rec = ck.data.get(key)
        if rec is not None and (rec.get("ok") or not retry_failed):
            continue
        chunk = universe[bi:bi + WALK_ARRAY]
        try:
            used, logs, ranges = _fetch_with_retry(chain, rcpt, key, coin, g4._unit_topics(pos, chunk), pin, day0, ep, ep2)
            wallets = decode_unit(logs, pos, set(chunk), pin, day0, COINS[coin])
        except rpc.GapError as ex:
            ck.put(key, {"gap": str(ex)[:300]})
            rcpt.add({"kind": "unit_failed", "unit": key, "endpoint": f"{ep}+{ep2}", "fromBlock": pin + 1, "toBlock": day0, "addressCount": len(chunk)})
            _log(f"  unit {key}: GAP on both endpoints -> {len(chunk)} wallets walk-incomplete")
            continue
        _put_unit(ck, rcpt, key, used, logs, ranges, wallets, len(chunk))
        _log(f"  walk unit {n}/{len(units)} {key}: {len(logs)} logs ({used}) | {time.time() - t0:.0f}s")


def acquire_destroyed(chain, ck, rcpt, universe_set, pin, day0, ep, ep2, retry_failed=True):
    """USDT DestroyedBlackFunds as log ranges (topic-only), same complete-walk / retry rules as the Transfer units."""
    rec = ck.data.get(DESTROYED_KEY)
    if rec is not None and (rec.get("ok") or not retry_failed):
        return
    try:
        used, logs, ranges = _fetch_with_retry(chain, rcpt, DESTROYED_KEY, "USDT", [DESTROYED_TOPIC], pin, day0, ep, ep2)
        wallets = decode_destroyed(logs, universe_set, pin, day0)
    except rpc.GapError as ex:
        ck.put(DESTROYED_KEY, {"gap": str(ex)[:300]})
        rcpt.add({"kind": "unit_failed", "unit": DESTROYED_KEY, "endpoint": f"{ep}+{ep2}", "fromBlock": pin + 1, "toBlock": day0, "addressCount": 0})
        _log("  DestroyedBlackFunds range: GAP on both endpoints -> every wallet walk-incomplete")
        return
    _put_unit(ck, rcpt, DESTROYED_KEY, used, logs, ranges, wallets, 0)
    _log(f"  DestroyedBlackFunds: {len(logs)} logs ({used}), {len(wallets)} tracked wallets")


def collect(ck, universe, pin, day0):
    """Checkpoint -> dict(opening, flows, gap, stored, unit_ep, open_ep, destroyed, destroyed_ep). stored[(addr, coin, pos)] =
    (n, digest) for every wallet of every OK unit (zero-log wallets get the empty digest); pos in from|to|destroyed (coin USDT).
    Records are type-checked; a malformed record raises."""
    opening, open_ep = {}, {}
    for coin in COINS:
        for bi in range(0, len(universe), OPEN_BATCH):
            chunk = universe[bi:bi + OPEN_BATCH]
            rec = ck.data.get(f"open:{coin}:{bi}")
            ok = rec is not None and "vals" in rec
            if ok:
                _check_open_rec(rec, chunk)
            for a in chunk:
                opening[(coin, a)] = rec["vals"][a] if ok else None
                open_ep[(coin, a)] = rec.get("ep") if ok else None
    flows, gap, stored, unit_ep = defaultdict(lambda: defaultdict(int)), set(), {}, {}
    for coin in COINS:
        for pos in ("from", "to"):
            for bi in range(0, len(universe), WALK_ARRAY):
                chunk = universe[bi:bi + WALK_ARRAY]
                rec = ck.data.get(f"unit:{coin}:{pos}:{bi}")
                if rec is None or not rec.get("ok"):
                    gap.update(chunk)
                    continue
                _check_unit_rec(rec)
                _assert_ranges_ok(rec, pin, day0)
                cs = set(chunk)
                for a in chunk:
                    stored[(a, coin, pos)] = (0, EMPTY_DIGEST)
                    unit_ep[(a, coin, pos)] = rec["ep"]
                for a, (days, n, dig) in rec["wallets"].items():
                    if a not in cs:
                        raise RuntimeError(f"checkpoint unit lists a wallet outside its chunk: {a}")
                    stored[(a, coin, pos)] = (n, dig)
                    for d, v in days:
                        flows[(coin, a)][d] += v
    destroyed, destroyed_ep = [], None
    rec = ck.data.get(DESTROYED_KEY)
    if rec is None or not rec.get("ok"):
        gap.update(universe)                              # one failed DestroyedBlackFunds range makes every wallet incomplete
    else:
        _check_unit_rec(rec)
        _assert_ranges_ok(rec, pin, day0)
        destroyed_ep, uset = rec["ep"], set(universe)
        for a in universe:
            stored[(a, "USDT", DESTROYED_KEY)] = (0, EMPTY_DIGEST)
        for a, (days, n, dig) in rec["wallets"].items():
            if a not in uset:
                raise RuntimeError(f"checkpoint destroyed record lists an untracked wallet: {a}")
            stored[(a, "USDT", DESTROYED_KEY)] = (n, dig)
            destroyed += [{"address": a, "day": d, "amount": v} for d, v in days]
    return {"opening": opening, "flows": {k: dict(v) for k, v in flows.items()}, "gap": gap, "stored": stored,
            "unit_ep": unit_ep, "open_ep": open_ep, "destroyed": destroyed, "destroyed_ep": destroyed_ep}


def _assert_ranges_ok(rec, pin, day0):
    g4._assert_tiles([(r[0], r[1]) for r in rec["ranges"]], pin + 1, day0)
    if any(r[1] > day0 for r in rec["ranges"]):
        raise g4.PostPeriodRead("checkpointed range beyond the day-0 block")


def run_reverify(chain, rcpt, sample, col, series, signed0, pin, day0, ep, ep2, ck=None, tag=""):
    """R10. Re-read, on the endpoint OTHER than the one that produced each record, the pin balances, the Transfer-log ranges
    pin+1..day0 (per coin x position) and the DestroyedBlackFunds range of the sampled wallets; compare count + sha256 per
    (wallet, coin, position), the pin balances, the day-0 balances (vs the signed pre panel) and the derived daily path
    (second-endpoint pin balance + flows - destroyed) against the primary path. Unavailability of the second endpoint
    raises (nothing is claimed verified). With `ck`, each read unit (log chunk, destroyed range, balance chunk) is
    checkpointed under `rvu:<tag>:...` as soon as it is read, so a resume continues instead of restarting; the
    comparisons are unchanged and run over cached and fresh units alike."""
    def unit(name, compute):
        key = f"rvu:{tag}:{name}"
        if ck is not None and key in ck.data:
            return ck.data[key]
        v = compute()
        if ck is not None:
            ck.put(key, v)
        return v

    other = lambda e: ep2 if e == ep else ep  # noqa: E731
    sset = set(sample)
    got, flows2, dest2 = {}, defaultdict(lambda: defaultdict(int)), defaultdict(lambda: defaultdict(int))
    for coin in COINS:
        for pos in ("from", "to"):
            by_ep = defaultdict(list)
            for a in sample:
                by_ep[other(col["unit_ep"][(a, coin, pos)])].append(a)
            for vep, ws in sorted(by_ep.items()):
                for bi in range(0, len(ws), WALK_ARRAY):
                    chunk = ws[bi:bi + WALK_ARRAY]
                    cs = set(chunk)

                    def read_logs(vep=vep, coin=coin, pos=pos, chunk=chunk, cs=cs):
                        logs, ranges = chain.logs(vep, coin, g4._unit_topics(pos, chunk), pin + 1, day0, day0)
                        g4._assert_tiles([(s, e) for s, e, *_ in ranges], pin + 1, day0)
                        w, fl = [], []
                        for a, wl in group_logs_by_wallet(logs, pos).items():
                            if a not in cs:
                                raise AssertionError(f"second endpoint returned a log for untracked address {a}")
                            w.append([a, len(wl), g4._digest(wl, with_block=True)])
                            fl += [[a, d, v] for d, v in wallet_day_flows(wl, pos).items()]
                        for s, e, cnt, dig, epn in ranges:
                            rcpt.add({"kind": "reverify_getLogs", "coin": coin, "pos": pos, "endpoint": epn, "fromBlock": s, "toBlock": e,
                                      "addressCount": len(chunk), "logCount": cnt, "sha256": dig})
                        return {"w": w, "flows": fl}
                    u = unit(f"logs:{coin}:{pos}:{vep}:{bi}", read_logs)
                    for a, n, dig in u["w"]:
                        got[(a, coin, pos)] = (n, dig)
                    for a, d, v in u["flows"]:
                        flows2[(coin, a)][d] += v
    vep = other(col["destroyed_ep"])

    def read_destroyed():
        logs, ranges = chain.logs(vep, "USDT", [DESTROYED_TOPIC], pin + 1, day0, day0)
        g4._assert_tiles([(s, e) for s, e, *_ in ranges], pin + 1, day0)
        w, dd = [], []
        for a, (days, n, dig) in decode_destroyed(logs, sset, pin, day0).items():
            w.append([a, n, dig])
            dd += [[a, d, v] for d, v in days]
        for s, e, cnt, dig, epn in ranges:
            rcpt.add({"kind": "reverify_getLogs", "coin": "USDT", "pos": DESTROYED_KEY, "endpoint": epn, "fromBlock": s, "toBlock": e,
                      "addressCount": 0, "logCount": cnt, "sha256": dig})
        return {"w": w, "dest": dd}
    u = unit(f"destroyed:{vep}", read_destroyed)
    for a, n, dig in u["w"]:
        got[(a, "USDT", DESTROYED_KEY)] = (n, dig)
    for a, d, v in u["dest"]:
        dest2[a][d] += v
    bad = compare_reverify({k: v for k, v in col["stored"].items() if k[0] in sset}, got)
    bal_bad, pin2 = [], {}
    for coin in COINS:
        by_ep = defaultdict(list)
        for a in sample:
            by_ep[other(col["open_ep"][(coin, a)])].append(a)
        for vep, ws in sorted(by_ep.items()):
            for bi in range(0, len(ws), OPEN_BATCH):
                chunk = ws[bi:bi + OPEN_BATCH]
                def read_pin(vep=vep, coin=coin, chunk=chunk):
                    vals = chain.balance_of(vep, coin, chunk, _guard(pin, day0))
                    _check_balances(vals, chunk)
                    rcpt.add({"kind": "reverify_balanceOf", "endpoint": vep, "coin": coin, "block": pin, "addressCount": len(chunk),
                              "sha256": hashlib.sha256(json.dumps(vals).encode()).hexdigest()})
                    return vals
                vals = unit(f"pin:{coin}:{vep}:{bi}", read_pin)
                _check_balances(vals, chunk)
                for a, v in zip(chunk, vals):
                    pin2[(coin, a)] = v
                    if v != col["opening"][(coin, a)]:
                        bal_bad.append({"addr": a, "coin": coin, "what": "pin", "expected": col["opening"][(coin, a)], "got": v})
        for bi in range(0, len(sample), OPEN_BATCH):
            chunk = sample[bi:bi + OPEN_BATCH]
            def read_day0(coin=coin, chunk=chunk):
                vals = chain.balance_of(ep2, coin, chunk, _guard(day0, day0))
                _check_balances(vals, chunk)
                rcpt.add({"kind": "reverify_balanceOf", "endpoint": ep2, "coin": coin, "block": day0, "addressCount": len(chunk),
                          "sha256": hashlib.sha256(json.dumps(vals).encode()).hexdigest()})
                return vals
            vals = unit(f"day0:{coin}:{ep2}:{bi}", read_day0)
            _check_balances(vals, chunk)
            for a, v in zip(chunk, vals):
                if v != signed0.get((a, coin)):
                    bal_bad.append({"addr": a, "coin": coin, "what": "day0", "expected": signed0.get((a, coin)), "got": v})
    path_bad = []
    for a in sample:
        for coin in COINS:
            p2 = build_path(pin2[(coin, a)], dict(flows2.get((coin, a), {})), dict(dest2.get(a, {})) if coin == "USDT" else {})
            diff = next((d for d in range(FIRST_DAY, IDENT_DAY + 1) if p2[d] != series[a][coin][d]), None)
            if diff is not None:
                path_bad.append({"addr": a, "coin": coin, "first_day": diff, "expected": series[a][coin][diff], "got": p2[diff]})
    return {"sample_n": len(sample), "log_mismatches": bad, "balance_mismatches": bal_bad, "path_mismatches": path_bad}


# ---------------------------------------------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------------------------------------------

def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _write_csv(path, header, rows):
    with atomic_text(path) as f:
        w = csv.writer(f)
        w.writerow(["banner"] + header)
        for r in rows:
            w.writerow([BANNER] + list(r))


def _write_json(path, obj):
    with atomic_text(path) as f:
        json.dump(obj, f, indent=1, sort_keys=True)


def run_extraction(cfg, chain):
    """cfg keys: arms {addr: arm}, signed0, pin, day0, outdir, walk_ep, verify_ep, max_mismatch_frac, max_reverify_frac,
    reverify_fraction, resume (bool), input_hashes (dict), producer (optional override of the producer identity).
    Returns the summary dict; raises Abort (outputs for diagnosis are still written)."""
    outdir, pin, day0, arms = cfg["outdir"], cfg["pin"], cfg["day0"], cfg["arms"]
    ep, ep2 = cfg["walk_ep"], cfg["verify_ep"]
    if ep == ep2:
        raise Abort("walk and verify endpoints must differ")
    mm_frac, rv_frac, rv_fraction = cfg["max_mismatch_frac"], cfg["max_reverify_frac"], cfg["reverify_fraction"]
    for nm, v in (("max_mismatch_frac", mm_frac), ("max_reverify_frac", rv_frac), ("reverify_fraction", rv_fraction)):
        if not (isinstance(v, (int, float)) and 0 <= v <= 1):
            raise Abort(f"{nm} must be a fraction in [0, 1]: {v!r}")
    refuse_symlinks(outdir)
    universe = sorted(arms)
    lock = os.fdopen(_open_fd(os.path.join(outdir, ".slot.lock"), os.O_RDWR | os.O_CREAT), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise Abort("another O1 acquisition holds the slot lock")
    ck = rcpt = None
    global _RUN_BLOCKS
    _RUN_BLOCKS = {}
    try:
        params = {"schema": SCHEMA, "pin": pin, "day0": day0, "walk_ep": ep, "verify_ep": ep2, "n_wallets": len(universe),
                  "universe_sha256": hashlib.sha256("\n".join(f"{a},{arms[a]}" for a in universe).encode()).hexdigest(),
                  "input_hashes": cfg.get("input_hashes", {}), "walk_array": WALK_ARRAY, "open_batch": OPEN_BATCH,
                  "max_mismatch_frac": mm_frac, "max_reverify_frac": rv_frac, "reverify_fraction": rv_fraction,
                  "reverify_seed": REVERIFY_SEED, "producer": cfg.get("producer") or rpc.producer_identity(PRODUCER_FILES)}
        pp = os.path.join(outdir, "run_params.json")
        if os.path.exists(pp):
            stored = json.loads(_read_bytes(pp))
            if {k: v for k, v in stored.items() if k != "producer"} != {k: v for k, v in params.items() if k != "producer"}:
                raise Abort("run_params.json differs from this invocation: refusing to resume a different run")
            if check_resume_producer(stored.get("producer"), params["producer"], cfg.get("producer_files", PRODUCER_FILES), REPO):
                # run_params.json stays untouched; the resuming HEAD is appended to a separate log.
                with os.fdopen(_open_fd(os.path.join(outdir, "resume_log.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND), "a") as f:
                    f.write(json.dumps({"t": time.time(), "stored_producer": stored["producer"],
                                        "resuming_producer": params["producer"]}, sort_keys=True) + "\n")
        else:
            _write_json(pp, params)
        ck, rcpt = Ckpt(os.path.join(outdir, "checkpoint.jsonl")), Receipts(os.path.join(outdir, "receipts.jsonl"))
        if "blocks" in ck.data:
            _check_blocks_rec(ck.data["blocks"])
        else:
            def ts_logged(b):                             # every block read is a receipt; header reads are timestamp-only
                t = chain.block_ts(ep, b)
                rcpt.add({"kind": "block_header", "endpoint": ep, "blockNumber": b, "timestamp": t, "timestamp_only": True})
                return t
            ck.put("blocks", check_pin_blocks(ts_logged, pin, day0))
        _log(f"{len(universe)} wallets; pin {pin} -> day-0 block {day0}")
        acquire_openings(chain, ck, rcpt, universe, pin, day0, ep, ep2)
        acquire_walk(chain, ck, rcpt, universe, pin, day0, ep, ep2)
        acquire_destroyed(chain, ck, rcpt, set(universe), pin, day0, ep, ep2)
        col = collect(ck, universe, pin, day0)
        res = classify_wallets(arms, col["opening"], col["flows"], col["gap"], col["destroyed"], cfg["signed0"], pin, day0)
        mm_stats = mismatch_stats(arms, res)              # before the reverify: identity-class mismatches among complete wallets
        ok = [a for a in universe if res[a]["status"] == "ok"]
        sample = pick_reverify_sample(arms, ok, rv_fraction)
        rv, rv_arm = None, {}
        if sample:
            rkey = "reverify:" + hashlib.sha256(json.dumps(sample).encode()).hexdigest()[:16]
            rv = ck.data.get(rkey)
            if rv is not None:
                _check_rv_rec(rv, sample)
            else:
                rv = run_reverify(chain, rcpt, sample, col, {a: res[a]["series"] for a in sample}, cfg["signed0"], pin, day0, ep, ep2,
                                  ck=ck, tag=rkey.split(":", 1)[1])
                ck.put(rkey, rv)
            rv_bad = {m["addr"] for k in ("log_mismatches", "balance_mismatches", "path_mismatches") for m in rv[k]}
            for a in rv_bad:
                res[a] = {"status": "missing", "reason": "reverify", "series": None}
            for arm in sorted(set(arms.values())):
                n = sum(1 for a in sample if arms[a] == arm)
                k = sum(1 for a in sample if arms[a] == arm and a in rv_bad)
                rv_arm[arm] = {"sample_n": n, "n_differ": k, "fraction": (k / n) if n else 0.0}
        assert_receipts(rcpt.path, pin, day0)
        mm_over, rv_over = over_threshold(mm_stats, mm_frac), over_threshold(rv_arm, rv_frac)
        counts = {arm: dict(Counter(res[a]["status"] for a in universe if arms[a] == arm)) for arm in sorted(set(arms.values()))}
        reasons = dict(Counter(f'{arms[a]}|{res[a]["status"]}|{res[a]["reason"].split("_diff_")[0]}' for a in universe if res[a]["status"] != "ok"))
        aborted = bool(mm_over or rv_over)
        summary = {"banner": BANNER, "params": params, "counts_by_arm": counts, "mismatch_by_arm": mm_stats,
                   "n_mismatch": sum(s["n_mismatch"] for s in mm_stats.values()), "max_mismatch_frac": mm_frac,
                   "mismatch_over_threshold_arms": mm_over, "reverify_over_threshold_arms": rv_over, "aborted": aborted,
                   "non_ok_reasons": reasons,
                   "reverify": None if rv is None else {"sample_n": rv["sample_n"], "n_log_mismatches": len(rv["log_mismatches"]),
                                                        "n_balance_mismatches": len(rv["balance_mismatches"]),
                                                        "n_path_mismatches": len(rv["path_mismatches"]), "by_arm": rv_arm},
                   "n_walk_units_gap": sum(1 for k, v in ck.data.items() if k.startswith("unit:") and not v.get("ok")),
                   "destroyed_range_ok": col["destroyed_ep"] is not None,
                   "destroyed_events_applied": len(col["destroyed"])}
        _write_csv(os.path.join(outdir, "o1_wallet_status.csv"), ["address", "arm", "status", "reason"],
                   [(a, arms[a], res[a]["status"], res[a]["reason"]) for a in universe])
        _write_json(os.path.join(outdir, "o1_summary.json"), summary)
        if aborted:
            raise Abort(f"mismatch fraction over {mm_frac} in arm(s) {mm_over} and/or re-verify difference fraction over {rv_frac} "
                        f"in arm(s) {rv_over}: see o1_wallet_status.csv / o1_summary.json (no panel written; fix the cause, re-run in full)")
        opening = col["opening"]
        _write_csv(os.path.join(outdir, "o1_pin_balances.csv"), ["address", "coin", "balance_units"],
                   [(a, c, "" if opening[(c, a)] is None else opening[(c, a)]) for a in universe for c in COINS])
        _write_csv(os.path.join(outdir, "o1_panel.csv"), ["address", "coin", "day_index", "date", "balance_units"],
                   ((a, c, d, date_of_day(d), res[a]["series"][c][d]) for a in universe if res[a]["status"] == "ok"
                    for c in COINS for d in range(FIRST_DAY, LAST_PANEL_DAY + 1)))
        names = sorted(n for n in os.listdir(outdir) if n not in ("MANIFEST.sha256", "checkpoint.jsonl") and not n.startswith("."))
        with atomic_text(os.path.join(outdir, "MANIFEST.sha256")) as f:
            for n in names:
                f.write(f"{_sha256_file(os.path.join(outdir, n))}  {n}\n")
        return summary
    finally:
        _RUN_BLOCKS = None
        if ck is not None:
            ck.close()
        lock.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("resolve-blocks", help="find the pin and day-0 blocks by timestamp bisection (non-sample RPC only)")
    r.add_argument("--endpoint", default=g4.WALK)
    r.add_argument("--hi", type=int, default=15_000_000, help="a block known to be after 2022-06-10 00:00 UTC")
    x = sub.add_parser("run")
    x.add_argument("--membership", required=True)
    x.add_argument("--signed-cov", required=True, help="signed covariates.csv (wallet_id -> address)")
    x.add_argument("--signed-panel", required=True, help="signed wallet_coin_day_pre.csv (day_index 0 rows are used)")
    x.add_argument("--pin-block", type=int, required=True)
    x.add_argument("--day0-block", type=int, required=True)
    x.add_argument("--out", required=True)
    x.add_argument("--repo-root", default=REPO)
    x.add_argument("--resume", action="store_true")
    x.add_argument("--walk-endpoint", default=g4.WALK, choices=sorted(g4.REPLAY_ENDPOINT))
    x.add_argument("--verify-endpoint", default=None, choices=sorted(g4.REPLAY_WORKERS),
                   help="endpoint for the R10 re-verify; must differ from --walk-endpoint (default: g4.REPLAY_ENDPOINT[walk])")
    x.add_argument("--max-mismatch-frac", type=float, default=MAX_MISMATCH_FRAC,
                   help="stop if mismatches exceed this fraction of the complete wallets of either arm (plan: 0.005)")
    x.add_argument("--max-reverify-frac", type=float, default=MAX_REVERIFY_FRAC,
                   help="stop if more than this fraction of the re-verified wallets of either arm differ (plan: 0.01)")
    x.add_argument("--reverify-fraction", type=float, default=REVERIFY_FRACTION, help="re-verify sample fraction within arm (plan: 0.05)")
    a = ap.parse_args(argv)
    chain = LiveChain()
    if a.cmd == "resolve-blocks":
        ts = lambda b: chain.block_ts(a.endpoint, b)  # noqa: E731
        pin = last_block_before(_ts_utc(FIRST_DATE), ts, 14_000_000, a.hi)
        d0 = last_block_before(_ts_utc(DAY0_DATE + timedelta(days=1)), ts, pin, a.hi)
        print(json.dumps({"pin_block": pin, "day0_block": d0}))
        return 0
    if (a.verify_endpoint or g4.REPLAY_ENDPOINT[a.walk_endpoint]) == a.walk_endpoint:
        raise Abort("walk and verify endpoints must differ")
    assert_producers_clean(PRODUCER_FILES, REPO)
    outdir = prepare_outdir(a.out, a.repo_root, a.resume)
    arms = load_universe(a.membership)
    cfg = {"arms": arms, "signed0": load_signed_day0(a.signed_cov, a.signed_panel), "pin": a.pin_block, "day0": a.day0_block,
           "outdir": outdir, "walk_ep": a.walk_endpoint, "verify_ep": a.verify_endpoint or g4.REPLAY_ENDPOINT[a.walk_endpoint],
           "max_mismatch_frac": a.max_mismatch_frac, "max_reverify_frac": a.max_reverify_frac, "reverify_fraction": a.reverify_fraction,
           "input_hashes": {k: _sha256_file(p) for k, p in (("membership", a.membership), ("signed_cov", a.signed_cov),
                                                            ("signed_panel", a.signed_panel))}}
    s = run_extraction(cfg, chain)
    print(json.dumps(s["counts_by_arm"], sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Abort as e:
        print(f"[o1] ABORT: {e}", file=sys.stderr)
        sys.exit(2)
