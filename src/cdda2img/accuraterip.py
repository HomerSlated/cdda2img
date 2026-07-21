"""
accuraterip.py — AccurateRip checksum computation and database verification.

Public interface:
    verify_rip(pcm_path, track_lsns, disc_last_lsn, drive_offset, cddb_id) -> list[ARTrackResult]
    fetch_ar_responses(track_lsns, disc_last_lsn, cddb_id) -> (responses, transport, b3sum)
    match_track_pcm(raw, track, n_tracks, responses) -> (v1_hex, v2_hex, conf_v1, conf_v2)
    detect_offset(pcm, track_lsns, disc_last_lsn, responses) -> list[OffsetMatch]
    print_ar_report(results) -> None
    pack_arip_block(results, track_lsns, disc_last_lsn, cddb_id) -> bytes
    unpack_arip_block(data, track_count) -> RBIArip
"""

from __future__ import annotations

import array
import logging
import struct
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cdda2img.rbi_format import RBIArip

import numpy as np

log = logging.getLogger(__name__)

# Sanity-check array item size at module load — AccurateRip frames are u32 LE.
# See LINT-014.
if array.array("I").itemsize != 4:  # LINT-014
    msg = "array.array('I').itemsize must be 4 (expected on x86/x86_64 Linux)"
    raise RuntimeError(msg)

_SKIP_FRAMES = 5 * 588  # 2940 frames — excluded from each boundary per AR spec

# R2: HTTPS-first transport with HTTP fallback. accuraterip.com serves a valid
# Let's Encrypt cert at the dBAR namespace (verified 2026-05-28). HTTP remains
# as fallback only — when reachable, the `arip_transport=http` PROV signal
# tells downstream readers to treat the confidence values with reduced trust.
_AR_BASE_HTTPS = "https://www.accuraterip.com/accuraterip"
_AR_BASE_HTTP = "http://www.accuraterip.com/accuraterip"

# R2: dBAR response cap. A 99-track block is ~900 bytes, total response is
# usually <5 KB; 1 MB is a generous defensive ceiling that bounds any
# pathological / hostile response without truncating real ones.
_AR_DBAR_MAX = 1_048_576


@dataclass
class ARTrackResult:
    """Per-track AccurateRip verification result."""

    track: int
    v1_crc: str  # 8-char hex
    v2_crc: str  # 8-char hex
    confidence_v1: int | None  # None = no CRC match for v1
    confidence_v2: int | None  # None = no CRC match for v2
    max_confidence: (
        int | None
    )  # None = disc not in AR database; highest single-block conf
    total_confidence: int | None = (
        None  # None = not in DB; sum of all dBAR block confidences
    )
    # Frame-450 sub-CRC match confidence. Only meaningful on tracks whose full
    # v1/v2 failed: a crc450 match there means the frame-450 region is
    # byte-identical to a DB submission — right pressing/offset, damage
    # elsewhere in the track ("DAMAGED" in the report). Never a verification
    # pass on its own and never a recovery splice gate.
    confidence_450: int | None = None


@dataclass
class ARVerifyResult:
    """Disc-level outcome of an AccurateRip verification pass (R2).

    *tracks* preserves the existing per-track API; the new fields surface
    R2's provenance signals for the PROV block.
    """

    tracks: list[ARTrackResult] = field(default_factory=list)
    # "https" | "http" | None. None when both transports failed at the network
    # level (DNS, TLS handshake, connection refused, 5xx). "https" is also the
    # value used when the server cleanly returned 404 over HTTPS — the disc is
    # not in the database, and we made successful contact at that transport.
    transport: str | None = None
    # 64 lowercase hex chars; None when no body was fetched (404 / network
    # failure). BLAKE3 of the *raw* response bytes lets later re-fetches
    # detect AR-side changes or mirror tampering without re-running the
    # full verification pipeline.
    dbar_b3sum: str | None = None


def _checksum_bounds(n: int, track: int, total_tracks: int) -> tuple[int, int]:
    """Return the (lo, sum_to) half-open frame window a track's v1/v2 checksum
    covers, given its length *n* in frames.

    Track 1 skips the first 2940 frames (mult 1..2939 excluded; >= 2940
    included); the last track skips the last 2940 (mult <= n-2940 included).
    sum_from/sum_to map to a contiguous slice: mult >= sum_from ↔ i >= sum_from-1.
    The multiplier is *not* reset by the exclusion — the first included frame
    carries multiplier lo+1, which is why the window is described by its
    absolute bounds rather than a length alone.
    """
    sum_from = _SKIP_FRAMES if track == 1 else 0
    sum_to = n - _SKIP_FRAMES if track == total_tracks else n
    return max(0, sum_from - 1), sum_to


def _ar_checksums_arr(
    arr32: np.ndarray, track: int, total_tracks: int
) -> tuple[int, int]:
    """(v1_crc, v2_crc) for one track, from a uint32 ndarray of stereo frames.

    Algorithm mirrors ARver arver/audio/_audio.c:accuraterip(). csum_hi
    accumulates overflow bits for v2.
    """
    n = len(arr32)
    lo, sum_to = _checksum_bounds(n, track, total_tracks)
    if sum_to <= lo or n == 0:
        return 0, 0
    arr = arr32[lo:sum_to].astype(np.uint64)
    mults = np.arange(lo + 1, sum_to + 1, dtype=np.uint64)
    products = arr * mults
    csum_lo = int((products & np.uint64(0xFFFFFFFF)).sum())
    csum_hi = int((products >> np.uint64(32)).sum())
    return csum_lo & 0xFFFFFFFF, (csum_lo + csum_hi) & 0xFFFFFFFF


def _ar_checksums(
    frames: array.array, track: int, total_tracks: int
) -> tuple[int, int]:
    """Return (v1_crc, v2_crc) AccurateRip checksums for one track's frames.

    frames: array.array('I') of unsigned 32-bit stereo frame values (s16le pairs).
    track, total_tracks: 1-based.
    """
    return _ar_checksums_arr(
        np.frombuffer(frames, dtype=np.uint32), track, total_tracks
    )


_CRC450_FRAME = 450  # sector offset into the track of the sub-CRC window
_CRC450_LO = _CRC450_FRAME * 588  # first frame of the sub-CRC window


def _ar_crc450_arr(arr32: np.ndarray) -> int | None:
    """Frame-450 sub-CRC from a uint32 ndarray of one track's stereo frames."""
    if len(arr32) < _CRC450_LO + 588:
        return None
    arr = arr32[_CRC450_LO : _CRC450_LO + 588].astype(np.uint64)
    mults = np.arange(1, 589, dtype=np.uint64)
    return int((arr * mults).sum()) & 0xFFFFFFFF


def _ar_crc450(frames: array.array) -> int | None:
    """AccurateRip frame-450 sub-CRC: v1-style sum over the single sector at
    track offset 450, with a LOCAL 1-based multiplier (1..588, not the global
    track position). Returns None when the track is too short to contain the
    window (< 451 sectors ≈ 6 s).

    Formula pinned empirically 2026-07-05 against the AR-verified reference
    disc: all 11 tracks match their dBAR ``crc450`` fields, and track 8's
    value equals the one cyanrip reports for the same disc.
    """
    return _ar_crc450_arr(np.frombuffer(frames, dtype=np.uint32))


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


def _ar_url(
    track_count: int, id1: str, id2: str, cddb_id: int, *, base: str = _AR_BASE_HTTPS
) -> str:
    # Directory path uses the LAST three chars of id1 in reverse order (LSBs first).
    return (
        f"{base}/{id1[-1]}/{id1[-2]}/{id1[-3]}/"
        f"dBAR-{track_count:03d}-{id1}-{id2}-{cddb_id:08x}.bin"
    )


def _fetch_ar(
    track_count: int, id1: str, id2: str, cddb_id: int
) -> tuple[bytes | None, str | None]:
    """Fetch the dBAR over HTTPS; fall back to HTTP on network/TLS failure.

    Returns ``(body, transport)``:
      * ``(<bytes>, "https")`` — TLS attempt succeeded with a body.
      * ``(<bytes>, "http")``  — TLS failed; plaintext fallback succeeded.
      * ``(None, "https")``    — TLS reached the server but the disc is not
        in the database (HTTP 404). No HTTP fallback in this case — the
        same disc will 404 over either transport.
      * ``(None, "http")``     — analogous 404 on the HTTP fallback.
      * ``(None, None)``       — both transports failed at the network level.

    Responses larger than ``_AR_DBAR_MAX`` are treated as malformed: body
    is dropped, transport is still recorded (the server *did* answer).
    """
    last_transport: str | None = None
    for base, name in ((_AR_BASE_HTTPS, "https"), (_AR_BASE_HTTP, "http")):
        url = _ar_url(track_count, id1, id2, cddb_id, base=base)
        log.debug("AccurateRip URL (%s): %s", name, url)
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310  # LINT-014
                # read(N+1) detects oversize without unbounded buffering.
                body = resp.read(_AR_DBAR_MAX + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                log.debug("AccurateRip %s: disc not found (404)", name)
                # 404 is a legitimate negative — same disc will 404 over HTTP too.
                return None, name
            log.warning("AccurateRip %s fetch failed: HTTP %d", name, exc.code)
            last_transport = name
            continue
        except (urllib.error.URLError, OSError) as exc:
            log.warning("AccurateRip %s fetch failed: %s", name, exc)
            continue
        if len(body) > _AR_DBAR_MAX:
            log.warning(
                "AccurateRip %s response > %d bytes; rejecting as malformed",
                name,
                _AR_DBAR_MAX,
            )
            return None, name
        import blake3 as _blake3

        log.debug(
            "AccurateRip %s: 200 OK, %d bytes, b3sum=%s",
            name,
            len(body),
            _blake3.blake3(body).hexdigest(),
        )
        return body, name
    return None, last_transport


def _parse_dbar(
    data: bytes,
    n_tracks: int,
    *,
    expected_id1: int | None = None,
    expected_id2: int | None = None,
    expected_cddb_id: int | None = None,
) -> list[list[dict]]:
    """Parse AccurateRip dBAR binary into response blocks.

    Returns a list of responses; each response is a list of n_tracks dicts
    with keys: conf (int), crc (int), crc450 (int).

    Binary layout: repeated blocks of (13-byte header + n_tracks x 9-byte entries).
    Header: <BLLL (n_tracks, id1, id2, cddb_id). Entry: <BLL (conf, crc, crc450).

    Per-track entry semantics (the subtle part): each track carries a SINGLE
    AccurateRip checksum (``crc``) — not separate v1 and v2 fields. Whether
    that value is a v1 or a v2 checksum depends on the ripper that submitted
    the block: v1-era rippers wrote a v1 checksum, v2-era rippers a v2
    checksum, into the same slot. Verification therefore computes both v1 and
    v2 locally and tests each against ``crc`` (see verify_rip). The second
    4-byte field (``crc450``) is the frame-450 sub-CRC used only for blind
    offset detection — it is NOT the v2 checksum and must not be matched
    against it.

    R2: when *expected_id1*, *expected_id2*, and *expected_cddb_id* are
    provided, each block's header is verified against them. Blocks with a
    mismatching identifier are skipped (logged at WARNING) — this defends
    against a poisoned response that interleaves attacker-controlled blocks
    for unrelated discs. With all three None (legacy callers), the check is
    bypassed and behaviour matches pre-R2.
    """
    responses: list[list[dict]] = []
    pos = 0
    block_size = 13 + n_tracks * 9
    while pos + block_size <= len(data):
        header_n, h_id1, h_id2, h_cddb = struct.unpack_from("<BLLL", data, pos)
        if header_n != n_tracks:
            log.debug(
                "AccurateRip: unexpected track count %d in dBAR block (expected %d)",
                header_n,
                n_tracks,
            )
            break
        if (
            (expected_id1 is not None and h_id1 != expected_id1)
            or (expected_id2 is not None and h_id2 != expected_id2)
            or (expected_cddb_id is not None and h_cddb != expected_cddb_id)
        ):
            log.warning(
                "AccurateRip: dBAR block at offset %d has mismatching IDs "
                "(got %08x-%08x-%08x, expected %08x-%08x-%08x); skipping",
                pos,
                h_id1,
                h_id2,
                h_cddb,
                expected_id1 or 0,
                expected_id2 or 0,
                expected_cddb_id or 0,
            )
            pos += block_size
            continue
        pos += 13
        tracks: list[dict] = []
        for _ in range(n_tracks):
            conf, crc, crc450 = struct.unpack_from("<BLL", data, pos)
            tracks.append({"conf": conf, "crc": crc, "crc450": crc450})
            pos += 9
        responses.append(tracks)
    return responses


def fetch_ar_responses(
    track_lsns: list[int], disc_last_lsn: int, cddb_id: int
) -> tuple[list[list[dict]], str | None, str | None]:
    """Fetch and parse the AccurateRip dBAR once. Returns ``(responses, transport,
    dbar_b3sum)``; *responses* is empty when the disc is not in the database. Lets a
    caller verify many tracks (or re-verify after a re-rip) without re-fetching."""
    import blake3 as _blake3

    n = len(track_lsns)
    id1, id2 = _ar_disc_ids(track_lsns, disc_last_lsn)
    ar_data, transport = _fetch_ar(n, id1, id2, cddb_id)
    dbar_b3sum = _blake3.blake3(ar_data).hexdigest() if ar_data else None
    responses = (
        _parse_dbar(
            ar_data,
            n,
            expected_id1=int(id1, 16),
            expected_id2=int(id2, 16),
            expected_cddb_id=cddb_id,
        )
        if ar_data
        else []
    )
    return responses, transport, dbar_b3sum


def match_track_pcm(
    raw: bytes, track: int, n_tracks: int, responses: list[list[dict]]
) -> tuple[str, str, int | None, int | None]:
    """Compute one track's v1/v2 AR checksums from its raw s16le PCM and match them against
    the per-block dBAR *responses*. *raw* must already be offset-corrected (e.g. a
    cd-paranoia ``-O`` rip) — there is no read-window shift here, unlike :func:`verify_rip`.
    Returns ``(v1_hex, v2_hex, conf_v1, conf_v2)``; a confidence is None when no block
    matched that variant. Interior tracks are self-contained; track 1 / the last track use
    the same ``sum_from``/``sum_to`` boundary guards as :func:`_ar_checksums`."""
    frames: array.array = array.array("I")
    frames.frombytes(raw[: len(raw) - len(raw) % 4])
    v1, v2 = _ar_checksums(frames, track, n_tracks)
    conf_v1: int | None = None
    conf_v2: int | None = None
    idx = track - 1
    for resp in responses:
        entry = resp[idx]
        if entry["crc"] == v1:
            conf_v1 = entry["conf"] if conf_v1 is None else max(conf_v1, entry["conf"])
        if entry["crc"] == v2:
            conf_v2 = entry["conf"] if conf_v2 is None else max(conf_v2, entry["conf"])
    return f"{v1:08x}", f"{v2:08x}", conf_v1, conf_v2


def verify_rip(
    pcm_path: Path,
    track_lsns: list[int],
    disc_last_lsn: int,
    read_offset: int = 0,
    cddb_id: int = 0,
) -> ARVerifyResult:
    """Verify a ripped disc against the AccurateRip database.

    Returns an ``ARVerifyResult`` carrying per-track results plus the R2
    provenance signals (``transport``, ``dbar_sha256``). Never raises —
    network or I/O errors yield ``max_confidence=None`` per track (disc
    not in database or unreachable).

    read_offset: CD drive read offset in samples (4 bytes/sample). Applied as a
    byte shift to each track's read window in the PCM file before checksum computation.
    cddb_id: 32-bit integer CDDB disc ID, used to construct the AccurateRip URL.
    """
    n = len(track_lsns)
    responses, transport, dbar_b3sum = fetch_ar_responses(
        track_lsns, disc_last_lsn, cddb_id
    )

    # Skip the checksum loop entirely when the disc is not in the database.
    if not responses:
        return ARVerifyResult(
            tracks=[
                ARTrackResult(
                    track=i + 1,
                    v1_crc="00000000",
                    v2_crc="00000000",
                    confidence_v1=None,
                    confidence_v2=None,
                    max_confidence=None,
                )
                for i in range(n)
            ],
            transport=transport,
            dbar_b3sum=dbar_b3sum,
        )

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
            c450 = _ar_crc450(frames)

            conf_v1: int | None = None
            conf_v2: int | None = None
            conf_450: int | None = None
            max_conf: int | None = None
            total_conf: int = 0
            for resp in responses:
                entry = resp[i]
                max_conf = (
                    max(max_conf, entry["conf"])
                    if max_conf is not None
                    else entry["conf"]
                )
                total_conf += entry["conf"]
                # Each block stores ONE checksum per track (entry["crc"]); it is
                # a v1 value in v1-era blocks and a v2 value in v2-era blocks.
                # Test both locally-computed checksums against that single field
                # and tally each variant's confidence from whichever blocks it
                # matched. entry["crc450"] is the frame-450 sub-CRC, tallied
                # separately: it grades a full-CRC failure ("DAMAGED" — right
                # pressing, corrupt elsewhere) and never verifies on its own.
                # Many blocks store 0 there (no data) — a zero never matches.
                if entry["crc"] == v1:
                    conf_v1 = (
                        max(conf_v1, entry["conf"])
                        if conf_v1 is not None
                        else entry["conf"]
                    )
                if entry["crc"] == v2:
                    conf_v2 = (
                        max(conf_v2, entry["conf"])
                        if conf_v2 is not None
                        else entry["conf"]
                    )
                if c450 is not None and entry["crc450"] and entry["crc450"] == c450:
                    conf_450 = (
                        max(conf_450, entry["conf"])
                        if conf_450 is not None
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
                    total_confidence=total_conf if total_conf > 0 else None,
                    confidence_450=conf_450,
                )
            )

    return ARVerifyResult(tracks=results, transport=transport, dbar_b3sum=dbar_b3sum)


# ── Blind offset detection ────────────────────────────────────────────────────
#
# What "blind" means: recover the sample offset of a rip with no knowledge of
# the drive that produced it, by sliding the checksum window against the
# AccurateRip database until it agrees. The answer is in the same sign
# convention as verify_rip's *read_offset* — the value that makes the rip
# verify.
#
# Search radius is AccurateRip's own: 5 frames minus one sample. That is one
# sample short of the 2940-frame boundary exclusion zone, which is exactly the
# point — a larger offset would drag real audio across the exclusion boundary
# and change which samples are checksummed at all, so no single shift could
# reconcile it.

_CHECK_RADIUS = 5 * 588 - 1  # 2939 samples


def _as_frames(pcm: Path | bytes | np.ndarray) -> np.ndarray:
    """View whole-disc s16le PCM as uint32 stereo frames. A Path is memory
    mapped — the crc450 sweep below touches only a few kB per track, so a
    850 MB disc image is never read in full."""
    if isinstance(pcm, Path):
        return np.memmap(pcm, dtype="<u4", mode="r")
    if isinstance(pcm, (bytes, bytearray, memoryview)):
        raw = bytes(pcm)
        return np.frombuffer(raw[: len(raw) - len(raw) % 4], dtype="<u4")
    return np.asarray(pcm, dtype="<u4")


def _window(a: np.ndarray, start: int, length: int) -> np.ndarray:
    """*length* frames of *a* from *start*, zero-padded outside the array.

    Zero-padding rather than clamping is what verify_rip does, and it is
    correct rather than merely convenient: the pad only ever lands inside the
    ±2940-frame exclusion zone at the disc edges, so it cannot perturb a
    checksum — but omitting it would shift the exclusion boundary itself.
    Returns a view (no copy) whenever the window is fully in range.
    """
    n = len(a)
    if start >= 0 and start + length <= n:
        return a[start : start + length]
    out = np.zeros(length, dtype=np.uint32)
    src_lo, src_hi = max(0, start), min(n, start + length)
    if src_hi > src_lo:
        out[src_lo - start : src_hi - start] = a[src_lo:src_hi]
    return out


def _sliding_v1(a: np.ndarray, base: int, n: int, m0: int, radius: int) -> np.ndarray:
    """Every AccurateRip-style weighted sum in a ±*radius* sweep, in one pass.

    Computes ``V(o) = sum_{k=0}^{n-1} a[base+o+k] * (k+m0)`` for every
    ``o`` in ``[-radius, radius]``, returned as uint32 indexed by ``o+radius``.

    The recurrence is what makes a full sweep affordable — shifting the window
    one sample right decrements every multiplier by one, which costs a window
    sum rather than a re-multiplication::

        V(o+1) = V(o) - T(o) - (m0-1)*a[base+o] + (n+m0-1)*a[base+o+n]

    with ``T(o)`` the plain window sum, itself a difference of two prefix sums.
    So the whole sweep is two cumulative sums and no inner loop: O(n + radius)
    instead of O(n * radius).

    Overflow is not a hazard here even though the intermediates far exceed the
    32-bit result: every step is add/subtract/multiply, so wrapping mod 2**64
    and then reducing mod 2**32 gives the same answer as exact arithmetic
    reduced mod 2**32. Increments are reduced early anyway to keep the
    cumulative sum comfortably inside uint64.
    """
    mask = np.uint64(0xFFFFFFFF)
    seg = _window(a, base - radius, 2 * radius + n + 1).astype(np.uint64)
    prefix = np.zeros(len(seg) + 1, dtype=np.uint64)
    np.cumsum(seg, out=prefix[1:])

    t = np.arange(2 * radius + 1, dtype=np.int64)
    window_sum = prefix[t + n] - prefix[t]
    delta = (
        np.uint64(n + m0 - 1) * seg[t + n] - window_sum - np.uint64(m0 - 1) * seg[t]
    ) & mask

    # V at the left end of the sweep, computed directly.
    head = seg[:n] * (np.arange(n, dtype=np.uint64) + np.uint64(m0))
    v_first = int((head & mask).sum()) & 0xFFFFFFFF

    out = np.empty(2 * radius + 1, dtype=np.uint64)
    out[0] = v_first
    if radius:
        np.cumsum(delta[:-1], out=out[1:])
        out[1:] += np.uint64(v_first)
    return (out & mask).astype(np.uint32)


@dataclass
class OffsetMatch:
    """One candidate offset and the evidence for it.

    *tracks_450* counts tracks whose frame-450 sub-CRC matched at this offset
    (the prefilter); *tracks_v1* / *tracks_v2* count tracks whose full-track
    checksum matched (the confirmation). A candidate with a full-track
    confirmation across most of the disc is conclusive; one with only sub-CRC
    hits is a lead, not an answer — that combination has been observed on a
    real disc. *tracks_450* is 0 when no block carried frame-450 data and
    detection fell back to sweeping full-track checksums.

    *confidence* sums, per track, the confidence of every dBAR block that
    matched — i.e. the population of the cohort this offset belongs to, not
    the single best block. Two offsets tying on it is a real outcome, not a
    defect: see :func:`detect_offset`.
    """

    offset: int
    tracks_450: int
    tracks_v1: int
    tracks_v2: int
    confidence: int
    total_tracks: int

    @property
    def confirmed(self) -> bool:
        """True when whole-track checksums agree, not merely the 588-frame probe."""
        return self.tracks_v1 > 0 or self.tracks_v2 > 0

    @property
    def tracks_matched(self) -> int:
        return max(self.tracks_v1, self.tracks_v2)

    def _rank(self) -> tuple[int, int, int, int]:
        return (
            self.tracks_matched,
            self.tracks_450,
            self.confidence,
            -abs(self.offset),
        )


def _track_frame_bounds(
    track_lsns: list[int], disc_last_lsn: int
) -> list[tuple[int, int]]:
    """Per-track [start, end) in stereo frames, offset 0."""
    ends = [*track_lsns[1:], disc_last_lsn + 1]
    return [(s * 588, e * 588) for s, e in zip(track_lsns, ends)]


def _sweep_hits(
    a: np.ndarray,
    bounds: list[tuple[int, int]],
    responses: list[list[dict]],
    radius: int,
    *,
    key: str,
) -> dict[int, dict[int, int]]:
    """Sweep every track and tally, per offset, which tracks matched the DB.

    *key* selects the reference field: ``"crc450"`` sweeps the cheap 588-frame
    probe window, ``"crc"`` sweeps the whole track. Returns
    ``{offset: {track_index: best_confidence}}``.
    """
    hits: dict[int, dict[int, int]] = {}
    n_tracks = len(bounds)
    for ti, (start, end) in enumerate(bounds):
        targets = _reference_values(responses, ti, key)
        params = _sweep_window(start, end, ti, n_tracks, key)
        if not targets or params is None:
            continue
        sweep = _sliding_v1(a, *params, radius)
        for value, conf in targets.items():
            for idx in np.flatnonzero(sweep == np.uint32(value)):
                per_track = hits.setdefault(int(idx) - radius, {})
                if conf > per_track.get(ti, -1):
                    per_track[ti] = conf
    return hits


def _reference_values(
    responses: list[list[dict]], track_index: int, key: str
) -> dict[int, int]:
    """{checksum: best confidence} the DB holds for one track. Zero means "no
    data submitted" for both fields, so it is never a target."""
    targets: dict[int, int] = {}
    for resp in responses:
        entry = resp[track_index]
        value = entry[key]
        if value:
            targets[value] = max(targets.get(value, 0), entry["conf"])
    return targets


def _sweep_window(
    start: int, end: int, track_index: int, n_tracks: int, key: str
) -> tuple[int, int, int] | None:
    """(base, length, first multiplier) for one track's sweep, or None when the
    track cannot carry the requested window."""
    if key == "crc450":
        if end - start < _CRC450_LO + 588:
            return None  # track shorter than ~6 s has no probe window
        return start + _CRC450_LO, 588, 1
    lo, sum_to = _checksum_bounds(end - start, track_index + 1, n_tracks)
    if sum_to <= lo:
        return None
    return start + lo, sum_to - lo, lo + 1


def detect_offset(
    pcm: Path | bytes | np.ndarray,
    track_lsns: list[int],
    disc_last_lsn: int,
    responses: list[list[dict]],
    *,
    radius: int = _CHECK_RADIUS,
    max_candidates: int = 8,
) -> list[OffsetMatch]:
    """Find the sample offset at which a rip agrees with AccurateRip.

    *pcm* is whole-disc raw s16le in the domain the offset applies to (i.e.
    uncorrected). The returned offsets carry verify_rip's ``read_offset``
    sign convention: passing the winner as ``read_offset`` makes the disc
    verify. Best candidate first; empty when the disc is not in the database.

    Two stages, in cost order:

    1. **Prefilter** — sweep the frame-450 sub-CRC. This is the field's whole
       reason to exist: it covers 588 frames at a fixed position, so the sweep
       reads ~6 k frames per track instead of the entire track, roughly a
       thousandfold less work than sweeping full checksums.
    2. **Confirm** — recompute exact full-track v1/v2 checksums at each
       surviving candidate, using the same window arithmetic as verify_rip.

    When no dBAR block carries frame-450 data (the field is often zero in
    older submissions) the prefilter is skipped and full-track checksums are
    swept directly — same answer, more work.

    **The answer is not always unique.** A widely-submitted disc can verify at
    several offsets at once, because the same master gets pressed at different
    absolute positions and every such pressing is its own AccurateRip cohort.
    Tracy Chapman's debut verifies fully at 0, -669, -1333 and -1997, the first
    two at identical confidence — and the -669 twin also appears in CTDB, so it
    is a property of the discs, not of one database. Callers must treat the
    result as a ranked list of *equally valid* readings and disambiguate with
    something outside the audio (a known drive offset, a CTDB cross-check, or
    the user). The final ranking tiebreak is proximity to zero, which suits
    verifying an already-corrected rip and is only a heuristic for anything else.
    """
    if not responses:
        return []

    a = _as_frames(pcm)
    n_tracks = len(track_lsns)
    bounds = _track_frame_bounds(track_lsns, disc_last_lsn)

    hits = _sweep_hits(a, bounds, responses, radius, key="crc450")
    used_450 = bool(hits)
    if not hits:
        log.debug("AccurateRip: no frame-450 hits; sweeping full-track checksums")
        hits = _sweep_hits(a, bounds, responses, radius, key="crc")

    # Confirmation costs a full pass over the disc per candidate, so only
    # offsets the prefilter liked on a decent share of the disc are worth
    # confirming. Random collisions are not the concern — a 32-bit probe over
    # the whole search space predicts ~0.0003 of them per disc — but SYSTEMATIC
    # false positives are real and were observed: ABBA "Gold" has an offset
    # where the frame-450 probe hits all 19 tracks and not one full-track
    # checksum agrees, which is what submitters computing crc450 under a
    # different convention look like. Hence the prefilter only ever nominates;
    # confirmation below decides.
    floor = max(2, n_tracks // 2)
    ranked = sorted(
        hits.items(), key=lambda kv: (-len(kv[1]), -sum(kv[1].values()), abs(kv[0]))
    )
    candidates = [
        off for off, tracks in ranked[:max_candidates] if len(tracks) >= floor
    ]
    if 0 not in candidates:
        candidates.append(0)  # always report the as-ripped offset for reference

    matches: list[OffsetMatch] = []
    for off in candidates:
        tracks_v1 = tracks_v2 = 0
        confidence = 0
        for ti, (start, end) in enumerate(bounds):
            v1, v2 = _ar_checksums_arr(
                _window(a, start + off, end - start), ti + 1, n_tracks
            )
            matched_v1 = matched_v2 = False
            for resp in responses:
                entry = resp[ti]
                # One checksum field per block; whether it holds a v1 or a v2
                # value depends on the era of the submitting ripper, so both
                # locally-computed variants are tested against it. A cohort is
                # typically represented by two blocks — one from each era —
                # so confidences are summed, not maxed.
                hit_v1 = entry["crc"] == v1
                hit_v2 = entry["crc"] == v2
                if hit_v1 or hit_v2:
                    matched_v1 |= hit_v1
                    matched_v2 |= hit_v2
                    confidence += entry["conf"]
            tracks_v1 += matched_v1
            tracks_v2 += matched_v2
        probe = hits.get(off, {})
        matches.append(
            OffsetMatch(
                # Honest field: frame-450 evidence only. On the fallback path
                # the sweep matched full-track checksums, which is already
                # reported by tracks_v1/tracks_v2 — not restated here.
                tracks_450=len(probe) if used_450 else 0,
                offset=off,
                tracks_v1=tracks_v1,
                tracks_v2=tracks_v2,
                confidence=confidence or sum(probe.values()),
                total_tracks=n_tracks,
            )
        )

    matches.sort(key=lambda m: m._rank(), reverse=True)
    return matches


def _track_status(r: ARTrackResult) -> str:
    """One track's report verdict. A track verifies if EITHER full-CRC variant
    matched. A failed track whose frame-450 sub-CRC matches the DB is graded
    DAMAGED — right pressing at the right offset, corrupt bytes elsewhere in
    the track (definitely damage, not a configuration problem); otherwise it
    stays a plain MISMATCH."""
    if r.confidence_v1 is not None or r.confidence_v2 is not None:
        return "OK"
    if r.confidence_450 is not None:
        return f"DAMAGED (crc450 [{r.confidence_450}])"
    return f"MISMATCH (max {r.max_confidence})"


def format_ar_report(results: list[ARTrackResult], read_offset: int = 0) -> str:
    """Return a per-track AccurateRip verification report as a multi-line string.

    ``print_ar_report`` is a thin wrapper that prints this string verbatim.
    """
    if not results:
        return ""
    if results[0].max_confidence is None:
        return "AccurateRip: disc not found in database"

    n = len(results)
    n_ok = sum(
        1 for r in results if r.confidence_v1 is not None or r.confidence_v2 is not None
    )

    # All tracks mismatch on a disc that IS in the database — almost always
    # a drive offset configuration gap, not data corruption.
    if n_ok == 0:
        max_conf = max(r.max_confidence or 0 for r in results)
        return (
            f"AccurateRip: disc found (max confidence {max_conf}) but no CRC "
            f"match at read_offset={read_offset}\n"
            f"  Add a [[drives]] entry in ~/.config/cdda2img/cdda2img.toml"
        )

    def _conf(c: int | None) -> str:
        # Bracketed confidence; "[ — ]" when this CRC variant had no DB match.
        return f"[{c}]" if c is not None else "[ — ]"

    lines: list[str] = ["AccurateRip:"]
    n_damaged = 0
    for r in results:
        status = _track_status(r)
        n_damaged += status.startswith("DAMAGED")
        lines.append(
            f"  Track {r.track:2d}: "
            f"v1={r.v1_crc} {_conf(r.confidence_v1):<7}"
            f"v2={r.v2_crc} {_conf(r.confidence_v2):<7}{status}"
        )

    if n_ok == n:
        # Per-track "best" = the stronger of the two variants; report the weakest
        # track's best as the floor of trust.
        best = [max(r.confidence_v1 or 0, r.confidence_v2 or 0) for r in results]
        lines.append(f"  {n}/{n} tracks verified (min confidence {min(best)})")
    else:
        parts = []
        if n_damaged:
            parts.append(f"{n_damaged} damaged")
        if n - n_ok - n_damaged:
            parts.append(f"{n - n_ok - n_damaged} mismatch")
        lines.append(f"  {n_ok}/{n} tracks verified ({', '.join(parts)})")
        if n_damaged:
            lines.append(
                "  DAMAGED = in the AR DB (frame-450 region matches) but the "
                "track has bad bytes elsewhere"
            )
    return "\n".join(lines)


def print_ar_report(results: list[ARTrackResult], read_offset: int = 0) -> None:
    """Print a per-track AccurateRip verification report to stdout."""
    text = format_ar_report(results, read_offset)
    if text:
        for line in text.splitlines():
            print(f"   {line}")


# ---------------------------------------------------------------------------
# ARIP block serialisation
# ---------------------------------------------------------------------------


def pack_arip_block(
    results: list[ARTrackResult],
    track_lsns: list[int],
    disc_last_lsn: int,
    cddb_id: int,
) -> bytes:
    """Serialise AccurateRip verification results into an ARIP block (rbi_spec.md §6.5).

    disc_id1/disc_id2 are recomputed from track_lsns/disc_last_lsn so that the
    block is self-describing (stores the exact AR URL parameters used).
    """
    from cdda2img.rbi_format import (
        ARIP_BLOCK_VERSION,
        ARIP_HEADER_STRUCT,
        ARIP_STATUS_MISMATCH,
        ARIP_STATUS_NOT_IN_DB,
        ARIP_STATUS_OK,
        ARIP_TRACK_STRUCT,
    )

    id1_hex, id2_hex = _ar_disc_ids(track_lsns, disc_last_lsn)
    disc_id1 = int(id1_hex, 16)
    disc_id2 = int(id2_hex, 16)

    header = struct.pack(
        ARIP_HEADER_STRUCT, ARIP_BLOCK_VERSION, disc_id1, disc_id2, cddb_id
    )

    tracks_bytes = bytearray()
    for r in results:
        if r.max_confidence is None:
            status = ARIP_STATUS_NOT_IN_DB
        elif r.confidence_v1 is not None or r.confidence_v2 is not None:
            status = ARIP_STATUS_OK
        else:
            status = ARIP_STATUS_MISMATCH

        v1 = int(r.v1_crc, 16)
        v2 = int(r.v2_crc, 16)
        conf_v1 = min(r.confidence_v1 or 0, 0xFFFF)
        conf_v2 = min(r.confidence_v2 or 0, 0xFFFF)
        db_total = min(r.total_confidence or 0, 0xFFFF)

        tracks_bytes += struct.pack(
            ARIP_TRACK_STRUCT, v1, v2, conf_v1, conf_v2, db_total, status
        )

    return header + bytes(tracks_bytes)


def format_arip_text(arip: RBIArip) -> str:
    """Render an ARIP block as a human-readable AccurateRip report (CUETools-style).

    disc_id1/disc_id2/cddb_id reconstruct the original AR lookup fingerprint.
    """
    from cdda2img.rbi_format import (
        ARIP_STATUS_NOT_IN_DB,
        ARIP_STATUS_OK,
    )

    lines = [
        f"AccurateRip [ID: {arip.disc_id1:08x}-{arip.disc_id2:08x}-{arip.cddb_id:08x}]",
        "Track   [ CRC V1 | CRC V2 ]   Status",
    ]
    for i, t in enumerate(arip.tracks):
        v1_str = f"{t.v1_crc:08x}"
        v2_str = f"{t.v2_crc:08x}"
        if t.status == ARIP_STATUS_NOT_IN_DB:
            status_str = "Not in database"
        elif t.status == ARIP_STATUS_OK:
            c1, c2 = t.v1_confidence, t.v2_confidence
            if c1 > 0 and c2 > 0:
                conf_str = f"{c1:03d}+{c2:03d}/{t.db_total}"
            elif c1 > 0:
                conf_str = f"{c1:03d}/{t.db_total}"
            else:
                conf_str = f"V2:{c2:03d}/{t.db_total}"
            status_str = f"({conf_str}) Accurately ripped"
        else:  # MISMATCH
            status_str = f"(000/{t.db_total}) No match"
        lines.append(f" {i + 1:02d}     [{v1_str}|{v2_str}]   {status_str}")
    return "\n".join(lines)


def unpack_arip_block(data: bytes, track_count: int) -> RBIArip:
    """Deserialise an ARIP block into an RBIArip dataclass."""
    from cdda2img.rbi_format import (
        ARIP_HEADER_SIZE,
        ARIP_HEADER_STRUCT,
        ARIP_TRACK_SIZE,
        ARIP_TRACK_STRUCT,
        RBIArip,
        RBIAripTrack,
    )

    if len(data) < ARIP_HEADER_SIZE + ARIP_TRACK_SIZE * track_count:
        msg = f"ARIP block too short: {len(data)} bytes for {track_count} tracks"
        raise ValueError(msg)

    arip_version, disc_id1, disc_id2, cddb_id = struct.unpack_from(
        ARIP_HEADER_STRUCT, data, 0
    )

    tracks: list[RBIAripTrack] = []
    for i in range(track_count):
        offset = ARIP_HEADER_SIZE + i * ARIP_TRACK_SIZE
        v1, v2, conf_v1, conf_v2, db_total, status = struct.unpack_from(
            ARIP_TRACK_STRUCT, data, offset
        )
        tracks.append(
            RBIAripTrack(
                v1_crc=v1,
                v2_crc=v2,
                v1_confidence=conf_v1,
                v2_confidence=conf_v2,
                db_total=db_total,
                status=status,
            )
        )

    return RBIArip(
        arip_version=arip_version,
        disc_id1=disc_id1,
        disc_id2=disc_id2,
        cddb_id=cddb_id,
        tracks=tracks,
    )
