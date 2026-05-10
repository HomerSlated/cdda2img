"""
test_resolve_drive_offset.py — unit tests for _resolve_drive_offset() in cdda2img.py.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

from cdda2img.cdda2img import _resolve_drive_offset
from cdda2img.config import Config, DriveConfig


def _cfg(**kwargs) -> Config:
    return Config(**kwargs)


# ---------------------------------------------------------------------------
# 1. cfg.drives hit — short-circuit: no DB opened
# ---------------------------------------------------------------------------


def test_uses_config_drives_when_name_matches() -> None:
    cfg = _cfg(drives=[DriveConfig("PLEXTOR DVDR PX-716A", 42)])

    with (
        patch(
            "cdda2img.drive_info.probe_drive_name", return_value="PLEXTOR DVDR PX-716A"
        ),
        patch("cdda2img.db.open_drive_offsets_db") as mock_db,
    ):
        result = _resolve_drive_offset("/dev/sr0", cfg)

    assert result == (42, "PLEXTOR DVDR PX-716A")
    mock_db.assert_not_called()


def test_config_drives_ignores_non_matching_entries() -> None:
    """Other drives in cfg.drives must not affect lookup for an unlisted drive."""
    cfg = _cfg(drive_offset=99, drives=[DriveConfig("OTHER DRIVE", 7)])

    conn_mock = MagicMock()
    conn_mock.__enter__ = lambda s: s
    conn_mock.__exit__ = MagicMock(return_value=False)

    with (
        patch(
            "cdda2img.drive_info.probe_drive_name", return_value="PLEXTOR DVDR PX-716A"
        ),
        patch("cdda2img.db.open_drive_offsets_db", return_value=conn_mock),
        patch("cdda2img.drive_info.ensure_drive_offsets"),
        patch("cdda2img.drive_info.find_drive_offset", return_value=None),
    ):
        result = _resolve_drive_offset("/dev/sr0", cfg)

    assert result == (99, "PLEXTOR DVDR PX-716A")  # drive known but fallback offset


# ---------------------------------------------------------------------------
# 2. AccurateRip auto-apply (submissions >= 3)
# ---------------------------------------------------------------------------


def test_ar_auto_apply_high_confidence(tmp_path: Path) -> None:
    cfg = _cfg(drive_offset=0)
    conn_mock = MagicMock()

    with (
        patch("cdda2img.drive_info.probe_drive_name", return_value="MY DRIVE"),
        patch("cdda2img.db.open_drive_offsets_db", return_value=conn_mock),
        patch("cdda2img.drive_info.ensure_drive_offsets"),
        patch("cdda2img.drive_info.find_drive_offset", return_value=(30, 100)),
        patch("cdda2img.config.save_drive") as mock_save,
    ):
        result = _resolve_drive_offset("/dev/sr0", cfg)

    assert result == (30, "MY DRIVE")
    mock_save.assert_called_once_with(DriveConfig(name="MY DRIVE", offset=30))


def test_ar_auto_apply_saves_to_config(tmp_path: Path) -> None:
    """Saving must use the resolved offset, not cfg.drive_offset."""
    cfg = _cfg(drive_offset=99)
    conn_mock = MagicMock()

    with (
        patch("cdda2img.drive_info.probe_drive_name", return_value="MY DRIVE"),
        patch("cdda2img.db.open_drive_offsets_db", return_value=conn_mock),
        patch("cdda2img.drive_info.ensure_drive_offsets"),
        patch("cdda2img.drive_info.find_drive_offset", return_value=(30, 5)),
        patch("cdda2img.config.save_drive") as mock_save,
    ):
        result = _resolve_drive_offset("/dev/sr0", cfg)

    assert result == (30, "MY DRIVE")
    assert mock_save.call_args == call(DriveConfig(name="MY DRIVE", offset=30))


# ---------------------------------------------------------------------------
# 3. AccurateRip prompt accepted (submissions < 3, TTY=True)
# ---------------------------------------------------------------------------


def test_ar_prompt_accepted(tmp_path: Path) -> None:
    cfg = _cfg(drive_offset=0)
    conn_mock = MagicMock()

    with (
        patch("cdda2img.drive_info.probe_drive_name", return_value="MY DRIVE"),
        patch("cdda2img.db.open_drive_offsets_db", return_value=conn_mock),
        patch("cdda2img.drive_info.ensure_drive_offsets"),
        patch("cdda2img.drive_info.find_drive_offset", return_value=(6, 2)),
        patch("sys.stdin") as mock_stdin,
        patch("builtins.input", return_value="y"),
        patch("cdda2img.config.save_drive") as mock_save,
    ):
        mock_stdin.isatty.return_value = True
        result = _resolve_drive_offset("/dev/sr0", cfg)

    assert result == (6, "MY DRIVE")
    mock_save.assert_called_once()


# ---------------------------------------------------------------------------
# 4. AccurateRip prompt rejected
# ---------------------------------------------------------------------------


def test_ar_prompt_rejected_falls_back(tmp_path: Path) -> None:
    cfg = _cfg(drive_offset=99)
    conn_mock = MagicMock()

    with (
        patch("cdda2img.drive_info.probe_drive_name", return_value="MY DRIVE"),
        patch("cdda2img.db.open_drive_offsets_db", return_value=conn_mock),
        patch("cdda2img.drive_info.ensure_drive_offsets"),
        patch("cdda2img.drive_info.find_drive_offset", return_value=(6, 2)),
        patch("sys.stdin") as mock_stdin,
        patch("builtins.input", return_value="n"),
        patch("cdda2img.config.save_drive") as mock_save,
    ):
        mock_stdin.isatty.return_value = True
        result = _resolve_drive_offset("/dev/sr0", cfg)

    assert result == (99, "MY DRIVE")
    mock_save.assert_not_called()


def test_ar_low_confidence_no_tty_falls_back(tmp_path: Path) -> None:
    """Non-TTY + low confidence: auto-reject without prompting."""
    cfg = _cfg(drive_offset=77)
    conn_mock = MagicMock()

    with (
        patch("cdda2img.drive_info.probe_drive_name", return_value="MY DRIVE"),
        patch("cdda2img.db.open_drive_offsets_db", return_value=conn_mock),
        patch("cdda2img.drive_info.ensure_drive_offsets"),
        patch("cdda2img.drive_info.find_drive_offset", return_value=(6, 1)),
        patch("sys.stdin") as mock_stdin,
        patch("cdda2img.config.save_drive") as mock_save,
    ):
        mock_stdin.isatty.return_value = False
        result = _resolve_drive_offset("/dev/sr0", cfg)

    assert result == (77, "MY DRIVE")
    mock_save.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Drive not in AccurateRip catalog
# ---------------------------------------------------------------------------


def test_drive_not_in_catalog_uses_global_fallback() -> None:
    cfg = _cfg(drive_offset=55)
    conn_mock = MagicMock()

    with (
        patch("cdda2img.drive_info.probe_drive_name", return_value="UNKNOWN DRIVE"),
        patch("cdda2img.db.open_drive_offsets_db", return_value=conn_mock),
        patch("cdda2img.drive_info.ensure_drive_offsets"),
        patch("cdda2img.drive_info.find_drive_offset", return_value=None),
        patch("cdda2img.config.save_drive") as mock_save,
    ):
        result = _resolve_drive_offset("/dev/sr0", cfg)

    assert result == (55, "UNKNOWN DRIVE")
    mock_save.assert_not_called()


# ---------------------------------------------------------------------------
# 6. sysfs probe fails — no DB opened
# ---------------------------------------------------------------------------


def test_probe_fails_uses_global_fallback_no_db() -> None:
    cfg = _cfg(drive_offset=33)

    with (
        patch("cdda2img.drive_info.probe_drive_name", return_value=None),
        patch("cdda2img.db.open_drive_offsets_db") as mock_db,
    ):
        result = _resolve_drive_offset("/dev/sr99", cfg)

    assert result == (33, None)
    mock_db.assert_not_called()


# ---------------------------------------------------------------------------
# save_drive OSError is swallowed
# ---------------------------------------------------------------------------


def test_save_drive_oserror_does_not_propagate() -> None:
    cfg = _cfg(drive_offset=0)
    conn_mock = MagicMock()

    with (
        patch("cdda2img.drive_info.probe_drive_name", return_value="MY DRIVE"),
        patch("cdda2img.db.open_drive_offsets_db", return_value=conn_mock),
        patch("cdda2img.drive_info.ensure_drive_offsets"),
        patch("cdda2img.drive_info.find_drive_offset", return_value=(30, 10)),
        patch("cdda2img.config.save_drive", side_effect=OSError("permission denied")),
    ):
        result = _resolve_drive_offset("/dev/sr0", cfg)

    assert result == (30, "MY DRIVE")  # offset still returned despite save failure
