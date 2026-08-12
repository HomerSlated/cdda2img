"""Tests for the PlexTools ``.pxi`` importer.

Every fixture here is **synthesised**, never sliced from a real image: a real
header carries CD-Text from a commercial pressing, which is not redistributable
and would have to live in ``private/``.  Building the bytes also means each
structural rule can be violated one at a time, which a captured file cannot do.

The layout constants are deliberately re-derived here from
``pxi_reader``'s own names rather than hard-coded, so a change to the parser's
idea of the format shows up as a test failure in the *behaviour* tests below and
not as a silent rewrite of the fixture to match.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from cdda2img import pxi_reader
from cdda2img.cdtext import crc16_gsm
from cdda2img.pxi_reader import (
    PXIError,
    _build_disc,
    _parse_pxi,
    import_pxi,
    info_pxi,
)

LEAD_IN = 150
SECTOR = 2352


# ---------------------------------------------------------------------------
# Fixture construction
# ---------------------------------------------------------------------------


def _cdtext_packs(album: str, artist: str, titles: list[str]) -> bytes:
    """Build block-0 TITLE/PERFORMER packs with valid CRCs and a SIZE_INFO pack."""

    def _stream(disc_level: str, per_track: list[str]) -> bytes:
        out = bytearray()
        for s in [disc_level, *per_track]:
            out += s.encode("ascii") + b"\x00"
        return bytes(out)

    packs = bytearray()
    for pti, payload in (
        (0x80, _stream(album, titles)),
        (0x81, _stream(artist, [artist] * len(titles))),
    ):
        track = 0
        for seq, off in enumerate(range(0, len(payload), 12)):
            chunk = payload[off : off + 12].ljust(12, b"\x00")
            body = bytes([pti, track, seq, 0x00]) + chunk
            packs += body + struct.pack(">H", crc16_gsm(body))
            track += chunk.count(b"\x00")

    # SIZE_INFO (0x8F) is a 36-byte payload spread over exactly three packs;
    # cdtext.py ignores a short group, so two packs would read as "no SIZE_INFO".
    info = bytearray(36)
    info[0] = 0x01  # charset: ASCII
    info[1] = 1  # first track
    info[2] = len(titles)  # last track
    info[28] = 0x09  # block 0 language: English
    for seq in range(3):
        body = bytes([0x8F, 0, seq, 0x00]) + bytes(info[seq * 12 : seq * 12 + 12])
        packs += body + struct.pack(">H", crc16_gsm(body))
    return bytes(packs)


def _index_record(session: int, track: int, position: int, length: int) -> bytes:
    rec = bytearray(pxi_reader._INDEX_RECORD_BYTES)
    struct.pack_into("<I", rec, pxi_reader._IDX_SESSION, session)
    struct.pack_into("<I", rec, pxi_reader._IDX_TRACK, track)
    struct.pack_into("<I", rec, pxi_reader._IDX_POSITION, position)
    struct.pack_into("<I", rec, pxi_reader._IDX_LENGTH, length)
    return bytes(rec)


def build_pxi(
    tmp_path: Path,
    *,
    records: list[tuple[int, int, int, int]],
    leadout: int,
    declared_tracks: int | None = None,
    mcn: str = "0" * 13,
    cdtext: bytes | None = None,
    audio_frames: int | None = None,
    magic: bytes = b"PXI\x00",
    name: str = "sample.pxi",
) -> Path:
    """Write a synthetic .pxi.  Audio is a per-sector ramp, never silence.

    Silence is also what a reader that seeks to the wrong offset produces, so a
    fixture of zeros cannot tell a correct audio origin from a broken one.
    """
    if declared_tracks is None:
        declared_tracks = len({r[1] for r in records})
    if audio_frames is None:
        audio_frames = leadout - LEAD_IN

    head = bytearray(pxi_reader._TOC_OFFSET)
    head[: len(magic)] = magic
    if cdtext:
        struct.pack_into(
            ">H", head, pxi_reader._CDTEXT_OFFSET, len(cdtext) + 2
        )  # +2 reserved
        start = pxi_reader._CDTEXT_OFFSET + 4
        head[start : start + len(cdtext)] = cdtext

    toc = bytearray(pxi_reader._INDEX_TABLE_OFFSET - pxi_reader._TOC_OFFSET)
    struct.pack_into("<I", toc, pxi_reader._TOC_LEADOUT, leadout)
    toc[pxi_reader._TOC_TRACK_COUNT] = declared_tracks
    encoded = mcn.encode("ascii")
    toc[pxi_reader._TOC_MCN : pxi_reader._TOC_MCN + len(encoded)] = encoded

    table = b"".join(_index_record(*r) for r in records)
    table += bytes(pxi_reader._INDEX_RECORD_BYTES)  # terminator

    body = bytes(head) + bytes(toc) + table
    pad = pxi_reader._AUDIO_OFFSET - len(body)
    assert pad >= 0, "fixture header overflowed the audio origin"

    audio = b"".join(
        bytes([n & 0xFF, (n >> 8) & 0xFF]) * (SECTOR // 2) for n in range(audio_frames)
    )

    path = tmp_path / name
    path.write_bytes(body + bytes(pad) + audio)
    return path


# A three-track disc: track 1 has a program-area pre-gap (INDEX 01 at LBA 33),
# track 2 a normal 50-frame pre-gap, track 3 none.  bounds[0] != 0 on purpose —
# the track-1 head offset is what the disc-ID arithmetic turns on.
SIMPLE_RECORDS = [
    (1, 1, 0, 183),  # t1 INDEX 00 — absolute frame 0, i.e. LBA -150
    (1, 1, 183, 1000),  # t1 INDEX 01 at LBA 33
    (1, 2, 1183, 50),  # t2 INDEX 00
    (1, 2, 1233, 2000),  # t2 INDEX 01 at LBA 1083
    (1, 3, 3233, 0),  # t3 INDEX 00, zero-length
    (1, 3, 3233, 500),  # t3 INDEX 01
]
SIMPLE_LEADOUT = 3733


@pytest.fixture
def simple(tmp_path: Path) -> Path:
    return build_pxi(
        tmp_path,
        records=SIMPLE_RECORDS,
        leadout=SIMPLE_LEADOUT,
        cdtext=_cdtext_packs("Test Album", "Test Artist", ["One", "Two", "Three"]),
    )


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def test_positions_are_absolute_frames_not_lbas(simple: Path) -> None:
    """LBA = stored position - 150.  Reading them as LBAs shifts every track."""
    disc, _, total = _parse_pxi(simple)
    assert total == SIMPLE_LEADOUT - LEAD_IN
    assert [t.start_frame + t.pregap_frames for t in disc.tracks] == [33, 1083, 3083]


def test_track1_head_offset_becomes_its_pregap(simple: Path) -> None:
    """INDEX 01 at LBA 33 must be declared as 33 frames of pre-gap at frame 0.

    Dropping those frames shifts every boundary and the lead-out down by 33 and
    breaks the round-trip disc ID (see subq_toc / project_track1_pregap_fix).
    """
    disc, _, _ = _parse_pxi(simple)
    first = disc.tracks[0]
    assert (first.start_frame, first.pregap_frames) == (0, 33)


def test_the_track_layout_is_gapless_and_reaches_the_leadout(simple: Path) -> None:
    disc, _, total = _parse_pxi(simple)
    cursor = 0
    for t in disc.tracks:
        assert t.start_frame == cursor
        cursor += t.pregap_frames + t.duration_frames
    assert cursor == total


def test_a_zero_length_index00_yields_no_pregap(simple: Path) -> None:
    assert disc_track(simple, 3).pregap_frames == 0


def disc_track(path: Path, number: int):
    disc, _, _ = _parse_pxi(path)
    return next(t for t in disc.tracks if t.track_number == number)


def test_index_points_beyond_01_are_kept_relative_to_the_audio_start(
    tmp_path: Path,
) -> None:
    """A third record for a track is INDEX 02, not a second track.

    The real sample has exactly two records per track; hard-coding that stride
    would silently mis-parse any disc carrying INDEX >= 02.
    """
    records = [
        (1, 1, 150, 0),
        (1, 1, 150, 400),
        (1, 1, 550, 600),  # INDEX 02, 400 frames after the audio start
        (1, 2, 1150, 0),
        (1, 2, 1150, 500),
    ]
    path = build_pxi(tmp_path, records=records, leadout=1650)
    disc, _, _ = _parse_pxi(path)
    assert disc.tracks[0].index_points == [400]
    assert disc.tracks[0].duration_frames == 1000  # INDEX 01 + INDEX 02 spans
    assert len(disc.tracks) == 2


def test_a_first_track_other_than_one_is_honoured(tmp_path: Path) -> None:
    records = [(1, 5, 150, 0), (1, 5, 150, 400), (1, 6, 550, 0), (1, 6, 550, 300)]
    path = build_pxi(tmp_path, records=records, leadout=850)
    disc, _, _ = _parse_pxi(path)
    assert [t.track_number for t in disc.tracks] == [5, 6]


# ---------------------------------------------------------------------------
# Rejections — each violates exactly one rule
# ---------------------------------------------------------------------------


def test_a_file_without_the_magic_is_refused(tmp_path: Path) -> None:
    path = build_pxi(
        tmp_path, records=SIMPLE_RECORDS, leadout=SIMPLE_LEADOUT, magic=b"NER5"
    )
    with pytest.raises(PXIError, match="not a PlexTools image"):
        _parse_pxi(path)


def test_a_multi_session_image_is_refused(tmp_path: Path) -> None:
    records = [(1, 1, 150, 0), (1, 1, 150, 400), (2, 2, 550, 0), (2, 2, 550, 300)]
    path = build_pxi(tmp_path, records=records, leadout=850)
    with pytest.raises(PXIError, match="multi-session"):
        _parse_pxi(path)


def test_a_gap_in_the_index_table_is_refused(tmp_path: Path) -> None:
    """Contiguity is what makes a single flat PCM copy correct."""
    records = [(1, 1, 150, 0), (1, 1, 150, 400), (1, 2, 999, 0), (1, 2, 999, 300)]
    path = build_pxi(tmp_path, records=records, leadout=1299)
    with pytest.raises(PXIError, match="gap in the index table"):
        _parse_pxi(path)


def test_a_table_that_misses_the_leadout_is_refused(tmp_path: Path) -> None:
    path = build_pxi(
        tmp_path, records=SIMPLE_RECORDS, leadout=SIMPLE_LEADOUT + 100, audio_frames=1
    )
    with pytest.raises(PXIError, match="lead-out"):
        _parse_pxi(path)


def test_a_disagreeing_track_count_warns_but_the_table_wins(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The 0x47 byte cross-checks the table; it does not overrule it.

    The table is independently validated — contiguous, and it reaches the
    lead-out — so refusing a file we demonstrably read correctly is the worse
    failure of the two.
    """
    path = build_pxi(
        tmp_path,
        records=SIMPLE_RECORDS,
        leadout=SIMPLE_LEADOUT,
        declared_tracks=7,
    )
    with caplog.at_level("WARNING"):
        disc, _, _ = _parse_pxi(path)
    assert len(disc.tracks) == 3
    assert "trusting the table" in caplog.text


def test_the_track_count_byte_is_accepted_as_a_last_track_number(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Whether 0x47 is a count or the last track is unresolved — accept both.

    They coincide on every disc whose first track is 1, which is every sample we
    have, so one sample cannot separate the readings.  Encoding either one as a
    rule would refuse a correctly-parsed file on a guess.
    """
    records = [(1, 5, 150, 0), (1, 5, 150, 400), (1, 6, 550, 0), (1, 6, 550, 300)]
    path = build_pxi(tmp_path, records=records, leadout=850, declared_tracks=6)
    with caplog.at_level("WARNING"):
        disc, _, _ = _parse_pxi(path)
    assert [t.track_number for t in disc.tracks] == [5, 6]
    assert caplog.text == ""


def test_non_ascending_tracks_are_refused(tmp_path: Path) -> None:
    records = [(1, 2, 150, 0), (1, 2, 150, 400), (1, 1, 550, 0), (1, 1, 550, 300)]
    path = build_pxi(tmp_path, records=records, leadout=850)
    with pytest.raises(PXIError, match="not ascending"):
        _parse_pxi(path)


def test_a_track_split_across_two_runs_is_refused(tmp_path: Path) -> None:
    records = [
        (1, 1, 150, 0),
        (1, 1, 150, 400),
        (1, 2, 550, 0),
        (1, 2, 550, 300),
        (1, 1, 850, 100),
    ]
    path = build_pxi(tmp_path, records=records, leadout=950)
    with pytest.raises(PXIError, match="two separate runs"):
        _parse_pxi(path)


def test_a_track_with_only_one_index_point_is_refused(tmp_path: Path) -> None:
    records = [(1, 1, 150, 400), (1, 2, 550, 0), (1, 2, 550, 300)]
    path = build_pxi(tmp_path, records=records, leadout=850)
    with pytest.raises(PXIError, match="INDEX 00 and INDEX 01"):
        _parse_pxi(path)


def test_an_empty_index_table_is_refused(tmp_path: Path) -> None:
    path = build_pxi(tmp_path, records=[], leadout=850, declared_tracks=0)
    with pytest.raises(PXIError, match="index table is empty"):
        _parse_pxi(path)


# ---------------------------------------------------------------------------
# MCN and CD-Text
# ---------------------------------------------------------------------------


def test_an_all_zero_mcn_reads_as_absent(simple: Path) -> None:
    """PlexTools writes thirteen ASCII zeros when the disc carries no MCN."""
    disc, _, _ = _parse_pxi(simple)
    assert disc.catalog is None


def test_a_real_mcn_is_kept(tmp_path: Path) -> None:
    path = build_pxi(
        tmp_path,
        records=SIMPLE_RECORDS,
        leadout=SIMPLE_LEADOUT,
        mcn="0731454490627",
    )
    disc, _, _ = _parse_pxi(path)
    assert disc.catalog == "0731454490627"


def test_cdtext_supplies_album_artist_and_titles(simple: Path) -> None:
    disc, has_cdtext, _ = _parse_pxi(simple)
    assert has_cdtext
    assert (disc.album, disc.artist) == ("Test Album", "Test Artist")
    assert [t.title for t in disc.tracks] == ["One", "Two", "Three"]


def test_an_image_without_cdtext_parses_with_empty_metadata(tmp_path: Path) -> None:
    path = build_pxi(tmp_path, records=SIMPLE_RECORDS, leadout=SIMPLE_LEADOUT)
    disc, has_cdtext, _ = _parse_pxi(path)
    assert not has_cdtext
    assert (disc.album, disc.artist) == ("", "")
    assert len(disc.tracks) == 3


def test_cdtext_for_a_different_disc_is_discarded(tmp_path: Path) -> None:
    """SIZE_INFO covering 1-9 on a 3-track disc means the block is not ours.

    Same rule as the rip path's binding guard: prefer no CD-Text over wrong
    CD-Text, because a wrong album gets baked into the container.
    """
    nine = [f"T{i}" for i in range(1, 10)]
    path = build_pxi(
        tmp_path,
        records=SIMPLE_RECORDS,
        leadout=SIMPLE_LEADOUT,
        cdtext=_cdtext_packs("Wrong Album", "Wrong Artist", nine),
    )
    disc, has_cdtext, _ = _parse_pxi(path)
    assert not has_cdtext
    assert disc.album == ""


# ---------------------------------------------------------------------------
# PCM
# ---------------------------------------------------------------------------


def test_pcm_starts_one_read_offset_into_the_audio_region(
    simple: Path, tmp_path: Path
) -> None:
    """The stored audio is raw, so the copy begins ``read_offset`` samples in.

    Checked against the file's own bytes rather than a recomputed expectation:
    the fixture fills each sector with its own index, so a copy that started at
    the wrong place would still be the right length and the wrong content.
    """
    out = tmp_path / "out.pcm"
    import_pxi(simple, out)
    shift = pxi_reader._PLEXTOOLS_READ_OFFSET * pxi_reader._BYTES_PER_SAMPLE
    raw = simple.read_bytes()
    body = out.read_bytes()
    assert body[:-shift] == raw[pxi_reader._AUDIO_OFFSET + shift :]
    assert body[-shift:] == bytes(shift)


def test_pcm_length_matches_the_declared_leadout(simple: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pcm"
    disc, _ = import_pxi(simple, out)
    assert out.stat().st_size == (SIMPLE_LEADOUT - LEAD_IN) * SECTOR


def test_offset_correction_pads_the_tail_and_records_both_facts(
    simple: Path, tmp_path: Path
) -> None:
    """A *complete* image still pads, and that is the expected outcome.

    Shifting a raw image forward by the read offset leaves the same number of
    samples missing at the lead-out end.  Padding keeps the PCM and the TOC
    agreeing — every consumer slices the PCM using the TOC — so both the
    fabricated samples and the offset that caused them stay identifiable.

    This inverts the older expectation, which read the 120 bytes as a shortfall
    in the file.  They were the head samples the offset discards (N7).
    """
    out = tmp_path / "out.pcm"
    prov: dict[str, str] = {}
    import_pxi(simple, out, prov)
    shift = pxi_reader._PLEXTOOLS_READ_OFFSET * pxi_reader._BYTES_PER_SAMPLE
    assert prov == {
        "pxi_read_offset": str(pxi_reader._PLEXTOOLS_READ_OFFSET),
        "pxi_offset_source": "assumed",
        "pxi_tail_padded": str(shift),
    }
    assert out.stat().st_size == (SIMPLE_LEADOUT - LEAD_IN) * SECTOR
    assert out.read_bytes()[-shift:] == bytes(shift)


def test_a_truncated_image_is_refused_rather_than_padded(tmp_path: Path) -> None:
    """Unbounded padding would import a half-copied file as a silent disc.

    The container would be structurally perfect — right TOC, right disc ID —
    and wrong in the only way that matters, reported as a success.  Zeros are
    also what a file that was never fully written produces.
    """
    path = build_pxi(
        tmp_path,
        records=SIMPLE_RECORDS,
        leadout=SIMPLE_LEADOUT,
        audio_frames=10,
    )
    out = tmp_path / "out.pcm"
    with pytest.raises(PXIError, match="looks truncated"):
        import_pxi(path, out)


def test_an_audio_region_short_by_a_single_byte_is_refused(
    simple: Path, tmp_path: Path
) -> None:
    """The raw region must hold the whole lead-out exactly — no tolerance.

    The old reader allowed a shortfall of up to one sector, because the reference
    image appeared to be 120 bytes short.  It was not: the origin was wrong.  With
    the origin measured from the file's own arithmetic there is no legitimate
    shortfall left, so the tolerance that hid a truncated copy is gone.
    """
    short = tmp_path / "short.pxi"
    short.write_bytes(simple.read_bytes()[:-1])
    with pytest.raises(PXIError, match="looks truncated"):
        import_pxi(short, tmp_path / "short.pcm")


def test_a_padding_import_touches_neither_the_terminal_nor_a_warning(
    simple: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both notes go to the sink; nothing reaches stdout, stderr or a WARNING.

    Under the TUI either one would orphan a progress bar — ``print`` by moving
    the cursor behind the renderer's back, ``log.warning`` by falling through
    to :data:`logging.lastResort` on stderr when no handler is configured.
    Tested by behaviour, not by grepping the source: the first version of this
    check searched for the string ``log.warning`` and matched the docstring
    explaining why there isn't one.
    """
    lines: list[str] = []
    capsys.readouterr()  # discard anything banked before this point

    with caplog.at_level("WARNING"):
        import_pxi(simple, tmp_path / "out.pcm", {}, lines.append)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert caplog.records == []
    assert any("CD-Text" in line for line in lines)
    assert any("zero-filled" in line for line in lines)


def test_without_a_sink_the_notes_still_print(
    simple: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The sink is opt-in — a standalone caller keeps the plain behaviour."""
    capsys.readouterr()
    import_pxi(simple, tmp_path / "out.pcm")
    assert "CD-Text" in capsys.readouterr().out


def test_info_pxi_reports_the_file_size_without_writing_pcm(simple: Path) -> None:
    disc, has_cdtext, size = info_pxi(simple)
    assert size == simple.stat().st_size
    assert has_cdtext and len(disc.tracks) == 3


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_the_import_dispatch_knows_the_pxi_suffix() -> None:
    """`.pxi` must reach the reader, and be named in the error listing sources."""
    import inspect

    from cdda2img import cdda2img as app

    assert '".pxi"' in inspect.getsource(app._import_source)
    assert '".pxi"' in inspect.getsource(app.info_image)
    assert ".pxi" in app._unsupported_source_msg(Path("x.bogus"))


def test_build_disc_defaults_the_performer_to_the_album_artist() -> None:
    """A track with no PERFORMER pack inherits the disc's, not an empty string."""
    groups = [[(150, 0), (150, 400)]]
    disc = _build_disc(groups, 1, None, None)
    assert disc.tracks[0].performer == ""


# ---------------------------------------------------------------------------
# Read-offset resolution (N7)
# ---------------------------------------------------------------------------
#
# The policy exists because neither of the two obvious rules works, and both
# failures were measured on the four real images rather than reasoned about:
# `matches[0]` picks +1573 on the 12-track disc where +30 ranks second, and a
# plausibility band admits 10 of Tracy Chapman's 13 confirmed offsets. These
# tests encode the outcomes, so a later "simplification" back to either rule
# fails here instead of silently storing audio aligned to another pressing.


def _cand(
    offset: int, matched: int = 11, total: int = 11
) -> pxi_reader.OffsetCandidate:
    return pxi_reader.OffsetCandidate(offset, matched, total)


def test_no_accuraterip_evidence_keeps_the_prior() -> None:
    res = pxi_reader._resolve_read_offset(None)
    assert res.offset == pxi_reader._PLEXTOOLS_READ_OFFSET
    assert res.source == "assumed"


def test_in_the_database_but_verifying_nowhere_is_not_the_same_as_no_evidence() -> None:
    """`[]` is a statement about the audio; `None` is silence from the network.

    Both keep the prior, so the offset is unaffected — but they must stay
    distinguishable in PROV, because an image that verifies at no offset at all
    is also what damage and a mis-parse look like.
    """
    res = pxi_reader._resolve_read_offset([])
    assert res.offset == pxi_reader._PLEXTOOLS_READ_OFFSET
    assert res.source == "assumed_unverified"
    assert res.source != pxi_reader._resolve_read_offset(None).source


def test_the_prior_among_the_candidates_is_confirmation_not_coincidence() -> None:
    res = pxi_reader._resolve_read_offset([_cand(30), _cand(-639), _cand(-1303)])
    assert res.offset == 30
    assert res.source == "accuraterip_confirmed"
    assert "2 other offset" in res.detail


def test_the_top_ranked_candidate_is_not_taken_when_the_prior_also_verifies() -> None:
    """Measured on the 12-track image: +1573 ranks FIRST and +30 second.

    Both are fully confirmed, so ranking cannot separate a drive offset from a
    pressing cohort. Taking the head of the list would have applied +1573.
    """
    res = pxi_reader._resolve_read_offset([_cand(1573, 11, 12), _cand(30, 11, 12)])
    assert res.offset == 30


def test_a_sole_verifying_offset_overrides_the_prior() -> None:
    """The case the feature exists for: evidence with no ambiguity to fear."""
    res = pxi_reader._resolve_read_offset([_cand(667)])
    assert res.offset == 667
    assert res.source == "accuraterip_sole"
    assert "does NOT verify" in res.detail


def test_several_candidates_without_the_prior_declines_rather_than_guesses() -> None:
    """Ambiguity with no drive evidence: keep the prior, record every candidate.

    Picking the nearest to zero here (-634) is the heuristic detect_offset's own
    docstring disowns, and it would silently store audio aligned to a different
    pressing.
    """
    res = pxi_reader._resolve_read_offset([_cand(1573), _cand(-634), _cand(-1967)])
    assert res.offset == pxi_reader._PLEXTOOLS_READ_OFFSET
    assert res.source == "assumed_ambiguous"
    assert "+1573" in res.detail and "-634" in res.detail


def test_a_detected_offset_reaches_the_pcm_and_the_provenance(
    simple: Path, tmp_path: Path
) -> None:
    """End to end: a supplied sole candidate must move the bytes, not just PROV.

    Guards the seam itself — a resolution that is recorded but never passed to
    the copy would leave every assertion above true and the audio unchanged.
    """
    out = tmp_path / "out.pcm"
    prov: dict[str, str] = {}
    import_pxi(simple, out, prov, None, lambda _d, _p, _o: [_cand(7, 11, 11)])
    assert prov["pxi_read_offset"] == "7"
    assert prov["pxi_offset_source"] == "accuraterip_sole"
    assert prov["pxi_offset_candidates"] == "+7"
    shift = 7 * pxi_reader._BYTES_PER_SAMPLE
    raw = simple.read_bytes()
    body = out.read_bytes()
    assert body[:-shift] == raw[pxi_reader._AUDIO_OFFSET + shift :]
    assert body[-shift:] == bytes(shift)
