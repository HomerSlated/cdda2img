"""c2_reader.py — raw MMC audio + C2 capture via the ``c2read`` helper (on ``$PATH``).

Used by the rip pipeline when C2-erasure recovery is enabled and the drive supports
C2: ``c2read`` does the one full-disc audio read (raw s16le) alongside the per-byte
C2 error-pointer bitmap that feeds ctanalyse's erasure decode. Subchannel metadata
(ISRC / MCN / CD-Text / pre-gaps) is captured separately by a ``cdrdao read-toc``
pass, because c2read does not yet read the subchannel (see docs/reference/TODO.md —
c2read is being extended toward a read-only cdrdao replacement).

``c2read`` is the standalone C helper built from ``tools/c2read`` and symlinked onto
``$PATH``. READ CD returns s16le, so — unlike cdrdao's s16be BIN — the PCM needs no
byte-swap.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

_C2READ = "c2read"  # resolved on $PATH


def drive_supports_c2(device: str) -> bool:
    """True iff ``c2read --features`` reports the drive both advertises AND functionally
    supports C2 (exit 0). Best-effort: any failure (helper missing, probe error) → False,
    so the pipeline degrades to the plain cdrdao read-cd path."""
    try:
        result = subprocess.run(  # noqa: S603 — fixed helper on $PATH
            [_C2READ, "--device", device, "--features"],
            capture_output=True,
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        log.debug("c2read --features unavailable for %s: %s", device, exc)
        return False
    return result.returncode == 0


def read_disc_c2(
    device: str, output_pcm: Path, output_c2: Path, read_speed: int | None = None
) -> None:
    """Full-disc raw audio (s16le, no byte-swap) + C2 bitmap via ``c2read --full``.

    Raises RuntimeError on a genuine failure. c2read's exit code is the C2 *verdict*:
    0 = flags found, 3 = none found / hard-unreadable regions — both mean the read
    itself completed, so only 1 (I/O error) or 2 (usage) are fatal here."""
    cmd = [
        _C2READ,
        "--device",
        device,
        "--full",
        "-q",
        "--pcm",
        str(output_pcm),
        "--c2",
        str(output_c2),
    ]
    if read_speed:
        cmd += ["--speed", str(read_speed)]
    try:
        # Capture output so c2read's progress/summary never corrupts the TUI.
        result = subprocess.run(  # noqa: S603 — fixed helper on $PATH
            cmd, capture_output=True, check=False
        )
    except FileNotFoundError:
        msg = "c2read not found — build tools/c2read and put it on $PATH"
        raise RuntimeError(msg) from None
    if result.returncode not in (0, 3):
        detail = result.stderr.decode(errors="replace").strip()
        msg = f"c2read read failed (exit {result.returncode}): {detail}"
        raise RuntimeError(msg)
    log.debug("c2read: %s", result.stderr.decode(errors="replace").strip())


def park_spindle(device: str) -> None:
    """Best-effort spindle stop (``c2read --stop`` → SCSI START STOP UNIT) once done
    reading, so a finished pass doesn't leave the drive spinning. Never raises."""
    try:
        subprocess.run(  # noqa: S603 — fixed helper on $PATH
            [_C2READ, "--device", device, "--stop", "-q"],
            capture_output=True,
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        log.debug("c2read --stop failed for %s: %s", device, exc)
