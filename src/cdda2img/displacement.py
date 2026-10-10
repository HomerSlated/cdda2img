"""Find audio a capture holds intact but out of place, and put it back.

A drive can drop or repeat a few samples in the middle of a stream and carry on.
Every byte after that point is right and in the wrong place. C2 raises no flag,
because no byte is wrong. Every later track fails AccurateRip, because the sum
weights each sample by its position. CTDB sees every later sample as an error and
declines with damage beyond its capacity.

Measured 2026-10-10 on a clean pressed disc, PX-716A, whole-disc pass at 40x:
tracks 1 to 4 verified, track 5 was DAMAGED (frame-450 matched), tracks 6 to 11
were plain MISMATCH. Tracks 6 to 11 were bit-intact and 12 samples early; the 12
were lost once, inside track 5. The fault is intermittent: the same pass read
clean twice later the same evening.

The check costs no reads. For each track that failed AccurateRip it looks for a
shift at which the track's audio verifies, and re-slices the track from the
capture at that shift.

**Sign.** A *shift* is where the audio was found, in samples relative to where it
belongs: negative means earlier in the capture. The *displacement* reported to
the user and in PROV is ``-shift``: ``+12`` means 12 samples were lost from the
stream before this track's audio, so it arrived 12 samples early.

**Three rules keep a coincidence from being written.**

1. *Our cohort only.* The same master is pressed at different absolute positions
   and each pressing is its own set of dBAR blocks (Tracy Chapman's debut
   verifies at 0, -669, -1333 and -1997). Audio in its right place therefore
   matches another pressing's block at a non-zero shift. Only blocks that matched
   a verified track of this disc at shift 0 are used (:func:`cohort_blocks`), so
   a match at shift ``s`` means displaced by ``s`` and nothing else.
2. *Two independent sums, or two tracks.* A shift is nominated by sweeping the
   frame-450 sum and confirmed by the whole-track sum, which are independent
   32-bit values. Where a track has no frame-450 data the whole-track sum is
   swept instead, and that one value over thousands of shifts is not enough on
   its own: the shift must then be confirmed on a second track.
3. *No further outside the capture.* A window that runs past an end of the
   capture is completed with zeros, which sit in AccurateRip's exclusion zone
   and pass. The last track's own window already does that by the read offset
   (the drive never delivered those samples). A shifted window may reach no
   further past either end than the track's own window does, so a re-slice
   never contains more made-up samples than the track would have had anyway.

The track that contains the slip itself cannot be re-sliced (its head and tail
sit at different shifts, and lost samples are gone). It stays failed and goes to
the re-read ladder.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np

from cdda2img.accuraterip import (
    _CHECK_RADIUS,
    _ar_checksums_arr,
    _as_frames,
    _reference_values,
    _sliding_v1,
    _sweep_window,
    _track_frame_bounds,
    _window,
    match_track_pcm,
)

log = logging.getLogger(__name__)

_FRAME_SAMPLES = 588
_SECTOR_BYTES = 2352

#: Shifts confirmed per check. A nomination is a 32-bit hit, so more than a
#: handful means a degenerate window (a tone, a loop), not a displacement.
_MAX_CANDIDATES = 8

#: ``splice(pcm_fh, corrected, track_lsn, read_offset, file_size)``: the rip's one
#: verified-write primitive (``cdda2img._splice_corrected``), passed in so the
#: clamp at the disc edge exists in one place.
Splice = Callable[[BinaryIO, bytes, int, int, int], None]


def outcome(shift: int) -> str:
    """The ``recovery_track_<n>`` value for a track found at *shift*."""
    return f"displaced@{-shift:+d}"


def failed_track_numbers(results: Sequence[Any]) -> list[int]:
    """Tracks AccurateRip holds and this rip did not match (1-based)."""
    return [
        r.track
        for r in results
        if r.max_confidence is not None
        and r.confidence_v1 is None
        and r.confidence_v2 is None
    ]


def cohort_blocks(
    responses: list[list[dict]], results: Sequence[Any]
) -> list[list[dict]]:
    """The dBAR blocks of this disc's own pressing (rule 1 in the module docstring).

    A block belongs when its checksum for some verified track equals what this
    rip computed for that track at shift 0. With no verified track there is no
    anchor and the result is empty.
    """
    verified = {
        r.track - 1: {int(r.v1_crc, 16), int(r.v2_crc, 16)}
        for r in results
        if r.confidence_v1 is not None or r.confidence_v2 is not None
    }
    return [
        resp
        for resp in responses
        if any(resp[ti]["crc"] in crcs for ti, crcs in verified.items())
    ]


def _nominate(
    a: np.ndarray,
    start: int,
    end: int,
    track_index: int,
    n_tracks: int,
    cohort: list[list[dict]],
    radius: int,
) -> tuple[set[int], bool]:
    """Non-zero shifts at which one track's probe sum matches the cohort.

    Returns ``(shifts, independent)``. *independent* is True when the probe was
    the frame-450 sum, which the whole-track confirmation does not repeat.
    """
    for key in ("crc450", "crc"):
        targets = _reference_values(cohort, track_index, key)
        params = _sweep_window(start, end, track_index, n_tracks, key)
        if not targets or params is None:
            continue
        sweep = _sliding_v1(a, *params, radius)
        shifts = {
            int(i) - radius
            for value in targets
            for i in np.flatnonzero(sweep == np.uint32(value))
        }
        return shifts - {0}, key == "crc450"
    return set(), False


def _reach_ok(start: int, end: int, shift: int, real: tuple[int, int]) -> bool:
    """Rule 3: the shifted window reaches no further outside *real* (the samples
    the drive delivered) than the unshifted one does."""
    lo, hi = real
    return max(0, lo - start - shift) <= max(0, lo - start) and max(
        0, end + shift - hi
    ) <= max(0, end - hi)


def _confirms(
    a: np.ndarray,
    start: int,
    end: int,
    shift: int,
    track_index: int,
    n_tracks: int,
    cohort: list[list[dict]],
    real: tuple[int, int],
) -> bool:
    """Whole-track v1 or v2 at *shift* matches a cohort block (rules 2 and 3)."""
    if not _reach_ok(start, end, shift, real):
        return False
    v1, v2 = _ar_checksums_arr(
        _window(a, start + shift, end - start), track_index + 1, n_tracks
    )
    return any(resp[track_index]["crc"] in (v1, v2) for resp in cohort)


def find_displaced(
    pcm: Path | bytes | np.ndarray,
    track_lsns: list[int],
    disc_last_lsn: int,
    cohort: list[list[dict]],
    failed: Sequence[int],
    read_offset: int,
    *,
    radius: int = _CHECK_RADIUS,
) -> dict[int, int]:
    """``{track: shift}`` for each failed track whose audio verifies out of place.

    *pcm* is the whole-disc capture in the raw domain; *read_offset* is the
    drive's, so shift 0 is where :func:`accuraterip.verify_rip` already looked.
    *failed* are 1-based track numbers. Reads nothing from the drive and writes
    nothing.
    """
    if not cohort or not failed:
        return {}
    a = _as_frames(pcm)
    n_tracks = len(track_lsns)
    bounds = [
        (s + read_offset, e + read_offset)
        for s, e in _track_frame_bounds(track_lsns, disc_last_lsn)
    ]

    own: dict[int, set[int]] = {}  # track -> shifts its frame-450 sum nominated
    votes: Counter[int] = Counter()
    for t in failed:
        start, end = bounds[t - 1]
        shifts, independent = _nominate(a, start, end, t - 1, n_tracks, cohort, radius)
        votes.update(shifts)
        own[t] = shifts if independent else set()
    pool = sorted(votes, key=lambda s: (-votes[s], abs(s)))[:_MAX_CANDIDATES]

    real = (0, len(a))
    confirmed: dict[int, list[int]] = {}
    for t in failed:
        start, end = bounds[t - 1]
        confirmed[t] = [
            s
            for s in pool
            if _confirms(a, start, end, s, t - 1, n_tracks, cohort, real)
        ]
    tracks_at: Counter[int] = Counter(s for hits in confirmed.values() for s in hits)

    found: dict[int, int] = {}
    for t, hits in confirmed.items():
        accepted = [s for s in hits if s in own[t] or tracks_at[s] >= 2]
        if accepted:
            found[t] = min(accepted, key=abs)
    return found


def _read_padded(src: BinaryIO, lo: int, length: int, file_size: int) -> bytes:
    """*length* bytes from *lo*, zero-padded where that leaves the file, exactly
    as :func:`accuraterip.verify_rip` pads a track's own window."""
    read_lo, read_hi = max(0, lo), min(file_size, lo + length)
    src.seek(read_lo)
    data = src.read(max(0, read_hi - read_lo))
    return bytes(read_lo - lo) + data + bytes(lo + length - max(read_hi, read_lo))


def reslice(
    pcm_file: Path,
    shifts: dict[int, int],
    track_lsns: list[int],
    disc_last_lsn: int,
    read_offset: int,
    cohort: list[list[dict]],
    scratch: Path,
    splice: Splice,
) -> dict[int, int]:
    """Write each displaced track back at its own place. Returns what was written.

    Two phases, through *scratch*, because the tracks overlap: track ``t``'s
    audio starts ``|shift|`` samples inside track ``t - 1``'s place, so writing
    one track in place would overwrite the source of its neighbour. Every track
    is first copied out of the untouched capture and checked once more against
    the cohort; only then is anything written. *scratch* must be on real disk
    (the rip's own scratch directory) and is removed before returning.
    """
    n_tracks = len(track_lsns)
    file_size = (disc_last_lsn + 1) * _SECTOR_BYTES
    ends = [*track_lsns[1:], disc_last_lsn + 1]
    staged: list[tuple[int, int, int, int]] = []  # track, shift, scratch pos, length
    try:
        with pcm_file.open("rb") as src, scratch.open("wb") as tmp:
            for t, shift in sorted(shifts.items()):
                start = track_lsns[t - 1] * _FRAME_SAMPLES + read_offset
                length = (ends[t - 1] - track_lsns[t - 1]) * _SECTOR_BYTES
                if not _reach_ok(
                    start, start + length // 4, shift, (0, file_size // 4)
                ):
                    continue
                data = _read_padded(src, (start + shift) * 4, length, file_size)
                _v1, _v2, conf_v1, conf_v2 = match_track_pcm(data, t, n_tracks, cohort)
                if not (conf_v1 or conf_v2):
                    log.warning("track %d did not verify at shift %+d", t, shift)
                    continue
                staged.append((t, shift, tmp.tell(), length))
                tmp.write(data)
        with scratch.open("rb") as tmp, pcm_file.open("r+b") as dst:
            for t, _shift, pos, length in staged:
                tmp.seek(pos)
                splice(dst, tmp.read(length), track_lsns[t - 1], read_offset, file_size)
    finally:
        scratch.unlink(missing_ok=True)
    return {t: shift for t, shift, _pos, _length in staged}


def displaced_in_window(
    window: bytes,
    origin: int,
    track_samples: int,
    real: tuple[int, int],
    track: int,
    n_tracks: int,
    cohort: list[list[dict]],
) -> tuple[int, bytes] | None:
    """A displaced copy of one track inside a re-read window, or ``None``.

    *window* is the bytes of one re-read, zero-padded to its unclamped extent;
    the track's audio belongs at sample *origin* in it, and *real* is the sample
    range the drive actually delivered (rule 3 is applied against it). Returns
    ``(shift, track_bytes)``.

    One track has no second track to corroborate it, so only a shift nominated
    by the frame-450 sum is accepted (rule 2). A cohort with no frame-450 data
    for the track gets ``None``.
    """
    end = origin + track_samples
    radius = min(_CHECK_RADIUS, max(origin - real[0], real[1] - end))
    if not cohort or radius <= 0:
        return None
    a = _as_frames(window)
    shifts, independent = _nominate(a, origin, end, track - 1, n_tracks, cohort, radius)
    if not independent:
        return None
    for shift in sorted(shifts, key=abs):
        if _confirms(a, origin, end, shift, track - 1, n_tracks, cohort, real):
            return shift, _window(a, origin + shift, track_samples).tobytes()
    return None
