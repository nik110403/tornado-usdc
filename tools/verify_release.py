#!/usr/bin/env python3
"""T0 check: do the bytes in this tree match the hashes in the signing record? Stdlib only, no git, no network.

Usage: python3 tools/verify_release.py [tree-root]      (default: the repository root above tools/)

Checks (1) every entry of prespec/SHA256 against the file in prespec/, and (2) every entry of
prespec/code_manifest.txt against the file at that path from the tree root. Exit 0 only if all match.
"""
import hashlib
import os
import sys


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def entries(listfile):
    """Yield (expected_hash, relative_path) from a `sha256sum`-style list."""
    with open(listfile, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if not line.strip():
                continue
            parts = line.split(None, 1)
            if len(parts) != 2 or len(parts[0]) != 64:
                raise ValueError(f"{listfile}:{n}: not a 'sha256  path' line: {line!r}")
            yield parts[0].lower(), parts[1].lstrip("*").strip()


def check(root, listfile, base, label):
    ok = bad = 0
    for want, rel in entries(os.path.join(root, listfile)):
        path = os.path.join(root, base, rel)
        got = sha256(path) if os.path.isfile(path) else None
        good = got == want
        ok += good
        bad += not good
        print(f"{'PASS' if good else 'FAIL'}  {label}  {os.path.join(base, rel) if base else rel}"
              + ("" if good else f"  (expected {want[:12]}..., got {'MISSING' if got is None else got[:12] + '...'})"))
    return ok, bad


def main(argv):
    root = os.path.abspath(argv[1] if len(argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    try:
        a_ok, a_bad = check(root, "prespec/SHA256", "prespec", "signing-list")
        b_ok, b_bad = check(root, "prespec/code_manifest.txt", "", "code-manifest")
    except (OSError, ValueError) as e:
        print(f"ERROR: {e}")
        return 2
    print(f"\nprespec/SHA256: {a_ok}/{a_ok + a_bad}   code_manifest.txt: {b_ok}/{b_ok + b_bad}")
    print("RESULT:", "PASS" if not (a_bad or b_bad) else "FAIL")
    print("""
What this proves: these bytes are the ones hashed in the signing record (prespec/SHA256 and
prespec/code_manifest.txt, as shipped in this tree).
What it does not prove: when they were hashed (a third-party timestamp is a separate step); that the
analysis is correct; that the numbers in the paper regenerate from these inputs.""")
    return 0 if not (a_bad or b_bad) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
