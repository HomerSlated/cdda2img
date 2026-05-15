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
    find_drive_write_offset,
    import_eac_drives_xml,
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


# ---------------------------------------------------------------------------
# EAC XML helpers — shared fixture XML
# ---------------------------------------------------------------------------

_EAC_XML_MINIMAL = """\
<?xml version="1.0" encoding="UTF-8"?>
<offsetbase>
  <drive>
    <brand>Plextor</brand>
    <model>708A</model>
    <firmware>?</firmware>
    <accurate_stream>Yes</accurate_stream>
    <audio_caching>No</audio_caching>
    <c2_error_retrieval>No</c2_error_retrieval>
    <read_command>?</read_command>
    <read_offset_correction>+30</read_offset_correction>
    <eac_write>Yes</eac_write>
    <write_offset>-30</write_offset>
  </drive>
  <drive>
    <brand>Acer</brand>
    <model>CD-636A</model>
    <firmware>1.0</firmware>
    <accurate_stream>Yes</accurate_stream>
    <audio_caching>No</audio_caching>
    <c2_error_retrieval>No</c2_error_retrieval>
    <read_command>?</read_command>
    <read_offset_correction>+686</read_offset_correction>
    <eac_write>No</eac_write>
    <write_offset>-</write_offset>
  </drive>
</offsetbase>
"""

_EAC_XML_CONFLICT = """\
<?xml version="1.0" encoding="UTF-8"?>
<offsetbase>
  <drive>
    <brand>Yamaha</brand>
    <model>CRW4416</model>
    <firmware>?</firmware>
    <accurate_stream>Yes</accurate_stream>
    <audio_caching>No</audio_caching>
    <c2_error_retrieval>No</c2_error_retrieval>
    <read_command>?</read_command>
    <read_offset_correction>+168</read_offset_correction>
    <eac_write>No</eac_write>
    <write_offset>+13</write_offset>
  </drive>
  <drive>
    <brand>Yamaha</brand>
    <model>CRW4416</model>
    <firmware>?</firmware>
    <accurate_stream>Yes</accurate_stream>
    <audio_caching>No</audio_caching>
    <c2_error_retrieval>No</c2_error_retrieval>
    <read_command>?</read_command>
    <read_offset_correction>+171</read_offset_correction>
    <eac_write>No</eac_write>
    <write_offset>+9</write_offset>
  </drive>
</offsetbase>
"""

_EAC_XML_UPGRADE = """\
<?xml version="1.0" encoding="UTF-8"?>
<offsetbase>
  <drive>
    <brand>Teac</brand>
    <model>CD-W54E</model>
    <firmware>1.0</firmware>
    <accurate_stream>Yes</accurate_stream>
    <audio_caching>No</audio_caching>
    <c2_error_retrieval>?</c2_error_retrieval>
    <read_command>?</read_command>
    <read_offset_correction>-582</read_offset_correction>
    <eac_write>No</eac_write>
    <write_offset>-</write_offset>
  </drive>
</offsetbase>
"""


def _xml_file(tmp_path: Path, content: str, name: str = "test.xml") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# import_eac_drives_xml
# ---------------------------------------------------------------------------


def test_import_eac_inserts_new_entries(tmp_path: Path) -> None:
    conn = _open_test_db(tmp_path)
    xml = _xml_file(tmp_path, _EAC_XML_MINIMAL)

    result, conflicts = import_eac_drives_xml(conn, xml)

    assert result.inserted == 2
    assert result.upgraded == 0
    assert result.skipped == 0
    assert result.conflicts == 0
    assert conflicts == []
    assert conn.execute("SELECT COUNT(*) FROM eac_drives").fetchone()[0] == 2
    conn.close()


def test_import_eac_parses_offsets(tmp_path: Path) -> None:
    conn = _open_test_db(tmp_path)
    xml = _xml_file(tmp_path, _EAC_XML_MINIMAL)
    import_eac_drives_xml(conn, xml)

    row = conn.execute(
        "SELECT read_offset, write_offset FROM eac_drives WHERE brand='Plextor'"
    ).fetchone()
    assert row["read_offset"] == 30
    assert row["write_offset"] == -30

    row2 = conn.execute(
        "SELECT read_offset, write_offset FROM eac_drives WHERE brand='Acer'"
    ).fetchone()
    assert row2["read_offset"] == 686
    assert row2["write_offset"] is None  # "-" → None
    conn.close()


def test_import_eac_idempotent_on_second_run(tmp_path: Path) -> None:
    conn = _open_test_db(tmp_path)
    xml = _xml_file(tmp_path, _EAC_XML_MINIMAL)

    import_eac_drives_xml(conn, xml)
    result2, _conflicts = import_eac_drives_xml(conn, xml)

    assert result2.inserted == 0
    assert result2.skipped == 2
    assert result2.conflicts == 0
    assert conn.execute("SELECT COUNT(*) FROM eac_drives").fetchone()[0] == 2
    conn.close()


def test_import_eac_conflict_excluded_and_returned(tmp_path: Path) -> None:
    conn = _open_test_db(tmp_path)
    xml = _xml_file(tmp_path, _EAC_XML_CONFLICT)

    result, conflicts = import_eac_drives_xml(conn, xml)

    assert result.inserted == 1  # first entry inserted
    assert result.conflicts == 1  # second entry conflicts
    assert len(conflicts) == 1
    assert conflicts[0]["brand"] == "Yamaha"
    assert conflicts[0]["read_offset_correction"] == "+171"
    assert conn.execute("SELECT COUNT(*) FROM eac_drives").fetchone()[0] == 1
    conn.close()


def test_import_eac_upgrade_fills_nulls(tmp_path: Path) -> None:
    conn = _open_test_db(tmp_path)
    # Pre-insert a row with c2_error_retrieval as NULL
    conn.execute(
        "INSERT INTO eac_drives (brand, model, firmware, accurate_stream,"
        " audio_caching, c2_error_retrieval, read_command, read_offset, eac_write, write_offset)"
        " VALUES ('Teac', 'CD-W54E', '1.0', 'Yes', 'No', NULL, NULL, -582, 'No', NULL)"
    )
    conn.commit()

    # XML has c2_error_retrieval=? → still no-data, no upgrade expected
    xml = _xml_file(tmp_path, _EAC_XML_UPGRADE)
    result, _conflicts = import_eac_drives_xml(conn, xml)

    assert result.skipped == 1
    assert result.upgraded == 0
    assert result.conflicts == 0
    conn.close()


def test_import_eac_upgrade_when_null_in_db(tmp_path: Path) -> None:
    conn = _open_test_db(tmp_path)
    # Pre-insert a row with read_offset NULL — the XML has the value
    conn.execute(
        "INSERT INTO eac_drives (brand, model, firmware, accurate_stream,"
        " audio_caching, c2_error_retrieval, read_command, read_offset, eac_write, write_offset)"
        " VALUES ('Teac', 'CD-W54E', '1.0', 'Yes', 'No', NULL, NULL, NULL, 'No', NULL)"
    )
    conn.commit()

    xml = _xml_file(tmp_path, _EAC_XML_UPGRADE)
    result, _conflicts = import_eac_drives_xml(conn, xml)

    assert result.upgraded == 1
    assert result.conflicts == 0
    row = conn.execute(
        "SELECT read_offset FROM eac_drives WHERE brand='Teac'"
    ).fetchone()
    assert row["read_offset"] == -582
    conn.close()


# ---------------------------------------------------------------------------
# find_drive_write_offset
# ---------------------------------------------------------------------------


def _db_with_eac(tmp_path: Path) -> sqlite3.Connection:
    conn = _open_test_db(tmp_path)
    conn.executemany(
        "INSERT INTO eac_drives (brand, model, firmware, accurate_stream, audio_caching,"
        " c2_error_retrieval, read_command, read_offset, eac_write, write_offset)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("Plextor", "708A", "?", "Yes", "No", "No", "?", 30, "Yes", -30),
            ("Acer", "CD-636A", "1.0", "Yes", "No", "No", "?", 686, "No", None),
            ("Yamaha", "CRW8424S", "?", "Yes", "No", "No", "?", 99, "Yes", 6),
        ],
    )
    conn.commit()
    return conn


def test_find_drive_write_offset_exact_brand_model(tmp_path: Path) -> None:
    conn = _db_with_eac(tmp_path)
    result = find_drive_write_offset(conn, "PLEXTOR DVDR PX-708A")
    assert result == -30
    conn.close()


def test_find_drive_write_offset_returns_none_when_null(tmp_path: Path) -> None:
    conn = _db_with_eac(tmp_path)
    result = find_drive_write_offset(conn, "ACER CD-636A SLIM")
    assert result is None
    conn.close()


def test_find_drive_write_offset_returns_none_when_no_match(tmp_path: Path) -> None:
    conn = _db_with_eac(tmp_path)
    result = find_drive_write_offset(conn, "SAMSUNG SH-S223")
    assert result is None
    conn.close()


def test_find_drive_write_offset_prefers_most_specific(tmp_path: Path) -> None:
    conn = _open_test_db(tmp_path)
    conn.executemany(
        "INSERT INTO eac_drives (brand, model, firmware, accurate_stream, audio_caching,"
        " c2_error_retrieval, read_command, read_offset, eac_write, write_offset)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("Yamaha", "CRW", "?", "Yes", "No", "No", "?", None, "Yes", 6),
            ("Yamaha", "CRW8424S", "?", "Yes", "No", "No", "?", 99, "Yes", 9),
        ],
    )
    conn.commit()
    result = find_drive_write_offset(conn, "YAMAHA CRW8424S")
    assert result == 9  # longer model string wins
    conn.close()
