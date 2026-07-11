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
* **CD-Text and full TOC are their own subcommands** (``cdtext`` / ``fulltoc``),
  not flags on the read pass. c2read captured audio+C2+sub+cdtext+fulltoc in one
  invocation; here the two lead-in dumps are separate (instant) reads issued by
  :func:`read_disc_c2`, so its caller contract is unchanged. The ``fulltoc``
  dump is byte-identical to c2read's, so ``subq_toc.parse_fulltoc`` is unaffected.
* **Progress** is a ``\\r``-updated *human* line on **stderr**
  (``  299 / 30000 sectors (1.0%)\\r…``), suppressed by ``-q``. c2read wrote
  machine ``progress <done> <total>`` tokens on stdout. AccuDisc's machine
  interface is the shared-memory status map (the library API); until the
  pipeline moves to that API we parse the stderr line in raw chunks
  (:func:`_run_with_progress`), because ``\\r`` updates are not newline-delimited.
* **Exit contract** — ``0`` = the read completed (hard-unreadable sectors are
  zero-filled and C2 flags are reported in the stderr summary, *not* via the
  code); ``1`` = fatal I/O; ``2`` = usage. AccuDisc never returns c2read's
  non-fatal ``3``, so the acceptance check is simply ``!= 0``.
* A disc with **no CD-Text** makes ``cdtext`` exit non-zero ("response too
  short") and write no file — benign, exactly like c2read's graceful no-op; the
  caller checks for the file's existence.

READ CD returns s16le, so — unlike cdrdao's s16be BIN — the PCM needs no
byte-swap. Hard-unreadable sectors are zero-filled by AccuDisc (C2 bitmap
all-ones), so the PCM/C2/sub streams always stay length-consistent.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
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

# Matches AccuDisc's stderr progress line: "  299 / 30000 sectors (1.0%)".
_PROGRESS_RE = re.compile(rb"^\s*(\d+)\s*/\s*(\d+)\s+sectors")


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

    Shared by :func:`read_disc_c2` and :func:`read_span`. Exit 0 means the read
    completed (any hard errors were zero-filled, C2 flags reported in the stderr
    summary); 1 = fatal I/O, 2 = usage — both raise here.
    """
    try:
        if progress_cb is None:
            # -q silences the stderr progress line; capture the rest for logging.
            result = subprocess.run(  # noqa: S603 — snapshot/PATH binary, fixed argv
                [*cmd, "-q"], capture_output=True, check=False
            )
            returncode = result.returncode
            stderr_text = result.stderr.decode(errors="replace")
        else:
            # Progress must be visible on stderr to parse it, so no -q here.
            returncode, stderr_text = _run_with_progress(cmd, progress_cb)
    except FileNotFoundError:
        msg = "accudisc not found — snapshot to tools/accudisc/ or put it on $PATH"
        raise RuntimeError(msg) from None
    if returncode != 0:
        msg = f"accudisc {what} failed (exit {returncode}): {stderr_text.strip()}"
        raise RuntimeError(msg)
    log.debug("accudisc %s: %s", what, stderr_text.strip())


def _dump_leadin(device: str, subcommand: str, output: Path, *, fatal: bool) -> None:
    """Run a lead-in dump subcommand (``cdtext`` / ``fulltoc``) writing to *output*.

    ``fatal=False`` (CD-Text): a non-zero exit means the disc simply has no
    CD-Text ("response too short") — no file is written and nothing is raised,
    matching c2read's graceful no-op. ``fatal=False`` for ``fulltoc`` too: a
    missing full TOC degrades ``subq_toc`` to TOC-only geometry rather than
    failing the whole rip, so a WARNING is logged instead of raising.
    """
    try:
        result = subprocess.run(  # noqa: S603 — snapshot/PATH binary, fixed argv
            [_ACCUDISC, "--device", device, subcommand, str(output)],
            capture_output=True,
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        log.warning("accudisc %s failed for %s: %s", subcommand, device, exc)
        return
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        if fatal:
            log.warning(
                "accudisc %s (exit %d): %s", subcommand, result.returncode, detail
            )
        else:
            log.debug("accudisc %s: %s (disc likely has none)", subcommand, detail)


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
    (96 B/sector) in the same read pass; *output_cdtext* / *output_fulltoc* are
    captured by separate instant lead-in subcommands (``cdtext`` / ``fulltoc``).
    A disc without CD-Text simply produces no cdtext file — check for existence.
    *progress_cb(done, total)* receives sector counts parsed from AccuDisc's
    stderr progress line.

    Raises RuntimeError on a genuine read failure (exit 1/2). Lead-in dumps are
    best-effort (see :func:`_dump_leadin`)."""
    # Lead-in metadata first (instant), so it is captured even if the long audio
    # read is later interrupted.
    if output_fulltoc is not None:
        _dump_leadin(device, "fulltoc", output_fulltoc, fatal=True)
    if output_cdtext is not None:
        _dump_leadin(device, "cdtext", output_cdtext, fatal=False)

    # Whole-disc audio: `read` with no --count defaults through the lead-out.
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

    Same exit contract as :func:`read_disc_c2` (0 = completed; hard-unreadable
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
    """Run ``accudisc read`` streaming its stderr progress line to the callback.

    AccuDisc's progress is a single ``\\r``-updated line on **stderr** — not
    newline-delimited — so line iteration would buffer the whole thing until the
    trailing summary. We read stderr in raw chunks and split on both ``\\r`` and
    ``\\n``, matching ``<done> / <total> sectors`` and forwarding the counts.
    stdout carries no data in read mode, so it is discarded. Non-progress stderr
    (the trailing ``accudisc read summary`` block) is retained for logging.
    """
    proc = subprocess.Popen(  # noqa: S603 — snapshot/PATH binary, fixed argv
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    assert proc.stderr is not None  # noqa: S101 — guaranteed by stderr=PIPE
    err_fd = proc.stderr.fileno()
    buf = b""
    tail: list[str] = []
    while True:
        # os.read returns bytes-available-now (up to 4096), so a \r-updated
        # line surfaces immediately instead of buffering until the pipe fills.
        chunk = os.read(err_fd, 4096)
        if not chunk:
            break
        buf += chunk
        # Split on either separator; keep the trailing partial fragment in buf.
        fragments = re.split(rb"[\r\n]", buf)
        buf = fragments.pop()
        for frag in fragments:
            m = _PROGRESS_RE.match(frag)
            if m:
                try:
                    progress_cb(int(m.group(1)), int(m.group(2)))
                except ValueError:  # pragma: no cover — regex guarantees ints
                    log.debug("accudisc: unparseable progress %r", frag)
            elif frag.strip():
                tail.append(frag.decode(errors="replace"))
    if buf.strip() and not _PROGRESS_RE.match(buf):
        tail.append(buf.decode(errors="replace"))
    proc.wait()
    return proc.returncode, "\n".join(tail)


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
