#!/usr/bin/env python3
"""Stub ctanalyse — Phase 2 stand-in for the C tool (docs/reference/ctanalyse_plan.md).

Implements the exact CLI and JSON contract of the future C ctanalyse so that
tools/ctdb_repair.py can be developed and tested end-to-end before any C exists.
Instead of RS decoding, it "analyses" by diffing the input PCM against a known-good
oracle copy, emitting real corrections in our sample domain.

Environment:
    CTANALYSE_ORACLE     path to the known-good PCM
                         (default private/testdata/ctanalyse/good.pcm)
    CTANALYSE_STUB_MODE  ok      normal operation (default)
                         refuse  emit can_recover=false (unrecoverable-disc path)
                         badold  corrupt one correction's "old" value (abort path)

Stub limitations (documented, deliberate): offset, crc_before and crc_after are null —
the driver's own gates are authoritative; the real tool fills them in.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np

_FRAME = 2352


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pcm", required=True, type=Path)
    ap.add_argument("--parity", required=True, type=Path)
    ap.add_argument("--npar", required=True, type=int)
    ap.add_argument(
        "--stride", required=True, type=int, help="wire stride (x2 internally)"
    )
    ap.add_argument("--toc", required=True, help="colon-joined INDEX-01 LBAs + leadout")
    ap.add_argument("--impl", default="auto")
    ap.add_argument("--threads", type=int, default=0)
    args = ap.parse_args()

    mode = os.environ.get("CTANALYSE_STUB_MODE", "ok")
    oracle = Path(
        os.environ.get("CTANALYSE_ORACLE", "private/testdata/ctanalyse/good.pcm")
    )

    if not args.parity.exists():
        print(f"stub: parity file missing: {args.parity}", file=sys.stderr)
        sys.exit(1)

    if mode == "refuse":
        json.dump(
            {
                "can_recover": False,
                "offset": None,
                "npar": args.npar,
                "corrections": [],
                "affected_sectors": [],
                "corrected_errors": 0,
                "crc_before": None,
                "crc_after": None,
            },
            sys.stdout,
        )
        return

    bad = np.fromfile(args.pcm, dtype="<u2")
    good = np.fromfile(oracle, dtype="<u2")
    if len(bad) != len(good):
        print(f"stub: size mismatch pcm={len(bad)} oracle={len(good)}", file=sys.stderr)
        sys.exit(1)

    idx = np.nonzero(bad != good)[0]
    corrections = [
        {"byte": int(i) * 2, "old": int(bad[i]), "new": int(good[i])} for i in idx
    ]
    if mode == "badold" and corrections:
        mid = len(corrections) // 2
        corrections[mid]["old"] ^= 0x5A5A  # deliberately wrong: driver must abort

    sectors = sorted({int(i) * 2 // _FRAME for i in idx})
    json.dump(
        {
            "can_recover": True,
            "offset": None,
            "npar": args.npar,
            "corrections": corrections,
            "affected_sectors": sectors,
            "corrected_errors": len(corrections),
            "crc_before": None,
            "crc_after": None,
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
