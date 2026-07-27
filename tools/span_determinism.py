#!/usr/bin/env python3
"""Is a disc region deterministic when re-read at a FIXED speed?

The recovery model rests on an answer to this. If re-reading at one speed
reproduces the same bytes, then repetition at a fixed rung is information-free —
it resamples a deterministic function, and raising ``--retries`` just makes the
same wrong answer arrive with more confidence. That is the mechanism we had
offered for the bench ordering (`track-ladder` 19/20 vs `sector-hammer` 2/20).

Measured on Tracy Chapman 2026-07-27, lba 112500 +700 (inside the damaged track
8), four reads per condition:

    unpinned   4/4 distinct, 31-43 sectors differ pairwise
    pinned 8x  3/4 distinct,  1-4  sectors differ pairwise (one identical pair)

So **speed is the dominant variable but fixed-speed determinism is false.** An
order-of-magnitude drop, not a collapse to zero. Repetition at a fixed rung has a
small non-zero yield, which the earlier "deterministic at fixed speed" framing
denied.

**The confound this tool does not control, and you must not forget.** A slower
rung produces fewer wrong sectors in the first place, so *fewer sectors differing*
is exactly what you would see even if the erroring sectors were equally random.
Divergence and error rate are not separable from a single condition. To separate
them you need a reference (AccurateRip/CTDB-verified bytes) so you can express
divergence as a fraction of sectors that are wrong at all, rather than as a raw
count. Order effects are live too: whichever condition runs second sees a warmer,
possibly re-governed drive.

Treat the output as a comparison of conditions, never as a determinism verdict.

Usage:
    TMPDIR=/var/tmp uv run python tools/span_determinism.py \\
        --device /dev/sr0 --lba 112500 --count 700 --speed 8 --speed 0 --reads 4

``--speed 0`` means unpinned (whatever the drive is already doing).
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

_FRAME = 2352


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _sectors_differing(a: bytes, b: bytes) -> int:
    n = min(len(a), len(b))
    return sum(1 for i in range(0, n, _FRAME) if a[i : i + _FRAME] != b[i : i + _FRAME])


def run_condition(
    device: str, lba: int, count: int, speed: int, reads: int
) -> tuple[int, list[int]]:
    """Read the span *reads* times at one speed. Returns (distinct, pairwise diffs)."""
    from cdda2img.accudisc_reader import read_span_bytes

    label = "unpinned" if speed == 0 else f"pinned {speed}x"
    print(f"\n=== {label}")
    captures: list[bytes] = []
    for i in range(reads):
        data = read_span_bytes(device, lba, count, read_speed=speed or None)
        captures.append(data)
        print(f"  #{i + 1}  {_digest(data)}")

    distinct = len({_digest(c) for c in captures})
    diffs: list[int] = []
    for (i, a), (j, b) in itertools.combinations(enumerate(captures, 1), 2):
        n = _sectors_differing(a, b)
        diffs.append(n)
        if n:
            print(f"    #{i} vs #{j}: {n} sector(s) differ")
    if not any(diffs):
        print("    all reads identical")
    print(f"  distinct results {distinct}/{reads}")
    return distinct, diffs


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--device", default="/dev/sr0")
    ap.add_argument("--lba", type=int, required=True)
    ap.add_argument("--count", type=int, required=True)
    ap.add_argument(
        "--speed",
        type=int,
        action="append",
        default=None,
        help="rung to test, repeatable; 0 = unpinned (default: 0 then 8)",
    )
    ap.add_argument("--reads", type=int, default=4)
    args = ap.parse_args()

    speeds = args.speed if args.speed is not None else [0, 8]
    print(
        f"# {args.device}  lba {args.lba} +{args.count}  {args.reads} reads/condition"
    )

    results = {
        s: run_condition(args.device, args.lba, args.count, s, args.reads)
        for s in speeds
    }

    print("\n=== summary")
    for s, (distinct, diffs) in results.items():
        label = "unpinned" if s == 0 else f"{s}x"
        span = f"{min(diffs)}-{max(diffs)}" if diffs else "n/a"
        print(f"  {label:<10} distinct {distinct}/{args.reads}   pairwise diff {span}")
    print(
        "\nNOT a determinism verdict: a slower rung errs less to begin with, so a "
        "lower diff count is expected even with identical randomness. See the "
        "module docstring."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
