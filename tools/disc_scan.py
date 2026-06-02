#!/usr/bin/env python3
"""
disc_scan.py — enumerate the identifying data a CD-DA disc actually carries.

Reads a disc via ``cdrdao read-toc --fast-toc`` (or reuses a pre-captured
``.toc``), extracts every identifying datum with its RAW value and storage
location, records the disc into ``rips/disc_scan.db``, then prints:

  1. this disc's data table — Type / Track / Raw value / Location
  2. a cross-disc stats table — Type / Location / number of discs seen

The goal is empirical: scan a handful of commercial pressings and see which data
types each one actually provides, and where, so we can decide the authoritative
source of truth for each field (e.g. is MCN reliably 13 digits? is it duplicated
in CD-Text?). Values are shown RAW — no normalisation — so MCN length, padding,
and check-digit anomalies are visible.

Locations distinguished from the cdrdao ``.toc``:

  - top-level ``CATALOG "..."``      -> Q-channel MCN, as cdrdao decoded it
  - per-track ``ISRC "..."``         -> Q-channel ISRC, as cdrdao decoded it
  - inside a ``CD_TEXT { }`` block:
      ``TITLE``    -> CD-Text (0x80)
      ``PERFORMER``-> CD-Text (0x81)
      ``UPC_EAN``  -> CD-Text (0x8E)  (the CD-Text copy of the MCN)
      ``ISRC``     -> CD-Text (0x8E)  (the CD-Text copy of an ISRC)
  - track count                      -> TOC
  - CDDB / MusicBrainz disc IDs       -> TOC checksum (derived, not stored)

"Location" is an *attribution*, not a raw observation: cdrdao's ``.toc``
collapses subchannel provenance, so the ``CATALOG`` line cannot distinguish a
lead-in Q-channel MCN from a program-area one — both surface as one CATALOG.
The distinction this tool *can* observe is the CD-Text copy (UPC_EAN / CD-Text
ISRC) versus the Q-channel original (CATALOG / track ISRC) — i.e. "is the MCN
also duplicated in CD-Text?". True lead-in-vs-program Q granularity would need a
full ``read-cd --read-subchan`` rip and is out of scope.

cdrdao writes an all-zeros value for an absent MCN/ISRC (e.g.
``CATALOG "0000000000000"``); such values are treated as ABSENT and not recorded,
otherwise every no-MCN disc would inflate the "MCN present" statistic.

Usage (from project root):
    uv run python tools/disc_scan.py --device /dev/sr0
    uv run python tools/disc_scan.py --toc /path/to/disc.toc   # reuse a capture
    uv run python tools/disc_scan.py --device /dev/sr0 --db rips/disc_scan.db
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cdda2img.cddb import compute_cddb_disc_id
from cdda2img.cdrdao_reader import parsed_to_rbi_disc
from cdda2img.mb_lookup import disc_id_from_rbi
from cdda2img.toc_parser import parse_toc

_DEFAULT_DB = Path("rips/disc_scan.db")

# A quoted cdrdao string value, with backslash escapes.
_STR = r'"((?:[^"\\]|\\.)*)"'


@dataclass(frozen=True)
class DataPoint:
    """One identifying datum found on the disc."""

    type: str
    track_no: int | None  # None for disc-level data
    raw_value: str
    location: str


def _is_absent(val: str) -> bool:
    """True if *val* is cdrdao's "no value" marker (empty or all zeros)."""
    v = val.strip()
    return not v or set(v) <= {"0"}


def _add_cdtext_point(
    points: list[DataPoint], field: str, val: str, track_no: int
) -> None:
    """Append the CD-Text datum for one TITLE/PERFORMER/UPC_EAN/ISRC line."""
    val = val.strip()
    disc_level = track_no == 0
    if field == "TITLE" and val:
        kind = "Album title" if disc_level else "Track title"
        points.append(
            DataPoint(kind, None if disc_level else track_no, val, "CD-Text (0x80)")
        )
    elif field == "PERFORMER" and val:
        kind = "Album artist" if disc_level else "Performer"
        points.append(
            DataPoint(kind, None if disc_level else track_no, val, "CD-Text (0x81)")
        )
    elif field == "UPC_EAN" and not _is_absent(val):
        points.append(DataPoint("MCN", None, val, "CD-Text (0x8E)"))
    elif field == "ISRC" and not _is_absent(val):
        points.append(
            DataPoint("ISRC", None if disc_level else track_no, val, "CD-Text (0x8E)")
        )


def scan_toc_text(text: str) -> list[DataPoint]:
    """Extract raw (type, track, value, location) data points from .toc text.

    Stateful single pass: tracks the current track number (0 = disc-level,
    before the first TRACK) and whether we are inside a ``CD_TEXT { }`` block,
    counting braces so nested ``LANGUAGE_MAP { }`` / ``LANGUAGE n { }`` don't
    end the block early.
    """
    points: list[DataPoint] = []
    track_no = 0
    in_cdtext = False
    depth = 0
    cdtext_re = re.compile(rf"(TITLE|PERFORMER|UPC_EAN|ISRC)\s+{_STR}")
    catalog_re = re.compile(rf"CATALOG\s+{_STR}")
    isrc_re = re.compile(rf"ISRC\s+{_STR}")

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if in_cdtext:
            depth += line.count("{") - line.count("}")
            m = cdtext_re.match(line)
            if m:
                _add_cdtext_point(points, m.group(1), m.group(2), track_no)
            if depth <= 0:
                in_cdtext = False
            continue
        if line.startswith("TRACK "):
            track_no += 1
            continue
        if line.startswith("CD_TEXT"):
            in_cdtext = True
            depth = line.count("{") - line.count("}")
            continue
        m = catalog_re.match(line)
        if m:
            if not _is_absent(m.group(1)):
                points.append(DataPoint("MCN", None, m.group(1), "Q-channel (CATALOG)"))
            continue
        m = isrc_re.match(line)
        if m and track_no > 0 and not _is_absent(m.group(1)):
            points.append(
                DataPoint("ISRC", track_no, m.group(1), "Q-channel (track ISRC)")
            )

    return points


def _derived_points(toc_bytes: bytes) -> tuple[list[DataPoint], str, str, int]:
    """Compute TOC-derived data points (disc IDs, track count) + the keys.

    Returns (points, disc_key, cddb_disc_id, n_tracks). The disc key is the MB
    disc ID when available (SHA-1; unlike the 32-bit CDDB id it does not
    collide), falling back to the CDDB id if the MB id cannot be computed.
    """
    parsed = parse_toc(toc_bytes)
    disc = parsed_to_rbi_disc(parsed)
    track_lsns = [pt.start_frame + pt.pregap_frames for pt in parsed.tracks]
    last = parsed.tracks[-1]
    disc_last_lsn = last.start_frame + last.pregap_frames + last.duration_frames - 1
    cddb_id = compute_cddb_disc_id(track_lsns, disc_last_lsn)
    mb_id = disc_id_from_rbi(disc)
    n = len(parsed.tracks)
    points = [
        DataPoint("Track count", None, str(n), "TOC"),
        DataPoint("CDDB Disc ID", None, cddb_id, "TOC checksum"),
    ]
    if mb_id:
        points.append(DataPoint("MB Disc ID", None, mb_id, "TOC checksum"))
    if parsed.pre_emphasis is not None:
        points.append(
            DataPoint(
                "Pre-emphasis", None, "yes" if parsed.pre_emphasis else "no", "TOC"
            )
        )
    return points, mb_id or cddb_id, cddb_id, n


def obtain_toc(device: str | None, toc_path: Path | None) -> bytes:
    """Return cdrdao .toc bytes — from *toc_path* if given, else read the disc."""
    if toc_path is not None:
        print(f"Using pre-captured TOC: {toc_path}")
        return toc_path.read_bytes()
    if device is None:
        msg = "either --toc or --device is required"
        raise SystemExit(msg)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "scan.toc"
        cmd = ["cdrdao", "read-toc", "--fast-toc", "--device", device, str(out)]
        print(f"Reading disc TOC: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
        if result.returncode != 0 or not out.exists():
            sys.stderr.write(result.stderr)
            msg = f"cdrdao read-toc failed (exit {result.returncode})"
            raise SystemExit(msg)
        return out.read_bytes()


# ---------------------------------------------------------------------------
# SQLite persistence
# ---------------------------------------------------------------------------


def _open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS discs ("
        " disc_key TEXT PRIMARY KEY, cddb_disc_id TEXT, n_tracks INTEGER,"
        " scanned_at TEXT, label TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS data_points ("
        " disc_key TEXT, type TEXT, track_no INTEGER, raw_value TEXT, location TEXT)"
    )
    conn.commit()
    return conn


def _store(
    conn: sqlite3.Connection,
    disc_key: str,
    cddb_id: str,
    n_tracks: int,
    label: str,
    points: list[DataPoint],
) -> None:
    """Upsert this disc — replace any prior rows for the same disc_key."""
    conn.execute("DELETE FROM data_points WHERE disc_key = ?", (disc_key,))
    conn.execute("DELETE FROM discs WHERE disc_key = ?", (disc_key,))
    conn.execute(
        "INSERT INTO discs VALUES (?, ?, ?, ?, ?)",
        (disc_key, cddb_id, n_tracks, datetime.now(timezone.utc).isoformat(), label),
    )
    conn.executemany(
        "INSERT INTO data_points VALUES (?, ?, ?, ?, ?)",
        [(disc_key, p.type, p.track_no, p.raw_value, p.location) for p in points],
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _sort_key(p: DataPoint) -> tuple[int, str]:
    """Disc-level rows first, then by track number; stable within a track."""
    return (p.track_no or 0, p.type)


def _print_disc_table(points: list[DataPoint]) -> None:
    print()
    print(f"  {'Type':<14} {'Trk':>3}  {'Raw value':<30}  Location")
    print(f"  {'─' * 14} {'─' * 3}  {'─' * 30}  {'─' * 24}")
    for p in sorted(points, key=_sort_key):
        trk = "" if p.track_no is None else str(p.track_no)
        print(f"  {p.type:<14} {trk:>3}  {p.raw_value:<30}  {p.location}")


def _print_stats(conn: sqlite3.Connection) -> None:
    n_discs = conn.execute("SELECT COUNT(*) FROM discs").fetchone()[0]
    rows = conn.execute(
        "SELECT type, location, COUNT(DISTINCT disc_key) FROM data_points"
        " GROUP BY type, location ORDER BY type, location"
    ).fetchall()
    print()
    print(f"  Cross-disc stats — {n_discs} disc(s) in the DB")
    print(f"  {'Type':<14}  {'Location':<24}  #discs")
    print(f"  {'─' * 14}  {'─' * 24}  {'─' * 6}")
    for typ, loc, count in rows:
        print(f"  {typ:<14}  {loc:<24}  {count:>6}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--device", default=None, help="optical device, e.g. /dev/sr0")
    p.add_argument("--toc", type=Path, default=None, help="reuse a captured .toc")
    p.add_argument("--db", type=Path, default=_DEFAULT_DB, help="sqlite db path")
    args = p.parse_args(argv)

    toc_bytes = obtain_toc(args.device, args.toc)
    text = toc_bytes.decode("latin-1", "replace")

    points = scan_toc_text(text)
    derived, disc_key, cddb_id, n_tracks = _derived_points(toc_bytes)
    points += derived

    label = next((p.raw_value for p in points if p.type == "Album title"), "(untitled)")

    conn = _open_db(args.db)
    try:
        _store(conn, disc_key, cddb_id, n_tracks, label, points)
        print(f"\n  Disc: {label}   (disc key {disc_key})")
        _print_disc_table(points)
        _print_stats(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
