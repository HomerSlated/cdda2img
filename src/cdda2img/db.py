"""
db.py — SQLite database management for cdda2img.

Manages local data storage under $XDG_DATA_HOME/cdda2img/:
    drive_offsets.db  — AccurateRip drive offset catalog + fetch state

Public interface:
    data_dir() -> Path
    drive_offsets_db_path() -> Path
    parse_frequency(s) -> timedelta
    ensure_backup(db_path, max_count, frequency) -> None
    open_drive_offsets_db(cfg) -> sqlite3.Connection
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cdda2img.config import Config

log = logging.getLogger(__name__)

_BACKUP_TS_FMT = "%Y%m%dT%H%M%S"
_BACKUP_TS_RE = re.compile(r"\.\d{8}T\d{6}$")

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
)

# ar_drives: one row per (ar_name, offset) pair from the AccurateRip page.
# A model name may appear more than once when multiple offsets have been
# submitted (pick the row with the highest submissions count for matching).
# fetch_state: key/value store for HTTP cache headers (Last-Modified, ETag)
# so that conditional requests (If-Modified-Since / If-None-Match) can avoid
# re-downloading the full page on every check.
_CREATE_TABLES = """\
CREATE TABLE IF NOT EXISTS ar_drives (
    id          INTEGER PRIMARY KEY,
    ar_name     TEXT    NOT NULL,
    offset      INTEGER NOT NULL,
    submissions INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ar_drives_name ON ar_drives(ar_name);

CREATE TABLE IF NOT EXISTS fetch_log (
    id            INTEGER PRIMARY KEY,
    fetched_at    TEXT    NOT NULL,
    http_status   INTEGER,
    last_modified TEXT,
    etag          TEXT,
    row_count     INTEGER
);

CREATE TABLE IF NOT EXISTS fetch_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# XDG paths
# ---------------------------------------------------------------------------


def data_dir() -> Path:
    """Return $XDG_DATA_HOME/cdda2img/ (default: ~/.local/share/cdda2img/)."""
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg) / "cdda2img"


def drive_offsets_db_path() -> Path:
    """Return the path to the AccurateRip drive offset database."""
    return data_dir() / "drive_offsets.db"


# ---------------------------------------------------------------------------
# Backup helpers
# ---------------------------------------------------------------------------


def parse_frequency(s: str) -> timedelta:
    """Parse a backup-frequency string into a timedelta.

    Accepted formats: ``Nd`` (days), ``Nh`` (hours), ``Nm`` (minutes).
    Raises ValueError on any other input.
    """
    m = re.fullmatch(r"(\d+)([mhd])", s.strip())
    if not m:
        msg = (
            f"invalid database_backup_frequency {s!r};"
            " expected Nd, Nh, or Nm (e.g. '1d', '12h', '30m')"
        )
        raise ValueError(msg)
    n, unit = int(m.group(1)), m.group(2)
    if unit == "m":
        return timedelta(minutes=n)
    if unit == "h":
        return timedelta(hours=n)
    return timedelta(days=n)


def _backup_files(db_path: Path) -> list[Path]:
    """Return timestamped backup files for *db_path*, sorted oldest-first."""
    return sorted(
        p
        for p in db_path.parent.glob(f"{db_path.name}.*")
        if _BACKUP_TS_RE.search(p.name)
    )


def _last_backup_time(db_path: Path) -> datetime | None:
    """Return the UTC timestamp of the most recent backup, or None if none exist."""
    backups = _backup_files(db_path)
    if not backups:
        return None
    # Timestamps are the final dot-separated component: "drive_offsets.db.20260509T143000"
    ts_str = backups[-1].name.rsplit(".", 1)[-1]
    try:
        return datetime.strptime(ts_str, _BACKUP_TS_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _create_backup(db_path: Path) -> Path:
    """Copy *db_path* to a timestamped file using the SQLite online backup API.

    Returns the path of the new backup file.
    Uses two separate connections so the backup API can snapshot a consistent
    state without holding an exclusive lock on the source.
    """
    ts = datetime.now(timezone.utc).strftime(_BACKUP_TS_FMT)
    backup_path = db_path.parent / f"{db_path.name}.{ts}"
    src = sqlite3.connect(db_path, timeout=5)
    dst = sqlite3.connect(backup_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    log.debug("Database backup created: %s", backup_path)
    return backup_path


def _rotate_backups(db_path: Path, max_count: int) -> None:
    """Delete the oldest backup files until at most *max_count* remain."""
    backups = _backup_files(db_path)
    while len(backups) > max_count:
        oldest = backups.pop(0)
        oldest.unlink()
        log.debug("Removed old backup: %s", oldest)


def ensure_backup(db_path: Path, max_count: int, frequency: str) -> None:
    """Create a timestamped backup of *db_path* if the configured interval has elapsed.

    Silently skips when:
    - *db_path* does not exist (nothing to back up yet)
    - *max_count* is 0 (backups disabled)
    - a backup already exists within the *frequency* window

    Logs a warning and skips when *frequency* is not a valid format string.
    """
    if max_count <= 0:
        return
    if not db_path.exists():
        return
    try:
        interval = parse_frequency(frequency)
    except ValueError as exc:
        log.warning("%s", exc)
        return
    last = _last_backup_time(db_path)
    now = datetime.now(timezone.utc)
    if last is not None and (now - last) < interval:
        return
    _create_backup(db_path)
    _rotate_backups(db_path, max_count)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def _apply_schema(conn: sqlite3.Connection) -> None:
    # PRAGMAs run outside executescript() because executescript() issues an
    # implicit COMMIT first, which would interfere with any open transaction.
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    conn.executescript(_CREATE_TABLES)


# ---------------------------------------------------------------------------
# Public database opener
# ---------------------------------------------------------------------------


def open_drive_offsets_db(cfg: Config) -> sqlite3.Connection:
    """Open (creating if necessary) the AccurateRip drive offset database.

    Runs ensure_backup() before opening so a backup is taken before any
    writes begin.  Returns a connection with sqlite3.Row factory and WAL
    journal mode enabled.

    The caller is responsible for closing the connection.
    """
    path = drive_offsets_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_backup(path, cfg.database_backups, cfg.database_backup_frequency)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _apply_schema(conn)
    return conn
