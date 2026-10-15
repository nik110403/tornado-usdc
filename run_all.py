#!/usr/bin/env python3
"""Entry point: runs the gates in order, stops on first failure, exit code =
gate result.

`--gate none` runs nothing -- it only asserts chain id 1 and prints the
pinned blocks with their timestamps (provenance smoke test). `--gate all`
runs g0, g1, g1b, g1c, g2 in order and stops at the first non-zero return.
"""
import argparse
import sys

from gates import g0_manifest, g1_cohort, g1b_frame, g1c_funnel, g2_integrity
from lib import blocks, rpc

GATES = [
    ("g0", g0_manifest),
    ("g1", g1_cohort),
    ("g1b", g1b_frame),
    ("g1c", g1c_funnel),
    ("g2", g2_integrity),
]


def _print_provenance():
    chain_id = rpc.assert_chain_id("drpc")
    print(f"chain id {chain_id}")
    pinned = blocks.load_pinned()
    if not pinned:
        print("(no pinned blocks in config/blocks.json)")
        return
    for name in sorted(pinned):
        rec = pinned[name]
        print(f"{name}: block {rec['block']} @ {rec['timestamp']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gate",
        required=True,
        choices=["none", "g0", "g1", "g1b", "g1c", "g2", "all"],
        help="which gate to run (or 'none' for a provenance smoke test, 'all' to run every gate in order)",
    )
    args = parser.parse_args(argv)

    if args.gate == "none":
        _print_provenance()
        return 0

    rpc.assert_chain_id("drpc")
    cfg = {"blocks": blocks.load_pinned(), "endpoints": rpc.ENDPOINTS}

    if args.gate == "all":
        to_run = GATES
    else:
        to_run = [(name, mod) for name, mod in GATES if name == args.gate]

    for name, mod in to_run:
        print(f"=== gate {name} ===")
        result = mod.run(cfg)
        if result != 0:
            print(f"gate {name} FAILED (exit {result})")
            return result
    return 0


if __name__ == "__main__":
    sys.exit(main())
