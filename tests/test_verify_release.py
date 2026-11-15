"""T0 verifier: passes on the tree it ships in; fails when one bound byte is flipped (tested on a temp copy)."""
import os
import shutil
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
VERIFY = os.path.join(ROOT, "tools", "verify_release.py")


def run(root):
    return subprocess.run([sys.executable, VERIFY, root], capture_output=True, text=True)


def bound_copy(tmp_path):
    shutil.copytree(os.path.join(ROOT, "prespec"), tmp_path / "prespec")
    for line in open(os.path.join(ROOT, "prespec", "code_manifest.txt"), encoding="utf-8"):
        if line.strip():
            rel = line.split(None, 1)[1].strip()
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(os.path.join(ROOT, rel), tmp_path / rel)
    return tmp_path


def test_passes_on_this_tree_and_prints_both_claims():
    r = run(ROOT)
    assert r.returncode == 0, r.stdout
    assert "prespec/SHA256: 4/4   code_manifest.txt: 35/35" in r.stdout
    assert "What this proves" in r.stdout and "What it does not prove" in r.stdout


def test_flipped_byte_in_a_manifest_file_fails(tmp_path):
    d = bound_copy(tmp_path)
    assert run(str(d)).returncode == 0
    p = d / "lib" / "keccak.py"
    b = bytearray(p.read_bytes()); b[0] ^= 1; p.write_bytes(bytes(b))
    r = run(str(d))
    assert r.returncode == 1 and "FAIL  code-manifest  lib/keccak.py" in r.stdout


def test_flipped_byte_in_a_signed_list_file_fails(tmp_path):
    d = bound_copy(tmp_path)
    p = d / "prespec" / "prespec.md"
    b = bytearray(p.read_bytes()); b[-1] ^= 1; p.write_bytes(bytes(b))
    r = run(str(d))
    assert r.returncode == 1 and "FAIL  signing-list  prespec/prespec.md" in r.stdout


def test_missing_bound_file_fails(tmp_path):
    d = bound_copy(tmp_path)
    (d / "run_all.py").unlink()
    r = run(str(d))
    assert r.returncode == 1 and "MISSING" in r.stdout
