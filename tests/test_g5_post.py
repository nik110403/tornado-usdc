"""Unit tests for gates/g5_post.py. Synthetic fixtures only; the RPC layer is a fake port.
No network, no out/ data."""
import copy
import shutil
import csv
import hashlib
import importlib
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from gates import g5_post as g5

PRE = 15_298_282                 # == config/blocks.json pre_period_end (check_config pins this)
BRIDGE_END = PRE + 100           # 100 blocks per day, 864 s each
POST_END = PRE + 100 + 35 * 100
T0 = int(datetime(2022, 8, 8, tzinfo=timezone.utc).timestamp())   # timestamp of block PRE+1
USDC, USDT = g5.COINS["USDC"], g5.COINS["USDT"]
A, B, C, D, E = ("0x" + c * 40 for c in "abcde")


def ts(block):
    return T0 + (block - PRE - 1) * 864


def word(addr):
    return "0x" + "0" * 24 + addr[2:]


class FakePort:
    """Deterministic chain: events = (block, coin, frm, to, value); destroyed = (block, addr, amt)."""

    def __init__(self, opening, events, destroyed=(), tamper_bridge=None, tamper_post=None):
        self.opening, self.events, self.destroyed = opening, list(events), list(destroyed)
        self.tamper_bridge, self.tamper_post = tamper_bridge, tamper_post
        self.calls = []

    def block_date(self, block):
        self.calls.append(("block_date", block))
        return datetime.fromtimestamp(ts(block), tz=timezone.utc).date()

    def _bal(self, coin, a, block):
        v = self.opening.get((coin, a), 0)
        for b, c, f, t, val in self.events:
            if c == coin and b <= block:
                v += (val if t == a else 0) - (val if f == a else 0)
        if coin == "USDT":
            v -= sum(amt for b, x, amt in self.destroyed if x == a and b <= block)
        return v

    def balance_of_many(self, coin, addrs, block):
        self.calls.append(("bal", coin, block))
        out = {a: self._bal(coin, a, block) for a in addrs}
        if block == BRIDGE_END and self.tamper_bridge is not None:
            out[A] += self.tamper_bridge
        if block == POST_END and self.tamper_post is not None:
            out[A] += self.tamper_post
        return out

    def _log(self, i, b, token, topics, data):
        return {"address": token, "topics": topics, "data": data, "blockNumber": hex(b), "blockTimestamp": hex(ts(b)),
                "blockHash": "0x" + f"{b:064x}", "logIndex": hex(i), "transactionHash": f"0x{i:064x}"}

    def transfers(self, coin, pos, addrs, s, e):
        self.calls.append(("walk", coin, pos, s, e))
        out = []
        for i, (b, c, f, t, val) in enumerate(self.events):
            if c == coin and s <= b <= e and (f if pos == "from" else t) in addrs:
                out.append(self._log(i, b, g5.COINS[coin], [g5.TRANSFER_TOPIC, word(f), word(t)], "0x" + f"{val:064x}"))
        return out

    def destroyed_logs(self, s, e):
        self.calls.append(("destroyed", s, e))
        return [self._log(1000 + i, b, USDT, [g5.DESTROYED_TOPIC], "0x" + "0" * 24 + a[2:] + f"{amt:064x}")
                for i, (b, a, amt) in enumerate(self.destroyed) if s <= b <= e]

    def receipts_sha256(self):
        return "ab" * 32


def d_block(day, k=0):
    """A block inside day (-1 = 8 Aug, 0 = 9 Aug ...)."""
    return PRE + 1 + (day + 1) * 100 + k


EVENTS = [
    (d_block(-1, 5), "USDC", D, A, 700),            # bridge-day inflow to A (bridge only)
    (d_block(0, 3), "USDC", A, B, 200),             # A -> B on day 0
    (d_block(2, 1), "USDC", B, g5.ZERO_ADDRESS, 50),  # burn from B
    (d_block(3, 9), "USDC", g5.ZERO_ADDRESS, C, 40),  # mint to C
    (d_block(34, 99), "USDC", A, C, 10),            # last block of 12 Sep (POST_END)
    (d_block(1, 0), "USDT", D, A, 500),
    (d_block(1, 1), "USDT", A, A, 30),              # self transfer nets zero
]
OPENING = {("USDC", A): 1000, ("USDC", B): 100, ("USDT", C): 80}
DESTROYED = [(d_block(5, 0), A, 150)]


def good_port(**kw):
    return FakePort(OPENING, EVENTS, DESTROYED, **kw)


def sha(b):
    return hashlib.sha256(b).hexdigest()


def _git(repo, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True, capture_output=True)


def commit_all(repo):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "x")


@pytest.fixture(autouse=True)
def repo(tmp_path, monkeypatch):
    """The signing dir is the repo-root prespec/; point REPO_ROOT at a throwaway git repo."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    monkeypatch.setattr(g5, "REPO_ROOT", str(r))
    return r


REAL_INSTALLED_R_VERSION = g5.installed_r_version     # before the autouse fixture patches it
REAL_R_STRING = g5.installed_r_string() if shutil.which("Rscript") else None
FAKE_R_VERSIONS = {"fixest": "0.12.1", "data.table": "1.15.4", "DRDID": "1.4.0", "trust": "0.1.8", "BMisc": "1.4.8", "fastglm": "0.0.3",
                   "Rcpp": "1.0.12", "jsonlite": "1.8.8", "Matrix": "1.6.5", "RcppEigen": "0.3.4.0.2", "sandwich": "3.1.2", "nlme": "3.1.164",
                   "dreamerr": "1.5.0", "numDeriv": "2016.8.1.1", "stringmagic": "1.2.0", "bigmemory": "4.6.6"}
FAKE_R_STRING = "R version 4.3.3 (fake)"


@pytest.fixture(autouse=True)
def installed_r(monkeypatch):
    """The fixtures pin FAKE_R_VERSIONS; the 'installed' versions are injected (one test queries real Rscript)."""
    monkeypatch.setattr(g5, "installed_r_version", lambda p: FAKE_R_VERSIONS.get(p))
    monkeypatch.setattr(g5, "installed_r_string", lambda: FAKE_R_STRING)


def token(cfg_path):
    return g5.verify_signature(open(cfg_path, "rb").read())


def build(tmp_path, mutate=None, eligible=("eligible",), commit=True, code_mutate=None):
    """Write a fully signed fixture; returns (config_path, cfg dict, paths)."""
    d = tmp_path / "pre"
    d.mkdir()
    repo = tmp_path.parent / "repo" if tmp_path.name != "repo" and (tmp_path.parent / "repo").is_dir() else tmp_path / "repo"
    sd = repo / "prespec"
    sd.mkdir(exist_ok=True)
    pid, gid = "main", "gen1"
    frame, wmap = d / "frame.csv", d / "wallet_map.csv"
    rows = [(A, "withdrawal", "eligible"), (B, "control", "eligible"), (C, "control", "eligible"),
            (E, "control", "eligible"), (D, "deposit", "excluded")]
    with open(frame, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["population_id", "generation_id", "address", "source_membership", "screen_status", "screen_reason"])
        for a, m, s in rows:
            w.writerow([pid, gid, a, m, s, ""])
    with open(wmap, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["population_id", "generation_id", "wallet_id", "address"])
        for i, a in enumerate([A, B, C, D, E]):
            w.writerow([pid, gid, 10 + i, a])
    cfg = {"out_dir": str(d / "out"), "pre_end_block": PRE, "bridge_end_block": BRIDGE_END,
           "post_end_block": POST_END, "post_start_date": "2022-08-09", "bridge_date": "2022-08-08",
           "post_end_date": "2022-09-12", "eligible_statuses": list(eligible),
           "populations": [{"population_id": pid, "generation_id": gid, "frame_path": str(frame), "wallet_map_path": str(wmap)}]}
    manifest = {k: cfg[k] for k in g5._MANIFEST_BOUND}
    manifest["populations"] = [[pid, gid]]
    manifest["input_file_sha256"] = {str(frame): sha(frame.read_bytes()), str(wmap): sha(wmap.read_bytes())}
    pin = d / "post_inputs"                                                                     # the R half of the signed package: real files, signed hashes
    pin.mkdir()
    cfg["post_input_paths"] = {}
    manifest["post_input_file_sha256"] = {}
    for k in g5.POST_REQUIRED_KEYS:
        f = pin / k.replace("/", "_")
        f.write_bytes(f"input {k}\n".encode())
        cfg["post_input_paths"][k] = str(f)
        manifest["post_input_file_sha256"][k] = sha(f.read_bytes())
    manifest["r_package_versions"] = dict(FAKE_R_VERSIONS)
    manifest["r_version"] = FAKE_R_STRING
    if mutate:
        mutate(cfg, manifest)
    cfg_path = d / "g5_config.json"
    cfg_bytes = json.dumps(cfg, sort_keys=True).encode()
    cfg_path.write_bytes(cfg_bytes)
    (sd / "prespec.md").write_text("signed text\n")
    (sd / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    (sd / "g5_config.json").write_bytes(cfg_bytes)
    real = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = []
    for rel in g5.ALL_CODE_FILES:                                # the signed code manifest binds the producer code + pinned config
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(os.path.join(real, rel), repo / rel)
        code.append(f"{sha((repo / rel).read_bytes())}  {rel}\n")
    (sd / "code_manifest.txt").write_text("".join(code))
    if code_mutate:
        code_mutate(repo, sd)
    lines = "".join(f"{sha((sd / n).read_bytes())}  {n}\n" for n in ("prespec.md", "manifest.json", "g5_config.json", "code_manifest.txt"))
    (sd / "SHA256").write_text(lines)
    (sd / "signing.json").write_text(json.dumps({
        "signed_by": "Nik", "signed_date": "2026-11-01", "sha256_list_sha256": sha(lines.encode()),
        "files": {"prespec_text": "prespec.md", "numerical_manifest": "manifest.json", "executable_config": "g5_config.json", "code_manifest": "code_manifest.txt"}}))
    if commit:
        commit_all(repo)
    return str(cfg_path), cfg, {"frame": frame, "wmap": wmap, "sd": sd, "cfg_path": cfg_path, "repo": repo}


def rows_of(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


# --- import ------------------------------------------------------------------

def test_import_makes_no_rpc():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = (
        "import sys, socket, urllib.request\n"
        f"sys.path.insert(0, {root!r})\n"
        "def boom(*a, **k): raise SystemExit('network touched at import')\n"
        "socket.socket.connect = boom; socket.getaddrinfo = boom; socket.create_connection = boom\n"
        "urllib.request.urlopen = boom\n"
        "import gates.g5_post as g\n"
        "assert g.N_POST_DAYS == 35\n")
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# --- happy path --------------------------------------------------------------

def test_full_frame_extraction_and_identities(tmp_path):
    cfg_path, cfg, p = build(tmp_path)
    port = good_port()
    path, n, _ = g5.run(cfg_path, port)
    rows = rows_of(path)
    assert n == len(rows) == 4 * 2 * 35                     # A,B,C,E (D ineligible); unmatched E included
    assert {r["address"] for r in rows} == {A, B, C, E}
    assert all(r["date"] != "2022-08-08" for r in rows)     # bridge never emitted
    assert min(r["date"] for r in rows) == "2022-08-09" and max(r["date"] for r in rows) == "2022-09-12"
    assert {(r["population_id"], r["generation_id"]) for r in rows} == {("main", "gen1")}
    assert {r["address"]: r["wallet_id"] for r in rows} == {A: "10", B: "11", C: "12", E: "14"}
    assert {r["address"]: r["arm"] for r in rows} == {A: "withdrawal", B: "control", C: "control", E: "control"}
    bal = {(r["address"], r["coin"], int(r["day_index"])): int(r["balance_units"]) for r in rows}
    # USDC A: opening 1000 + 700 bridge = 1700 on 9 Aug open; day0 -200; day34 -10
    assert bal[A, "USDC", 0] == 1500 and bal[A, "USDC", 33] == 1500 and bal[A, "USDC", 34] == 1490
    assert bal[B, "USDC", 0] == 300 and bal[B, "USDC", 1] == 300 and bal[B, "USDC", 2] == 250   # burn
    assert bal[C, "USDC", 2] == 0 and bal[C, "USDC", 3] == 40 and bal[C, "USDC", 34] == 50       # mint, last block
    assert bal[A, "USDT", 0] == 0 and bal[A, "USDT", 1] == 500 and bal[A, "USDT", 4] == 500       # self-transfer nets 0
    assert bal[A, "USDT", 5] == 350 and bal[A, "USDT", 34] == 350                                 # DestroyedBlackFunds
    assert bal[C, "USDT", 0] == 80 and bal[E, "USDC", 20] == 0
    assert all(r["nonzero"] == str(int(r["balance_units"]) > 0).lower() for r in rows)
    # no Transfer request outside (pre_end, post_end]; no D (ineligible) in any request
    assert all(c[3] > PRE and c[4] <= POST_END for c in port.calls if c[0] == "walk")
    schema = json.load(open(os.path.join(cfg["out_dir"], "post_wallet_coin_day.schema.json")))
    listed = g5.parse_sha256_list((p["sd"] / "SHA256").read_text())
    assert schema["signed_file_sha256"] == {
        "prespec_text": listed["prespec.md"], "numerical_manifest": listed["manifest.json"],
        "executable_config": listed["g5_config.json"], "code_manifest": listed["code_manifest.txt"], "signing_json": sha((p["sd"] / "signing.json").read_bytes())}
    assert [x for x in os.listdir(os.path.dirname(cfg["out_dir"])) if ".tmp-" in x] == []
    assert schema["row_count"] == n and schema["stats"]["destroyed_applied"] == 1
    assert schema["stats"]["mint_burn_legs"] == 2 and schema["stats"]["addresses"] == 4


def test_output_dir_is_immutable(tmp_path):
    cfg_path, cfg, _ = build(tmp_path)
    g5.run(cfg_path, good_port())
    with pytest.raises(g5.InputMismatch, match="immutable"):
        g5.run(cfg_path, good_port())


# --- fail before any RPC -------------------------------------------------------

def _fails_before_rpc(cfg_path, exc):
    port = good_port()
    with pytest.raises(exc):
        g5.run(cfg_path, port)
    assert port.calls == []


def test_no_signature(tmp_path):
    cfg_path, cfg, p = build(tmp_path)
    (p["sd"] / "signing.json").unlink()
    commit_all(p["repo"])
    _fails_before_rpc(cfg_path, g5.SignatureError)


def test_blank_signer_and_unbound_list(tmp_path):
    cfg_path, cfg, p = build(tmp_path)
    rec = json.loads((p["sd"] / "signing.json").read_text())
    (p["sd"] / "signing.json").write_text(json.dumps({**rec, "signed_by": " "}))
    commit_all(p["repo"])
    _fails_before_rpc(cfg_path, g5.SignatureError)
    (p["sd"] / "signing.json").write_text(json.dumps({**rec, "sha256_list_sha256": "0" * 64}))
    commit_all(p["repo"])
    _fails_before_rpc(cfg_path, g5.SignatureError)


@pytest.mark.parametrize("victim", ["prespec.md", "manifest.json", "g5_config.json"])
def test_tampered_signed_file(tmp_path, victim):
    cfg_path, cfg, p = build(tmp_path)
    with open(p["sd"] / victim, "ab") as f:
        f.write(b" ")
    commit_all(p["repo"])
    _fails_before_rpc(cfg_path, g5.SignatureError)


def test_run_config_is_not_the_signed_config(tmp_path):
    cfg_path, cfg, p = build(tmp_path)
    p["cfg_path"].write_text(json.dumps({**cfg, "out_dir": cfg["out_dir"] + "2"}))
    _fails_before_rpc(cfg_path, g5.SignatureError)


def test_config_differs_from_signed_manifest(tmp_path):
    def m(cfg, man):
        man["post_end_block"] = POST_END - 1
    _fails_before_rpc(build(tmp_path, m)[0], g5.InputMismatch)


def test_input_file_hash_mismatch(tmp_path):
    cfg_path, cfg, p = build(tmp_path)
    with open(p["frame"], "a") as f:
        f.write("main,gen1," + D + ",control,eligible,\n")
    _fails_before_rpc(cfg_path, g5.InputMismatch)


def test_input_without_signed_hash(tmp_path):
    def m(cfg, man):
        man["input_file_sha256"].popitem()
    _fails_before_rpc(build(tmp_path, m)[0], g5.InputMismatch)


def test_missing_post_end(tmp_path):
    def m(cfg, man):
        del cfg["post_end_block"]
    _fails_before_rpc(build(tmp_path, m)[0], g5.InputMismatch)


def test_out_of_span_configs(tmp_path):
    for i, mut in enumerate([
        lambda c, m: (c.update(pre_end_block=PRE + 1), m.update(pre_end_block=PRE + 1)),
        lambda c, m: (c.update(bridge_end_block=POST_END), m.update(bridge_end_block=POST_END)),
        lambda c, m: (c.update(post_end_date="2022-09-13"), m.update(post_end_date="2022-09-13")),
        lambda c, m: (c.update(post_start_date="2022-08-08"), m.update(post_start_date="2022-08-08")),
    ]):
        sub = tmp_path / f"c{i}"
        sub.mkdir()
        _fails_before_rpc(build(sub, mut)[0], g5.SpanError)


def test_frame_validation(tmp_path):
    for i, (row, exc) in enumerate([
        (("main", "gen1", "0x" + "A" * 40, "control", "eligible", ""), g5.InputMismatch),   # not lowercase
        (("main", "gen1", A, "control", "eligible", ""), g5.InputMismatch),                  # duplicate
        (("main", "gen1", "0x" + "9" * 40, "control", "eligible", ""), g5.InputMismatch),   # eligible, no map entry
        (("main", "gen2", "0x" + "8" * 40, "control", "eligible", ""), g5.InputMismatch),   # mixed generation
    ]):
        sub = tmp_path / f"f{i}"
        sub.mkdir()

        def m(cfg, man, row=row):
            fp = cfg["populations"][0]["frame_path"]
            with open(fp, "a", newline="") as f:
                csv.writer(f, lineterminator="\n").writerow(row)
            man["input_file_sha256"][fp] = sha(open(fp, "rb").read())
        _fails_before_rpc(build(sub, m)[0], exc)


def test_wallet_map_duplicate_wallet_id(tmp_path):
    def m(cfg, man):
        wp = cfg["populations"][0]["wallet_map_path"]
        with open(wp, "a", newline="") as f:
            csv.writer(f, lineterminator="\n").writerow(["main", "gen1", 10, "0x" + "7" * 40])
        man["input_file_sha256"][wp] = sha(open(wp, "rb").read())
    _fails_before_rpc(build(tmp_path, m)[0], g5.InputMismatch)


# --- guard + identities --------------------------------------------------------

def test_guard_blocks_out_of_span_before_delegating():
    port = good_port()
    g = g5.GuardedPort(port, PRE, BRIDGE_END, POST_END)
    for bad in (lambda: g.transfers("USDC", "from", [A], PRE, POST_END),        # starts at pre_end
                lambda: g.transfers("USDC", "from", [A], PRE + 1, POST_END + 1),
                lambda: g.destroyed_logs(PRE + 1, POST_END + 1),
                lambda: g.balance_of_many("USDC", [A], POST_END + 1),
                lambda: g.block_date(POST_END + 2)):
        with pytest.raises(g5.SpanError):
            bad()
    assert port.calls == []


def test_bridge_and_post_end_must_be_last_block_of_day(tmp_path):
    def m(cfg, man):
        cfg["post_end_block"] = man["post_end_block"] = POST_END - 1
    cfg_path, cfg, _ = build(tmp_path, m)
    port = good_port()
    with pytest.raises(g5.SpanError, match="last block"):
        g5.run(cfg_path, port)
    assert not any(c[0] in ("bal", "walk") for c in port.calls)


@pytest.mark.parametrize("kw,msg", [({"tamper_bridge": 1}, "bridge_end"), ({"tamper_post": 1}, "post_end")])
def test_opening_and_closing_identity_failures(tmp_path, kw, msg):
    cfg_path, cfg, _ = build(tmp_path)
    with pytest.raises(AssertionError, match=msg):
        g5.run(cfg_path, good_port(**kw))


def test_negative_reconstruction_is_an_error_not_clamped(tmp_path):
    cfg_path, cfg, _ = build(tmp_path)
    port = FakePort(OPENING, EVENTS + [(d_block(7, 0), "USDC", B, D, 10_000)], DESTROYED)
    with pytest.raises(AssertionError, match="negative|non-integer"):
        g5.run(cfg_path, port)


def test_log_on_wrong_side_of_bridge_end(tmp_path):
    bounds = (BRIDGE_END, PRE, POST_END)
    port = FakePort({}, [(BRIDGE_END + 1, "USDC", D, A, 1)])   # first block of 9 Aug is fine ...
    logs = port.transfers("USDC", "to", [A], PRE + 1, POST_END)
    assert g5.decode_transfers(logs, "to", {A}, USDC, bounds)[A][0] == {0: 1}
    with pytest.raises(AssertionError, match="disagree"):
        g5.decode_transfers(logs, "to", {A}, USDC, (BRIDGE_END + 5, PRE, POST_END))


def test_decode_rejects_malformed_and_duplicate_logs():
    bounds = (BRIDGE_END, PRE, POST_END)
    port = FakePort({}, [(d_block(0, 1), "USDC", D, A, 1)])
    log = port.transfers("USDC", "to", [A], PRE + 1, POST_END)[0]
    with pytest.raises(AssertionError, match="duplicate"):
        g5.decode_transfers([log, dict(log)], "to", {A}, USDC, bounds)
    with pytest.raises(AssertionError, match="untracked"):
        g5.decode_transfers([log], "to", {B}, USDC, bounds)
    with pytest.raises(AssertionError):
        g5.decode_transfers([{**log, "data": "0x01"}], "to", {A}, USDC, bounds)
    with pytest.raises(g5.SpanError):
        g5.decode_transfers([{**log, "blockNumber": hex(POST_END + 1)}], "to", {A}, USDC, bounds)


def test_sha256_list_parsing():
    h = "a" * 64
    assert g5.parse_sha256_list(f"{h}  x.md\n{h} *y.json\n") == {"x.md": h, "y.json": h}
    for bad in (f"{h}  a/b.md", f"{h}  x\n{h}  x", "zz  x", f"{h} x"[:-2] + "\n"):
        with pytest.raises(g5.SignatureError):
            g5.parse_sha256_list(bad)


# --- fix round 1 ---------------------------------------------------------------

def test_config_named_signing_dir_is_rejected(tmp_path):
    forged = tmp_path / "forged"
    forged.mkdir()

    def m(cfg, man):
        cfg["signing_dir"] = str(forged)
    cfg_path, cfg, _ = build(tmp_path, m)
    _fails_before_rpc(cfg_path, g5.InputMismatch)


def test_untracked_signing_files_rejected(tmp_path):
    cfg_path, cfg, _ = build(tmp_path, commit=False)
    _fails_before_rpc(cfg_path, g5.SignatureError)


def test_dirty_or_staged_signing_files_rejected(tmp_path):
    cfg_path, cfg, p = build(tmp_path)
    (p["sd"] / "signing.json").write_text((p["sd"] / "signing.json").read_text() + " ")
    _fails_before_rpc(cfg_path, g5.SignatureError)       # unstaged edit
    _git(p["repo"], "add", "prespec/signing.json")
    _fails_before_rpc(cfg_path, g5.SignatureError)       # staged, uncommitted


def test_missing_head_commit_rejected(tmp_path):
    cfg_path, cfg, p = build(tmp_path, commit=False)
    _git(p["repo"], "add", "-A")
    _fails_before_rpc(cfg_path, g5.SignatureError)       # tracked in index but no HEAD


def test_roles_must_be_distinct_and_nonempty(tmp_path):
    cfg_path, cfg, p = build(tmp_path)
    rec = json.loads((p["sd"] / "signing.json").read_text())
    rec["files"]["prespec_text"] = "manifest.json"
    (p["sd"] / "signing.json").write_text(json.dumps(rec))
    commit_all(p["repo"])
    _fails_before_rpc(cfg_path, g5.SignatureError)
    rec["files"]["prespec_text"] = "prespec.md"
    (p["sd"] / "signing.json").write_text(json.dumps(rec))
    (p["sd"] / "prespec.md").write_text("\n")
    (p["sd"] / "SHA256").write_text("".join(f"{sha((p['sd'] / n).read_bytes())}  {n}\n" for n in ("prespec.md", "manifest.json", "g5_config.json")))
    rec["sha256_list_sha256"] = sha((p["sd"] / "SHA256").read_bytes())
    (p["sd"] / "signing.json").write_text(json.dumps(rec))
    commit_all(p["repo"])
    _fails_before_rpc(cfg_path, g5.SignatureError)


def test_extract_and_write_need_verification_token(tmp_path):
    cfg_path, cfg, _ = build(tmp_path)
    port = good_port()
    with pytest.raises(g5.SignatureError):
        g5.extract({}, port, None)
    with pytest.raises(g5.SignatureError):
        g5.write_output({}, {}, {}, {}, "x", object())
    with pytest.raises(g5.SignatureError):
        g5.Verified(object(), {}, "", {}, {})
    assert not hasattr(g5.Verified, "_KEY")
    assert port.calls == [] and not os.path.exists(cfg["out_dir"])


def test_token_binds_the_config(tmp_path):
    cfg_path, cfg, _ = build(tmp_path)
    v = token(cfg_path)
    assert v.cfg == cfg and v.config_sha == sha(open(cfg_path, "rb").read())
    v.cfg = {**cfg, "out_dir": cfg["out_dir"] + "-other"}      # caller cannot swap the cfg: extract reads it from the token only
    import inspect
    assert "cfg" not in inspect.signature(g5.extract).parameters
    assert "cfg" not in inspect.signature(g5.write_output).parameters


def test_assume_unchanged_edit_is_rejected(tmp_path):
    cfg_path, cfg, p = build(tmp_path)
    _git(p["repo"], "update-index", "--assume-unchanged", "prespec/signing.json")
    (p["sd"] / "signing.json").write_text((p["sd"] / "signing.json").read_text() + " ")
    assert subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "prespec/signing.json"], cwd=p["repo"]).returncode == 0  # git is fooled
    _fails_before_rpc(cfg_path, g5.SignatureError)


def test_symlinked_signing_file_is_rejected(tmp_path):
    cfg_path, cfg, p = build(tmp_path)
    real = p["sd"] / "signing.json"
    (tmp_path / "sj.json").write_bytes(real.read_bytes())
    real.unlink()
    real.symlink_to(tmp_path / "sj.json")
    commit_all(p["repo"])
    _fails_before_rpc(cfg_path, g5.SignatureError)


def test_git_env_is_scrubbed_and_toplevel_enforced(tmp_path, monkeypatch):
    cfg_path, cfg, p = build(tmp_path)
    (p["sd"] / "signing.json").write_text((p["sd"] / "signing.json").read_text() + " ")   # real repo now dirty
    forged = tmp_path / "forged"
    forged.mkdir()
    _git(forged, "init", "-q")
    shutil.copytree(p["sd"], forged / "prespec")
    commit_all(forged)
    monkeypatch.setenv("GIT_DIR", str(forged / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(forged))
    _fails_before_rpc(cfg_path, g5.SignatureError)       # still judged against the real repo
    monkeypatch.delenv("GIT_DIR")
    monkeypatch.delenv("GIT_WORK_TREE")
    sub = p["repo"] / "nested"
    sub.mkdir()
    monkeypatch.setattr(g5, "REPO_ROOT", str(sub))
    _fails_before_rpc(cfg_path, g5.SignatureError)       # REPO_ROOT inside a repo but not its top level


def test_live_rpc_refuses_receipts_inside_output_dir(tmp_path, monkeypatch):
    out = tmp_path / "o"
    monkeypatch.setattr(g5.g4, "_OUT_DIR", str(out / "receipts"))
    with pytest.raises(RuntimeError, match="inside"):
        g5.LiveRpc(POST_END, str(out))


def test_invalid_json_and_types_fail_closed(tmp_path):
    cfg_path, cfg, p = build(tmp_path)
    p["cfg_path"].write_text("{not json")
    _fails_before_rpc(cfg_path, g5.SignatureError)
    for i, (k, v) in enumerate([("eligible_statuses", "eligible"), ("eligible_statuses", [1]), ("populations", ["x"])]):
        sub = tmp_path / f"t{i}"
        sub.mkdir()
        _fails_before_rpc(build(sub, lambda c, m, k=k, v=v: c.update({k: v}) or m.update({k: v}) if k == "eligible_statuses" else c.update({k: v}))[0], g5.InputMismatch)


def test_malformed_csv_is_input_mismatch(tmp_path):
    def m(cfg, man):
        fp = cfg["populations"][0]["frame_path"]
        open(fp, "w").write("population_id,generation_id\nmain,gen1\n")
        man["input_file_sha256"][fp] = sha(open(fp, "rb").read())
    _fails_before_rpc(build(tmp_path, m)[0], g5.InputMismatch)


def test_destroyed_log_shape_checks():
    bounds = (BRIDGE_END, PRE, POST_END)
    port = FakePort({}, [], [(d_block(0, 1), A, 5)])
    log = port.destroyed_logs(PRE + 1, POST_END)[0]
    assert g5.decode_destroyed([log], {A}, bounds) == [(A, 0, 5)]
    with pytest.raises(AssertionError):
        g5.decode_destroyed([{**log, "topics": log["topics"] + [word(A)]}], {A}, bounds)
    with pytest.raises(AssertionError):
        g5.decode_destroyed([{**log, "data": "0x" + "1" * 24 + log["data"][26:]}], {A}, bounds)


# --- S20: LiveRpc via a fake transport, independent re-verification, checkpoint/resume -------------------------------

class FakeNet:
    """Replaces urllib.request.urlopen (the only transport lib/rpc and g4's batch POST use). Chain = block timestamps,
    a balance table, and per-endpoint log lists. Records every (endpoint, method, params)."""

    def __init__(self, logs_by_ep, balances):
        self.logs_by_ep, self.balances, self.requests = logs_by_ep, balances, []
        self.url_ep = {v["url"]: k for k, v in g5.rpc.ENDPOINTS.items()}

    def _one(self, ep, msg):
        m, p = msg["method"], msg["params"]
        self.requests.append((ep, m, p))
        if m == "eth_chainId":
            r = "0x1"
        elif m == "eth_getBlockByNumber":
            r = {"timestamp": hex(ts(int(p[0], 16)))}
        elif m == "eth_call":
            r = "0x" + f"{self.balances.get(('0x' + p[0]['data'][-40:], int(p[1], 16)), 7):064x}"
        elif m == "eth_getLogs":
            f = p[0]
            lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            r = [l for l in self.logs_by_ep[ep] if lo <= int(l["blockNumber"], 16) <= hi and all(
                t is None or l["topics"][i] in ([t] if isinstance(t, str) else t) for i, t in enumerate(f.get("topics") or []))]
        else:
            raise AssertionError(f"unexpected method {m}")
        return {"jsonrpc": "2.0", "id": msg["id"], "result": r}

    def __call__(self, req, timeout=None):
        ep = self.url_ep[req.full_url]
        body = json.loads(req.data)
        out = [self._one(ep, m) for m in body] if isinstance(body, list) else self._one(ep, body)

        class R:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def read(s):
                return json.dumps(out).encode()
        return R()


def _log(i, b, token, topics, data="0x" + "0" * 63 + "5"):
    return {"address": token, "topics": topics, "data": data, "blockNumber": hex(b), "blockTimestamp": hex(ts(b)),
            "blockHash": "0x" + f"{b:064x}", "logIndex": hex(i), "transactionHash": f"0x{i:064x}"}


@pytest.fixture
def live(tmp_path, monkeypatch):
    rd = tmp_path / "receipts"
    monkeypatch.setattr(g5.g4, "_OUT_DIR", str(rd))
    monkeypatch.setattr(g5.g4, "_RECEIPTS_PATH", str(rd / "receipts.jsonl"))
    monkeypatch.setattr(g5.g4.time, "sleep", lambda s: None)
    tr = [_log(1, d_block(0, 3), USDC, [g5.TRANSFER_TOPIC, word(A), word(B)]),
          _log(2, d_block(20, 4), USDC, [g5.TRANSFER_TOPIC, word(B), word(A)])]
    ds = [_log(3, d_block(5, 0), USDT, [g5.DESTROYED_TOPIC], "0x" + "0" * 24 + A[2:] + f"{9:064x}")]
    net = FakeNet({"tenderly": tr + ds, "mevblocker": list(tr + ds)}, {(A, POST_END): 1234})
    monkeypatch.setattr("urllib.request.urlopen", net)
    return net, rd, tr, ds


def test_live_rpc_query_methods_via_fake_transport(tmp_path, live):
    net, rd, tr, ds = live
    port = g5.LiveRpc(POST_END, str(tmp_path / "out"))
    assert port.block_date(BRIDGE_END) == g5.BRIDGE_DATE
    assert port.block_date(POST_END + 1) > g5.POST_END_DATE
    assert port.balance_of_many("USDC", [A, B], POST_END) == {A: 1234, B: 7}
    got = port.transfers("USDC", "from", [A], PRE + 1, POST_END)
    assert [l["logIndex"] for l in got] == ["0x1"]                       # only the A -> B leg
    assert [l["logIndex"] for l in port.transfers("USDC", "to", [A], PRE + 1, POST_END)] == ["0x2"]
    assert port.destroyed_logs(PRE + 1, POST_END) == [l for l in net.logs_by_ep["mevblocker"] if l["topics"] == [g5.DESTROYED_TOPIC]]
    recs = [json.loads(x) for x in open(rd / "receipts.jsonl")]
    walk = [r for r in recs if r["kind"] == "getLogs"]
    assert {r["unit"].split(":")[0] for r in walk} == {"USDC", "destroyed"}
    assert all(r["gate"] == "g5" and r["sha256"].startswith("v2:") and r["endpoint"] == "mevblocker" for r in walk)   # W1: walk=mevblocker
    assert g5.g4._assert_receipts_within(str(rd / "receipts.jsonl"), POST_END) == len(recs)
    assert port.receipts_sha256() == hashlib.sha256((rd / "receipts.jsonl").read_bytes()).hexdigest()
    for ep, m, p in net.requests:                                       # every walk request is inside (pre_end, post_end]
        if m == "eth_getLogs":
            assert PRE < int(p[0]["fromBlock"], 16) <= int(p[0]["toBlock"], 16) <= POST_END


def test_live_rpc_refuses_post_end_overrun_and_dirty_receipts_dir(tmp_path, live):
    net, rd, *_ = live
    port = g5.LiveRpc(POST_END, str(tmp_path / "out"))
    with pytest.raises(g5.g4.PostPeriodRead):
        port.balance_of_many("USDC", [A], POST_END + 1)
    (rd / "stray").write_text("x")
    with pytest.raises(RuntimeError, match="must be empty"):
        g5.LiveRpc(POST_END, str(tmp_path / "out"))


def test_reverify_compares_to_acquisition_receipts_on_the_other_endpoint(tmp_path, live):
    net, rd, tr, ds = live
    port = g5.LiveRpc(POST_END, str(tmp_path / "out"))
    addrs = [A, B]
    for coin in ("USDC", "USDT"):
        for pos in ("from", "to"):
            port.transfers(coin, pos, addrs, PRE + 1, POST_END)
    port.destroyed_logs(PRE + 1, POST_END)
    net.requests.clear()
    out = rd / "reverify.jsonl"
    summ = g5.reverify(str(rd / "receipts.jsonl"), port.refetch, addrs, PRE, POST_END, str(out))
    assert summ["ranges_compared"] >= 3 and summ["ranges_compared"] == len(out.read_text().splitlines())
    assert {ep for ep, m, p in net.requests if m == "eth_getLogs"} == {"tenderly"}   # W1: acquisition walks on mevblocker, so the verifier is tenderly (the OTHER endpoint, never the acquirer)
    # a divergent second endpoint is caught against the recorded digest, not against a fresh fetch
    net.logs_by_ep["tenderly"] = [l for l in net.logs_by_ep["tenderly"] if l["logIndex"] != "0x2"]   # diverge the verifier
    with pytest.raises(g5.ReverifyError, match="mismatch"):
        g5.reverify(str(rd / "receipts.jsonl"), port.refetch, addrs, PRE, POST_END, str(tmp_path / "rv2.jsonl"), fraction=1.0)


def _rng(unit, s, e, ep="tenderly", coin="USDC", pos="from"):
    r = {"gate": "g5", "kind": "getLogs", "unit": unit, "endpoint": ep, "fromBlock": s, "toBlock": e, "logCount": 1, "sha256": "v2:x"}
    return {**r, "coin": coin, "topics_position": pos} if unit != "destroyed" else r


def _covered(picks, ep, kind, k):
    return any(r["endpoint"] == ep and g5._kind(r) == kind and k in g5._overlapped(r, PRE, POST_END) for r in picks)


def test_sample_covers_every_endpoint_unit_kind_and_span_segment_by_overlap():
    step = (POST_END - PRE) // 12
    ranges = []
    for ep, coin in (("tenderly", "USDC"), ("mevblocker", "USDT")):
        for pos in ("from", "to"):
            for i in range(12):                       # 12 sub-spans tile the span: the sample may not stop at the earliest
                s, e = PRE + 1 + i * step, (PRE + (i + 1) * step if i < 11 else POST_END)
                ranges.append(_rng(f"{coin}:{pos}:{ep}", s, e, ep, coin, pos))
    picks, cover = g5.pick_reverify_ranges(ranges, PRE, POST_END, fraction=0.0)
    for ep, coin in (("tenderly", "USDC"), ("mevblocker", "USDT")):
        for pos in ("from", "to"):
            for k in range(g5.REVERIFY_BUCKETS):
                assert _covered(picks, ep, f"{coin}:{pos}", k), (ep, coin, pos, k)
    assert all(v >= 1 for v in cover.values()) and {r["endpoint"] for r in picks} == {"tenderly", "mevblocker"}
    assert len(g5.pick_reverify_ranges(ranges, PRE, POST_END, fraction=0.9)[0]) >= int(0.9 * len(ranges))


def test_whole_range_walk_is_covered_in_every_segment_by_its_single_range():
    """Tenderly whole-range walk: every range starts in segment 0. The old start-bucket rule 'covered' only segment 0; coverage must be by overlap."""
    whole = [_rng("USDC:from:h", PRE + 1, POST_END)]
    picks, cover = g5.pick_reverify_ranges(whole, PRE, POST_END, fraction=0.0)
    assert len(picks) == 1 and sorted(k.rsplit("/", 1)[1] for k in cover) == [str(k) for k in range(g5.REVERIFY_BUCKETS)]
    # two bisected halves: the second half must be sampled too (the later segments are only overlapped by it)
    halves = [_rng("USDC:from:h", PRE + 1, PRE + 500), _rng("USDC:from:h", PRE + 501, POST_END)]
    picks, _ = g5.pick_reverify_ranges(halves, PRE, POST_END, fraction=0.0)
    assert _covered(picks, "tenderly", "USDC:from", g5.REVERIFY_BUCKETS - 1) and _covered(picks, "tenderly", "USDC:from", 0)


def test_acquisition_receipts_must_tile_the_span(tmp_path):
    p = tmp_path / "r.jsonl"
    rs = [_rng("USDC:from:h", PRE + 1, PRE + 500), _rng("USDC:from:h", PRE + 502, POST_END)]
    p.write_text("".join(json.dumps(r) + "\n" for r in rs))
    with pytest.raises(AssertionError, match="gap"):
        g5.load_acquisition_ranges(str(p), PRE, POST_END)
    p.write_text("")
    with pytest.raises(g5.ReverifyError, match="no getLogs"):
        g5.load_acquisition_ranges(str(p), PRE, POST_END)


class FlakyPort(FakePort):
    """FakePort that dies with a quota error on its Nth walk call."""

    def __init__(self, *a, die_on_walk=None, **k):
        super().__init__(*a, **k)
        self.die_on_walk, self.walks = die_on_walk, 0

    def transfers(self, *a):
        self.walks += 1
        if self.die_on_walk == self.walks:
            raise RuntimeError("HTTP Error 429: quota")
        return super().transfers(*a)


def _queries(port):
    return [c for c in port.calls if c[0] in ("bal", "walk", "destroyed")]


def test_checkpoint_resume_after_quota_error_reuses_finished_queries(tmp_path, monkeypatch):
    cfg_path, cfg, p = build(tmp_path)
    monkeypatch.setattr(g5.rpc, "producer_identity", lambda paths, head=None: "a" * 40 + ":" + "b" * 16)
    ck = str(tmp_path / "work")
    first = FlakyPort(OPENING, EVENTS, DESTROYED, die_on_walk=3)
    with pytest.raises(RuntimeError, match="429"):
        g5.run(cfg_path, first, checkpoint_dir=ck)
    assert not os.path.exists(cfg["out_dir"])                        # nothing published
    done = len(_queries(first))                                      # the failing call raised before FakePort logged it
    second = good_port()
    path, n, _ = g5.run(cfg_path, second, checkpoint_dir=ck)
    assert n == 4 * 2 * 35
    scratch = good_port()
    tmp2 = tmp_path / "scratch"
    tmp2.mkdir()
    cfg2, _, _ = build(tmp2)
    g5.run(cfg2, scratch)
    assert len(_queries(second)) == len(_queries(scratch)) - done   # exactly the finished queries were replayed from disk
    assert os.path.exists(os.path.join(ck, g5.BINDING_NAME))


def test_checkpoint_refuses_other_binding_and_unbound_leftovers(tmp_path):
    b1 = {"producer_identity": "p1", "config_sha256": "c", "input_hashes": {}, "pinned_config": {}}
    d = tmp_path / "w"
    g5.bind_work_dir(str(d), b1)
    g5.bind_work_dir(str(d), b1)                                     # identical binding: a resume
    with pytest.raises(g5.CheckpointError, match="different producer"):
        g5.bind_work_dir(str(d), {**b1, "producer_identity": "p2"})
    with pytest.raises(g5.CheckpointError, match="different producer"):
        g5.bind_work_dir(str(d), {**b1, "input_hashes": {"x": "y"}})
    e = tmp_path / "e"
    e.mkdir()
    (e / "checkpoint.jsonl").write_text("{}\n")
    with pytest.raises(g5.CheckpointError, match="unbound"):
        g5.bind_work_dir(str(e), b1)


def test_run_reverifier_failure_publishes_nothing(tmp_path):
    cfg_path, cfg, _ = build(tmp_path)

    def boom(addrs):
        raise g5.ReverifyError("mismatch")
    with pytest.raises(g5.ReverifyError):
        g5.run(cfg_path, good_port(), reverifier=boom)
    assert not os.path.exists(cfg["out_dir"])
    g5.run(cfg_path, good_port(), reverifier=lambda addrs: {"ranges_compared": 1})
    assert json.load(open(os.path.join(cfg["out_dir"], "post_wallet_coin_day.schema.json")))["stats"]["reverify"] == {"ranges_compared": 1}


# --- S20 fix round 1: signed code manifest (F2), binding (M1), receipts completeness/torn lines (M2), cache vs receipts (M3), LOWs ---

def _resign(p, signed_date="2026-11-02", extra_sha_line=None):
    """Re-sign an already built fixture: optionally add a SHA256 line, rebind signing.json, commit."""
    sd = p["sd"]
    if extra_sha_line:
        with open(sd / "SHA256", "a") as f:
            f.write(extra_sha_line)
    rec = json.loads((sd / "signing.json").read_text())
    rec["sha256_list_sha256"] = sha((sd / "SHA256").read_bytes())
    rec["signed_date"] = signed_date
    (sd / "signing.json").write_text(json.dumps(rec))
    commit_all(p["repo"])


def test_unchecked_extra_sha256_line_is_refused(tmp_path):
    cfg_path, cfg, p = build(tmp_path)
    token(cfg_path)                                                    # control: the fixture verifies
    _resign(p, extra_sha_line=f"{sha(b'x')}  stray.txt\n")
    _fails_before_rpc(cfg_path, g5.SignatureError)
    with pytest.raises(g5.SignatureError, match="unchecked"):
        token(cfg_path)


def test_code_manifest_binds_producer_code(tmp_path):
    cfg_path, cfg, p = build(tmp_path)
    (p["repo"] / "gates" / "g5_post.py").write_text("# changed after signing\n")     # committed, but not what was signed
    commit_all(p["repo"])
    with pytest.raises(g5.SignatureError, match="code file gates/g5_post.py"):
        token(cfg_path)
    _fails_before_rpc(cfg_path, g5.SignatureError)


@pytest.mark.parametrize("victim", ["lib/rpc.py", "gates/g4_controls.py", "config/endpoints.json"])
def test_code_manifest_must_list_every_required_file(tmp_path, victim):
    def drop(repo, sd):
        lines = [l for l in (sd / "code_manifest.txt").read_text().splitlines(True) if not l.endswith(f"  {victim}\n")]
        (sd / "code_manifest.txt").write_text("".join(lines))
    cfg_path, cfg, p = build(tmp_path, code_mutate=drop)
    with pytest.raises(g5.SignatureError, match="does not bind"):
        token(cfg_path)


def test_code_manifest_role_is_mandatory_and_dirty_code_refused(tmp_path):
    cfg_path, cfg, p = build(tmp_path)
    rec = json.loads((p["sd"] / "signing.json").read_text())
    del rec["files"]["code_manifest"]
    (p["sd"] / "signing.json").write_text(json.dumps(rec))
    commit_all(p["repo"])
    with pytest.raises(g5.SignatureError, match="files must be exactly"):
        token(cfg_path)
    sub = tmp_path / "s2"
    sub.mkdir()
    cfg2, _, p2 = build(sub)
    (p2["repo"] / "lib" / "rpc.py").write_text("# dirty, uncommitted\n")
    with pytest.raises(g5.SignatureError, match="HEAD"):
        token(cfg2)


def test_binding_includes_every_signed_hash_walk_endpoint_and_changes_on_resign(tmp_path, monkeypatch):
    cfg_path, cfg, p = build(tmp_path)
    b1 = g5.make_binding(token(cfg_path))
    assert set(b1["signed_file_sha256"]) == {"prespec_text", "numerical_manifest", "executable_config", "code_manifest", "signing_json"}
    assert b1["walk_endpoint"] == g5.g4.WALK and b1["batch_endpoint"] == g5.g4.BATCH
    _resign(p, signed_date="2026-11-09")                               # same role files, re-signed
    assert g5.make_binding(token(cfg_path)) != b1
    monkeypatch.setattr(g5.g4, "WALK", "mevblocker")
    assert g5.make_binding(token(cfg_path))["walk_endpoint"] == "mevblocker"


def test_receipts_must_cover_every_expected_unit(tmp_path):
    addrs = [A, B]
    exp = g5.expected_units(addrs)
    assert exp == {"destroyed"} | {g5.walk_unit(c, p, addrs) for c in ("USDC", "USDT") for p in ("from", "to")}
    path = tmp_path / "r.jsonl"
    units = sorted(exp - {g5.walk_unit("USDT", "to", addrs)})          # one whole unit has NO receipt at all
    path.write_text("".join(json.dumps(_rng(u, PRE + 1, POST_END) | ({} if u == "destroyed" else {"coin": u.split(":")[0], "topics_position": u.split(":")[1]})) + "\n" for u in units))
    with pytest.raises(g5.ReverifyError, match="missing"):
        g5.load_acquisition_ranges(str(path), PRE, POST_END, exp)
    assert len(g5.load_acquisition_ranges(str(path), PRE, POST_END, set(units))) == len(units)


def test_torn_receipt_line_is_refused_for_inspection_and_repaired_on_resume(tmp_path):
    path = tmp_path / "receipts.jsonl"
    good = json.dumps(_rng("destroyed", PRE + 1, POST_END)) + "\n"
    path.write_text(good + '{"gate": "g5", "kind": "getLo')
    with pytest.raises(g5.ReverifyError, match="torn"):
        g5.read_getlogs_receipts(str(path))
    g5.repair_torn_receipts(str(path))
    assert path.read_text() == good and len(g5.read_getlogs_receipts(str(path))) == 1
    path.write_text(good + "not json\n" + good)
    with pytest.raises(g5.ReverifyError, match="corrupt"):
        g5.read_getlogs_receipts(str(path))


def test_resume_repairs_a_torn_receipts_tail_in_live_rpc(tmp_path, live):
    net, rd, *_ = live
    b = {"producer_identity": "p", "config_sha256": "c", "signed_file_sha256": {}, "input_hashes": {}, "pinned_config": {}}
    port = g5.LiveRpc(POST_END, str(tmp_path / "out"), binding=b)
    port.destroyed_logs(PRE + 1, POST_END)
    with open(rd / "receipts.jsonl", "a") as f:
        f.write('{"gate": "g5", "kind": "getLogs", "unit": "USDC:fr')
    g5.LiveRpc(POST_END, str(tmp_path / "out"), binding=b)             # resume
    assert (rd / "receipts.jsonl").read_text().endswith("\n") and len(g5.read_getlogs_receipts(str(rd / "receipts.jsonl"))) >= 1


def test_cached_logs_are_checked_against_the_acquisition_receipts(tmp_path, live):
    net, rd, tr, ds = live
    b = {"producer_identity": "p", "config_sha256": "c", "signed_file_sha256": {}, "input_hashes": {}, "pinned_config": {}}
    rcp = str(rd / "receipts.jsonl")
    cp = g5.CheckpointedPort(g5.LiveRpc(POST_END, str(tmp_path / "out"), binding=b), str(rd), b, receipts_path=rcp)
    first = cp.transfers("USDC", "from", [A, B], PRE + 1, POST_END)
    cp.close()

    class Dead:
        def transfers(self, *a):
            raise AssertionError("network used")
    ok = g5.CheckpointedPort(Dead(), str(rd), b, receipts_path=rcp)
    assert ok.transfers("USDC", "from", [A, B], PRE + 1, POST_END) == first        # intact cache hit, no fetch
    ok.close()
    lines = [json.loads(x) for x in open(rd / "checkpoint.jsonl")]
    for rec in lines:
        if rec["k"].startswith("tr:USDC:from"):
            rec["v"] = rec["v"][:-1]                                                # lose a log in the cache
    with open(rd / "checkpoint.jsonl", "w") as f:
        f.write("".join(json.dumps(r) + "\n" for r in lines))
    bad = g5.CheckpointedPort(Dead(), str(rd), b, receipts_path=rcp)
    with pytest.raises(g5.CheckpointCorrupt, match="disagrees"):
        bad.transfers("USDC", "from", [A, B], PRE + 1, POST_END)


def test_bind_work_dir_rejects_unknown_names_and_symlinked_work_files(tmp_path):
    b = {"producer_identity": "p"}
    d = tmp_path / "w"
    g5.bind_work_dir(str(d), b)
    (d / "surprise.txt").write_text("x")
    with pytest.raises(g5.CheckpointError, match="unknown file"):
        g5.bind_work_dir(str(d), b)
    (d / "surprise.txt").unlink()
    (tmp_path / "elsewhere").write_text("{}\n")
    os.symlink(tmp_path / "elsewhere", d / "checkpoint.jsonl")
    with pytest.raises(g5.CheckpointError, match="not a regular"):
        g5.bind_work_dir(str(d), b)


def test_live_run_refuses_a_caller_supplied_reverifier_before_reading_anything(tmp_path):
    cfg_path, cfg, p = build(tmp_path)
    with pytest.raises(g5.PreAccessDenied, match="re-verification"):
        g5.run(cfg_path, None, reverifier=lambda a: {})


def test_pre_access_flag_marks_only_pre_rpc_failures(tmp_path):
    cfg_path, cfg, p = build(tmp_path)
    (p["sd"] / "signing.json").unlink()
    commit_all(p["repo"])
    with pytest.raises(g5.SignatureError) as e:
        g5.run(cfg_path, good_port())
    assert e.value.pre_access is True
    sub = tmp_path / "ok"
    sub.mkdir()
    cfg2, _, _ = build(sub)
    with pytest.raises(AssertionError) as e2:
        g5.run(cfg2, good_port(tamper_post=5))
    assert not getattr(e2.value, "pre_access", False)


# --- S20 fix round 2 ---------------------------------------------------------------------------------------------------

def test_resume_with_several_logs_in_one_block_does_not_crash(tmp_path, live):
    """sec MED: the cache/receipt comparison sorted (block, dict) tuples -> TypeError when two logs shared a block."""
    net, rd, tr, ds = live
    extra = _log(7, d_block(0, 3), USDC, [g5.TRANSFER_TOPIC, word(A), word(C)])          # same block as tr[0]
    for ep in net.logs_by_ep:
        net.logs_by_ep[ep].append(dict(extra))
    b = {"producer_identity": "p", "config_sha256": "c", "signed_file_sha256": {}, "input_hashes": {}, "pinned_config": {}}
    rcp = str(rd / "receipts.jsonl")
    cp = g5.CheckpointedPort(g5.LiveRpc(POST_END, str(tmp_path / "out"), binding=b), str(rd), b, receipts_path=rcp)
    first = cp.transfers("USDC", "from", [A, B], PRE + 1, POST_END)
    cp.close()
    assert sum(1 for l in first if l["blockNumber"] == hex(d_block(0, 3))) == 2

    class Dead:
        def transfers(self, *a):
            raise AssertionError("network used")
    again = g5.CheckpointedPort(Dead(), str(rd), b, receipts_path=rcp)
    assert again.transfers("USDC", "from", [A, B], PRE + 1, POST_END) == first          # resume = intact cache hit
    again.close()
    assert g5._log_order((5, {"logIndex": "0x2", "transactionHash": "0x1"})) < g5._log_order((5, {"logIndex": "0x10", "transactionHash": "0x0"}))


def test_missing_receipts_file_trusts_nothing(tmp_path):
    """sec LOW: receipts path given but no file -> {} (cache entries are refetched), not None (= trust the cache)."""
    d = tmp_path / "w"
    d.mkdir()
    b = {"producer_identity": "p"}
    cp = g5.CheckpointedPort(object(), str(d), b, receipts_path=str(d / "receipts.jsonl"))
    assert cp._acq == {}
    assert cp._logs_match_receipts("destroyed", PRE + 1, POST_END, []) is False
    cp.close()
    assert g5.CheckpointedPort(object(), str(tmp_path / "w2"), b)._acq is None           # no receipts in play (fixture ports)


def _ra(**mut):
    return lambda cfg, manifest: manifest.update(mut)


def _drop_key(k):
    return lambda cfg, manifest: manifest["post_input_file_sha256"].pop(k)


@pytest.mark.parametrize("mutate,match", [
    (_ra(post_input_file_sha256={**{k: "ab" * 32 for k in g5.POST_REQUIRED_KEYS}, "later_freeze": "cd" * 32}), "refused"),
    (_ra(post_input_file_sha256={**{k: "ab" * 32 for k in g5.POST_REQUIRED_KEYS}, "outflow_evidence": "cd" * 32}), "refused"),
    (_ra(post_input_file_sha256={**{k: "ab" * 32 for k in g5.POST_REQUIRED_KEYS}, "post_panel": "cd" * 32}), "unknown"),
    (_drop_key("match_membership"), "lacks required"),
    (_ra(post_input_file_sha256={**{k: "ab" * 32 for k in g5.POST_REQUIRED_KEYS}, "g0_circle_events": "cd" * 32, "g0_usdt_events": "cd" * 32}), "all of .* or none"),   # S59: g0 receipts hash omitted
    (_ra(post_input_file_sha256="nope"), "no valid post_input_file_sha256"),
    (_ra(post_input_file_sha256={**{k: "ab" * 32 for k in g5.POST_REQUIRED_KEYS}, "usdc_pool_flag": "xyz"}), "no valid post_input_file_sha256"),
    (_ra(r_package_versions={"fixest": "0.12.1"}), "r_package_versions"),
    (_ra(r_package_versions=None), "r_package_versions"),
    (_ra(r_package_versions={k: v for k, v in FAKE_R_VERSIONS.items() if k != "DRDID"}), "DRDID"),     # S24 F4: DRDID + imports are pinned
    (_ra(r_package_versions={k: v for k, v in FAKE_R_VERSIONS.items() if k != "jsonlite"}), "jsonlite"),
    (_ra(r_version=None), "r_version"),
    (_ra(r_version="R version 4.2.0 (other)"), "differs from the signed r_version"),
])
def test_inconsistent_r_half_fails_before_any_access(tmp_path, mutate, match):
    """T-F1: g5 is the first post-data access, so the R-half of the signed package is checked here, before any RPC."""
    cfg_path, cfg, p = build(tmp_path, mutate=mutate)
    port = good_port()
    with pytest.raises(g5.SignatureError, match=match) as e:
        g5.run(cfg_path, port)
    assert e.value.pre_access is True and port.calls == []
    assert not os.path.exists(cfg["out_dir"])


def test_r_code_files_are_bound_before_access(tmp_path):
    for victim in ("R/post.R", "R/post_gate.R", "run_all.R", "run_all.py", "gates/g1b_frame.py"):
        sub = tmp_path / victim.replace("/", "_")
        sub.mkdir()

        def drop(repo, sd, victim=victim):
            ls = [l for l in (sd / "code_manifest.txt").read_text().splitlines(True) if not l.endswith(f"  {victim}\n")]
            (sd / "code_manifest.txt").write_text("".join(ls))
        cfg_path, cfg, p = build(sub, code_mutate=drop)
        port = good_port()
        with pytest.raises(g5.SignatureError, match="does not bind") as e:
            g5.run(cfg_path, port)
        assert victim in str(e.value) and port.calls == []


def test_code_lists_include_run_all_and_every_imported_gate_and_mirror_the_r_gate():
    for f in ("run_all.py", "gates/g1b_frame.py"):
        assert f in g5.CODE_FILES and f in g5.POST_R_CODE_FILES
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    gates = {f"gates/{n}" for n in os.listdir(os.path.join(root, "gates")) if n.endswith(".py")}
    assert gates <= set(g5.CODE_FILES)
    assert {f"lib/{n}" for n in os.listdir(os.path.join(root, "lib")) if n.endswith(".py")} <= set(g5.CODE_FILES)
    if shutil.which("Rscript"):
        out = subprocess.run(["Rscript", "-e", 'source("R/post_gate.R"); cat(POST_R_CODE_FILES, sep="\\n"); cat("--\\n"); '
                              'cat(POST_REQUIRED_KEYS, "|", POST_OPTIONAL_KEYS, "|", POST_REFUSED_KEYS, "|", POST_PINNED_PACKAGES, sep="\\n")'],
                             cwd=root, capture_output=True, text=True, check=True).stdout
        files, rest = out.split("--\n")
        assert set(files.split()) == set(g5.POST_R_CODE_FILES)
        req, opt, ref, pk = (x.split() for x in rest.split("|\n"))
        assert (tuple(req), tuple(opt), tuple(ref), tuple(pk)) == (g5.POST_REQUIRED_KEYS, g5.POST_OPTIONAL_KEYS, g5.POST_REFUSED_KEYS, g5.POST_PINNED_PACKAGES)


def test_any_verification_phase_exception_is_pre_access(tmp_path, monkeypatch):
    """sec LOW: a non-Signature/Input/Span exception while verifying (OSError...) must exit 3 (pre-access), not 4."""
    cfg_path, cfg, p = build(tmp_path)

    def boom():
        raise OSError("blocks.json unreadable")
    monkeypatch.setattr(g5.blocks, "load_pinned", boom)
    port = good_port()
    with pytest.raises(g5.PreAccessDenied, match="OSError") as e:
        g5.run(cfg_path, port)
    assert port.calls == []
    import run_all
    orig = g5.run
    monkeypatch.setattr(run_all.g5_post, "run", lambda c: orig(c, port))
    assert run_all.run_g5(cfg_path) == 3


def test_checkpoint_setup_failure_is_pre_access_and_makes_no_rpc(tmp_path, live, monkeypatch):
    """sec LOW: CheckpointedPort is built before the chain-id RPC, so its failure is truthfully 'no RPC' (exit 3)."""
    net, rd, tr, ds = live
    cfg_path, cfg, p = build(tmp_path)
    monkeypatch.setattr(g5.rpc, "producer_identity", lambda paths, head=None: "a" * 40 + ":" + "b" * 16)
    binding = g5.make_binding(token(cfg_path))
    g5.bind_work_dir(str(rd), binding)
    (rd / "receipts.jsonl").write_text(json.dumps({"gate": "g5", "kind": "getLogs", "unit": "x"}) + "\n")      # complete line, but not a valid getLogs receipt
    with pytest.raises(g5.PreAccessDenied, match="before any RPC"):
        g5.run(cfg_path, None)
    assert net.requests == []


# --- S20 fix round 3 -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("pin,ok", [("0.12.1", True), ("0.12.2", False), ("0.12", False), ("0.12.1 ", False), (" 0.12.1", False), ("v0.12.1", False),
                                    ("", False), ("0.12.1.", False), ("0.12.1\n", False), (12, False), (None, False), (["0.12.1"], False)])
def test_signed_r_package_pin_must_equal_the_installed_version(tmp_path, pin, ok):
    """SEV2: R denies unless installed == signed (post_gate.R verify_post_package_versions); g5 must deny the same package pre-access."""
    cfg_path, cfg, p = build(tmp_path, mutate=lambda c, m: m.update(r_package_versions={**FAKE_R_VERSIONS, "fixest": pin}))
    if ok:
        token(cfg_path)
        return
    port = good_port()
    with pytest.raises(g5.SignatureError, match="R-half") as e:
        g5.run(cfg_path, port)
    assert e.value.pre_access is True and port.calls == []


def test_installed_package_missing_or_other_version_is_denied(tmp_path, monkeypatch):
    cfg_path, cfg, p = build(tmp_path)
    monkeypatch.setattr(g5, "installed_r_version", lambda pkg: None)
    with pytest.raises(g5.SignatureError, match="not installed"):
        token(cfg_path)
    monkeypatch.setattr(g5, "installed_r_version", lambda pkg: "9.9.9" if pkg == "data.table" else FAKE_R_VERSIONS[pkg])
    with pytest.raises(g5.SignatureError, match="differs from the signed version"):
        token(cfg_path)


@pytest.mark.skipif(not shutil.which("Rscript"), reason="needs Rscript")
def test_real_rscript_version_query_matches_package_version():
    got = REAL_INSTALLED_R_VERSION("data.table")
    if got is None:
        pytest.skip("data.table not installed in R")
    want = subprocess.run(["Rscript", "-e", 'cat(as.character(packageVersion("data.table")))'], capture_output=True, text=True).stdout
    assert got == want
    assert REAL_INSTALLED_R_VERSION("noSuchPackageXyz") is None
    assert REAL_INSTALLED_R_VERSION('x"); system("true")') is None


def _put(path, v):
    path.write_bytes(v if isinstance(v, bytes) else v.encode())


def _rewrite(p, signing=None, manifest=None, shalist=None, code=None):
    """Re-sign a built fixture with RAW control: manifest text/bytes, signing.json text/bytes (signing: rec dict -> raw), the SHA256 list
    (shalist: lines text -> raw) and the code manifest (code: text -> raw)."""
    sd = p["sd"]
    if manifest is not None:
        _put(sd / "manifest.json", manifest)
    if code is not None:
        _put(sd / "code_manifest.txt", code((sd / "code_manifest.txt").read_text()))
    lines = "".join(f"{sha((sd / n).read_bytes())}  {n}\n" for n in ("prespec.md", "manifest.json", "g5_config.json", "code_manifest.txt"))
    raw_lines = lines if shalist is None else shalist(lines)
    _put(sd / "SHA256", raw_lines)
    rec = json.loads((sd / "signing.json").read_text())
    rec["sha256_list_sha256"] = sha(raw_lines if isinstance(raw_lines, bytes) else raw_lines.encode())
    _put(sd / "signing.json", json.dumps(rec) if signing is None else signing(rec))
    commit_all(p["repo"])


def _with(**kv):
    return lambda rec: json.dumps({**rec, **kv})


def _dup(key, val):
    return lambda rec: json.dumps(rec)[:-1] + f", {json.dumps(key)}: {json.dumps(val)}" + "}"


def _without(key):
    return lambda rec: json.dumps({k: v for k, v in rec.items() if k != key})


def _renamed(old, new):
    return lambda rec: json.dumps({(new if k == old else k): v for k, v in rec.items()})


# (name, signing-text fn or None, manifest edit or None, expected accept?)
_PARITY = [
    ("valid", None, None, True),
    ("date_ok_leap", _with(signed_date="2024-02-29"), None, True),
    ("date_bad_month", _with(signed_date="2026-13-01"), None, False),
    ("date_not_calendar", _with(signed_date="2026-02-30"), None, False),
    ("date_trailing_newline", _with(signed_date="2026-11-01\n"), None, False),
    ("date_leading_space", _with(signed_date=" 2026-11-01"), None, False),
    ("date_compact", _with(signed_date="20261101"), None, False),
    ("date_short_fields", _with(signed_date="2026-1-01"), None, False),
    ("date_datetime", _with(signed_date="2026-11-01T00:00:00"), None, False),
    ("date_int", _with(signed_date=20261101), None, False),
    ("date_list", _with(signed_date=["2026-11-01"]), None, False),
    ("date_null", _with(signed_date=None), None, False),
    ("date_missing", _without("signed_date"), None, False),
    ("by_empty", _with(signed_by=""), None, False),
    ("by_blank", _with(signed_by="  \t "), None, False),
    ("by_nbsp_is_not_blank", _with(signed_by="\u00a0"), None, True),
    ("by_list_of_one", _with(signed_by=["Nik"]), None, False),
    ("by_two_element_list", _with(signed_by=["Nik", "Mallory"]), None, False),
    ("by_int", _with(signed_by=5), None, False),
    ("by_null", _with(signed_by=None), None, False),
    ("by_missing", _without("signed_by"), None, False),
    ("by_only_partial_key", _renamed("signed_by", "signed_by_name"), None, False),
    ("dup_signed_by", _dup("signed_by", "Mallory"), None, False),
    ("dup_signed_date", _dup("signed_date", "2026-11-01"), None, False),
    ("dup_files_inner", lambda rec: json.dumps(rec).replace('"prespec_text": "prespec.md"', '"prespec_text": "prespec.md", "prespec_text": "prespec.md"'), None, False),
    ("dup_manifest_top", None, lambda t: t[:-1] + ', "pre_end_block": 1}', False),
    ("dup_manifest_nested", None, lambda t: t.replace('"fixest": "{fx}"', '"fixest": "{fx}", "fixest": "{fx}"'), False),
    ("pin_mismatch", None, lambda t: t.replace('"fixest": "{fx}"', '"fixest": "0.0.1"'), False),
    ("pin_padded", None, lambda t: t.replace('"fixest": "{fx}"', '"fixest": "{fx} "'), False),
    ("pin_not_string", None, lambda t: t.replace('"fixest": "{fx}"', '"fixest": 1'), False),
]


def _bind_nl(rec):
    return json.dumps({**rec, "sha256_list_sha256": rec["sha256_list_sha256"] + "\n"})


def _man_extra(token):
    return lambda t: t[:-1] + f', "extra": {token}' + "}"


def _sign_extra(token):
    return lambda rec: json.dumps(rec)[:-1] + f', "extra": {token}' + "}"


# Round 4 grammar cases (F1/F3/F4): (name, signing fn, manifest fn, accept?, {shalist/code/manifest-bytes fn})
_PARITY += [
    ("nan_in_signing", _sign_extra("NaN"), None, False),
    ("infinity_in_signing", _sign_extra("Infinity"), None, False),
    ("neg_infinity_in_signing", _sign_extra("-Infinity"), None, False),
    ("nan_in_manifest", None, _man_extra("NaN"), False),
    ("infinity_in_manifest", None, _man_extra("Infinity"), False),
    ("neg_infinity_in_manifest", None, _man_extra("-Infinity"), False),
    ("utf16_signing_json", lambda rec: json.dumps(rec).encode("utf-16"), None, False),
    ("utf16le_nobom_signing_json", lambda rec: json.dumps(rec).encode("utf-16-le"), None, False),
    ("utf16_manifest", None, lambda t: t.encode("utf-16"), False),
    ("utf8_bom_signing_json", lambda rec: b"\xef\xbb\xbf" + json.dumps(rec).encode(), None, False),
    ("utf8_bom_manifest", None, lambda t: b"\xef\xbb\xbf" + t.encode(), False),
    ("sha_binding_trailing_newline", _bind_nl, None, False),
    ("list_crlf_endings_ok", None, None, True, {"shalist": lambda t: t.replace("\n", "\r\n")}),
    ("list_lone_cr_endings_ok", None, None, True, {"shalist": lambda t: t.replace("\n", "\r")}),
    ("list_blank_ascii_ws_line_ok", None, None, True, {"shalist": lambda t: " \t \n" + t + "\n  \n"}),
    ("list_nbsp_line", None, None, False, {"shalist": lambda t: t + "\u00a0\n"}),
    ("list_em_space_line", None, None, False, {"shalist": lambda t: t + "\u2003\n"}),
    ("list_line_separator_u2028", None, None, False, {"shalist": lambda t: t + "\u2028\n"}),
    ("list_vertical_tab_line", None, None, False, {"shalist": lambda t: t + "\x0b\n"}),
    ("list_form_feed_line", None, None, False, {"shalist": lambda t: t + "\x0c\n"}),
    ("list_invalid_utf8", None, None, False, {"shalist": lambda t: t.encode() + b"\xff\xfe\n"}),
    ("code_nbsp_line", None, None, False, {"code": lambda t: t + "\u00a0\n"}),
    ("code_u2028_line", None, None, False, {"code": lambda t: t + "\u2028\n"}),
    ("code_blank_ascii_ws_line_ok", None, None, True, {"code": lambda t: t + " \t\n\n"}),
    ("code_path_starts_with_nbsp", None, None, False, {"code": lambda t: t + "ab" * 32 + "  \u00a0x\n"}),
]


@pytest.mark.skipif(not shutil.which("Rscript"), reason="needs Rscript")
def test_python_and_r_accept_and_deny_the_same_signing_packages(tmp_path):
    """SEV3 parity: ONE fixture set through g5.verify_signature AND R verify_post_signature; every case must get the same verdict."""
    if any(REAL_INSTALLED_R_VERSION(k) is None for k in g5.POST_PINNED_PACKAGES):
        pytest.skip("fixest/data.table not installed in R")
    real = {k: REAL_INSTALLED_R_VERSION(k) for k in g5.POST_PINNED_PACKAGES}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    verdicts, snaps = {}, {}
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(g5, "installed_r_version", lambda pkg: real.get(pkg))
        mp.setattr(g5, "installed_r_string", lambda: REAL_R_STRING)
        for i, (name, signing, medit, _ok, *extra) in enumerate(_PARITY):
            extra = extra[0] if extra else {}
            sub = tmp_path / f"c{i}"
            sub.mkdir()
            cfg_path, cfg, p = build(sub, mutate=lambda c, m: m.update(r_package_versions=dict(real), r_version=REAL_R_STRING))
            if signing is not None or medit is not None or extra:
                mtext = None
                if medit is not None:
                    mtext = medit((p["sd"] / "manifest.json").read_text().replace(f'"fixest": "{real["fixest"]}"', '"fixest": "{fx}"'))
                    mtext = mtext.replace("{fx}", real["fixest"]) if isinstance(mtext, str) else mtext.replace(b"{fx}", real["fixest"].encode())
                _rewrite(p, signing=signing, manifest=mtext, **extra)
            try:
                token(cfg_path)
                verdicts[name] = True
            except (g5.SignatureError, g5.PreAccessDenied):     # ONLY a deny verdict counts; any other exception is a test failure
                verdicts[name] = False
            snaps[name] = str(tmp_path / f"snap{i}")
            shutil.copytree(p["repo"], snaps[name])
    finally:
        mp.undo()
    script = ('for (f in c("utils", "decide", "cem", "estimate", "validate", "synth", "pre", "blacklist", "presign", "post_gate")) source(file.path("R", paste0(f, ".R")))\n'
              'args <- commandArgs(TRUE)\n'
              'for (a in args) { nm <- sub("=.*$", "", a); d <- sub("^[^=]*=", "", a)\n'
              '  r <- tryCatch({ verify_post_signature(d, POST_R_CODE_FILES); "ACCEPT" }, error = function(e) "DENY")\n'
              '  cat(nm, r, "\\n") }\n')
    out = subprocess.run(["Rscript", "-e", script, *[f"{n}={d}" for n, d in snaps.items()]], cwd=root, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-2000:]
    rverd = {l.split()[0]: l.split()[1] == "ACCEPT" for l in out.stdout.splitlines() if l.strip().endswith(("ACCEPT", "DENY"))}
    assert set(rverd) == set(verdicts)
    assert verdicts == rverd, {k: (verdicts[k], rverd[k]) for k in verdicts if verdicts[k] != rverd[k]}
    want = {t[0]: t[3] for t in _PARITY}
    assert verdicts == want, {n: (verdicts[n], ok) for n, ok in want.items() if verdicts[n] != ok}



class AddressFilteringNet(FakeNet):
    """FakeNet whose eth_getLogs also honours the address filter, so a USDT walk does not return USDC logs."""

    def _one(self, ep, msg):
        out = super()._one(ep, msg)
        if msg["method"] == "eth_getLogs":
            a = msg["params"][0].get("address")
            if a:
                a = {x.lower() for x in ([a] if isinstance(a, str) else a)}
                out["result"] = [l for l in out["result"] if l["address"].lower() in a]
        return out


@pytest.fixture
def full_live(tmp_path, live, monkeypatch):
    """A complete live g5 run is possible on the fake transport: balances are flat (7), the two USDC legs cancel, no destroyed log."""
    net, rd, tr, ds = live
    n2 = AddressFilteringNet({"tenderly": list(tr), "mevblocker": list(tr)}, {})
    monkeypatch.setattr("urllib.request.urlopen", n2)
    return n2, rd


def test_full_live_run_attests_walk_and_replay_endpoints_in_the_schema(tmp_path, full_live):
    net, rd = full_live
    cfg_path, cfg, p = build(tmp_path)
    path, n, _ = g5.run(cfg_path, None)
    schema = json.load(open(os.path.join(cfg["out_dir"], "post_wallet_coin_day.schema.json")))
    assert (schema["walk_endpoint"], schema["replay_endpoint"], schema["batch_endpoint"]) == (g5.g4.WALK, g5.g4.REPLAY_ENDPOINT[g5.g4.WALK], g5.g4.BATCH)
    assert (schema["walk_endpoint"], schema["replay_endpoint"]) == ("mevblocker", "tenderly")
    assert {ep for ep, m, _ in net.requests if m == "eth_chainId"} == {"mevblocker", "tenderly"}     # item 4: every endpoint incl. the verifier
    g5.g4_june.acquire_run_lock(str(rd)).close()          # the run lock is released when the run ends (item 3)


def test_endpoint_attestation_refuses_a_mixed_or_wrong_endpoint_record(tmp_path):
    rcp, rv = tmp_path / "receipts.jsonl", tmp_path / "reverify.jsonl"
    walk, rep = g5.g4.WALK, g5.g4.REPLAY_ENDPOINT[g5.g4.WALK]
    bal = lambda ep: json.dumps({"gate": "g4", "kind": "balanceOf", "endpoint": ep, "coin": "USDC", "fromBlock": POST_END, "toBlock": POST_END, "addressCount": 1}) + "\n"
    rcp.write_text(json.dumps(_rng("destroyed", PRE + 1, POST_END, ep=walk)) + "\n" + bal("https://batch.example/rpc"))
    rv.write_text(json.dumps({"source": walk, "endpoint": rep}) + "\n")
    # batch_endpoint is what the balanceOf receipts RECORD, not the constant
    assert g5.endpoint_attestation(str(rcp), str(rv)) == {"walk_endpoint": walk, "replay_endpoint": rep, "batch_endpoint": "https://batch.example/rpc"}
    rcp.write_text(json.dumps(_rng("destroyed", PRE + 1, POST_END, ep=walk)) + "\n" + bal("https://a.example") + bal("https://b.example"))
    with pytest.raises(g5.ReverifyError, match="exactly one batch endpoint"):
        g5.endpoint_attestation(str(rcp), str(rv))
    rcp.write_text(json.dumps(_rng("destroyed", PRE + 1, POST_END, ep=walk)) + "\n")
    with pytest.raises(g5.ReverifyError, match="exactly one batch endpoint"):
        g5.endpoint_attestation(str(rcp), str(rv))
    rcp.write_text(json.dumps(_rng("destroyed", PRE + 1, POST_END, ep=walk)) + "\n" + bal(g5.g4.BATCH))
    rcp.write_text(json.dumps(_rng("destroyed", PRE + 1, PRE + 5, ep=walk)) + "\n" + json.dumps(_rng("destroyed", PRE + 6, POST_END, ep=rep)) + "\n")
    with pytest.raises(g5.ReverifyError, match="single walk endpoint"):
        g5.endpoint_attestation(str(rcp), str(rv))
    rcp.write_text(json.dumps(_rng("destroyed", PRE + 1, POST_END, ep=walk)) + "\n")
    rv.write_text(json.dumps({"source": walk, "endpoint": walk}) + "\n")
    with pytest.raises(g5.ReverifyError, match="reverify records"):
        g5.endpoint_attestation(str(rcp), str(rv))
    rv.write_text("")
    with pytest.raises(g5.ReverifyError, match="reverify records"):
        g5.endpoint_attestation(str(rcp), str(rv))


@pytest.mark.parametrize("var", ["G4_WALK_ENDPOINT", "G4_AUDIT_MAX", "G4_SAMPLE_CONTROLS"])
def test_live_g5_refuses_g4_environment_overrides_before_any_rpc(tmp_path, live, monkeypatch, var):
    net, rd, *_ = live
    cfg_path, cfg, p = build(tmp_path)
    monkeypatch.setenv(var, "tenderly")
    with pytest.raises(g5.PreAccessDenied, match=var):
        g5.run(cfg_path, None)
    assert net.requests == [] and not rd.exists()
    monkeypatch.setenv("G4_OUT_DIR", str(rd))      # g5's own required work-dir variable is not an override
    monkeypatch.delenv(var)
    assert [k for k in g5.g4_june._stale_g4_env() if k != "G4_OUT_DIR"] == []


def test_second_run_on_the_same_work_dir_is_refused_by_the_run_lock(tmp_path, live):
    net, rd, *_ = live
    cfg_path, cfg, p = build(tmp_path)
    rd.mkdir()
    held = g5.g4_june.acquire_run_lock(str(rd))
    try:
        with pytest.raises(g5.PreAccessDenied, match="locked") as e:
            g5.run(cfg_path, None)
        assert net.requests == [] and not os.path.exists(cfg["out_dir"])
        assert os.listdir(rd) == [".lock"]                         # nothing was bound or written before the lock
        ck = tmp_path / "w"
        ck.mkdir()
        other = g5.g4_june.acquire_run_lock(str(ck))
        port = good_port()
        with pytest.raises(g5.PreAccessDenied, match="locked"):
            g5.run(cfg_path, port, checkpoint_dir=str(ck))
        assert port.calls == [] and os.listdir(ck) == [".lock"]
        other.close()
    finally:
        held.close()


def test_lock_file_is_not_a_leftover_and_resume_works(tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    g5.g4_june.acquire_run_lock(str(d)).close()
    b = {"producer_identity": "p"}
    g5.bind_work_dir(str(d), b)                                    # a lone .lock is not an "unbound file"
    g5.bind_work_dir(str(d), b)


def test_binding_follows_producer_source_hashes_not_git_head(tmp_path, monkeypatch):
    cfg_path, cfg, p = build(tmp_path)
    heads = iter(range(100))
    monkeypatch.setattr(g5.rpc, "producer_identity", lambda paths, head=None: f"{next(heads):040x}:{'b' * 16}")   # HEAD moves between calls
    b1, b2 = g5.make_binding(token(cfg_path)), g5.make_binding(token(cfg_path))
    assert b1 == b2 and "producer_identity" not in b1
    assert set(b1["producer_source_sha256"]) == set(g5._PRODUCER_PY)
    mine = tmp_path / "src"
    mine.mkdir()
    files = []
    for rel in g5._PRODUCER_PY:
        f = mine / rel.replace("/", "_")
        f.write_bytes(open(os.path.join(g5.REPO_ROOT, rel) if os.path.exists(os.path.join(g5.REPO_ROOT, rel)) else g5.PRODUCER_FILES[g5._PRODUCER_PY.index(rel)], "rb").read())
        files.append(str(f))
    monkeypatch.setattr(g5, "PRODUCER_FILES", tuple(files))
    c1 = g5.make_binding(token(cfg_path))
    assert c1 == g5.make_binding(token(cfg_path))
    open(files[0], "ab").write(b"# edited\n")
    assert g5.make_binding(token(cfg_path)) != c1                   # any producer-file change = a different run (fail closed)


def _bound_dir(tmp_path, entries):
    d = tmp_path / "w"
    b = {"producer_identity": "p"}
    cp = g5.CheckpointedPort(object(), str(d), b)
    for k, v in entries.items():
        cp._ck.put(k, v)
    cp.close()
    return d, b


def _bal_receipt_sha(addrs, bals):
    """What g4._point_batch receipts: sha256(json.dumps(raw eth_call results, sort_keys=True)), the results being 32-byte hex words."""
    return hashlib.sha256(json.dumps(["0x%064x" % bals[a] for a in addrs], sort_keys=True).encode()).hexdigest()


def test_cached_balances_need_the_receipts_file_or_a_matching_receipt(tmp_path):
    key = f"bal:USDC:{POST_END}:{g5._addrs_sha([A, B])}"
    d, b = _bound_dir(tmp_path, {key: {A: 5, B: 6}})
    with pytest.raises(g5.CheckpointError, match="receipts.jsonl.*missing|missing"):
        g5.CheckpointedPort(object(), str(d), b, receipts_path=str(d / "receipts.jsonl"))
    # a receipts file that never receipted that balance query: the cached batch is a miss (refetched), not trusted
    (d / "receipts.jsonl").write_text(json.dumps({"gate": "g4", "kind": "balanceOf", "coin": "USDT", "fromBlock": POST_END, "toBlock": POST_END, "addressCount": 2}) + "\n")

    class Net:
        calls = 0

        def balance_of_many(self, coin, addrs, block):
            Net.calls += 1
            return {a: 99 for a in addrs}
    cp = g5.CheckpointedPort(Net(), str(d), b, receipts_path=str(d / "receipts.jsonl"))
    assert cp.balance_of_many("USDC", [A, B], POST_END) == {A: 99, B: 99} and Net.calls == 1
    cp.close()
    # with the matching balanceOf receipt, the cache is a hit
    (tmp_path / "x").mkdir()
    d2, b2 = _bound_dir(tmp_path / "x", {key: {A: 5, B: 6}})
    (d2 / "receipts.jsonl").write_text(json.dumps({"gate": "g4", "kind": "balanceOf", "coin": "USDC", "fromBlock": POST_END, "toBlock": POST_END,
                                                   "addressCount": 2, "sha256": _bal_receipt_sha([A, B], {A: 5, B: 6})}) + "\n")
    Net.calls = 0
    cp = g5.CheckpointedPort(Net(), str(d2), b2, receipts_path=str(d2 / "receipts.jsonl"))
    assert cp.balance_of_many("USDC", [A, B], POST_END) == {A: 5, B: 6} and Net.calls == 0
    cp.close()


def test_exit3_on_a_resume_does_not_claim_no_post_data_was_read(tmp_path, live, monkeypatch, capsys):
    net, rd, *_ = live
    cfg_path, cfg, p = build(tmp_path)
    binding = g5.make_binding(token(cfg_path))
    g5.bind_work_dir(str(rd), binding)
    (rd / "checkpoint.jsonl").write_text(json.dumps({"k": f"bal:USDC:{POST_END}:x", "v": {A: 1}}) + "\n")      # earlier run's cache, receipts gone
    import run_all
    assert run_all.run_g5(cfg_path) == 3
    out = capsys.readouterr().out
    assert "no RPC made" in out and "already parsed" in out and "no post data read" not in out and net.requests == []
    (p["sd"] / "signing.json").unlink()
    commit_all(p["repo"])
    assert run_all.run_g5(cfg_path) == 3
    assert "no post data read, no RPC" in capsys.readouterr().out         # a pre-signature denial keeps the stronger statement


def test_cached_balances_are_recomputed_against_the_receipt_digest(tmp_path):
    """Security MED: a cached balance batch of the right SHAPE but with tampered values (or a receipt without a digest) is a miss, not a hit."""
    key = f"bal:USDC:{POST_END}:{g5._addrs_sha([A, B])}"

    class Net:
        calls = 0

        def balance_of_many(self, coin, addrs, block):
            Net.calls += 1
            return {a: 99 for a in addrs}
    rec = lambda **kw: json.dumps({"gate": "g4", "kind": "balanceOf", "coin": "USDC", "fromBlock": POST_END, "toBlock": POST_END, "addressCount": 2, **kw}) + "\n"
    for i, (cached, receipt) in enumerate([
            ({A: 5, B: 7}, rec(sha256=_bal_receipt_sha([A, B], {A: 5, B: 6}))),          # value tampered in the cache, receipt honest
            ({A: 5, B: 6}, rec()),                                                      # receipt carries no digest
            ({A: 5, B: 6}, rec(sha256="0" * 64))]):                                     # receipt digest differs
        (tmp_path / str(i)).mkdir()
        d, b = _bound_dir(tmp_path / str(i), {key: cached})
        (d / "receipts.jsonl").write_text(receipt)
        Net.calls = 0
        cp = g5.CheckpointedPort(Net(), str(d), b, receipts_path=str(d / "receipts.jsonl"))
        assert cp.balance_of_many("USDC", [A, B], POST_END) == {A: 99, B: 99} and Net.calls == 1, i
        cp.close()
    # a receipt is spent once: two identical cached batches need two receipts
    (tmp_path / "once").mkdir()
    d, b = _bound_dir(tmp_path / "once", {key: {A: 5, B: 6}})
    (d / "receipts.jsonl").write_text(rec(sha256=_bal_receipt_sha([A, B], {A: 5, B: 6})))
    cp = g5.CheckpointedPort(Net(), str(d), b, receipts_path=str(d / "receipts.jsonl"))
    Net.calls = 0
    assert cp.balance_of_many("USDC", [A, B], POST_END) == {A: 5, B: 6} and Net.calls == 0
    cp.close()


def test_run_lock_never_follows_a_symlinked_lock_file(tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("precious\n")
    os.symlink(victim, d / ".lock")
    with pytest.raises(g5.g4_june.RunLocked):
        g5.g4_june.acquire_run_lock(str(d))
    assert victim.read_text() == "precious\n"          # not truncated / written through the link


# --- round 4: F1 / F3 / F4 unit cases (the Py/R parity cases are in _PARITY) -------------------------------------------------------

@pytest.mark.parametrize("raw", [b'{"a": NaN}', b'{"a": Infinity}', b'{"a": -Infinity}', json.dumps({"a": 1}).encode("utf-16"),
                                 json.dumps({"a": 1}).encode("utf-16-le"), json.dumps({"a": 1}).encode("utf-32"), b"\xef\xbb\xbf" + b'{"a": 1}', b'{"a": "\xff"}'])
def test_loads_is_strict_utf8_without_bom_or_nonfinite_constants(raw):
    with pytest.raises(g5.SignatureError, match="not valid JSON"):
        g5._loads(raw, "doc")
    assert g5._loads(b'{"a": 1}', "doc") == {"a": 1}


def test_unicode_whitespace_is_not_blank_in_the_sha_and_code_lists():
    ok = "ab" * 32 + "  a.md\n"
    assert g5.parse_sha256_list(ok + " \t\r\n\n") == {"a.md": "ab" * 32}
    for bad in ("\u00a0\n", "\u2003\n", "\u2028\n", "\x0b\n", "\x0c\n", "\x85\n", "\x1c\n"):
        with pytest.raises(g5.SignatureError):
            g5.parse_sha256_list(ok + bad)


def test_fullmatch_everywhere_a_trailing_newline_is_not_a_hash_address_or_integer():
    # the patterns carry no `^`/`$` anchors, so a stray `.match` (which would accept "\n") cannot be reintroduced silently
    assert not any(r.pattern.startswith("^") or r.pattern.endswith("$") for r in (g5._SHA_RE, g5._ADDR_RE, g5._INT_RE))
    assert g5._SHA_RE.fullmatch("ab" * 32) and not g5._SHA_RE.fullmatch("ab" * 32 + "\n")
    assert not g5._ADDR_RE.fullmatch(A + "\n") and g5._ADDR_RE.fullmatch(A) and not g5._INT_RE.fullmatch("5\n")


def test_trailing_newline_sha_in_the_signed_post_input_map_is_denied(tmp_path):
    def m(c, man):
        k = g5.POST_REQUIRED_KEYS[0]
        man["post_input_file_sha256"][k] += "\n"
    cfg_path, cfg, p = build(tmp_path, mutate=m)
    with pytest.raises(g5.SignatureError, match="post_input_file_sha256"):
        token(cfg_path)


def test_wallet_id_with_a_trailing_newline_is_denied(tmp_path):
    def m(cfg, man):
        wp = cfg["populations"][0]["wallet_map_path"]
        with open(wp, "a", newline="") as f:
            csv.writer(f, lineterminator="\n").writerow(["main", "gen1", "77\n", "0x" + "7" * 40])      # quoted field: "77\n" is a valid CSV cell
        man["input_file_sha256"][wp] = sha(open(wp, "rb").read())
    _fails_before_rpc(build(tmp_path, m)[0], g5.InputMismatch)


def test_destroyed_data_regex_rejects_a_trailing_newline():
    log = {"topics": [g5.DESTROYED_TOPIC], "address": g5.g1.USDT, "data": "0x" + "0" * 24 + "ab" * 20 + "0" * 63 + "5" + "\n", "blockHash": "0x1", "logIndex": "0x0"}
    with pytest.raises(AssertionError, match="64 bytes"):
        g5.decode_destroyed([log], set(), (1, 0, 2))


# --- round 4: F2 signed post inputs are hashed against the real files before any RPC -----------------------------------------------

def _hash_case(tmp_path, edit):
    cfg_path, cfg, p = build(tmp_path)
    edit(cfg, p)
    port = good_port()
    with pytest.raises(g5.InputMismatch, match="post input") as e:
        g5.run(cfg_path, port)
    assert e.value.pre_access is True and port.calls == []


def test_post_input_bytes_must_match_the_signed_hash_before_any_rpc(tmp_path):
    def edit(cfg, p):
        f = cfg["post_input_paths"][g5.POST_REQUIRED_KEYS[2]]
        open(f, "ab").write(b"tampered")
    _hash_case(tmp_path, edit)


def test_missing_or_symlinked_post_input_is_denied_before_any_rpc(tmp_path):
    def gone(cfg, p):
        os.unlink(cfg["post_input_paths"][g5.POST_REQUIRED_KEYS[0]])
    (tmp_path / "a").mkdir()
    _hash_case(tmp_path / "a", gone)

    def link(cfg, p):
        f = cfg["post_input_paths"][g5.POST_REQUIRED_KEYS[1]]
        good = open(f, "rb").read()
        os.unlink(f)
        (tmp_path / "real.csv").write_bytes(good)
        os.symlink(tmp_path / "real.csv", f)
    (tmp_path / "b").mkdir()
    _hash_case(tmp_path / "b", link)


def test_post_input_paths_must_cover_exactly_the_signed_keys(tmp_path):
    cfg_path, cfg, p = build(tmp_path, mutate=lambda c, m: c["post_input_paths"].pop(g5.POST_REQUIRED_KEYS[0]))
    with pytest.raises(g5.InputMismatch, match="post_input_paths keys"):
        token(cfg_path)
    (tmp_path / "n").mkdir()
    cfg_path, cfg, p = build(tmp_path / "n", mutate=lambda c, m: c.pop("post_input_paths"))
    with pytest.raises(g5.InputMismatch, match="missing required keys"):
        token(cfg_path)


@pytest.mark.skipif(not shutil.which("Rscript"), reason="needs Rscript")
def test_python_and_r_agree_on_signed_input_hashes(tmp_path):
    """F2 parity: the same signed input set through g5.verify_post_inputs and R verify_post_inputs -> same verdict (mismatch, missing, ok)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    real = {k: REAL_INSTALLED_R_VERSION(k) for k in g5.POST_PINNED_PACKAGES}
    if None in real.values():
        pytest.skip("fixest/data.table not installed in R")
    cases = {"ok": None, "tampered": lambda fs: open(fs[0], "ab").write(b"x"), "missing": lambda fs: os.unlink(fs[0])}
    verdicts, dirs = {}, {}
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(g5, "installed_r_version", lambda pkg: real.get(pkg))
        mp.setattr(g5, "installed_r_string", lambda: REAL_R_STRING)
        for i, (name, edit) in enumerate(cases.items()):
            sub = tmp_path / f"c{i}"
            sub.mkdir()
            cfg_path, cfg, p = build(sub, mutate=lambda c, m: m.update(r_package_versions=dict(real), r_version=REAL_R_STRING))
            v = token(cfg_path)
            files = [cfg["post_input_paths"][k] for k in g5.POST_REQUIRED_KEYS]
            if edit:
                edit(files)
            try:
                g5.verify_post_inputs(v)
                verdicts[name] = True
            except g5.InputMismatch:
                verdicts[name] = False
            dirs[name] = (str(p["repo"]), cfg["post_input_paths"])
    finally:
        mp.undo()
    script = ('for (f in c("utils", "decide", "cem", "estimate", "validate", "synth", "pre", "blacklist", "presign", "post_gate")) source(file.path("R", paste0(f, ".R")))\n'
              'a <- commandArgs(TRUE); repo <- a[1]; inp <- jsonlite::fromJSON(a[2], simplifyVector = TRUE)\n'
              'r <- tryCatch({ sig <- verify_post_signature(repo, POST_R_CODE_FILES); verify_post_inputs(sig, unlist(inp)); "ACCEPT" }, error = function(e) "DENY")\n'
              'cat(r, "\\n")\n')
    rv = {}
    for name, (repo_dir, paths) in dirs.items():
        out = subprocess.run(["Rscript", "-e", script, repo_dir, json.dumps(paths)], cwd=root, capture_output=True, text=True)
        assert out.returncode == 0, out.stderr[-2000:]
        rv[name] = out.stdout.split()[0] == "ACCEPT"
    assert verdicts == rv == {"ok": True, "tampered": False, "missing": False}, (verdicts, rv)
