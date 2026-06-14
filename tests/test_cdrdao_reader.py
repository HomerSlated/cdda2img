"""
test_cdrdao_reader.py — Unit and integration tests for the cdrdao import pipeline.

Uses a synthetic two-track TOC + BIN fixture so the real Technotronic disc image
(which is private) is not required.  All byte counts are exact multiples of 2352
(one CD frame = 588 stereo s16 samples x 4 bytes).
"""

import struct
from pathlib import Path

import pytest

from cdda2img.cdrdao_reader import (
    _byteswap_s16,
    _find_bin_filename,
    convert_cdrdao_bin,
    import_cdrdao,
)
from cdda2img.toc_parser import parse_toc

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BYTES_PER_FRAME = 2352  # one CD frame of s16be/s16le stereo PCM

# Track 1: 75 frames (1 second), no pregap.
# Track 2: 15-frame pregap + 75-frame audio = 90-frame slot.
_T1_FRAMES = 75
_T2_PREGAP_FRAMES = 15
_T2_AUDIO_FRAMES = 75
_T2_SLOT_FRAMES = _T2_PREGAP_FRAMES + _T2_AUDIO_FRAMES  # 90

_BIN_TOTAL_FRAMES = _T1_FRAMES + _T2_SLOT_FRAMES  # 165

# Timestamps for the synthetic TOC (MM:SS:FF).
_T1_OFFSET = "00:00:00"
_T1_LENGTH = "00:01:00"  # 75 frames
_T2_OFFSET = "00:01:00"  # track 1 ends at frame 75
_T2_LENGTH = "00:01:15"  # 90 frames (pregap 15 + audio 75)
_T2_PREGAP = "00:00:15"

_SYNTHETIC_TOC = f"""\
CD_DA

CATALOG "5099746863722"

CD_TEXT {{
  LANGUAGE_MAP {{
    0: 9
  }}
  LANGUAGE 0 {{
    TITLE "Test Album"
    PERFORMER "Test Artist"
  }}
}}

// Track 1
TRACK AUDIO
NO COPY
NO PRE_EMPHASIS
TWO_CHANNEL_AUDIO
ISRC "GBAYE9300135"
CD_TEXT {{
  LANGUAGE 0 {{
    TITLE "Alpha"
    PERFORMER "Test Artist"
  }}
}}
FILE "test.bin" {_T1_OFFSET} {_T1_LENGTH}


// Track 2
TRACK AUDIO
NO COPY
NO PRE_EMPHASIS
TWO_CHANNEL_AUDIO
ISRC "GBAYE9300136"
CD_TEXT {{
  LANGUAGE 0 {{
    TITLE "Beta"
    PERFORMER "Test Artist"
  }}
}}
FILE "test.bin" {_T2_OFFSET} {_T2_LENGTH}
START {_T2_PREGAP}
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_synthetic_bin(path: Path, fill_be: int = 0x0100) -> None:
    """Write a synthetic s16be BIN with a recognisable fill value.

    fill_be=0x0100 → big-endian bytes b'\\x01\\x00' per sample.
    After byteswap: b'\\x00\\x01' = 1 in little-endian.
    """
    sample_bytes = struct.pack(">h", fill_be)  # 2 bytes per sample (big-endian)
    frame_bytes = sample_bytes * (588 * 2)  # 588 samples x 2 channels
    assert len(frame_bytes) == _BYTES_PER_FRAME
    with open(path, "wb") as f:
        for _ in range(_BIN_TOTAL_FRAMES):
            f.write(frame_bytes)


@pytest.fixture()
def synthetic_image(tmp_path: Path) -> tuple[Path, Path]:
    """Return (toc_path, bin_path) for the synthetic two-track image."""
    bin_path = tmp_path / "test.bin"
    toc_path = tmp_path / "test.toc"
    _make_synthetic_bin(bin_path)
    toc_path.write_text(_SYNTHETIC_TOC, encoding="utf-8")
    return toc_path, bin_path


# ---------------------------------------------------------------------------
# Unit tests: byte-swap
# ---------------------------------------------------------------------------


def test_byteswap_round_trip():
    """Applying byteswap twice returns the original data."""
    original = b"\x01\x02\x03\x04\xab\xcd"
    assert _byteswap_s16(_byteswap_s16(original)) == original


def test_byteswap_known_value():
    """b'\\x01\\x00' (BE 256) becomes b'\\x00\\x01' (LE 1) after swap."""
    be_sample = b"\x01\x00"
    le_sample = b"\x00\x01"
    assert _byteswap_s16(be_sample) == le_sample


def test_byteswap_silence():
    """All-zero data is unchanged by byteswap."""
    silence = b"\x00" * 64
    assert _byteswap_s16(silence) == silence


# ---------------------------------------------------------------------------
# Unit tests: TOC parsing with new fields
# ---------------------------------------------------------------------------


def test_parse_toc_catalog():
    parsed = parse_toc(_SYNTHETIC_TOC.encode())
    assert parsed.catalog == "5099746863722"


def test_parse_toc_all_zeros_catalog():
    toc = 'CD_DA\nCATALOG "0000000000000"\n// Track 1\nTRACK AUDIO\nFILE "x.bin" 00:00:00 00:01:00\n'
    parsed = parse_toc(toc.encode())
    assert parsed.catalog is None  # all-zeros MCN treated as absent


def test_parse_toc_isrc():
    parsed = parse_toc(_SYNTHETIC_TOC.encode())
    assert parsed.tracks[0].isrc == "GBAYE9300135"
    assert parsed.tracks[1].isrc == "GBAYE9300136"


def test_parse_toc_pregap():
    parsed = parse_toc(_SYNTHETIC_TOC.encode())
    t1, t2 = parsed.tracks
    assert t1.pregap_frames == 0
    assert t2.pregap_frames == 15
    assert t2.duration_frames == 75  # audio-only
    assert t2.audio_start_frame == 75 + 15  # BIN offset + pregap


def test_parse_toc_bare_zero_offset():
    """FILE entry with bare '0' offset (not 00:00:00) must not be silently dropped."""
    toc = (
        "CD_DA\n// Track 1\nTRACK AUDIO\n"
        'FILE "x.bin" 0 00:01:00\n'
        "// Track 2\nTRACK AUDIO\n"
        'FILE "x.bin" 00:01:00 00:01:00\n'
    )
    parsed = parse_toc(toc.encode())
    assert len(parsed.tracks) == 2
    assert parsed.tracks[0].start_frame == 0
    assert parsed.tracks[0].duration_frames == 75


def test_parse_toc_no_pregap_duration_unchanged():
    """Track without START: duration_frames equals the FILE length."""
    parsed = parse_toc(_SYNTHETIC_TOC.encode())
    t1 = parsed.tracks[0]
    assert t1.duration_frames == 75
    assert t1.audio_start_frame == 0


# cdrdao read-toc writes SILENCE directives (not stored in the WAV) for
# pre-gaps, while read-cd embeds them as real PCM in the BIN. The
# SILENCE/ZERO before FILE fix in parse_toc must produce the same INDEX 01
# positions (and thus the same MB disc-ID) from both styles.
_READ_TOC_STYLE = """\
CD_DA

// Track 1
TRACK AUDIO
NO COPY
NO PRE_EMPHASIS
TWO_CHANNEL_AUDIO
SILENCE 00:00:33
FILE "data.wav" 0 00:01:00
START 00:00:33

// Track 2
TRACK AUDIO
NO COPY
NO PRE_EMPHASIS
TWO_CHANNEL_AUDIO
FILE "data.wav" 00:01:00 00:01:15
"""

# Equivalent read-cd style: silence embedded in BIN, FILE offsets include it.
_READ_CD_STYLE = """\
CD_DA

// Track 1
TRACK AUDIO
NO COPY
NO PRE_EMPHASIS
TWO_CHANNEL_AUDIO
FILE "data.bin" 00:00:00 00:01:33
START 00:00:33

// Track 2
TRACK AUDIO
NO COPY
NO PRE_EMPHASIS
TWO_CHANNEL_AUDIO
FILE "data.bin" 00:01:33 00:01:15
"""


def test_parse_toc_silence_adjusts_start_frame():
    """SILENCE before FILE must shift subsequent track start_frame by silence duration."""
    parsed = parse_toc(_READ_TOC_STYLE.encode())
    t1, t2 = parsed.tracks
    # Track 1: SILENCE is the pre-gap; start_frame is unchanged (disc position 0).
    assert t1.start_frame == 0
    assert t1.pregap_frames == 33
    assert t1.duration_frames == 75  # audio-only; SILENCE is not in the WAV
    # Track 2: start_frame shifted forward by 33 (track 1's SILENCE).
    assert t2.start_frame == 75 + 33  # 108 = WAV offset (75) + accumulated silence (33)
    assert t2.pregap_frames == 0
    assert t2.audio_start_frame == 108  # INDEX 01 = INDEX 00 (no pre-gap)


def test_parse_toc_silence_index01_matches_read_cd():
    """read-toc and read-cd styles must produce identical INDEX 01 positions."""
    rtoc = parse_toc(_READ_TOC_STYLE.encode())
    rcd = parse_toc(_READ_CD_STYLE.encode())
    for tr, tc in zip(rtoc.tracks, rcd.tracks, strict=True):
        assert tr.audio_start_frame == tc.audio_start_frame, (
            f"track {tr.track_number}: read-toc INDEX 01 = {tr.audio_start_frame}, "
            f"read-cd INDEX 01 = {tc.audio_start_frame}"
        )


def test_parse_toc_silence_total_frames():
    """total_frames must include SILENCE bytes (disc space, not WAV size)."""
    from cdda2img.cdrdao_reader import parsed_to_rbi_disc

    rtoc = parse_toc(_READ_TOC_STYLE.encode())
    rcd = parse_toc(_READ_CD_STYLE.encode())
    assert parsed_to_rbi_disc(rtoc).total_frames == parsed_to_rbi_disc(rcd).total_frames


# ---------------------------------------------------------------------------
# Unit tests: BIN filename extraction
# ---------------------------------------------------------------------------


def test_find_bin_filename():
    assert _find_bin_filename(_SYNTHETIC_TOC) == "test.bin"


def test_find_bin_filename_missing():
    with pytest.raises(ValueError, match="No FILE entry"):
        _find_bin_filename("CD_DA\n\nTRACK AUDIO\n")


# ---------------------------------------------------------------------------
# Unit tests: convert_cdrdao_bin
# ---------------------------------------------------------------------------


def test_convert_bin_byte_order(synthetic_image, tmp_path):
    """After conversion the first two bytes of every frame are swapped."""
    _toc_path, bin_path = synthetic_image
    pcm_out = tmp_path / "out.s16le"
    convert_cdrdao_bin(bin_path, pcm_out)

    with open(pcm_out, "rb") as f:
        first_two = f.read(2)

    assert first_two == b"\x00\x01"  # 0x0100 big-endian → 0x0001 little-endian


def test_convert_bin_size_preserved(synthetic_image, tmp_path):
    """Output PCM file is the same size as the input BIN."""
    _toc_path, bin_path = synthetic_image
    pcm_out = tmp_path / "out.s16le"
    convert_cdrdao_bin(bin_path, pcm_out)
    assert pcm_out.stat().st_size == bin_path.stat().st_size


# ---------------------------------------------------------------------------
# Integration test: import_cdrdao → RBIDisc
# ---------------------------------------------------------------------------


def test_import_cdrdao_disc_metadata(synthetic_image, tmp_path):
    toc_path, _bin_path = synthetic_image
    pcm_out = tmp_path / "out.s16le"
    disc, _flags = import_cdrdao(toc_path, pcm_out)

    assert disc.album == "Test Album"
    assert disc.artist == "Test Artist"
    assert disc.catalog == "5099746863722"
    assert len(disc.tracks) == 2


def test_import_cdrdao_track_fields(synthetic_image, tmp_path):
    toc_path, _bin_path = synthetic_image
    pcm_out = tmp_path / "out.s16le"
    disc, _ = import_cdrdao(toc_path, pcm_out)

    t1, t2 = disc.tracks
    assert t1.isrc == "GBAYE9300135"
    assert t1.pregap_frames == 0
    assert t1.duration_frames == 75
    assert t1.start_frame == 0

    assert t2.isrc == "GBAYE9300136"
    assert t2.pregap_frames == 15
    assert t2.duration_frames == 75
    assert t2.start_frame == 75  # immediately after track 1 slot


def test_import_cdrdao_master_flag(synthetic_image, tmp_path):
    from cdda2img.rbi_format import FLAG_MASTER_MODE

    toc_path, _bin_path = synthetic_image
    pcm_out = tmp_path / "out.s16le"
    _disc, flags = import_cdrdao(toc_path, pcm_out)
    assert flags == FLAG_MASTER_MODE


def test_import_cdrdao_missing_bin(tmp_path):
    toc_path = tmp_path / "test.toc"
    toc_path.write_text(_SYNTHETIC_TOC, encoding="utf-8")
    pcm_out = tmp_path / "out.s16le"
    with pytest.raises(FileNotFoundError, match=r"test\.bin"):
        import_cdrdao(toc_path, pcm_out)


def test_import_cdrdao_total_frames(synthetic_image, tmp_path):
    """RBIDisc.total_frames accounts for both pregap and audio slots."""
    toc_path, _bin_path = synthetic_image
    pcm_out = tmp_path / "out.s16le"
    disc, _ = import_cdrdao(toc_path, pcm_out)
    # Track 1: 0 pregap + 75 audio = 75; Track 2: 15 pregap + 75 audio = 90
    assert disc.total_frames == 165
