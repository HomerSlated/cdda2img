"""
test_drive_info.py — unit tests for drive_info.py.

The AccurateRip / EAC catalogue tests went with the catalogues on 2026-08-27.
What is left is the sysfs identity probe, which was never about offsets.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from cdda2img.drive_info import probe_drive_inquiry, probe_drive_name

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
# probe_drive_inquiry — the two INQUIRY fields kept APART
# ---------------------------------------------------------------------------


def test_probe_drive_inquiry_keeps_vendor_and_product_separate(tmp_path: Path) -> None:
    """The split is the point: AccuDisc's lookup keys on product, vendor narrows.

    probe_drive_name joins these two and is lossy by design. Anything feeding a
    lookup must use this instead — the boundary is free here because sysfs
    exposes the fields separately, and it was the retired _normalize_ar_name
    that used to throw it away.
    """
    dev = tmp_path / "sr0" / "device"
    dev.mkdir(parents=True)
    (dev / "vendor").write_text("PLEXTOR ")
    (dev / "model").write_text("DVDR   PX-716A  ")

    with patch("cdda2img.drive_info._SYSFS_BLOCK", tmp_path):
        assert probe_drive_inquiry("sr0") == ("PLEXTOR", "DVDR PX-716A")
        # Negative control: the joined form really does lose the boundary, so a
        # test that passed on both functions would not be testing the split.
        assert probe_drive_name("sr0") == "PLEXTOR DVDR PX-716A"


def test_probe_drive_inquiry_empty_vendor_is_a_value_not_a_failure(
    tmp_path: Path,
) -> None:
    """An empty vendor is legitimate and must survive as ``""``.

    Firmware reports that field inconsistently, and AccuDisc's contract says a
    vendor matching no row is not a rejection. Collapsing this to None would
    make "the drive reports no vendor" indistinguishable from "sysfs is absent",
    which is the case below.
    """
    dev = tmp_path / "sr0" / "device"
    dev.mkdir(parents=True)
    (dev / "vendor").write_text("")
    (dev / "model").write_text("16X12 DVD DUAL")

    with patch("cdda2img.drive_info._SYSFS_BLOCK", tmp_path):
        assert probe_drive_inquiry("sr0") == ("", "16X12 DVD DUAL")


def test_probe_drive_inquiry_missing_sysfs_is_none(tmp_path: Path) -> None:
    with patch("cdda2img.drive_info._SYSFS_BLOCK", tmp_path):
        assert probe_drive_inquiry("sr99") is None
