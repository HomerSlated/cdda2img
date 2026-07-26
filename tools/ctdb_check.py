#!/usr/bin/env python3
"""CTDB per-track checksum check for raw whole-disc PCM captures.

CTDB (db.cuetools.net) publishes, per submitted rip of a TOC, a plain CRC32 per
track plus Reed-Solomon parity. This tool does the *checksum* half only — lookup,
entry selection, per-track CRC comparison. It never fetches parity and never
writes. For the repair half, see ``tools/ctdb_repair.py``.

**Two offsets, never interchangeable.** CTDB's is a *consensus* offset between our
PCM and the submitters' — recovered here by sweeping one track's CRC over ±700
stereo samples. AccurateRip's is the *drive read* offset. They are different
numbers in different reference frames; passing one where the other belongs
produces a full table of clean-looking misses. This tool only ever uses the CTDB
one, and prints it.

**Image domain.** CTDB's CRCs cover ``[bounds[0], bounds[-1])`` — first-track
INDEX 01 to lead-out — not our ``[0, lead-out)`` capture. ``track_crc_at`` owns
that conversion (including the ``laststride`` derivation that must come from the
image, not from ``len(pcm)``); this tool must not second-guess it.

Usage:
    uv run python tools/ctdb_check.py --device /dev/sr0 /var/tmp/disc*.pcm
    uv run python tools/ctdb_check.py --toc 0:…:162892 --xml /var/tmp/ctdb.xml a.pcm

Exit codes: 0 every track of every file matched; 1 something did not; 2 the disc
is not in CTDB, or no entry reconciles with any of the given rips.
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

from cdda2img.ctdb_repair import (
    Entry,
    Selection,
    load_entries,
    select_entry,
    track_crc_at,
)


def _select(
    pcm: bytes,
    entries: list[Entry],
    geom: Geometry,
    forced: tuple[str | None, int | None],
) -> Selection | None:
    """Pick the (entry, offset) pair to grade against, honouring any override.

    An override is for experiments — grading file B against the entry file A
    selected, say. It is deliberately not validated: forcing an offset that does
    not reconcile is a legitimate thing to want to *see*."""
    want_id, want_off = forced
    if want_id is None and want_off is None:
        return select_entry(pcm, entries, geom.bounds, geom.n_tracks)

    entry = entries[0]
    if want_id is not None:
        match = [e for e in entries if e.id == want_id]
        if not match:
            msg = f"--entry {want_id} not among the fetched entries"
            raise SystemExit(msg)
        entry = match[0]
    if want_off is None:
        sel = select_entry(pcm, [entry], geom.bounds, geom.n_tracks)
        if sel is None:
            return None
        want_off = sel.offset
    return Selection(entry, want_off)


def report(path: Path, pcm: bytes, sel: Selection, geom: Geometry) -> tuple[int, int]:
    """Print one file's per-track CRC table. Returns (matched, total)."""
    e = sel.entry
    print(f"\n=== {path.name}")
    print(
        f"  entry {e.id}  conf={e.confidence} npar={e.npar} stride={e.stride}  "
        f"ctdb_offset={sel.offset:+d} samples"
    )
    print("  trk  ours      theirs    verdict")
    ok = 0
    for t in range(1, geom.n_tracks + 1):
        crc = track_crc_at(pcm, t, sel.offset, e.stride, geom.bounds, geom.n_tracks)
        theirs = e.trackcrcs[t - 1]
        if crc is None:
            verdict, mine = "ABSTAIN", "        "
        elif crc == theirs:
            verdict, mine = "OK", f"{crc:08x}"
            ok += 1
        else:
            verdict, mine = "MISS", f"{crc:08x}"
        print(f"  {t:3d}  {mine}  {theirs:08x}  {verdict}")
    n = geom.n_tracks
    print(f"  → {ok}/{n} matched")
    return ok, n


def run(geom: Geometry, pcms: list[Path], args: argparse.Namespace) -> int:
    print(f"# disc: {geom.describe()}")

    entries = load_entries(geom.bounds, geom.n_tracks, xml_cache=args.xml)
    if not entries:
        print("Disc is not in CTDB (no parity-bearing entries) — nothing was tested.")
        return 2
    print(f"# {len(entries)} parity-bearing CTDB entries:")
    for e in entries:
        print(f"#   {e.id}  conf={e.confidence} npar={e.npar} stride={e.stride}")

    summary: list[tuple[str, int, int]] = []
    any_selected = False
    for path in pcms:
        check_size(geom, path)
        pcm = path.read_bytes()
        sel = _select(pcm, entries, geom, (args.entry, args.offset))
        if sel is None:
            print(f"\n=== {path.name}\n  no CTDB entry reconciles with this rip")
            summary.append((path.name, 0, geom.n_tracks))
            continue
        any_selected = True
        ok, n = report(path, pcm, sel, geom)
        summary.append((path.name, ok, n))

    if not any_selected:
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
        "--xml",
        type=Path,
        default=None,
        help="cache file for the CTDB lookup XML (one fetch across all inputs)",
    )
    ap.add_argument("--entry", help="force a CTDB entry id instead of selecting one")
    ap.add_argument(
        "--offset",
        type=int,
        default=None,
        help="force the CTDB consensus offset (stereo samples) instead of sweeping",
    )
    ap.add_argument("pcm", nargs="+", type=Path, help="raw whole-disc s16le PCM")
    args = ap.parse_args()

    missing = [p for p in args.pcm if not p.is_file()]
    if missing:
        msg = "no such file: " + ", ".join(str(p) for p in missing)
        raise SystemExit(msg)

    return run(resolve_geometry(args), args.pcm, args)


if __name__ == "__main__":
    raise SystemExit(main())
