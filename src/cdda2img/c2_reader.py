"""c2_reader.py — raw MMC audio + C2 (+ subchannel) capture via ``c2read`` (on ``$PATH``).

Used by the rip pipeline when C2-erasure recovery is enabled and the drive supports
C2: ``c2read`` does the one full-disc audio read (raw s16le) alongside the per-byte
C2 error-pointer bitmap that feeds ctanalyse's erasure decode, and can capture the
raw P-W subchannel stream in the same pass (``output_sub``). Until the subchannel
decode is wired end-to-end, disc metadata (pre-gaps / ISRC / MCN / CD-Text) still
comes from a separate ``cdrdao read-toc`` pass (see docs/reference/
c2read-upgrade-plan.md — c2read is being extended into a read-only cdrdao
replacement).

``c2read`` is the standalone C helper built from ``tools/c2read`` and symlinked onto
``$PATH``. READ CD returns s16le, so — unlike cdrdao's s16be BIN — the PCM needs no
byte-swap. Hard-unreadable sectors are zero-filled by c2read (C2 bitmap all-ones), so
the PCM/C2/sub streams always stay length-consistent.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

log = logging.getLogger(__name__)

_C2READ = "c2read"  # resolved on $PATH


def _run_features(device: str) -> tuple[int, str] | None:
    """Run ``c2read --features``; return (returncode, stdout) or None if unavailable."""
    try:
        result = subprocess.run(  # noqa: S603 — fixed helper on $PATH
            [_C2READ, "--device", device, "--features"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        log.debug("c2read --features unavailable for %s: %s", device, exc)
        return None
    return result.returncode, result.stdout


def drive_supports_c2(device: str) -> bool:
    """True iff ``c2read --features`` reports the drive both advertises AND functionally
    supports C2 (exit 0). Best-effort: any failure (helper missing, probe error) → False,
    so the pipeline degrades to the plain cdrdao read-cd path."""
    probe = _run_features(device)
    return probe is not None and probe[0] == 0


def probe_combos(device: str) -> dict[str, bool]:
    """Per-combination READ CD support from the ``--features`` smoke probe.

    Returns e.g. ``{"c2": True, "sub_raw": True, "c2+sub_raw": True, ...}`` — the
    ``c2+sub_raw`` key gates the single-pass audio+C2+subchannel capture. Empty dict
    when the probe is unavailable.
    """
    probe = _run_features(device)
    if probe is None:
        return {}
    combos: dict[str, bool] = {}
    for line in probe[1].splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "combo":
            combos[parts[1]] = parts[2] == "ok"
    return combos


def read_disc_c2(
    device: str,
    output_pcm: Path,
    output_c2: Path,
    output_sub: Path | None = None,
    read_speed: int | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
) -> None:
    """Full-disc raw audio (s16le, no byte-swap) + C2 bitmap via ``c2read --full``.

    *output_sub*, when given, additionally captures the raw P-W subchannel stream
    (96 B/sector) in the same pass. *progress_cb(done, total)* receives c2read's
    machine-parseable stdout progress (sector counts) when provided.

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
    if output_sub is not None:
        cmd += ["--sub", "raw", "--subf", str(output_sub)]
    if read_speed:
        cmd += ["--speed", str(read_speed)]

    try:
        if progress_cb is None:
            # Capture output so c2read's progress/summary never corrupts the TUI.
            result = subprocess.run(  # noqa: S603 — fixed helper on $PATH
                cmd, capture_output=True, check=False
            )
            returncode = result.returncode
            stderr_text = result.stderr.decode(errors="replace")
        else:
            returncode, stderr_text = _run_with_progress(cmd, progress_cb)
    except FileNotFoundError:
        msg = "c2read not found — build tools/c2read and put it on $PATH"
        raise RuntimeError(msg) from None
    if returncode not in (0, 3):
        msg = f"c2read read failed (exit {returncode}): {stderr_text.strip()}"
        raise RuntimeError(msg)
    log.debug("c2read: %s", stderr_text.strip())


def _run_with_progress(
    cmd: list[str], progress_cb: Callable[[int, int], None]
) -> tuple[int, str]:
    """Run c2read streaming its stdout ``progress <done> <total>`` lines to the callback.

    stderr goes to a temp file (not a pipe) so a heavily damaged disc emitting many
    ``hard <lba>`` lines can never deadlock the single-threaded stdout reader; it is
    read back afterwards for logging/error detail.
    """
    with tempfile.TemporaryFile() as err_fp:
        proc = subprocess.Popen(  # noqa: S603 — fixed helper on $PATH
            cmd, stdout=subprocess.PIPE, stderr=err_fp, text=True
        )
        assert proc.stdout is not None  # noqa: S101  # guaranteed by stdout=PIPE
        for line in proc.stdout:
            parts = line.split()
            if len(parts) == 3 and parts[0] == "progress":
                try:
                    progress_cb(int(parts[1]), int(parts[2]))
                except ValueError:
                    log.debug("c2read: unparseable progress line %r", line)
        proc.wait()
        err_fp.seek(0)
        stderr_text = err_fp.read().decode(errors="replace")
    return proc.returncode, stderr_text


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
