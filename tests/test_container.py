"""
test_container.py — RBI v4.0 container roundtrip tests.

Covers: header fields, checksum integrity, TOC parse round-trip, RG block
serialisation round-trip, FLAC extraction with embedded RG tags, and the
no-RG-block code path.
"""

import hashlib
from pathlib import Path

import av
import pytest

from cdda2img.concat import concat_wav
from cdda2img.container import (
    build_container,
    extract_data,
    read_header,
    wav_to_raw_pcm,
)
from cdda2img.rbi_format import (
    BLOCK_TYPE_PROV,
    BLOCK_TYPE_RGDB,
    BLOCK_TYPE_TOC,
    FLAG_MASTER_MODE,
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

    disc = RBIDisc(
        album="Test Album", artist="Test Artist", disc_number=1, disc_total=1
    )
    durations = get_track_durations(wav_tracks)
    disc.tracks = build_toc_entries(_EXAMPLE_TRACKS, durations, disc)
    toc_data = generate_toc(disc)

    concat = tmp / "all.wav"
    pcm = tmp / "all.pcm"
    concat_wav(wav_tracks, concat)
    wav_to_raw_pcm(concat, pcm)

    rg_result = analyse(wav_tracks)
    rg_block = pack_rg_block(rg_result)

    prov = {"mode": "c", "source": "/test", "ripper": "file"}

    rbi_rg = tmp / "test_rg.rbi"
    rbi_no_rg = tmp / "test_no_rg.rbi"

    build_container(pcm, toc_data, disc, rbi_rg, rg_block=rg_block, prov_data=prov)
    build_container(pcm, toc_data, disc, rbi_no_rg, rg_block=None, prov_data=prov)

    return {
        "rg": (rbi_rg, disc, rg_result),
        "no_rg": (rbi_no_rg, disc, None),
    }


# ---------------------------------------------------------------------------
# Header round-trip
# ---------------------------------------------------------------------------


def test_header_fields_with_rg(built_containers):
    rbi, _disc, _ = built_containers["rg"]
    h = read_header(rbi)

    assert h.version_major == VERSION_MAJOR
    assert h.version_minor == VERSION_MINOR
    assert h.track_count == len(_EXAMPLE_TRACKS)
    assert h.disc_number == 1
    assert h.disc_total == 1
    assert h.pcm_sample_rate == PCM_SAMPLE_RATE
    assert h.pcm_channels == PCM_CHANNELS
    assert h.pcm_bit_depth == PCM_BIT_DEPTH

    rg_entry = h.find_block(BLOCK_TYPE_RGDB)
    assert rg_entry is not None
    assert rg_entry.offset > 0
    assert rg_entry.length > 0


def test_header_fields_without_rg(built_containers):
    rbi, _, _ = built_containers["no_rg"]
    h = read_header(rbi)

    assert h.find_block(BLOCK_TYPE_RGDB) is None


# ---------------------------------------------------------------------------
# Checksum integrity
# ---------------------------------------------------------------------------


def test_checksums_pass(built_containers):
    """All SHA-256 checksums in directory entries match the actual block bytes."""
    rbi, _, _ = built_containers["rg"]
    h = read_header(rbi)

    for entry in h.directory:
        with open(rbi, "rb") as f:
            f.seek(entry.offset)
            block_bytes = f.read(entry.length)
        assert hashlib.sha256(block_bytes).digest() == entry.checksum, (
            f"Checksum mismatch for block {entry.type_id!r}"
        )


# ---------------------------------------------------------------------------
# TOC round-trip
# ---------------------------------------------------------------------------


def test_toc_roundtrip(built_containers):
    """TOC bytes written into the container re-parse to the correct disc structure."""
    rbi, disc, _ = built_containers["rg"]
    h = read_header(rbi)

    toc_entry = h.find_block(BLOCK_TYPE_TOC)
    assert toc_entry is not None

    with open(rbi, "rb") as f:
        f.seek(toc_entry.offset)
        toc_bytes = f.read(toc_entry.length)

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

    rg_entry = h.find_block(BLOCK_TYPE_RGDB)
    assert rg_entry is not None

    with open(rbi, "rb") as f:
        f.seek(rg_entry.offset)
        rg_raw = f.read(rg_entry.length)

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
    rbi, _disc, rg_result = built_containers["rg"]
    h = read_header(rbi)

    toc_entry = h.find_block(BLOCK_TYPE_TOC)
    assert toc_entry is not None
    with open(rbi, "rb") as f:
        f.seek(toc_entry.offset)
        toc_bytes = f.read(toc_entry.length)
    parsed_disc = parse_toc(toc_bytes)

    extract_data(rbi, raw_dir=None, tracks=True, base_dir=tmp_path, embed_rg=True)

    flac_paths = collect_track_flac_paths(
        parsed_disc, h.disc_number, h.disc_total, tmp_path
    )
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


# ---------------------------------------------------------------------------
# PROV block
# ---------------------------------------------------------------------------


def test_prov_block_present(built_containers):
    """PROV block is written and contains the expected keys."""
    rbi, _, _ = built_containers["rg"]
    h = read_header(rbi)

    prov_entry = h.find_block(BLOCK_TYPE_PROV)
    assert prov_entry is not None
    assert prov_entry.is_skippable

    with open(rbi, "rb") as f:
        f.seek(prov_entry.offset)
        prov_bytes = f.read(prov_entry.length)

    text = prov_bytes.decode("utf-8")
    pairs = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    assert "creator" in pairs
    assert "created" in pairs
    assert pairs.get("mode") == "c"
    assert pairs.get("ripper") == "file"


def test_prov_block_absent_when_not_passed(tmp_path_factory, wav_tracks):
    """PROV block is absent when prov_data=None."""
    tmp = tmp_path_factory.mktemp("no_prov")
    pcm = tmp / "all.pcm"
    concat_wav(wav_tracks, tmp / "all.wav")
    wav_to_raw_pcm(tmp / "all.wav", pcm)

    disc = RBIDisc(album="Test Album", artist="Test Artist")
    durations = get_track_durations(wav_tracks)
    disc.tracks = build_toc_entries(_EXAMPLE_TRACKS, durations, disc)
    toc_data = generate_toc(disc)

    rbi = tmp / "no_prov.rbi"
    build_container(pcm, toc_data, disc, rbi, prov_data=None)

    h = read_header(rbi)
    assert h.find_block(BLOCK_TYPE_PROV) is None


# ---------------------------------------------------------------------------
# Directory structure
# ---------------------------------------------------------------------------


def test_directory_structure(built_containers):
    """Block directory is internally consistent: offsets, lengths, ordering."""
    from cdda2img.rbi_format import DIR_ENTRY_SIZE, HEADER_FIXED_SIZE

    rbi, _, _ = built_containers["rg"]
    h = read_header(rbi)
    file_size = rbi.stat().st_size

    # Rule 12: dir_offset + dir_count * 54 == file_size
    assert h.dir_offset + h.dir_count * DIR_ENTRY_SIZE == file_size

    for entry in h.directory:
        # Rule 16: blocks end before directory
        assert entry.offset + entry.length <= h.dir_offset
        # Rule 17: blocks start after fixed header
        assert entry.offset >= HEADER_FIXED_SIZE

    # Rule 18: no overlapping ranges
    sorted_entries = sorted(h.directory, key=lambda e: e.offset)
    for i in range(len(sorted_entries) - 1):
        assert (
            sorted_entries[i].offset + sorted_entries[i].length
            <= sorted_entries[i + 1].offset
        )
