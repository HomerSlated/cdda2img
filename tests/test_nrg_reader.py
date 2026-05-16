"""
test_nrg_reader.py — Unit tests for the Nero NRG importer.

All tests use a synthetic NRG fixture built from struct.pack; no real audio
files are required and no files from private/ are referenced.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from cdda2img.nrg_reader import (
    _BLK_CDTX,
    _BLK_DAOX,
    _BLK_MTYP,
    _CDDA_SECTOR_BYTES,
    _DAO_HDR_SIZE,
    _DAOX_TRACK_FMT,
    _SIG_NER5,
    _SIG_NERO,
    _detect_format,
    import_nrg,
)

# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------

_SEC = _CDDA_SECTOR_BYTES  # 2352 bytes per sector


def _sector(pattern: bytes) -> bytes:
    """Fill one 2352-byte sector with *pattern* (repeated or truncated)."""
    tile = (pattern * ((_SEC // len(pattern)) + 1))[:_SEC]
    return tile


def _make_daox_block(
    mcn: bytes = b"",
    tracks: list[tuple[bytes, int, int, int]] | None = None,
) -> bytes:
    """Build a DAOX block body.

    *tracks* is a list of (isrc_12, pregap_offset, start_offset, end_offset).
    """
    if tracks is None:
        tracks = []
    n = len(tracks)
    mcn_padded = mcn[:13].ljust(13, b"\x00")
    hdr = b"\x00" * 4 + mcn_padded + b"\x00\x01\x00" + bytes([1, n])
    assert len(hdr) == _DAO_HDR_SIZE
    body = b""
    for isrc_raw, pregap_off, start_off, end_off in tracks:
        isrc_padded = isrc_raw[:12].ljust(12, b"\x00")
        body += struct.pack(
            _DAOX_TRACK_FMT, isrc_padded, 2352, 0x07, pregap_off, start_off, end_off
        )
    return hdr + body


def _make_trailer(blocks: list[tuple[bytes, bytes]]) -> bytes:
    """Serialise trailer blocks as block_id(4) + length(4 BE) + data."""
    out = b""
    for block_id, data in blocks:
        out += block_id + struct.pack(">I", len(data)) + data
    out += b"END!" + struct.pack(">I", 0)
    return out


def _build_ner5(audio_data: bytes, trailer: bytes) -> bytes:
    """Assemble a complete NER5 NRG file."""
    trailer_offset = len(audio_data)
    footer = _SIG_NER5 + struct.pack(">Q", trailer_offset)
    return audio_data + trailer + footer


def _build_nero(audio_data: bytes, trailer: bytes) -> bytes:
    """Assemble a complete NERO (old format) NRG file."""
    trailer_offset = len(audio_data)
    footer = _SIG_NERO + struct.pack(">I", trailer_offset)
    return audio_data + trailer + footer


# Standard 2-track test fixture
#
# Layout:
#   offset 0          : track 1 pregap  (2 sectors, pattern 0xdead) — will be SKIPPED
#   offset 2*_SEC     : track 1 audio   (10 sectors, pattern 0x1122)
#   offset 12*_SEC    : track 2 pregap  (2 sectors, pattern 0xbeef)
#   offset 14*_SEC    : track 2 audio   (15 sectors, pattern 0x3344)
#
T1_PREGAP_OFF = 0
T1_START_OFF = 2 * _SEC
T1_END_OFF = 12 * _SEC
T2_PREGAP_OFF = 12 * _SEC
T2_START_OFF = 14 * _SEC
T2_END_OFF = 29 * _SEC


def _make_standard_audio() -> bytes:
    return (
        _sector(b"\xde\xad") * 2  # track 1 pregap (skipped)
        + _sector(b"\x11\x22") * 10  # track 1 audio
        + _sector(b"\xbe\xef") * 2  # track 2 pregap
        + _sector(b"\x33\x44") * 15  # track 2 audio
    )


def _make_standard_nrg(
    mcn: bytes = b"5099767013432",
    t1_isrc: bytes = b"GBAYE9300001",
    t2_isrc: bytes = b"GBAYE9300002",
    extra_blocks: list[tuple[bytes, bytes]] | None = None,
) -> bytes:
    daox = _make_daox_block(
        mcn=mcn,
        tracks=[
            (t1_isrc, T1_PREGAP_OFF, T1_START_OFF, T1_END_OFF),
            (t2_isrc, T2_PREGAP_OFF, T2_START_OFF, T2_END_OFF),
        ],
    )
    blocks: list[tuple[bytes, bytes]] = [(_BLK_DAOX, daox)]
    if extra_blocks:
        blocks.extend(extra_blocks)
    return _build_ner5(_make_standard_audio(), _make_trailer(blocks))


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def test_detect_ner5(tmp_path: Path) -> None:
    nrg = _make_standard_nrg()
    p = tmp_path / "test.nrg"
    p.write_bytes(nrg)
    with open(p, "rb") as f:
        new_fmt, offset = _detect_format(f)
    assert new_fmt is True
    assert offset == len(_make_standard_audio())


def test_detect_nero(tmp_path: Path) -> None:
    audio = _sector(b"\x11\x22") * 4
    daox = _make_daox_block(
        tracks=[(b"GBAYE9300001", 0, 0, len(audio))],
    )
    trailer = _make_trailer([(_BLK_DAOX, daox)])
    nrg = _build_nero(audio, trailer)
    p = tmp_path / "test.nrg"
    p.write_bytes(nrg)
    with open(p, "rb") as f:
        new_fmt, offset = _detect_format(f)
    assert new_fmt is False
    assert offset == len(audio)


def test_detect_rejects_garbage(tmp_path: Path) -> None:
    p = tmp_path / "notanrg.nrg"
    p.write_bytes(b"\x00" * 64)
    with open(p, "rb") as f, pytest.raises(ValueError, match="NER5/NERO"):
        _detect_format(f)


# ---------------------------------------------------------------------------
# Full import — disc metadata
# ---------------------------------------------------------------------------


def test_import_nrg_track_count(tmp_path: Path) -> None:
    p = tmp_path / "test.nrg"
    p.write_bytes(_make_standard_nrg())
    disc, _ = import_nrg(p, tmp_path / "out.pcm")
    assert len(disc.tracks) == 2


def test_import_nrg_catalog(tmp_path: Path) -> None:
    p = tmp_path / "test.nrg"
    p.write_bytes(_make_standard_nrg(mcn=b"5099767013432"))
    disc, _ = import_nrg(p, tmp_path / "out.pcm")
    assert disc.catalog == "5099767013432"


def test_import_nrg_null_mcn_gives_none(tmp_path: Path) -> None:
    p = tmp_path / "test.nrg"
    p.write_bytes(_make_standard_nrg(mcn=b"\x00" * 13))
    disc, _ = import_nrg(p, tmp_path / "out.pcm")
    assert disc.catalog is None


def test_import_nrg_isrc(tmp_path: Path) -> None:
    p = tmp_path / "test.nrg"
    p.write_bytes(_make_standard_nrg())
    disc, _ = import_nrg(p, tmp_path / "out.pcm")
    assert disc.tracks[0].isrc == "GBAYE9300001"
    assert disc.tracks[1].isrc == "GBAYE9300002"


def test_import_nrg_null_isrc_gives_none(tmp_path: Path) -> None:
    p = tmp_path / "test.nrg"
    p.write_bytes(_make_standard_nrg(t1_isrc=b"\x00" * 12, t2_isrc=b"\x00" * 12))
    disc, _ = import_nrg(p, tmp_path / "out.pcm")
    assert disc.tracks[0].isrc is None
    assert disc.tracks[1].isrc is None


def test_import_nrg_track1_pregap_zero(tmp_path: Path) -> None:
    """Track 1 lead-in pre-gap must not be stored (pregap_frames=0, start_frame=0)."""
    p = tmp_path / "test.nrg"
    p.write_bytes(_make_standard_nrg())
    disc, _ = import_nrg(p, tmp_path / "out.pcm")
    assert disc.tracks[0].pregap_frames == 0
    assert disc.tracks[0].start_frame == 0


def test_import_nrg_track2_pregap_nonzero(tmp_path: Path) -> None:
    p = tmp_path / "test.nrg"
    p.write_bytes(_make_standard_nrg())
    disc, _ = import_nrg(p, tmp_path / "out.pcm")
    # Track 2 pregap: (T2_START_OFF - T2_PREGAP_OFF) / 2352 = 2*_SEC / 2352 = 2
    assert disc.tracks[1].pregap_frames == 2


def test_import_nrg_duration_frames(tmp_path: Path) -> None:
    p = tmp_path / "test.nrg"
    p.write_bytes(_make_standard_nrg())
    disc, _ = import_nrg(p, tmp_path / "out.pcm")
    assert disc.tracks[0].duration_frames == 10
    assert disc.tracks[1].duration_frames == 15


def test_import_nrg_start_frames(tmp_path: Path) -> None:
    p = tmp_path / "test.nrg"
    p.write_bytes(_make_standard_nrg())
    disc, _ = import_nrg(p, tmp_path / "out.pcm")
    assert disc.tracks[0].start_frame == 0
    # Track 2 starts after track 1's 10 audio frames
    assert disc.tracks[1].start_frame == 10


# ---------------------------------------------------------------------------
# Full import — PCM output
# ---------------------------------------------------------------------------


def test_import_nrg_pcm_size(tmp_path: Path) -> None:
    """PCM output = track1_audio + track2_pregap + track2_audio, no track1 pregap."""
    p = tmp_path / "test.nrg"
    p.write_bytes(_make_standard_nrg())
    out = tmp_path / "out.pcm"
    import_nrg(p, out)
    expected = (10 + 2 + 15) * _SEC
    assert out.stat().st_size == expected


def test_import_nrg_pcm_passthrough(tmp_path: Path) -> None:
    """NRG is s16le — audio bytes must be copied without modification."""
    p = tmp_path / "test.nrg"
    p.write_bytes(_make_standard_nrg())
    out = tmp_path / "out.pcm"
    import_nrg(p, out)
    pcm = out.read_bytes()
    # Track 1 audio: 10 sectors of b"\x11\x22" — unchanged in output
    t1_audio = pcm[: 10 * _SEC]
    assert t1_audio == b"\x11\x22" * (10 * _SEC // 2)


def test_import_nrg_track1_pregap_not_in_pcm(tmp_path: Path) -> None:
    """The 0xdead track-1 pre-gap pattern must not appear in the output."""
    p = tmp_path / "test.nrg"
    p.write_bytes(_make_standard_nrg())
    out = tmp_path / "out.pcm"
    import_nrg(p, out)
    pcm = out.read_bytes()
    assert b"\xde\xad" * 4 not in pcm


def test_import_nrg_track2_pregap_in_pcm(tmp_path: Path) -> None:
    """Track 2 pre-gap bytes must appear unchanged in output (NRG is s16le, no swap)."""
    p = tmp_path / "test.nrg"
    p.write_bytes(_make_standard_nrg())
    out = tmp_path / "out.pcm"
    import_nrg(p, out)
    pcm = out.read_bytes()
    # Track 2 pregap starts right after track 1 audio (10 sectors)
    t2_pregap = pcm[10 * _SEC : 12 * _SEC]
    assert t2_pregap == b"\xbe\xef" * (2 * _SEC // 2)


# ---------------------------------------------------------------------------
# CD-Text
# ---------------------------------------------------------------------------


def _make_cdtx_pack(pti: int, track: int, seq: int, text_12: bytes) -> bytes:
    """Build one 18-byte CD-Text pack (no CRC — parser ignores it)."""
    return (
        bytes([pti, track, seq, 0x00]) + text_12[:12].ljust(12, b"\x00") + b"\x00\x00"
    )


def _make_cdtx_block(disc_title: str, disc_performer: str) -> bytes:
    """Minimal CDTX block with disc-level TITLE and PERFORMER strings."""
    packs = b""
    # PTI 0x80 TITLE: NUL-terminated disc title, then per-track titles
    t_raw = disc_title.encode("iso-8859-1") + b"\x00"
    packs += _make_cdtx_pack(0x80, 0, 0, t_raw[:12])
    # PTI 0x81 PERFORMER
    p_raw = disc_performer.encode("iso-8859-1") + b"\x00"
    packs += _make_cdtx_pack(0x81, 0, 0, p_raw[:12])
    return packs


def test_import_nrg_cdtx_applied(tmp_path: Path) -> None:
    cdtx = _make_cdtx_block("Technotronic", "Technotronic")
    p = tmp_path / "test.nrg"
    p.write_bytes(_make_standard_nrg(extra_blocks=[(_BLK_CDTX, cdtx)]))
    disc, _ = import_nrg(p, tmp_path / "out.pcm")
    assert disc.album == "Technotronic"
    assert disc.artist == "Technotronic"


def test_import_nrg_no_cdtx_empty_strings(tmp_path: Path) -> None:
    p = tmp_path / "test.nrg"
    p.write_bytes(_make_standard_nrg())
    disc, _ = import_nrg(p, tmp_path / "out.pcm")
    assert disc.album == ""
    assert disc.artist == ""


# ---------------------------------------------------------------------------
# MTYP validation
# ---------------------------------------------------------------------------


def test_import_nrg_mtyp_cd_accepted(tmp_path: Path) -> None:
    mtyp = struct.pack(">I", 0x01)  # MEDIA_CD
    p = tmp_path / "test.nrg"
    p.write_bytes(_make_standard_nrg(extra_blocks=[(_BLK_MTYP, mtyp)]))
    disc, _ = import_nrg(p, tmp_path / "out.pcm")
    assert len(disc.tracks) == 2


def test_import_nrg_mtyp_dvd_rejected(tmp_path: Path) -> None:
    mtyp = struct.pack(">I", 0x10)  # DVD — not CD
    p = tmp_path / "test.nrg"
    p.write_bytes(_make_standard_nrg(extra_blocks=[(_BLK_MTYP, mtyp)]))
    with pytest.raises(ValueError, match="unsupported media type"):
        import_nrg(p, tmp_path / "out.pcm")


# ---------------------------------------------------------------------------
# Track validation errors
# ---------------------------------------------------------------------------


def _make_nrg_bad_mode(tmp_path: Path) -> Path:
    """NRG with track 1 mode set to 0x01 (data) instead of 0x07 (audio)."""
    audio = _sector(b"\x11\x22") * 4
    bad_isrc = b"\x00" * 12
    hdr = b"\x00" * 4 + b"\x00" * 13 + b"\x00\x01\x00" + bytes([1, 1])
    # Pack track with wrong mode code
    track_data = struct.pack(">12sHBxxx3Q", bad_isrc, 2352, 0x01, 0, 0, len(audio))
    daox = hdr + track_data
    trailer = _make_trailer([(_BLK_DAOX, daox)])
    p = tmp_path / "bad_mode.nrg"
    p.write_bytes(_build_ner5(audio, trailer))
    return p


def _make_nrg_bad_sector_size(tmp_path: Path) -> Path:
    """NRG with sector_size=2048 (data CD sector) instead of 2352."""
    audio = bytes(2048 * 4)
    bad_isrc = b"\x00" * 12
    hdr = b"\x00" * 4 + b"\x00" * 13 + b"\x00\x01\x00" + bytes([1, 1])
    track_data = struct.pack(">12sHBxxx3Q", bad_isrc, 2048, 0x07, 0, 0, 2048 * 4)
    daox = hdr + track_data
    trailer = _make_trailer([(_BLK_DAOX, daox)])
    p = tmp_path / "bad_sz.nrg"
    p.write_bytes(_build_ner5(audio, trailer))
    return p


def _make_nrg_multi_session(tmp_path: Path) -> Path:
    """NRG with two DAOX blocks (multi-session)."""
    audio = _sector(b"\x11\x22") * 4
    isrc = b"\x00" * 12
    daox_data = _make_daox_block(tracks=[(isrc, 0, 0, len(audio))])
    # Two identical DAOX blocks → multi-session
    trailer = _make_trailer([(_BLK_DAOX, daox_data), (_BLK_DAOX, daox_data)])
    p = tmp_path / "multi.nrg"
    p.write_bytes(_build_ner5(audio, trailer))
    return p


def test_import_nrg_bad_mode_rejected(tmp_path: Path) -> None:
    p = _make_nrg_bad_mode(tmp_path)
    with pytest.raises(ValueError, match="mode"):
        import_nrg(p, tmp_path / "out.pcm")


def test_import_nrg_bad_sector_size_rejected(tmp_path: Path) -> None:
    p = _make_nrg_bad_sector_size(tmp_path)
    with pytest.raises(ValueError, match="sector size"):
        import_nrg(p, tmp_path / "out.pcm")


def test_import_nrg_multi_session_rejected(tmp_path: Path) -> None:
    p = _make_nrg_multi_session(tmp_path)
    with pytest.raises(ValueError, match="multi-session"):
        import_nrg(p, tmp_path / "out.pcm")


def test_import_nrg_no_dao_block_rejected(tmp_path: Path) -> None:
    """File with only END! block (no DAOX) should raise ValueError."""
    audio = _sector(b"\x11\x22") * 2
    trailer = _make_trailer([])  # just END!
    p = tmp_path / "nodao.nrg"
    p.write_bytes(_build_ner5(audio, trailer))
    with pytest.raises(ValueError, match="no DAO track data"):
        import_nrg(p, tmp_path / "out.pcm")
