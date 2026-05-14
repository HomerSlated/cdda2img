"""
disc_reader.py — CD-DA ripping via cd-paranoia subprocess.

Public interface:
    RipInfo(disc, track_lsns, disc_last_lsn)
    rip_disc(device, output_pcm, *, paranoia="overlap") -> RipInfo
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
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


def _query_disc(device: str) -> tuple[int, int, list[tuple[int, int, int]]]:
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
    drive_offset: int = 0,
) -> RipInfo:
    """Rip all audio from *device* to *output_pcm* (raw s16le PCM).

    *paranoia* controls read quality:
      "off"     — single raw read, no correction (fastest)
      "overlap" — overlap + verify, standard jitter correction (default)
      "full"    — full paranoia with retry cap (slowest, best for damaged discs)

    *drive_offset* is applied via cd-paranoia's ``-O`` flag so the output PCM
    is offset-corrected at rip time (corrected audio stored directly in the RBI).

    Returns a RipInfo with the skeleton RBIDisc and raw TOC data for CDDB lookup.
    cd-paranoia progress output passes through to the terminal directly.
    """
    from cdda2img.container import wav_to_raw_pcm

    disc_first, disc_last, tracks = _query_disc(device)
    log.debug(
        "disc: first=%d last=%d sectors=%d tracks=%d",
        disc_first,
        disc_last,
        disc_last - disc_first + 1,
        len(tracks),
    )

    mode_flags = _PARANOIA_FLAGS.get(paranoia, _PARANOIA_FLAGS["overlap"])
    offset_flags = ["-O", str(drive_offset)] if drive_offset != 0 else []
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
    try:
        result = subprocess.run(cmd)  # noqa: S603  # LINT-012
        if result.returncode != 0:
            msg = f"cd-paranoia exited with code {result.returncode} — rip failed or incomplete"
            raise RuntimeError(msg)
        wav_to_raw_pcm(wav_path, output_pcm)
    finally:
        wav_path.unlink(missing_ok=True)

    disc = _build_rbi_disc(disc_first, tracks)
    track_lsns = [first_lsn for _, first_lsn, _ in tracks]
    return RipInfo(disc=disc, track_lsns=track_lsns, disc_last_lsn=disc_last)
