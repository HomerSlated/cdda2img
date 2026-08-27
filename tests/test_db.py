"""
test_db.py — unit tests for db.py (SQLite management, backup helpers).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cdda2img.db import (
    _BACKUP_TS_FMT,
    _backup_files,
    _create_backup,
    _last_backup_time,
    _rotate_backups,
    ensure_backup,
    parse_frequency,
)

# ---------------------------------------------------------------------------
# parse_frequency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "s, expected",
    [
        ("1d", timedelta(days=1)),
        ("30d", timedelta(days=30)),
        ("  7d  ", timedelta(days=7)),
        ("1w", timedelta(weeks=1)),
        ("4w", timedelta(weeks=4)),
        ("1mo", timedelta(days=30)),
        ("3mo", timedelta(days=90)),
    ],
)
def test_parse_frequency_valid(s: str, expected: timedelta) -> None:
    assert parse_frequency(s) == expected


@pytest.mark.parametrize(
    "bad", ["", "1x", "abc", "1 d", "-1d", "1.5d", "d", "1", "1h", "30m", "1m"]
)
def test_parse_frequency_invalid(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid database_backup_frequency"):
        parse_frequency(bad)


# ---------------------------------------------------------------------------
# Backup helpers
# ---------------------------------------------------------------------------


def _make_fake_backup(db_path: Path, ts: str) -> Path:
    """Write a trivial SQLite file as a fake backup with the given timestamp."""
    backup = db_path.parent / f"{db_path.name}.{ts}"
    conn = sqlite3.connect(backup)
    conn.close()
    return backup


def test_backup_files_empty(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    db.touch()
    assert _backup_files(db) == []


def test_backup_files_sorted(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    db.touch()
    b1 = _make_fake_backup(db, "20260101T000000")
    b2 = _make_fake_backup(db, "20260201T000000")
    b3 = _make_fake_backup(db, "20260301T000000")
    assert _backup_files(db) == [b1, b2, b3]


def test_last_backup_time_none(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    db.touch()
    assert _last_backup_time(db) is None


def test_last_backup_time_returns_most_recent(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    db.touch()
    _make_fake_backup(db, "20260101T000000")
    _make_fake_backup(db, "20260201T120000")
    result = _last_backup_time(db)
    assert result is not None
    assert result == datetime(2026, 2, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_create_backup_produces_valid_sqlite(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (42)")
    conn.commit()
    conn.close()

    backup = _create_backup(db)
    assert backup.exists()
    assert _BACKUP_TS_FMT  # sanity
    check = sqlite3.connect(backup)
    rows = check.execute("SELECT x FROM t").fetchall()
    check.close()
    assert rows == [(42,)]


def test_rotate_backups_removes_oldest(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    db.touch()
    b1 = _make_fake_backup(db, "20260101T000000")
    b2 = _make_fake_backup(db, "20260201T000000")
    b3 = _make_fake_backup(db, "20260301T000000")

    _rotate_backups(db, max_count=2)

    assert not b1.exists()
    assert b2.exists()
    assert b3.exists()


def test_rotate_backups_noop_when_under_limit(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    db.touch()
    b1 = _make_fake_backup(db, "20260101T000000")
    b2 = _make_fake_backup(db, "20260201T000000")

    _rotate_backups(db, max_count=3)

    assert b1.exists()
    assert b2.exists()


# ---------------------------------------------------------------------------
# ensure_backup
# ---------------------------------------------------------------------------


def test_ensure_backup_creates_when_no_existing(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.close()

    ensure_backup(db, max_count=3, frequency="1d")

    assert len(_backup_files(db)) == 1


def test_ensure_backup_skips_when_max_count_zero(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.close()

    ensure_backup(db, max_count=0, frequency="1d")

    assert len(_backup_files(db)) == 0


def test_ensure_backup_skips_when_db_missing(tmp_path: Path) -> None:
    db = tmp_path / "nonexistent.db"
    ensure_backup(db, max_count=3, frequency="1d")
    assert not db.exists()


def test_ensure_backup_skips_when_recent(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.close()

    # Create a backup timestamped "now" — well within the 1-day window.
    now_ts = datetime.now(timezone.utc).strftime(_BACKUP_TS_FMT)
    _make_fake_backup(db, now_ts)

    ensure_backup(db, max_count=3, frequency="1d")

    # Should still be exactly one backup (the recent one we planted).
    assert len(_backup_files(db)) == 1


def test_ensure_backup_logs_warning_on_bad_frequency(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.close()

    with caplog.at_level(logging.WARNING, logger="cdda2img.db"):
        ensure_backup(db, max_count=3, frequency="BAD")

    assert any("invalid database_backup_frequency" in r.message for r in caplog.records)
    assert len(_backup_files(db)) == 0
