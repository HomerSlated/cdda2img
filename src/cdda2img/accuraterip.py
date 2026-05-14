"""
accuraterip.py — AccurateRip checksum computation and database verification.

Public interface:
    verify_rip(pcm_path, track_lsns, disc_last_lsn, drive_offset, cddb_id) -> list[ARTrackResult]
    print_ar_report(results) -> None
"""

from __future__ import annotations

import array
import logging
import struct
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# Sanity-check array item size at module load — AccurateRip frames are u32 LE.
# See LINT-014.
if array.array("I").itemsize != 4:  # LINT-014
    msg = "array.array('I').itemsize must be 4 (expected on x86/x86_64 Linux)"
    raise RuntimeError(msg)

_SKIP_FRAMES = 5 * 588  # 2940 frames — excluded from each boundary per AR spec
_AR_BASE = "http://www.accuraterip.com/accuraterip"


@dataclass
class ARTrackResult:
    """Per-track AccurateRip verification result."""

    track: int
    v1_crc: str  # 8-char hex
    v2_crc: str  # 8-char hex
    confidence_v1: int | None
    confidence_v2: int | None
    max_confidence: int | None  # None = disc not in AR database


def _ar_checksums(
    frames: array.array, track: int, total_tracks: int
) -> tuple[int, int]:
    """Return (v1_crc, v2_crc) AccurateRip checksums for one track's frames.

    frames: array.array('I') of unsigned 32-bit stereo frame values (s16le pairs).
    track, total_tracks: 1-based.

    Algorithm mirrors ARver arver/audio/_audio.c:accuraterip(). Multiplier is
    always 1-based from frame 0; boundary exclusion uses sum_from/sum_to guards
    without resetting the multiplier. csum_hi accumulates overflow bits for v2.
    """
    n = len(frames)
    # Track 1: skip first 2940 frames (mult 1..2939 excluded; >= 2940 included)
    # Last track: skip last 2940 frames (mult <= n-2940 included)
    sum_from = _SKIP_FRAMES if track == 1 else 0
    sum_to = n - _SKIP_FRAMES if track == total_tracks else n
    # sum_from/sum_to map to a contiguous slice: mult >= sum_from ↔ i >= sum_from-1
    lo = max(0, sum_from - 1)
    if sum_to <= lo or n == 0:
        return 0, 0
    arr = np.frombuffer(frames, dtype=np.uint32)[lo:sum_to].astype(np.uint64)
    mults = np.arange(lo + 1, sum_to + 1, dtype=np.uint64)
    products = arr * mults
    csum_lo = int((products & np.uint64(0xFFFFFFFF)).sum())
    csum_hi = int((products >> np.uint64(32)).sum())
    return csum_lo & 0xFFFFFFFF, (csum_lo + csum_hi) & 0xFFFFFFFF


def _ar_disc_ids(track_lsns: list[int], disc_last_lsn: int) -> tuple[str, str]:
    """Return (id1, id2) as 8-char hex strings for the AccurateRip URL.

    Formula from ARver arver/disc/fingerprint.py. Inputs are LSNs (not LBA).
    lsn_leadout = disc_last_lsn + 1 (= lead-out LSN, first sector past last audio).
    """
    n = len(track_lsns)
    lsn_leadout = disc_last_lsn + 1
    id1 = sum(track_lsns) + lsn_leadout
    id2 = sum(
        (lsn or 1) * (i + 1) for i, lsn in enumerate(track_lsns)
    ) + lsn_leadout * (n + 1)
    return f"{id1 & 0xFFFFFFFF:08x}", f"{id2 & 0xFFFFFFFF:08x}"


def _ar_url(track_count: int, id1: str, id2: str, cddb_id: int) -> str:
    # Directory path uses the LAST three chars of id1 in reverse order (LSBs first).
    return (
        f"{_AR_BASE}/{id1[-1]}/{id1[-2]}/{id1[-3]}/"
        f"dBAR-{track_count:03d}-{id1}-{id2}-{cddb_id:08x}.bin"
    )


def _fetch_ar(url: str) -> bytes | None:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310  # LINT-014
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            log.debug("AccurateRip: disc not found (404)")
        else:
            log.warning("AccurateRip fetch failed: HTTP %d", exc.code)
        return None
    except OSError as exc:
        log.warning("AccurateRip fetch failed: %s", exc)
        return None


def _parse_dbar(data: bytes, n_tracks: int) -> list[list[dict]]:
    """Parse AccurateRip dBAR binary into response blocks.

    Returns a list of responses; each response is a list of n_tracks dicts
    with keys: conf (int), v1 (int), v2 (int).

    Binary layout: repeated blocks of (13-byte header + n_tracks x 9-byte entries).
    Header: <BLLL (n_tracks, id1, id2, cddb_id). Entry: <BLL (conf, v1_crc, v2_crc).
    """
    responses: list[list[dict]] = []
    pos = 0
    block_size = 13 + n_tracks * 9
    while pos + block_size <= len(data):
        header_n, _, _, _ = struct.unpack_from("<BLLL", data, pos)
        if header_n != n_tracks:
            log.debug(
                "AccurateRip: unexpected track count %d in dBAR block (expected %d)",
                header_n,
                n_tracks,
            )
            break
        pos += 13
        tracks: list[dict] = []
        for _ in range(n_tracks):
            conf, v1, v2 = struct.unpack_from("<BLL", data, pos)
            tracks.append({"conf": conf, "v1": v1, "v2": v2})
            pos += 9
        responses.append(tracks)
    return responses


def verify_rip(
    pcm_path: Path,
    track_lsns: list[int],
    disc_last_lsn: int,
    read_offset: int = 0,
    cddb_id: int = 0,
) -> list[ARTrackResult]:
    """Verify a ripped disc against the AccurateRip database.

    Returns per-track results. Never raises — network or I/O errors yield results
    with max_confidence=None (disc not in database or unreachable).

    read_offset: CD drive read offset in samples (4 bytes/sample). Applied as a
    byte shift to each track's read window in the PCM file before checksum computation.
    cddb_id: 32-bit integer CDDB disc ID, used to construct the AccurateRip URL.
    """
    n = len(track_lsns)
    ar_id1, ar_id2 = _ar_disc_ids(track_lsns, disc_last_lsn)
    url = _ar_url(n, ar_id1, ar_id2, cddb_id)
    log.debug("AccurateRip URL: %s", url)

    ar_data = _fetch_ar(url)
    responses = _parse_dbar(ar_data, n) if ar_data else []

    # Skip the checksum loop entirely when the disc is not in the database.
    if not responses:
        return [
            ARTrackResult(
                track=i + 1,
                v1_crc="00000000",
                v2_crc="00000000",
                confidence_v1=None,
                confidence_v2=None,
                max_confidence=None,
            )
            for i in range(n)
        ]

    # Drive offset shifts the read window by offset_bytes relative to track boundaries.
    offset_bytes = read_offset * 4
    pcm_size = pcm_path.stat().st_size
    results: list[ARTrackResult] = []

    with open(pcm_path, "rb") as f:
        for i, lsn in enumerate(track_lsns):
            byte_start = lsn * 2352 + offset_bytes
            if i < n - 1:
                byte_end = track_lsns[i + 1] * 2352 + offset_bytes
            else:
                byte_end = (disc_last_lsn + 1) * 2352 + offset_bytes

            read_start = max(0, byte_start)
            read_end = min(pcm_size, byte_end)
            f.seek(read_start)
            raw = f.read(read_end - read_start)

            # Zero-pad to cover the full offset window when it extends outside the
            # file.  The padded zeros fall within the ±2940-frame exclusion zone, so
            # they don't affect the checksum — but without them the exclusion boundary
            # shifts and the last (or first) track mismatches.
            if byte_start < 0:
                raw = bytes(-byte_start) + raw
            if byte_end > pcm_size:
                raw = raw + bytes(byte_end - pcm_size)

            frames: array.array = array.array("I")
            frames.frombytes(raw[: len(raw) - len(raw) % 4])

            v1, v2 = _ar_checksums(frames, i + 1, n)

            conf_v1: int | None = None
            conf_v2: int | None = None
            max_conf: int | None = None
            for resp in responses:
                entry = resp[i]
                max_conf = (
                    max(max_conf, entry["conf"])
                    if max_conf is not None
                    else entry["conf"]
                )
                if entry["v1"] == v1:
                    conf_v1 = (
                        max(conf_v1, entry["conf"])
                        if conf_v1 is not None
                        else entry["conf"]
                    )
                if entry["v2"] == v2:
                    conf_v2 = (
                        max(conf_v2, entry["conf"])
                        if conf_v2 is not None
                        else entry["conf"]
                    )

            results.append(
                ARTrackResult(
                    track=i + 1,
                    v1_crc=f"{v1:08x}",
                    v2_crc=f"{v2:08x}",
                    confidence_v1=conf_v1,
                    confidence_v2=conf_v2,
                    max_confidence=max_conf,
                )
            )

    return results


def print_ar_report(results: list[ARTrackResult], read_offset: int = 0) -> None:
    """Print a per-track AccurateRip verification report to stdout."""
    if not results:
        return
    if results[0].max_confidence is None:
        print("  AccurateRip: disc not found in database")
        return

    n = len(results)
    n_ok = sum(
        1 for r in results if r.confidence_v1 is not None or r.confidence_v2 is not None
    )

    # All tracks mismatch on a disc that IS in the database — almost always a
    # drive offset configuration gap, not data corruption.
    if n_ok == 0:
        max_conf = max(r.max_confidence or 0 for r in results)
        print(
            f"  AccurateRip: disc found (max confidence {max_conf}) but no CRC match"
            f" at read_offset={read_offset}"
        )
        print("    Add a [[drives]] entry in ~/.config/cdda2img/cdda2img.toml")
        return

    print("  AccurateRip:")
    for r in results:
        if r.confidence_v1 is not None:
            status = f"OK  [conf {r.confidence_v1}/{r.max_confidence}]"
        elif r.confidence_v2 is not None:
            status = f"OK v2  [conf {r.confidence_v2}/{r.max_confidence}]"
        else:
            status = f"MISMATCH  [max conf {r.max_confidence}]"
        print(f"    Track {r.track:2d}: v1={r.v1_crc}  {status}")

    if n_ok == n:
        confs = [r.confidence_v1 or r.confidence_v2 or 0 for r in results]
        print(f"    {n}/{n} tracks verified (min confidence {min(confs)})")
    else:
        print(f"    {n_ok}/{n} tracks verified ({n - n_ok} mismatch)")
