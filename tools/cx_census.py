"""cx_census.py — aggregate a Plextor C1/C2/CU error census (c2read --cxscan).

Per-track error totals and worst-second hotspots from the Q-Check census. C1
(errors corrected at the first CIRC stage) is the early-warning signal no MMC
command exposes: a disc whose C1 rate climbs across re-rips is degrading long
before C2/CU appear. Complements — never replaces — the byte-accurate C2
pointer bitmap captured during rips.

Live (runs the scan, needs a Plextor drive; ~2 min):
    uv run python tools/cx_census.py --device /dev/sr0

Offline (re-aggregate saved 'cx <lba> <c1> <c2> <cu>' lines):
    uv run python tools/cx_census.py --cx cx.txt --device /dev/sr0
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _toc_boundaries(device: str) -> list[tuple[int, int]]:
    """[(track, start_lba)] from ``c2read --toc``."""
    cmd = ["c2read", "--device", device, "--toc"]
    out = subprocess.run(  # noqa: S603  # LINT-013
        cmd, capture_output=True, text=True, check=True
    ).stdout
    bounds: list[tuple[int, int]] = []
    for line in out.splitlines():
        parts = line.split()
        if parts[:1] == ["track"]:
            bounds.append((int(parts[1]), int(parts[3])))
        elif parts[:1] == ["leadout"]:
            bounds.append((0, int(parts[2])))  # sentinel: track 0 = lead-out
    return bounds


def _run_scan(device: str) -> list[str]:
    cmd = ["c2read", "--device", device, "--cxscan", "-q"]
    proc = subprocess.run(  # noqa: S603  # LINT-013
        cmd, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        sys.exit(proc.returncode)
    return proc.stdout.splitlines()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--device", default="/dev/sr0")
    ap.add_argument("--cx", type=Path, help="saved cx lines (skip the live scan)")
    ap.add_argument("--top", type=int, default=5, help="hotspots to list")
    args = ap.parse_args()

    lines = args.cx.read_text().splitlines() if args.cx else _run_scan(args.device)
    intervals = []
    for line in lines:
        parts = line.split()
        if len(parts) == 5 and parts[0] == "cx":
            lba, c1, c2, cu = (int(x) for x in parts[1:])
            intervals.append((lba, c1, c2, cu))
    if not intervals:
        print("no cx data")
        return 1

    bounds = _toc_boundaries(args.device)
    tracks = [(t, lba) for t, lba in bounds if t]
    leadout = next((lba for t, lba in bounds if t == 0), intervals[-1][0] + 75)

    def track_of(lba: int) -> int:
        cur = tracks[0][0]
        for t, start in tracks:
            if lba < start:
                break
            cur = t
        return cur

    per_track: dict[int, list[int]] = {t: [0, 0, 0] for t, _ in tracks}
    for lba, c1, c2, cu in intervals:
        agg = per_track[track_of(lba)]
        agg[0] += c1
        agg[1] += c2
        agg[2] += cu

    total = [sum(v[i] for v in per_track.values()) for i in range(3)]
    seconds = leadout / 75
    print(f"{'trk':>3} {'C1':>8} {'C2':>6} {'CU':>6}")
    for t, (c1, c2, cu) in sorted(per_track.items()):
        mark = "  <-- damage" if cu or c2 > 10 * (1 + total[1] // 100) else ""
        print(f"{t:>3} {c1:>8} {c2:>6} {cu:>6}{mark}")
    print(
        f"disc: C1 {total[0]} ({total[0] / seconds:.2f}/s avg), "
        f"C2 {total[1]}, CU {total[2]}"
    )

    hot = sorted(intervals, key=lambda x: (x[3], x[2], x[1]), reverse=True)
    print(f"top {args.top} hotspots (lba: C1/C2/CU):")
    for lba, c1, c2, cu in hot[: args.top]:
        print(f"  {lba} (track {track_of(lba)}): {c1}/{c2}/{cu}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
