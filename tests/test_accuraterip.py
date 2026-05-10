"""
test_accuraterip.py — unit tests for accuraterip.py.

Sections:
  1. _ar_disc_ids  — LSN-based disc fingerprint (frozen vector + guard)
  2. _ar_checksums — per-track CRC accumulator (middle/first/last/overflow/padding)
  3. _parse_dbar   — AccurateRip binary response parser
  4. verify_rip    — integration: disc-not-found early return, zero-padding
"""

from __future__ import annotations

import array
import struct
from pathlib import Path
from unittest.mock import patch

from cdda2img.accuraterip import (
    _ar_checksums,
    _ar_disc_ids,
    _parse_dbar,
    verify_rip,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SKIP = 5 * 588  # 2940 — boundary exclusion per AR spec


def _frames(*values: int) -> array.array:
    """Build an array.array('I') from int values."""
    return array.array("I", values)


def _build_dbar(n_tracks: int, blocks: list[list[tuple[int, int, int]]]) -> bytes:
    """Serialise a dBAR binary from a list of blocks.

    Each block is a list of n_tracks (conf, v1_crc, v2_crc) tuples.
    Header id1/id2/cddb_id are all zero — _parse_dbar ignores them.
    """
    data = b""
    for block in blocks:
        data += struct.pack("<BLLL", n_tracks, 0, 0, 0)
        for conf, v1, v2 in block:
            data += struct.pack("<BLL", conf, v1, v2)
    return data


# ---------------------------------------------------------------------------
# 1. _ar_disc_ids
# ---------------------------------------------------------------------------

# Frozen vector: Technotronic "Pump Up the Jam: The Album" (12 tracks).
# track_lsns derived from the PQDESCR of the DDP 2.0 image (abs_frame - 150).
# leadout_frame = 259662 → disc_last_lsn = 259511.
_TECHNOTRONIC_LSNS = [
    0,
    24337,
    49832,
    69982,
    93097,
    107660,
    132412,
    156647,
    178840,
    205097,
    226850,
    246345,
]
_TECHNOTRONIC_LAST_LSN = 259511


def test_ar_disc_ids_technotronic() -> None:
    """Real 12-track disc: frozen vector verified against AccurateRip URL."""
    id1, id2 = _ar_disc_ids(_TECHNOTRONIC_LSNS, _TECHNOTRONIC_LAST_LSN)
    assert id1 == "001ab653"
    assert id2 == "00f80930"


def test_ar_disc_ids_lsn_zero_guard() -> None:
    """Track-1 LSN=0 uses 1 in the id2 weighted sum (lsn or 1 guard).

    Without the guard: id2 = (0*1 + 100*2) + 200*3 = 800 = 0x00000320
    With the guard:    id2 = (1*1 + 100*2) + 200*3 = 801 = 0x00000321
    """
    id1, id2 = _ar_disc_ids([0, 100], 199)
    assert id1 == "0000012c"  # (0 + 100 + 200) = 300
    assert id2 == "00000321"  # guard fires: 1*1 + 100*2 + 200*3 = 801


def test_ar_disc_ids_wraps_at_32_bits() -> None:
    """id1 and id2 are masked to 32 bits."""
    # All-max LSNs to exercise the modular reduction.
    lsns = [0xFFFFFFFF, 0xFFFFFFFF]
    id1, id2 = _ar_disc_ids(lsns, 0xFFFFFFFF)
    assert len(id1) == 8
    assert len(id2) == 8
    assert all(c in "0123456789abcdef" for c in id1 + id2)


# ---------------------------------------------------------------------------
# 2. _ar_checksums
# ---------------------------------------------------------------------------


def test_ar_checksums_middle_track_basic() -> None:
    """Middle track: all frames contribute. v1=v2=14 for [1,2,3]."""
    # track=2, total_tracks=3 → sum_from=0, sum_to=3
    # i=0 mult=1: 1*1=1; i=1 mult=2: 2*2=4; i=2 mult=3: 3*3=9 → csum_lo=14
    v1, v2 = _ar_checksums(_frames(1, 2, 3), track=2, total_tracks=3)
    assert v1 == 14
    assert v2 == 14


def test_ar_checksums_first_track_all_excluded() -> None:
    """First track with fewer than _SKIP_FRAMES elements → no frames contribute."""
    # sum_from = 2940; mult 1..5 all < 2940 → v1 = v2 = 0
    v1, v2 = _ar_checksums(_frames(1, 2, 3, 4, 5), track=1, total_tracks=2)
    assert v1 == 0
    assert v2 == 0


def test_ar_checksums_last_track_all_excluded() -> None:
    """Last track with fewer than _SKIP_FRAMES elements → sum_to < 0 → no frames."""
    # sum_to = 5 - 2940 = -2935; mult > 0 > -2935 never → v1 = v2 = 0
    v1, v2 = _ar_checksums(_frames(1, 2, 3, 4, 5), track=2, total_tracks=2)
    assert v1 == 0
    assert v2 == 0


def test_ar_checksums_multiplier_starts_at_one() -> None:
    """Multiplier is 1-based from the first frame (never mult=0)."""
    # Single middle-track frame: csum_lo = frames[0] * 1 = 7
    v1, v2 = _ar_checksums(_frames(7), track=2, total_tracks=3)
    assert v1 == 7
    assert v2 == 7


def test_ar_checksums_overflow_v2_differs_v1() -> None:
    """When product exceeds 32 bits, csum_hi accumulates and v2 ≠ v1.

    frames=[0, 0xFFFFFFFF], track=2/3 (middle, no boundary exclusion):
      i=0 mult=1: 0*1=0               → csum_lo+=0, csum_hi+=0
      i=1 mult=2: 0xFFFFFFFF*2 = 0x1FFFFFFFE
                                      → csum_lo+=0xFFFFFFFE, csum_hi+=1
    v1 = 0xFFFFFFFE, v2 = (0xFFFFFFFE + 1) & mask = 0xFFFFFFFF
    """
    v1, v2 = _ar_checksums(_frames(0, 0xFFFFFFFF), track=2, total_tracks=3)
    assert v1 == 0xFFFFFFFE
    assert v2 == 0xFFFFFFFF


def test_ar_checksums_first_track_boundary_inclusive() -> None:
    """Frame at mult==_SKIP_FRAMES is included (>= not >).

    Build an array whose first _SKIP_FRAMES-1 elements are zero and element
    _SKIP_FRAMES-1 (mult=_SKIP_FRAMES) is 1.  It must contribute.
    """
    vals = [0] * _SKIP + [1]  # 2941 frames; index 2940 has mult=2941
    # For track=1/2, sum_from=2940, sum_to=2941.
    # mult=2940 (frame 2939): included (2940 >= 2940 and 2940 <= 2941)
    # mult=2941 (frame 2940): included (2941 >= 2940 and 2941 <= 2941)
    # Only frame 2940 is non-zero: product = 1 * 2941
    v1, _v2 = _ar_checksums(array.array("I", vals), track=1, total_tracks=2)
    assert v1 == 2941


def test_ar_checksums_padding_differs_from_clipping() -> None:
    """Zero-padding extends n → increases sum_to → includes boundary frames.

    Design (single-track disc, track=1=last):
      n_core frames, of which frames at indices [sum_to_clipped..sum_to_padded-1]
      are non-zero.  Appending n_pad zero frames (the "drive offset" padding)
      increases n and therefore sum_to so those frames become included.

    sum_from = _SKIP (first track)
    sum_to_core    = n_core - _SKIP
    sum_to_padded  = (n_core + n_pad) - _SKIP  =  sum_to_core + n_pad
    Non-zero region: indices [sum_to_core .. sum_to_core + n_pad - 1]
                   → included in padded, excluded in clipped.
    """
    n_pad = 10
    n_core = _SKIP * 3  # 8820; well above 2*_SKIP so sum_to_core > 0

    sum_to_core = n_core - _SKIP  # 5880
    vals_core = [0] * n_core
    for idx in range(sum_to_core, sum_to_core + n_pad):
        vals_core[idx] = 1  # non-zero in the boundary region

    arr_clipped = array.array("I", vals_core)
    arr_padded = array.array("I", vals_core + [0] * n_pad)

    v1_clipped, _ = _ar_checksums(arr_clipped, track=1, total_tracks=1)
    v1_padded, _ = _ar_checksums(arr_padded, track=1, total_tracks=1)

    assert v1_clipped != v1_padded


# ---------------------------------------------------------------------------
# 3. _parse_dbar
# ---------------------------------------------------------------------------


def test_parse_dbar_empty() -> None:
    assert _parse_dbar(b"", n_tracks=1) == []


def test_parse_dbar_two_blocks_two_tracks() -> None:
    """Happy path: two consecutive blocks, two tracks each."""
    data = _build_dbar(
        n_tracks=2,
        blocks=[
            [(10, 0xAAAAAAAA, 0xBBBBBBBB), (5, 0xCCCCCCCC, 0xDDDDDDDD)],
            [(3, 0xEEEEEEEE, 0xFFFFFFFF), (7, 0x12345678, 0x9ABCDEF0)],
        ],
    )
    result = _parse_dbar(data, n_tracks=2)

    assert len(result) == 2
    assert result[0][0] == {"conf": 10, "v1": 0xAAAAAAAA, "v2": 0xBBBBBBBB}
    assert result[0][1] == {"conf": 5, "v1": 0xCCCCCCCC, "v2": 0xDDDDDDDD}
    assert result[1][0] == {"conf": 3, "v1": 0xEEEEEEEE, "v2": 0xFFFFFFFF}
    assert result[1][1] == {"conf": 7, "v1": 0x12345678, "v2": 0x9ABCDEF0}


def test_parse_dbar_truncated_block_is_ignored() -> None:
    """A partial final block (too few bytes) is silently skipped by the while guard."""
    full = _build_dbar(n_tracks=1, blocks=[[(5, 0x11111111, 0x22222222)]])
    truncated = full + b"\x01\x00\x00\x00"  # incomplete second block
    result = _parse_dbar(truncated, n_tracks=1)
    assert len(result) == 1
    assert result[0][0]["conf"] == 5


def test_parse_dbar_wrong_track_count_first_block_returns_empty() -> None:
    """If the very first header has the wrong n_tracks, parsing stops → []."""
    data = _build_dbar(n_tracks=3, blocks=[[(1, 0, 0), (2, 0, 0), (3, 0, 0)]])
    result = _parse_dbar(data, n_tracks=2)  # caller expects 2, file says 3
    assert result == []


def test_parse_dbar_wrong_track_count_second_block_stops() -> None:
    """Mismatch in the second block header stops parsing; first block is returned."""
    # Build 1-track block 1, then a 2-track block 2 — caller expects 1.
    block1 = struct.pack("<BLLL", 1, 0, 0, 0) + struct.pack("<BLL", 9, 0xAA, 0xBB)
    block2 = struct.pack("<BLLL", 2, 0, 0, 0) + struct.pack("<BLL", 1, 0, 0) * 2
    result = _parse_dbar(block1 + block2, n_tracks=1)
    assert len(result) == 1
    assert result[0][0] == {"conf": 9, "v1": 0xAA, "v2": 0xBB}


# ---------------------------------------------------------------------------
# 4. verify_rip
# ---------------------------------------------------------------------------


def test_verify_rip_disc_not_in_database(tmp_path: Path) -> None:
    """When _fetch_ar returns None, all results have max_confidence=None."""
    pcm = tmp_path / "disc.pcm"
    pcm.write_bytes(bytes(100 * 2352))

    with patch("cdda2img.accuraterip._fetch_ar", return_value=None):
        results = verify_rip(pcm, track_lsns=[0, 50], disc_last_lsn=99)

    assert len(results) == 2
    assert all(r.max_confidence is None for r in results)
    assert all(r.confidence_v1 is None for r in results)
    assert all(r.confidence_v2 is None for r in results)


def test_verify_rip_last_track_zero_padding(tmp_path: Path) -> None:
    """drive_offset causes the last-track read window to extend past EOF.

    verify_rip must zero-pad the buffer (not clip it) so that _ar_checksums
    sees the correct n and sum_to.  The test:

      1. Writes a PCM file with non-zero frames at the clipping boundary.
      2. Manually constructs the padded frame array and computes the expected
         CRC via _ar_checksums (the reference implementation).
      3. Verifies the clipped CRC differs (test-setup sanity check).
      4. Patches _fetch_ar with a dBAR containing the padded CRC, then calls
         verify_rip and asserts the track matches at full confidence.

    Disc geometry:
      n_sectors = 20 sectors (single track), drive_offset = 30 samples.
      offset_bytes = 30 * 4 = 120.

    Last-track window:
      byte_start  = 0 * 2352 + 120 = 120   (track 1, lsn=0)
      byte_end    = 20 * 2352 + 120 = 47160 (one past last sector + offset)
      read from PCM file: [120 : 47040] = 46920 bytes
      zero-padded:        + 120 bytes = 47040 bytes total
      n_frames_padded     = 47040 // 4 = 11760

    Clipping (the bug): omit the 120 zero bytes → 46920 bytes → 11730 frames.

    Boundary frames (single-track: track=1=last):
      sum_to_padded  = 11760 - 2940 = 8820
      sum_to_clipped = 11730 - 2940 = 8790
      Frames at padded-array indices [8790 .. 8819] are in the valid zone for
      the padded sum_to (8820) but excluded by the clipped sum_to (8790).
      These frames correspond to PCM file bytes [35280 .. 35399].
    """
    drive_offset = 30
    offset_bytes = drive_offset * 4  # 120
    n_sectors = 20
    sector_size = 2352
    pcm_size = n_sectors * sector_size  # 47040

    # Mark 30 frames at the clipping boundary as non-zero (value 1).
    pcm_data = bytearray(pcm_size)
    clip_boundary_start = 8790  # padded-array index of first boundary frame
    for idx in range(clip_boundary_start, clip_boundary_start + drive_offset):
        file_byte = offset_bytes + idx * 4  # 120 + idx * 4
        pcm_data[file_byte : file_byte + 4] = struct.pack("<I", 1)

    pcm_path = tmp_path / "disc.pcm"
    pcm_path.write_bytes(bytes(pcm_data))

    # --- Reference: compute CRC from the padded buffer ---
    raw_padded = bytes(pcm_data[offset_bytes:]) + bytes(offset_bytes)
    assert len(raw_padded) == pcm_size  # sanity: 46920 + 120 = 47040
    frames_padded: array.array = array.array("I")
    frames_padded.frombytes(raw_padded)

    v1_expected, v2_expected = _ar_checksums(frames_padded, track=1, total_tracks=1)

    # --- Sanity: clipped CRC must differ ---
    raw_clipped = bytes(pcm_data[offset_bytes:])
    frames_clipped: array.array = array.array("I")
    frames_clipped.frombytes(raw_clipped[: len(raw_clipped) - len(raw_clipped) % 4])
    v1_clipped, _ = _ar_checksums(frames_clipped, track=1, total_tracks=1)
    assert v1_clipped != v1_expected, "test setup: clipped CRC must differ from padded"

    # --- Integration: verify_rip must produce the padded CRC ---
    dbar = struct.pack("<BLLL", 1, 0, 0, 0)  # header: 1 track
    dbar += struct.pack("<BLL", 15, v1_expected, v2_expected)  # conf=15

    with patch("cdda2img.accuraterip._fetch_ar", return_value=dbar):
        results = verify_rip(
            pcm_path,
            track_lsns=[0],
            disc_last_lsn=n_sectors - 1,
            drive_offset=drive_offset,
        )

    assert len(results) == 1
    assert results[0].confidence_v1 == 15, (
        "last track should match the zero-padded CRC; "
        "if confidence_v1 is None, the buffer was clipped instead of padded"
    )
