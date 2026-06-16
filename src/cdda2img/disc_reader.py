"""
disc_reader.py — CD-DA ripping via cd-paranoia subprocess.

Public interface:
    RipInfo(disc, track_lsns, disc_last_lsn)
    rip_disc(device, output_pcm, *, paranoia="overlap") -> RipInfo
    rip_single_track(device, track_num, output_pcm, *, paranoia, read_offset, progress_cb) -> int
    query_disc(device) -> (disc_first, disc_last, [(num, first_lsn, length), ...])
"""

from __future__ import annotations

import logging
import re
import subprocess
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

# TUI progress on the fallback path is derived from the growing output WAV's
# size, not cd-paranoia's stderr meter (which redraws one line with '\r' and
# reports non-monotonic sectors during paranoia re-reads — unparseable
# line-by-line). File size is monotonic, format-agnostic, version-proof; it
# requires only that cd-paranoia stream the WAV, which it does.
_WAV_HEADER_BYTES = 44
_CD_FRAME_BYTES = 2352  # one CD frame = 588 stereo s16 sample pairs
_PARANOIA_POLL_S = 0.3

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


def rip_disc(
    device: str,
    output_pcm: Path,
    *,
    paranoia: str = "overlap",
    read_offset: int = 0,
    progress_cb: Callable[[ProgressUpdate], None] | None = None,
) -> RipInfo:
    """Rip all audio from *device* to *output_pcm* (raw s16le PCM).

    *paranoia* controls read quality:
      "off"     — single raw read, no correction (fastest)
      "overlap" — overlap + verify, standard jitter correction (default)
      "full"    — full paranoia with retry cap (slowest, best for damaged discs)

    *read_offset* is applied via cd-paranoia's ``-O`` flag so the output PCM
    is offset-corrected at rip time (corrected audio stored directly in the RBI).

    *progress_cb*, when given, receives a :class:`ProgressUpdate` derived from the
    growing output-file size (see :func:`_run_paranoia_with_progress`) — used to
    drive the TUI progress bar on the fallback path. When None, cd-paranoia's
    output passes through to the terminal directly (unchanged behaviour).

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
    wav_path = output_pcm.with_suffix(".paranoia.wav")
    cmd = [
        "cd-paranoia",
        "-d",
        device,
        *mode_flags,
        *offset_flags,
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
    progress_cb: Callable[[ProgressUpdate], None] | None = None,
) -> int:
    """Rip one track by track number to raw s16le PCM. Returns sector count.

    *read_offset* is applied via ``-O`` so the output is offset-corrected,
    matching the coordinate system of a PCM file already processed by
    ``apply_offset``. Does not call ``query_disc`` to determine overall disc
    layout — only queries enough to locate the requested track.
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
    wav_path = output_pcm.with_suffix(".paranoia.wav")
    cmd = [
        "cd-paranoia",
        "-d",
        device,
        *mode_flags,
        *offset_flags,
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

    return track_length


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


def _run_paranoia_with_progress(
    cmd: list[str],
    wav_path: Path,
    total_sectors: int,
    tracks: list[tuple[int, int, int]],
    disc_first: int,
    progress_cb: Callable[[ProgressUpdate], None],
) -> int:
    """Run cd-paranoia, emitting ProgressUpdate from the growing output-file size.

    Polls ``wav_path``'s size every _PARANOIA_POLL_S seconds and converts
    bytes → sectors → ProgressUpdate, rather than parsing cd-paranoia's stderr
    meter. stdout/stderr are discarded: the meter is unparseable, and an
    undrained PIPE would deadlock the child once its buffer fills. Returns the
    process exit code.
    """
    from cdda2img.cdrdao_progress import ProgressUpdate

    n_tracks = len(tracks)

    def emit(sectors_done: int) -> None:
        sectors_done = max(0, min(sectors_done, total_sectors))
        progress_cb(
            ProgressUpdate(
                track=_sector_to_track(tracks, disc_first + sectors_done),
                n_tracks=n_tracks,
                elapsed_frames=sectors_done,
                total_frames=total_sectors,
            )
        )

    proc = subprocess.Popen(  # noqa: S603  # LINT-012
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    while True:
        try:
            proc.wait(timeout=_PARANOIA_POLL_S)
        except subprocess.TimeoutExpired:
            try:
                size = wav_path.stat().st_size
            except OSError:
                continue
            emit((size - _WAV_HEADER_BYTES) // _CD_FRAME_BYTES)
        else:
            break

    if proc.returncode == 0:
        emit(total_sectors)  # close the bar at 100%
    return proc.returncode
