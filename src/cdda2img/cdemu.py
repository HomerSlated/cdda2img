"""
cdemu.py — cdemu virtual drive management for RBI mount.
"""

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

_DEV_SR_RE = re.compile(r"^/dev/sr[0-9]+$")


@dataclass
class CdemuDevice:
    slot: int
    loaded: bool
    filename: str | None


def _run_cdemu(*args: str) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
    try:
        return subprocess.run(  # noqa: S603
            ["cdemu", *args],  # noqa: S607
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        msg = "cdemu is not installed or not on PATH — install cdemu and ensure the daemon is running"
        raise RuntimeError(msg) from e


def cdemu_device_mapping() -> dict[int, str]:
    """Return {slot: /dev/sr* path} from ``cdemu device-mapping``.

    Returns an empty dict if the command fails or produces no output
    (older cdemu versions without the subcommand).
    """
    result = _run_cdemu("device-mapping")
    if result.returncode != 0:
        return {}
    mapping: dict[int, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit() and _DEV_SR_RE.match(parts[1]):
            mapping[int(parts[0])] = parts[1]
    return mapping


def cdemu_status() -> list[CdemuDevice]:
    result = _run_cdemu("status")
    if result.returncode != 0:
        msg = f"cdemu status failed — is the cdemu daemon running?\n{result.stderr.strip()}"
        raise RuntimeError(msg)
    devices: list[CdemuDevice] = []
    for line in result.stdout.splitlines():
        parts = line.split(maxsplit=2)
        if not parts or not parts[0].isdigit():
            continue
        slot = int(parts[0])
        loaded = len(parts) > 1 and parts[1] == "True"
        filename = parts[2].strip() if len(parts) > 2 else None
        devices.append(CdemuDevice(slot=slot, loaded=loaded, filename=filename))
    return devices


def _find_free_slot(devices: list[CdemuDevice]) -> int:
    for dev in devices:
        if not dev.loaded:
            return dev.slot
    occupied = ", ".join(str(d.slot) for d in devices)
    msg = f"No free cdemu slots (occupied: {occupied}) — unload a slot with: cdemu unload <slot>"
    raise RuntimeError(msg)


def _load_slot(slot: int, toc_path: Path) -> None:
    result = _run_cdemu("load", str(slot), str(toc_path))
    if result.returncode != 0:
        msg = f"cdemu load failed:\n{result.stderr.strip()}"
        raise RuntimeError(msg)


def mount_rbi(
    rbi_file: Path,
    slot: int | None = None,
    mnt_dir: Path | None = None,
) -> tuple[int, Path, str | None]:
    """Extract raw TOC+BIN from *rbi_file* and load into a cdemu virtual slot.

    Returns (slot, toc_path, device) on success.  toc_path is absolute.
    device is the /dev/sr* node for the slot, or None if unavailable.
    """
    from cdda2img.container import ExtractOptions, extract_data

    devices = cdemu_status()
    if not devices:
        msg = "cdemu reports no virtual devices — is the kernel module loaded?\nTry: modprobe vhba"
        raise RuntimeError(msg)

    if slot is None:
        slot = _find_free_slot(devices)
    else:
        dev = next((d for d in devices if d.slot == slot), None)
        if dev is None:
            msg = f"cdemu slot {slot} does not exist"
            raise RuntimeError(msg)
        if dev.loaded:
            msg = f"cdemu slot {slot} is already loaded: {dev.filename}\nUnload first with: cdemu unload {slot}"
            raise RuntimeError(msg)

    if mnt_dir is None:
        mnt_dir = Path(tempfile.mkdtemp(prefix="cdda2img_mnt_"))

    opts = ExtractOptions(raw=True, tracks=False, warn_missing=False)
    extract_data(rbi_file, opts, base_dir=mnt_dir)

    stem = rbi_file.stem
    toc_path = (mnt_dir / f"{stem}.toc").resolve()
    if not toc_path.exists():
        msg = f"Expected extracted TOC not found: {toc_path}"
        raise RuntimeError(msg)

    _load_slot(slot, toc_path)
    mapping = cdemu_device_mapping()
    device = mapping.get(slot)
    return slot, toc_path, device
