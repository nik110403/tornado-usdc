"""gates/g0_baseline_blacklist.py: producer (mocked RPC), extension-reuse proof, consumer loader. No network."""
import builtins
import csv
import json
import os

import pytest

from gates import g0_baseline_blacklist as g0b
from gates import g4_controls as g4
from gates import g4_june as gj
from lib import blocks, rpc

CENSUS = 6_200_000   # synthetic pins, small enough to run the producer fast
PRE_START = 6_240_000
JUNE_END = 6_250_000
PRE_END = 6_300_000
PINNED = {"census_start": {"block": CENSUS, "timestamp": "2019-11-25T18:27:24Z"},
          "pre_period_start": {"block": PRE_START, "timestamp": "2022-06-08T23:59:08Z"},
          "june_end": {"block": JUNE_END, "timestamp": "2022-06-30T23:59:28Z"},
          "pre_period_end": {"block": PRE_END, "timestamp": "2022-08-07T23:59:57Z"}}
W1, W2, W3, W4 = ("0x" + c * 40 for c in "abcd")

# (coin, address, event, block, log_index)
EVENTS = [
    ("USDT", W1, "AddedBlackList", 6_030_000, 1),     # pre-census add, never removed -> blacklisted at June start
    ("USDC", W2, "Blacklisted", 6_100_000, 0),        # pre-census add ...
    ("USDC", W2, "UnBlacklisted", 6_110_000, 0),      # ... removed pre-census -> clean at June start
    ("USDC", W3, "Blacklisted", 6_245_000, 2),        # added inside June window
    ("USDC", W3, "UnBlacklisted", 6_246_000, 0),      # ... and removed again (any-time still True)
    ("USDT", W4, "AddedBlackList", 6_270_000, 0),     # after june_end, before pre_period_end: main only
]


def _topic(coin, ev):
    return g0b.TOPICS[coin][ev]


def _log(coin, addr, ev, block, idx):
    return {"blockNumber": hex(block), "transactionHash": "0x" + f"{block:032x}{idx:032x}", "logIndex": hex(idx),
            "address": g0b.ADDRESS[coin], "topics": [_topic(coin, ev), "0x" + "0" * 24 + addr[2:]], "data": "0x"}


class Provider:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self, endpoint, a, b, address, topics):
        self.calls.append((endpoint, address, a, b))
        if self.fail:
            self.fail(len(self.calls), a, b)
        addr = address if isinstance(address, str) else address[0]
        wanted = {t.lower() for t in topics[0]}
        out = [_log(*e) for e in EVENTS
               if g0b.ADDRESS[e[0]].lower() == addr.lower() and a <= e[3] <= b and _topic(e[0], e[2]).lower() in wanted]
        return out, json.dumps(out).encode()


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(g0b, "OUT_ROOT", str(tmp_path / "out"))
    monkeypatch.setattr(g0b, "DEPLOY_BLOCK", {"USDC": 6_050_000, "USDT": 6_020_000})  # small synthetic spans
    monkeypatch.setattr(g0b.blocks, "load_pinned", lambda: PINNED)
    monkeypatch.setattr(g0b.rpc, "assert_chain_id", lambda ep: 1)
    monkeypatch.setattr(g0b, "_git_commit", lambda: "c0ffee" * 6 + "abcd")
    monkeypatch.setattr(rpc.time, "sleep", lambda *_: None)
    prov = Provider()
    monkeypatch.setattr(rpc, "_fetch_one_range", prov)
    return prov


def _gen(tmp_path, name="gen-1"):
    return str(tmp_path / "out" / name)


# --- producer -------------------------------------------------------------------------------

def test_full_span_fetch_and_manifest(env, tmp_path):
    assert g0b.run({"generation": "gen-1"}) == 0
    m = json.load(open(os.path.join(_gen(tmp_path), "manifest.json")))
    assert m["population_id"] == "blacklist_baseline" and m["cutoff_block"] == PRE_END
    assert m["span"] == {c: {"start": g0b.DEPLOY_BLOCK[c], "end": PRE_END} for c in g0b.COINS}
    assert m["row_counts"] == {"events.csv": 6, "USDC": 4, "USDT": 2}
    assert m["producing_commit"].startswith("c0ffee") and len(m["config_sha256"]) == 64
    assert set(m["output_file_sha256"]) == {"events.csv", "receipts.jsonl"}
    assert all(p["lo"] == g0b.DEPLOY_BLOCK[c] and p["hi"] == PRE_END for c, p in m["coverage"].items())
    assert {ep for ep, *_ in env.calls} == {"mevblocker"}  # one provider, no fallback
    assert all(not isinstance(a, list) or len(a) == 1 for _, a, *_ in env.calls)
    with pytest.raises(g0b.BaselineError, match="immutable"):
        g0b.run({"generation": "gen-1"})


def test_pre_census_opening_state_reaches_june(env, tmp_path):
    g0b.run({"generation": "gen-1"})
    b = g0b.load_baseline(_gen(tmp_path), "june", pinned=PINNED)
    # USDT add at 6.03M (before the census start), never removed: blacklisted at the June start although the g0 census starts later
    assert b.state_at("USDT", W1, PRE_START) is True
    assert b.any_time("USDT", W1, PRE_START, JUNE_END) is True
    assert b.state_at("USDC", W2, PRE_START) is False        # added and removed before the census
    assert b.any_time("USDC", W2, PRE_START, JUNE_END) is False
    assert b.state_at("USDC", W3, PRE_START) is False
    assert b.any_time("USDC", W3, PRE_START, JUNE_END) is True   # add+remove inside the window still counts


def test_consumer_specific_cutoffs(env, tmp_path):
    g0b.run({"generation": "gen-1"})
    june = g0b.load_baseline(_gen(tmp_path), "june", pinned=PINNED)
    main = g0b.load_baseline(_gen(tmp_path), "main", pinned=PINNED)
    assert june.cutoff == JUNE_END and main.cutoff == PRE_END
    assert W4 not in june.events({W4})["USDT"] and W4 in main.events({W4})["USDT"]
    assert main.any_time("USDT", W4, PRE_START, PRE_END) is True
    with pytest.raises(g0b.BaselineError, match="cutoff"):
        june.any_time("USDT", W4, PRE_START, PRE_END)
    with pytest.raises(g0b.BaselineError, match="unknown consumer"):
        g0b.load_baseline(_gen(tmp_path), "post", pinned=PINNED)
    with pytest.raises(g0b.BaselineError, match="not pinned"):
        g0b.load_baseline(_gen(tmp_path), "june", pinned={k: v for k, v in PINNED.items() if k != "june_end"})


def test_interrupted_producer_resumes_without_refetch(env, monkeypatch, tmp_path):
    def quota_after(n, a, b):
        if n > 7:
            raise rpc.RpcError("HTTP Error 429: Too Many Requests")

    first = Provider(fail=quota_after)
    monkeypatch.setattr(rpc, "_fetch_one_range", first)
    monkeypatch.setattr(rpc, "QUOTA_COOLDOWNS", (0,))
    with pytest.raises(rpc.QuotaError):
        g0b.run({"generation": "gen-1"})
    assert not os.path.exists(_gen(tmp_path))                       # no half generation
    done = {(a, b) for _, _, a, b in first.calls[:7]}
    second = Provider()
    monkeypatch.setattr(rpc, "_fetch_one_range", second)
    assert g0b.run({"generation": "gen-1"}) == 0
    assert done.isdisjoint({(a, b) for _, _, a, b in second.calls})  # completed chunks not refetched
    m = json.load(open(os.path.join(_gen(tmp_path), "manifest.json")))
    assert len(m["coverage"]["USDT"]["attempt_ids"]) == 2   # one proof spans chunks from both attempts
    assert m["row_counts"]["events.csv"] == 6


def test_corrupt_work_chunk_fails_the_producer(env, tmp_path):
    g0b.run({"generation": "gen-1"})
    chunk = next(p for p in (tmp_path / "out" / "_work").rglob("chunk-*.json"))
    chunk.write_text("{not json")
    with pytest.raises(rpc.CheckpointError):
        g0b.run({"generation": "gen-2"})
    assert not os.path.exists(_gen(tmp_path, "gen-2"))


def test_cross_commit_work_is_not_reused(env, monkeypatch, tmp_path):
    g0b.run({"generation": "gen-1"})
    n = len(env.calls)
    monkeypatch.setattr(g0b, "_git_commit", lambda: "deadbe" * 6 + "ef01")
    g0b.run({"generation": "gen-2"})
    assert len(env.calls) == 2 * n  # fresh work dir: nothing carried over from another commit


def test_no_lifetime_census_files_are_opened(env, monkeypatch, tmp_path):
    opened = []
    real = builtins.open
    monkeypatch.setattr(builtins, "open", lambda p, *a, **k: (opened.append(str(p)), real(p, *a, **k))[1])
    g0b.run({"generation": "gen-1"})
    g0b.load_baseline(_gen(tmp_path), "main", pinned=PINNED)
    assert opened and not [p for p in opened if f"{os.sep}g0{os.sep}" in p or f"{os.sep}g0_ext{os.sep}" in p]


# --- deployment-extension reuse -------------------------------------------------------------

def _write_extension(tmp_path, tamper=None):
    ext_end = CENSUS - 1
    rows = [e for e in EVENTS if e[3] <= ext_end]
    csv_path, rcp_path = tmp_path / "ext.csv", tmp_path / "ext_receipts.jsonl"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=g0b.CSV_COLS)
        w.writeheader()
        for coin, addr, ev, blk, idx in rows:
            w.writerow({"coin": coin, "address": addr, "event": ev, "block": blk,
                        "tx_hash": _log(coin, addr, ev, blk, idx)["transactionHash"], "log_index": idx})
    recs = []
    for coin in g0b.COINS:
        for a, b in rpc._split_ranges(g0b.DEPLOY_BLOCK[coin], ext_end, 50_000):
            n = sum(1 for e in rows if e[0] == coin and a <= e[3] <= b)
            recs.append({"endpoint": "mevblocker", "addressCount": 1, "fromBlock": a, "toBlock": b, "logCount": n,
                         "topics": [list(g0b.TOPICS[coin].values())], "sha256": "x", "gate": "g0_ext"})
    if tamper:
        recs = tamper(recs)
    rcp_path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    return {"csv": str(csv_path), "receipts": str(rcp_path), "csv_sha256": g0b._sha256_file(str(csv_path)),
            "receipts_sha256": g0b._sha256_file(str(rcp_path))}


def test_extension_reuse_fetches_only_from_census_start(env, tmp_path):
    ext = _write_extension(tmp_path)
    g0b.run({"generation": "gen-1", "extension": ext})
    assert min(a for _, _, a, _ in env.calls) == CENSUS            # nothing before the census was refetched
    m = json.load(open(os.path.join(_gen(tmp_path), "manifest.json")))
    assert m["input_file_sha256"] == {"extension_csv": ext["csv_sha256"], "extension_receipts": ext["receipts_sha256"]}
    assert m["row_counts"] == {"events.csv": 6, "USDC": 4, "USDT": 2}
    b = g0b.load_baseline(_gen(tmp_path), "june", pinned=PINNED)
    assert b.state_at("USDT", W1, PRE_START) is True               # opening state comes via the proven extension rows


@pytest.mark.parametrize("what", ["csv_hash", "receipts_hash", "no_hash", "csv_edit", "gap", "wrong_count", "wrong_endpoint", "wrong_topics"])
def test_extension_reuse_rejects_unproven_data(env, tmp_path, what):
    def drop_middle(recs):
        return recs[:1] + recs[2:]

    def wrong_count(recs):
        recs[0]["logCount"] += 1
        return recs

    def wrong_endpoint(recs):
        recs[0]["endpoint"] = "drpc"
        return recs

    def wrong_topics(recs):
        recs[0]["topics"] = [[g0b.TOPICS["USDT"]["AddedBlackList"]]]
        return recs

    tamper = {"gap": drop_middle, "wrong_count": wrong_count, "wrong_endpoint": wrong_endpoint,
              "wrong_topics": wrong_topics}.get(what)
    ext = _write_extension(tmp_path, tamper=tamper)
    if what == "csv_hash":
        ext["csv_sha256"] = "0" * 64
    elif what == "receipts_hash":
        ext["receipts_sha256"] = "1" * 64
    elif what == "no_hash":
        ext.pop("csv_sha256")
    elif what == "csv_edit":
        with open(ext["csv"], "a") as f:
            f.write("USDC,0x1,Blacklisted,6100000,0xdead,9\n")
    with pytest.raises(g0b.BaselineError):
        g0b.run({"generation": "gen-1", "extension": ext})
    assert env.calls == []  # rejected before any fetch, and never silently replaced by a full fetch


# --- loader integrity -----------------------------------------------------------------------

def test_loader_rejects_tampered_generation(env, tmp_path):
    g0b.run({"generation": "gen-1"})
    d = _gen(tmp_path)
    good = open(os.path.join(d, "events.csv"), "rb").read()
    with open(os.path.join(d, "events.csv"), "ab") as f:
        f.write(b"USDC,0x" + b"9" * 40 + b",Blacklisted,6100000,0xbeef,1\r\n")
    with pytest.raises(g0b.BaselineError, match="sha256"):
        g0b.load_baseline(d, "june", pinned=PINNED)
    open(os.path.join(d, "events.csv"), "wb").write(good)
    g0b.load_baseline(d, "june", pinned=PINNED)
    m = json.load(open(os.path.join(d, "manifest.json")))
    for edit in (lambda m: m.update(cutoff_block=PRE_END - 1),
                 lambda m: m["span"]["USDT"].update(start=g0b.DEPLOY_BLOCK["USDT"] + 1),
                 lambda m: m.update(config_sha256="0" * 64),
                 lambda m: m.update(population_id="other"),
                 lambda m: m["row_counts"].update(USDC=99)):
        bad = json.loads(json.dumps(m))
        edit(bad)
        json.dump(bad, open(os.path.join(d, "manifest.json"), "w"))
        with pytest.raises(g0b.BaselineError):
            g0b.load_baseline(d, "june", pinned=PINNED)
    json.dump(m, open(os.path.join(d, "manifest.json"), "w"))
    with pytest.raises(g0b.BaselineError, match="pre_period_end"):
        g0b.load_baseline(d, "june", pinned={**PINNED, "pre_period_end": {"block": PRE_END + 1}})
    os.remove(os.path.join(d, "manifest.json"))
    with pytest.raises(g0b.BaselineError, match="missing"):
        g0b.load_baseline(d, "june", pinned=PINNED)


def test_state_functions_match_g4(env):
    ev = [(10, 0, "add"), (20, 1, "remove"), (20, 0, "add"), (40, 0, "add")]
    for blk in (5, 10, 15, 20, 25, 45):
        assert g0b.state_at(ev, blk) == g4.blacklist_state_at(ev, blk)
    for s, e in ((5, 15), (22, 30), (22, 45), (0, 5)):
        assert g0b.any_time(ev, s, e) == gj.blacklisted_in_window(ev, s, e)


def test_events_have_g4_loader_shape(env, tmp_path, monkeypatch):
    """Baseline.events() is a drop-in for g4_controls._load_blacklist_events on the same data."""
    g0b.run({"generation": "gen-1"})
    g0 = tmp_path / "g0"
    g0.mkdir()
    with open(g0 / "circle_events.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["address", "block", "log_index", "event"])
        w.writeheader()
        w.writerows({"address": a, "block": b, "log_index": i, "event": ev} for c, a, ev, b, i in EVENTS if c == "USDC")
    with open(g0 / "usdt_events.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["address", "block", "log_index", "event"])
        w.writeheader()
        w.writerows({"address": a, "block": b, "log_index": i, "event": ev} for c, a, ev, b, i in EVENTS if c == "USDT")
    monkeypatch.setattr(g4, "_G0_DIR", str(g0))
    universe = {W1, W2, W3, W4}
    want = g4._load_blacklist_events(universe, PRE_END)
    got = g0b.load_baseline(_gen(tmp_path), "main", pinned=PINNED).events(universe)
    norm = lambda d: {c: {a: sorted(v) for a, v in d[c].items()} for c in d}
    assert norm(got) == norm(want)


# --- g4_june consumption: explicit input, no fallback -----------------------------------------

def test_g4_june_requires_explicit_baseline_input(monkeypatch):
    monkeypatch.setenv("G4_JUNE_BASELINE_BLACKLIST_DIR", "/y")  # the env var is gone: it must NOT satisfy the input
    with pytest.raises(RuntimeError, match="never falls back"):
        gj.baseline_dir({})
    assert gj.baseline_dir({"baseline_blacklist_dir": "/x"}) == "/x"
    with pytest.raises(RuntimeError, match="never falls back"):
        gj.baseline_dir({})
    assert "circle_events.csv" not in gj.INPUT_FILES  # lifetime blacklist events are not a June input


# --- fix round 1 ------------------------------------------------------------------------------

def _edit_manifest(d, fn):
    m = json.load(open(os.path.join(d, "manifest.json")))
    fn(m)
    json.dump(m, open(os.path.join(d, "manifest.json"), "w"))


def test_loader_rejects_unknown_commit_extra_outputs_and_bad_coverage(env, tmp_path):
    g0b.run({"generation": "gen-1"})
    d = _gen(tmp_path)
    good = open(os.path.join(d, "manifest.json")).read()
    for edit in (lambda m: m.update(producing_commit="unknown"),
                 lambda m: m["output_file_sha256"].update({"../events.csv": "0" * 64}),
                 lambda m: m["output_file_sha256"].pop("receipts.jsonl"),
                 lambda m: m["coverage"]["USDT"].update(lo=m["coverage"]["USDT"]["lo"] + 1),
                 lambda m: m["coverage"]["USDC"].update(hi=PRE_END - 1)):
        _edit_manifest(d, edit)
        with pytest.raises(g0b.BaselineError):
            g0b.load_baseline(d, "june", pinned=PINNED)
        open(os.path.join(d, "manifest.json"), "w").write(good)
    g0b.load_baseline(d, "june", pinned=PINNED)


def test_vcs_failure_is_an_error_not_unknown(monkeypatch):
    def nogit(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(g0b.subprocess, "check_output", nogit)
    with pytest.raises(g0b.BaselineError, match="producer commit"):
        g0b._git_commit()


def test_edited_producer_does_not_reuse_chunks(env, monkeypatch, tmp_path):
    g0b.run({"generation": "gen-1"})
    n = len(env.calls)
    monkeypatch.setattr(g0b.rpc, "producer_identity", lambda paths, head=None: head + ":edited")
    g0b.run({"generation": "gen-2"})
    assert len(env.calls) == 2 * n


@pytest.mark.parametrize("bad", ["../x", ".hidden", "a/b", "a b"])
def test_generation_name_validated(env, bad):
    with pytest.raises(g0b.BaselineError, match="invalid generation"):
        g0b.run({"generation": bad})


def test_decode_address_guards():
    ok = {"topics": ["t", "0x" + "0" * 24 + "ab" * 20], "data": "0x"}
    assert g0b._decode_address(ok) == "0x" + "ab" * 20
    for bad in ({"topics": ["t", "0x1234"], "data": "0x"}, {"topics": ["t"], "data": "0x12"},
                {"topics": ["t", "0x" + "1" * 64], "data": "0x"}, {"topics": ["t"], "data": None}):
        with pytest.raises(g0b.BaselineError):
            g0b._decode_address(bad)


def test_extension_rejects_receipt_at_result_cap_and_wrong_address(env, tmp_path):
    cap = rpc.result_cap("mevblocker")

    def at_cap(recs):
        recs[0]["logCount"] = cap
        return recs

    with pytest.raises(g0b.BaselineError, match="result cap"):
        g0b.run({"generation": "g1", "extension": _write_extension(tmp_path, tamper=at_cap)})

    def wrong_addr(recs):
        recs[0]["address"] = "0x" + "77" * 20
        return recs

    with pytest.raises(g0b.BaselineError, match="address"):
        g0b.run({"generation": "g2", "extension": _write_extension(tmp_path, tamper=wrong_addr)})
    assert env.calls == []


def test_generation_dir_contents(env, tmp_path):
    g0b.run({"generation": "gen-1"})
    assert sorted(os.listdir(_gen(tmp_path))) == ["events.csv", "manifest.json", "receipts.jsonl"]
