"""
disc_reader.py — CD-DA ripping via cd-paranoia subprocess.

Public interface:
    RipInfo(disc, track_lsns, disc_last_lsn)
    rip_disc(device, output_pcm, *, paranoia, read_offset, read_speed, max_retries, never_skip, progress_cb) -> RipInfo
    rip_single_track(device, track_num, output_pcm, *, paranoia, read_offset, read_speed, max_retries, never_skip, progress_cb) -> int
    rip_sector_range(device, track_num, start_frame, end_frame, output_pcm, *, paranoia, read_offset, read_speed, max_retries, never_skip) -> int
    query_disc(device) -> (disc_first, disc_last, [(num, first_lsn, length), ...])
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Callable

    from cdda2img.cdrdao_progress import ProgressUpdate
    from cdda2img.rbi_format import RBIDisc

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# cd-paranoia command-line flags per paranoia mode
_PARANOIA_FLAGS: dict[str, list[str]] = {
    "off": ["-Z"],  # disable all checking — fastest
    "overlap": ["-Y"],  # overlap+verify only — standard jitter correction
    "full": [],  # full paranoia with retry cap (slowest, best for damaged discs)
}

# Matches track table rows from 'cd-paranoia -Q' output (all on stderr).
# Example: "  1.    24337 [05:24.37]        0 [00:00.00]    no   no  2"
_TRACK_RE = re.compile(r"^\s+(\d+)\.\s+(\d+)\s+\[[\d:.]+\]\s+(\d+)")

# TUI progress on the fallback path is driven by cd-paranoia's machine-readable
# callback stream, enabled with -e ("callscript"). Each callback event prints one
# line to stderr (unbuffered in C — no flush lag):
#     "##: <fn> [<name>] @ <wordpos>"
# We follow the WROTE frontier (fn == _CB_WROTE): the committed-output sector,
# emitted once per verified sector, so the bar advances per-sector (smooth) yet
# stays honest — during a paranoia stall no WROTE fires and the bar holds, while
# read/verify/readerr lines keep arriving so the stall is visible as activity.
# <wordpos> is an absolute position in 16-bit words; LSN = wordpos // CD_FRAMEWORDS
# (cd-paranoia.c computes the same `sector = inpos / CD_FRAMEWORDS`).
# This supersedes output-file-size polling, which was coarse: cd-paranoia buffers
# output in 32 KiB (~14-sector) chunks (buffering_write OUTBUFSZ), so on a slow
# read the file size — and thus the bar — stair-stepped.
_CD_FRAMEWORDS = 1176  # 16-bit words per CD frame (2352 bytes / 2)
_CD_FRAME_BYTES = _CD_FRAMEWORDS * 2  # 2352 bytes per CD frame (raw s16le)
_CB_WROTE = 14  # paranoia_cb_mode_t: a verified sector was committed to output
_CALLSCRIPT_RE = re.compile(r"^##:\s*(-?\d+)\s+\[[^\]]*\]\s+@\s+(-?\d+)")

# paranoia_cb_mode_t codes that count as recovery work, mapped to a short bucket name
# for the live tally. ONLY correction/trouble events — plain read (0) / verify (1) /
# overlap (9) are normal flow and are not counted, so a clean track shows a smooth bar
# with no recovery chatter. (callback_strings order in cd-paranoia.c.)
_RECOVERY_BUCKETS: dict[int, str] = {
    2: "jitter",  # FIXUP_EDGE
    3: "jitter",  # FIXUP_ATOM
    4: "scratch",
    6: "skipped",
    10: "dropped",
    11: "duped",
    12: "read err",
    13: "cache err",
}
# Display order for the tally: most serious first, so data-loss buckets stay visible even
# when the line is long. Buckets with a zero count are omitted.
_RECOVERY_ORDER = (
    "skipped",
    "read err",
    "cache err",
    "scratch",
    "dropped",
    "duped",
    "jitter",
)

# Why a cumulative tally, not a dwelling label: the corrections arrive in fast in-memory
# sweeps (thousands of events in <100ms, between two TUI refreshes) — the same blind spot
# as the WROTE commit bursts. The only phase the TUI reliably catches is the slow drive
# reads (cd-paranoia emits [read] at ~75/s at 1x). So we DON'T try to flash a label during
# the correction sweep; we accumulate per-bucket counts for the whole track and render them
# on the read-head line that already survives. When the committed-output (WROTE) frontier
# hasn't advanced for this many callback events, we treat it as a stall and emit
# "reading @ sector N" — or, once any recovery has happened, "recovering @ sector N — N
# jitter, …" with the running totals. The tally is cumulative and never reset (resetting on
# WROTE would zero it inside the same burst that hides it), so every visible read-hold shows
# the totals climbing. The count returns the instant the frontier advances (a commit).
# _STALL_EVENTS must exceed the normal non-WROTE gap during a commit run; the initial
# read-ahead legitimately trips it (bar at 0%, "reading …" instead of looking frozen).
_STALL_EVENTS = 8

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class RipInfo(NamedTuple):
    """Return type of rip_disc — carries both the disc skeleton and raw TOC data."""

    disc: RBIDisc
    track_lsns: list[int]  # absolute first_lsn for each track, needed for CDDB lookup
    disc_last_lsn: int


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def query_disc(device: str) -> tuple[int, int, list[tuple[int, int, int]]]:
    """Run 'cd-paranoia -Q' and return (disc_first, disc_last, [(num, first_lsn, length), ...]).

    disc_first is the first sector of track 1 (index 01); disc_last is the last sector of
    the final audio track. All -Q output goes to stderr; stdout is empty.
    """
    try:
        result = subprocess.run(  # noqa: S603  # LINT-012
            ["cd-paranoia", "-Q", "-d", device],  # noqa: S607  # LINT-012
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        msg = "cd-paranoia not found — install cdparanoia"
        raise RuntimeError(msg) from None

    tracks: list[tuple[int, int, int]] = []
    for line in result.stderr.splitlines():
        m = _TRACK_RE.match(line)
        if m:
            tracks.append((int(m.group(1)), int(m.group(3)), int(m.group(2))))

    if not tracks:
        snippet = result.stderr[:500] or result.stdout[:500]
        msg = f"cd-paranoia -Q returned no tracks for {device!r} — no disc or drive error:\n{snippet}"
        raise RuntimeError(msg)

    disc_first = tracks[0][1]
    disc_last = tracks[-1][1] + tracks[-1][2] - 1
    return disc_first, disc_last, tracks


def _build_rbi_disc(disc_first: int, tracks: list[tuple[int, int, int]]) -> RBIDisc:
    """Build a skeleton RBIDisc from (num, first_lsn, length) track tuples.

    start_frame and pregap_frames are relative to disc_first (track 1 INDEX 01).
    Titles are left empty for _finalize_import to fill from CDDB/MB.
    """
    from cdda2img.rbi_format import RBIDisc, RBITocEntry

    entries = []
    for i, (num, first_lsn, length) in enumerate(tracks):
        if i == 0:
            start_frame = 0
            pregap = 0
        else:
            _, prev_first, prev_len = tracks[i - 1]
            start_frame = (prev_first + prev_len) - disc_first
            pregap = first_lsn - (prev_first + prev_len)
        entries.append(
            RBITocEntry(
                track_number=num,
                title="",
                performer="",
                start_frame=start_frame,
                duration_frames=length,
                pregap_frames=max(0, pregap),
            )
        )
    return RBIDisc(album="", artist="", tracks=entries)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def _retry_flags(max_retries: int | None, never_skip: bool) -> list[str]:
    """cd-paranoia ``-z`` (per-block retry budget = attempts per frame).

    cd-paranoia takes ``-z`` as an *optional-argument* flag: bare ``-z`` =
    never-skip (unlimited retries — cd-paranoia never abandons a sector), and the
    attached form ``-zN`` = N retries before giving up (default 20). *never_skip*
    takes precedence over *max_retries*; both unset ⇒ no flag ⇒ the default.
    """
    if never_skip:
        return ["-z"]
    if max_retries is not None:
        return [f"-z{max_retries}"]
    return []


def rip_disc(
    device: str,
    output_pcm: Path,
    *,
    paranoia: str = "overlap",
    read_offset: int = 0,
    read_speed: int | None = None,
    max_retries: int | None = None,
    never_skip: bool = False,
    progress_cb: Callable[[ProgressUpdate], None] | None = None,
) -> RipInfo:
    """Rip all audio from *device* to *output_pcm* (raw s16le PCM).

    *paranoia* controls read quality:
      "off"     — single raw read, no correction (fastest)
      "overlap" — overlap + verify, standard jitter correction (default)
      "full"    — full paranoia with retry cap (slowest, best for damaged discs)

    *read_offset* is applied via cd-paranoia's ``-O`` flag so the output PCM
    is offset-corrected at rip time (corrected audio stored directly in the RBI).

    *read_speed*, when given, forces the drive read speed via ``-S`` (e.g. ``1``
    for 1x). A slower read gives the drive's error correction more time per
    sector, reducing uncorrectable errors on a damaged disc. None leaves the
    drive at its default (fastest) speed. The fallback call sites pass ``1`` —
    they only run when the primary cdrdao rip already failed, so the disc is
    suspect and accuracy outranks speed.

    *max_retries* / *never_skip* control cd-paranoia's per-block retry budget via
    ``-z`` (see :func:`_retry_flags`): the number of attempts per frame before it
    gives up on a sector. None / False leaves the default (20). *never_skip* forces
    unlimited retries (cd-paranoia never abandons a sector) — use with care, it can
    hang forever on a physically unreadable sector.

    *progress_cb*, when given, receives a :class:`ProgressUpdate` parsed from
    cd-paranoia's -e callback stream (see :func:`_run_paranoia_with_progress`) —
    used to drive the TUI progress bar on the fallback path. When None,
    cd-paranoia's output passes through to the terminal directly (unchanged).

    Returns a RipInfo with the skeleton RBIDisc and raw TOC data for CDDB lookup.
    """
    from cdda2img.container import wav_to_raw_pcm

    disc_first, disc_last, tracks = query_disc(device)
    log.debug(
        "disc: first=%d last=%d sectors=%d tracks=%d",
        disc_first,
        disc_last,
        disc_last - disc_first + 1,
        len(tracks),
    )

    mode_flags = _PARANOIA_FLAGS.get(paranoia, _PARANOIA_FLAGS["overlap"])
    offset_flags = ["-O", str(read_offset)] if read_offset != 0 else []
    speed_flags = ["-S", str(read_speed)] if read_speed is not None else []
    retry_flags = _retry_flags(max_retries, never_skip)
    wav_path = output_pcm.with_suffix(".paranoia.wav")
    cmd = [
        "cd-paranoia",
        "-d",
        device,
        *mode_flags,
        *offset_flags,
        *speed_flags,
        *retry_flags,
        "--",
        "1-",
        str(wav_path),
    ]  # LINT-012
    total_sectors = disc_last - disc_first + 1
    try:
        if progress_cb is None:
            returncode = subprocess.run(cmd).returncode  # noqa: S603  # LINT-012
        else:
            returncode = _run_paranoia_with_progress(
                cmd, wav_path, total_sectors, tracks, disc_first, progress_cb
            )
        if returncode != 0:
            msg = (
                f"cd-paranoia exited with code {returncode} — rip failed or incomplete"
            )
            raise RuntimeError(msg)
        wav_to_raw_pcm(wav_path, output_pcm)
    finally:
        wav_path.unlink(missing_ok=True)
        if read_speed is not None:
            from cdda2img import drive_speed

            drive_speed.restore_drive_speed(device)

    disc = _build_rbi_disc(disc_first, tracks)
    track_lsns = [first_lsn for _, first_lsn, _ in tracks]
    return RipInfo(disc=disc, track_lsns=track_lsns, disc_last_lsn=disc_last)


def rip_single_track(
    device: str,
    track_num: int,
    output_pcm: Path,
    *,
    paranoia: str = "full",
    read_offset: int = 0,
    read_speed: int | None = None,
    max_retries: int | None = None,
    never_skip: bool = False,
    progress_cb: Callable[[ProgressUpdate], None] | None = None,
) -> int:
    """Rip one track by track number to raw s16le PCM. Returns sector count.

    *read_offset* is applied via ``-O`` so the output is offset-corrected,
    matching the coordinate system of a PCM file already processed by
    ``apply_offset``. Does not call ``query_disc`` to determine overall disc
    layout — only queries enough to locate the requested track.

    *read_speed*, when given, forces the drive read speed via ``-S`` (1 = 1x);
    None leaves it at the drive default. The AR-failure retry passes ``1`` — the
    track already failed verification, so a slow, careful re-read is warranted.

    *max_retries* / *never_skip* set cd-paranoia's per-block retry budget via
    ``-z`` (see :func:`_retry_flags`) — the attempts per frame before a sector is
    abandoned. The AR-failure retry is the natural place to spend a larger budget.
    """
    from cdda2img.container import wav_to_raw_pcm

    _, _, tracks = query_disc(device)
    track_entry = next((t for t in tracks if t[0] == track_num), None)
    if track_entry is None:
        msg = f"Track {track_num} not found on disc ({len(tracks)} tracks detected)"
        raise RuntimeError(msg)
    _, track_first_lsn, track_length = track_entry

    mode_flags = _PARANOIA_FLAGS.get(paranoia, _PARANOIA_FLAGS["overlap"])
    offset_flags = ["-O", str(read_offset)] if read_offset != 0 else []
    speed_flags = ["-S", str(read_speed)] if read_speed is not None else []
    retry_flags = _retry_flags(max_retries, never_skip)
    wav_path = output_pcm.with_suffix(".paranoia.wav")
    cmd = [
        "cd-paranoia",
        "-d",
        device,
        *mode_flags,
        *offset_flags,
        *speed_flags,
        *retry_flags,
        "--",
        str(track_num),
        str(wav_path),
    ]  # LINT-012
    try:
        if progress_cb is None:
            returncode = subprocess.run(cmd).returncode  # noqa: S603  # LINT-012
        else:
            returncode = _run_paranoia_with_progress(
                cmd, wav_path, track_length, tracks, track_first_lsn, progress_cb
            )
        if returncode != 0:
            msg = f"cd-paranoia exited with code {returncode} ripping track {track_num}"
            raise RuntimeError(msg)
        wav_to_raw_pcm(wav_path, output_pcm)
    finally:
        wav_path.unlink(missing_ok=True)
        if read_speed is not None:
            from cdda2img import drive_speed

            drive_speed.restore_drive_speed(device)

    return track_length


def rip_sector_range(
    device: str,
    track_num: int,
    start_frame: int,
    end_frame: int,
    output_pcm: Path,
    *,
    paranoia: str = "full",
    read_offset: int = 0,
    read_speed: int | None = None,
    max_retries: int | None = None,
    never_skip: bool = False,
) -> int:
    """Re-rip an exact frame range *within* one track to raw s16le PCM.

    *start_frame* / *end_frame* are sector (frame) offsets relative to the start
    of track *track_num*. The span is given to cd-paranoia as
    ``N:[.start]-N:[.end]``: the ``[.k]`` form is a raw sector offset into the
    track (75 sectors/s — see the cd-paranoia(1) SPAN ARGUMENT grammar). This
    recovers only the damaged frames of a track that failed AccurateRip instead of
    re-ripping the whole track (the AccurateRip frame-450 partial-verify and the
    cd-paranoia callback's READERR positions can pinpoint the bad region).

    cd-paranoia's bracket end-bound inclusivity is its own (the produced length may
    be ``end - start`` or ``end - start + 1`` frames), so this returns the **actual**
    frame count produced from the output PCM size rather than the requested width —
    the caller splices by what was delivered, not by an assumed bound.
    """
    from cdda2img.container import wav_to_raw_pcm

    if not 0 <= start_frame < end_frame:
        msg = f"invalid frame range [{start_frame}, {end_frame}) for track {track_num}"
        raise ValueError(msg)

    mode_flags = _PARANOIA_FLAGS.get(paranoia, _PARANOIA_FLAGS["overlap"])
    offset_flags = ["-O", str(read_offset)] if read_offset != 0 else []
    speed_flags = ["-S", str(read_speed)] if read_speed is not None else []
    retry_flags = _retry_flags(max_retries, never_skip)
    span = f"{track_num}:[.{start_frame}]-{track_num}:[.{end_frame}]"
    wav_path = output_pcm.with_suffix(".paranoia.wav")
    cmd = [
        "cd-paranoia",
        "-d",
        device,
        *mode_flags,
        *offset_flags,
        *speed_flags,
        *retry_flags,
        "--",
        span,
        str(wav_path),
    ]  # LINT-012
    try:
        returncode = subprocess.run(cmd).returncode  # noqa: S603  # LINT-012
        if returncode != 0:
            msg = (
                f"cd-paranoia exited with code {returncode} ripping "
                f"track {track_num} frames [{start_frame}, {end_frame})"
            )
            raise RuntimeError(msg)
        wav_to_raw_pcm(wav_path, output_pcm)
    finally:
        wav_path.unlink(missing_ok=True)
        if read_speed is not None:
            from cdda2img import drive_speed

            drive_speed.restore_drive_speed(device)

    return output_pcm.stat().st_size // _CD_FRAME_BYTES


def _sector_to_track(tracks: list[tuple[int, int, int]], sector: int) -> int:
    """Map an absolute LSN to its 1-based track number (clamped to the disc).

    *tracks* is the (num, first_lsn, length) list from :func:`query_disc`.
    """
    track = tracks[0][0]
    for num, first_lsn, length in tracks:
        if sector >= first_lsn:
            track = num
        if first_lsn <= sector < first_lsn + length:
            return num
    return track


def _recovery_summary(recovery: Counter[str]) -> str:
    """Render the per-bucket recovery tally as "N jitter, M read err", or "" if empty.

    Buckets are emitted in :data:`_RECOVERY_ORDER` (most serious first), omitting zeros,
    so a clean stretch produces no summary and the read line stays "reading @ sector N".
    """
    parts = [f"{recovery[b]} {b}" for b in _RECOVERY_ORDER if recovery.get(b)]
    return ", ".join(parts)


@dataclass
class _ParanoiaProgress:
    """Folds the cd-paranoia ``-e`` callback stream into bar position + status note.

    ``feed(fn, abs_sector)`` updates state for one callback event and returns True
    when the display should be re-emitted. The WROTE frontier (``elapsed``) is
    monotonic. When it stalls — no advance for :data:`_STALL_EVENTS` callback events —
    the ``note`` narrates the live read head: ``reading @ sector N`` normally, or
    ``recovering @ sector N — 2513 jitter, …`` once any recovery work has happened, the
    running per-bucket totals in :attr:`recovery`. The tally is cumulative across the
    track and never reset, because the corrections that feed it arrive in fast bursts the
    TUI can't catch — only the slow read-holds are visible, so the read line carries the
    totals (see the module note on :data:`_STALL_EVENTS`). The note clears to the plain
    frame count the instant the frontier advances again (a commit).
    """

    disc_first: int
    elapsed: int = 0
    note: str = ""
    read_head: int = 0
    since_advance: int = 0  # callback events since the WROTE frontier last advanced
    recovery: Counter[str] = field(
        default_factory=Counter
    )  # bucket -> cumulative count

    def feed(self, fn: int, abs_sector: int) -> bool:
        if fn == _CB_WROTE:
            if abs_sector - self.disc_first <= self.elapsed:
                return False  # non-advancing / backward WROTE — ignore
            self.elapsed = abs_sector - self.disc_first
            self.since_advance = 0
            self.note = ""  # frontier advanced — committing, show the count
            return True
        # non-WROTE event (read / verify / correction / error / overlap …)
        self.since_advance += 1
        self.read_head = max(self.read_head, abs_sector)
        bucket = _RECOVERY_BUCKETS.get(fn)
        if bucket:
            self.recovery[bucket] += 1
        if self.since_advance < _STALL_EVENTS:
            return False  # not a stall yet — bar is moving, stay on the count
        summary = _recovery_summary(self.recovery)
        line = (
            f"recovering @ sector {self.read_head} — {summary}"
            if summary
            else f"reading @ sector {self.read_head}"
        )
        if line != self.note:
            self.note = line
            return True
        return False


def _run_paranoia_with_progress(
    cmd: list[str],
    wav_path: Path,
    total_sectors: int,
    tracks: list[tuple[int, int, int]],
    disc_first: int,
    progress_cb: Callable[[ProgressUpdate], None],
) -> int:
    """Run cd-paranoia with -e, emitting ProgressUpdate from its callback stream.

    Injects -e ("callscript") so cd-paranoia streams a machine-readable callback
    line per event to stderr; we follow the WROTE frontier (committed-output
    sector) for smooth, honest per-sector progress (see the module-level note on
    _CALLSCRIPT_RE). Recovery work (corrections/errors) is accumulated into a
    cumulative per-bucket tally (:class:`_ParanoiaProgress`) and rendered on the
    read-head line during a stall — "recovering @ sector N — 2513 jitter, …" — since
    that line is the only phase the TUI reliably catches; the bar reverts to the plain
    frame count the instant the frontier advances. The bar position never moves on a
    trouble event. stdout is discarded; the audio still streams to *wav_path* (the file
    argument), unaffected by -e. stderr is drained line-by-line so the PIPE never fills.
    *wav_path* is unused now but kept for call-site symmetry. Returns the exit code.
    """
    from cdda2img.cdrdao_progress import ProgressUpdate

    n_tracks = len(tracks)

    def emit(sectors_done: int, note: str = "") -> None:
        sectors_done = max(0, min(sectors_done, total_sectors))
        progress_cb(
            ProgressUpdate(
                track=_sector_to_track(tracks, disc_first + sectors_done),
                n_tracks=n_tracks,
                elapsed_frames=sectors_done,
                total_frames=total_sectors,
                note=note,
            )
        )

    # Opt-in ground-truth capture: tee every raw -e line to this file so a rip that
    # exercises real recovery can be replayed offline (tools/replay_paranoia_progress.py)
    # without reproducing a flaky read. No-op unless the env var is set.
    capture_path = os.environ.get("CDDA2IMG_PARANOIA_CAPTURE")
    capture = open(capture_path, "a") if capture_path else None  # noqa: SIM115

    state = _ParanoiaProgress(disc_first)
    proc = subprocess.Popen(  # noqa: S603  # LINT-012
        [cmd[0], "-e", *cmd[1:]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    stream = proc.stderr
    if stream is not None:
        for line in stream:
            if capture is not None:
                capture.write(line)
            m = _CALLSCRIPT_RE.match(line)
            if m is None:
                continue
            if state.feed(int(m.group(1)), int(m.group(2)) // _CD_FRAMEWORDS):
                emit(state.elapsed, state.note)

    if capture is not None:
        capture.close()
    rc = proc.wait()
    if rc == 0:
        emit(total_sectors)  # close the bar at 100%
    return rc
