#!/usr/bin/env python3
"""A7 (10 Nov 2026) EXPLORATORY, DESCRIPTIVE, no estimate, selects nothing.
(1) Independent recompute of paper Table A6 (the 6 covariate rows) from match_membership.csv + covariates.csv,
    compared with balance.csv from exploratory/balance_table.R; plus weighted medians and quantiles.
(2) Off-support trim: controls outside the exposed matched range on the matched traits, balance after trim.
Usage: python3 exploratory/a7_balance_verify_trim.py [outdir]   (cwd = repo root)"""
import sys, os, numpy as np, pandas as pd
out = sys.argv[1] if len(sys.argv) > 1 else "out/exploratory/a7-2026-11-10"
assert os.path.abspath(out).startswith(os.path.abspath("out/exploratory"))
os.makedirs(out, exist_ok=True)
MEM = "out/recovery/presign/main-gen-M-20261027/match_membership.csv"
COV = "out/recovery/main/gen-M/covariates.csv"
m = pd.read_csv(MEM, usecols=["address", "arm", "matched", "weight", "stratum"])
c = pd.read_csv(COV, usecols=["address", "mean_total_units", "usdc_share_ppm", "transfer_count_total",
                              "eth_balance_wei_units", "wallet_age_blocks"], dtype={"eth_balance_wei_units": str})
x = m.merge(c, on="address", validate="1:1")
assert len(x) == len(m)
x["matched"] = x["matched"].astype(str).str.upper().eq("TRUE")
x["hold_usd"] = x.mean_total_units.astype(float) / 1e6
x["usdc_share"] = x.usdc_share_ppm / 1e6
x["transfers"] = x.transfer_count_total.astype(float)
x["age_blocks"] = x.wallet_age_blocks.astype(float)
x["never_sent"] = x.wallet_age_blocks.isna().astype(float)
x["eth"] = x.eth_balance_wei_units.map(lambda s: int(s) / 1e18)
x["wt"] = np.where(x.matched, x.weight, 1.0)
VARS = {"hold_usd": "Mean holdings B (USD)", "usdc_share": "USDC share", "transfers": "Transfer count (baseline)",
        "age_blocks": "Wallet age (blocks; sent-wallets only)", "never_sent": "Never-sent (share)", "eth": "ETH balance (ETH)"}
def wstats(v, w):
    k = ~np.isnan(v); v, w = v[k], w[k]
    mu = (w * v).sum() / w.sum(); sd = np.sqrt((w * (v - mu) ** 2).sum() / w.sum())
    o = np.argsort(v); cw = np.cumsum(w[o]) / w.sum()
    q = lambda p: v[o][np.searchsorted(cw, p)]
    return mu, sd, len(v), q(.5), q(.9), q(.99), v.max()
def table(E, C):
    rows = []
    for k, lab in VARS.items():
        e = wstats(E[k].to_numpy(float), E.wt.to_numpy(float)); cc = wstats(C[k].to_numpy(float), C.wt.to_numpy(float))
        smd = (e[0] - cc[0]) / np.sqrt((e[1] ** 2 + cc[1] ** 2) / 2) if (e[1] or cc[1]) else 0.0
        rows.append(dict(variable=lab, exp_mean=e[0], exp_sd=e[1], exp_n=e[2], exp_median=e[3], exp_p99=e[5], exp_max=e[6],
                         ctl_mean=cc[0], ctl_sd=cc[1], ctl_n=cc[2], ctl_median=cc[3], ctl_p99=cc[5], ctl_max=cc[6], smd=smd))
    return pd.DataFrame(rows)
E = x[(x.arm == "exposed") & x.matched]; C = x[(x.arm == "control") & x.matched]
assert (len(E), len(C)) == (4487, 11495), (len(E), len(C))
full = table(E, C); full.to_csv(f"{out}/balance_recomputed_with_medians.csv", index=False)
ref = pd.read_csv("out/exploratory/balance-2026-11-05/balance.csv")
fails = []
for (_, r), (_, q) in zip(full.iterrows(), ref.iloc[:6].iterrows()):
    for a, b in [("exp_mean", "exposed_mean"), ("exp_sd", "exposed_sd"), ("ctl_mean", "control_mean"), ("ctl_sd", "control_sd"), ("smd", "smd")]:
        if not np.isclose(r[a], q[b], rtol=1e-6, atol=1e-9): fails.append((r.variable, a, r[a], q[b]))
    if int(r.exp_n) != int(q.exposed_n) or int(r.ctl_n) != int(q.control_n): fails.append((r.variable, "n"))
print("A6 recompute vs balance.csv (6 covariate rows): ", "MATCH all means, SDs, SMDs, Ns" if not fails else f"MISMATCH {fails}")
# trim: controls outside exposed matched min..max on B, transfers, eth, age (age only sent wallets)
lim = {k: (E[k].min(), E[k].max()) for k in ["hold_usd", "transfers", "eth", "age_blocks"]}
ok = np.ones(len(C), bool)
for k, (lo, hi) in lim.items():
    v = C[k].to_numpy(float); ok &= np.isnan(v) | ((v >= lo) & (v <= hi))
Ct = C[ok]
print("exposed ranges:", {k: (float(a), float(b)) for k, (a, b) in lim.items()})
print(f"controls kept {len(Ct)} of {len(C)}; weight kept {Ct.wt.sum():.1f} of {C.wt.sum():.1f}")
tr = table(E, Ct); tr.to_csv(f"{out}/balance_trimmed_to_exposed_range.csv", index=False)
pd.set_option("display.width", 250); pd.set_option("display.float_format", lambda v: f"{v:,.4g}")
print("\nFULL matched sample"); print(full[["variable", "exp_mean", "ctl_mean", "smd", "exp_median", "ctl_median", "exp_max", "ctl_max"]].to_string(index=False))
print("\nTRIMMED controls"); print(tr[["variable", "exp_mean", "ctl_mean", "smd", "exp_median", "ctl_median", "exp_max", "ctl_max"]].to_string(index=False))
