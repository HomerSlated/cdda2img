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
The distinction this tool *can* observe from the ``.toc`` is the CD-Text copy
(UPC_EAN / CD-Text ISRC) versus the Q-channel original (CATALOG / track ISRC) —
i.e. "is the MCN also duplicated in CD-Text?". True lead-in-vs-program Q
granularity is available via ``--deep``: pass a redumper ``.subcode`` capture
and :mod:`cdda2img.subchannel` decodes the raw Q-channel directly, attributing
each MCN/ISRC to the lead-in or a specific program track (see below).

cdrdao writes an all-zeros value for an absent MCN/ISRC (e.g.
``CATALOG "0000000000000"``); such values are treated as ABSENT and not recorded,
otherwise every no-MCN disc would inflate the "MCN present" statistic.

Usage (from project root):
    uv run python tools/disc_scan.py --device /dev/sr0
    uv run python tools/disc_scan.py --toc /path/to/disc.toc   # reuse a capture
    uv run python tools/disc_scan.py --device /dev/sr0 --db rips/disc_scan.db
    uv run python tools/disc_scan.py --deep /path/to/dump.subcode  # pre-captured
    uv run python tools/disc_scan.py --toc disc.toc --deep dump.subcode  # both
    # Full auto pipeline: cdrdao TOC + redumper subchannel rip to a tempdir,
    # scan, update db, print, then discard the rip (needs redumper; ~minutes):
    REDUMPER=path/to/redumper uv run python tools/disc_scan.py --device /dev/sr0 --deep
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
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
from cdda2img.subchannel import SubcodeScan, parse_fulltoc_leadout, scan_subcode
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
# Raw subchannel (--deep): true lead-in vs program-area Q provenance
# ---------------------------------------------------------------------------


# Sentinel: ``--deep`` given with no path means "rip the disc now" (needs --device).
_DEEP_AUTO = "@redumper-auto"

# Fixed redumper output prefix. Using an explicit name on BOTH `dump` and
# `dump::extra` means the second command augments the first's files without
# having to parse redumper's auto-generated ``dump_<ts>_<drive>`` prefix.
_REDUMPER_IMAGE_NAME = "scan"


def resolve_redumper(explicit: str | None) -> str | None:
    """Locate the redumper binary: --redumper, then $REDUMPER, then PATH.

    The binary is a separate, often locally-built tool — never hardcode a path
    into this tracked script.
    """
    return explicit or os.environ.get("REDUMPER") or shutil.which("redumper")


def redumper_dump_argv(redumper: str, image_path: str, device: str | None) -> list[str]:
    """argv for the primary subchannel dump (writes ``<name>.subcode``/.fulltoc)."""
    argv = [
        redumper,
        "dump",
        f"--image-path={image_path}",
        f"--image-name={_REDUMPER_IMAGE_NAME}",
        "--retries=5",
    ]
    if device:
        argv.append(f"--drive={device}")
    return argv


def redumper_extra_argv(
    redumper: str, image_path: str, device: str | None
) -> list[str]:
    """argv for the lead-in/lead-out pass that augments the dump (Plextor only)."""
    argv = [
        redumper,
        "dump::extra",
        f"--image-path={image_path}",
        f"--image-name={_REDUMPER_IMAGE_NAME}",
    ]
    if device:
        argv.append(f"--drive={device}")
    return argv


def run_redumper(redumper: str, image_path: str, device: str | None) -> Path:
    """Rip the subchannel into *image_path*; return the ``.subcode`` path.

    Runs ``dump`` (mandatory) then ``dump::extra`` (best-effort — the lead-in
    capture only works on Plextor drives; its failure leaves the program-area
    Q intact). redumper's progress is streamed to the terminal. Raises
    SystemExit if the dump fails or produces no subcode.
    """
    dump_cmd = redumper_dump_argv(redumper, image_path, device)
    print(f"Ripping subchannel (this takes several minutes):\n  {' '.join(dump_cmd)}")
    if subprocess.run(dump_cmd).returncode != 0:  # noqa: S603
        msg = "redumper dump failed"
        raise SystemExit(msg)

    extra_cmd = redumper_extra_argv(redumper, image_path, device)
    print(f"Capturing lead-in (best-effort):\n  {' '.join(extra_cmd)}")
    if subprocess.run(extra_cmd).returncode != 0:  # noqa: S603
        print(
            "  lead-in capture unavailable on this drive; program-area Q only",
            file=sys.stderr,
        )

    subcode = Path(image_path) / f"{_REDUMPER_IMAGE_NAME}.subcode"
    if not subcode.exists():
        msg = f"redumper produced no subcode at {subcode}"
        raise SystemExit(msg)
    return subcode


def deep_scan(subcode_path: Path) -> tuple[SubcodeScan, int | None]:
    """Decode a redumper ``.subcode``; lead-out from a sibling ``.fulltoc``.

    The lead-out LBA bounds the program-area invalid-Q count; region
    attribution does not need it. Returns (scan, leadout_lba).
    """
    data = subcode_path.read_bytes()
    fulltoc = subcode_path.with_suffix(".fulltoc")
    leadout = parse_fulltoc_leadout(fulltoc.read_bytes()) if fulltoc.exists() else None
    return scan_subcode(data, leadout_lba=leadout), leadout


def deep_points(scan: SubcodeScan) -> list[DataPoint]:
    """Canonical (stable-location) data points for the DB and cross-disc stats.

    Locations are deliberately free of per-disc frame counts / LBA spans so the
    stats table can aggregate "how many discs carry MCN in program-area Q". The
    quantitative detail is shown separately by :func:`_print_deep`.
    """
    pts: list[DataPoint] = []
    for d in scan.data:
        if d.type == "MCN":
            region = "lead-in" if d.region == "lead-in" else "program"
            pts.append(DataPoint("MCN", None, d.value or "", f"Q Mode-2 ({region})"))
        elif d.type == "ISRC":
            tno = int(d.region.split()[-1]) if d.region.startswith("track") else None
            value = d.value or "(undecoded)"
            pts.append(DataPoint("ISRC", tno, value, "Q Mode-3 (program track)"))
    return pts


def _print_deep(scan: SubcodeScan, leadout: int | None) -> None:
    """Rich per-disc view of the raw Q-channel provenance."""
    print()
    print("  Raw subchannel (Q) provenance — redumper .subcode")
    base = "unanchored" if scan.base_lba is None else str(scan.base_lba)
    summary = (
        f"  base LBA {base} (anchor agreement {scan.base_agreement:.1%}); "
        f"valid Q {scan.valid_q}, invalid Q {scan.invalid_q}"
    )
    if scan.program_invalid_q is not None:
        summary += f" (program-area {scan.program_invalid_q}, lead-out {leadout})"
    print(summary)
    print(f"  {'Type':<5} {'Region':<10} {'Frames':>6}  {'Value':<14}  LBA span")
    print(f"  {'─' * 5} {'─' * 10} {'─' * 6}  {'─' * 14}  {'─' * 20}")
    for d in scan.data:
        span = f"[{d.lba_min}..{d.lba_max}]" if d.lba_min is not None else "—"
        print(f"  {d.type:<5} {d.region:<10} {d.count:>6}  {d.value or '':<14}  {span}")


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


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--device", default=None, help="optical device, e.g. /dev/sr0")
    p.add_argument("--toc", type=Path, default=None, help="reuse a captured .toc")
    p.add_argument(
        "--deep",
        nargs="?",
        const=_DEEP_AUTO,
        default=None,
        metavar="SUBCODE",
        help="raw Q-channel provenance. With a path: use a pre-captured redumper "
        ".subcode (sibling .fulltoc supplies the lead-out). With NO path (and "
        "--device): rip the disc via redumper into a tempdir, scan, then discard "
        "the rip. Composes with --toc/--device or runs standalone.",
    )
    p.add_argument(
        "--redumper",
        default=None,
        help="path to the redumper binary for --deep auto-rip "
        "(default: $REDUMPER, then PATH)",
    )
    p.add_argument("--db", type=Path, default=_DEFAULT_DB, help="sqlite db path")
    return p


def main(argv: list[str] | None = None) -> int:
    p = _build_parser()
    args = p.parse_args(argv)

    if args.device is None and args.toc is None and args.deep is None:
        p.error("provide --toc/--device and/or --deep")

    points: list[DataPoint] = []
    disc_key: str | None = None
    cddb_id = ""
    n_tracks = 0
    label = "(untitled)"
    scan: SubcodeScan | None = None
    leadout: int | None = None

    if args.device is not None or args.toc is not None:
        toc_bytes = obtain_toc(args.device, args.toc)
        text = toc_bytes.decode("latin-1", "replace")
        points += scan_toc_text(text)
        derived, disc_key, cddb_id, n_tracks = _derived_points(toc_bytes)
        points += derived
        label = next(
            (pt.raw_value for pt in points if pt.type == "Album title"), "(untitled)"
        )

    if args.deep is not None:
        if args.deep == _DEEP_AUTO:
            if args.device is None:
                p.error("--deep with no path requires --device (to rip the disc)")
            redumper = resolve_redumper(args.redumper)
            if redumper is None:
                msg = (
                    "redumper not found — pass --redumper PATH, set $REDUMPER, "
                    "or add redumper to PATH"
                )
                raise SystemExit(msg)
            deep_stem = "auto"
            # deep_scan reads the bytes immediately, so the multi-hundred-MB
            # redumper byproducts (.scram/.state) are gone by the time we print.
            with tempfile.TemporaryDirectory(prefix="disc_scan_deep_") as tmp:
                scan, leadout = deep_scan(run_redumper(redumper, tmp, args.device))
        else:
            deep_stem = Path(args.deep).stem
            scan, leadout = deep_scan(Path(args.deep))
        points += deep_points(scan)
        if disc_key is None:  # deep-only run: key on the MCN, else the file stem
            mcn = next(
                (d.value for d in scan.data if d.type == "MCN" and d.value), None
            )
            disc_key = f"mcn:{mcn}" if mcn else f"deep:{deep_stem}"
            label = f"(deep-only {deep_stem})"

    if disc_key is None:  # unreachable: the arg check above guarantees a source
        msg = "no disc identity resolved"
        raise RuntimeError(msg)
    conn = _open_db(args.db)
    try:
        _store(conn, disc_key, cddb_id, n_tracks, label, points)
        print(f"\n  Disc: {label}   (disc key {disc_key})")
        _print_disc_table(points)
        if scan is not None:
            _print_deep(scan, leadout)
        _print_stats(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
