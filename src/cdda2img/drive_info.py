"""
drive_info.py — sysfs drive identity probe.

Public interface:
    probe_drive_inquiry(device) -> tuple[str, str] | None
    probe_drive_name(device) -> str | None

**The offset catalogues are gone (2026-08-27).** This module used to own the
AccurateRip ``driveoffsets.htm`` scrape, the EAC OffsetBase importer and the
``ar_drives`` / ``eac_drives`` tables in ``drive_offsets.db``. Drive offsets are
AccuDisc's now: the read offset is a lookup into their compiled table
(``accudisc_reader.drive_offset_lookup``) and the write offset is *measured* per
drive rather than looked up at all, the published write-offset data being too
sparse to be worth a table. Both results are stored in ``[[drives]]`` in the
user's config, which remains authoritative.

What survives is the part that was never about offsets: naming the drive. That
name keys the ``[[drives]]`` config entries and fills ``PROVENANCE_DRIVE_NAME``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

_SYSFS_BLOCK = Path("/sys/block")


def probe_drive_inquiry(device: str) -> tuple[str, str] | None:
    """Return ``(vendor, product)`` for *device* from sysfs, or None.

    The two INQUIRY fields **kept apart**, which is what AccuDisc's
    ``offset_for(vendor, product)`` wants. Splitting them costs nothing here
    because sysfs exposes them as separate attributes — the boundary was
    available at the source all along, and it was the retired
    ``_normalize_ar_name`` one layer down that joined and discarded it.

    Either field may legitimately be empty: firmware reports the vendor
    inconsistently (blank, the host adapter's ``SATA``, the OEM rather than the
    badge), and an empty vendor is not a failure for a lookup whose key is the
    product. ``None`` means the sysfs paths are absent, which is different.
    """
    dev_path = _SYSFS_BLOCK / Path(device).name / "device"
    try:
        vendor = re.sub(r"\s+", " ", (dev_path / "vendor").read_text().strip())
        model = re.sub(r"\s+", " ", (dev_path / "model").read_text().strip())
    except OSError:
        return None
    return vendor, model


def probe_drive_name(device: str) -> str | None:
    """Return the normalized drive name for *device* from sysfs, or None.

    ``"VENDOR MODEL"``, or just ``"MODEL"`` when the vendor string is empty.
    This is the **display and config key**, not a lookup key — use
    :func:`probe_drive_inquiry` for anything that needs the two fields apart.
    """
    pair = probe_drive_inquiry(device)
    if pair is None:
        return None
    vendor, model = pair
    return f"{vendor} {model}".strip() if vendor else model or None
