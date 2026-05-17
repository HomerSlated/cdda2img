"""
test_ccd_reader.py — Unit tests for the CloneCD CCD/IMG importer.

All tests use synthetic CCD text + IMG fixtures built in memory;
no real audio files or private/ paths are required.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cdda2img.ccd_reader import (
    _CDDA_SECTOR_BYTES,
    _STANDARD_PREGAP_SECTORS,
    _extract_entries,
    _parse_ccd,
    import_ccd,
)

_SEC = _CDDA_SECTOR_BYTES  # 2352


# ---------------------------------------------------------------------------
# CCD text builder helpers
# ---------------------------------------------------------------------------


def _entry(
    idx: int, session: int, point: str, control: str, plba: int, **extra: str
) -> str:
    lines = [
        f"[Entry {idx}]",
        f"Session={session}",
        f"Point={point}",
        "ADR=0x01",
        f"Control={control}",
        "TrackNo=0",
        "AMin=0",
        "ASec=0",
        "AFrame=0",
        "ALBA=-150",
        "Zero=0",
        "PMin=0",
        "PSec=0",
        "PFrame=0",
        f"PLBA={plba}",
    ]
    return "\n".join(lines)


def _track_section(
    n: int, index1: int, index0: int | None = None, isrc: str = ""
) -> str:
    lines = [f"[TRACK {n}]", "MODE=0"]
    if index0 is not None:
        lines.append(f"INDEX 0={index0}")
    lines.append(f"INDEX 1={index1}")
    if isrc:
        lines.append(f"ISRC={isrc}")
    return "\n".join(lines)


def _ccd_text(
    track_plbas: list[int],
    lead_out_plba: int,
    sessions: int = 1,
    cdtext_length: int = 0,
    track_details: list[dict] | None = None,
    entry_controls: list[str] | None = None,
) -> str:
    """Build a minimal but valid CCD text fixture."""
    if track_details is None:
        track_details = [{} for _ in track_plbas]
    if entry_controls is None:
        entry_controls = ["0x00"] * len(track_plbas)

    n_entries = len(track_plbas) + 3  # A0, A1, A2 + per track
    lines = [
        "[CloneCD]",
        "Version=3",
        "[Disc]",
        f"TocEntries={n_entries}",
        f"Sessions={sessions}",
        "DataTracksScrambled=0",
        f"CDTextLength={cdtext_length}",
        "[Session 1]",
        "PreGapMode=0",
        "PreGapSubC=0",
        _entry(0, 1, "0xa0", "0x00", 0),
        _entry(1, 1, "0xa1", "0x00", 0),
        _entry(2, 1, "0xa2", "0x00", lead_out_plba),
    ]

    for i, (plba, ctrl) in enumerate(zip(track_plbas, entry_controls), start=1):
        lines.append(_entry(i + 2, 1, f"0x{i:02x}", ctrl, plba))

    for n, (plba, detail) in enumerate(zip(track_plbas, track_details), start=1):
        lines.append(
            _track_section(
                n,
                index1=plba,
                index0=detail.get("index0"),
                isrc=detail.get("isrc", ""),
            )
        )

    return "\n".join(lines)


def _sector(pattern: bytes) -> bytes:
    tile = (pattern * ((_SEC // len(pattern)) + 1))[:_SEC]
    return tile


def _make_img(
    lead_out_plba: int,
    track_plbas: list[int],
    patterns: list[bytes],
) -> bytes:
    """Build an IMG file with distinct per-track byte patterns.

    Sectors 0..150 are silence (track 1 lead-in pre-gap).
    Each track fills from its PLBA to the next PLBA (or lead-out).
    """
    img = bytearray(lead_out_plba * _SEC)
    boundaries = [*track_plbas, lead_out_plba]
    for i, (plba, nxt, pattern) in enumerate(
        zip(track_plbas, boundaries[1:], patterns), start=1
    ):
        start = max(plba, _STANDARD_PREGAP_SECTORS) if i == 1 else plba
        for s in range(start, nxt):
            img[s * _SEC : (s + 1) * _SEC] = _sector(pattern)
    return bytes(img)


# ---------------------------------------------------------------------------
# Standard 2-track no-pre-gap fixture
#
#  Sectors 0-149    track 1 lead-in pre-gap (zero, skipped)
#  Sectors 150-159  track 1 audio  (10 sectors, pattern 0x1122)
#  Sectors 160-174  track 2 audio  (15 sectors, pattern 0x3344)
#  lead-out PLBA = 175
# ---------------------------------------------------------------------------

T1_PLBA = 0
T2_PLBA = 160
LEAD_OUT = 175
T1_PATTERN = b"\x11\x22"
T2_PATTERN = b"\x33\x44"


def _make_standard(tmp_path: Path) -> tuple[Path, Path]:
    ccd_text = _ccd_text([T1_PLBA, T2_PLBA], LEAD_OUT)
    img_data = _make_img(LEAD_OUT, [T1_PLBA, T2_PLBA], [T1_PATTERN, T2_PATTERN])
    ccd = tmp_path / "test.ccd"
    img = tmp_path / "test.img"
    ccd.write_text(ccd_text)
    img.write_bytes(img_data)
    return ccd, img


# ---------------------------------------------------------------------------
# _parse_ccd
# ---------------------------------------------------------------------------


def test_parse_ccd_sections() -> None:
    text = "[Disc]\nSessions=1\nCDTextLength=0\n[TRACK 1]\nMODE=0\nINDEX 1=0\n"
    sections = _parse_ccd(text)
    assert "disc" in sections
    assert sections["disc"]["sessions"] == "1"
    assert "track 1" in sections
    assert sections["track 1"]["index 1"] == "0"


def test_parse_ccd_skips_comments() -> None:
    text = "; this is a comment\n[Disc]\nSessions=1\n"
    sections = _parse_ccd(text)
    assert "disc" in sections
    assert "; this is a comment" not in sections


def test_parse_ccd_handles_hex_values() -> None:
    text = "[Entry 0]\nPoint=0xa2\nControl=0x00\nPLBA=175\n"
    sections = _parse_ccd(text)
    from cdda2img.ccd_reader import _parse_int

    assert _parse_int(sections["entry 0"]["point"]) == 0xA2


# ---------------------------------------------------------------------------
# _extract_entries
# ---------------------------------------------------------------------------


def test_extract_entries_lead_out() -> None:
    text = _ccd_text([0, 160], lead_out_plba=175)
    sections = _parse_ccd(text)
    lead_out, _entries = _extract_entries(sections)
    assert lead_out == 175


def test_extract_entries_track_count() -> None:
    text = _ccd_text([0, 160, 350], lead_out_plba=500)
    sections = _parse_ccd(text)
    _, entries = _extract_entries(sections)
    assert len(entries) == 3


def test_extract_entries_sorted_by_point() -> None:
    # Entries in reverse order should come out sorted.
    text = _ccd_text([350, 160, 0], lead_out_plba=500)
    sections = _parse_ccd(text)
    _, entries = _extract_entries(sections)
    points = [e["point"] for e in entries]
    assert points == sorted(points)


def test_extract_entries_ignores_a0_a1() -> None:
    text = _ccd_text([0, 160], lead_out_plba=175)
    sections = _parse_ccd(text)
    _, entries = _extract_entries(sections)
    for e in entries:
        assert e["point"] not in (0xA0, 0xA1, 0xA2)


# ---------------------------------------------------------------------------
# import_ccd — disc metadata
# ---------------------------------------------------------------------------


def test_import_ccd_track_count(tmp_path: Path) -> None:
    ccd, _ = _make_standard(tmp_path)
    disc, _ = import_ccd(ccd, tmp_path / "out.pcm")
    assert len(disc.tracks) == 2


def test_import_ccd_track1_pregap_zero(tmp_path: Path) -> None:
    ccd, _ = _make_standard(tmp_path)
    disc, _ = import_ccd(ccd, tmp_path / "out.pcm")
    assert disc.tracks[0].pregap_frames == 0
    assert disc.tracks[0].start_frame == 0


def test_import_ccd_track2_no_pregap(tmp_path: Path) -> None:
    ccd, _ = _make_standard(tmp_path)
    disc, _ = import_ccd(ccd, tmp_path / "out.pcm")
    assert disc.tracks[1].pregap_frames == 0


def test_import_ccd_duration_frames(tmp_path: Path) -> None:
    ccd, _ = _make_standard(tmp_path)
    disc, _ = import_ccd(ccd, tmp_path / "out.pcm")
    # Track 1: sectors 150-159 = 10 frames; Track 2: sectors 160-174 = 15 frames
    assert disc.tracks[0].duration_frames == 10
    assert disc.tracks[1].duration_frames == 15


def test_import_ccd_start_frames(tmp_path: Path) -> None:
    ccd, _ = _make_standard(tmp_path)
    disc, _ = import_ccd(ccd, tmp_path / "out.pcm")
    assert disc.tracks[0].start_frame == 0
    assert disc.tracks[1].start_frame == 10


def test_import_ccd_isrc(tmp_path: Path) -> None:
    details = [{"isrc": "GBAYE9300001"}, {"isrc": "GBAYE9300002"}]
    ccd = tmp_path / "test.ccd"
    ccd.write_text(_ccd_text([T1_PLBA, T2_PLBA], LEAD_OUT, track_details=details))
    (tmp_path / "test.img").write_bytes(
        _make_img(LEAD_OUT, [T1_PLBA, T2_PLBA], [T1_PATTERN, T2_PATTERN])
    )
    disc, _ = import_ccd(ccd, tmp_path / "out.pcm")
    assert disc.tracks[0].isrc == "GBAYE9300001"
    assert disc.tracks[1].isrc == "GBAYE9300002"


def test_import_ccd_no_isrc_gives_none(tmp_path: Path) -> None:
    ccd, _ = _make_standard(tmp_path)
    disc, _ = import_ccd(ccd, tmp_path / "out.pcm")
    assert disc.tracks[0].isrc is None
    assert disc.tracks[1].isrc is None


# ---------------------------------------------------------------------------
# import_ccd — PCM output
# ---------------------------------------------------------------------------


def test_import_ccd_pcm_size(tmp_path: Path) -> None:
    """PCM = track1_audio + track2_audio; lead-in pre-gap is excluded."""
    ccd, _ = _make_standard(tmp_path)
    out = tmp_path / "out.pcm"
    import_ccd(ccd, out)
    # 10 + 15 = 25 sectors (pre-gap sectors 0-149 are excluded)
    assert out.stat().st_size == 25 * _SEC


def test_import_ccd_track1_audio_passthrough(tmp_path: Path) -> None:
    ccd, _ = _make_standard(tmp_path)
    out = tmp_path / "out.pcm"
    import_ccd(ccd, out)
    pcm = out.read_bytes()
    t1 = pcm[: 10 * _SEC]
    assert t1 == _sector(T1_PATTERN) * 10


def test_import_ccd_pregap_not_in_pcm(tmp_path: Path) -> None:
    """Sectors 0-149 are silence; their pattern must not appear in PCM output."""
    ccd, _ = _make_standard(tmp_path)
    out = tmp_path / "out.pcm"
    import_ccd(ccd, out)
    pcm = out.read_bytes()
    # The IMG pre-gap is all-zero silence; track patterns are non-zero.
    # Verify first byte of track 1 audio is 0x11, not 0x00.
    assert pcm[0] == 0x11


def test_import_ccd_track2_bytes_correct(tmp_path: Path) -> None:
    ccd, _ = _make_standard(tmp_path)
    out = tmp_path / "out.pcm"
    import_ccd(ccd, out)
    pcm = out.read_bytes()
    t2 = pcm[10 * _SEC : 25 * _SEC]
    assert t2 == _sector(T2_PATTERN) * 15


# ---------------------------------------------------------------------------
# Inter-track pre-gap (INDEX 0 < PLBA)
#
#  Track 1: sectors 150-154 (5 sectors audio)
#  Track 2 pre-gap: sectors 155-159 (5 sectors, pattern 0xbeef)
#  Track 2 audio:   sectors 160-174 (15 sectors, pattern 0x3344)
#  lead-out = 175
# ---------------------------------------------------------------------------


def _make_pregap_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """2-track image where track 2 has a 5-sector inter-track pre-gap."""
    t1_plba = 0
    t2_plba = 160
    t2_index0 = 155
    lead_out = 175

    details = [{}, {"index0": t2_index0}]
    text = _ccd_text([t1_plba, t2_plba], lead_out, track_details=details)

    # Build IMG manually: sectors 150-154 = track 1, 155-159 = pregap, 160-174 = track 2
    img = bytearray(lead_out * _SEC)
    for s in range(150, 155):
        img[s * _SEC : (s + 1) * _SEC] = _sector(T1_PATTERN)
    for s in range(155, 160):
        img[s * _SEC : (s + 1) * _SEC] = _sector(b"\xbe\xef")
    for s in range(160, 175):
        img[s * _SEC : (s + 1) * _SEC] = _sector(T2_PATTERN)

    ccd = tmp_path / "test.ccd"
    img_path = tmp_path / "test.img"
    ccd.write_text(text)
    img_path.write_bytes(bytes(img))
    return ccd, img_path


def test_pregap_track1_duration(tmp_path: Path) -> None:
    ccd, _ = _make_pregap_fixture(tmp_path)
    disc, _ = import_ccd(ccd, tmp_path / "out.pcm")
    # Track 1: sectors 150-154 = 5 frames; img_end = INDEX0 of track2 = 155
    assert disc.tracks[0].duration_frames == 5


def test_pregap_track2_pregap_frames(tmp_path: Path) -> None:
    ccd, _ = _make_pregap_fixture(tmp_path)
    disc, _ = import_ccd(ccd, tmp_path / "out.pcm")
    assert disc.tracks[1].pregap_frames == 5


def test_pregap_track2_duration(tmp_path: Path) -> None:
    ccd, _ = _make_pregap_fixture(tmp_path)
    disc, _ = import_ccd(ccd, tmp_path / "out.pcm")
    assert disc.tracks[1].duration_frames == 15


def test_pregap_track2_start_frame(tmp_path: Path) -> None:
    ccd, _ = _make_pregap_fixture(tmp_path)
    disc, _ = import_ccd(ccd, tmp_path / "out.pcm")
    # PCM block offset of track 2 pre-gap start = 5 (after track 1's 5 audio frames)
    assert disc.tracks[1].start_frame == 5


def test_pregap_pcm_size(tmp_path: Path) -> None:
    ccd, _ = _make_pregap_fixture(tmp_path)
    out = tmp_path / "out.pcm"
    import_ccd(ccd, out)
    # track1 audio=5 + track2 pregap=5 + track2 audio=15 = 25 sectors
    assert out.stat().st_size == 25 * _SEC


def test_pregap_bytes_in_pcm(tmp_path: Path) -> None:
    ccd, _ = _make_pregap_fixture(tmp_path)
    out = tmp_path / "out.pcm"
    import_ccd(ccd, out)
    pcm = out.read_bytes()
    # Pre-gap pattern (0xbeef) must appear in the PCM after track 1's 5 sectors
    pregap_region = pcm[5 * _SEC : 10 * _SEC]
    assert pregap_region == _sector(b"\xbe\xef") * 5


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_reject_multi_session_disc_field(tmp_path: Path) -> None:
    ccd = tmp_path / "test.ccd"
    img = tmp_path / "test.img"
    text = _ccd_text([T1_PLBA, T2_PLBA], LEAD_OUT, sessions=2)
    ccd.write_text(text)
    img.write_bytes(_make_img(LEAD_OUT, [T1_PLBA, T2_PLBA], [T1_PATTERN, T2_PATTERN]))
    with pytest.raises(ValueError, match="multi-session"):
        import_ccd(ccd, tmp_path / "out.pcm")


def test_reject_data_track(tmp_path: Path) -> None:
    ccd = tmp_path / "test.ccd"
    img = tmp_path / "test.img"
    controls = ["0x00", "0x04"]  # track 2 is a data track
    text = _ccd_text([T1_PLBA, T2_PLBA], LEAD_OUT, entry_controls=controls)
    ccd.write_text(text)
    img.write_bytes(_make_img(LEAD_OUT, [T1_PLBA, T2_PLBA], [T1_PATTERN, T2_PATTERN]))
    with pytest.raises(ValueError, match="data track"):
        import_ccd(ccd, tmp_path / "out.pcm")


def test_reject_img_size_mismatch(tmp_path: Path) -> None:
    ccd, _ = _make_standard(tmp_path)
    img = tmp_path / "test.img"
    # Truncate IMG by one sector to create a size mismatch.
    data = img.read_bytes()
    img.write_bytes(data[:-_SEC])
    with pytest.raises(ValueError, match="IMG file size"):
        import_ccd(ccd, tmp_path / "out.pcm")


def test_reject_missing_img(tmp_path: Path) -> None:
    ccd = tmp_path / "test.ccd"
    ccd.write_text(_ccd_text([T1_PLBA, T2_PLBA], LEAD_OUT))
    # No IMG file written.
    with pytest.raises(FileNotFoundError, match="IMG file not found"):
        import_ccd(ccd, tmp_path / "out.pcm")


def test_reject_no_lead_out_entry(tmp_path: Path) -> None:
    # Remove the 0xa2 entry by writing a CCD with only track entries.
    ccd = tmp_path / "test.ccd"
    img = tmp_path / "test.img"
    text = "\n".join([
        "[CloneCD]",
        "Version=3",
        "[Disc]",
        "TocEntries=2",
        "Sessions=1",
        "DataTracksScrambled=0",
        "CDTextLength=0",
        "[Entry 0]",
        "Session=1",
        "Point=0x01",
        "ADR=0x01",
        "Control=0x00",
        "TrackNo=0",
        "AMin=0",
        "ASec=0",
        "AFrame=0",
        "ALBA=-150",
        "Zero=0",
        "PMin=0",
        "PSec=0",
        "PFrame=0",
        "PLBA=0",
        "[TRACK 1]",
        "MODE=0",
        "INDEX 1=0",
    ])
    ccd.write_text(text)
    img.write_bytes(b"\x00" * _SEC)
    with pytest.raises(ValueError, match="lead-out"):
        import_ccd(ccd, tmp_path / "out.pcm")
