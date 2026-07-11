"""accudisc_reader.py — raw MMC audio + C2 + subchannel capture via AccuDisc.

Drop-in AccuDisc replacement for :mod:`cdda2img.c2_reader` (which drove the
``c2read`` prototype). The public surface is identical — ``read_disc_c2`` /
``read_span`` / ``drive_supports_c2`` / ``probe_combos`` / ``park_spindle`` — so
the rip pipeline swaps modules with no call-site logic change. AccuDisc is the
successor to c2read; the invocation moved from flags to subcommands and the
progress interface changed, both handled here.

Differences from c2read, and how they are absorbed:

* **Subcommand form** — ``accudisc --device DEV <command>`` (``--device`` is a
  global option *before* the command), vs c2read's flat flag list.
* **C2 bitmap file** is ``--c2f`` (was ``--c2``); C2 pointers are requested by
  default (``--no-c2`` opts out).
* **Whole-disc read** is ``read`` with no ``--count`` (defaults through the
  lead-out), replacing c2read's ``--full``.
* **CD-Text and full TOC** are captured inline on the read pass
  (``read --fulltoc F --cdtext F``) — one spin-up, like c2read's single pass.
  The ``fulltoc`` dump is byte-identical to c2read's, so
  ``subq_toc.parse_fulltoc`` is unaffected. Absence of CD-Text writes no file and
  does not change the read's exit code, so the caller just checks for the file.
* **Progress** uses ``read --progress-fd 1``: newline-delimited machine tokens on
  stdout — ``progress <done> <total>`` lines plus a final
  ``summary hard=… c2=… recovered=… suspect=… rereads=… slips=…`` — unaffected by
  ``-q`` (which mutes only the human ``\\r`` stderr line). Parsed in
  :func:`_run_with_progress`. The frozen contract is the AccuDisc repo's
  ``docs/cli-machine-interface.md`` — parse against that, never the human stderr.
* **Exit contract** — ``0`` = completed clean; ``3`` = completed with caveats
  (``hard``/``suspect``/residual-C2 after recovery). Both mean the image was
  delivered, so the acceptance check is ``in (0, 3)``. ``1`` = usage/argument/
  local-file; ``2`` = fatal device/transport. Exit 0 is **not** verification — it
  means no *relative* signal fired; AccurateRip/CTDB gating stays with the caller.

READ CD returns s16le, so — unlike cdrdao's s16be BIN — the PCM needs no
byte-swap. Hard-unreadable sectors are zero-filled by AccuDisc (C2 bitmap
all-ones), so the PCM/C2/sub streams always stay length-consistent.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger(__name__)


def _resolve_accudisc() -> str:
    """Locate the AccuDisc binary: the snapshot in ``tools/accudisc/`` first, else PATH.

    The snapshot (``tools/accudisc/accudisc``) is the version validated against
    this checkout; falling back to a bare ``accudisc`` on ``$PATH`` keeps a
    system install working. Path is dev-tree relative (mirrors the conf-example
    resolution); replace with ``importlib.resources`` when packaging lands.
    """
    snapshot = Path(__file__).parent.parent.parent / "tools" / "accudisc" / "accudisc"
    return str(snapshot) if snapshot.is_file() else "accudisc"


_ACCUDISC = _resolve_accudisc()


def _run_features(device: str) -> tuple[int, str] | None:
    """Run ``accudisc features``; return (returncode, stdout) or None if unavailable."""
    try:
        result = subprocess.run(  # noqa: S603 — snapshot/PATH binary, fixed argv
            [_ACCUDISC, "--device", device, "features"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        log.debug("accudisc features unavailable for %s: %s", device, exc)
        return None
    return result.returncode, result.stdout


def drive_supports_c2(device: str) -> bool:
    """True iff ``accudisc features`` reports the drive both advertises AND
    functionally supports C2 (exit 0 == verdict C2_SUPPORTED). Best-effort: any
    failure (binary missing, probe error) → False, so the pipeline degrades to
    the plain cdrdao read-cd path."""
    probe = _run_features(device)
    return probe is not None and probe[0] == 0


def probe_combos(device: str) -> dict[str, bool]:
    """Per-combination READ CD support from the ``features`` smoke probe.

    Returns e.g. ``{"c2": True, "sub_raw": True, "c2+sub_raw": True, ...}`` — the
    ``c2+sub_raw`` key gates the single-pass audio+C2+subchannel capture. The
    ``combo <name> ok|failed`` line format is byte-compatible with c2read. Empty
    dict when the probe is unavailable.
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


def _run_read(
    cmd: list[str],
    progress_cb: Callable[[int, int], None] | None,
    what: str,
) -> None:
    """Run an ``accudisc read`` command, raising RuntimeError on a fatal exit.

    Shared by :func:`read_disc_c2` and :func:`read_span`. Exit 0 (clean) and 3
    (completed with caveats — hard/suspect/residual-C2, all zero-filled and
    reported) both mean the image was delivered, so neither raises; 1 (usage) and
    2 (fatal device/transport) do. Exit 0 is not verification — AR/CTDB gating is
    the caller's job.
    """
    try:
        if progress_cb is None:
            # -q silences the human stderr line; capture the rest for logging.
            result = subprocess.run(  # noqa: S603 — snapshot/PATH binary, fixed argv
                [*cmd, "-q"], capture_output=True, check=False
            )
            returncode = result.returncode
            stderr_text = result.stderr.decode(errors="replace")
        else:
            returncode, stderr_text = _run_with_progress(cmd, progress_cb)
    except FileNotFoundError:
        msg = "accudisc not found — snapshot to tools/accudisc/ or put it on $PATH"
        raise RuntimeError(msg) from None
    if returncode not in (0, 3):
        msg = f"accudisc {what} failed (exit {returncode}): {stderr_text.strip()}"
        raise RuntimeError(msg)
    log.debug("accudisc %s (exit %d): %s", what, returncode, stderr_text.strip())


def read_disc_c2(
    device: str,
    output_pcm: Path,
    output_c2: Path,
    output_sub: Path | None = None,
    output_cdtext: Path | None = None,
    output_fulltoc: Path | None = None,
    read_speed: int | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
) -> None:
    """Full-disc raw audio (s16le, no byte-swap) + C2 bitmap via ``accudisc read``.

    *output_sub*, when given, additionally captures the raw P-W subchannel stream
    (96 B/sector) in the same pass; *output_cdtext* / *output_fulltoc* are captured
    **inline** on the same read (``--cdtext`` / ``--fulltoc``, single spin-up). A
    disc without CD-Text simply produces no cdtext file (absence does not affect
    the read's exit code) — check for existence. *progress_cb(done, total)*
    receives sector counts from the ``--progress-fd`` machine channel.

    Raises RuntimeError only on a genuine read failure (exit 1/2); exit 3
    (completed with caveats) is not a failure."""
    # Whole-disc audio + inline lead-in dumps: `read` with no --count defaults
    # through the lead-out.
    cmd = [
        _ACCUDISC,
        "--device",
        device,
        "read",
        "--pcm",
        str(output_pcm),
        "--c2f",
        str(output_c2),
    ]
    if output_sub is not None:
        cmd += ["--sub", "raw", "--subf", str(output_sub)]
    if output_fulltoc is not None:
        cmd += ["--fulltoc", str(output_fulltoc)]
    if output_cdtext is not None:
        cmd += ["--cdtext", str(output_cdtext)]
    if read_speed:
        cmd += ["--speed", str(read_speed)]
    _run_read(cmd, progress_cb, "read")


def read_span(
    device: str,
    start_lba: int,
    count: int,
    output_pcm: Path,
    read_speed: int | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
) -> None:
    """Targeted raw read of ``[start_lba, start_lba + count)`` sectors (s16le PCM only,
    no C2/sub capture) — the AR-recovery re-read primitive. Speed is set per invocation
    (``--speed``) and NOT restored by AccuDisc, so a recovery sweep can step the ladder
    without re-spinning between attempts; the caller restores once after.

    Same exit contract as :func:`read_disc_c2` (0/3 = completed; hard-unreadable
    sectors arrive zero-filled, so the output is always exactly ``count`` sectors
    long)."""
    cmd = [
        _ACCUDISC,
        "--device",
        device,
        "read",
        "--start",
        str(start_lba),
        "--count",
        str(count),
        "--pcm",
        str(output_pcm),
    ]
    if read_speed:
        cmd += ["--speed", str(read_speed)]
    _run_read(cmd, progress_cb, "span read")


def _run_with_progress(
    cmd: list[str], progress_cb: Callable[[int, int], None]
) -> tuple[int, str]:
    """Run ``accudisc read`` with the ``--progress-fd`` machine channel on stdout.

    We add ``-q --progress-fd 1``: ``-q`` mutes the human ``\\r`` stderr line, and
    ``--progress-fd 1`` emits newline-delimited ``progress <done> <total>`` tokens
    (plus a final ``summary …`` line) on stdout, which we iterate line by line and
    forward to *progress_cb*. stderr goes to a temp file (not a pipe) so a heavily
    damaged disc printing many log lines can never deadlock the single-threaded
    stdout reader; it is read back afterwards for error detail.
    """
    with tempfile.TemporaryFile() as err_fp:
        proc = subprocess.Popen(  # noqa: S603 — snapshot/PATH binary, fixed argv
            [*cmd, "-q", "--progress-fd", "1"],
            stdout=subprocess.PIPE,
            stderr=err_fp,
            text=True,
        )
        assert proc.stdout is not None  # noqa: S101 — guaranteed by stdout=PIPE
        for line in proc.stdout:
            parts = line.split()
            if len(parts) == 3 and parts[0] == "progress":
                try:
                    progress_cb(int(parts[1]), int(parts[2]))
                except ValueError:  # pragma: no cover — malformed token
                    log.debug("accudisc: unparseable progress line %r", line)
            elif parts and parts[0] == "summary":
                log.debug("accudisc read %s", line.strip())
        proc.wait()
        err_fp.seek(0)
        stderr_text = err_fp.read().decode(errors="replace")
    return proc.returncode, stderr_text


def park_spindle(device: str) -> None:
    """Best-effort spindle stop (``accudisc stop`` → SCSI START STOP UNIT) once done
    reading, so a finished pass doesn't leave the drive spinning. Never raises."""
    try:
        subprocess.run(  # noqa: S603 — snapshot/PATH binary, fixed argv
            [_ACCUDISC, "--device", device, "stop"],
            capture_output=True,
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        log.debug("accudisc stop failed for %s: %s", device, exc)
