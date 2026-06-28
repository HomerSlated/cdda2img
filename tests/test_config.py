"""
test_config.py — unit tests for config.py DriveConfig / [[drives]] round-trip.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from cdda2img.config import (
    DriveConfig,
    _overlay,
    _parse_drives,
    _parse_preferred_country,
    _render_scalar,
    _rewrite_config_drives,
    _toml_quote,
    load_config,
    save_drive,
    save_drive_read_offset,
    save_drive_write_offset,
)

# ---------------------------------------------------------------------------
# _toml_quote
# ---------------------------------------------------------------------------


def test_toml_quote_plain() -> None:
    assert _toml_quote("PLEXTOR DVDR PX-716A") == '"PLEXTOR DVDR PX-716A"'


# ---------------------------------------------------------------------------
# _render_scalar / _overlay — list (array) values must round-trip as TOML arrays
# ---------------------------------------------------------------------------


def test_render_scalar_renders_list_as_toml_array() -> None:
    # Regression: a list value was stringified to "['GB', 'XE', 'US']" (a quoted
    # Python repr) instead of a TOML array, corrupting preferred_country on
    # `setup --update-config`.
    assert (
        _render_scalar("preferred_country", ["GB", "XE", "US"])
        == 'preferred_country = ["GB", "XE", "US"]'
    )


def test_render_scalar_scalars_unchanged() -> None:
    assert _render_scalar("enable_catalogue", True) == "enable_catalogue = true"
    assert _render_scalar("low_dr_threshold", 5.0) == "low_dr_threshold = 5.0"
    assert _render_scalar("cddb_server", "gnudb.org") == 'cddb_server = "gnudb.org"'


def test_overlay_list_value_reparses_as_list() -> None:
    # End-to-end: a list user value survives the template overlay and parses back
    # as a list (not a string) — the exact path `setup --update-config` runs.
    from cdda2img.config import tomllib  # stdlib on 3.11+, tomli on 3.10

    example = "# preferred_country = []\nenable_catalogue = true\n"
    merged = _overlay(example, {"preferred_country": ["GB", "XE", "US"]})
    parsed = tomllib.loads(merged)
    assert parsed["preferred_country"] == ["GB", "XE", "US"]
    assert _parse_preferred_country(parsed["preferred_country"]) == ["GB", "XE", "US"]


def test_toml_quote_backslash() -> None:
    assert _toml_quote("a\\b") == '"a\\\\b"'


def test_toml_quote_double_quote() -> None:
    assert _toml_quote('say "hi"') == '"say \\"hi\\""'


def test_toml_quote_newline() -> None:
    assert _toml_quote("a\nb") == '"a\\nb"'


# ---------------------------------------------------------------------------
# _parse_preferred_country
# ---------------------------------------------------------------------------


def test_parse_preferred_country_order_and_upper() -> None:
    assert _parse_preferred_country(["gb", "XE", "us"]) == ["GB", "XE", "US"]


def test_parse_preferred_country_strips_and_dedups() -> None:
    assert _parse_preferred_country([" gb ", "GB", "", "  "]) == ["GB"]


def test_parse_preferred_country_drops_non_strings() -> None:
    assert _parse_preferred_country(["GB", 42, None, "US"]) == ["GB", "US"]


def test_parse_preferred_country_empty() -> None:
    assert _parse_preferred_country([]) == []


def test_parse_preferred_country_non_list() -> None:
    assert _parse_preferred_country("GB") == []


# ---------------------------------------------------------------------------
# _parse_drives
# ---------------------------------------------------------------------------


def test_parse_drives_valid() -> None:
    raw = [{"name": "PLEXTOR DVDR PX-716A", "read_offset": 30}]
    result = _parse_drives(raw)
    assert result == [DriveConfig(name="PLEXTOR DVDR PX-716A", read_offset=30)]


def test_parse_drives_negative_offset() -> None:
    raw = [{"name": "TEAC CD-W54E", "read_offset": -582}]
    assert _parse_drives(raw) == [DriveConfig(name="TEAC CD-W54E", read_offset=-582)]


def test_parse_drives_with_write_offset() -> None:
    raw = [{"name": "PLEXTOR DVDR PX-716A", "read_offset": 30, "write_offset": -30}]
    result = _parse_drives(raw)
    assert result == [
        DriveConfig(name="PLEXTOR DVDR PX-716A", read_offset=30, write_offset=-30)
    ]


def test_parse_drives_write_offset_none_when_absent() -> None:
    raw = [{"name": "My Drive", "read_offset": 6}]
    result = _parse_drives(raw)
    assert result[0].write_offset is None


def test_parse_drives_multiple() -> None:
    raw = [
        {"name": "Drive A", "read_offset": 10},
        {"name": "Drive B", "read_offset": -5},
    ]
    assert _parse_drives(raw) == [
        DriveConfig(name="Drive A", read_offset=10),
        DriveConfig(name="Drive B", read_offset=-5),
    ]


def test_parse_drives_missing_name_skipped(caplog: pytest.LogCaptureFixture) -> None:
    raw = [{"read_offset": 30}]
    with caplog.at_level(logging.WARNING, logger="cdda2img.config"):
        result = _parse_drives(raw)
    assert result == []
    assert any("missing/invalid name" in r.message for r in caplog.records)


def test_parse_drives_bad_offset_skipped(caplog: pytest.LogCaptureFixture) -> None:
    raw = [{"name": "X Drive", "read_offset": "not-a-number"}]
    with caplog.at_level(logging.WARNING, logger="cdda2img.config"):
        result = _parse_drives(raw)
    assert result == []
    assert any("invalid read_offset" in r.message for r in caplog.records)


def test_parse_drives_bad_write_offset_ignored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Invalid write_offset is ignored (logged), drive still parsed with write_offset=None."""
    raw = [{"name": "X Drive", "read_offset": 6, "write_offset": "bad"}]
    with caplog.at_level(logging.WARNING, logger="cdda2img.config"):
        result = _parse_drives(raw)
    assert result == [DriveConfig(name="X Drive", read_offset=6, write_offset=None)]
    assert any("invalid write_offset" in r.message for r in caplog.records)


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
    assert "read_offset = 30" in result


def test_rewrite_write_offset_emitted_when_set() -> None:
    drives = [DriveConfig("My Drive", 30, write_offset=-30)]
    result = _rewrite_config_drives("", drives)
    assert "write_offset = -30" in result


def test_rewrite_write_offset_omitted_when_none() -> None:
    drives = [DriveConfig("My Drive", 30, write_offset=None)]
    result = _rewrite_config_drives("", drives)
    assert "write_offset" not in result


def test_rewrite_removes_existing_drives_block() -> None:
    text = 'cddb_server = "a"\n\n[[drives]]\nname = "Old Drive"\nread_offset = 99\n'
    result = _rewrite_config_drives(text, [])
    assert "[[drives]]" not in result
    assert "Old Drive" not in result
    assert 'cddb_server = "a"' in result


def test_rewrite_replaces_drives_appends_at_end() -> None:
    text = 'cddb_server = "a"\n\n[[drives]]\nname = "Old Drive"\nread_offset = 99\n'
    new_drive = DriveConfig("New Drive", 6)
    result = _rewrite_config_drives(text, [new_drive])
    assert "Old Drive" not in result
    assert "New Drive" in result
    assert result.index("cddb_server") < result.index("[[drives]]")


def test_rewrite_preserves_other_settings() -> None:
    text = 'cddb_server = "cddb.example.com:888"\n'
    result = _rewrite_config_drives(text, [DriveConfig("X", 0)])
    assert 'cddb_server = "cddb.example.com:888"' in result


def test_rewrite_multiple_drives_blocks_stripped() -> None:
    text = (
        'cddb_server = "a"\n'
        '\n[[drives]]\nname = "A"\nread_offset = 1\n'
        '\n[[drives]]\nname = "B"\nread_offset = 2\n'
    )
    result = _rewrite_config_drives(text, [])
    assert "[[drives]]" not in result
    assert 'cddb_server = "a"' in result


def test_rewrite_drives_block_mid_file() -> None:
    """[[drives]] in middle of file: stripped, other sections preserved."""
    text = (
        'cddb_server = "a"\n'
        '\n[[drives]]\nname = "Old"\nread_offset = 1\n'
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


def test_rewrite_round_trips_drive_with_write_offset() -> None:
    drives = [DriveConfig("Drive A", 30, write_offset=-30)]
    text = _rewrite_config_drives("", drives)
    text2 = _rewrite_config_drives(text, drives)
    assert text == text2


# ---------------------------------------------------------------------------
# save_drive / load_config integration
# ---------------------------------------------------------------------------


def test_save_drive_creates_file(tmp_path: Path) -> None:
    cfg = tmp_path / "cdda2img.toml"
    save_drive(DriveConfig("PLEXTOR DVDR PX-716A", 30), path=cfg)
    assert cfg.exists()


def test_save_drive_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "cdda2img" / "cdda2img.toml"
    drive = DriveConfig("PLEXTOR DVDR PX-716A", 30)

    save_drive(drive, path=cfg)
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    config = load_config()

    assert config.drives == [drive]


def test_save_drive_with_write_offset_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "cfg.toml"
    drive = DriveConfig("My Drive", 30, write_offset=-30)
    save_drive(drive, path=cfg)
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    config = load_config()
    assert config.drives == [drive]


def test_save_drive_upsert_updates_read_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "cfg.toml"
    save_drive(DriveConfig("My Drive", 30), path=cfg)
    save_drive(DriveConfig("My Drive", 6), path=cfg)

    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    config = load_config()

    assert len(config.drives) == 1
    assert config.drives[0].read_offset == 6


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
    cfg.write_text('cddb_server = "example.com:888"\n')

    save_drive(DriveConfig("X Drive", 0), path=cfg)
    text = cfg.read_text()

    assert 'cddb_server = "example.com:888"' in text


def test_save_drive_atomic_temp_file_cleaned_up(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg.toml"
    save_drive(DriveConfig("X", 0), path=cfg)
    tmp = cfg.with_name(cfg.name + ".tmp")
    assert not tmp.exists()


def test_embedart_read_from_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # BUG-6 regression: load_config must read embedart from the TOML.
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("embedart = true\n")
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    assert load_config().embedart is True


def test_preferred_country_read_from_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text('preferred_country = ["gb", "XE", "us"]\n')
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    assert load_config().preferred_country == ["GB", "XE", "US"]


def test_preferred_country_defaults_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text('cddb_server = "example.com:888"\n')
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    assert load_config().preferred_country == []


def test_embedart_defaults_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("")
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    assert load_config().embedart is False


def test_recovery_passes_defaults_to_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("")
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    assert load_config().recovery_passes == 3


def test_recovery_passes_read_and_zero_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("recovery_passes = 0\n")  # 0 disables laddered recovery
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    assert load_config().recovery_passes == 0


def test_recovery_passes_out_of_range_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("recovery_passes = 999\n")
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    assert load_config().recovery_passes == 3


# ---------------------------------------------------------------------------
# save_drive_read_offset — merge-safe partial update
# ---------------------------------------------------------------------------


def test_save_drive_read_offset_creates_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "cfg.toml"
    save_drive_read_offset("My Drive", 30, path=cfg)
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    config = load_config()
    assert config.drives[0].read_offset == 30
    assert config.drives[0].write_offset is None


def test_save_drive_read_offset_preserves_write_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "cfg.toml"
    save_drive(DriveConfig("My Drive", 0, write_offset=-30), path=cfg)
    save_drive_read_offset("My Drive", 30, path=cfg)
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    config = load_config()
    assert config.drives[0].read_offset == 30
    assert config.drives[0].write_offset == -30


def test_save_drive_read_offset_round_trip_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """save_drive_read_offset then save_drive_write_offset yields both fields."""
    cfg = tmp_path / "cfg.toml"
    save_drive_read_offset("My Drive", 30, path=cfg)
    save_drive_write_offset("My Drive", -30, path=cfg)
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    config = load_config()
    assert config.drives[0].read_offset == 30
    assert config.drives[0].write_offset == -30


# ---------------------------------------------------------------------------
# save_drive_write_offset — merge-safe partial update
# ---------------------------------------------------------------------------


def test_save_drive_write_offset_creates_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "cfg.toml"
    save_drive_write_offset("My Drive", -30, path=cfg)
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    config = load_config()
    assert config.drives[0].write_offset == -30
    assert config.drives[0].read_offset == 0  # default when no prior entry


def test_save_drive_write_offset_preserves_read_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "cfg.toml"
    save_drive(DriveConfig("My Drive", 30), path=cfg)
    save_drive_write_offset("My Drive", -30, path=cfg)
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    config = load_config()
    assert config.drives[0].read_offset == 30
    assert config.drives[0].write_offset == -30


def test_save_drive_write_offset_reverse_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """save_drive_write_offset then save_drive_read_offset yields both fields."""
    cfg = tmp_path / "cfg.toml"
    save_drive_write_offset("My Drive", -30, path=cfg)
    save_drive_read_offset("My Drive", 30, path=cfg)
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    config = load_config()
    assert config.drives[0].read_offset == 30
    assert config.drives[0].write_offset == -30


def test_save_drive_write_offset_only_key_absent_from_config(tmp_path: Path) -> None:
    """Drive with only read_offset in config must not acquire spurious write_offset=0."""
    cfg = tmp_path / "cfg.toml"
    save_drive(DriveConfig("My Drive", 30), path=cfg)
    text = cfg.read_text()
    assert "write_offset" not in text


# ---------------------------------------------------------------------------
# load_config — no drives field
# ---------------------------------------------------------------------------


def test_load_config_no_drives_field_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text('cddb_server = "example.com:888"\n')
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    config = load_config()
    assert config.drives == []


# ---------------------------------------------------------------------------
# default_device
# ---------------------------------------------------------------------------


def test_default_device_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("")
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    assert load_config().default_device == "/dev/sr0"


def test_default_device_reads_from_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text('default_device = "/dev/sr1"\n')
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    assert load_config().default_device == "/dev/sr1"


# ---------------------------------------------------------------------------
# silence / capacity / preview / tui
# ---------------------------------------------------------------------------


def test_silence_capacity_preview_tui_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("")
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    c = load_config()
    assert c.silence_threshold == 55
    assert c.capacity == 80
    assert c.preview is True
    assert c.tui is True
    assert c.low_dr_threshold == 5.0


def test_silence_capacity_preview_tui_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        "silence_threshold = 40\ncapacity = 90\npreview = false\ntui = false\n"
        "low_dr_threshold = 6.5\n"
    )
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    c = load_config()
    assert c.silence_threshold == 40
    assert c.capacity == 90
    assert c.preview is False
    assert c.tui is False
    assert c.low_dr_threshold == 6.5


def test_silence_out_of_range_falls_back_to_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("silence_threshold = 200\n")
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    with caplog.at_level(logging.WARNING):
        c = load_config()
    assert c.silence_threshold == 55
    assert "Invalid silence_threshold" in caplog.text


def test_capacity_out_of_range_falls_back_to_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("capacity = 0\n")
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    with caplog.at_level(logging.WARNING):
        c = load_config()
    assert c.capacity == 80
    assert "Invalid capacity" in caplog.text


def test_silence_non_integer_falls_back_to_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text('silence_threshold = "loud"\n')
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    with caplog.at_level(logging.WARNING):
        c = load_config()
    assert c.silence_threshold == 55
    assert "Invalid silence_threshold" in caplog.text


def test_low_dr_threshold_out_of_range_falls_back_to_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("low_dr_threshold = 99\n")
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    with caplog.at_level(logging.WARNING):
        c = load_config()
    assert c.low_dr_threshold == 5.0
    assert "Invalid low_dr_threshold" in caplog.text


def test_low_dr_threshold_non_numeric_falls_back_to_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text('low_dr_threshold = "loud"\n')
    monkeypatch.setattr("cdda2img.config.config_path", lambda: cfg)
    with caplog.at_level(logging.WARNING):
        c = load_config()
    assert c.low_dr_threshold == 5.0
    assert "Invalid low_dr_threshold" in caplog.text
