"""S24 signing-package ASSEMBLER (offline, RPC-free, no network; never reads a post-window PANEL).

Input files it reads: the pre-window inputs, the g0 lifetime freeze files (g0_circle_events / g0_usdt_events / g0_receipts, to block 22,170,334) and the
other signed post inputs. The g0 lifetime freeze files contain events AFTER the 7 Aug 2022 cutoff (up to block 22,170,334); they are already-disclosed
post-cutoff material (prespec §0) and are listed as such in package_summary.md. They are only hashed here; R's pre-window stage (tools/s24_dryrun.R,
run in an Rscript, calling the same R/post.R function the post stage calls) opens the pre-window inputs, never a post panel or a g0 file.
It proves the pre-data-only stops of the post stage; the stops that need post data or the g0 files remain (post panel reader, g0 provenance pg_fail,
the rematch cut-point/hash downgrade, malformed g0 receipts), see semantic_checks.

Writes the UNSIGNED candidate package that `gates/g5_post.py` (and the R post stage) will accept once the author signs it:
  prespec.md, manifest.json, g5_config.json, code_manifest.txt   (the four signed roles)
  SHA256                                                         (sha256sum list of exactly those four: the file signing.json binds)
  package_summary.md                                             (every file + sha256 + every config value + "Author check: text vs code"; explicitly UNSIGNED)
A --test-only-allow-blanks build writes NO SHA256 file and sets manifest `test_only: true`, which g5 and R's verify_post_signature refuse.
It NEVER writes `signing.json`, never fabricates a signature and never claims signing: that is the author's act (S10b).

It is the inverse of the verification chain, and reuses it (no duplicated lists): CODE_FILES / POST_* keys / check_r_half / check_config /
verify_code_manifest / verify_post_inputs / _load_frame all come from g5_post. Refusal (PackageError, exit 3) writes NOTHING.

Entry: `python3 run_all.py --gate s24_package --outdir DIR ...`  (or `python3 -m tools.s24_package ...`). Lives in tools/, not gates/:
every gates/*.py is bound by the signed code manifest and mirrored in R/post_gate.R; the assembler is the author's offline tool, not
signed code. Assemble at the commit that will be signed: code hashes are taken from HEAD blobs and any producer file differing from
HEAD is refused.
"""
import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from types import SimpleNamespace

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from gates import g5_post as g5   # noqa: E402
from lib import blocks            # noqa: E402

ROLE_FILES = {"prespec_text": "prespec.md", "numerical_manifest": "manifest.json",
              "executable_config": "g5_config.json", "code_manifest": "code_manifest.txt"}
CHECKED_EXTRA_PACKAGES = ("DRDID",)   # frozen in R/drdid.R DRDID_FREEZE (not part of the signed r_package_versions; see package_summary)
_DRDID_PIN_RE = re.compile(r'package\s*=\s*"DRDID"\s*,\s*version\s*=\s*"([^"]+)"')
UNSIGNED_LINE = "UNSIGNED — awaiting author signature"


class PackageError(RuntimeError):
    """Refusal: nothing is written."""


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _finite(obj, where="value"):
    """NaN / +-Inf anywhere in the values is refused (the strict parsers on both sides reject them; do not even try to emit one)."""
    if isinstance(obj, float) and not math.isfinite(obj):
        raise PackageError(f"non-finite number in {where}: {obj!r}")
    if isinstance(obj, dict):
        for k, v in obj.items():
            _finite(v, f"{where}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _finite(v, f"{where}[{i}]")


def _dumps(obj):
    """Strict canonical JSON: sorted keys, allow_nan=False, ASCII only (ensure_ascii), LF, no BOM."""
    return (json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")


def _regular(path, what):
    if not isinstance(path, str) or not path:
        raise PackageError(f"{what}: no path given")
    p = os.path.abspath(path)
    if os.path.islink(p) or not os.path.isfile(p):
        raise PackageError(f"{what}: {p} is missing, not a regular file or a symlink")
    try:
        with open(p, "rb") as f:
            return p, f.read()
    except OSError as e:
        raise PackageError(f"{what}: {p} unreadable: {e}") from e


TOOL_REL, DRYRUN_REL = "tools/s24_package.py", "tools/s24_dryrun.R"
DRYRUN_R = os.path.join(ROOT, DRYRUN_REL)
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# |declared bridge/post_end block - the block expected for that date's end| must not exceed this. 3,000 blocks ~ 10 h at ~12 s: the expectation is a linear
# interpolation between the two pinned timestamps (offline, no RPC), and real block times drift only by minutes over the ~3 weeks event->merge, so
# a date wrong by even one day (~7,200 blocks) is refused while ordinary interpolation error (a few hundred blocks) passes.
BLOCK_TOLERANCE = 3000
# summary forging: C0 controls, DEL + C1 controls, U+2028/2029 line separators, bidi marks/embeddings/isolates, zero-width marks, backtick, '|'
_BAD_TEXT = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\u200b-\u200f\u202a-\u202e\u2066-\u2069`|]")
# unfilled prespec fields: >= 3 underscores (plain or markdown-escaped "\\_"), TBD / XXX placeholders, a field that is only an em/en dash, or a
# "(pipeline records)" field with nothing after the colon. The "Signed: ____ Date: ____" line is the author's own act and is exempt (_SIGNED_LINE_RE).
_BLANK_RE = re.compile(r"(?:\\?_){3,}|\bTBD\b|\bXXX+\b|(?<=[:\[,(])\s*[\u2014\u2013]+\s*(?=[,.;\])]|$)|\(pipeline records\)\*?\s*:\s*[_\s]*(?:[.;]|$)",
                       re.M | re.I)
_SIGNED_LINE_RE = re.compile(r"^\W*Signed\b.*\bDate\b", re.I)
_TRIM_LINE_RE = re.compile(r"propens.*(?:trim|0\.99|dropped)|(?:trim|dropped).*propens", re.I)
_CAP_LINE_RE = re.compile(r"cut-?points?", re.I)
_CAP_PAIR_RE = re.compile(r"(?<![\w])([DA])\s*\[\s*([^\[\],]*?)\s*,\s*([^\[\],]*?)\s*\]")


def _git_head():
    """HEAD commit and the sha256 of the HEAD blobs of this tool and of tools/s24_dryrun.R. Hard-fails (no 'unknown'): a package without provenance is
    never emitted, and neither tool may differ from its HEAD blob."""
    r = subprocess.run(["git", "-C", g5.REPO_ROOT, "rev-parse", "--verify", "HEAD"], capture_output=True, text=True, env=g5._git_env())
    head = r.stdout.strip()
    if r.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise PackageError(f"cannot read the repo HEAD commit in {g5.REPO_ROOT} (git rev-parse failed): refusing to emit a package without provenance")
    shas = {}
    for rel in (TOOL_REL, DRYRUN_REL):
        b = subprocess.run(["git", "-C", g5.REPO_ROOT, "show", f"HEAD:{rel}"], capture_output=True, env=g5._git_env())
        if b.returncode != 0:
            raise PackageError(f"{rel} is not a tracked file at HEAD of {g5.REPO_ROOT}: refusing to emit a package without provenance")
        with open(os.path.join(ROOT, rel), "rb") as f:
            if f.read() != b.stdout:
                raise PackageError(f"{rel} differs from its HEAD blob: assemble with the committed assembler")
        shas[rel] = _sha(b.stdout)
    return head, shas[TOOL_REL], shas[DRYRUN_REL]


def _clean(v, what):
    """Anything that reaches package_summary.md (paths, ids, statuses) must be free of control characters, backticks and '|': they would forge
    summary lines / table cells."""
    if not isinstance(v, str) or not v or _BAD_TEXT.search(v):
        raise PackageError(f"{what} {v!r} is empty or contains a control character, backtick or '|' (summary forging)")
    return v


def expected_block(day_end):
    """Block expected at 23:59:59 UTC of ISO date `day_end`, linear between the pinned event_block and merge_block timestamps (offline, no RPC)."""
    pin = blocks.load_pinned()
    (b0, t0), (b1, t1) = ((pin[k]["block"], datetime.fromisoformat(pin[k]["timestamp"].replace("Z", "+00:00")).timestamp()) for k in ("event_block", "merge_block"))
    t = datetime.fromisoformat(day_end + "T23:59:59+00:00").timestamp()
    return b0 + (t - t0) * (b1 - b0) / (t1 - t0)


def check_dates_and_blocks(p):
    for k in ("post_start_date", "bridge_date", "post_end_date"):
        v = p[k]
        if not isinstance(v, str) or not _DATE_RE.fullmatch(v):
            raise PackageError(f"{k} {v!r} must be YYYY-MM-DD")
        try:
            date.fromisoformat(v)
        except ValueError as e:
            raise PackageError(f"{k} {v!r} is not an ISO date") from e
    pin = blocks.load_pinned()
    ev, mg = pin["event_block"]["block"], pin["merge_block"]["block"]
    br, po = p["bridge_end_block"], p["post_end_block"]
    if not all(isinstance(b, int) and not isinstance(b, bool) for b in (br, po)):
        raise PackageError("bridge_end_block / post_end_block must be integers")
    if not (ev < br < po < mg):
        raise PackageError(f"need event_block {ev} < bridge_end {br} < post_end {po} < merge_block {mg} (config/blocks.json)")
    for name, blk, day in (("bridge_end_block", br, p["bridge_date"]), ("post_end_block", po, p["post_end_date"])):
        exp = expected_block(day)
        if abs(blk - exp) > BLOCK_TOLERANCE:
            raise PackageError(f"{name} {blk} is inconsistent with {day}: about block {exp:.0f} expected (tolerance +-{BLOCK_TOLERANCE} blocks)")


def _unsigned_text(t):
    """The prespec with the author's own 'Signed: ____ Date: ____' line blanked (same line count): that blank is filled by signing, never by the assembler."""
    return "\n".join("" if _SIGNED_LINE_RE.match(l) else l for l in t.split("\n"))


def check_prespec(text, allow_blanks):
    try:
        t = text.decode("utf-8")
    except UnicodeDecodeError as e:
        raise PackageError("prespec text is not UTF-8") from e
    if not allow_blanks:
        u = _unsigned_text(t)
        m = _BLANK_RE.search(u)
        if m:
            raise PackageError(f"prespec has an unfilled blank near {u[max(0, m.start() - 40):m.end()].strip()!r} ('____' / '\\_\\_\\_' / TBD / XXX / an em-dash-only "
                               "field / an empty '(pipeline records)' field): production mode refuses unfilled fields (the 'Signed: / Date:' line is exempt)")
    return t


def _num(x):
    x = x.strip().replace("\u2212", "-")
    return float(x) if re.fullmatch(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", x) else None


def author_check_caps(t, frozen):
    """Cap values the author wrote in the prespec ('Cut-points ...: D [a, b], A [c, d]') vs the frozen analysis_parameters cap_values.
    Refuses a mismatch of a PARSEABLE pair (compared at the precision written: |x - y| <= 0.5 unit of the last written digit); an unparseable or
    missing one is returned as text for the summary's author check. Returns list of lines."""
    found = {"D": [], "A": []}
    for line in t.split("\n"):
        if _CAP_LINE_RE.search(line):
            for k, lo, hi in _CAP_PAIR_RE.findall(line):
                found[k].append((lo, hi))
    out = []
    for k in ("D", "A"):
        want = frozen[k]
        if len(found[k]) != 1:
            out.append(f"- cap {k}: prespec has {len(found[k])} parseable-looking `{k} [a, b]` entries on 'cut-points' lines (found {found[k]!r}); frozen cap_values[{k}] = {want!r}: CHECK BY HAND")
            continue
        vals = [_num(x) for x in found[k][0]]
        if None in vals:
            out.append(f"- cap {k}: prespec values {found[k][0]!r} are not numbers; frozen cap_values[{k}] = {want!r}: CHECK BY HAND")
            continue
        for v, raw, w in zip(vals, found[k][0], want):
            dec = len(raw.split(".")[1].split("e")[0].split("E")[0]) if "." in raw else 0
            if abs(v - w) > 0.5 * 10 ** (-dec) + 1e-12:
                raise PackageError(f"prespec cap {k} {found[k][0]!r} does not match the frozen analysis_parameters cap_values[{k}] = {want!r} (compared at the written precision)")
        out.append(f"- cap {k}: prespec {found[k][0]!r} matches the frozen cap_values[{k}] = {want!r} at the written precision")
    return out


def r_dry_run(post_inputs, cwd):
    """R's OWN pre-window readers (tools/s24_dryrun.R: po_read_params / po_check_population / po_read_membership / po_read_addr_file / load_pre_inputs) on
    the real candidate inputs. Returns {population_id, generation_id, matched, retained|None}. Any R stop = refusal."""
    g = lambda k: post_inputs[k][0] if k in post_inputs else "-"
    argv = ["Rscript", DRYRUN_R, g("pre/covariates.csv"), g("pre/wallet_coin_day_pre.csv"), g("match_membership"), g("analysis_parameters"),
            g("eligible_frame_coverage"), g("usdc_pool_flag"), g("deposit_counts")]
    try:
        r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.SubprocessError) as e:
        raise PackageError(f"R dry run could not run: {e}") from e
    if r.returncode != 0:
        raise PackageError(f"R's own readers refuse these inputs (the post stage would po_stop): {(r.stderr or r.stdout).strip()[-1500:]}")
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as e:
        raise PackageError(f"R dry run printed no result: {r.stdout[-500:]!r}") from e


def semantic_checks(pkg, frames):
    """Pre-signature semantics proven on the real inputs. Checked HERE (R's own shared function R/post.R po_pre_stage via tools/s24_dryrun.R, plus Python):
    analysis_parameters / membership / coverage schema and ids; matched subset of retained; required wallets present in the pre population; matched
    wallets with baseline B > 0; recomputed cap values equal the frozen cap_values; every eligible wallet has the 60 baseline days; pool / deposit file
    schemas; g5's eligible set == coverage retained set; the prespec's filled cap values vs cap_values.
    NOT checked (needs post data or the g0 files, so the post stage can still stop after post data was fetched):
    - the post panel reader's stops;
    - g0 provenance (pg_fail on the receipts / veto);
    - the rematch cut-point / hash downgrade (a failed re-derivation silently turns rematch rows into 'not estimable');
    - malformed g0 receipts."""
    cfg = pkg["cfg"]
    (pop,) = cfg["populations"]
    res = r_dry_run(pkg["post_inputs"], g5.REPO_ROOT)
    for k in ("matched", "retained"):
        if not (isinstance(res.get(k), list) or (k == "retained" and res.get(k) is None)):
            raise PackageError(f"R dry run returned {k}={res.get(k)!r}, not an array of addresses")
    if (res.get("population_id"), res.get("generation_id")) != (pop["population_id"], pop["generation_id"]):
        raise PackageError(f"analysis_parameters population/generation {(res.get('population_id'), res.get('generation_id'))} != config populations "
                           f"{(pop['population_id'], pop['generation_id'])}")
    frame = set(frames[0])
    matched, retained = set(res["matched"]), res.get("retained")
    if retained is not None:
        if set(retained) != frame:
            raise PackageError(f"g5 frame eligible set (eligible_statuses {cfg['eligible_statuses']}, {len(frame)} addresses) != eligible_frame_coverage retained set "
                               f"({len(set(retained))} addresses): R's post panel reader would po_stop (post panel addresses differ from the retained frame)")
        if not matched <= set(retained):
            raise PackageError("matched wallets are not all in the retained eligible frame")
    elif not matched <= frame:
        raise PackageError("matched wallets are not all in g5's eligible frame")
    t = pkg["files"]["prespec.md"].decode("utf-8")
    pkg["author_check"] = {"caps": author_check_caps(t, res["cap_values"]), "r_version": res["r_version"], "drdid_trimming": res["drdid_trimming"],
                           "trim_lines": [l.strip() for l in t.split("\n") if _TRIM_LINE_RE.search(l)]}


def resolve_post_inputs(inputs):
    """inputs: {post_input_key: path} (keys exactly as g5/R name them). Applies every key rule; returns {key: (abspath, bytes)}."""
    keys = set(inputs)
    refused = sorted(keys & set(g5.POST_REFUSED_KEYS))
    if refused:
        raise PackageError(f"refused post-window input key(s) {refused}: they have no signature-bound producer")
    unknown = sorted(keys - set(g5.POST_REQUIRED_KEYS) - set(g5.POST_OPTIONAL_KEYS))
    if unknown:
        raise PackageError(f"unknown post input key(s) {unknown}")
    missing = [k for k in g5.POST_REQUIRED_KEYS if not inputs.get(k)]
    if missing:
        raise PackageError(f"required post input(s) missing: {missing}")
    g0 = [k for k in g5.POST_G0_KEYS if inputs.get(k)]
    if g0 and len(g0) != len(g5.POST_G0_KEYS):
        raise PackageError(f"the g0 source inputs {list(g5.POST_G0_KEYS)} are all-or-none (got {g0})")
    if g0 and not inputs.get("eligible_frame_coverage"):
        raise PackageError("g0 inputs given without eligible_frame_coverage: the post stage would po_stop after the veto walk "
                           "(R/post.R: the §5.9 list is derived against the eligible frame)")
    return {k: _regular(inputs[k], f"post input {k}") for k in sorted(inputs) if inputs[k]}


def pin_r_packages(installed=None):
    """Signed r_package_versions captured from the installed R. DRDID (frozen in R/drdid.R) must equal its freeze or we refuse."""
    installed = g5.installed_r_version if installed is None else installed
    pins = {}
    for p in g5.POST_PINNED_PACKAGES:
        v = installed(p)
        if v is None or not g5._RVER_RE.fullmatch(v):
            raise PackageError(f"R package {p}: installed version {v!r} is missing / not an exact version string")
        pins[p] = v
    m = _DRDID_PIN_RE.search(g5._committed_bytes("R/drdid.R").decode("utf-8", "replace"))
    if not m:
        raise PackageError("cannot read the DRDID freeze version from R/drdid.R (DRDID_FREEZE)")
    have = installed("DRDID")
    if have is None or have != m.group(1):
        raise PackageError(f"installed DRDID {have!r} differs from the frozen {m.group(1)!r} (R/drdid.R DRDID_FREEZE)")
    return pins, {"DRDID": have}


def _r_string():
    v = g5.installed_r_string()
    if v is None or not v.strip():
        raise PackageError("cannot read R.version.string from Rscript (the signed manifest records it)")
    return v


def build_package(p, installed=None):
    """p: dict with keys out_dir, bridge_end_block, post_end_block, post_start_date, bridge_date, post_end_date, eligible_statuses,
    populations [(pid, gid, frame_path, wallet_map_path)], post_inputs {key: path}, prespec_text path. Pure: returns
    {"files": {name: bytes}, "cfg", "manifest", "post_inputs", "summary"}; raises PackageError, writes nothing."""
    _finite(p, "params")
    allow_blanks = bool(p.get("allow_blanks", False))
    for k in ("out_dir", "bridge_end_block", "post_end_block", "post_start_date", "bridge_date", "post_end_date",
              "eligible_statuses", "populations", "post_inputs", "prespec_text"):
        if p.get(k) in (None, "", [], {}):
            raise PackageError(f"required parameter {k} missing")
    check_dates_and_blocks(p)
    _clean(p["out_dir"], "g5 out_dir")
    out = os.path.abspath(p["out_dir"])
    if os.path.lexists(out) and (os.path.islink(out) or not os.path.isdir(out) or os.listdir(out)):
        raise PackageError(f"g5 out_dir {out} exists and is not an empty directory (g5 publishes into a fresh directory)")
    for st in p["eligible_statuses"]:
        _clean(st, "eligible status")
    post_inputs = resolve_post_inputs(p["post_inputs"])
    for k in REQUIRED_H2_INPUTS:   # R/post.R skips H2 / s5.10 silently when these are absent; the pre-sign coverage check must never be bypassable by omission
        if k not in post_inputs:
            raise PackageError(f"post input {k!r} is required (--{POST_FLAGS[k]}): without it the post stage silently skips H2 / s5.10 and the pre-sign coverage check is bypassed")
    for k, (ap, _) in post_inputs.items():
        _clean(k, "post input key"), _clean(ap, f"post input {k} path")
    _clean(os.path.abspath(p["prespec_text"]), "prespec path")
    _, prespec = _regular(p["prespec_text"], "prespec text")
    if not prespec.strip():
        raise PackageError("prespec text is empty")
    check_prespec(prespec, allow_blanks)
    keys = [(x[0], x[1]) for x in p["populations"]]
    if len(set(keys)) != len(keys):
        raise PackageError(f"duplicate (population_id, generation_id) in {keys}")
    if len(keys) != 1:
        raise PackageError(f"exactly one population is supported (R's post panel reader needs a single population_id; g5 builds one panel): got {keys}")
    pops, in_hashes = [], {}
    for pid, gid, fr, wm in p["populations"]:
        _clean(pid, "population_id"), _clean(gid, "generation_id"), _clean(os.path.abspath(fr), "frame path"), _clean(os.path.abspath(wm), "wallet_map path")
        fp, fb = _regular(fr, f"population {pid} frame")
        wp, wb = _regular(wm, f"population {pid} wallet_map")
        pops.append({"population_id": pid, "generation_id": gid, "frame_path": fp, "wallet_map_path": wp})
        in_hashes[fp], in_hashes[wp] = _sha(fb), _sha(wb)
    pre_end = blocks.load_pinned()["pre_period_end"]["block"]
    cfg = {"out_dir": os.path.abspath(p["out_dir"]), "pre_end_block": pre_end, "bridge_end_block": p["bridge_end_block"],
           "post_end_block": p["post_end_block"], "post_start_date": p["post_start_date"], "bridge_date": p["bridge_date"],
           "post_end_date": p["post_end_date"], "eligible_statuses": list(p["eligible_statuses"]), "populations": pops,
           "post_input_paths": {k: v[0] for k, v in post_inputs.items()}}
    pins, extra_pins = pin_r_packages(installed)
    manifest = {k: cfg[k] for k in g5._MANIFEST_BOUND}
    if allow_blanks:
        manifest["test_only"] = True        # g5 check_r_half/check_config and R verify_post_signature refuse this key: a test-only build can never verify
    head, tool_sha, dryrun_sha = _git_head()
    manifest.update(r_version=_r_string(), assembled_at_commit=head, assembler_sha256=tool_sha, dryrun_sha256=dryrun_sha, populations=[[x["population_id"], x["generation_id"]] for x in pops], input_file_sha256=in_hashes,
                    post_input_file_sha256={k: _sha(v[1]) for k, v in post_inputs.items()}, r_package_versions=pins)
    _finite(cfg, "config"), _finite(manifest, "manifest")
    code = [(rel, _sha(g5._committed_bytes(rel))) for rel in sorted(g5.ALL_CODE_FILES)]   # raises SignatureError if not tracked / != HEAD
    files = {ROLE_FILES["prespec_text"]: prespec, ROLE_FILES["numerical_manifest"]: _dumps(manifest),
             ROLE_FILES["executable_config"]: _dumps(cfg),
             ROLE_FILES["code_manifest"]: "".join(f"{h}  {rel}\n" for rel, h in code).encode("ascii")}
    if not allow_blanks:     # a TEST-ONLY build gets no SHA256 list: there is nothing signing.json could bind
        files["SHA256"] = "".join(f"{_sha(files[n])}  {n}\n" for n in ROLE_FILES.values()).encode("ascii")
    pkg = {"files": files, "cfg": cfg, "manifest": manifest, "post_inputs": post_inputs, "code": code, "extra_pins": extra_pins,
           "allow_blanks": allow_blanks}
    frames = check_unsigned(pkg, installed)
    semantic_checks(pkg, frames)
    files["package_summary.md"] = summary(pkg).encode("utf-8")
    return pkg


def check_unsigned(pkg, installed=None):
    """Everything g5 checks that needs no signature: re-parse the emitted bytes with g5's strict parser, then run g5's own checks on them."""
    f = pkg["files"]
    try:
        cfg, man = g5._loads(f["g5_config.json"], "candidate config"), g5._loads(f["manifest.json"], "candidate manifest")
        if pkg["allow_blanks"]:
            # a test-only manifest is REFUSED by g5 by design (that is its point); prove that, then check the rest on the manifest without the marker
            try:
                g5.check_r_half(man, installed)
            except g5.SignatureError:
                pass
            else:
                raise PackageError("g5 check_r_half accepted a test_only manifest: a test-only build must be unsignable")
            man = {k: v for k, v in man.items() if k != "test_only"}
            if "SHA256" in f:
                raise PackageError("a test-only build must not carry a SHA256 list")
        g5.check_r_half(man, installed)
        g5.check_config(cfg, man, blocks.load_pinned()["pre_period_end"]["block"])
        g5.verify_code_manifest(f["code_manifest.txt"])
        g5.verify_post_inputs(SimpleNamespace(manifest=man, cfg=cfg))
        if "SHA256" in f:
            listed = g5.parse_sha256_list(f["SHA256"].decode("ascii"))
            if listed != {n: _sha(f[n]) for n in ROLE_FILES.values()}:
                raise g5.SignatureError("SHA256 list does not equal the four role files")
        frames = [g5._load_frame(pop, set(cfg["eligible_statuses"]), man["input_file_sha256"])[0] for pop in cfg["populations"]]
    except (g5.SignatureError, g5.InputMismatch, g5.SpanError) as e:
        raise PackageError(f"candidate fails g5's own check: {type(e).__name__}: {e}") from e
    return frames


def summary(pkg):
    f, cfg = pkg["files"], pkg["cfg"]
    test_only = pkg.get("allow_blanks")
    man = pkg["manifest"]
    L = ["# S24 signing package (candidate)", "", f"**{UNSIGNED_LINE}**", ""]
    if test_only:
        L += ["**TEST-ONLY BUILD: unfilled prespec blanks were allowed. No SHA256 list exists and the manifest carries `test_only: true`; g5 and R refuse it. "
              "This package must NOT be signed.**", "",
              "No `signing.json` exists here and none was fabricated."]
    else:
        L += ["No `signing.json` exists here and none was fabricated. To sign: copy the five package files into the repo-root `prespec/`, "
              "write `signing.json` (`signed_by`, `signed_date`, `sha256_list_sha256`, `files`) and commit. "
              f"`sha256_list_sha256` for this exact `SHA256` is `{_sha(f['SHA256'])}`; `files` = {json.dumps(ROLE_FILES, sort_keys=True)}."]
    L += ["", f"Assembled at repo HEAD `{man['assembled_at_commit']}` by `tools/s24_package.py` (HEAD blob sha256 `{man['assembler_sha256']}`) and the R dry run "
          f"`tools/s24_dryrun.R` (HEAD blob sha256 `{man['dryrun_sha256']}`); code hashes below are of the HEAD blobs; re-assemble if HEAD moves.", "",
          "## Signed role files", "", "| role | file | sha256 |", "|---|---|---|"]
    L += [f"| {r} | {n} | `{_sha(f[n])}` |" for r, n in ROLE_FILES.items()]
    ac = pkg["author_check"]
    L += ["", "## Author check: text vs code", "",
          f"R: `{man['r_version']}` (signed manifest `r_version`; the dry run ran under `{ac['r_version']}`). Nothing below is enforced by the assembler except where it says matches/refused; read both sides.", "",
          "### Trimming (prespec §5 item 5 vs R/drdid.R)", "", "Code, `DRDID_FREEZE$trimming` (rule incl. >= vs >; controls only):", "",
          f"> {ac['drdid_trimming']}", "", "Prespec, every line that mentions propensity together with trimming / 0.99 / dropped:", ""]
    L += [f"> {json.dumps(x)}" for x in ac["trim_lines"]] or ["> (no such line found in the prespec: CHECK BY HAND)"]
    L += ["", "### Cap values (prespec 'Cut-points' vs frozen analysis_parameters)", ""] + ac["caps"]
    g0 = [k for k in g5.POST_G0_KEYS if k in cfg["post_input_paths"]]
    if g0:
        L += ["", "## Post-cutoff material read (hashed only)", "",
              f"The g0 lifetime freeze files run to block {blocks.load_pinned()['apr2025']['block']:,} and contain events after the 7 Aug 2022 cutoff "
              "(already disclosed, prespec §0). They are hashed here, never parsed, and no post panel is read:"]
        L += [f"- `{k}`: {json.dumps(cfg['post_input_paths'][k])}" for k in g0]
    L += ["", "## Config values (g5_config.json)", ""]
    for k in sorted(cfg):
        if k not in ("populations", "post_input_paths"):
            L.append(f"- `{k}`: `{json.dumps(cfg[k])}`")
    L += ["", "## Populations", ""] + [f"- {json.dumps(x['population_id'])} / {json.dumps(x['generation_id'])}: frame {json.dumps(x['frame_path'])}, wallet_map {json.dumps(x['wallet_map_path'])}" for x in cfg["populations"]]
    L += ["", "## Input files (sha256)", "", "| key / path | sha256 |", "|---|---|"]
    L += [f"| post `{k}` = {json.dumps(cfg['post_input_paths'][k])} | `{h}` |" for k, h in sorted(man["post_input_file_sha256"].items())]
    L += [f"| frame/wallet_map {json.dumps(k)} | `{h}` |" for k, h in sorted(man["input_file_sha256"].items())]
    L += ["", "## R packages", ""] + [f"- signed `r_package_versions[{k}]` = `{v}`" for k, v in sorted(man["r_package_versions"].items())]
    L += [f"- {k} {v}: checked equal to the freeze in R/drdid.R (not part of the signed pin set)" for k, v in sorted(pkg["extra_pins"].items())]
    L += ["", "## Code manifest (sha256 of HEAD blobs)", "", "| file | sha256 |", "|---|---|"] + [f"| {rel} | `{h}` |" for rel, h in pkg["code"]]
    L += ["", f"**{UNSIGNED_LINE}**", ""]
    return "\n".join(L)


def write_package(pkg, outdir):
    """All-or-nothing: written into a sibling temp dir, then renamed. The outdir must not exist or be an empty directory."""
    outdir = os.path.abspath(outdir)
    if os.path.lexists(outdir) and (os.path.islink(outdir) or not os.path.isdir(outdir) or os.listdir(outdir)):
        raise PackageError(f"outdir {outdir} exists and is not an empty directory")
    os.makedirs(os.path.dirname(outdir), exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".s24-", dir=os.path.dirname(outdir))
    try:
        for n, b in pkg["files"].items():
            with open(os.path.join(tmp, n), "wb") as fh:
                fh.write(b)
        os.rename(tmp, outdir)   # POSIX: atomically replaces an EMPTY directory, fails (ENOTEMPTY/EEXIST) if it became non-empty meanwhile: no rmdir window
    except OSError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise PackageError(f"could not move the package into {outdir}: {e}") from e
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


# --- TEST-ONLY: fixture signature -------------------------------------------------------------------------------------------------
def _git(repo, *a):
    subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@invalid", "-c", "commit.gpgsign=false", *a], cwd=repo, check=True, capture_output=True, env=g5._git_env())


def fixture_signed_repo(files, dest):
    """TEST ONLY. A throwaway git repo at `dest` holding the HEAD bytes of every bound code file plus prespec/ = the candidate files and a
    FIXTURE signing record. This is never written next to the package and is not a signature by anyone."""
    for rel in g5.ALL_CODE_FILES:
        os.makedirs(os.path.dirname(os.path.join(dest, rel)) or dest, exist_ok=True)
        with open(os.path.join(dest, rel), "wb") as fh:
            fh.write(g5._committed_bytes(rel))
    sd = os.path.join(dest, "prespec")
    os.makedirs(sd, exist_ok=True)
    for n in (*ROLE_FILES.values(), "SHA256"):
        with open(os.path.join(sd, n), "wb") as fh:
            fh.write(files[n])
    with open(os.path.join(sd, "signing.json"), "w") as fh:
        json.dump({"signed_by": "FIXTURE-ONLY-NOT-A-SIGNATURE", "signed_date": "2000-01-01",
                   "sha256_list_sha256": _sha(files["SHA256"]), "files": ROLE_FILES}, fh)
    _git(dest, "init", "-q")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "fixture")


def selfcheck_fixture_signature(pkg):
    """TEST ONLY: run g5's FULL verify_signature + verify_post_inputs on the candidate, under a fixture signature in a temp repo."""
    if pkg.get("allow_blanks"):
        raise PackageError("a test-only build is unsignable by design: no fixture signature can be built for it")
    saved, tmp = g5.REPO_ROOT, tempfile.mkdtemp(prefix="s24-fixture-")
    try:
        fixture_signed_repo(pkg["files"], tmp)
        g5.REPO_ROOT = tmp
        v = g5.verify_signature(pkg["files"]["g5_config.json"])
        g5.verify_post_inputs(v)
        return v
    except (g5.SignatureError, g5.InputMismatch, g5.SpanError) as e:
        raise PackageError(f"fixture-signature self-check failed: {type(e).__name__}: {e}") from e
    finally:
        g5.REPO_ROOT = saved
        shutil.rmtree(tmp, ignore_errors=True)


# --- CLI ----------------------------------------------------------------------------------------------------------------------------
REQUIRED_H2_INPUTS = ("usdc_pool_flag", "deposit_counts")
POST_FLAGS = {"pre/covariates.csv": "pre-covariates", "pre/wallet_coin_day_pre.csv": "wallet-coin-day-pre", "match_membership": "match-membership",
              "analysis_parameters": "analysis-parameters", "eligible_frame_coverage": "eligible-frame-coverage",
              "usdc_pool_flag": "usdc-pool-flag", "deposit_counts": "deposit-counts", "g0_circle_events": "g0-circle-events",
              "g0_usdt_events": "g0-usdt-events", "g0_receipts": "g0-receipts"}


def parser():
    a = argparse.ArgumentParser(prog="--gate s24_package", description=__doc__.split("\n")[0])
    a.add_argument("--outdir", required=True, help="NEW (or empty) directory for the candidate package")
    a.add_argument("--g5-out-dir", required=True, help="the config's out_dir: where g5 will publish the panel")
    a.add_argument("--prespec-text", required=True, help="the prespec text file to be signed (copied byte-for-byte as prespec.md)")
    for k, flag in POST_FLAGS.items():
        a.add_argument(f"--{flag}", help=f"post input {k!r}")
    a.add_argument("--extra-input", action="append", default=[], metavar="KEY=PATH", help="any other post_input key (validated: refused/unknown keys are denied)")
    a.add_argument("--population", action="append", nargs=4, required=True, metavar=("POP_ID", "GEN_ID", "FRAME", "WALLET_MAP"))
    a.add_argument("--eligible-status", action="append", required=True, dest="eligible_statuses")
    a.add_argument("--bridge-end-block", type=int, required=True)
    a.add_argument("--post-end-block", type=int, required=True)
    a.add_argument("--post-start-date", required=True)
    a.add_argument("--bridge-date", required=True)
    a.add_argument("--post-end-date", required=True)
    a.add_argument("--test-only-allow-blanks", action="store_true", help="TEST ONLY: allow unfilled prespec blanks (production refuses); writes NO SHA256 and sets manifest test_only=true (unsignable)")
    a.add_argument("--selfcheck-fixture-signature", action="store_true",
                   help="TEST ONLY: also run g5's full signature chain under a throwaway fixture signature (never written to --outdir)")
    return a


def run(args):
    post = {k: getattr(args, v.replace("-", "_")) for k, v in POST_FLAGS.items()}
    for kv in args.extra_input:
        k, sep, path = kv.partition("=")
        if not sep:
            raise PackageError(f"--extra-input {kv!r} is not KEY=PATH")
        post[k] = path
    pkg = build_package({"out_dir": args.g5_out_dir, "bridge_end_block": args.bridge_end_block, "post_end_block": args.post_end_block,
                         "post_start_date": args.post_start_date, "bridge_date": args.bridge_date, "post_end_date": args.post_end_date,
                         "eligible_statuses": args.eligible_statuses, "populations": [tuple(x) for x in args.population],
                         "post_inputs": {k: v for k, v in post.items() if v}, "prespec_text": args.prespec_text,
                         "allow_blanks": args.test_only_allow_blanks})
    if args.selfcheck_fixture_signature:
        selfcheck_fixture_signature(pkg)
    write_package(pkg, args.outdir)
    return pkg


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        pkg = run(args)
    except (PackageError, g5.SignatureError, g5.InputMismatch, g5.SpanError) as e:
        print(f"s24_package REFUSED (nothing written): {type(e).__name__}: {e}")
        return 3
    if pkg["allow_blanks"]:
        print(f"[s24_package] !!! TEST-ONLY BUILD wrote {os.path.abspath(args.outdir)} ({len(pkg['files'])} files): NO SHA256 list, manifest test_only=true, "
              "g5 and R refuse it. DO NOT SIGN.")
        return 0
    print(f"[s24_package] wrote {os.path.abspath(args.outdir)} ({len(pkg['files'])} files); {UNSIGNED_LINE}; "
          f"sha256_list_sha256 = {_sha(pkg['files']['SHA256'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
