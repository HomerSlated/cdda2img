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

import contextlib
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
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


# ── TOC geometry (READ TOC, with the 0x02 → 0x00 degrade) ─────────────────────


@dataclass
class TocGeometry:
    """Track boundaries from ``accudisc toc``, plus how they were obtained.

    ``accudisc toc`` prefers READ TOC format 0x02 (the lead-in) and falls back to
    format 0x00 (the drive's cooked track list) when the lead-in cannot be read,
    reporting which served the answer. The ``track``/``leadout`` lines are
    identical either way — AccuDisc cross-checked both decodes of the same disc
    byte-for-byte — so *geometry does not depend on the path*. What the fallback
    loses is **session structure**, which is why :attr:`session_safe` exists.
    """

    track_lsns: list[int]  # audio track start LBAs, in order
    disc_last_lsn: int  # last audio sector (lead-out - 1)
    source: str  # "fulltoc" (0x02) | "toc" (0x00)
    degrade: str  # "none" | leadin_unreadable | leadin_absent | leadin_malformed
    sessions: str | None = None  # "1..1" range — fulltoc only; the lead-in's numbering
    data_tracks: list[int] = field(default_factory=list)  # 1-based, CTRL bit 2
    # READ DISC INFORMATION session count. A separate token from `sessions`, and a
    # different opcode — answered from the drive's disc model, not the groove — so
    # it survives a degrade where the `sessions` range does not. 0 = unknown; None
    # = a pre-count AccuDisc build that never emitted it.
    session_count: int | None = None
    # A malformed lead-in that contradicts itself (copy protection, usually). When
    # `toc_trusted` is False the track map cannot be believed; `anomalies` names why.
    anomalies: list[str] = field(default_factory=list)
    toc_trusted: bool = True

    @property
    def degraded(self) -> bool:
        return self.degrade != "none"

    @property
    def session_safe(self) -> tuple[bool, str]:
        """Whether the session-1-only policy can be applied to this geometry.

        The policy (``subchannel.session1_audio_tracks``) needs per-track session
        membership and session 1's lead-out. Format 0x02 carries both; format
        0x00 carries neither — it returns a flat track list and the **last**
        session's lead-out. On a multi-session disc that lead-out is not session
        1's, so building geometry from it yields a wrong disc ID silently.

        The decision follows AccuDisc's §2026-07-22e/f table verbatim, strongest
        evidence first:

        0. ``toc_trusted`` is False — a self-contradicting lead-in (copy
           protection). The map is unbelievable whatever the sessions say; refuse
           and surface it as needing a human.
        1. ``session_count`` from READ DISC INFORMATION. This is a *separate*
           opcode that does not re-read the lead-in, so it is present on a
           degrade. ``1`` is a measured fact and settles it whatever the tracks
           are; ``>1`` is the multi-session hole and is refused.
        2. No count (a pre-count build, or ``0`` = unknown): fall back to "no data
           track anywhere". This rules out an Enhanced CD (whose session 2 is
           always data) but **not** a multi-session all-audio disc (an audio CD-R
           written in two TAO sessions) — AccuDisc caught that hole in our first
           formulation. So it is an inference, not a measurement, reported as such.
        """
        if not self.toc_trusted:
            detail = ", ".join(self.anomalies) or "self-contradicting lead-in"
            return False, f"untrusted TOC geometry ({detail}) — needs a human"
        if not self.degraded:
            return True, "full TOC"
        if self.session_count == 1:
            return True, "single session (measured)"
        if self.session_count is not None and self.session_count > 1:
            return False, f"{self.session_count} sessions and no session structure"
        if self.data_tracks:
            return False, (
                f"data track(s) {self.data_tracks} and no session structure — "
                "cannot distinguish mixed-mode (refuse) from Enhanced CD (exclude)"
            )
        return True, "all-audio, single session inferred (NOT measured)"


_TRACK_RE = re.compile(r"^track\s+(\d+)\s+lba\s+(-?\d+)\s+sectors\s+(\d+)\s+(\w+)")
_LEADOUT_RE = re.compile(r"^leadout\s+lba\s+(\d+)")


def parse_toc_output(stdout: str) -> TocGeometry:
    """Parse ``accudisc toc`` output. Frozen in AccuDisc's cli-machine-interface.md.

    The acquisition line is ``key=value`` tokens and may gain keys, so it is
    parsed as tokens, never by position.
    """
    lsns: list[int] = []
    data_tracks: list[int] = []
    leadout: int | None = None
    tokens: dict[str, str] = {}

    for line in stdout.splitlines():
        track = _TRACK_RE.match(line)
        if track:
            number, lba, _sectors, kind = track.groups()
            lsns.append(int(lba))
            if kind != "audio":
                data_tracks.append(int(number))
            continue
        tail = _LEADOUT_RE.match(line)
        if tail:
            leadout = int(tail.group(1))
            continue
        if "=" in line:
            for item in line.split():
                key, _, value = item.partition("=")
                if value:
                    tokens[key] = value

    if not lsns or leadout is None:
        msg = f"could not parse accudisc toc output:\n{stdout}"
        raise ValueError(msg)

    session_count: int | None = None
    if "session_count" in tokens:
        with contextlib.suppress(ValueError):
            session_count = int(tokens["session_count"])

    return TocGeometry(
        track_lsns=lsns,
        disc_last_lsn=leadout - 1,
        # Pre-degrade AccuDisc builds emit no acquisition line; they only ever
        # answered from the lead-in, so "fulltoc"/"none" is the honest default.
        source=tokens.get("source", "fulltoc"),
        degrade=tokens.get("degrade", "none"),
        sessions=tokens.get("sessions"),
        data_tracks=data_tracks,
        session_count=session_count,
        # `anomalies=` is absent when clean; `toc_trusted=0` appears only when the
        # geometry is untrusted (its absence means trusted).
        anomalies=tokens["anomalies"].split(",") if tokens.get("anomalies") else [],
        toc_trusted=tokens.get("toc_trusted") != "0",
    )


def read_lead_in(
    device: str, fulltoc_path: Path, cdtext_path: Path | None = None
) -> None:
    """Dump the raw full TOC (and optionally CD-Text) from the lead-in only.

    The two standalone subcommands answer from the lead-in without spinning the
    program area, which is what makes this cheap enough for the pre-rip banner
    (M3, replacing ``cdrdao read-toc --fast-toc``).

    Best-effort by contract: **CD-Text absence is normal** — most discs have
    none — so a missing or failed ``cdtext`` leaves no file and is not an error.
    A failed ``fulltoc`` likewise leaves no file; the caller checks for it rather
    than catching, because every caller of this is cosmetic.
    """
    for sub, out in (("fulltoc", fulltoc_path), ("cdtext", cdtext_path)):
        if out is None:
            continue
        try:
            subprocess.run(  # noqa: S603 — snapshot/PATH binary, fixed argv
                [_ACCUDISC, "--device", device, sub, str(out)],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.debug("accudisc %s failed for %s: %s", sub, device, exc)


def read_toc(device: str) -> TocGeometry:
    """Track geometry via ``accudisc toc``, tolerating an unreadable lead-in.

    Raises RuntimeError when the command itself fails; a *degrade* is a success
    (exit 0) and is reported in the result, not raised — failing it would break
    exactly the discs the fallback exists to serve.
    """
    try:
        proc = subprocess.run(  # noqa: S603 — snapshot/PATH binary, fixed argv
            [_ACCUDISC, "--device", device, "toc"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        msg = f"accudisc toc failed to run: {exc}"
        raise RuntimeError(msg) from exc
    if proc.returncode not in (0, 3):
        msg = f"accudisc toc exited {proc.returncode}: {proc.stderr.strip()}"
        raise RuntimeError(msg)
    geom = parse_toc_output(proc.stdout)
    if not geom.toc_trusted:
        log.warning(
            "TOC geometry is untrusted (anomalies: %s) — the track map contradicts "
            "itself; this usually means copy protection, not damage",
            ", ".join(geom.anomalies) or "unspecified",
        )
    elif geom.degraded:
        log.warning(
            "TOC lead-in unavailable (%s) — geometry from the cooked track list; "
            "session count is %s",
            geom.degrade,
            geom.session_count if geom.session_count else "unknown",
        )
    return geom


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
    output_pcm: Path | None = None,
    output_c2: Path | None = None,
    output_sub: Path | None = None,
    output_cdtext: Path | None = None,
    output_fulltoc: Path | None = None,
    read_speed: int | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
) -> None:
    """Full-disc raw audio (s16le, no byte-swap) + C2 bitmap via ``accudisc read``.

    Every output is opt-in — ``accudisc read`` reads the disc and writes only the
    streams asked for. *output_pcm* / *output_c2* write the audio and C2 bitmap;
    *output_sub* captures the raw P-W subchannel (96 B/sector); *output_cdtext* /
    *output_fulltoc* are captured **inline** on the same read (``--cdtext`` /
    ``--fulltoc``, single spin-up). Omitting *output_pcm* still reads the whole
    disc (the sub stream needs it) but skips the ~600 MB PCM write — the
    metadata-only pass the parity gate uses. A disc without CD-Text simply
    produces no cdtext file (absence does not affect the read's exit code) — check
    for existence. *progress_cb(done, total)* receives sector counts from the
    ``--progress-fd`` machine channel.

    Raises RuntimeError only on a genuine read failure (exit 1/2); exit 3
    (completed with caveats) is not a failure."""
    # Whole-disc read (no --count → through the lead-out); each output is opt-in.
    cmd = [_ACCUDISC, "--device", device, "read"]
    if output_pcm is not None:
        cmd += ["--pcm", str(output_pcm)]
    if output_c2 is not None:
        cmd += ["--c2f", str(output_c2)]
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
