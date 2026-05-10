"""
test_drive_info.py — unit tests for drive_info.py.
"""

from __future__ import annotations

import sqlite3
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cdda2img.db import _apply_schema
from cdda2img.drive_info import (
    _normalize_ar_name,
    _parse_drive_offsets_html,
    ensure_drive_offsets,
    find_drive_offset,
    probe_drive_name,
)

FIXTURE_HTML = Path(__file__).parent / "fixtures" / "driveoffsets_sample.html"

# ---------------------------------------------------------------------------
# probe_drive_name
# ---------------------------------------------------------------------------


def test_probe_drive_name_returns_normalized(tmp_path: Path) -> None:
    dev = tmp_path / "sr0" / "device"
    dev.mkdir(parents=True)
    (dev / "vendor").write_text("PLEXTOR ")
    (dev / "model").write_text("DVDR   PX-716A  ")

    with patch("cdda2img.drive_info._SYSFS_BLOCK", tmp_path):
        result = probe_drive_name("sr0")

    assert result == "PLEXTOR DVDR PX-716A"


def test_probe_drive_name_no_vendor(tmp_path: Path) -> None:
    dev = tmp_path / "sr0" / "device"
    dev.mkdir(parents=True)
    (dev / "vendor").write_text("")
    (dev / "model").write_text("16X12 DVD DUAL")

    with patch("cdda2img.drive_info._SYSFS_BLOCK", tmp_path):
        result = probe_drive_name("sr0")

    assert result == "16X12 DVD DUAL"


def test_probe_drive_name_missing_sysfs(tmp_path: Path) -> None:
    with patch("cdda2img.drive_info._SYSFS_BLOCK", tmp_path):
        result = probe_drive_name("sr99")

    assert result is None


def test_probe_drive_name_device_path_prefix_stripped(tmp_path: Path) -> None:
    dev = tmp_path / "sr0" / "device"
    dev.mkdir(parents=True)
    (dev / "vendor").write_text("ASUS")
    (dev / "model").write_text("DRW-24D5MT")

    with patch("cdda2img.drive_info._SYSFS_BLOCK", tmp_path):
        result = probe_drive_name("/dev/sr0")

    assert result == "ASUS DRW-24D5MT"


# ---------------------------------------------------------------------------
# _normalize_ar_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("PLEXTOR  - DVDR   PX-716A", "PLEXTOR DVDR PX-716A"),
        ("ACER     - DVD-RW AXD001", "ACER DVD-RW AXD001"),
        ("HL-DT-ST - BD-RE  WH16NS60", "HL-DT-ST BD-RE WH16NS60"),
        ("- 16X12 DVD DUAL", "16X12 DVD DUAL"),
        ("- 16X52X32X52COMBO", "16X52X32X52COMBO"),
        (
            "  PLEXTOR  - DVDR   PX-716A  ",
            "PLEXTOR DVDR PX-716A",
        ),  # leading/trailing ws
    ],
)
def test_normalize_ar_name(raw: str, expected: str) -> None:
    assert _normalize_ar_name(raw) == expected


def test_normalize_ar_name_empty_string_returns_none() -> None:
    assert _normalize_ar_name("") is None


# ---------------------------------------------------------------------------
# _parse_drive_offsets_html
# ---------------------------------------------------------------------------


def test_parse_drive_offsets_html_fixture() -> None:
    data = FIXTURE_HTML.read_bytes()
    rows = _parse_drive_offsets_html(data)

    names = {r[0] for r in rows}
    assert "16X12 DVD DUAL" in names
    assert "PLEXTOR DVDR PX-716A" in names
    assert "ACER DVD-RW AXD001" in names
    assert "HL-DT-ST BD-RE WH16NS60" in names


def test_parse_drive_offsets_html_plextor_values() -> None:
    data = FIXTURE_HTML.read_bytes()
    rows = _parse_drive_offsets_html(data)
    px = next(r for r in rows if r[0] == "PLEXTOR DVDR PX-716A")
    assert px == ("PLEXTOR DVDR PX-716A", 30, 2781)


def test_parse_drive_offsets_html_negative_offset() -> None:
    html = b"""
    <table>
    <tr><td bgcolor="#F4F4F4"><font size="2">TEAC - CD-W54E</font></td>
    <td bgcolor="#F4F4F4"><font size="2">-582</font></td>
    <td bgcolor="#F4F4F4"><font size="2">3</font></td>
    <td bgcolor="#F4F4F4"><font size="2">100%</font></td></tr>
    </table>
    """
    rows = _parse_drive_offsets_html(html)
    assert rows == [("TEAC CD-W54E", -582, 3)]


def test_parse_drive_offsets_html_purged_skipped() -> None:
    html = b"""
    <table>
    <tr><td bgcolor="#F4F4F4"><font size="2">SOME - Drive</font></td>
    <td bgcolor="#F4F4F4"><font size="2">[Purged]</font></td>
    <td bgcolor="#F4F4F4"><font size="2">5</font></td>
    <td bgcolor="#F4F4F4"><font size="2">100%</font></td></tr>
    </table>
    """
    rows = _parse_drive_offsets_html(html)
    assert rows == []


def test_parse_drive_offsets_html_header_row_skipped() -> None:
    data = FIXTURE_HTML.read_bytes()
    rows = _parse_drive_offsets_html(data)
    # Header row has bgcolor="#000000" — should not appear in results
    names = {r[0] for r in rows}
    assert "CD Drive" not in names
    assert "Correction Offset" not in names


# ---------------------------------------------------------------------------
# ensure_drive_offsets — cooldown
# ---------------------------------------------------------------------------


def _open_test_db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    _apply_schema(conn)
    return conn


def test_ensure_drive_offsets_skips_when_recent(tmp_path: Path) -> None:
    conn = _open_test_db(tmp_path)
    recent = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO fetch_log (fetched_at, http_status, last_modified, etag, row_count)"
        " VALUES (?, 200, NULL, NULL, 100)",
        (recent,),
    )
    conn.commit()

    with patch("cdda2img.drive_info.urllib.request.urlopen") as mock_open:
        ensure_drive_offsets(conn)

    mock_open.assert_not_called()
    conn.close()


def test_ensure_drive_offsets_fetches_when_no_log(tmp_path: Path) -> None:
    conn = _open_test_db(tmp_path)
    fixture = FIXTURE_HTML.read_bytes()

    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.status = 200
    mock_resp.read.return_value = fixture
    mock_resp.headers.get.return_value = None

    with patch("cdda2img.drive_info.urllib.request.urlopen", return_value=mock_resp):
        ensure_drive_offsets(conn)

    count = conn.execute("SELECT COUNT(*) FROM ar_drives").fetchone()[0]
    assert count > 0
    log_row = conn.execute("SELECT http_status FROM fetch_log").fetchone()
    assert log_row["http_status"] == 200
    conn.close()


def test_ensure_drive_offsets_fetches_when_stale(tmp_path: Path) -> None:
    conn = _open_test_db(tmp_path)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    conn.execute(
        "INSERT INTO fetch_log (fetched_at, http_status, last_modified, etag, row_count)"
        " VALUES (?, 200, NULL, NULL, 10)",
        (old_ts,),
    )
    conn.commit()

    fixture = FIXTURE_HTML.read_bytes()
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.status = 200
    mock_resp.read.return_value = fixture
    mock_resp.headers.get.return_value = None

    with patch("cdda2img.drive_info.urllib.request.urlopen", return_value=mock_resp):
        ensure_drive_offsets(conn)

    count = conn.execute("SELECT COUNT(*) FROM ar_drives").fetchone()[0]
    assert count > 0
    conn.close()


def test_ensure_drive_offsets_handles_network_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    conn = _open_test_db(tmp_path)

    with (
        caplog.at_level(logging.WARNING, logger="cdda2img.drive_info"),
        patch(
            "cdda2img.drive_info.urllib.request.urlopen",
            side_effect=OSError("connection refused"),
        ),
    ):
        ensure_drive_offsets(conn)

    assert any("fetch failed" in r.message for r in caplog.records)
    count = conn.execute("SELECT COUNT(*) FROM ar_drives").fetchone()[0]
    assert count == 0
    conn.close()


def test_ensure_drive_offsets_handles_304(tmp_path: Path) -> None:
    conn = _open_test_db(tmp_path)
    conn.execute(
        "INSERT INTO ar_drives (ar_name, offset, submissions) VALUES ('X', 0, 1)"
    )
    conn.commit()

    with patch(
        "cdda2img.drive_info.urllib.request.urlopen",
        side_effect=urllib.error.HTTPError(None, 304, "Not Modified", {}, None),  # type: ignore[arg-type]
    ):
        ensure_drive_offsets(conn)

    # ar_drives should be untouched; fetch_log should have one 304 row
    count = conn.execute("SELECT COUNT(*) FROM ar_drives").fetchone()[0]
    assert count == 1
    log_row = conn.execute("SELECT http_status FROM fetch_log").fetchone()
    assert log_row["http_status"] == 304
    conn.close()


def test_ensure_drive_offsets_atomic_replace(tmp_path: Path) -> None:
    """New fetch replaces all old rows atomically."""
    conn = _open_test_db(tmp_path)
    conn.execute(
        "INSERT INTO ar_drives (ar_name, offset, submissions) VALUES ('Old Drive', 999, 1)"
    )
    conn.commit()

    fixture = FIXTURE_HTML.read_bytes()
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.status = 200
    mock_resp.read.return_value = fixture
    mock_resp.headers.get.return_value = None

    with patch("cdda2img.drive_info.urllib.request.urlopen", return_value=mock_resp):
        ensure_drive_offsets(conn)

    names = {r[0] for r in conn.execute("SELECT ar_name FROM ar_drives").fetchall()}
    assert "Old Drive" not in names
    assert "PLEXTOR DVDR PX-716A" in names
    conn.close()


# ---------------------------------------------------------------------------
# find_drive_offset
# ---------------------------------------------------------------------------


def _db_with_drives(tmp_path: Path) -> sqlite3.Connection:
    conn = _open_test_db(tmp_path)
    conn.executemany(
        "INSERT INTO ar_drives (ar_name, offset, submissions) VALUES (?, ?, ?)",
        [
            ("PLEXTOR DVDR PX-716A", 30, 2781),
            ("PLEXTOR DVDR PX-716A", 6, 3),  # minority-offset entry for same drive
            ("ACER DVD-RW AXD001", 6, 2),
        ],
    )
    conn.commit()
    return conn


def test_find_drive_offset_returns_highest_submissions(tmp_path: Path) -> None:
    conn = _db_with_drives(tmp_path)
    result = find_drive_offset(conn, "PLEXTOR DVDR PX-716A")
    assert result == (30, 2781)
    conn.close()


def test_find_drive_offset_returns_none_when_missing(tmp_path: Path) -> None:
    conn = _db_with_drives(tmp_path)
    result = find_drive_offset(conn, "NONEXISTENT DRIVE")
    assert result is None
    conn.close()


def test_find_drive_offset_exact_match_required(tmp_path: Path) -> None:
    conn = _db_with_drives(tmp_path)
    # Partial name should not match
    result = find_drive_offset(conn, "PLEXTOR")
    assert result is None
    conn.close()
