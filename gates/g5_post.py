"""Gate 5 -- signature-gated post-period extractor (`post_wallet_coin_day`).

FIXTURE-ONLY at this stage (S18): nothing here has touched post-event data. Before a
single file body (other than the run config and the signing record) is loaded, and long
before any RPC, `run()` verifies:

  0. the signing dir is the repo-root `prespec/` (NOT taken from the run config); `SHA256` and
     `signing.json` must be git-tracked and unmodified against HEAD, else SignatureError;
  1. the signing record `prespec/signing.json` (signer + date + the hash of the
     `SHA256` list) and `<signing_dir>/SHA256` (sha256sum format, S10b) -- every role file
     (signed prespec text, numerical manifest, executable config, CODE MANIFEST) must match
     its listed hash;
  2. the run config being executed is byte-identical to the signed executable config;
  3. the signed code manifest binds every file in CODE_FILES (producer + every gate run_all.py
     imports) AND POST_R_CODE_FILES (the R estimator + R gate): each listed file is git-tracked,
     equal to HEAD and matches its hash. g5 is the first post-data access (S11), so it ALSO runs
     the R half of the package here -- `check_r_half`: the exact `post_input_file_sha256` key
     set (required keys present, only known keys, no refused key such as later_freeze /
     outflow_evidence) and the signed `r_package_versions` -- so a package that R's `--stage post`
     gate would deny is denied BEFORE any RPC, not after the panel exists;
  4. the run config agrees with the signed numerical manifest (pinned blocks, dates,
     eligible statuses, populations, input-file hashes);
  5. every frame / wallet_map file matches the manifest hash before it is parsed.

Any failure raises before the RPC port is touched. The port is then wrapped in a block
guard: balance queries only at pre_end / bridge_end / post_end, Transfer walks only inside
(pre_end, post_end]. Nothing here reads `latest`; the post end is a required, pinned input.

Span: post = 2022-08-09 .. 2022-09-12 (35 days, day_index 0..34). 2022-08-08 is a BRIDGE day:
it is walked only to reconcile the 9 Aug opening balance
(opening@pre_end + sum(8 Aug flows) == balanceOf@bridge_end) and is never emitted.

Population: the FULL corrected eligible exposed + control frame (`eligible_frame_coverage`
rows whose screen_status is in the signed `eligible_statuses`), not matched controls only.
Each population's own `wallet_map.csv` supplies wallet_id; the cross-file key is the
canonical lowercase address. The `arm` column carries `source_membership` verbatim. No
Binance/owner/exchange label is read or invented; no placebo cohort is extracted.

Flows: Transfer legs (a transfer to/from the zero address is a mint/burn and is a normal
flow); USDT DestroyedBlackFunds subtracts the destroyed amount (it emits no Transfer), the
same correction g1/g4 apply. Identities, asserted per wallet-coin, tolerance 0:
  opening@pre_end + sum(bridge-day flows)           == balanceOf@bridge_end
  opening(9 Aug) + sum(35 daily flows)              == balanceOf@post_end
  balance(d) == balance(d-1) + net_flow(d)          (g4.reconstruct_series; negative -> error)
Output is integer base units only.
"""
import csv
import hashlib
import io
import bisect
from collections import Counter
import json
import math
import os
import random
import re
import stat
import subprocess
import time
from datetime import date, datetime, timezone

from gates import g1_cohort as g1
from gates import g4_controls as g4
from gates import g4_june
from lib import blocks, keccak, rpc

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
POST_START_DATE = date(2022, 8, 9)
BRIDGE_DATE = date(2022, 8, 8)
POST_END_DATE = date(2022, 9, 12)
N_POST_DAYS = (POST_END_DATE - POST_START_DATE).days + 1  # 35
COINS = g4.COINS
ZERO_ADDRESS = g4.ZERO_ADDRESS
TRANSFER_TOPIC = g4.TRANSFER_TOPIC
DESTROYED_TOPIC = keccak.topic("DestroyedBlackFunds(address,uint256)")
SCHEMA_VERSION = 1
ADDR_BATCH = g4.WALK_ARRAY  # 1,000-address OR arrays, as the g4 walk

OUT_COLS = [
    ("wallet_id", "int", "population-local id from that population's wallet_map.csv"),
    ("coin", "str", "USDC|USDT"),
    ("date", "str", "UTC YYYY-MM-DD, 2022-08-09..2022-09-12 (8 Aug never emitted)"),
    ("day_index", "int", "days since 2022-08-09, 0-based (0..34)"),
    ("balance_units", "int", "end-of-day balance, base units (6 dec)"),
    ("arm", "str", "source_membership of the eligible_frame_coverage row, verbatim"),
    ("nonzero", "bool", "balance_units > 0 this day (this coin)"),
    ("address", "str", "canonical lowercase 0x address"),
    ("population_id", "str", ""),
    ("generation_id", "str", ""),
    ("source_file_sha256", "str", "sha256 of the population's eligible_frame_coverage file"),
    ("source_range_id", "str", "pre_end+1..post_end block range walked, with the bridge end"),
    ("source_receipt_sha256", "str", "sha256 of the receipts file of this extraction"),
]

_ADDR_RE = re.compile(r"0x[0-9a-f]{40}")   # every use is fullmatch: `$` would accept a trailing newline
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_INT_RE = re.compile(r"[0-9]+")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")   # used with fullmatch
_RVER_RE = re.compile(r"[0-9]+([.-][0-9]+)*")        # an exact R package_version string, used with fullmatch
_ROLES = ("prespec_text", "numerical_manifest", "executable_config", "code_manifest")
# Producer code + pinned config the signature must bind (repo-relative). The signed `code_manifest` role is a sha256sum-format
# list `<sha256>  <repo-relative path>`; every path below MUST be listed and every listed file must equal HEAD and its hash.
_PRODUCER_PY = ("gates/g5_post.py", "gates/g4_controls.py", "gates/g1_cohort.py", "lib/rpc.py", "lib/keccak.py", "lib/blocks.py",
                "gates/g1b_frame.py", "run_all.py", "gates/__init__.py", "lib/__init__.py",
                # every other gate run_all.py imports at module level (their top-level code runs in the g5 process)
                "gates/g0_baseline_blacklist.py", "gates/g0_blacklist_ext.py", "gates/g0_manifest.py", "gates/g1c_funnel.py",
                "gates/g2_integrity.py", "gates/g3_mde.py", "gates/g4_june.py", "gates/presign_side_files.py",
                # the separate §5.9 veto walk (S59): run_all.py imports it, and its output is what R/later_freeze.R verifies
                "gates/g5_veto.py")
CODE_FILES = _PRODUCER_PY + ("config/blocks.json", "config/endpoints.json")
# The R half of the signed package (MIRROR of R/post_gate.R POST_R_CODE_FILES / POST_*_KEYS; tests/test_g5_post.py diffs the two).
POST_R_CODE_FILES = ("run_all.R", *(f"R/{n}.R" for n in ("utils", "decide", "cem", "estimate", "validate", "synth", "pre", "blacklist",
                                                      "presign", "drdid", "post", "post_gate", "later_freeze")),
                     "run_all.py", "config/blocks.json", "config/endpoints.json", *_PRODUCER_PY)
POST_REQUIRED_KEYS = ("pre/covariates.csv", "pre/wallet_coin_day_pre.csv", "match_membership", "analysis_parameters")
# g0_circle_events / g0_usdt_events / g0_receipts: the g0 lifetime freeze files + their receipts.jsonl (pre-signature, hash-bound). R derives the §5.9 drop list from them and
# only with a verified g5_veto output (--veto-dir); a hand-supplied `later_freeze` csv stays refused.
POST_OPTIONAL_KEYS = ("eligible_frame_coverage", "usdc_pool_flag", "deposit_counts", "g0_circle_events", "g0_usdt_events", "g0_receipts")
POST_REFUSED_KEYS = ("later_freeze", "outflow_evidence")
POST_G0_KEYS = ("g0_circle_events", "g0_usdt_events", "g0_receipts")   # the §5.9 source (event files + the receipts they came from): all or none; mirror of R POST_G0_KEYS
# the R estimator's version-sensitive packages (fixest/data.table: H1/panel; DRDID + its imports trust, BMisc, fastglm, Rcpp: the doubly robust row; jsonlite: every
# JSON input reader); signed in manifest `r_package_versions`. The R version string is signed as manifest `r_version`.
# Transitive dependencies of the above that can change numbers (linear algebra / solvers / variance and formula code): Matrix, RcppEigen, sandwich, nlme, dreamerr,
# numDeriv, stringmagic, bigmemory. All eight were installed when this list was frozen (S24 fix round 2); mirror: R/post_gate.R.
POST_PINNED_PACKAGES = ("fixest", "data.table", "DRDID", "trust", "BMisc", "fastglm", "Rcpp", "jsonlite",
                        "Matrix", "RcppEigen", "sandwich", "nlme", "dreamerr", "numDeriv", "stringmagic", "bigmemory")
ALL_CODE_FILES = tuple(dict.fromkeys(CODE_FILES + POST_R_CODE_FILES))
_CFG_KEYS = ("out_dir", "pre_end_block", "bridge_end_block", "post_end_block",
             "post_start_date", "bridge_date", "post_end_date", "eligible_statuses", "populations", "post_input_paths")
_POP_KEYS = ("population_id", "generation_id", "frame_path", "wallet_map_path")
_MANIFEST_BOUND = ("pre_end_block", "bridge_end_block", "post_end_block", "post_start_date",
                   "bridge_date", "post_end_date", "eligible_statuses")


class SignatureError(RuntimeError):
    pass


class InputMismatch(RuntimeError):
    pass


class SpanError(RuntimeError):
    pass


# --- signature + config + input verification (no RPC, no frame loading) ----

def _sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _no_dup_pairs(pairs):
    keys = [k for k, _ in pairs]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    if dup:
        raise ValueError(f"duplicate JSON key(s) {dup}")   # R reads the first of a duplicate, Python the last: refuse both ways
    return dict(pairs)


def _no_constant(c):
    raise ValueError(f"non-finite JSON constant {c} is not allowed")   # R's jsonlite rejects NaN / Infinity / -Infinity


def _loads(raw, what):
    """Strict parse of a signed-package document: strictly UTF-8 (no BOM, no UTF-16/32 sniffing: json.loads(bytes) would auto-detect those),
    valid JSON, no NaN/Infinity, no duplicate key at any depth (mirrored by pg_dupkeys in R/post_gate.R)."""
    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        if text.startswith("\ufeff"):
            raise ValueError("UTF-8 BOM")
        return json.loads(text, object_pairs_hook=_no_dup_pairs, parse_constant=_no_constant)
    except (ValueError, UnicodeDecodeError) as e:
        raise SignatureError(f"{what} is not valid JSON: {e}") from e


_WS = " \t\r\n"   # the ONLY whitespace the signed-list grammar knows (R: trimws() default); mirrored in R/post_gate.R
_EOL = re.compile(r"\r\n|\n|\r")   # R readLines' line terminators; str.splitlines() would also split on \x0b \x0c \x1c-\x1e \x85 \u2028 \u2029


def _lines(text):
    lines = _EOL.split(text)
    if lines and lines[-1] == "":
        lines.pop()          # a final terminator ends the last line, it does not open another
    return lines


def _make_verified():
    key = object()   # lives only in this closure

    class Verified:
        """Accident guard, not a security boundary (same-process code can always forge it): proves
        verify_signature() succeeded for THIS config. Carries the checked cfg and its byte hash;
        extract()/write_output() take cfg from here, never from a caller."""

        def __init__(self, k, cfg, config_sha, manifest, signed_hashes):
            if k is not key:
                raise SignatureError("Verified can only be issued by verify_signature")
            self.cfg, self.config_sha, self.manifest, self.signed_hashes = cfg, config_sha, manifest, signed_hashes

    return Verified, lambda *a: Verified(key, *a)


Verified, _issue = _make_verified()


def _require_verified(v):
    if not isinstance(v, Verified):
        raise SignatureError("signature verification token required")


def _git_env():
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(*args):
    """stdout bytes of `git -C REPO_ROOT ...` with every GIT_* variable scrubbed; None on failure."""
    r = subprocess.run(["git", "-C", REPO_ROOT, *args], capture_output=True, env=_git_env())
    return r.stdout if r.returncode == 0 else None


def _committed_bytes(rel):
    """Bytes of `rel` (repo-relative) read once from a non-symlink worktree file and required to
    equal the HEAD blob (not trusting `git diff`, which assume-unchanged/skip-worktree/filters fool)."""
    path = os.path.join(REPO_ROOT, rel)
    if os.path.islink(path) or not os.path.isfile(path):
        raise SignatureError(f"{rel} is missing or a symlink")
    body = _read_bytes(path)
    if _git("ls-files", "--error-unmatch", "--", rel) is None:
        raise SignatureError(f"{rel} is not git-tracked")
    head = _git("cat-file", "blob", f"HEAD:{rel}")
    if head is None or head != body:
        raise SignatureError(f"{rel} differs from the HEAD blob")
    return body


def parse_sha256_list(text):
    """sha256sum semantics: `<64 hex>  <name>` (or ` *<name>` for binary); names are plain
    basenames; a name listed twice is an error. Returns {name: hex}."""
    out = {}
    for i, line in enumerate(_lines(text), 1):
        if not line.strip(_WS):
            continue
        m = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line)
        if not m or "/" in m.group(2) or m.group(2) in (".", ".."):
            raise SignatureError(f"SHA256 line {i} is not '<sha256>  <basename>': {line[:80]!r}")
        if m.group(2) in out:
            raise SignatureError(f"SHA256 lists {m.group(2)!r} twice")
        out[m.group(2)] = m.group(1)
    return out


def verify_code_manifest(body, required=None):
    """The signed code manifest must list every file in CODE_FILES + POST_R_CODE_FILES, and each listed file must be a
    git-tracked, HEAD-equal, non-symlink file whose sha256 matches. Duplicates, `..`/absolute paths or bad lines are refused."""
    required = ALL_CODE_FILES if required is None else required
    seen = {}
    try:
        lines = _lines(body.decode("utf-8"))
    except UnicodeDecodeError as e:
        raise SignatureError(f"code manifest is not UTF-8: {e}") from e
    for i, line in enumerate(lines, 1):
        if not line.strip(_WS):
            continue
        m = re.fullmatch(r"([0-9a-f]{64})  ([^ \t\r\n].*)", line)
        if not m or m.group(2).startswith("/") or ".." in m.group(2).split("/"):
            raise SignatureError(f"code manifest line {i} is not '<sha256>  <repo-relative path>'")
        if m.group(2) in seen:
            raise SignatureError(f"code manifest lists {m.group(2)!r} twice")
        seen[m.group(2)] = m.group(1)
    missing = [f for f in required if f not in seen]
    if missing:
        raise SignatureError(f"code manifest does not bind {missing}")
    for rel, h in sorted(seen.items()):
        if _sha256_bytes(_committed_bytes(rel)) != h:
            raise SignatureError(f"code file {rel} does not match the signed code manifest")


def installed_r_version(pkg):
    """The installed R version of `pkg` (what R's own gate compares: as.character(packageVersion(pkg))), or None when R / the package is
    missing. Patched in tests."""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9.]*", pkg):
        return None
    try:
        r = subprocess.run(["Rscript", "-e", f'cat(as.character(packageVersion("{pkg}")))'], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 and r.stdout else None


def installed_r_string():
    """R.version.string of the Rscript that will run the post stage (what R's own gate compares), or None when R is missing. Patched in tests."""
    try:
        r = subprocess.run(["Rscript", "-e", "cat(R.version.string)"], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 and r.stdout else None


def check_r_half(manifest, installed=None, installed_r=None):
    """The R-half checks of the signed package that do not need the command line or an R process (mirror of R/post_gate.R):
    `post_input_file_sha256` is a map of hex digests whose keys include every required key, are all known, and include no refused key;
    `r_package_versions` pins every package in POST_PINNED_PACKAGES to an exact version string EQUAL to the installed version, and `r_version` EQUALS the running R.version.string. Run by g5 before any RPC (the code-file hashes are checked by
    verify_code_manifest against ALL_CODE_FILES). The R stage additionally requires the key set to equal the inputs given."""
    if "test_only" in manifest:      # S24 --test-only-allow-blanks build: unsignable by design (mirror of R verify_post_signature)
        raise SignatureError("manifest carries test_only: a test-only package build can never be verified or signed (R-half check)")
    want = manifest.get("post_input_file_sha256")
    if not isinstance(want, dict) or not want or not all(isinstance(v, str) and _SHA_RE.fullmatch(v) for v in want.values()):
        raise SignatureError("signed manifest has no valid post_input_file_sha256 map (R-half check)")
    refused = sorted(set(want) & set(POST_REFUSED_KEYS))
    if refused:
        raise SignatureError(f"signed post_input_file_sha256 lists refused post-window input(s) {refused} (R-half check)")
    unknown = sorted(set(want) - set(POST_REQUIRED_KEYS) - set(POST_OPTIONAL_KEYS))
    if unknown:
        raise SignatureError(f"signed post_input_file_sha256 has unknown key(s) {unknown} (R-half check)")
    g0_given = [k for k in POST_G0_KEYS if k in want]
    if g0_given and len(g0_given) != len(POST_G0_KEYS):
        raise SignatureError(f"signed post_input_file_sha256 must carry all of {list(POST_G0_KEYS)} or none (R-half check)")
    missing = [k for k in POST_REQUIRED_KEYS if k not in want]
    if missing:
        raise SignatureError(f"signed post_input_file_sha256 lacks required key(s) {missing} (R-half check)")
    pk = manifest.get("r_package_versions")
    if not isinstance(pk, dict):
        raise SignatureError(f"signed manifest r_package_versions must pin {POST_PINNED_PACKAGES} (R-half check)")
    installed = installed_r_version if installed is None else installed
    for k in POST_PINNED_PACKAGES:      # R's rule (post_gate.R verify_post_package_versions): installed version EXACTLY equals the signed pin
        want = pk.get(k)
        if not isinstance(want, str) or not _RVER_RE.fullmatch(want):
            raise SignatureError(f"signed r_package_versions[{k!r}] = {want!r} is not an exact version string (R-half check)")
        have = installed(k)
        if have is None:
            raise SignatureError(f"R package {k} is not installed / Rscript unavailable (signed {want}) (R-half check)")
        if have != want:
            raise SignatureError(f"installed R package {k} {have} differs from the signed version {want} (R-half check)")
    rv = manifest.get("r_version")
    if not isinstance(rv, str) or not rv.strip():
        raise SignatureError("signed manifest r_version must record R.version.string (R-half check)")
    have_r = (installed_r_string if installed_r is None else installed_r)()
    if have_r is None:
        raise SignatureError(f"Rscript unavailable (signed r_version {rv!r}) (R-half check)")
    if have_r != rv:
        raise SignatureError(f"installed R {have_r!r} differs from the signed r_version {rv!r} (R-half check)")


def verify_signature(config_bytes):
    """Check git-committed signer record, SHA256 list, the three role files and the executable
    config in the repo-root prespec/. Returns a Verified token (manifest + signed-file hashes).
    Raises SignatureError on any absence or mismatch."""
    signing_dir = os.path.join(REPO_ROOT, "prespec")
    top = _git("rev-parse", "--show-toplevel")
    if top is None or os.path.realpath(top.decode().strip()) != os.path.realpath(REPO_ROOT):
        raise SignatureError("REPO_ROOT is not the top level of its git repository")
    rec_bytes = _committed_bytes("prespec/signing.json")
    list_bytes = _committed_bytes("prespec/SHA256")
    rec = _loads(rec_bytes, "signing.json")
    # exactly as strict as R/post_gate.R (is.character, length 1, nzchar(trimws()), ^dddd-dd-dd$ + a real calendar date): no str() coercion
    if not isinstance(rec, dict):
        raise SignatureError("signing.json is not a JSON object")
    signer, sdate = rec.get("signed_by"), rec.get("signed_date")
    if not isinstance(signer, str) or not signer.strip(_WS):
        raise SignatureError("signing.json carries no signed_by (a non-empty string is required)")
    if not isinstance(sdate, str) or not _DATE_RE.fullmatch(sdate):
        raise SignatureError("signing.json carries no valid ISO signed_date (a YYYY-MM-DD string is required)")
    try:
        date.fromisoformat(sdate)
    except ValueError:
        raise SignatureError("signing.json carries no valid ISO signed_date (not a calendar date)")
    if rec.get("sha256_list_sha256") != _sha256_bytes(list_bytes):
        raise SignatureError("signing.json does not bind the current SHA256 list")
    try:
        listed = parse_sha256_list(list_bytes.decode("utf-8"))
    except UnicodeDecodeError as e:
        raise SignatureError(f"SHA256 list is not UTF-8: {e}") from e
    files = rec.get("files")
    if not isinstance(files, dict) or set(files) != set(_ROLES):
        raise SignatureError(f"signing.json files must be exactly {_ROLES}")
    if len(set(files.values())) != len(_ROLES):
        raise SignatureError("role files must be distinct paths")
    if set(listed) != set(files.values()):
        raise SignatureError(f"SHA256 lists entries that are not role files (unchecked): {sorted(set(listed) - set(files.values()))}; "
                             "or omits a role file")
    bodies, hashes = {}, {}
    for role in _ROLES:
        name = files[role]
        if not isinstance(name, str) or "/" in name or name not in listed:
            raise SignatureError(f"role {role}: {name!r} is not a basename listed in SHA256")
        p = os.path.join(signing_dir, name)
        if os.path.islink(p) or not os.path.isfile(p):
            raise SignatureError(f"role {role}: signed file {p} missing or a symlink")
        bodies[role] = _read_bytes(p)
        if not bodies[role].strip():
            raise SignatureError(f"role {role}: {name} is empty")
        if _sha256_bytes(bodies[role]) != listed[name]:
            raise SignatureError(f"role {role}: {name} does not match its SHA256 entry")
        hashes[role] = listed[name]
    if _sha256_bytes(config_bytes) != listed[files["executable_config"]]:
        raise SignatureError("the run config is not the signed executable config")
    verify_code_manifest(bodies["code_manifest"])
    manifest = _loads(bodies["numerical_manifest"], "numerical manifest")
    if not isinstance(manifest, dict):
        raise SignatureError("numerical manifest is not a JSON object")
    hashes["signing_json"] = _sha256_bytes(rec_bytes)
    cfg = _loads(config_bytes, "run config")
    if not isinstance(cfg, dict):
        raise InputMismatch("run config is not a JSON object")
    check_r_half(manifest)
    check_config(cfg, manifest, blocks.load_pinned()["pre_period_end"]["block"])
    return _issue(cfg, _sha256_bytes(config_bytes), manifest, hashes)


def check_config(cfg, manifest, pinned_pre_end):
    """Required keys, config == signed manifest, and span/pin arithmetic. No RPC."""
    if "test_only" in manifest:
        raise InputMismatch("manifest carries test_only: a test-only package build can never be verified or signed")
    miss = [k for k in _CFG_KEYS if k not in cfg or cfg[k] in (None, "", [])]
    if miss:
        raise InputMismatch(f"config missing required keys {miss} (no defaults)")
    if "signing_dir" in cfg:
        raise InputMismatch("signing_dir is fixed to repo-root prespec/; it may not be set in the config")
    st = cfg["eligible_statuses"]
    if not (isinstance(st, list) and all(isinstance(x, str) and x for x in st)):
        raise InputMismatch("eligible_statuses must be a list of non-empty strings")
    if not (isinstance(cfg["populations"], list) and all(isinstance(p, dict) for p in cfg["populations"])):
        raise InputMismatch("populations must be a list of objects")
    for p in cfg["populations"]:
        if any(not isinstance(p.get(k), str) or not p.get(k) for k in _POP_KEYS):
            raise InputMismatch(f"population entry needs {_POP_KEYS}: {p}")
    pip, want_in = cfg["post_input_paths"], manifest.get("post_input_file_sha256")
    if not (isinstance(pip, dict) and all(isinstance(v, str) and v for v in pip.values())):
        raise InputMismatch("post_input_paths must map each signed post input key to a file path")
    if set(pip) != set(want_in):
        raise InputMismatch(f"post_input_paths keys {sorted(pip)} differ from the signed post_input_file_sha256 keys {sorted(want_in)}")
    for k in _MANIFEST_BOUND:
        if manifest.get(k) != cfg[k]:
            raise InputMismatch(f"config {k}={cfg[k]!r} differs from signed manifest {manifest.get(k)!r}")
    if [(p["population_id"], p["generation_id"]) for p in cfg["populations"]] != \
            [tuple(x) for x in manifest.get("populations", [])]:
        raise InputMismatch("config populations differ from signed manifest populations")
    pre, bridge, post = (cfg[k] for k in ("pre_end_block", "bridge_end_block", "post_end_block"))
    if not all(isinstance(b, int) and not isinstance(b, bool) for b in (pre, bridge, post)):
        raise SpanError("blocks must be pinned integers")
    if pre != pinned_pre_end:
        raise SpanError(f"pre_end_block {pre} != config/blocks.json pre_period_end {pinned_pre_end}")
    if not (pre < bridge < post):
        raise SpanError(f"need pre_end < bridge_end < post_end, got {pre}, {bridge}, {post}")
    if (date.fromisoformat(cfg["post_start_date"]), date.fromisoformat(cfg["bridge_date"]),
            date.fromisoformat(cfg["post_end_date"])) != (POST_START_DATE, BRIDGE_DATE, POST_END_DATE):
        raise SpanError("dates are not the signed post span 2022-08-09..2022-09-12 with 8 Aug bridge")


def verify_post_inputs(verified):
    """F2: hash EVERY signed `post_input_file_sha256` input (paths from the signed config's `post_input_paths`) against the signed value,
    before any RPC. A missing / non-regular / symlinked file or any hash difference is a denial -- the same inputs R's `--stage post` will
    open (R/post_gate.R verify_post_inputs) are proven to be the signed bytes before g5 spends a single query."""
    want, paths = verified.manifest["post_input_file_sha256"], verified.cfg["post_input_paths"]
    for k in sorted(want):
        path = paths[k]
        if os.path.islink(path) or not os.path.isfile(path):
            raise InputMismatch(f"post input {k}: {path} is missing, not a regular file or a symlink")
        if _sha256_bytes(_read_bytes(path)) != want[k]:
            raise InputMismatch(f"post input {k}: {path} does not match the signed post_input_file_sha256")


def _load_frame(pop, status_ok, manifest_hashes):
    """Hash-check then parse one population's frame + wallet_map. Returns
    ({address: (wallet_id, arm)}, frame_sha256, map_stats)."""
    shas, texts = {}, {}
    for key in ("frame_path", "wallet_map_path"):
        path = pop[key]
        want = manifest_hashes.get(path)
        if not isinstance(want, str) or not _SHA_RE.fullmatch(want):
            raise InputMismatch(f"{path}: no signed hash in the numerical manifest")
        if not os.path.isfile(path):
            raise InputMismatch(f"{path}: missing")
        raw = _read_bytes(path)  # hashed and parsed from these exact bytes; never reopened
        shas[key] = _sha256_bytes(raw)
        if shas[key] != want:
            raise InputMismatch(f"{path}: sha256 differs from the signed manifest")
        try:
            texts[key] = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise InputMismatch(f"{path}: not UTF-8") from e
    ids = {}
    seen_wid = set()
    for r in csv.DictReader(io.StringIO(texts["wallet_map_path"], newline="")):
        a, wid = r["address"], r["wallet_id"]
        if (r["population_id"], r["generation_id"]) != (pop["population_id"], pop["generation_id"]):
            raise InputMismatch(f"wallet_map row {a}: mixed population/generation")
        if not _ADDR_RE.fullmatch(a) or not _INT_RE.fullmatch(wid):
            raise InputMismatch(f"wallet_map row malformed: {r}")
        if a in ids or wid in seen_wid:
            raise InputMismatch(f"wallet_map duplicate address/wallet_id: {a}/{wid}")
        ids[a] = int(wid)
        seen_wid.add(wid)
    frame, seen = {}, set()
    for r in csv.DictReader(io.StringIO(texts["frame_path"], newline="")):
        a = r["address"]
        if (r["population_id"], r["generation_id"]) != (pop["population_id"], pop["generation_id"]):
            raise InputMismatch(f"frame row {a}: mixed population/generation")
        if not _ADDR_RE.fullmatch(a):
            raise InputMismatch(f"frame address not canonical lowercase: {a!r}")
        if a in seen:
            raise InputMismatch(f"frame duplicate address {a}")
        seen.add(a)
        if r["screen_status"] in status_ok:
            if a not in ids:
                raise InputMismatch(f"eligible address {a} has no wallet_map entry")
            frame[a] = (ids[a], r["source_membership"])
    if not frame:
        raise InputMismatch(f"{pop['population_id']}: no eligible rows")
    return frame, shas["frame_path"], {"frame_rows": len(seen), "map_rows": len(ids)}


# --- RPC port (injected; live implementation at the bottom) ----------------

class GuardedPort:
    """Wraps a port so no request can leave (pre_end, post_end]; balance queries only at the
    three reconciliation blocks. Raises SpanError BEFORE delegating."""

    def __init__(self, port, pre_end, bridge_end, post_end):
        self.port, self.pre, self.bridge, self.post = port, pre_end, bridge_end, post_end

    def _range(self, s, e):
        if not (self.pre < s <= e <= self.post):
            raise SpanError(f"request [{s},{e}] outside ({self.pre}, {self.post}]")

    def block_date(self, block):
        if block not in (self.bridge, self.bridge + 1, self.post, self.post + 1):
            raise SpanError(f"block_date({block}) is not a pin boundary")
        return self.port.block_date(block)

    def balance_of_many(self, coin, addrs, block):
        if block not in (self.pre, self.bridge, self.post):
            raise SpanError(f"balanceOf at {block} is not pre/bridge/post end")
        return self.port.balance_of_many(coin, addrs, block)

    def transfers(self, coin, pos, addrs, s, e):
        self._range(s, e)
        return self.port.transfers(coin, pos, addrs, s, e)

    def destroyed_logs(self, s, e):
        self._range(s, e)
        return self.port.destroyed_logs(s, e)

    def receipts_sha256(self):
        return self.port.receipts_sha256()


def _day(log, bridge_end, pre_end, post_end):
    """Day index (-1 = bridge) of a log, cross-checked against the pinned bridge block."""
    blk = int(log["blockNumber"], 16)
    if not (pre_end < blk <= post_end):
        raise SpanError(f"log block {blk} outside ({pre_end}, {post_end}]")
    if not log.get("blockTimestamp"):
        raise RuntimeError(f"log missing blockTimestamp: tx={log.get('transactionHash')}")
    d = (datetime.fromtimestamp(int(log["blockTimestamp"], 16), tz=timezone.utc).date() - POST_START_DATE).days
    if d < -1 or d >= N_POST_DAYS:
        raise AssertionError(f"log outside bridge+post span: tx={log.get('transactionHash')} day {d}")
    if (d == -1) != (blk <= bridge_end):
        raise AssertionError(f"block {blk} / bridge_end {bridge_end} disagree with its date (day {d})")
    return d


def decode_transfers(logs, pos, addr_set, token, bounds):
    """One walk unit -> {addr: ({day: delta}, legs, mint_burn_legs)}; same checks as
    g4.decode_unit_logs (token, 3-topic Transfer, strict shape, unique (blockHash, logIndex),
    tracked address in the right slot, consistent timestamps) over the bridge+post day range."""
    agg, uniq = {}, set()
    token = token.lower()
    g4.check_block_timestamps(logs)
    for l in logs:
        if str(l.get("address", "")).lower() != token:
            raise AssertionError(f"log from {l.get('address')}, expected {token}")
        topics = l.get("topics") or []
        if len(topics) != 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
            raise AssertionError(f"not a 3-topic Transfer: tx={l.get('transactionHash')}")
        k = (l.get("blockHash"), l.get("logIndex"))
        if k[0] is None or k[1] is None or k in uniq:
            raise AssertionError(f"missing or duplicate (blockHash, logIndex) {k}")
        uniq.add(k)
        g4._strict_transfer_shape(l)
        frm, to, value = g1._transfer_decode(l)
        me, other, sign = (frm, to, -1) if pos == "from" else (to, frm, 1)
        if me not in addr_set:
            raise AssertionError(f"endpoint returned a log for untracked address {me}")
        day = _day(l, *bounds)
        rec = agg.setdefault(me, [{}, 0, 0])
        rec[0][day] = rec[0].get(day, 0) + sign * value
        rec[1] += 1
        if other == ZERO_ADDRESS or me == ZERO_ADDRESS:
            rec[2] += 1
    return agg


def decode_destroyed(logs, addr_set, bounds):
    """USDT DestroyedBlackFunds(address,uint256) -> [(addr, day, amount)] for tracked wallets."""
    out, uniq = [], set()
    for l in logs:
        topics = l.get("topics") or []
        data = l.get("data", "")
        if len(topics) != 1 or str(topics[0]).lower() != DESTROYED_TOPIC or str(l.get("address", "")).lower() != g1.USDT.lower():
            raise AssertionError(f"not a USDT DestroyedBlackFunds log: tx={l.get('transactionHash')}")
        if not re.fullmatch(r"0x0{24}[0-9a-fA-F]{104}", str(data)):
            raise AssertionError(f"DestroyedBlackFunds data is not 64 bytes: tx={l.get('transactionHash')}")
        k = (l.get("blockHash"), l.get("logIndex"))
        if k[0] is None or k[1] is None or k in uniq:
            raise AssertionError(f"missing or duplicate (blockHash, logIndex) {k}")
        uniq.add(k)
        addr = "0x" + data[26:66].lower()
        day = _day(l, *bounds)
        if addr in addr_set:
            out.append((addr, day, int(data[66:130], 16)))
    return out


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def extract(frames, port, verified):
    """Walk the bridge+post span for the union of all populations' eligible addresses and
    return (series, stats): series[(coin, addr)] = (opening_9aug, [35 daily balances])."""
    _require_verified(verified)
    cfg = verified.cfg
    pre, bridge, post = cfg["pre_end_block"], cfg["bridge_end_block"], cfg["post_end_block"]
    g = GuardedPort(port, pre, bridge, post)
    # pin check: bridge_end is the last block of 8 Aug, post_end the last block of 12 Sep
    for blk, last_day in ((bridge, BRIDGE_DATE), (post, POST_END_DATE)):
        if g.block_date(blk) != last_day or g.block_date(blk + 1) <= last_day:
            raise SpanError(f"block {blk} is not the last block of {last_day}")
    addrs = sorted({a for fr in frames.values() for a in fr})
    aset = set(addrs)
    bounds = (bridge, pre, post)
    flows = {c: {a: {} for a in addrs} for c in COINS}
    stats = {"transfer_legs": 0, "mint_burn_legs": 0, "destroyed_applied": 0, "addresses": len(addrs)}
    open_pre, bal_bridge, bal_post = {}, {}, {}
    for coin, token in COINS.items():
        open_pre[coin] = _balances(g, coin, addrs, pre)
        bal_bridge[coin] = _balances(g, coin, addrs, bridge)
        bal_post[coin] = _balances(g, coin, addrs, post)
        for pos in ("from", "to"):
            for chunk in _chunks(addrs, ADDR_BATCH):
                agg = decode_transfers(g.transfers(coin, pos, chunk, pre + 1, post), pos, aset, token, bounds)
                for a, (by_day, legs, mb) in agg.items():
                    fa = flows[coin][a]
                    for d, v in by_day.items():
                        fa[d] = fa.get(d, 0) + v
                    stats["transfer_legs"] += legs
                    stats["mint_burn_legs"] += mb
    for a, d, amt in decode_destroyed(g.destroyed_logs(pre + 1, post), aset, bounds):
        fa = flows["USDT"][a]
        fa[d] = fa.get(d, 0) - amt
        stats["destroyed_applied"] += 1
    series = {}
    for coin in COINS:
        for a in addrs:
            fa = flows[coin][a]
            bridge_net = fa.get(-1, 0)
            opening = open_pre[coin][a] + bridge_net
            if opening != bal_bridge[coin][a]:
                raise AssertionError(f"{coin} {a}: opening@pre_end {open_pre[coin][a]} + 8 Aug flows {bridge_net} "
                                     f"!= balanceOf@bridge_end {bal_bridge[coin][a]}")
            bals = g4.reconstruct_series(opening, {d: v for d, v in fa.items() if d >= 0}, n_days=N_POST_DAYS)
            if bals[-1] != bal_post[coin][a]:
                raise AssertionError(f"{coin} {a}: reconstructed closing {bals[-1]} != balanceOf@post_end {bal_post[coin][a]}")
            series[(coin, a)] = (opening, bals)
    return series, stats


def _balances(g, coin, addrs, block):
    out = {}
    for chunk in _chunks(addrs, g4.BALANCE_BATCH):
        got = g.balance_of_many(coin, chunk, block)
        if set(got) != set(chunk) or any(not isinstance(v, int) or v < 0 for v in got.values()):
            raise AssertionError(f"balance_of_many {coin}@{block}: incomplete or non-integer reply")
        out.update(got)
    return out


def write_output(frames, frame_shas, series, stats, receipts_sha, verified, attestation=None):
    """Immutable output dir; refuses to overwrite. Written to a temp sibling dir, dense panel
    asserted, schema written, then renamed into place atomically."""
    _require_verified(verified)
    cfg = verified.cfg
    out_dir = os.path.normpath(cfg["out_dir"])
    if os.path.exists(out_dir) and os.listdir(out_dir):
        raise InputMismatch(f"{out_dir} is not empty (generations are immutable)")
    tmp_dir = f"{out_dir}.tmp-{os.getpid()}"
    os.makedirs(tmp_dir)
    rng = f"{cfg['pre_end_block'] + 1}-{cfg['post_end_block']}/bridge_end={cfg['bridge_end_block']}"
    path = os.path.join(tmp_dir, "post_wallet_coin_day.csv")
    n = 0
    with open(path, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow([c[0] for c in OUT_COLS])
        for pop in cfg["populations"]:
            pid, gid = pop["population_id"], pop["generation_id"]
            fr = frames[pid, gid]
            for a, (wid, arm) in sorted(fr.items(), key=lambda kv: kv[1][0]):
                for coin in COINS:
                    _, bals = series[(coin, a)]
                    assert len(bals) == N_POST_DAYS
                    for d, b in enumerate(bals):
                        dt = date.fromordinal(POST_START_DATE.toordinal() + d).isoformat()
                        w.writerow([wid, coin, dt, d, b, arm, str(b > 0).lower(), a, pid, gid,
                                    frame_shas[pid, gid], rng, receipts_sha])
                        n += 1
    expect = sum(len(fr) for fr in frames.values()) * len(COINS) * N_POST_DAYS
    if n != expect:
        raise AssertionError(f"panel not dense: {n} rows, expected {expect} (temp dir {tmp_dir} left for inspection)")
    sha = _sha256_bytes(_read_bytes(path))
    with open(os.path.join(tmp_dir, "post_wallet_coin_day.schema.json"), "w") as f:
        json.dump({
            "schema_version": SCHEMA_VERSION,
            "columns": [{"name": c[0], "dtype": c[1], "units": c[2]} for c in OUT_COLS],
            "primary_key": ["population_id", "generation_id", "wallet_id", "coin", "day_index"],
            "null_policy": "no missing values; an unreadable wallet-coin-day is a hard failure",
            "row_count": n, "sha256": sha,
            "signed_file_sha256": verified.signed_hashes,  # from the committed SHA256 list (+ signing.json bytes)
            "stats": stats, "producing_commit": g4._git_commit(),
            # attested from the receipts + reverify records of a live run (null for an injected fixture port)
            "walk_endpoint": (attestation or {}).get("walk_endpoint"), "replay_endpoint": (attestation or {}).get("replay_endpoint"),
            "batch_endpoint": (attestation or {}).get("batch_endpoint"),
        }, f, indent=2, sort_keys=True)
        f.write("\n")
    if os.path.isdir(out_dir):
        os.rmdir(out_dir)  # empty (checked above)
    os.rename(tmp_dir, out_dir)
    return os.path.join(out_dir, "post_wallet_coin_day.csv"), n, sha


PRODUCER_FILES = tuple(os.path.join(REPO_ROOT, p) for p in _PRODUCER_PY)
CONFIG_FILES = tuple(os.path.join(REPO_ROOT, "config", n) for n in ("blocks.json", "endpoints.json"))
BINDING_NAME = "g5_binding.json"
CHECKPOINT_NAME = "checkpoint.jsonl"
RECEIPTS_NAME = "receipts.jsonl"
REVERIFY_NAME = "reverify.jsonl"
LOCK_NAME = ".lock"          # g4_june.acquire_run_lock's flock file; held for the life of a run, not a binding-relevant leftover
_WORK_NAMES = frozenset({BINDING_NAME, CHECKPOINT_NAME, RECEIPTS_NAME, REVERIFY_NAME, LOCK_NAME})
REVERIFY_FRACTION = 0.05
REVERIFY_BUCKETS = 6          # equal block-span segments of (pre_end, post_end]; each (endpoint, unit kind, segment) needs a sampled range OVERLAPPING it
SEED = g4.SEED


class PreAccessDenied(RuntimeError):
    """Refused before any post data was read or any RPC was made."""


class CheckpointError(PreAccessDenied):
    pass


class CheckpointCorrupt(RuntimeError):
    """A cached query disagrees with the acquisition's own receipts (detected mid-run)."""


class ReverifyError(AssertionError):
    pass


def make_binding(verified):
    """Identity every checkpoint/receipt in a work dir is bound to: the sha256 of every producer source file (NOT the git HEAD: an unrelated
    commit by the autopilot must not invalidate a resume; any producer-file change still does), the signed executable config bytes,
    ALL signed file hashes (incl. signing.json: a re-signed prespec cannot resume pre-signature fetches), the signed input-file hashes,
    the pinned block/endpoint config and the effective endpoints. Any change = a different run."""
    return {"producer_source_sha256": {rel: g4._sha256_file(p) for rel, p in zip(_PRODUCER_PY, PRODUCER_FILES)},
            "config_sha256": verified.config_sha,
            "signed_file_sha256": dict(sorted(verified.signed_hashes.items())),
            "input_hashes": dict(sorted((verified.manifest.get("input_file_sha256") or {}).items())),
            "pinned_config": {os.path.basename(p): g4._sha256_file(p) for p in CONFIG_FILES},
            "walk_endpoint": g4.WALK, "batch_endpoint": g4.BATCH}


def _check_work_names(directory):
    for n in sorted(os.listdir(directory)):
        if n not in _WORK_NAMES:
            raise CheckpointError(f"{directory} holds an unknown file {n!r}; move it aside")
        if not stat.S_ISREG(os.lstat(os.path.join(directory, n)).st_mode):
            raise CheckpointError(f"{os.path.join(directory, n)} is not a regular non-symlink file")


def bind_work_dir(directory, binding):
    """Create/validate `directory` for this binding. Empty/new -> write the binding. Non-empty -> only if it carries the
    identical binding (a resume) and holds nothing but regular, known work files; leftovers without a binding, or under another
    binding, are refused (move aside)."""
    os.makedirs(directory, exist_ok=True)
    bpath = os.path.join(directory, BINDING_NAME)
    if os.path.lexists(bpath):
        if os.path.islink(bpath):
            raise CheckpointError(f"{bpath} is a symlink; refusing to trust it")
        try:
            with open(bpath) as f:
                stored = json.load(f)
        except ValueError as e:
            raise CheckpointError(f"{bpath} is corrupt: {e}") from e
        if stored != binding:
            raise CheckpointError(f"{directory}: checkpoints/receipts were produced under a different producer/config/inputs "
                                  f"(stored {rpc.canonical_sha256(stored)[:12]} != {rpc.canonical_sha256(binding)[:12]}); "
                                  "move the directory aside to start fresh")
        _check_work_names(directory)
        return
    left = sorted(n for n in os.listdir(directory) if n != LOCK_NAME)
    if left:
        raise CheckpointError(f"{directory} holds unbound files ({', '.join(left[:5])}); cannot attribute them to this run; move it aside")
    rpc._atomic_write_json(bpath, binding)


def repair_torn_receipts(path):
    """A crash can leave the receipts file with an unterminated last line. Its query was never checkpointed (receipt is fsynced BEFORE
    the checkpoint put), so truncating to the last complete line and refetching is safe."""
    if not os.path.exists(path):
        return
    with open(path, "rb") as f:
        raw = f.read()
    if raw and not raw.endswith(b"\n"):
        with open(path, "r+b") as f:
            f.truncate(raw.rfind(b"\n") + 1)


def _fsync_path(path):
    with open(path, "ab") as f:
        f.flush()
        os.fsync(f.fileno())


def _addrs_sha(addrs):
    return hashlib.sha256("\n".join(addrs).encode()).hexdigest()


def walk_unit(coin, pos, addrs):
    return f"{coin}:{pos}:{_addrs_sha(addrs)[:16]}"


def expected_units(addrs):
    """Every walk unit an acquisition of this universe must have receipted: coins x positions x address chunks, plus destroyed."""
    return {"destroyed"} | {walk_unit(c, p, ch) for c in COINS for p in ("from", "to") for ch in _chunks(addrs, ADDR_BATCH)}


_RECEIPT_KEYS = ("unit", "endpoint", "fromBlock", "toBlock", "logCount", "sha256")


def read_getlogs_receipts(receipts_path):
    """{(unit, from, to): record} of the acquisition's g5 getLogs receipts. A torn/corrupt line is refused (never skipped); the same range
    receipted twice must agree."""
    with open(receipts_path, "rb") as f:
        raw = f.read()
    if raw and not raw.endswith(b"\n"):
        raise ReverifyError(f"{receipts_path} ends in a torn (unterminated) receipt line; a resumed run repairs it, an inspection must not guess")
    out = {}
    for n, line in enumerate(raw.decode("utf-8").split("\n"), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError as e:
            raise ReverifyError(f"{receipts_path} line {n} is corrupt: {e}") from e
        if not isinstance(r, dict) or r.get("gate") != "g5" or r.get("kind") != "getLogs":
            continue
        if any(k not in r for k in _RECEIPT_KEYS):
            raise ReverifyError(f"{receipts_path} line {n}: getLogs receipt lacks {[k for k in _RECEIPT_KEYS if k not in r]}")
        k = (r["unit"], r["fromBlock"], r["toBlock"])
        if out.setdefault(k, r)["sha256"] != r["sha256"] or out[k]["logCount"] != r["logCount"]:
            raise ReverifyError(f"receipts disagree about {k}: the same range was acquired with two different results")
    return out


def read_balance_receipts(receipts_path):
    """{(coin, fromBlock, toBlock, addressCount): [receipt, ...]} of the acquisition's balanceOf point-query receipts (g4._point_batch writes
    one per call; `sha256` = sha256 of json.dumps(raw results, sort_keys=True)). Corrupt lines are refused, never skipped."""
    out = {}
    with open(receipts_path, "rb") as f:
        raw = f.read()
    for n, line in enumerate(raw.decode("utf-8").split("\n"), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError as e:
            raise ReverifyError(f"{receipts_path} line {n} is corrupt: {e}") from e
        if isinstance(r, dict) and r.get("kind") == "balanceOf":
            out.setdefault((r.get("coin"), r.get("fromBlock"), r.get("toBlock"), r.get("addressCount")), []).append(r)
    return out


def balance_digest(addrs, balances):
    """The digest g4._point_batch receipts for a balanceOf batch, recomputed from a cached {addr: int} in the batch's address order
    (each eth_call result is the 32-byte word "0x%064x")."""
    return hashlib.sha256(json.dumps(["0x%064x" % balances[a] for a in addrs], sort_keys=True).encode()).hexdigest()


def _log_order(bl):
    """Total deterministic order of (block, log) pairs: block, logIndex, transactionHash (never compares the log dicts)."""
    b, l = bl
    return (b, int(l.get("logIndex") or "0x0", 16), str(l.get("transactionHash")))


class CheckpointedPort:
    """Durable per-query cache in front of a port (ChunkStore-style: append-only, fsync per write, torn final line
    tolerated, bound to producer+config+inputs by bind_work_dir). A quota 429 aborts the run; the re-run replays the finished
    queries from disk instead of the network. Sits INSIDE GuardedPort, so the span guard still sees every request.
    With `receipts_path`, a cached log list is used only if, per receipted range, its count and v2 digest equal the acquisition's
    receipt; a disagreement is CheckpointCorrupt, a cache entry without complete receipts is treated as a miss (refetched)."""

    def __init__(self, port, directory, binding, receipts_path=None):
        bind_work_dir(directory, binding)
        self._port = port
        # receipts_path given but no file yet -> {} (nothing is trusted: every cached log list is refetched); None only when no receipts are in play
        self._acq = None if not receipts_path else (read_getlogs_receipts(receipts_path) if os.path.exists(receipts_path) else {})
        self._ck = g4._Ckpt(os.path.join(directory, CHECKPOINT_NAME))   # absolute name: os.path.join ignores g4._OUT_DIR
        self._bal = None      # balanceOf receipts still available to vouch for a cached balance batch (None: no receipts in play)
        if receipts_path:
            if os.path.exists(receipts_path):
                self._bal = read_balance_receipts(receipts_path)
            elif self._ck.data:
                self._ck.close()
                raise CheckpointError(f"{directory}: the checkpoint holds {len(self._ck.data)} cached queries but {receipts_path} is missing; "
                                      "cached balances cannot be vouched for -- move the work dir aside")
            else:
                self._bal = {}

    def __getattr__(self, name):
        return getattr(self._port, name)

    def _cached(self, key, fetch, ok=lambda v: True):
        v = self._ck.data.get(key)
        if v is not None and ok(v):
            return v
        v = fetch()
        self._ck.put(key, v)
        return v

    def _logs_match_receipts(self, unit, s, e, logs):
        if self._acq is None:
            return True
        recs = sorted((r for (u, a, b), r in self._acq.items() if u == unit and s <= a and b <= e), key=lambda r: r["fromBlock"])
        try:
            g4._assert_tiles([(r["fromBlock"], r["toBlock"]) for r in recs], s, e)
        except AssertionError:
            return False                       # no complete receipts for this cached query: refetch rather than trust it
        keyed = sorted(((int(l["blockNumber"], 16), l) for l in logs), key=_log_order)
        blks = [b for b, _ in keyed]
        total = 0
        for r in recs:
            sub = [l for _, l in keyed[bisect.bisect_left(blks, r["fromBlock"]):bisect.bisect_right(blks, r["toBlock"])]]
            if len(sub) != r["logCount"] or g4._digest(sub, with_block=True) != r["sha256"]:
                raise CheckpointCorrupt(f"cached {unit} [{r['fromBlock']},{r['toBlock']}] disagrees with its acquisition receipt "
                                        f"({len(sub)} logs vs receipt {r['logCount']}, digest equal: {g4._digest(sub, with_block=True) == r['sha256']})")
            total += len(sub)
        if total != len(logs):
            raise CheckpointCorrupt(f"cached {unit} holds {len(logs) - total} logs outside every receipted range")
        return True

    def _take_balance_receipt(self, coin, block, addrs, balances):
        """A cached balance batch is used only if the acquisition receipted a balanceOf query of the same shape AND the digest recomputed
        from the cached values (re-encoded "0x%064x", sha256) equals that receipt's recorded sha256 (one receipt per _point_batch call,
        spent once); otherwise it is a miss and is refetched."""
        if self._bal is None:
            return True
        recs = self._bal.get((coin, block, block, len(addrs)), [])
        try:
            dig = balance_digest(addrs, balances)
        except (KeyError, TypeError, ValueError):
            return False
        for i, r in enumerate(recs):
            if r.get("sha256") == dig:
                del recs[i]
                return True
        return False

    def balance_of_many(self, coin, addrs, block):
        return self._cached(f"bal:{coin}:{block}:{_addrs_sha(addrs)}", lambda: self._port.balance_of_many(coin, addrs, block),
                            ok=lambda v: set(v) == set(addrs) and self._take_balance_receipt(coin, block, addrs, v))

    def transfers(self, coin, pos, addrs, s, e):
        unit = walk_unit(coin, pos, addrs)
        return self._cached(f"tr:{coin}:{pos}:{s}:{e}:{_addrs_sha(addrs)}", lambda: self._port.transfers(coin, pos, addrs, s, e),
                            ok=lambda v: self._logs_match_receipts(unit, s, e, v))

    def destroyed_logs(self, s, e):
        return self._cached(f"dst:{s}:{e}", lambda: self._port.destroyed_logs(s, e),
                            ok=lambda v: self._logs_match_receipts("destroyed", s, e, v))

    def close(self):
        self._ck.close()


def _bucket_bounds(k, pre_end, post_end):
    span = post_end - pre_end
    return pre_end + 1 + k * span // REVERIFY_BUCKETS, (post_end if k == REVERIFY_BUCKETS - 1 else pre_end + (k + 1) * span // REVERIFY_BUCKETS)


def _overlapped(r, pre_end, post_end):
    out = []
    for k in range(REVERIFY_BUCKETS):
        lo, hi = _bucket_bounds(k, pre_end, post_end)
        if r["fromBlock"] <= hi and r["toBlock"] >= lo:
            out.append(k)
    return out


def _kind(r):
    return "destroyed" if r["unit"] == "destroyed" else f"{r['coin']}:{r['topics_position']}"


def load_acquisition_ranges(receipts_path, pre_end, post_end, expected=None):
    """The acquisition's own getLogs receipts (deduplicated), each unit required to tile (pre_end, post_end] exactly, and -- when
    `expected` is given -- the receipted units required to EQUAL the expected set: a unit with no receipt at all (a wholly dropped
    walk) is an error, not a smaller sample."""
    by_range = read_getlogs_receipts(receipts_path)
    units = {}
    for (u, s, e) in by_range:
        units.setdefault(u, []).append((s, e))
    if expected is not None and set(units) != set(expected):
        raise ReverifyError(f"receipted units differ from the expected walk set: missing {sorted(set(expected) - set(units))[:4]}, "
                            f"unexpected {sorted(set(units) - set(expected))[:4]}")
    for u, rs in units.items():
        g4._assert_tiles(rs, pre_end + 1, post_end)
    if not by_range:
        raise ReverifyError("no getLogs receipts to re-verify")
    return [by_range[k] for k in sorted(by_range)]


def pick_reverify_ranges(ranges, pre_end, post_end, fraction=REVERIFY_FRACTION, seed=SEED):
    """Coverage-stratified sample. Strata = (fetching endpoint, unit kind [coin+position | destroyed], span segment k of
    REVERIFY_BUCKETS); a range belongs to EVERY segment its [from,to] overlaps. Every non-empty stratum must be overlapped by a
    sampled range (one whole-span range covers all segments of its kind); then random ranges are added until the sample holds
    ceil(fraction * all ranges) -- i.e. the sample size is max(coverage picks, ceil(5%)). Returns (sorted picks, {stratum: n picks overlapping})."""
    key = lambda r: (r["unit"], r["fromBlock"], r["toBlock"])
    strata = {}
    for r in ranges:
        for k in _overlapped(r, pre_end, post_end):
            strata.setdefault((r["endpoint"], _kind(r), k), []).append(r)
    rng = random.Random(seed)
    picked = {}

    def covered(st):
        return any((p["endpoint"], _kind(p)) == st[:2] and st[2] in _overlapped(p, pre_end, post_end) for p in picked.values())
    for st in sorted(strata):
        if not covered(st):
            r = rng.choice(sorted(strata[st], key=key))
            picked[key(r)] = r
    rest = [r for r in ranges if key(r) not in picked]
    for r in rng.sample(rest, min(max(0, math.ceil(len(ranges) * fraction) - len(picked)), len(rest))):
        picked[key(r)] = r
    cover = {st: sum(1 for p in picked.values() if (p["endpoint"], _kind(p)) == st[:2] and st[2] in _overlapped(p, pre_end, post_end)) for st in strata}
    if any(v == 0 for v in cover.values()):
        raise ReverifyError(f"sample misses strata {sorted(st for st, v in cover.items() if v == 0)[:3]}")
    return [picked[k] for k in sorted(picked)], {"/".join(map(str, k)): v for k, v in sorted(cover.items())}


def reverify(receipts_path, refetch, addrs, pre_end, post_end, out_path, fraction=REVERIFY_FRACTION, seed=SEED):
    """Independent re-verification: for a coverage-stratified sample of the acquisition's ranges, re-fetch on the OTHER endpoint and
    compare log count and v2 digest to what the ACQUISITION recorded in its receipts (never to a second fresh fetch). Any
    mismatch, missing/unexpected unit, a same-endpoint verifier, a legacy digest or an empty sample raises ReverifyError. Results go to
    `out_path` (separate from the acquisition receipts, whose hash is bound into the panel). Returns a summary for the schema."""
    ranges = load_acquisition_ranges(receipts_path, pre_end, post_end, expected_units(addrs))
    picks, cover = pick_reverify_ranges(ranges, pre_end, post_end, fraction, seed)
    if not picks:
        raise ReverifyError("reverify sample is empty -- nothing was re-verified")
    n = 0
    with open(out_path, "a") as out:
        for r in picks:
            other = g4.REPLAY_ENDPOINT.get(r["endpoint"])
            if other is None or other == r["endpoint"]:
                raise ReverifyError(f"no independent second endpoint for {r['endpoint']!r}")
            if not str(r["sha256"]).startswith("v2:"):
                raise ReverifyError(f"receipt {r['unit']} [{r['fromBlock']},{r['toBlock']}] carries a legacy digest")
            alt = refetch(other, r, addrs)
            dig = g4._digest(alt, with_block=True)
            if len(alt) != r["logCount"] or dig != r["sha256"]:
                raise ReverifyError(f"reverify mismatch {r['unit']} [{r['fromBlock']},{r['toBlock']}]: {r['endpoint']} "
                                    f"count={r['logCount']} {other} count={len(alt)} (digest equal: {dig == r['sha256']})")
            out.write(json.dumps({"gate": "g5", "kind": "reverify", "unit": r["unit"], "endpoint": other, "source": r["endpoint"],
                                  "fromBlock": r["fromBlock"], "toBlock": r["toBlock"], "logCount": len(alt), "sha256": dig,
                                  "t": time.time()}, sort_keys=True) + "\n")
            out.flush()
            os.fsync(out.fileno())
            n += 1
    return {"ranges_total": len(ranges), "ranges_compared": n, "strata": cover}


def run(config_path, port=None, checkpoint_dir=None, reverifier=None):
    """Verify signature -> config -> inputs (all RPC-free), then extract, re-verify and write.
    port=None is the live run: LiveRpc, checkpointed + receipt-bound in the g5 receipts dir, independent re-verification always on
    (a caller-supplied `reverifier` is refused there). Tests pass a fake port (+ optionally checkpoint_dir / reverifier(addrs) -> summary).
    SignatureError / InputMismatch / SpanError raised before any RPC carry `.pre_access = True`."""
    try:
        if port is None and reverifier is not None:
            raise PreAccessDenied("a live run always uses the built-in independent re-verification; a caller reverifier is refused")
        if port is None:   # as routed main g4 (run_all._stale_g4_env): G4_* silently re-route g4's helpers (walk endpoint, sampling, ...). G4_OUT_DIR is g5's own required work dir.
            stale = [k for k in g4_june._stale_g4_env() if k != "G4_OUT_DIR"]
            if stale:
                raise PreAccessDenied(f"environment {', '.join(stale)} must be unset for a g5 run (no env overrides of the g4 helpers)")
        config_bytes = _read_bytes(config_path)
        verified = verify_signature(config_bytes)   # includes check_config; cfg below is the checked one
        cfg, manifest = verified.cfg, verified.manifest
        verify_post_inputs(verified)
        hashes = manifest.get("input_file_sha256") or {}
        if not isinstance(hashes, dict):
            raise InputMismatch("manifest input_file_sha256 is not an object")
        frames, frame_shas = {}, {}
        for pop in cfg["populations"]:
            key = (pop["population_id"], pop["generation_id"])
            if key in frames:
                raise InputMismatch(f"population {key} listed twice")
            try:
                frames[key], frame_shas[key], _ = _load_frame(pop, set(cfg["eligible_statuses"]), hashes)
            except (KeyError, TypeError, AttributeError, csv.Error) as e:
                raise InputMismatch(f"{pop['population_id']}: malformed frame/wallet_map: {e!r}") from e
        addrs = sorted({a for fr in frames.values() for a in fr})
        binding = make_binding(verified)
    except (SignatureError, InputMismatch, SpanError) as e:
        e.pre_access = True
        raise
    except PreAccessDenied:
        raise
    except Exception as e:   # any other verification-phase failure (OSError, KeyError, bad date...) is still pre-access: exit 3, not 4
        raise PreAccessDenied(f"verification failed before any data access: {type(e).__name__}: {e}") from e
    live = port is None
    ck = lock = None
    parsing = False
    try:
        work = g4._OUT_DIR if live else checkpoint_dir
        if live:
            check_receipts_dir(cfg["out_dir"])   # before anything is created in the work dir
        if work is not None:
            os.makedirs(work, exist_ok=True)
            lock = g4_june.acquire_run_lock(work)   # before bind_work_dir: a second run on the same work dir is refused (exit 3)
        if live:
            # No RPC yet: LiveRpc only checks/binds the local work dir; the chain-id RPC runs AFTER the checkpoint (which reads the
            # receipts) is built, so any failure up to here is truthfully "no RPC was made".
            port = LiveRpc(cfg["post_end_block"], cfg["out_dir"], binding=binding, check_chain=False)
            checkpoint_dir = g4._OUT_DIR
            reverifier = lambda a: reverify(g4._RECEIPTS_PATH, port.refetch, a, cfg["pre_end_block"], cfg["post_end_block"],
                                            os.path.join(g4._OUT_DIR, REVERIFY_NAME))
        if checkpoint_dir is not None:
            raw = port
            parsing = True   # from here an earlier run's checkpoint is being parsed
            port = ck = CheckpointedPort(raw, checkpoint_dir, binding, receipts_path=g4._RECEIPTS_PATH if live else None)
    except Exception as e:
        if lock is not None:
            lock.close()
        err = e if isinstance(e, PreAccessDenied) else PreAccessDenied(f"work-dir/checkpoint setup failed before any RPC: {type(e).__name__}: {e}")
        err.checkpoint_parsed = parsing
        if err is e:
            raise
        raise err from e
    try:
        try:
            if live:
                raw.assert_chain()   # first RPC; a failure here is a post-RPC failure (exit 4), by design
            series, stats = extract(frames, port, verified)
        finally:
            if ck is not None:
                ck.close()
        attestation = None
        if reverifier is not None:
            stats["reverify"] = reverifier(addrs)   # before anything is published: a mismatch leaves no output
            if live:
                attestation = endpoint_attestation(g4._RECEIPTS_PATH, os.path.join(g4._OUT_DIR, REVERIFY_NAME))
        return write_output(frames, frame_shas, series, stats, port.receipts_sha256(), verified, attestation)
    finally:
        if lock is not None:
            lock.close()


def check_receipts_dir(out_dir):
    """The receipts/work dir (G4_OUT_DIR) is a g5-specific dir, never out/g4, never the output dir or inside it. Creates nothing."""
    default = os.path.realpath(os.path.join(os.path.dirname(g4.__file__), "..", "out", "g4"))
    if os.path.realpath(g4._OUT_DIR) == default:
        raise PreAccessDenied("set G4_OUT_DIR to a g5 receipts directory before import; refusing out/g4")
    rd, od = os.path.realpath(g4._OUT_DIR), os.path.realpath(out_dir)
    if rd == od or rd.startswith(od + os.sep):
        raise PreAccessDenied("G4_OUT_DIR (receipts) must not be, or lie inside, the output dir")


def endpoint_attestation(receipts_path, reverify_path):
    """Mirror of g4_controls.assert_single_provider for g5: every acquisition getLogs receipt ran on the walk endpoint and every
    re-verification record is WALK -> REPLAY_ENDPOINT[WALK]. Anything else raises. Returns the attestation the output schema carries."""
    walk, replay = g4.WALK, g4.REPLAY_ENDPOINT[g4.WALK]
    seen = Counter(r["endpoint"] for r in read_getlogs_receipts(receipts_path).values())
    if set(seen) != {walk}:
        raise ReverifyError(f"acquisition getLogs endpoints {dict(seen)} != the single walk endpoint {walk!r}; refusing to publish")
    rv = Counter()
    if os.path.exists(reverify_path):
        with open(reverify_path) as f:
            for line in f:
                if line.strip():
                    r = json.loads(line)
                    rv[(r.get("source"), r.get("endpoint"))] += 1
    if set(rv) != {(walk, replay)}:
        raise ReverifyError(f"reverify records (source, verifier) {dict(rv)} != {(walk, replay)}; refusing to publish")
    batch = {r.get("endpoint") for rs in read_balance_receipts(receipts_path).values() for r in rs}
    if len(batch) != 1 or not all(isinstance(b, str) and b for b in batch):
        raise ReverifyError(f"balanceOf receipts name {sorted(map(str, batch))} as their endpoint; exactly one batch endpoint is required")
    return {"walk_endpoint": walk, "replay_endpoint": replay, "batch_endpoint": batch.pop()}


# --- live port (query methods fixture-tested through a fake urllib transport; never run against the real chain before the signed run) ---

class LiveRpc:
    """Thin adapter over the accepted g4 point-query and adaptive-walk helpers, with post_end as
    their block guard. g4 appends receipts to its own `_RECEIPTS_PATH`, so G4_OUT_DIR must point
    at a g5-specific directory before g4 is imported; the default out/g4 is refused."""

    def __init__(self, post_end, out_dir, binding=None, check_chain=True):
        check_receipts_dir(out_dir)
        if binding is None:   # unbound use: one fresh dir per run
            os.makedirs(g4._OUT_DIR, exist_ok=True)
            if [n for n in os.listdir(g4._OUT_DIR) if n != LOCK_NAME]:
                raise PreAccessDenied(f"receipts dir {g4._OUT_DIR} must be empty (one per run)")
        else:                 # bound use: empty, or a resume of the identical producer/config/inputs
            bind_work_dir(g4._OUT_DIR, binding)
            repair_torn_receipts(g4._RECEIPTS_PATH)
        self.post_end = post_end
        if check_chain:
            self.assert_chain()

    def assert_chain(self):
        """The first RPC of a live run (chain id 1 on every endpoint used)."""
        for ep in sorted({g4.BATCH, g4.WALK, g4.REPLAY_ENDPOINT[g4.WALK]}):   # every endpoint this run will use, incl. the re-verifier
            rpc.assert_chain_id(ep)

    def block_date(self, block):
        b = rpc.get_block_by_number(g4.BATCH, block)
        return datetime.fromtimestamp(int(b["timestamp"], 16), tz=timezone.utc).date()

    def balance_of_many(self, coin, addrs, block):
        token = COINS[coin]
        items = [([{"to": token, "data": g1._balance_of_data(a)}, hex(block)], block) for a in addrs]
        res = g4._point_batch("balanceOf", "eth_call", items, self.post_end, coin)
        _fsync_path(g4._RECEIPTS_PATH)   # durable before the caller checkpoints the result
        return {a: g1._hex_to_int(r) for a, r in zip(addrs, res)}

    def _receipted_walk(self, unit, token, topics, s, e, n_addrs, extra):
        logs, ranges = g4._walk_fetch(token, topics, s, e, self.post_end)
        g4._assert_tiles([(a, b) for a, b, *_ in ranges], s, e)
        for a, b, cnt, dig, ep in ranges:   # the acquisition's own record: what the independent re-verification compares to
            g4._receipt({"gate": "g5", "kind": "getLogs", "unit": unit, "endpoint": ep, "fromBlock": a, "toBlock": b,
                         "addressCount": n_addrs, "logCount": cnt, "sha256": dig, "t": time.time(), **extra})
        _fsync_path(g4._RECEIPTS_PATH)      # durable before the caller checkpoints these logs
        return logs

    def transfers(self, coin, pos, addrs, s, e):
        h = _addrs_sha(addrs)
        return self._receipted_walk(walk_unit(coin, pos, addrs), COINS[coin], g4._unit_topics(pos, addrs), s, e, len(addrs),
                                    {"coin": coin, "topics_position": pos, "addrs_sha256": h})

    def destroyed_logs(self, s, e):
        return self._receipted_walk("destroyed", g1.USDT, [DESTROYED_TOPIC], s, e, 0, {})

    def refetch(self, endpoint, rec, addrs):
        """Re-fetch one acquisition range on `endpoint` (the verifier); the address chunk is found by its recorded hash."""
        if rec["unit"] == "destroyed":
            token, topics = g1.USDT, [DESTROYED_TOPIC]
        else:
            chunk = next((c for c in _chunks(addrs, ADDR_BATCH) if _addrs_sha(c) == rec["addrs_sha256"]), None)
            if chunk is None:
                raise ReverifyError(f"receipt {rec['unit']}: no address chunk of the signed universe hashes to {rec['addrs_sha256'][:12]}")
            token, topics = COINS[rec["coin"]], g4._unit_topics(rec["topics_position"], chunk)
        logs = g4._replay(endpoint, token, topics, rec["fromBlock"], rec["toBlock"], self.post_end)
        g4.check_block_timestamps(logs)
        return logs

    def receipts_sha256(self):
        g4._assert_receipts_within(g4._RECEIPTS_PATH, self.post_end)  # raises PostPeriodRead
        return g4._sha256_file(g4._RECEIPTS_PATH)
