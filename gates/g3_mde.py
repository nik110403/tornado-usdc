"""Gate 3 -- pilot MDE on the wallet-level Delta. Built by S5.

Reads S2's persisted `out/g1/cohort.csv` and `out/g1/wallet_coin_day_pre.csv`
only (their sha256 is checked against the schema.json sidecars S2 wrote, and
both files' primary-key ordering is checked for strict monotonicity -- a
cheap proxy for "no duplicate key" that matches how S2 already writes these
tables, sorted by primary key). No RPC calls, no independent balance
reconstruction: every balance used here is S2's own reconstructed
`balance_units` and S2's own `pre_mean_total_units`.

500 floor-passing cohort wallets are drawn (seeded, without replacement;
fewer if fewer than 500 floor-pass -- reported either way). For each
sampled wallet the scaled outcome y_ic,t = balance_ic,t / pre_mean_total_i
is built at daily resolution for both coins over all T=60 pre-period days,
winsorized at the 1st/99th percentile (caps computed once, pooled over the
whole sampled panel, then applied everywhere so every breakdown below is a
subset of the same winsorized population). The wallet-day gap
d_it = y_iUSDC,t - y_iUSDT,t feeds a placebo Delta_i = mean(d, day_index
30..59) - mean(d, day_index 0..29) (pre-period "second half minus first
half", the only split available with no post-period data yet).

Every Delta_i (and therefore every SD(Delta) and MDE cell) is computed two
independent ways and the two are asserted equal (tolerance 1e-9, since both
paths do float division/aggregation in a different order): (a) direct
per-wallet aggregation -- slice the wallet's day series into the two
30-day halves and take the difference of the two means; (b) a per-wallet
OLS slope on a post-period indicator via the covariance/variance formula
(this is *not* the same code path as (a) -- for a balanced 30/30 split it
is algebraically identical to the difference of means, so agreement is a
correctness check on the aggregation logic, not an independent statistical
claim). `summary.md` prints both and the gate fails on any mismatch.
"""
import csv
import hashlib
import json
import math
import os
import random
import statistics
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_OUT_DIR = os.path.join(_HERE, "..", "out", "g3")
_G1_DIR = os.path.join(_HERE, "..", "out", "g1")

COHORT_CSV = os.path.join(_G1_DIR, "cohort.csv")
COHORT_SCHEMA = os.path.join(_G1_DIR, "cohort.schema.json")
WCDP_CSV = os.path.join(_G1_DIR, "wallet_coin_day_pre.csv")
WCDP_SCHEMA = os.path.join(_G1_DIR, "wallet_coin_day_pre.schema.json")

SAMPLE_SEED = 20260905
SAMPLE_SIZE = 500
T_DAYS = 60
MDE_CONST = 2.8
N_LEVELS = [500, 1500, 2000, 3300]
FLOOR_TIERS = [
    ("$0", 0),
    ("$100", 100_000_000),
    ("$1,000", 1_000_000_000),
    ("$10,000", 10_000_000_000),
]
WINSOR_LO_PCT, WINSOR_HI_PCT = 1, 99
DELTA_TOL = 1e-9
TARGET_MDES_PP = [10, 5]

# IDENT's own pilot, per SPEC S5 acceptance line -- literal comparison numbers.
IDENT_SD, IDENT_ICC, IDENT_MDE_PP, IDENT_N_PER_ARM = 1.44, 0.23, 13, 2000


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _percentile(sorted_vals, p):
    """Nearest-rank percentile (0 <= p <= 100) on an already-sorted list.
    Same convention as gates/g1b_frame.py's _percentile."""
    if not sorted_vals:
        return None
    k = max(0, min(len(sorted_vals) - 1, round(p / 100 * (len(sorted_vals) - 1))))
    return sorted_vals[k]


# --- S2 output readers (sha256 + strict-key-order verification inline) ---

def _load_cohort():
    if not os.path.exists(COHORT_CSV) or not os.path.exists(COHORT_SCHEMA):
        raise RuntimeError("out/g1/cohort.csv or its schema.json not found -- gate g3 needs gate g1 to have run first")
    with open(COHORT_SCHEMA) as f:
        schema = json.load(f)
    actual_sha = _sha256_file(COHORT_CSV)
    sha_ok = actual_sha == schema["sha256"]

    rows = {}
    keys_ok = True
    prev_key = None
    with open(COHORT_CSV, newline="") as f:
        for row in csv.DictReader(f):
            wid = int(row["wallet_id"])
            if prev_key is not None and wid <= prev_key:
                keys_ok = False
            prev_key = wid
            rows[wid] = {
                "address": row["address"],
                "arm": row["arm"],
                "holding_type": row["holding_type"],
                "pre_mean_total_units": int(row["pre_mean_total_units"]),
                "floor_pass": row["floor_pass"] == "True",
                "in_panel": row["in_panel"] == "True",
            }
    return rows, sha_ok, keys_ok, actual_sha, schema["sha256"]


def _load_wallet_coin_day_pre(sample_ids):
    """Single streaming pass: verifies sha256, checks the (wallet_id, coin,
    date) primary key is in strictly increasing order over the WHOLE file
    (no duplicate/out-of-order keys), and -- only for wallet_ids in
    `sample_ids` -- builds panel[wallet_id][coin][day_index] = balance_units.
    """
    if not os.path.exists(WCDP_CSV) or not os.path.exists(WCDP_SCHEMA):
        raise RuntimeError("out/g1/wallet_coin_day_pre.csv or its schema.json not found -- gate g3 needs gate g1 to have run first")
    with open(WCDP_SCHEMA) as f:
        schema = json.load(f)
    actual_sha = _sha256_file(WCDP_CSV)
    sha_ok = actual_sha == schema["sha256"]

    panel = defaultdict(lambda: defaultdict(dict))
    keys_ok = True
    prev_key = None
    n_rows = 0
    with open(WCDP_CSV, newline="") as f:
        for row in csv.DictReader(f):
            n_rows += 1
            wid = int(row["wallet_id"])
            key = (wid, row["coin"], row["date"])
            if prev_key is not None and key <= prev_key:
                keys_ok = False
            prev_key = key
            if wid in sample_ids:
                panel[wid][row["coin"]][int(row["day_index"])] = int(row["balance_units"])
    return panel, sha_ok, keys_ok, actual_sha, schema["sha256"], n_rows


# --- Delta computation: two independent paths ---

def _direct_agg_delta(d_series):
    """Path (a): slice the T=60 series into two 30-day halves, difference
    of means."""
    pre_half = d_series[:T_DAYS // 2]
    post_half = d_series[T_DAYS // 2:]
    return statistics.mean(post_half) - statistics.mean(pre_half)


def _ols_slope_delta(d_series):
    """Path (b): per-wallet OLS slope of d on a post-period 0/1 indicator,
    via the covariance/variance normal-equation formula -- a different
    computation from (a), algebraically identical for a balanced 30/30
    split."""
    post = [0.0] * (T_DAYS // 2) + [1.0] * (T_DAYS - T_DAYS // 2)
    n = len(d_series)
    xbar = sum(post) / n
    ybar = sum(d_series) / n
    num = sum((x - xbar) * (y - ybar) for x, y in zip(post, d_series))
    den = sum((x - xbar) ** 2 for x in post)
    return num / den


def _group_stats(wallet_ids, delta_a, delta_b):
    """Given a subset of wallet ids, return (n, sd_a, sd_b, max_abs_diff)."""
    vals_a = [delta_a[w] for w in wallet_ids]
    vals_b = [delta_b[w] for w in wallet_ids]
    n = len(vals_a)
    if n < 2:
        return n, None, None, 0.0
    sd_a = statistics.stdev(vals_a)
    sd_b = statistics.stdev(vals_b)
    max_diff = max((abs(a - b) for a, b in zip(vals_a, vals_b)), default=0.0)
    return n, sd_a, sd_b, max_diff


def _mde(sd, n_per_arm):
    return MDE_CONST * sd * math.sqrt(2.0 / n_per_arm)


def _n_needed(sd, target_mde):
    return math.ceil(2.0 * (MDE_CONST * sd / target_mde) ** 2)


def _icc_one_way(d_by_wallet, wallet_ids):
    """Balanced one-way random-effects ICC(1): (MSB - MSW) / (MSB + (T-1)*MSW)."""
    n = len(wallet_ids)
    wallet_means = {w: statistics.mean(d_by_wallet[w]) for w in wallet_ids}
    all_vals = [v for w in wallet_ids for v in d_by_wallet[w]]
    grand_mean = statistics.mean(all_vals)
    ssb = T_DAYS * sum((wallet_means[w] - grand_mean) ** 2 for w in wallet_ids)
    ssw = sum((v - wallet_means[w]) ** 2 for w in wallet_ids for v in d_by_wallet[w])
    msb = ssb / (n - 1)
    msw = ssw / (n * (T_DAYS - 1))
    icc = (msb - msw) / (msb + (T_DAYS - 1) * msw)
    return icc, msb, msw


def run(cfg):
    os.makedirs(_OUT_DIR, exist_ok=True)

    checks = []

    cohort, cohort_sha_ok, cohort_keys_ok, cohort_sha, cohort_sha_expected = _load_cohort()
    checks.append(("cohort.csv sha256 matches out/g1/cohort.schema.json", cohort_sha_ok,
                    f"actual {cohort_sha} expected {cohort_sha_expected}"))
    checks.append(("cohort.csv primary key (wallet_id) strictly increasing / no duplicates", cohort_keys_ok, "ok" if cohort_keys_ok else "out-of-order or duplicate key found"))

    floor_ids = sorted(w for w, r in cohort.items() if r["floor_pass"])
    print(f"[g3] {len(floor_ids)} floor-passing cohort wallets")
    sample_size = min(SAMPLE_SIZE, len(floor_ids))
    sample_ids = set(random.Random(SAMPLE_SEED).sample(floor_ids, sample_size))
    sample_note = (
        f"sampled {sample_size} of {len(floor_ids)} floor-passing wallets (seed {SAMPLE_SEED})"
        if sample_size == SAMPLE_SIZE else
        f"only {len(floor_ids)} floor-passing wallets exist (< {SAMPLE_SIZE}); used all {sample_size}"
    )
    print(f"[g3] {sample_note}")

    panel, wcdp_sha_ok, wcdp_keys_ok, wcdp_sha, wcdp_sha_expected, wcdp_rows = _load_wallet_coin_day_pre(sample_ids)
    checks.append(("wallet_coin_day_pre.csv sha256 matches out/g1/wallet_coin_day_pre.schema.json", wcdp_sha_ok,
                    f"actual {wcdp_sha} expected {wcdp_sha_expected}"))
    checks.append(("wallet_coin_day_pre.csv primary key (wallet_id,coin,date) strictly increasing / no duplicates",
                    wcdp_keys_ok, "ok" if wcdp_keys_ok else "out-of-order or duplicate key found"))
    print(f"[g3] read {wcdp_rows} wallet_coin_day_pre rows")

    missing = [w for w in sample_ids if len(panel.get(w, {}).get("USDC", {})) != T_DAYS
               or len(panel.get(w, {}).get("USDT", {})) != T_DAYS]
    dense_ok = not missing
    checks.append((f"every sampled wallet has a dense {T_DAYS}-day panel for both coins", dense_ok,
                    "ok" if dense_ok else f"{len(missing)} wallets incomplete, e.g. {missing[:3]}"))
    if missing:
        raise RuntimeError(f"{len(missing)} sampled wallets have an incomplete daily panel: {missing[:5]}")

    sample_ids_sorted = sorted(sample_ids)

    # --- scaled outcome y, pooled winsorization caps, gap d ---
    all_y = []
    for w in sample_ids_sorted:
        pre_mean = cohort[w]["pre_mean_total_units"]
        for coin in ("USDC", "USDT"):
            for t in range(T_DAYS):
                all_y.append(panel[w][coin][t] / pre_mean)
    all_y.sort()
    lo_cap = _percentile(all_y, WINSOR_LO_PCT)
    hi_cap = _percentile(all_y, WINSOR_HI_PCT)
    print(f"[g3] winsorization caps (pooled, {WINSOR_LO_PCT}/{WINSOR_HI_PCT} pct): [{lo_cap}, {hi_cap}]")

    def _wins(y):
        return min(max(y, lo_cap), hi_cap)

    d_by_wallet = {}          # wallet_id -> [d_t for t in 0..T_DAYS-1], winsorized scaled gap
    dollar_d_by_wallet = {}   # wallet_id -> [raw dollar (base-unit) gap for t], unscaled, unwinsorized
    for w in sample_ids_sorted:
        pre_mean = cohort[w]["pre_mean_total_units"]
        usdc, usdt = panel[w]["USDC"], panel[w]["USDT"]
        d_by_wallet[w] = [_wins(usdc[t] / pre_mean) - _wins(usdt[t] / pre_mean) for t in range(T_DAYS)]
        dollar_d_by_wallet[w] = [usdc[t] - usdt[t] for t in range(T_DAYS)]

    delta_a = {w: _direct_agg_delta(d_by_wallet[w]) for w in sample_ids_sorted}
    delta_b = {w: _ols_slope_delta(d_by_wallet[w]) for w in sample_ids_sorted}

    pooled_mean_units = statistics.mean(cohort[w]["pre_mean_total_units"] for w in sample_ids_sorted)
    dollar_delta_a = {w: _direct_agg_delta(dollar_d_by_wallet[w]) / pooled_mean_units for w in sample_ids_sorted}
    dollar_delta_b = {w: _ols_slope_delta(dollar_d_by_wallet[w]) / pooled_mean_units for w in sample_ids_sorted}

    # --- breakdown groups ---
    groups = [("pooled", sample_ids_sorted)]
    for arm in ("withdrawal", "deposit", "both"):
        groups.append((f"arm={arm}", [w for w in sample_ids_sorted if cohort[w]["arm"] == arm]))
    for label, thresh in FLOOR_TIERS:
        groups.append((f"floor>={label}", [w for w in sample_ids_sorted if cohort[w]["pre_mean_total_units"] >= thresh]))
    groups.append(("dual-holder (holding_type=both)", [w for w in sample_ids_sorted if cohort[w]["holding_type"] == "both"]))

    group_rows = []
    max_diff_overall = 0.0
    for label, ids in groups:
        n, sd_a, sd_b, max_diff = _group_stats(ids, delta_a, delta_b)
        max_diff_overall = max(max_diff_overall, max_diff)
        row = {"label": label, "n": n, "sd_a": sd_a, "sd_b": sd_b}
        for n_arm in N_LEVELS:
            row[f"mde_a_{n_arm}"] = _mde(sd_a, n_arm) if sd_a is not None else None
            row[f"mde_b_{n_arm}"] = _mde(sd_b, n_arm) if sd_b is not None else None
        group_rows.append(row)

    # dollar-weighted variant, pooled only
    n_dw, sd_dw_a, sd_dw_b, max_diff_dw = _group_stats(sample_ids_sorted, dollar_delta_a, dollar_delta_b)
    max_diff_overall = max(max_diff_overall, max_diff_dw)
    dw_row = {"label": "dollar-weighted (pooled, unwinsorized)", "n": n_dw, "sd_a": sd_dw_a, "sd_b": sd_dw_b}
    for n_arm in N_LEVELS:
        dw_row[f"mde_a_{n_arm}"] = _mde(sd_dw_a, n_arm)
        dw_row[f"mde_b_{n_arm}"] = _mde(sd_dw_b, n_arm)

    paths_match = max_diff_overall <= DELTA_TOL
    checks.append((f"direct aggregation and OLS-slope Delta paths agree (tolerance {DELTA_TOL})", paths_match,
                    f"max abs per-wallet difference {max_diff_overall:.3e}"))

    # --- ICC + effective obs, pooled sample ---
    icc, msb, msw = _icc_one_way(d_by_wallet, sample_ids_sorted)
    n_eff = T_DAYS / (1 + (T_DAYS - 1) * icc)

    # --- N per arm needed for target MDEs, pooled SD (path a) ---
    pooled_sd = next(r["sd_a"] for r in group_rows if r["label"] == "pooled")
    n_needed = [(pp, _n_needed(pooled_sd, pp / 100.0)) for pp in TARGET_MDES_PP]

    # IDENT comparison, pooled at N=2000/arm
    our_mde_2000 = next(r["mde_a_2000"] for r in group_rows if r["label"] == "pooled")

    passed = sum(1 for _, ok, _ in checks if ok)
    failed = [(n, d) for n, ok, d in checks if not ok]
    all_pass = not failed

    # --- summary.md ---
    lines = [
        "# Gate 3 -- pilot MDE on the wallet-level Delta",
        "",
        f"Inputs: `out/g1/cohort.csv` (sha256 `{cohort_sha}`), `out/g1/wallet_coin_day_pre.csv` (sha256 `{wcdp_sha}`). "
        "No RPC calls; no independent balance reconstruction -- every balance is S2's own.",
        "",
        f"Sample: {sample_note}. Winsorization caps (pooled, {WINSOR_LO_PCT}/{WINSOR_HI_PCT} pct of the scaled "
        f"outcome y): [{lo_cap:.6f}, {hi_cap:.6f}]. `d_it = y_iUSDC,t - y_iUSDT,t` (winsorized). "
        f"Placebo Delta_i = mean(d, day_index {T_DAYS // 2}..{T_DAYS - 1}) - mean(d, day_index 0..{T_DAYS // 2 - 1}) "
        f"(T={T_DAYS} pre-period days, split in half -- no post-period data exists yet).",
        "",
        "## MDE table (both computation paths; SD(Delta) and wallet count per cell)",
        "",
        "$0/$100 floor tiers coincide with pooled because the cohort floor rule already requires "
        "pre-period mean total holding >= $100.",
        "",
        "| group | n wallets | SD(Delta) direct-agg | SD(Delta) OLS-slope | MDE@500 | MDE@1,500 | MDE@2,000 | MDE@3,300 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in group_rows + [dw_row]:
        def _fmt(x):
            return f"{x:.4f}" if x is not None else "n/a (n<2)"
        lines.append(
            f"| {r['label']} | {r['n']} | {_fmt(r['sd_a'])} | {_fmt(r['sd_b'])} | "
            + " | ".join(_fmt(r.get(f"mde_a_{n}")) for n in N_LEVELS) + " |"
        )
    lines += [
        "",
        "(MDE columns above are the direct-aggregation path; the OLS-slope path's MDE cells, computed "
        "from the same SD(Delta) OLS-slope column, are identical within the agreement tolerance below.)",
        "",
        "## ICC and effective observations",
        "",
        "| metric | value |",
        "|---|---|",
        f"| ICC(d), pooled sample ({len(sample_ids_sorted)} wallets, T={T_DAYS}) | {icc:.4f} |",
        f"| MSB | {msb:.6f} |",
        f"| MSW | {msw:.6f} |",
        f"| effective obs per wallet @ T={T_DAYS} | {n_eff:.3f} |",
        "",
        "## N per arm needed for target MDE (pooled SD(Delta), direct-agg path)",
        "",
        "| target MDE | N per arm needed |",
        "|---|---|",
    ]
    for pp, n in n_needed:
        lines.append(f"| {pp} pp | {n} |")
    lines += [
        "",
        "## Comparison to IDENT's pilot",
        "",
        f"IDENT pilot: SD(Delta)={IDENT_SD}, ICC={IDENT_ICC}, MDE={IDENT_MDE_PP} pp at N={IDENT_N_PER_ARM}/arm. "
        f"This pilot (pooled, floor-passing sample): SD(Delta)={pooled_sd:.4f}, ICC={icc:.4f}, "
        f"MDE={our_mde_2000 * 100:.2f} pp at N={IDENT_N_PER_ARM}/arm.",
        "",
        "## Acceptance checks",
        "",
        "| check | result | detail |",
        "|---|---|---|",
    ]
    for name, ok, detail in checks:
        lines.append(f"| {name} | {'PASS' if ok else 'FAIL'} | {detail} |")
    lines += [
        "",
        f"{passed}/{len(checks)} checks passed.",
        "",
        f"**Gate 3 result: {'PASS' if all_pass else 'FAIL'}.**",
    ]
    with open(os.path.join(_OUT_DIR, "summary.md"), "w") as f:
        f.write("\n".join(lines) + "\n")

    print(f"[g3] {passed}/{len(checks)} acceptance checks passed")
    if failed:
        for name, detail in failed:
            print(f"[g3] FAIL: {name} -- {detail}")
        return 1
    return 0
