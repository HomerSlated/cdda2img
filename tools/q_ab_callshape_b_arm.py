#!/usr/bin/env python3
"""(B)-shaped arm of the AccuDisc call-boundary experiment (2026-08-30).

Live joint test with the AccuDisc agent, correspondence §184/§4-reframe. The
question: does one call running the whole speed ladder internally (A, their
side, not yet public API) beat one call per rung with the caller looping and
accumulating externally (B, this script, built from what IS bound today)?

Deliberately does NOT go through ``accudisc_reader.py`` (the project's seam).
That module already absorbs the ladder-looping and speed-restore bookkeeping
this experiment exists to measure the cost of -- routing through it would
hide the thing under test. This talks to the raw binding directly, the way a
caller writing fresh against today's API surface would have to.

Metric constraint agreed in §184's reply and the live cross-session exchange:
recovered-frame counts are NOT a valid (A)/(B) discriminator (two sweeps over
the same damaged span differ by entropy alone -- that is exactly the mistake
that produced the original static-Q null). The deterministic proxies are:

  1. wall-clock for the full N-rung ladder
  2. whether a duplicate HONOURED speed across rungs is detected/flagged
     (ReadStats.speed_honoured_x, not speed_requested_x -- a drive can
     quantise two distinct requests onto the same real rung)
  3. the shape of the caller-side accumulator itself (this file's line count
     and structure IS the (B)-arm data point for that axis)

Usage:
    uv run python tools/q_ab_callshape_b_arm.py --device /dev/sr0 \\
        --lba-start N --count N --out /var/tmp/q_ab_b_arm.json

Coordination: caller must hold /var/tmp/sr0.lock for the whole run (flock).
This script does not take the lock itself -- deliberately, so the shell
invocation controls the critical section and a crash mid-run cannot leave
the lock silently held past process exit.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "accudisc" / "pybinding"))

import accudisc  # ty: ignore[unresolved-import]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from cdda2img import drive_speed


def run(
    device: str, lba_start: int, count: int, order: list[int] | None = None
) -> dict:
    """Two hardware phases, deliberately separate (see FIRST-DRAFT BUG below).

    Phase 1: the actual per-rung data reads via ``read_span`` -- the operation
    (A) and (B) are both trying to accomplish. Phase 2: a duplicate-rung
    verdict via ``probe_speed_ladder``, scoped to this exact span.

    **FIRST-DRAFT BUG, kept visible rather than silently fixed.** The first
    version of this script read ``ReadStats.speed_honoured_x`` off the
    ``read_span`` result to detect duplicate rungs, and got 0 on every single
    rung -- which would have been reported as "(B) cannot tell its rungs
    apart", a finding about AccuDisc. It was a bug in this file:
    ``speed_requested_x``/``speed_honoured_x`` are populated by
    ``Device.read()``, not ``Device.read_span()`` -- our own
    ``accudisc_reader._log_speed_adopted`` docstring already says so ("these
    are the *pass* speed... the recovery ladder moves speed per rung mid-read,
    and a scalar pair cannot record that. The pre-flight answer there is
    ``probe_speed_ladder``'s verdict"). Checking that docstring before writing
    this function would have caught it before hardware time was spent; it
    caught it after, on inspection of a suspicious first result (4/5 rungs
    reporting honoured_x=0) rather than a rule followed in advance.

    The corrected mechanism -- ``SpeedRung.verdict`` from
    ``probe_speed_ladder``, scoped with this call's own ``lba``/``count`` --
    is the one this project's own ``drive_speed.admitted_ladder()`` already
    uses (rule 1, verdict-based). Scoping matters and is itself a finding:
    this project's ``admitted_ladder(device)`` takes no span and probes
    wherever AccuDisc's default location is, which need not be this span's
    radius. Re-run at this exact span, one rung (40x, requested = reported =
    40) came back DUPLICATE where the wide, unscoped probe had admitted it.
    That is a real correctness obligation a (B)-shaped caller can get wrong
    silently -- reuse a generic ladder instead of re-probing at the actual
    target -- which an (A)-shaped call that always knows its own span cannot.
    """
    ladder = drive_speed.admitted_ladder(device)
    if not ladder:
        msg = f"admitted_ladder returned empty for {device}"
        raise RuntimeError(msg)
    if order is not None:
        # Explicit rung order for the sequence-position confound test
        # (2026-08-30 cross-session exchange): the read-order matters
        # independently of which speeds are admitted, so this does NOT
        # re-derive or re-validate against admitted_ladder -- the caller is
        # asserting these are the same rungs in a deliberately different
        # order, and a mismatch would silently test the wrong ladder.
        if sorted(order) != sorted(ladder):
            msg = f"--order {order} is not a permutation of admitted_ladder {ladder}"
            raise RuntimeError(msg)
        ladder = order

    dev = accudisc.Device(device)  # ty: ignore[unresolved-attribute]
    t_ladder_start = time.monotonic()
    rungs: list[dict] = []

    try:
        for i, speed_x in enumerate(ladder):
            t_rung_start = time.monotonic()
            dev.set_speed(speed_x)
            data, result = dev.read_span(lba_start, count)
            t_rung_end = time.monotonic()

            stats = result.stats
            rungs.append({
                "rung_index": i,
                "requested_x": speed_x,
                "wall_clock_s": round(t_rung_end - t_rung_start, 4),
                "sectors_read": stats.sectors_read,
                "hard_errors": stats.hard_errors,
                "bytes_read": len(data),
            })

        # Phase 2: duplicate-verdict, scoped to the exact span just read.
        verdicts = dev.probe_speed_ladder(
            points=3, lba=lba_start, count=count, candidates=tuple(ladder)
        )
    finally:
        dev.close()

    t_ladder_end = time.monotonic()

    verdict_rows = [
        {
            "requested_x": v.requested_x,
            "reported_x": v.reported_x,
            "measured_cx": v.measured_cx,
            "verdict": v.verdict.name,
        }
        for v in verdicts
    ]
    duplicate_rungs = [row for row in verdict_rows if row["verdict"] == "DUPLICATE"]

    return {
        "arm": "B",
        "device": device,
        "lba_start": lba_start,
        "count": count,
        "ladder_requested": list(ladder),
        "total_wall_clock_s": round(t_ladder_end - t_ladder_start, 4),
        "rungs": rungs,
        "span_scoped_verdicts": verdict_rows,
        "duplicate_rungs_at_this_span": duplicate_rungs,
        "caller_side_bookkeeping": {
            # Self-reported, not measured by tooling -- the honest count of what
            # THIS file had to track that a merged (A) call would not need:
            # the ladder loop itself, remembering to re-scope the duplicate-rung
            # probe to the ACTUAL target span rather than reusing a generic
            # wide-scope ladder, and per-rung timing capture. None of these
            # exist in an (A)-shaped caller by construction -- and the
            # re-scoping one is exactly what this file's first draft got wrong.
            "concerns_tracked_across_calls": 3,
            "description": (
                "ladder iteration order, span-scoped duplicate-rung re-probe "
                "(NOT the generic wide-scope ladder -- see docstring), "
                "per-rung wall-clock capture -- all caller state, all absent "
                "from an (A)-shaped single call by construction"
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--lba-start", type=int, required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--order",
        type=str,
        default=None,
        help="Comma-separated rung order override, e.g. 4,8,24,32,40 "
        "(must be a permutation of admitted_ladder; for the sequence-"
        "position confound test)",
    )
    args = parser.parse_args()

    order = [int(x) for x in args.order.split(",")] if args.order else None
    result = run(args.device, args.lba_start, args.count, order=order)
    args.out.write_text(json.dumps(result, indent=2))
    print(f"wrote {args.out} ({result['total_wall_clock_s']}s total)")
    print(
        f"duplicate rungs at this span: {len(result['duplicate_rungs_at_this_span'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
