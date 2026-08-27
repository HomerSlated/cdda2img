"""
db.py — SQLite backup rotation, shared by the databases this project keeps.

Public interface:
    parse_frequency(s) -> timedelta
    ensure_backup(db_path, max_count, frequency) -> None

**This module no longer owns a database.** It was built around
``drive_offsets.db`` — the AccurateRip scrape and the EAC OffsetBase import —
which was retired on 2026-08-27 when drive offsets became AccuDisc's: the read
offset is a lookup into their compiled table and the write offset is measured
per drive, with both results living in ``[[drives]]`` in the user's config.

What is left is the backup machinery, which was never offsets-specific and is
live for the *catalogue* database (``catalogue.py`` calls ``ensure_backup``).
The ``database_backups`` / ``database_backup_frequency`` config keys that fed
the retired opener are deliberately still accepted: ``CONFIG_SCHEMA`` treats an
unknown key as an error, so dropping them would stop every existing config
loading.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_BACKUP_TS_FMT = "%Y%m%dT%H%M%S"
_BACKUP_TS_RE = re.compile(r"\.\d{8}T\d{6}$")

# ---------------------------------------------------------------------------
# Backup helpers
# ---------------------------------------------------------------------------


def parse_frequency(s: str) -> timedelta:
    """Parse a backup-frequency string into a timedelta.

    Accepted formats: ``Nd`` (days), ``Nw`` (weeks), ``Nmo`` (months, ~30 days).
    Raises ValueError on any other input.
    """
    m = re.fullmatch(r"(\d+)(mo|w|d)", s.strip())
    if not m:
        msg = (
            f"invalid database_backup_frequency {s!r};"
            " expected Nd, Nw, or Nmo (e.g. '1d', '4w', '3mo')"
        )
        raise ValueError(msg)
    n, unit = int(m.group(1)), m.group(2)
    if unit == "w":
        return timedelta(weeks=n)
    if unit == "mo":
        return timedelta(days=n * 30)
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
