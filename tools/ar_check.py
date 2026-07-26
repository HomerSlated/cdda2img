#!/usr/bin/env python3
"""AccurateRip check for raw whole-disc PCM captures — 450 / v1 / v2, per track.

Answers the first three questions of a rip post-mortem *separately*, because they
fail for different reasons:

  * **crc450** — the single sector at track offset 450 matches a DB submission.
    A match here with v1/v2 failing means the pressing and the offset are right
    and the damage is elsewhere in the track. A miss on *every* track usually
    means the offset is wrong, not that the disc is damaged.
  * **v1** — the whole-track checksum, v1-era rippers' variant.
  * **v2** — the whole-track checksum, v2-era rippers' variant.

The DB stores ONE checksum per track and it may be either variant, so both are
computed locally and tested against it; matching either verifies the track.

**Offset.** ``accudisc read`` returns *raw* PCM — ``apply_offset`` has not run. The
drive read offset must therefore be supplied here (PX-716A: 30). At the wrong
offset every track misses, which looks exactly like a ruined rip; the header line
prints the offset used so a table is never read without it.

Usage:
    uv run python tools/ar_check.py --device /dev/sr0 /var/tmp/disc*.pcm
    uv run python tools/ar_check.py --toc 0:12032:…:162892 --read-offset 30 a.pcm b.pcm

Exit codes: 0 every track of every file verified; 1 something did not; 2 the disc
is not in the AccurateRip database (nothing was tested).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from disc_geom import (
    Geometry,
    add_geometry_args,
    check_size,
    resolve_geometry,
)

from cdda2img.accuraterip import ARTrackResult, verify_rip

_PLEXTOR_716A_READ_OFFSET = 30


def _conf(c: int | None) -> str:
    return f"{c:>3}" if c is not None else "  ·"


def _verdict(r: ARTrackResult) -> str:
    """A track verifies iff either full-CRC variant matched. crc450 alone never
    verifies — it grades a failure, it does not overturn one."""
    if r.confidence_v1 is not None or r.confidence_v2 is not None:
        return "OK"
    if r.confidence_450 is not None:
        return "DAMAGED"
    return "MISS"


def report(path: Path, results: list[ARTrackResult]) -> tuple[int, int]:
    """Print one file's per-track table. Returns (verified, total)."""
    print(f"\n=== {path.name}")
    if not results or results[0].max_confidence is None:
        print("  disc not in the AccurateRip database")
        return 0, len(results)

    print("  trk  450   v1    v2   maxconf  v1 crc    v2 crc    verdict")
    ok = 0
    for r in results:
        v = _verdict(r)
        ok += v == "OK"
        print(
            f"  {r.track:3d}  {_conf(r.confidence_450)}  {_conf(r.confidence_v1)}  "
            f"{_conf(r.confidence_v2)}   {r.max_confidence:>5}   "
            f"{r.v1_crc}  {r.v2_crc}  {v}"
        )
    n = len(results)
    dmg = sum(1 for r in results if _verdict(r) == "DAMAGED")
    tail = f" ({dmg} damaged)" if dmg else ""
    print(f"  → {ok}/{n} verified{tail}")
    return ok, n


def run(geom: Geometry, pcms: list[Path], read_offset: int) -> int:
    print(f"# disc: {geom.describe()}")
    print(f"# read_offset={read_offset} samples (raw PCM needs the drive's offset)")

    in_db = False
    summary: list[tuple[str, int, int]] = []
    for path in pcms:
        check_size(geom, path)
        # verify_rip refetches the dBAR per call. Deliberate: its boundary
        # windowing and zero-padding are the delicate part, and reimplementing
        # them here to save four HTTPS GETs would be trading correctness for
        # politeness.
        res = verify_rip(path, geom.lsns, geom.disc_last_lsn, read_offset, geom.cddb_id)
        in_db |= bool(res.tracks and res.tracks[0].max_confidence is not None)
        ok, n = report(path, res.tracks)
        summary.append((path.name, ok, n))

    if not in_db:
        print("\nDisc is not in the AccurateRip database — nothing was tested.")
        return 2

    if len(summary) > 1:
        print("\n=== summary")
        for name, ok, n in summary:
            print(f"  {name:<20} {ok}/{n}")
    return 0 if all(ok == n for _n, ok, n in summary) else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_geometry_args(ap)
    ap.add_argument(
        "--read-offset",
        type=int,
        default=_PLEXTOR_716A_READ_OFFSET,
        help=(
            "drive read offset in stereo samples; use 0 for already-corrected PCM "
            f"(default {_PLEXTOR_716A_READ_OFFSET}, the PX-716A)"
        ),
    )
    ap.add_argument("pcm", nargs="+", type=Path, help="raw whole-disc s16le PCM")
    args = ap.parse_args()

    missing = [p for p in args.pcm if not p.is_file()]
    if missing:
        msg = "no such file: " + ", ".join(str(p) for p in missing)
        raise SystemExit(msg)

    return run(resolve_geometry(args), args.pcm, args.read_offset)


if __name__ == "__main__":
    raise SystemExit(main())
