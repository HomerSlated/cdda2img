"""
cdemu.py — cdemu virtual drive management for RBI mount.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path


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
) -> tuple[int, Path]:
    """Extract raw TOC+BIN from *rbi_file* and load into a cdemu virtual slot.

    Returns (slot, toc_path) on success.  toc_path is absolute.
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
        mnt_dir = Path.cwd() / "mnt"

    opts = ExtractOptions(raw=True, tracks=False, warn_missing=False)
    extract_data(rbi_file, opts, base_dir=mnt_dir)

    stem = rbi_file.stem
    toc_path = (mnt_dir / "raw" / f"{stem}.toc").resolve()
    if not toc_path.exists():
        msg = f"Expected extracted TOC not found: {toc_path}"
        raise RuntimeError(msg)

    _load_slot(slot, toc_path)
    return slot, toc_path
