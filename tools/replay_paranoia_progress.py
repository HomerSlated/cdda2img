#!/usr/bin/env python3
"""Replay a cd-paranoia ``-e`` callback capture through the real progress path.

The cd-paranoia recovery readout can only render "recovering @ sector N — K jitter"
when the live stream actually contains correction events (fn=3 etc.). A disc that reads
clean produces only "reading @ sector N" — correct, but it never exercises the recovery
display. This tool feeds a *real* capture (e.g. a prior damaged-track ``-e`` dump) through
the genuine ``disc_reader._run_paranoia_with_progress`` + note→detail logic, with
realistic per-event wall-clock timing, so the recovery line renders in your terminal
exactly as it would during a damaged rip — no flaky disc required.

Capture a stream to replay with:

    cd-paranoia -e -d /dev/sr0 -O 30 -S 1 -- 8 /var/tmp/probe.wav 2> /var/tmp/t8.cs

Then:

    uv run python tools/replay_paranoia_progress.py /var/tmp/t8.cs
    uv run python tools/replay_paranoia_progress.py /var/tmp/t8.cs --speed 20   # 20x faster

The detail string is computed identically to cdda2img._paranoia_cb
(``update.note or f"({elapsed}/{total})"``).
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import cdda2img.disc_reader as dr

# Per-event wall-clock model: a drive read at 1x is the slow, visible phase
# (~75 sectors/s ≈ 13 ms/event); corrections/verify/wrote are in-memory and ~instant.
_READ_DELAY_S = 0.0133


def _replay_lines(path: Path, speed: float):
    """Yield capture lines, sleeping ~13ms before each [read] (scaled by 1/speed)."""
    name_re = re.compile(r"\[([^\]]*)\]")
    for line in path.read_text(errors="replace").splitlines():
        if not line.startswith("##:"):
            continue
        m = name_re.search(line)
        if m and m.group(1) == "read":
            time.sleep(_READ_DELAY_S / speed)
        yield line + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("capture", type=Path, help="cd-paranoia -e capture file")
    ap.add_argument(
        "--speed", type=float, default=1.0, help="time compression factor (default 1x)"
    )
    args = ap.parse_args()
    if not args.capture.is_file():
        print(f"no such capture: {args.capture}", file=sys.stderr)
        return 2

    lines = list(_replay_lines(args.capture, args.speed))

    # Stand in for cd-paranoia: hand the captured lines to the real progress reader.
    class _Replay:
        returncode = 0

        def __init__(self, cmd, **kw):
            self.stderr = iter(lines)

        def wait(self) -> int:
            return 0

    dr.subprocess.Popen = _Replay  # type: ignore[assignment]

    # Total sectors = highest WROTE position seen, so the bar scales sanely.
    rx = dr._CALLSCRIPT_RE
    wrote = [
        int(m.group(2)) // dr._CD_FRAMEWORDS
        for m in (rx.match(line) for line in lines)
        if m and int(m.group(1)) == dr._CB_WROTE
    ]
    disc_first = min(wrote) if wrote else 0
    total = (max(wrote) - disc_first + 1) if wrote else 1

    def render(update) -> None:
        # Identical detail logic to cdda2img._paranoia_cb.
        detail = update.note or f"({update.elapsed_frames}/{update.total_frames})"
        pct = int(update.fraction * 100)
        sys.stdout.write(f"\r{pct:3d}%  {detail:<60}")
        sys.stdout.flush()

    cmd = ["cd-paranoia", "-d", "/dev/sr0", "--", "8", "/dev/null"]
    dr._run_paranoia_with_progress(
        cmd, Path("/dev/null"), total, [(8, disc_first, total)], disc_first, render
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
