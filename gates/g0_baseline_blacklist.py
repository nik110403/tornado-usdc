"""Baseline-only issuer blacklist history: contract deployment -> pre_period_end (15,298,282).

SPEC S15 / prospective-access rule. The lifetime g0 census (out/g0/*) is never loaded here. This gate
fetches the issuer add/remove events afresh -- Circle USDC `Blacklisted`/`UnBlacklisted` and Tether
USDT `AddedBlackList`/`RemovedBlackList` -- from each contract's deployment block through
`pre_period_end` inclusive, and writes an immutable generation

    out/recovery/blacklist_baseline/<generation>/{events.csv, receipts.jsonl, manifest.json}

manifest.json is written last (a directory without it is not a generation). It pins producer commit,
config hash, per-coin span, input/output sha256, row counts and the per-coin coverage proof.

Acquisition is `lib.rpc.ChunkStore` (provider-scoped to ENDPOINT, no fallback, quota -> bounded
cool-down then hard failure, result cap -> bisect, atomic per-chunk checkpoints bound to commit /
config / inputs / query shape): an interrupted run resumes without refetching completed chunks.

Optional reuse of the deployment-extension output (gates/g0_blacklist_ext.py, deployment ->
census_start - 1) is accepted ONLY through `verify_extension`: caller-supplied sha256 of the csv and of
its receipts file, receipts that tile each contract's [deployment, census_start - 1] on ENDPOINT, and
receipt log counts equal to the csv rows. Without that proof the full span is fetched.

Consumers use `load_baseline(dir, consumer)`: it re-verifies the manifest and file hashes, drops events
after the consumer's own cutoff (`june` -> june_end, `main` -> pre_period_end) and serves
opening state at a block and any-time status over [a, b]. No fallback to old flags.

Run (not by this builder): python3 -m gates.g0_baseline_blacklist
"""
import csv
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict

from lib import blocks, keccak, rpc

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_ROOT = os.path.abspath(os.path.join(_HERE, "..", "out", "recovery", "blacklist_baseline"))
POPULATION_ID = "blacklist_baseline"
SCHEMA_VERSION = 1
ENDPOINT = "mevblocker"  # single-address filtered getLogs: the shape its 50,000-block cap was measured for
WINDOW = 50_000
GATE = "g0_baseline_blacklist"

USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
ADDRESS = {"USDC": USDC, "USDT": USDT}
# first block with non-empty eth_getCode, binary-searched 2026-10-16 on mevblocker AND drpc (identical); see g0_blacklist_ext
DEPLOY_BLOCK = {"USDC": 6082465, "USDT": 4634748}
COINS = ("USDC", "USDT")
TOPICS = {
    "USDC": {"Blacklisted": keccak.topic("Blacklisted(address)"),
             "UnBlacklisted": keccak.topic("UnBlacklisted(address)")},
    "USDT": {"AddedBlackList": keccak.topic("AddedBlackList(address)"),
             "RemovedBlackList": keccak.topic("RemovedBlackList(address)")},
}
KIND = {"Blacklisted": "add", "UnBlacklisted": "remove", "AddedBlackList": "add", "RemovedBlackList": "remove"}
CSV_COLS = ["coin", "address", "event", "block", "tx_hash", "log_index"]
CONSUMER_CUTOFF = {"june": "june_end", "main": "pre_period_end"}  # name of the pinned block each consumer is cut at


class BaselineError(RuntimeError):
    pass


_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_GEN_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")  # no leading dot, no separators
OUTPUT_FILES = ("events.csv", "receipts.jsonl")


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_commit():
    """HEAD sha; a failure is an error -- the manifest never records an "unknown" producer."""
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=_HERE, text=True).strip()
    except (subprocess.CalledProcessError, OSError) as e:
        raise BaselineError(f"cannot determine producer commit: {e}") from e
    if not _HEX40.match(head):
        raise BaselineError(f"unexpected git HEAD {head!r}")
    return head


def _producer_identity(head):
    """Chunk-binding identity: HEAD + hash of the producer sources (uncommitted edits change it)."""
    return rpc.producer_identity([os.path.join(_HERE, "..", "lib", "rpc.py"), os.path.abspath(__file__)], head=head)


def _decode_address(log):
    """Indexed (topics[1]) or unindexed (first data word) single-address event."""
    if len(log["topics"]) >= 2:
        raw = log["topics"][1]
        ok = isinstance(raw, str) and re.fullmatch(r"0x0{24}[0-9a-fA-F]{40}", raw)
        if not ok:
            raise BaselineError(f"malformed indexed address topic {raw!r}")
        return "0x" + raw[-40:].lower()
    data = log.get("data")
    if not (isinstance(data, str) and re.fullmatch(r"0x0{24}[0-9a-fA-F]{40}(?:[0-9a-fA-F]{64})*", data)):
        raise BaselineError(f"malformed unindexed address data {str(data)[:80]!r}")
    return "0x" + data[2:][24:64].lower()


def config_sha256(span_end):
    """Hash of everything that shapes the query: endpoint caps, contracts, deployment blocks, topics, span."""
    cfg = rpc._endpoint_cfg(ENDPOINT)
    return rpc.canonical_sha256({
        "endpoint": ENDPOINT, "window": WINDOW, "span_end": span_end,
        "caps": {k: cfg.get(k) for k in ("block_range_cap", "shape_caps", "unmeasured_shape_cap", "result_cap")},
        "address": ADDRESS, "deploy": DEPLOY_BLOCK, "topics": TOPICS})


def _topic_filter(coin):
    return [list(TOPICS[coin].values())]


def rows_from_logs(coin, logs):
    names = {v.lower(): k for k, v in TOPICS[coin].items()}
    rows = []
    for l in logs:
        if l["address"].lower() != ADDRESS[coin].lower():
            raise BaselineError(f"{coin}: log from {l['address']}, expected {ADDRESS[coin]}")
        rows.append({"coin": coin, "address": _decode_address(l), "event": names[l["topics"][0].lower()],
                     "block": rpc._log_block_num(l), "tx_hash": l["transactionHash"].lower(),
                     "log_index": rpc._log_event_key(l)[1]})
    return rows


# --- deployment-extension reuse: manifest/hash/coverage proof, checked in code -----------------

def _require_sha(name, value):
    if not (isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)):
        raise BaselineError(f"extension reuse requires an explicit sha256 for {name}")
    return value


def verify_extension(csv_path, receipts_path, expected_csv_sha256, expected_receipts_sha256, census_start):
    try:
        return _verify_extension(csv_path, receipts_path, expected_csv_sha256, expected_receipts_sha256, census_start)
    except (KeyError, TypeError, ValueError, IndexError) as e:  # malformed receipt/csv record
        raise BaselineError(f"extension files malformed: {e!r}") from e


def _verify_extension(csv_path, receipts_path, expected_csv_sha256, expected_receipts_sha256, census_start):
    """Return the extension rows ONLY if hashes and coverage are proven; else raise BaselineError.
    Proof: both files hash to the caller-pinned values; the receipts form exactly two contiguous runs on
    ENDPOINT, USDC [deploy, census_start-1] then USDT [deploy, census_start-1], one address, this gate's
    topic filter; each run's logCount sum equals that coin's csv row count; rows are in-span, known
    events, unique by (tx_hash, log_index)."""
    ext_end = census_start - 1
    for name, path, want in (("csv", csv_path, expected_csv_sha256), ("receipts", receipts_path, expected_receipts_sha256)):
        if not os.path.isfile(path):
            raise BaselineError(f"extension {name} {path} not found")
        if _sha256_file(path) != _require_sha(name, want):
            raise BaselineError(f"extension {name} {path} does not match the pinned sha256")
    recs = []
    with open(receipts_path) as f:
        for line in f:
            if line.strip():
                recs.append(json.loads(line))
    runs = []
    for r in recs:
        if r.get("endpoint") != ENDPOINT or r.get("addressCount") != 1:
            raise BaselineError(f"extension receipt not from {ENDPOINT} single-address: {r}")
        if r["logCount"] >= rpc.result_cap(ENDPOINT):
            raise BaselineError(f"extension receipt at/over the result cap (possible truncation): {r}")
        if "address" in r and r["address"].lower() not in {a.lower() for a in ADDRESS.values()}:
            raise BaselineError(f"extension receipt for an unexpected address: {r}")
        if runs and r["fromBlock"] == runs[-1][-1]["toBlock"] + 1:
            runs[-1].append(r)
        else:
            runs.append([r])
    if len(runs) != 2:
        raise BaselineError(f"extension receipts form {len(runs)} contiguous runs, expected 2 (USDC, USDT)")
    with open(csv_path, newline="") as f:
        rdr = csv.DictReader(f)
        if rdr.fieldnames != CSV_COLS:
            raise BaselineError(f"extension csv header {rdr.fieldnames} != {CSV_COLS}")
        rows = [{**r, "block": int(r["block"]), "log_index": int(r["log_index"])} for r in rdr]
    seen = set()
    for coin, run in zip(COINS, runs):
        if run[0]["fromBlock"] != DEPLOY_BLOCK[coin] or run[-1]["toBlock"] != ext_end:
            raise BaselineError(f"extension {coin} receipts cover [{run[0]['fromBlock']},{run[-1]['toBlock']}], "
                                f"expected [{DEPLOY_BLOCK[coin]},{ext_end}]")
        if any("address" in r and r["address"].lower() != ADDRESS[coin].lower() for r in run):
            raise BaselineError(f"extension {coin} receipts record a different contract address")
        if any({t.lower() for t in r["topics"][0]} != {t.lower() for t in _topic_filter(coin)[0]} for r in run):
            raise BaselineError(f"extension {coin} receipts were not this gate's topic filter")
        mine = [r for r in rows if r["coin"] == coin]
        if sum(r["logCount"] for r in run) != len(mine):
            raise BaselineError(f"extension {coin}: receipts count {sum(r['logCount'] for r in run)} != csv rows {len(mine)}")
        for r in mine:
            k = (r["tx_hash"], r["log_index"])
            if k in seen or r["event"] not in TOPICS[coin] or not DEPLOY_BLOCK[coin] <= r["block"] <= ext_end:
                raise BaselineError(f"extension row invalid/duplicate/out of span: {r}")
            seen.add(k)
    if len(rows) != len(seen):
        raise BaselineError("extension csv has rows for an unknown coin")
    return rows


# --- producer -------------------------------------------------------------------------------

def _lock(path):
    fh = open(path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        fh.close()
        raise BaselineError(f"{path} is locked by another run") from e
    return fh


def _write_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        w.writeheader()
        w.writerows(rows)
        f.flush()
        os.fsync(f.fileno())


def run(cfg=None):
    """cfg keys (all optional): generation; extension = {csv, receipts, csv_sha256, receipts_sha256}."""
    cfg = cfg or {}
    pinned = blocks.load_pinned()
    census_start = pinned["census_start"]["block"]
    span_end = pinned["pre_period_end"]["block"]
    commit = _git_commit()
    gen = cfg.get("generation") or "gen-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + commit[:8]
    if not _GEN_RE.match(gen):
        raise BaselineError(f"invalid generation name {gen!r}")
    final = os.path.join(OUT_ROOT, gen)
    if os.path.exists(final):
        raise BaselineError(f"{final} exists; generations are immutable")
    rpc.assert_chain_id(ENDPOINT)

    ext = cfg.get("extension")
    ext_rows, input_hashes, start_from = [], {}, {c: DEPLOY_BLOCK[c] for c in COINS}
    if ext:
        ext_rows = verify_extension(ext["csv"], ext["receipts"], ext.get("csv_sha256"), ext.get("receipts_sha256"), census_start)
        input_hashes = {"extension_csv": ext["csv_sha256"], "extension_receipts": ext["receipts_sha256"]}
        start_from = {c: census_start for c in COINS}
    cfg_sha = config_sha256(span_end)
    ident = _producer_identity(commit)
    work = os.path.join(OUT_ROOT, "_work", f"{rpc.canonical_sha256(ident)[:16]}-{cfg_sha[:12]}")
    os.makedirs(work, exist_ok=True)
    lock = _lock(os.path.join(work, ".lock"))
    try:
        rows, proofs, receipts = list(ext_rows), {}, []
        for coin in COINS:
            binding = rpc.make_binding(ident, cfg_sha, input_hashes, ENDPOINT, ADDRESS[coin], _topic_filter(coin))
            store = rpc.ChunkStore(os.path.join(work, coin), binding)
            lo = start_from[coin]
            rpc.acquire_logs(store, lo, span_end, WINDOW, guard=lambda b: _guard(b, span_end),
                             on_chunk=lambda rec, c=coin: print(f"[{GATE}] {c} chunk [{rec['lo']},{rec['hi']}] {rec['logCount']} logs", flush=True))
            proofs[coin] = store.coverage_proof(lo, span_end)
            rows.extend(rows_from_logs(coin, store.logs(lo, span_end)))
            receipts.extend({"coin": coin, **{k: v for k, v in store.read_chunk(a, b).items() if k != "logs"}}
                            for a, b in sorted(store._index))
        keys = [(r["coin"], r["tx_hash"], r["log_index"]) for r in rows]
        if len(set(keys)) != len(keys):
            raise BaselineError("duplicate (coin, tx_hash, log_index) across extension and fresh rows")
        rows.sort(key=lambda r: (r["coin"], r["block"], r["log_index"]))
        stage = os.path.join(OUT_ROOT, ".staging-" + gen)
        os.makedirs(stage)
        _write_csv(os.path.join(stage, "events.csv"), rows)
        with open(os.path.join(stage, "receipts.jsonl"), "w") as f:
            for r in receipts:
                f.write(json.dumps(r, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        manifest = {
            "population_id": POPULATION_ID, "generation_id": gen, "producing_commit": commit,
            "producer": "gates/g0_baseline_blacklist.py", "config_sha256": cfg_sha,
            "baseline_start_date": None, "baseline_end_date": pinned["pre_period_end"]["timestamp"][:10],
            "excluded_dates": [], "cutoff_block": span_end,
            "span": {c: {"start": DEPLOY_BLOCK[c], "end": span_end} for c in COINS},
            "endpoint": ENDPOINT, "input_file_sha256": input_hashes,
            "extension_reuse": {"census_start": census_start, "rows": len(ext_rows)} if ext else None,
            "output_file_sha256": {n: _sha256_file(os.path.join(stage, n)) for n in ("events.csv", "receipts.jsonl")},
            "row_counts": {"events.csv": len(rows), **{c: sum(1 for r in rows if r["coin"] == c) for c in COINS}},
            "coverage": proofs, "schema_version": SCHEMA_VERSION,
        }
        with open(os.path.join(stage, "manifest.json"), "w") as f:
            json.dump(manifest, f, sort_keys=True, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(stage, final)
        _fsync_dir(OUT_ROOT)
        print(f"[{GATE}] wrote {final}: {len(rows)} events", flush=True)
        return 0
    finally:
        lock.close()


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _guard(block, span_end):
    if block > span_end:
        raise BaselineError(f"refusing to read block {block} > pre_period_end {span_end}")


# --- consumer API -----------------------------------------------------------------------------

def state_at(events, block):
    """events: iterable of (block, log_index, kind) for ONE address and ONE coin. Blacklisted at `block`
    iff the last event with block <= `block` is an add."""
    state = False
    for _, _, kind in sorted(e for e in events if e[0] <= block):
        state = kind == "add"
    return state


def any_time(events, start, end):
    """Blacklisted at `start` (opening state, from ALL history incl. pre-census) or an add in (start, end]."""
    ev = [e for e in events if e[0] <= end]
    return state_at(ev, start) or any(k == "add" for b, _, k in ev if b > start)


class Baseline:
    def __init__(self, by_coin, cutoff, consumer, manifest_sha256):
        self._ev = by_coin
        self.cutoff = cutoff
        self.consumer = consumer
        self.manifest_sha256 = manifest_sha256

    def _check(self, block):
        if block > self.cutoff:
            raise BaselineError(f"{self.consumer} consumer may not query block {block} > its cutoff {self.cutoff}")

    def state_at(self, coin, addr, block):
        self._check(block)
        return state_at(self._ev[coin].get(addr.lower(), ()), block)

    def any_time(self, coin, addr, start, end):
        self._check(end)
        return any_time(self._ev[coin].get(addr.lower(), ()), start, end)

    def events(self, universe_set):
        """{coin: {address: [(block, log_index, kind)]}} for the universe, <= cutoff; same shape as
        g4_controls._load_blacklist_events."""
        out = {c: defaultdict(list) for c in COINS}
        for c in COINS:
            for a in universe_set:
                if a in self._ev[c]:
                    out[c][a] = list(self._ev[c][a])
        return out


def load_baseline(directory, consumer, pinned=None):
    """Verify manifest + file hashes + span, then serve events up to the consumer's own cutoff."""
    if consumer not in CONSUMER_CUTOFF:
        raise BaselineError(f"unknown consumer {consumer!r}; expected one of {sorted(CONSUMER_CUTOFF)}")
    pinned = pinned if pinned is not None else blocks.load_pinned()
    mpath = os.path.join(directory, "manifest.json")
    if not os.path.isfile(mpath):
        raise BaselineError(f"{mpath} missing: not a baseline generation")
    try:
        with open(mpath) as f:
            m = json.load(f)
    except ValueError as e:
        raise BaselineError(f"{mpath} unreadable: {e}") from e
    try:
        if m["population_id"] != POPULATION_ID or m["schema_version"] != SCHEMA_VERSION:
            raise BaselineError("manifest population_id/schema_version mismatch")
        if not (isinstance(m["producing_commit"], str) and _HEX40.match(m["producing_commit"])):
            raise BaselineError(f"manifest producing_commit {m['producing_commit']!r} is not a git sha")
        if set(m["output_file_sha256"]) != set(OUTPUT_FILES):
            raise BaselineError(f"manifest output_file_sha256 keys {sorted(m['output_file_sha256'])} != {sorted(OUTPUT_FILES)}")
        pre_end = pinned["pre_period_end"]["block"]
        if m["cutoff_block"] != pre_end:
            raise BaselineError(f"manifest cutoff_block {m['cutoff_block']} != pinned pre_period_end {pre_end}")
        for c in COINS:
            if m["span"][c] != {"start": DEPLOY_BLOCK[c], "end": pre_end}:
                raise BaselineError(f"manifest span for {c} is {m['span'][c]}, expected [{DEPLOY_BLOCK[c]},{pre_end}]")
        if m["config_sha256"] != config_sha256(pre_end):
            raise BaselineError("manifest config_sha256 differs from this code's query configuration")
        ext_start = m["extension_reuse"]["census_start"] if m.get("extension_reuse") else None
        for c in COINS:
            want_lo = ext_start if ext_start is not None else DEPLOY_BLOCK[c]
            cov = m["coverage"][c]
            if (cov["lo"], cov["hi"]) != (want_lo, pre_end):
                raise BaselineError(f"manifest coverage for {c} is [{cov['lo']},{cov['hi']}], expected [{want_lo},{pre_end}]")
        for name, want in m["output_file_sha256"].items():
            if _sha256_file(os.path.join(directory, name)) != want:
                raise BaselineError(f"{name} does not match the manifest sha256")
    except (KeyError, TypeError) as e:
        raise BaselineError(f"manifest malformed: {e!r}") from e
    try:
        cutoff = pinned[CONSUMER_CUTOFF[consumer]]["block"]
    except KeyError as e:
        raise BaselineError(f"{CONSUMER_CUTOFF[consumer]} is not pinned in config/blocks.json") from e
    ev = {c: defaultdict(list) for c in COINS}
    counts, seen = defaultdict(int), set()
    with open(os.path.join(directory, "events.csv"), newline="") as f:
        rdr = csv.DictReader(f)
        if rdr.fieldnames != CSV_COLS:
            raise BaselineError(f"events.csv header {rdr.fieldnames} != {CSV_COLS}")
        for r in rdr:
            c, b, li = r["coin"], int(r["block"]), int(r["log_index"])
            if c not in COINS or r["event"] not in TOPICS[c] or not DEPLOY_BLOCK[c] <= b <= pre_end:
                raise BaselineError(f"events.csv row outside the proven span/event set: {r}")
            k = (c, r["tx_hash"], li)
            if k in seen:
                raise BaselineError(f"duplicate event {k}")
            seen.add(k)
            counts[c] += 1
            if b <= cutoff:
                ev[c][r["address"].lower()].append((b, li, KIND[r["event"]]))
    if m["row_counts"] != {"events.csv": sum(counts.values()), **{c: counts[c] for c in COINS}}:
        raise BaselineError("events.csv row counts differ from the manifest")
    return Baseline(ev, cutoff, consumer, _sha256_file(mpath))


if __name__ == "__main__":
    sys.exit(run())
