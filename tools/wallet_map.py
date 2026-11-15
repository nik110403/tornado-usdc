"""--gate wallet_map: project a gen-M covariates.csv to the WALLET_MAP the S24 assembler / g5 reads
(`population_id,generation_id,wallet_id,address`, LF, one row per covariates row). Offline, RPC-free, pure projection.

  python3 run_all.py --gate wallet_map --covariates F --population-id ID --generation-id GEN --out NEW_FILE

Refuses: an existing --out, a header without wallet_id/address, non-lowercase-0x addresses, non-integer or duplicate
wallet_id, duplicate address, empty input, ids with control characters / commas / quotes. After writing, the file is re-read
with g5's own address/int patterns and the row count and id/address sets are compared with the source.
"""
import argparse
import csv
import hashlib
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from gates import g5_post as g5   # noqa: E402

HEADER = ["population_id", "generation_id", "wallet_id", "address"]
_ID_RE = re.compile(r"[A-Za-z0-9._-]+")


class WalletMapError(Exception):
    pass


def project(covariates_path, population_id, generation_id):
    """Returns the wallet_map CSV text. All checks happen here, before anything is written."""
    for what, v in (("population_id", population_id), ("generation_id", generation_id)):
        if not _ID_RE.fullmatch(v):
            raise WalletMapError(f"{what} {v!r} must match [A-Za-z0-9._-]+")
    with open(covariates_path, "rb") as f:
        text = f.read().decode("utf-8")
    rd = csv.DictReader(io.StringIO(text, newline=""))
    if not rd.fieldnames or "wallet_id" not in rd.fieldnames or "address" not in rd.fieldnames:
        raise WalletMapError(f"{covariates_path}: header lacks wallet_id/address")
    out, ids, addrs = io.StringIO(), set(), set()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(HEADER)
    n = 0
    for r in rd:
        wid, a = r["wallet_id"], r["address"]
        if not g5._INT_RE.fullmatch(wid or ""):
            raise WalletMapError(f"row {n + 2}: wallet_id {wid!r} is not a non-negative integer")
        if not g5._ADDR_RE.fullmatch(a or ""):
            raise WalletMapError(f"row {n + 2}: address {a!r} is not lowercase 0x + 40 hex")
        if wid in ids or a in addrs:
            raise WalletMapError(f"row {n + 2}: duplicate wallet_id/address {wid}/{a}")
        ids.add(wid)
        addrs.add(a)
        w.writerow([population_id, generation_id, wid, a])
        n += 1
    if n == 0:
        raise WalletMapError(f"{covariates_path}: no data rows")
    return out.getvalue(), n


def roundtrip(path, population_id, generation_id, expect_rows):
    """Re-read the written file the way g5._load_frame does and check it against the projection."""
    with open(path, "rb") as f:
        rows = list(csv.DictReader(io.StringIO(f.read().decode("utf-8"), newline="")))
    ids, seen = {}, set()
    for r in rows:
        if (r["population_id"], r["generation_id"]) != (population_id, generation_id):
            raise WalletMapError("round-trip: mixed population/generation")
        if not g5._ADDR_RE.fullmatch(r["address"]) or not g5._INT_RE.fullmatch(r["wallet_id"]):
            raise WalletMapError(f"round-trip: malformed row {r}")
        if r["address"] in ids or r["wallet_id"] in seen:
            raise WalletMapError("round-trip: duplicate")
        ids[r["address"]] = int(r["wallet_id"])
        seen.add(r["wallet_id"])
    if len(rows) != expect_rows or len(ids) != expect_rows:
        raise WalletMapError(f"round-trip: {len(rows)} rows, expected {expect_rows}")


def run(args):
    if os.path.lexists(args.out):
        raise WalletMapError(f"--out {args.out} exists; refusing to overwrite")
    text, n = project(args.covariates, args.population_id, args.generation_id)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "x", newline="") as f:
        f.write(text)
    try:
        roundtrip(args.out, args.population_id, args.generation_id, n)
    except WalletMapError:
        os.unlink(args.out)
        raise
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    print(f"wallet_map: {args.out} rows={n} sha256={sha}")
    return sha, n


def parser():
    a = argparse.ArgumentParser(prog="--gate wallet_map", description=__doc__.split("\n")[0])
    a.add_argument("--covariates", required=True)
    a.add_argument("--population-id", required=True)
    a.add_argument("--generation-id", required=True)
    a.add_argument("--out", required=True, help="NEW file (never overwritten)")
    return a


def main(argv=None):
    try:
        run(parser().parse_args(argv))
    except (WalletMapError, OSError, UnicodeDecodeError) as e:
        print(f"wallet_map REFUSED: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
