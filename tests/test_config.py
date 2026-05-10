"""
test_config.py — unit tests for config.py DriveConfig / [[drives]] round-trip.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from cdda2img.config import (
    DriveConfig,
    _parse_drives,
    _rewrite_config_drives,
    _toml_quote,
    load_config,
    save_drive,
)

# ---------------------------------------------------------------------------
# _toml_quote
# ---------------------------------------------------------------------------


def test_toml_quote_plain() -> None:
    assert _toml_quote("PLEXTOR DVDR PX-716A") == '"PLEXTOR DVDR PX-716A"'


def test_toml_quote_backslash() -> None:
    assert _toml_quote("a\\b") == '"a\\\\b"'


def test_toml_quote_double_quote() -> None:
    assert _toml_quote('say "hi"') == '"say \\"hi\\""'


def test_toml_quote_newline() -> None:
    assert _toml_quote("a\nb") == '"a\\nb"'


# ---------------------------------------------------------------------------
# _parse_drives
# ---------------------------------------------------------------------------


def test_parse_drives_valid() -> None:
    raw = [{"name": "PLEXTOR DVDR PX-716A", "offset": 30}]
    result = _parse_drives(raw)
    assert result == [DriveConfig(name="PLEXTOR DVDR PX-716A", offset=30)]


def test_parse_drives_negative_offset() -> None:
    raw = [{"name": "TEAC CD-W54E", "offset": -582}]
    assert _parse_drives(raw) == [DriveConfig(name="TEAC CD-W54E", offset=-582)]


def test_parse_drives_multiple() -> None:
    raw = [
        {"name": "Drive A", "offset": 10},
        {"name": "Drive B", "offset": -5},
    ]
    assert _parse_drives(raw) == [
        DriveConfig(name="Drive A", offset=10),
        DriveConfig(name="Drive B", offset=-5),
    ]


def test_parse_drives_missing_name_skipped(caplog: pytest.LogCaptureFixture) -> None:
    raw = [{"offset": 30}]
    with caplog.at_level(logging.WARNING, logger="cdda2img.config"):
        result = _parse_drives(raw)
    assert result == []
    assert any("missing/invalid name" in r.message for r in caplog.records)


def test_parse_drives_bad_offset_skipped(caplog: pytest.LogCaptureFixture) -> None:
    raw = [{"name": "X Drive", "offset": "not-a-number"}]
    with caplog.at_level(logging.WARNING, logger="cdda2img.config"):
        result = _parse_drives(raw)
    assert result == []
    assert any("invalid offset" in r.message for r in caplog.records)


def test_parse_drives_not_a_list_returns_empty() -> None:
    assert _parse_drives("wrong") == []


def test_parse_drives_empty_list() -> None:
    assert _parse_drives([]) == []


# ---------------------------------------------------------------------------
# _rewrite_config_drives
# ---------------------------------------------------------------------------


def test_rewrite_empty_text_no_drives() -> None:
    assert _rewrite_config_drives("", []) == ""


def test_rewrite_empty_text_appends_drives() -> None:
    drives = [DriveConfig("PLEXTOR DVDR PX-716A", 30)]
    result = _rewrite_config_drives("", drives)
    assert "[[drives]]" in result
    assert 'name = "PLEXTOR DVDR PX-716A"' in result
    assert "offset = 30" in result


def test_rewrite_removes_existing_drives_block() -> None:
    text = 'drive_offset = 30\n\n[[drives]]\nname = "Old Drive"\noffset = 99\n'
    result = _rewrite_config_drives(text, [])
    assert "[[drives]]" not in result
    assert "Old Drive" not in result
    assert "drive_offset = 30" in result


def test_rewrite_replaces_drives_appends_at_end() -> None:
    text = 'drive_offset = 30\n\n[[drives]]\nname = "Old Drive"\noffset = 99\n'
    new_drive = DriveConfig("New Drive", 6)
    result = _rewrite_config_drives(text, [new_drive])
    assert "Old Drive" not in result
    assert "New Drive" in result
    assert result.index("drive_offset") < result.index("[[drives]]")


def test_rewrite_preserves_other_settings() -> None:
    text = 'drive_offset = 30\ncddb_server = "cddb.example.com:888"\n'
    result = _rewrite_config_drives(text, [DriveConfig("X", 0)])
    assert 'cddb_server = "cddb.example.com:888"' in result


def test_rewrite_multiple_drives_blocks_stripped() -> None:
    text = (
        "drive_offset = 0\n"
        '\n[[drives]]\nname = "A"\noffset = 1\n'
        '\n[[drives]]\nname = "B"\noffset = 2\n'
    )
    result = _rewrite_config_drives(text, [])
    assert "[[drives]]" not in result
    assert "drive_offset = 0" in result


def test_rewrite_drives_block_mid_file() -> None:
    """[[drives]] in middle of file: stripped, other sections preserved."""
    text = (
        "drive_offset = 0\n"
        '\n[[drives]]\nname = "Old"\noffset = 1\n'
        "\n[section]\nfoo = 1\n"
    )
    result = _rewrite_config_drives(text, [DriveConfig("New", 2)])
    assert "Old" not in result
    assert "[section]" in result
    assert "foo = 1" in result
    assert "New" in result
    # New drives appended after [section]
    assert result.index("[section]") < result.index("[[drives]]")


def test_rewrite_round_trips_two_drives() -> None:
    drives = [DriveConfig("Drive A", 30), DriveConfig("Drive B", -582)]
    text = _rewrite_config_drives("", drives)
    text2 = _rewrite_config_drives(text, drives)
    assert text == text2


# ---------------------------------------------------------------------------
# save_drive / load_config integration
# ---------------------------------------------------------------------------


def test_save_drive_creates_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "cdda2img.toml"
    save_drive(DriveConfig("PLEXTOR DVDR PX-716A", 30), path=cfg)
    assert cfg.exists()


def test_save_drive_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = tmp_path / "cdda2img" / "cdda2img.toml"
    drive = DriveConfig("PLEXTOR DVDR PX-716A", 30)

    save_drive(drive, path=cfg)
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    config = load_config()

    assert config.drives == [drive]


def test_save_drive_upsert_updates_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "cfg.toml"
    save_drive(DriveConfig("My Drive", 30), path=cfg)
    save_drive(DriveConfig("My Drive", 6), path=cfg)

    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    config = load_config()

    assert len(config.drives) == 1
    assert config.drives[0].offset == 6


def test_save_drive_adds_new_alongside_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "cfg.toml"
    save_drive(DriveConfig("Drive A", 30), path=cfg)
    save_drive(DriveConfig("Drive B", -582), path=cfg)

    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    config = load_config()

    names = {d.name for d in config.drives}
    assert names == {"Drive A", "Drive B"}


def test_save_drive_preserves_other_settings(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text('drive_offset = 42\ncddb_server = "example.com:888"\n')

    save_drive(DriveConfig("X Drive", 0), path=cfg)
    text = cfg.read_text()

    assert "drive_offset = 42" in text
    assert 'cddb_server = "example.com:888"' in text


def test_save_drive_atomic_temp_file_cleaned_up(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg.toml"
    save_drive(DriveConfig("X", 0), path=cfg)
    tmp = cfg.with_name(cfg.name + ".tmp")
    assert not tmp.exists()


def test_load_config_no_drives_field_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("drive_offset = 30\n")
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    config = load_config()
    assert config.drives == []
