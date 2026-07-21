"""
test_fix_offset.py — unit tests for tools/fix_offset.py.

The tool lives in tools/ (not the installed package), so it is imported by
path. Only its pure logic is tested here; the end-to-end behaviour was proven
against a real rip (byte-exact +100/-100 round trip with cross-boundary
migration at all 10 track boundaries).
"""

from __future__ import annotations

import io
import sys
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

from fix_offset import Rip, _shifted_slice, load_tracks

_SPS = 588  # samples per sector


def _stream(n_samples: int) -> bytes:
    """Distinguishable content: sample i has both channels set to i."""
    return b"".join((i & 0xFFFF).to_bytes(2, "little") * 2 for i in range(n_samples))


def _fh(data: bytes):
    return io.BytesIO(data)


# ---------------------------------------------------------------------------
# _shifted_slice — the sample-migration core
# ---------------------------------------------------------------------------


def test_shifted_slice_interior_track_takes_from_its_neighbours() -> None:
    """A shifted interior track is a plain window into the disc stream — it
    reaches into the next track and gives up samples to the previous one."""
    total = 30
    data = _stream(total)
    got = _shifted_slice(_fh(data), total, start=10, length=10, offset=3)
    assert got == data[13 * 4 : 23 * 4]


def test_shifted_slice_zero_fills_before_the_start_of_the_disc() -> None:
    """A negative offset on track 1 has no earlier audio to draw on: the head
    is zeros, and nothing else moves."""
    total = 20
    data = _stream(total)
    got = _shifted_slice(_fh(data), total, start=0, length=5, offset=-2)
    assert got == bytes(2 * 4) + data[: 3 * 4]


def test_shifted_slice_zero_fills_past_the_lead_out() -> None:
    total = 20
    data = _stream(total)
    got = _shifted_slice(_fh(data), total, start=15, length=5, offset=3)
    assert got == data[18 * 4 :] + bytes(3 * 4)


def test_shifted_slice_offset_beyond_the_disc_is_all_zeros() -> None:
    total = 20
    got = _shifted_slice(_fh(_stream(total)), total, start=0, length=5, offset=-99)
    assert got == bytes(5 * 4)


def test_shifted_slice_zero_offset_is_the_identity() -> None:
    total = 20
    data = _stream(total)
    assert _shifted_slice(_fh(data), total, 5, 10, 0) == data[5 * 4 : 15 * 4]


def test_shifted_slice_preserves_length_at_every_offset() -> None:
    total = 40
    data = _stream(total)
    for offset in range(-50, 51):
        got = _shifted_slice(_fh(data), total, start=10, length=10, offset=offset)
        assert len(got) == 10 * 4, offset


# ---------------------------------------------------------------------------
# Rip geometry
# ---------------------------------------------------------------------------


def test_rip_geometry_from_track_lengths() -> None:
    rip = Rip(pcm_path=Path("x"), lengths=[10 * _SPS, 20 * _SPS, 5 * _SPS], sources=[])
    assert rip.track_lsns == [0, 10, 30]
    assert rip.disc_last_lsn == 34  # 35 sectors, last index 34
    assert rip.total_samples == 35 * _SPS


def test_rip_geometry_honours_a_program_area_pregap() -> None:
    """A disc whose track 1 does not start at LSN 0 (ABBA "Gold" has 33 frames)
    cannot be reconstructed from file lengths alone — --pregap supplies it, and
    every LSN including the lead-out must shift with it."""
    # Realistic track lengths: the CDDB id quantises to whole seconds, so a
    # 33-frame shift only changes it on a disc of plausible size.
    lengths = [15_000 * _SPS, 20_000 * _SPS]
    plain = Rip(pcm_path=Path("x"), lengths=lengths, sources=[])
    shifted = Rip(pcm_path=Path("x"), lengths=lengths, sources=[], pregap=33)
    assert shifted.track_lsns == [t + 33 for t in plain.track_lsns]
    assert shifted.disc_last_lsn == plain.disc_last_lsn + 33
    assert shifted.cddb_id != plain.cddb_id


# ---------------------------------------------------------------------------
# load_tracks guards
# ---------------------------------------------------------------------------


def _write_wav(path: Path, n_samples: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(_stream(n_samples))


def test_load_tracks_concatenates_and_records_lengths(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    _write_wav(src / "01.wav", 2 * _SPS)
    _write_wav(src / "02.wav", 3 * _SPS)
    rip = load_tracks([src], tmp_path)
    assert rip.lengths == [2 * _SPS, 3 * _SPS]
    assert rip.pcm_path.stat().st_size == 5 * _SPS * 4
    assert [p.name for p in rip.sources] == ["01.wav", "02.wav"]


def test_load_tracks_rejects_a_track_that_is_not_whole_sectors(tmp_path: Path) -> None:
    """Offsets are a CD concept; a file that is not a whole number of sectors
    has already been edited, and shifting it would be meaningless."""
    src = tmp_path / "src"
    src.mkdir()
    _write_wav(src / "01.wav", _SPS + 7)
    with pytest.raises(SystemExit, match="not a whole number of 588-sample sectors"):
        load_tracks([src], tmp_path)
