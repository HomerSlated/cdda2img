"""
test_container.py — RBI v2.0 container roundtrip tests.

Covers: header fields, checksum integrity, TOC parse round-trip, RG block
serialisation round-trip, FLAC extraction with embedded RG tags, and the
no-RG-block code path.
"""

import hashlib
from pathlib import Path

import av
import pytest

from cdda2img.concat import concat_wav
from cdda2img.container import build_container, extract_data, read_header, wav_to_raw_pcm
from cdda2img.rbi_format import (
    FLAG_MASTER_MODE,
    FLAG_RG_PRESENT,
    PCM_BIT_DEPTH,
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
    VERSION_MAJOR,
    VERSION_MINOR,
    RBIDisc,
)
from cdda2img.replaygain import analyse, pack_rg_block, unpack_rg_block
from cdda2img.toc import build_toc_entries, generate_toc, get_track_durations
from cdda2img.toc_parser import parse_toc
from cdda2img.track_extract import collect_track_flac_paths
from cdda2img.transcode import transcode_audio

_EXAMPLE_TRACKS = [
    Path("example/Koiduuni.mp3"),
    Path("example/Action Strike.mp3"),
]

pytestmark = pytest.mark.skipif(
    not all(p.exists() for p in _EXAMPLE_TRACKS),
    reason="example audio files not present",
)


# ---------------------------------------------------------------------------
# Shared fixtures (module-scoped to avoid repeated transcode + RG measurement)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def wav_tracks(tmp_path_factory):
    """Transcode example MPs to Red Book WAVs once per test session."""
    tmp = tmp_path_factory.mktemp("wavs")
    wavs = []
    for src in _EXAMPLE_TRACKS:
        out = tmp / f"{src.stem}.wav"
        transcode_audio(src, out)
        wavs.append(out)
    return wavs


@pytest.fixture(scope="module")
def built_containers(tmp_path_factory, wav_tracks):
    """Build one RBI with an RG block and one without. Returns a dict with keys
    'rg' and 'no_rg', each a tuple of (rbi_path, disc, rg_result_or_None).
    """
    tmp = tmp_path_factory.mktemp("containers")

    disc = RBIDisc(album="Test Album", artist="Test Artist", disc_number=1, disc_total=1)
    durations = get_track_durations(wav_tracks)
    disc.tracks = build_toc_entries(_EXAMPLE_TRACKS, durations, disc)
    toc_data = generate_toc(disc)

    concat = tmp / "all.wav"
    pcm = tmp / "all.pcm"
    concat_wav(wav_tracks, concat)
    wav_to_raw_pcm(concat, pcm)

    rg_result = analyse(wav_tracks)
    rg_block = pack_rg_block(rg_result)

    rbi_rg = tmp / "test_rg.rbi"
    rbi_no_rg = tmp / "test_no_rg.rbi"

    build_container(pcm, toc_data, disc, rbi_rg, rg_block=rg_block)
    build_container(pcm, toc_data, disc, rbi_no_rg, rg_block=None)

    return {
        "rg": (rbi_rg, disc, rg_result),
        "no_rg": (rbi_no_rg, disc, None),
    }


# ---------------------------------------------------------------------------
# Header round-trip
# ---------------------------------------------------------------------------


def test_header_fields_with_rg(built_containers):
    rbi, disc, _ = built_containers["rg"]
    h = read_header(rbi)

    assert h.version_major == VERSION_MAJOR
    assert h.version_minor == VERSION_MINOR
    assert h.track_count == len(_EXAMPLE_TRACKS)
    assert h.disc_number == 1
    assert h.disc_total == 1
    assert h.pcm_sample_rate == PCM_SAMPLE_RATE
    assert h.pcm_channels == PCM_CHANNELS
    assert h.pcm_bit_depth == PCM_BIT_DEPTH
    assert h.has_rg
    assert bool(h.flags & FLAG_RG_PRESENT)
    assert h.rg_start > 0
    assert h.rg_end > h.rg_start


def test_header_fields_without_rg(built_containers):
    rbi, _, _ = built_containers["no_rg"]
    h = read_header(rbi)

    assert not h.has_rg
    assert not bool(h.flags & FLAG_RG_PRESENT)
    assert h.rg_start == 0
    assert h.rg_end == 0


# ---------------------------------------------------------------------------
# Checksum integrity
# ---------------------------------------------------------------------------


def test_checksums_pass(built_containers):
    """All three SHA-256 checksums in the header match the actual block bytes."""
    rbi, _, _ = built_containers["rg"]
    h = read_header(rbi)

    with open(rbi, "rb") as f:
        f.seek(h.toc_start)
        toc_bytes = f.read(h.toc_length)

        f.seek(h.pcm_start)
        pcm_bytes = f.read(h.pcm_length)

        f.seek(h.rg_start)
        rg_bytes = f.read(h.rg_length)

    assert hashlib.sha256(toc_bytes).digest() == h.toc_checksum, "TOC checksum mismatch"
    assert hashlib.sha256(pcm_bytes).digest() == h.pcm_checksum, "PCM checksum mismatch"
    assert hashlib.sha256(rg_bytes).digest() == h.rg_checksum, "RG checksum mismatch"


# ---------------------------------------------------------------------------
# TOC round-trip
# ---------------------------------------------------------------------------


def test_toc_roundtrip(built_containers):
    """TOC bytes written into the container re-parse to the correct disc structure."""
    rbi, disc, _ = built_containers["rg"]
    h = read_header(rbi)

    with open(rbi, "rb") as f:
        f.seek(h.toc_start)
        toc_bytes = f.read(h.toc_length)

    parsed = parse_toc(toc_bytes)

    assert parsed.title == disc.album
    assert parsed.performer == disc.artist
    assert len(parsed.tracks) == len(_EXAMPLE_TRACKS)
    for i, track in enumerate(parsed.tracks):
        assert track.track_number == i + 1
        assert track.duration_frames > 0


# ---------------------------------------------------------------------------
# RG block serialisation round-trip
# ---------------------------------------------------------------------------


def test_rg_block_roundtrip(built_containers):
    """RG values survive pack→embed→read→unpack; all fields match within float tolerance."""
    rbi, _, rg_result = built_containers["rg"]
    h = read_header(rbi)

    assert h.has_rg

    with open(rbi, "rb") as f:
        f.seek(h.rg_start)
        rg_raw = f.read(h.rg_length)

    unpacked = unpack_rg_block(rg_raw, h.track_count)

    assert unpacked.rg_reference == pytest.approx(rg_result.reference, abs=0.01)
    assert unpacked.album_gain == pytest.approx(rg_result.album_gain, abs=0.01)
    assert unpacked.album_peak == pytest.approx(rg_result.album_peak, abs=0.001)
    assert unpacked.album_range == pytest.approx(rg_result.album_lra, abs=0.01)
    assert len(unpacked.track_gain) == len(_EXAMPLE_TRACKS)

    for i, t in enumerate(rg_result.tracks):
        assert unpacked.track_gain[i] == pytest.approx(t.gain, abs=0.01)
        assert unpacked.track_peak[i] == pytest.approx(t.peak, abs=0.001)
        assert unpacked.track_range[i] == pytest.approx(t.lra, abs=0.01)


# ---------------------------------------------------------------------------
# FLAC extraction with RG tag embedding
# ---------------------------------------------------------------------------


def test_flac_extraction_rg_tags(tmp_path, built_containers):
    """Extracted FLACs carry uppercase RG Vorbis comment tags matching stored values."""
    rbi, disc, rg_result = built_containers["rg"]
    h = read_header(rbi)

    with open(rbi, "rb") as f:
        f.seek(h.toc_start)
        toc_bytes = f.read(h.toc_length)
    parsed_disc = parse_toc(toc_bytes)

    extract_data(rbi, raw_dir=None, tracks=True, base_dir=tmp_path, embed_rg=True)

    flac_paths = collect_track_flac_paths(parsed_disc, h.disc_number, h.disc_total, tmp_path)
    assert len(flac_paths) == len(_EXAMPLE_TRACKS)

    for i, flac in enumerate(flac_paths):
        assert flac.exists(), f"Expected FLAC not found: {flac}"

        with av.open(str(flac)) as c:
            tags = {k.upper(): v for k, v in c.metadata.items()}

        assert "REPLAYGAIN_TRACK_GAIN" in tags
        assert "REPLAYGAIN_TRACK_PEAK" in tags
        assert "REPLAYGAIN_ALBUM_GAIN" in tags
        assert "REPLAYGAIN_REFERENCE_LOUDNESS" in tags

        stored_gain = rg_result.tracks[i].gain
        tag_gain = float(tags["REPLAYGAIN_TRACK_GAIN"].removesuffix(" dB"))
        assert tag_gain == pytest.approx(stored_gain, abs=0.01)

        stored_peak = rg_result.tracks[i].peak
        tag_peak = float(tags["REPLAYGAIN_TRACK_PEAK"])
        assert tag_peak == pytest.approx(stored_peak, abs=0.001)


# ---------------------------------------------------------------------------
# FLAG_MASTER_MODE
# ---------------------------------------------------------------------------


def test_master_mode_flag(tmp_path_factory, wav_tracks):
    """FLAG_MASTER_MODE is set when extra_flags carries it; is_master reflects correctly."""
    tmp = tmp_path_factory.mktemp("master_flag")
    pcm = tmp / "all.pcm"
    concat = tmp / "all.wav"
    concat_wav(wav_tracks, concat)
    wav_to_raw_pcm(concat, pcm)

    disc = RBIDisc(album="Test Album", artist="Test Artist")
    durations = get_track_durations(wav_tracks)
    disc.tracks = build_toc_entries(_EXAMPLE_TRACKS, durations, disc)
    toc_data = generate_toc(disc)

    rbi_master = tmp / "master.rbi"
    rbi_remaster = tmp / "remaster.rbi"
    build_container(pcm, toc_data, disc, rbi_master, extra_flags=FLAG_MASTER_MODE)
    build_container(pcm, toc_data, disc, rbi_remaster, extra_flags=0)

    h_master = read_header(rbi_master)
    h_remaster = read_header(rbi_remaster)

    assert h_master.is_master
    assert bool(h_master.flags & FLAG_MASTER_MODE)
    assert not h_remaster.is_master
    assert not bool(h_remaster.flags & FLAG_MASTER_MODE)
