"""
catalogue.py — disc catalogue SQLite database.

One row per registered RBI file in catalogue; one row per track in catalogue_tracks.
Schema version 4.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_SCHEMA_VERSION = "4"
_APP_NAME = "cdda2img"


def catalogue_db_path() -> Path:
    """Return the default catalogue database path under $XDG_DATA_HOME."""
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg) / "cdda2img" / "cdda2img.db"


_DDL = """\
CREATE TABLE IF NOT EXISTS db_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS catalogue (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    mcn              TEXT,
    album            TEXT NOT NULL,
    artist           TEXT NOT NULL,
    year             INTEGER,
    disc_number      INTEGER NOT NULL DEFAULT 1,
    disc_total       INTEGER NOT NULL DEFAULT 1,
    track_count      INTEGER NOT NULL,
    rg_album_gain    REAL,
    rg_album_peak    REAL,
    rg_album_range   REAL,
    file_basename    TEXT NOT NULL,
    file_path        TEXT NOT NULL,
    file_size        INTEGER NOT NULL,
    registered_at    TEXT NOT NULL,
    created_by       TEXT NOT NULL,
    mode             TEXT NOT NULL,
    source           TEXT,
    ripper           TEXT,
    drive            TEXT,
    -- Release intelligence (see docs/reference/rbi_spec.md §6.3.2):
    --   low_dynamic_range:        derived from EBU R128 album LRA vs
    --                             Config.low_dr_threshold. NULL = not
    --                             measured; 0 = no; 1 = yes.
    --   original_release_*:       MB release-group lookup result (the
    --                             auto-detected trio + user override).
    --                             original_release_found is the boolean
    --                             gate; the trio is rendered via the shared
    --                             rbi_format.format_original_fields core
    --                             ("Original: Yes/No/Unknown, …") — see
    --                             docs/reference/rbi_spec.md §6.3.2.
    low_dynamic_range          INTEGER,
    original_release_found     INTEGER NOT NULL DEFAULT 0,
    original_release_title     TEXT,
    original_release_year      INTEGER,
    b3sum                      TEXT
);

CREATE TABLE IF NOT EXISTS catalogue_tracks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    catalogue_id    INTEGER NOT NULL REFERENCES catalogue(id) ON DELETE CASCADE,
    track_number    INTEGER NOT NULL,
    title           TEXT NOT NULL,
    duration_frames INTEGER NOT NULL,
    rg_track_gain   REAL,
    rg_track_peak   REAL,
    rg_track_range  REAL,
    ar_v1_crc       TEXT,
    ar_v2_crc       TEXT,
    ar_status       TEXT,
    ar_confidence   INTEGER,
    UNIQUE (catalogue_id, track_number)
);
"""


def open_catalogue_db(path: Path | None = None) -> sqlite3.Connection:
    """Open or create the catalogue database at *path* (default: XDG data dir)."""
    db_path = path or catalogue_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not db_path.exists()

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_DDL)

    if is_new:
        _init_meta(conn)
    else:
        _check_schema_version(conn)

    return conn


def _init_meta(conn: sqlite3.Connection) -> None:
    """Populate db_meta on first DB creation; prompt for owner name on TTY."""
    owner = ""
    if sys.stdin.isatty():
        try:
            owner = input(
                "  Catalogue owner/label (optional, press Enter to skip): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT OR IGNORE INTO db_meta (key, value) VALUES (?, ?)",
        [
            ("app_name", _APP_NAME),
            ("owner_name", owner),
            ("created_at", now),
            ("updated_at", now),
            ("schema_version", _SCHEMA_VERSION),
        ],
    )
    conn.commit()


def _compute_b3sum(path: Path) -> str:
    """Return the blake3 hex digest of *path*, read in streaming 64 KB chunks."""
    import blake3 as _blake3

    h = _blake3.blake3()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE catalogue ADD COLUMN b3sum TEXT")
    conn.execute(
        "INSERT OR REPLACE INTO db_meta (key, value) VALUES ('schema_version', '4')"
    )
    conn.commit()
    log.info("Catalogue schema migrated v3 → v4 (added b3sum column)")


def _check_schema_version(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT value FROM db_meta WHERE key='schema_version'"
    ).fetchone()
    if row is None:
        return
    db_ver = row[0]
    if db_ver == _SCHEMA_VERSION:
        return
    if db_ver > _SCHEMA_VERSION:
        log.warning(
            "Catalogue schema v%s is newer than supported v%s; "
            "all rips will skip registration until cdda2img is upgraded",
            db_ver,
            _SCHEMA_VERSION,
        )
        msg = f"catalogue schema v{db_ver} > supported v{_SCHEMA_VERSION}"
        raise RuntimeError(msg)
    if db_ver == "3":
        _migrate_v3_to_v4(conn)
        return
    msg = (
        f"catalogue schema v{db_ver} predates current v{_SCHEMA_VERSION}; "
        f"delete the catalogue and re-scan the archive: rm {catalogue_db_path()}"
    )
    raise RuntimeError(msg)


def _parse_prov(prov_bytes: bytes) -> dict[str, str]:
    """Parse PROV block bytes into a key→value dict (one pair per line)."""
    try:
        text = prov_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    result: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key] = value
    return result


def _parse_year(date_str: str | None) -> int | None:
    """Extract a 4-digit year from the leading digits of a date string."""
    if not date_str:
        return None
    m = re.match(r"^(\d{4})", date_str)
    return int(m.group(1)) if m else None


def _fmt_datetime(s: str) -> str:
    """Reformat an ISO 8601 datetime string as RFC 5322 for human display."""
    try:
        dt = datetime.fromisoformat(s)
        return dt.strftime("%a, %d %b %Y %H:%M:%S %z").strip()
    except (ValueError, TypeError):
        return s


def _ar_status_str(status: int) -> str | None:
    """Map an ARIP_STATUS_* constant to a catalogue status string."""
    from cdda2img.rbi_format import ARIP_STATUS_MISMATCH, ARIP_STATUS_OK

    if status == ARIP_STATUS_OK:
        return "OK"
    if status == ARIP_STATUS_MISMATCH:
        return "BAD"
    return None  # NOT_IN_DB


def _find_duplicates(
    conn: sqlite3.Connection,
    album: str,
    artist: str,
    year: int | None,
    disc_number: int,
    disc_total: int,
    track_count: int,
    mcn: str | None,
    track_durations: list[int],
    ar_v1_crcs: list[str | None],
) -> list[int]:
    """Return catalogue.id values that match this disc."""
    rows = conn.execute(
        "SELECT id, mcn, year FROM catalogue "
        "WHERE album=? AND artist=? AND disc_number=? AND disc_total=? AND track_count=?",
        (album, artist, disc_number, disc_total, track_count),
    ).fetchall()

    matches: list[int] = []
    for row_id, db_mcn, db_year in rows:
        if year is not None and db_year is not None and year != db_year:
            continue
        if mcn and db_mcn and mcn != db_mcn:
            continue

        db_tracks = conn.execute(
            "SELECT duration_frames, ar_v1_crc FROM catalogue_tracks "
            "WHERE catalogue_id=? ORDER BY track_number",
            (row_id,),
        ).fetchall()
        if len(db_tracks) != len(track_durations):
            continue
        if any(r[0] != d for r, d in zip(db_tracks, track_durations)):
            continue

        db_ar_crcs = [r[1] for r in db_tracks]
        has_new_ar = any(c is not None for c in ar_v1_crcs)
        has_db_ar = any(c is not None for c in db_ar_crcs)
        if has_new_ar and has_db_ar and ar_v1_crcs != db_ar_crcs:
            continue

        matches.append(row_id)

    return matches


def _show_duplicate(conn: sqlite3.Connection, dup_id: int) -> None:
    row = conn.execute(
        "SELECT album, artist, year, disc_number, disc_total, file_path, registered_at "
        "FROM catalogue WHERE id=?",
        (dup_id,),
    ).fetchone()
    if row is None:
        return
    album, artist, year, dn, dt, fpath, reg_at = row
    disc_str = f" disc {dn}/{dt}" if dt > 1 else ""
    year_str = f" ({year})" if year else ""
    print(f"   Existing: {artist} — {album}{year_str}{disc_str}")
    print(f"     File:       {fpath}")
    print(f"     Registered: {_fmt_datetime(reg_at)}")


def _prompt_duplicate_action(conn: sqlite3.Connection, dup_ids: list[int]) -> str:
    """Prompt the user for action on duplicates. Returns 's', 'r', or 'a'."""
    if not sys.stdin.isatty():
        log.info("Catalogue duplicate detected (non-TTY) — skipping registration")
        return "s"
    print(f"\n   Catalogue: {len(dup_ids)} duplicate(s) found:")
    for dup_id in dup_ids:
        _show_duplicate(conn, dup_id)
    while True:
        try:
            answer = (
                input("   [s]kip  [r]eplace  [a]dd anyway  [s]: ").strip().lower()
                or "s"
            )
        except (EOFError, KeyboardInterrupt):
            print()
            return "s"
        if answer in ("s", "r", "a"):
            return answer
        print("  Please enter s, r, or a.")


def _get_catalogue_config() -> tuple[bool, Path | None, str]:
    """Return (enable_catalogue, catalogue_path, duplicate_policy) from user config."""
    with contextlib.suppress(Exception):
        from cdda2img.config import load_config

        cfg = load_config()
        return cfg.enable_catalogue, cfg.catalogue_path, cfg.duplicate_catalogue_entry
    return True, None, "ask"


def register_rbi(
    rbi_path: Path,
    catalogue_path: Path | None = None,
    duplicate_policy: str | None = None,
) -> None:
    """Register *rbi_path* in the disc catalogue.

    Reads all metadata directly from the RBI container. Silently warns on failure.
    When *catalogue_path* is None, reads enable_catalogue and catalogue_path from config.

    *duplicate_policy* controls what happens when a matching disc is already in the
    catalogue: ``"skip"`` silently aborts, ``"replace"`` deletes the existing row
    then inserts, ``"add"`` inserts unconditionally.  ``"ask"`` (the default) prompts
    interactively when stdin is a TTY; falls back to ``"skip"`` without one.
    When *duplicate_policy* is None the value is read from config
    (``duplicate_catalogue_entry``, default ``"ask"``).
    """
    resolved_policy = duplicate_policy
    if catalogue_path is None or resolved_policy is None:
        enabled, cfg_path, cfg_policy = _get_catalogue_config()
        if catalogue_path is None:
            if not enabled:
                return
            catalogue_path = cfg_path
        if resolved_policy is None:
            resolved_policy = cfg_policy
    try:
        _register_impl(rbi_path, catalogue_path, resolved_policy or "ask")
    except Exception as exc:
        log.warning("Catalogue registration failed for %s: %s", rbi_path.name, exc)


def _register_impl(  # noqa: C901
    rbi_path: Path, catalogue_path: Path | None, duplicate_policy: str = "ask"
) -> None:
    from cdda2img.accuraterip import unpack_arip_block
    from cdda2img.container import read_header
    from cdda2img.rbi_format import (
        ARIP_STATUS_NOT_IN_DB,
        BLOCK_TYPE_ARIP,
        BLOCK_TYPE_PROV,
        BLOCK_TYPE_RGDB,
        BLOCK_TYPE_TOC,
    )
    from cdda2img.replaygain import unpack_rg_block
    from cdda2img.toc_parser import parse_toc

    header = read_header(rbi_path)
    n_tracks = header.track_count
    file_size = rbi_path.stat().st_size

    # TOC (required)
    toc_entry = header.find_block(BLOCK_TYPE_TOC)
    if toc_entry is None:
        msg = "no TOC block in container"
        raise ValueError(msg)
    with open(rbi_path, "rb") as f:
        f.seek(toc_entry.offset)
        disc = parse_toc(f.read(toc_entry.length))

    # PROV (optional)
    prov: dict[str, str] = {}
    prov_entry = header.find_block(BLOCK_TYPE_PROV)
    if prov_entry is not None:
        with open(rbi_path, "rb") as f:
            f.seek(prov_entry.offset)
            prov = _parse_prov(f.read(prov_entry.length))

    # RGDB (optional)
    rg_data = None
    rg_entry = header.find_block(BLOCK_TYPE_RGDB)
    if rg_entry is not None:
        with open(rbi_path, "rb") as f:
            f.seek(rg_entry.offset)
            rg_raw = f.read(rg_entry.length)
        with contextlib.suppress(Exception):
            rg_data = unpack_rg_block(rg_raw, n_tracks)

    # ARIP (optional)
    arip_data = None
    arip_entry = header.find_block(BLOCK_TYPE_ARIP)
    if arip_entry is not None:
        with open(rbi_path, "rb") as f:
            f.seek(arip_entry.offset)
            arip_raw = f.read(arip_entry.length)
        with contextlib.suppress(Exception):
            arip_data = unpack_arip_block(arip_raw, n_tracks)

    album = disc.title or ""
    artist = disc.performer or ""
    mcn = disc.catalog
    year = _parse_year(prov.get("release_date"))
    low_dr_str = prov.get("low_dynamic_range")
    low_dynamic_range: int | None = (
        1 if low_dr_str == "YES" else 0 if low_dr_str == "NO" else None
    )
    original_release_found = 1 if prov.get("original_release_found") == "YES" else 0
    original_release_title = prov.get("original_release_title")
    original_release_year = _parse_year(prov.get("original_release_year"))
    created_by = prov.get("creator", "")
    mode = prov.get("mode", "?")
    source = prov.get("source")
    ripper = prov.get("ripper")
    drive = prov.get("drive_name")

    b3sum = _compute_b3sum(rbi_path)

    track_durations = [t.duration_frames for t in disc.tracks]
    ar_v1_crcs: list[str | None] = [None] * n_tracks
    if arip_data is not None:
        for i, at in enumerate(arip_data.tracks):
            if at.status != ARIP_STATUS_NOT_IN_DB:
                ar_v1_crcs[i] = f"{at.v1_crc:08x}"

    db_path = catalogue_path or catalogue_db_path()
    with contextlib.suppress(Exception):
        from cdda2img.config import load_config
        from cdda2img.db import ensure_backup

        _cfg = load_config()
        ensure_backup(db_path, _cfg.catalogue_backups, _cfg.catalogue_backup_frequency)

    conn = open_catalogue_db(catalogue_path)
    try:
        dups = _find_duplicates(
            conn,
            album,
            artist,
            year,
            header.disc_number,
            header.disc_total,
            n_tracks,
            mcn,
            track_durations,
            ar_v1_crcs,
        )

        action = "n"
        if dups:
            if duplicate_policy == "ask":
                action = _prompt_duplicate_action(conn, dups)
            elif duplicate_policy == "skip":
                log.info(
                    "Catalogue: duplicate found, skipping registration of %s",
                    rbi_path.name,
                )
                action = "s"
            elif duplicate_policy == "replace":
                log.info(
                    "Catalogue: replacing %d existing entry(s) for %s",
                    len(dups),
                    rbi_path.name,
                )
                action = "r"
            elif duplicate_policy == "add":
                log.info(
                    "Catalogue: adding duplicate entry for %s",
                    rbi_path.name,
                )
                action = "a"
            if action == "s":
                return

        now = datetime.now(timezone.utc).isoformat()

        with conn:
            if action == "r":
                for dup_id in dups:
                    conn.execute("DELETE FROM catalogue WHERE id=?", (dup_id,))

            cur = conn.execute(
                """INSERT INTO catalogue
                   (mcn, album, artist, year, disc_number, disc_total, track_count,
                    rg_album_gain, rg_album_peak, rg_album_range,
                    file_basename, file_path, file_size,
                    registered_at, created_by, mode, source, ripper, drive,
                    low_dynamic_range, original_release_found,
                    original_release_title, original_release_year, b3sum)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    mcn,
                    album,
                    artist,
                    year,
                    header.disc_number,
                    header.disc_total,
                    n_tracks,
                    rg_data.album_gain if rg_data else None,
                    rg_data.album_peak if rg_data else None,
                    rg_data.album_range if rg_data else None,
                    rbi_path.name,
                    str(rbi_path.resolve()),
                    file_size,
                    now,
                    created_by,
                    mode,
                    source,
                    ripper,
                    drive,
                    low_dynamic_range,
                    original_release_found,
                    original_release_title,
                    original_release_year,
                    b3sum,
                ),
            )
            catalogue_id = cur.lastrowid

            for i, track in enumerate(disc.tracks):
                if arip_data is not None and i < len(arip_data.tracks):
                    at = arip_data.tracks[i]
                    if at.status != ARIP_STATUS_NOT_IN_DB:
                        ar_v1 = f"{at.v1_crc:08x}"
                        ar_v2 = f"{at.v2_crc:08x}"
                    else:
                        ar_v1 = ar_v2 = None
                    ar_status = _ar_status_str(at.status)
                    ar_conf = (
                        max(at.v1_confidence, at.v2_confidence) if ar_status else None
                    )
                else:
                    ar_v1: str | None = None
                    ar_v2: str | None = None
                    ar_status: str | None = None
                    ar_conf: int | None = None

                conn.execute(
                    """INSERT INTO catalogue_tracks
                       (catalogue_id, track_number, title, duration_frames,
                        rg_track_gain, rg_track_peak, rg_track_range,
                        ar_v1_crc, ar_v2_crc, ar_status, ar_confidence)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        catalogue_id,
                        track.track_number,
                        track.title or "",
                        track.duration_frames,
                        rg_data.track_gain[i] if rg_data else None,
                        rg_data.track_peak[i] if rg_data else None,
                        rg_data.track_range[i] if rg_data else None,
                        ar_v1,
                        ar_v2,
                        ar_status,
                        ar_conf,
                    ),
                )

            conn.execute(
                "INSERT OR REPLACE INTO db_meta (key, value) VALUES ('updated_at', ?)",
                (now,),
            )

        print(f"   Catalogue: registered {artist} — {album} ({rbi_path.name})")
    finally:
        conn.close()
