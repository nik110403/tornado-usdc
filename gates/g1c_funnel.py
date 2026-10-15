"""Gate 1c -- control-side balance funnel probe (Q5, appended after gate 1b).

Gate 1b (S3) yields a control sampling frame -- 172,296 EOA survivors with an
implied inverse-probability weight -- but no balance, floor or holding-type
information: none of the numbers the MDE (S4) and the Q2 re-opening are
waiting on (control-side floor-pass rate, coin split, dual-holding rate)
exist yet. This gate draws a seeded random 2,000 of the g1b survivors and
fetches USDC + USDT `balanceOf` at the pre-period start and end blocks for
each, then reports the funnel: positive-balance share, a two-block PROXY for
gate 1's $100/14-day floor, coin split, and the dual-holding rate compared
directly against gate 1's floor-passing exposed cohort (4,508 wallets, 1,117
dual holders, 24.8% -- out/g1/crosstab.md).

Not a SPEC S3 change -- QUESTIONS.md Q5 authorizes this as an appended probe,
scope-noted rather than folded into g1b_frame.py. balanceOf batching
(_batch_post/_batch_balance_of, endpoint `mevblocker`) is copied verbatim
from gates/g1_cohort.py, same precedent g1b_frame.py already set for its own
copy of _batch_post/_batch_get_code: g1_cohort.py does not expose these as a
lib, and lib/rpc.py is out of this item's touch scope. _write_table (CSV +
schema.json sidecar) is copied from gates/g1b_frame.py verbatim ("same style
as g1b" per the brief).

Deviation (receipts): every other gate's receipts.jsonl is populated by
lib.rpc.get_logs's own tiling-scoped range-fetch receipts; point queries
(g0's _eth_call/_get_code, g1's _batch_get_code/_batch_balance_of) are
documented precedent as bypassing receipts.jsonl entirely, since there is no
block span to tile for a point query. This gate has ONLY point queries -- no
eth_getLogs call at all -- so following that precedent literally would leave
receipts.jsonl empty, and the brief explicitly requires "receipts present"
as an acceptance check. Resolved by writing one receipt per balanceOf batch
POST (32 total: 4 [coin x block] series x 8 chunks of 250) with fields
{gate, endpoint, coin, block, addressCount, sha256(payload), t} -- the
closest point-query analog of the SPEC's range-fetch receipt shape.
"""
import csv
import hashlib
import json
import os
import random
import subprocess
import time
import urllib.error
import urllib.request

from lib import keccak, rpc

_HERE = os.path.dirname(os.path.abspath(__file__))
_OUT_DIR = os.path.join(_HERE, "..", "out", "g1c")
_G1B_SURVIVORS_PATH = os.path.join(_HERE, "..", "out", "g1b", "frame_survivors.csv")
_G1_CROSSTAB_PATH = os.path.join(_HERE, "..", "out", "g1", "crosstab.md")
_RECEIPTS_PATH = os.path.join(_OUT_DIR, "receipts.jsonl")

USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
COINS = {"USDC": USDC, "USDT": USDT}

OR = "mevblocker"  # same endpoint g1_cohort.py uses for its batched balanceOf calls

BALANCE_OF_SELECTOR = keccak.selector("balanceOf(address)")
assert BALANCE_OF_SELECTOR == "0x70a08231", BALANCE_OF_SELECTOR

FUNNEL_SEED = 20220609  # pre-period start date (2022-06-09), YYYYMMDD -- same convention as g1b's WINDOW_SEED
SAMPLE_N = 2000
PROXY_FLOOR_UNITS = 100 * 10 ** 6  # $100 in combined 6-decimal base units -- matches g1_cohort.py's FLOOR_MIN_UNITS

_NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, OSError)

SCHEMA_VERSION = 1


# --- batched JSON-RPC eth_call (copied from gates/g1_cohort.py -- see module
# docstring for why this is a copy, not an import). ---

class _BatchError(Exception):
    pass


def _batch_post(endpoint, calls, max_retries=4, backoff_s=0.5):
    """calls: list of (method, params). Returns list of raw `result` values
    in the same order as `calls`, matched by request id (not response
    order). Retries transient failures with backoff; on a 4xx that looks
    size-related, bisects the batch and recurses. Raises RuntimeError if a
    call still can't be resolved after that -- never silently dropped."""
    if not calls:
        return []
    cfg = rpc.ENDPOINTS[endpoint]
    payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)]
    req = urllib.request.Request(
        cfg["url"], data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": rpc.USER_AGENT},
        method="POST",
    )
    last_err = None
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = json.loads(resp.read())
            by_id = {}
            for item in raw:
                if "error" in item:
                    raise _BatchError(f"{endpoint} batch id={item.get('id')} -> {item['error']}")
                by_id[item["id"]] = item["result"]
            return [by_id[i] for i in range(len(calls))]
        except urllib.error.HTTPError as e:
            last_err = e
            if len(calls) > 1 and e.code in (400, 406, 413, 429):
                mid = len(calls) // 2
                return (_batch_post(endpoint, calls[:mid], max_retries, backoff_s)
                        + _batch_post(endpoint, calls[mid:], max_retries, backoff_s))
            time.sleep(backoff_s * (2 ** attempt))
        except (_NETWORK_ERRORS + (_BatchError,)) as e:
            last_err = e
            time.sleep(backoff_s * (2 ** attempt))
    raise RuntimeError(f"{endpoint} batch of {len(calls)} failed after {max_retries} retries: {last_err}")


def _balance_of_data(addr):
    return BALANCE_OF_SELECTOR + addr[2:].lower().rjust(64, "0")


def _hex_to_int(h):
    if h in (None, "0x", "0x0", ""):
        return 0
    return int(h, 16)


def _batch_balance_of_receipted(endpoint, coin_name, token, addrs, block, batch_size=250):
    """Same chunking as g1_cohort.py's _batch_balance_of, plus one receipt
    per chunk appended to out/g1c/receipts.jsonl (see module docstring
    Deviation note)."""
    block_hex = hex(block)
    out = {}
    for i in range(0, len(addrs), batch_size):
        chunk = addrs[i:i + batch_size]
        calls = [("eth_call", [{"to": token, "data": _balance_of_data(a)}, block_hex]) for a in chunk]
        payload_bytes = json.dumps(
            [{"jsonrpc": "2.0", "id": j, "method": m, "params": p} for j, (m, p) in enumerate(calls)],
            sort_keys=True,
        ).encode()
        results = _batch_post(endpoint, calls)
        for a, r in zip(chunk, results):
            out[a] = _hex_to_int(r)
        with open(_RECEIPTS_PATH, "a") as f:
            f.write(json.dumps({
                "gate": "g1c", "endpoint": endpoint, "coin": coin_name, "block": block,
                "addressCount": len(chunk), "sha256": hashlib.sha256(payload_bytes).hexdigest(),
                "t": time.time(),
            }, sort_keys=True) + "\n")
    return out


# --- table writer: CSV + schema.json sidecar (copied from gates/g1b_frame.py) ---

def _git_commit():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=_HERE, text=True).strip()
    except (subprocess.CalledProcessError, OSError, FileNotFoundError):
        return "unknown"


def _write_table(name, rows, columns, primary_key, input_hashes, notes=None):
    fieldnames = [c[0] for c in columns]
    pk = primary_key if isinstance(primary_key, (list, tuple)) else (primary_key,)
    rows_sorted = sorted(rows, key=lambda r: tuple(r[k] for k in pk))

    csv_path = os.path.join(_OUT_DIR, f"{name}.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows_sorted:
            w.writerow(r)
    with open(csv_path, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()

    schema = {
        "schema_version": SCHEMA_VERSION,
        "columns": [{"name": c[0], "dtype": c[1], "units": c[2]} for c in columns],
        "primary_key": list(pk),
        "null_policy": "empty string = missing / not applicable",
        "row_count": len(rows_sorted),
        "input_hashes": input_hashes,
        "producing_commit": _git_commit(),
        "sha256": sha,
    }
    if notes:
        schema["notes"] = notes
    with open(os.path.join(_OUT_DIR, f"{name}.schema.json"), "w") as f:
        json.dump(schema, f, indent=2, sort_keys=True)
        f.write("\n")
    return csv_path, sha, len(rows_sorted)


# --- classification (mirrors g1_cohort.py's holding_type / dual-holder logic) ---

def _classify(usdc_flags, usdt_flags):
    """usdc_flags/usdt_flags: tuples of bool, one per block checked (same
    length, same block order). 'both'/dual = positive in both coins at the
    SAME block, on at least one of the checked blocks -- the two-block
    analog of g1_cohort.py's holding_type=='both' ("index1 day with both
    coins strictly positive"). A wallet positive in both coins but never
    simultaneously (e.g. USDC only at the start block, USDT only at the
    end block) falls back to USDC-first, matching g1_cohort.py's documented
    tie-break for non-overlapping day-sets (SPEC S2 Deviation note)."""
    dual = any(u and t for u, t in zip(usdc_flags, usdt_flags))
    usdc_ever = any(usdc_flags)
    usdt_ever = any(usdt_flags)
    if dual:
        return "both"
    if usdc_ever:
        return "USDC-only"
    if usdt_ever:
        return "USDT-only"
    return "none"


def _split_counts(rows, usdc_keys, usdt_keys):
    """rows restricted to some subset; usdc_keys/usdt_keys: the column
    name(s) (1 for a single block, 2 for 'either') used to build the flag
    tuples passed to _classify. Returns a dict of holding_type -> count."""
    counts = {"USDC-only": 0, "USDT-only": 0, "both": 0, "none": 0}
    for r in rows:
        usdc_flags = tuple(r[k] > 0 for k in usdc_keys)
        usdt_flags = tuple(r[k] > 0 for k in usdt_keys)
        counts[_classify(usdc_flags, usdt_flags)] += 1
    return counts


def _fmt_split(counts, denom):
    total_positive = counts["USDC-only"] + counts["USDT-only"] + counts["both"]
    pct = lambda n: f"{n} ({100 * n / denom:.1f}%)" if denom else f"{n} (n/a)"
    return (f"USDC-only {pct(counts['USDC-only'])}, USDT-only {pct(counts['USDT-only'])}, "
            f"both {pct(counts['both'])}, none {pct(counts['none'])} -- "
            f"any-positive {pct(total_positive)}")


def run(cfg):
    os.makedirs(_OUT_DIR, exist_ok=True)
    if os.path.exists(_RECEIPTS_PATH):
        os.remove(_RECEIPTS_PATH)  # fresh ledger each run -- gitignored, regenerable

    pinned = cfg["blocks"]
    pre_start_block = pinned["pre_period_start"]["block"]
    pre_end_block = pinned["pre_period_end"]["block"]

    checks = []

    # --- load g1b survivors, draw the seeded sample ---
    with open(_G1B_SURVIVORS_PATH) as f:
        survivor_addrs = sorted({row["address"] for row in csv.DictReader(f)})
    print(f"[g1c] {len(survivor_addrs)} g1b survivors loaded from {_G1B_SURVIVORS_PATH}")

    sample = sorted(random.Random(FUNNEL_SEED).sample(survivor_addrs, SAMPLE_N))
    checks.append(("sample size == 2,000", len(sample) == SAMPLE_N, f"{len(sample)}"))

    # --- fetch balanceOf for USDC/USDT at pre_period_start/pre_period_end ---
    balances = {}  # (coin, block_label) -> {addr: units}
    block_of = {"start": pre_start_block, "end": pre_end_block}
    for coin_name, token in COINS.items():
        for block_label, block in block_of.items():
            print(f"[g1c] balanceOf {coin_name} @ {block_label} ({block}) for {len(sample)} addrs on {OR} (batched)")
            balances[(coin_name, block_label)] = _batch_balance_of_receipted(OR, coin_name, token, sample, block)

    coverage_ok = all(len(balances[k]) == SAMPLE_N and set(balances[k]) == set(sample) for k in balances)
    n_calls = sum(len(v) for v in balances.values())
    checks.append((
        "4x2,000 balance calls all returned (no None)", coverage_ok and n_calls == 4 * SAMPLE_N,
        f"{n_calls} values across {len(balances)} series" if coverage_ok else "missing/extra addresses in >=1 series",
    ))

    receipts_n = 0
    if os.path.exists(_RECEIPTS_PATH):
        with open(_RECEIPTS_PATH) as f:
            receipts_n = sum(1 for _ in f)
    checks.append(("receipts present", receipts_n > 0, f"{receipts_n} receipts"))

    any_positive_call = any(v > 0 for series in balances.values() for v in series.values())
    checks.append(("at least one positive balance observed", any_positive_call, str(any_positive_call)))

    # --- build funnel_sample.csv rows ---
    funnel_rows = []
    for addr in sample:
        funnel_rows.append({
            "wallet_id": None,  # filled below once wallet_id assignment is known
            "address": addr,
            "usdc_start": balances[("USDC", "start")][addr],
            "usdc_end": balances[("USDC", "end")][addr],
            "usdt_start": balances[("USDT", "start")][addr],
            "usdt_end": balances[("USDT", "end")][addr],
        })
    wallet_id = {a: i for i, a in enumerate(sample)}  # sample is already sorted by address
    for r in funnel_rows:
        r["wallet_id"] = wallet_id[r["address"]]

    funnel_columns = [
        ("wallet_id", "int", "dense id, assigned by sorted address within this gate's 2,000-address sample"),
        ("address", "str", "0x, 42 chars, lowercase"),
        ("usdc_start", "int", "USDC balanceOf base units (6 dec) @ pre_period_start"),
        ("usdc_end", "int", "USDC balanceOf base units (6 dec) @ pre_period_end"),
        ("usdt_start", "int", "USDT balanceOf base units (6 dec) @ pre_period_start"),
        ("usdt_end", "int", "USDT balanceOf base units (6 dec) @ pre_period_end"),
    ]
    receipt_shas = []
    if os.path.exists(_RECEIPTS_PATH):
        with open(_RECEIPTS_PATH) as f:
            receipt_shas = [json.loads(line)["sha256"] for line in f if line.strip()]
    funnel_path, funnel_sha, funnel_n = _write_table(
        "funnel_sample", funnel_rows, funnel_columns, "address", input_hashes=receipt_shas,
        notes="Two-block (pre_period_start, pre_period_end) balanceOf snapshot on a seeded 2,000 "
              "sample of gate 1b's control frame survivors -- see summary.md for the derived funnel.",
    )

    # --- funnel numbers ---
    for r in funnel_rows:
        r["total_start"] = r["usdc_start"] + r["usdt_start"]
        r["total_end"] = r["usdc_end"] + r["usdt_end"]
        r["mean_total_units"] = (r["total_start"] + r["total_end"]) // 2
        r["any_positive"] = r["usdc_start"] > 0 or r["usdc_end"] > 0 or r["usdt_start"] > 0 or r["usdt_end"] > 0
        r["proxy_pass"] = r["mean_total_units"] >= PROXY_FLOOR_UNITS

    def _funnel_block(rows, label):
        n = len(rows)
        n_positive = sum(1 for r in rows if r["any_positive"])
        n_proxy = sum(1 for r in rows if r["proxy_pass"])
        proxy_rows = [r for r in rows if r["proxy_pass"]]

        split_start = _split_counts(rows, ("usdc_start",), ("usdt_start",))
        split_end = _split_counts(rows, ("usdc_end",), ("usdt_end",))
        split_either = _split_counts(rows, ("usdc_start", "usdc_end"), ("usdt_start", "usdt_end"))
        dual_rate = 100 * split_either["both"] / n if n else 0.0

        lines = [
            f"## {label} (n={n})",
            "",
            f"Positive stablecoin balance at either block: {n_positive} ({100 * n_positive / n:.1f}%)." if n else "n=0",
            f"Mean-of-two-blocks >= $100-equivalent proxy: {n_proxy} ({100 * n_proxy / n:.1f}%)." if n else "",
            "",
            f"Coin split @ pre_period_start: {_fmt_split(split_start, n)}",
            f"Coin split @ pre_period_end:   {_fmt_split(split_end, n)}",
            f"Coin split @ either block:     {_fmt_split(split_either, n)}",
            "",
            f"Dual-holding rate (both coins positive at the same block, either block, / {label.lower()} n): "
            f"{split_either['both']}/{n} = {dual_rate:.1f}%.",
        ]
        return lines, proxy_rows, dual_rate, n_proxy, split_either["both"]

    full_lines, proxy_rows, full_dual_rate, n_proxy, full_dual_n = _funnel_block(funnel_rows, "Full sample")
    proxy_lines, _, proxy_dual_rate, _, proxy_dual_n = _funnel_block(proxy_rows, "$100-proxy subset")

    passed = sum(1 for _, ok, _ in checks if ok)
    failed = [(n, d) for n, ok, d in checks if not ok]

    g1_floor_n, g1_dual_n, g1_dual_pct = 4508, 1117, 24.8
    comparison_line = (
        f"Gate 1 floor-passing exposed cohort: {g1_floor_n} wallets, {g1_dual_n} dual holders "
        f"({g1_dual_pct}%, out/g1/crosstab.md). Control-side $100-proxy subset ({n_proxy} wallets): "
        f"{proxy_dual_n} dual holders ({proxy_dual_rate:.1f}%)."
    )

    summary_lines = [
        "# Gate 1c -- control-side balance funnel probe (Q5)",
        "",
        f"Sample: {len(sample)} addresses drawn seed={FUNNEL_SEED} from gate 1b's {len(survivor_addrs)} "
        "control frame survivors (`out/g1b/frame_survivors.csv`).",
        f"balanceOf: USDC + USDT @ pre_period_start ({pre_start_block}) and pre_period_end ({pre_end_block}), "
        f"batched eth_call on {OR}.",
        "",
        "PROXY notice: gate 1's floor is pre-period mean total holding >= $100 **and** >= 14 pre-period "
        "days with nonzero balance, computed on a 60-day daily panel. This probe has only two balanceOf "
        "snapshots (start, end) -- it cannot compute a day count, so \"$100-proxy pass\" below means "
        "mean(total_start, total_end) >= 100,000,000 base units ($100-equivalent) only. It is NOT gate "
        "1's floor and is not comparable to it in isolation -- only the dual-holding rate comparison below is.",
        "",
        *full_lines,
        "",
        *proxy_lines,
        "",
        "## Comparison to gate 1's floor-passing exposed cohort",
        "",
        f"**{comparison_line}**",
        "",
        "## Acceptance checks",
        "",
        "| check | result | detail |",
        "|---|---|---|",
    ]
    for name, ok, detail in checks:
        summary_lines.append(f"| {name} | {'PASS' if ok else 'FAIL'} | {detail} |")
    summary_lines += [
        "",
        f"{passed}/{len(checks)} checks passed.",
        "",
        "## Outputs",
        "",
        f"- `funnel_sample.csv` ({funnel_n} rows, sha256 `{funnel_sha}`)",
        f"- `receipts.jsonl` ({receipts_n} receipts)",
    ]
    with open(os.path.join(_OUT_DIR, "summary.md"), "w") as f:
        f.write("\n".join(summary_lines) + "\n")

    print(f"[g1c] {passed}/{len(checks)} acceptance checks passed")
    if failed:
        for name, detail in failed:
            print(f"[g1c] FAIL: {name} -- {detail}")
        return 1
    return 0
