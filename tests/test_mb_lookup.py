"""
test_mb_lookup.py — MusicBrainz lookup tests.

Pure computation tests (compute_disc_id, disc_id_from_rbi) need no mocking.
Network lookup tests mock musicbrainzngs to avoid any network dependency.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from cdda2img.lookup_result import (
    DiscMeta,
    TrackMeta,
)
from cdda2img.mb_lookup import (
    _merge_into_disc,
    _overwrite_disc,
    _parse_release,
    _parse_year,
    compute_disc_id,
    disc_id_from_rbi,
    lookup_disc_id,
    prepopulate_from_mb,
)
from cdda2img.rbi_format import RBIDisc, RBITocEntry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_disc(tracks: list[tuple[int, int, int]] | None = None) -> RBIDisc:
    """Build a minimal RBIDisc. tracks: list of (track_number, start_frame, duration_frames)."""
    entries = []
    if tracks:
        for num, start, dur in tracks:
            entries.append(
                RBITocEntry(
                    track_number=num,
                    title=f"Track {num}",
                    performer="Artist",
                    start_frame=start,
                    duration_frames=dur,
                )
            )
    return RBIDisc(album="Test Album", artist="Test Artist", tracks=entries)


# ---------------------------------------------------------------------------
# compute_disc_id — deterministic, no network
# ---------------------------------------------------------------------------


def test_compute_disc_id_format():
    """Output must be 28 chars using only MB-safe base64 characters."""
    disc_id = compute_disc_id(1, 12, [150] + [i * 10000 for i in range(1, 12)], 242457)
    assert len(disc_id) == 28
    assert "+" not in disc_id
    assert "/" not in disc_id
    assert "=" not in disc_id


def test_compute_disc_id_deterministic():
    """Same input always produces the same output."""
    offsets = [150, 18130, 38945, 55800]
    lead_out = 90000
    a = compute_disc_id(1, 4, offsets, lead_out)
    b = compute_disc_id(1, 4, offsets, lead_out)
    assert a == b


def test_compute_disc_id_sensitivity():
    """Different track offsets produce different disc IDs."""
    offsets_a = [150, 18130]
    offsets_b = [150, 18131]
    id_a = compute_disc_id(1, 2, offsets_a, 90000)
    id_b = compute_disc_id(1, 2, offsets_b, 90000)
    assert id_a != id_b


def test_compute_disc_id_single_track():
    """Single-track disc ID has correct format."""
    disc_id = compute_disc_id(1, 1, [150], 280000)
    assert len(disc_id) == 28


def test_compute_disc_id_matches_libdiscid_for_eliminator():
    """ZZ Top — Eliminator (EU 1983) pressing.

    Anchor against libdiscid's canonical output for a known disc, computed
    from the verified LBA set (cross-checked against cd-discid --musicbrainz
    and libdiscid itself). Pins us to the real MB spec — a regression to the
    old raw-byte implementation would change this output and fail this test.
    """
    track_lbas = [
        150,
        18495,
        36708,
        56065,
        84368,
        97545,
        118123,
        137535,
        155030,
        173148,
        189728,
    ]
    lead_out = 204293
    assert (
        compute_disc_id(1, 11, track_lbas, lead_out) == "nRQLbh410ePjmaAHHVpt9purZJI-"
    )


# ---------------------------------------------------------------------------
# disc_id_from_rbi
# ---------------------------------------------------------------------------


def test_disc_id_from_rbi_no_tracks():
    disc = _make_disc(tracks=[])
    assert disc_id_from_rbi(disc) is None


def test_disc_id_from_rbi_two_tracks():
    """disc_id_from_rbi produces a valid 28-char disc ID for a two-track disc."""
    disc = _make_disc(tracks=[(1, 0, 18000), (2, 18000, 20000)])
    disc_id = disc_id_from_rbi(disc)
    assert disc_id is not None
    assert len(disc_id) == 28


def test_disc_id_from_rbi_lead_out():
    """Lead-out equals total_frames + 150."""
    disc = _make_disc(tracks=[(1, 0, 10000), (2, 10000, 5000)])
    # total_frames = 15000; lead_out LBA = 15150
    # offsets: [150, 10150]
    expected = compute_disc_id(1, 2, [150, 10150], 15150)
    assert disc_id_from_rbi(disc) == expected


def test_disc_id_from_rbi_pregap():
    """Pregap frames shift the INDEX 01 offset used in the disc ID."""
    # track 2 has a 150-frame pregap; start_frame=10000, pregap=150
    disc = RBIDisc(
        album="A",
        artist="B",
        tracks=[
            RBITocEntry(1, "T1", "B", start_frame=0, duration_frames=10000),
            RBITocEntry(
                2, "T2", "B", start_frame=10000, duration_frames=5000, pregap_frames=150
            ),
        ],
    )
    # INDEX 01 for track 1: 0 + 0 + 150 = 150
    # INDEX 01 for track 2: 10000 + 150 + 150 = 10300
    # lead_out: total_frames + 150 = (10000 + 150 + 5000) + 150 = 15300
    expected = compute_disc_id(1, 2, [150, 10300], 15300)
    assert disc_id_from_rbi(disc) == expected


# ---------------------------------------------------------------------------
# _parse_year
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "date_str,expected",
    [
        ("2003", 2003),
        ("2003-04-01", 2003),
        ("", None),
        (None, None),
        ("not-a-date", None),
    ],
)
def test_parse_year(date_str, expected):
    assert _parse_year(date_str) == expected


# ---------------------------------------------------------------------------
# _merge_into_disc
# ---------------------------------------------------------------------------


def _make_meta_disc() -> DiscMeta:
    return DiscMeta(
        album="Pump Up the Jam",
        artist="Technotronic",
        catalog="5099747023521",
        mb_release_id="abc-123",
        release_date="1989",
        tracks=[
            TrackMeta(
                number=1,
                title="Pump Up the Jam",
                performer="Technotronic",
                isrc="BEXX89300001",
            ),
            TrackMeta(
                number=2, title="Get Up!", performer="Technotronic", isrc="BEXX89300002"
            ),
        ],
    )


def test_merge_fills_missing_album():
    meta = _make_meta_disc()
    disc = _make_disc(tracks=[(1, 0, 10000), (2, 10000, 9000)])
    disc.album = ""
    result = _merge_into_disc(meta, disc)
    assert result.album == "Pump Up the Jam"


def test_merge_preserves_existing_album():
    meta = _make_meta_disc()
    disc = _make_disc(tracks=[(1, 0, 10000), (2, 10000, 9000)])
    disc.album = "My Custom Title"
    result = _merge_into_disc(meta, disc)
    assert result.album == "My Custom Title"


def test_merge_fills_unknown_artist():
    meta = _make_meta_disc()
    disc = _make_disc(tracks=[(1, 0, 10000), (2, 10000, 9000)])
    disc.artist = "Unknown Artist"
    result = _merge_into_disc(meta, disc)
    assert result.artist == "Technotronic"


def test_merge_fills_track_isrc():
    meta = _make_meta_disc()
    disc = _make_disc(tracks=[(1, 0, 10000), (2, 10000, 9000)])
    result = _merge_into_disc(meta, disc)
    assert result.tracks[0].isrc == "BEXX89300001"
    assert result.tracks[1].isrc == "BEXX89300002"


def test_merge_preserves_existing_isrc():
    """Existing disc ISRC wins over meta when both are structurally valid (R13)."""
    meta = _make_meta_disc()
    disc = _make_disc(tracks=[(1, 0, 10000), (2, 10000, 9000)])
    # Real-shape ISO 3901 ISRC: country (2 alpha) + registrant (3 alphanumeric)
    # + 7 digits. The pre-R13 fixture "EXISTING0001" was malformed and is now
    # correctly dropped at the merge site — replaced here with a valid form.
    disc.tracks[0].isrc = "USXX10100001"
    result = _merge_into_disc(meta, disc)
    assert result.tracks[0].isrc == "USXX10100001"


# ---------------------------------------------------------------------------
# lookup_disc_id — mocked network
# ---------------------------------------------------------------------------


_MOCK_MB_RESPONSE = {
    "disc": {
        "release-list": [
            {
                "id": "mock-release-uuid",
                "title": "Pump Up the Jam - The Album",
                "date": "1989-10-31",
                "country": "BE",
                "barcode": "5099747023521",
                "artist-credit": [
                    {"artist": {"name": "Technotronic"}, "joinphrase": ""}
                ],
                "release-group": {
                    "id": "mock-rg-uuid",
                    "first-release-date": "1989-10-31",
                },
                "label-info-list": [
                    {"label": {"name": "Epic"}, "catalog-number": "466247 2"}
                ],
                "medium-list": [
                    {
                        "track-list": [
                            {
                                "number": "1",
                                "title": "Pump Up the Jam",
                                "recording": {
                                    "title": "Pump Up the Jam",
                                    "length": "220000",
                                    "isrc-list": ["BEXX89300001"],
                                },
                            },
                        ]
                    }
                ],
            }
        ]
    }
}


def test_lookup_disc_id_single_match():
    disc = _make_disc(tracks=[(1, 0, 18000)])
    with patch("musicbrainzngs.get_releases_by_discid", return_value=_MOCK_MB_RESPONSE):
        results = lookup_disc_id(disc)
    assert len(results) == 1
    r = results[0]
    assert r.album == "Pump Up the Jam - The Album"
    assert r.artist == "Technotronic"
    assert r.release_date == "1989-10-31"
    assert r.country == "BE"
    assert r.source == "musicbrainz"
    assert r.tracks[0].isrc == "BEXX89300001"


def test_lookup_disc_id_network_error():
    import musicbrainzngs

    disc = _make_disc(tracks=[(1, 0, 18000)])
    with patch(
        "musicbrainzngs.get_releases_by_discid",
        side_effect=musicbrainzngs.NetworkError("timeout"),
    ):
        results = lookup_disc_id(disc)
    assert results == []


def test_lookup_disc_id_response_error():
    import musicbrainzngs

    disc = _make_disc(tracks=[(1, 0, 18000)])
    err = musicbrainzngs.ResponseError(cause=Exception("404"))
    with patch("musicbrainzngs.get_releases_by_discid", side_effect=err):
        results = lookup_disc_id(disc)
    assert results == []


def test_lookup_disc_id_empty_disc():
    """A disc with no tracks returns [] without making any network call."""
    disc = _make_disc(tracks=[])
    with patch("musicbrainzngs.get_releases_by_discid") as mock_mb:
        results = lookup_disc_id(disc)
    mock_mb.assert_not_called()
    assert results == []


# ---------------------------------------------------------------------------
# Multi-disc: _parse_release with disc-list matching
# ---------------------------------------------------------------------------

_MOCK_BOXSET_RELEASE = {
    "id": "mock-boxset-uuid",
    "title": "The Complete Studio Albums",
    "date": "2013-06-10",
    "country": "US",
    "barcode": "0081227975173",
    "medium-count": 10,
    "artist-credit": [{"artist": {"name": "ZZ Top"}, "joinphrase": ""}],
    "release-group": {"id": "mock-rg-uuid", "first-release-date": "1983-03-14"},
    "label-info-list": [],
    "medium-list": [
        {
            "position": "7",
            "title": "El Loco",
            "disc-list": [{"id": "WRONG_DISC_ID"}],
            "track-list": [
                {
                    "number": "1",
                    "recording": {"title": "Tube Snake Boogie", "length": "210000"},
                }
            ],
        },
        {
            "position": "8",
            "title": "Eliminator",
            "disc-list": [{"id": "TARGET_DISC_ID"}],
            "track-list": [
                {
                    "number": "1",
                    "recording": {
                        "title": "Sharp Dressed Man",
                        "length": "248000",
                        "isrc-list": ["USEE18300025"],
                    },
                }
            ],
        },
    ],
}


def test_parse_release_multi_disc_matching():
    """disc-list match sets disc_number, disc_total, set_title, and filters tracks."""
    meta = _parse_release(_MOCK_BOXSET_RELEASE, _disc_id="TARGET_DISC_ID")
    assert meta.album == "Eliminator"
    assert meta.set_title == "The Complete Studio Albums"
    assert meta.disc_number == 8
    assert meta.disc_total == 10
    assert len(meta.tracks) == 1
    assert meta.tracks[0].title == "Sharp Dressed Man"
    assert meta.tracks[0].isrc == "USEE18300025"


def test_parse_release_multi_disc_no_match_falls_back():
    """No disc-list match falls back to all mediums; disc_total still set from medium-count."""
    meta = _parse_release(_MOCK_BOXSET_RELEASE, _disc_id="NONEXISTENT_ID")
    assert meta.disc_number is None
    assert meta.disc_total == 10  # medium-count always populated
    assert meta.set_title is None
    assert meta.album == "The Complete Studio Albums"
    assert len(meta.tracks) == 2  # both mediums flattened


def test_parse_release_no_disc_id_flattens_all_mediums():
    """Without _disc_id, all mediums are returned; disc_total still set from medium-count."""
    meta = _parse_release(_MOCK_BOXSET_RELEASE)
    assert meta.disc_number is None
    assert meta.disc_total == 10  # medium-count always populated
    assert len(meta.tracks) == 2


def test_lookup_disc_id_populates_disc_position():
    """lookup_disc_id passes the computed disc_id to _parse_release."""
    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc_id_str = disc_id_from_rbi(disc)
    boxset_response = {
        "disc": {
            "id": disc_id_str,
            "release-list": [
                {
                    **_MOCK_BOXSET_RELEASE,
                    "medium-list": [
                        {
                            "position": "8",
                            "title": "Eliminator",
                            "disc-list": [{"id": disc_id_str}],
                            "track-list": [
                                {
                                    "number": "1",
                                    "recording": {
                                        "title": "Sharp Dressed Man",
                                        "length": "248000",
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    }
    with patch("musicbrainzngs.get_releases_by_discid", return_value=boxset_response):
        results = lookup_disc_id(disc)
    assert len(results) == 1
    r = results[0]
    assert r.album == "Eliminator"
    assert r.set_title == "The Complete Studio Albums"
    assert r.disc_number == 8
    assert r.disc_total == 10


# ---------------------------------------------------------------------------
# _merge_into_disc / _overwrite_disc — disc position propagation
# ---------------------------------------------------------------------------


def test_merge_into_disc_propagates_disc_position():
    """disc_number/disc_total from meta update disc when meta has values."""
    disc = _make_disc(tracks=[])
    meta = DiscMeta(disc_number=3, disc_total=5, set_title="Box Set Title")
    result = _merge_into_disc(meta, disc)
    assert result.disc_number == 3
    assert result.disc_total == 5
    assert result.set_title == "Box Set Title"


def test_merge_into_disc_preserves_existing_disc_position():
    """Existing disc position is not overwritten when meta has no position."""
    disc = _make_disc(tracks=[])
    disc.disc_number = 2
    disc.disc_total = 4
    meta = DiscMeta(disc_number=None, disc_total=None)
    result = _merge_into_disc(meta, disc)
    assert result.disc_number == 2
    assert result.disc_total == 4


def test_overwrite_disc_propagates_disc_position():
    """_overwrite_disc updates position from meta when meta has values."""
    disc = _make_disc(tracks=[])
    disc.disc_number = 1
    disc.disc_total = 1
    meta = DiscMeta(
        disc_number=8, disc_total=10, set_title="The Complete Studio Albums"
    )
    result = _overwrite_disc(meta, disc)
    assert result.disc_number == 8
    assert result.disc_total == 10
    assert result.set_title == "The Complete Studio Albums"


# ---------------------------------------------------------------------------
# prepopulate_from_mb
# ---------------------------------------------------------------------------


def test_prepopulate_single_match_fills_fields():
    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.artist = "Unknown Artist"
    match = DiscMeta(album="Found Album", artist="Found Artist", tracks=[])
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[match]):
        r = prepopulate_from_mb(disc, verbose=False)
    assert r.disc.artist == "Found Artist"
    assert r.disc.album == "Test Album"  # existing album preserved
    assert r.barcode_hints == []  # match had no catalog
    assert r.match_count == 1
    assert r.isrc_disambiguated is False  # N=1 never sets the flag


def test_prepopulate_multiple_matches_no_change():
    disc = _make_disc(tracks=[(1, 0, 18000)])
    matches = [DiscMeta(album="A"), DiscMeta(album="B")]
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=matches):
        r = prepopulate_from_mb(disc, verbose=False)
    assert r.disc.album == "Test Album"  # unchanged: no ISRCs → no disambiguation
    assert r.barcode_hints == []
    assert r.match_count == 2
    assert r.isrc_disambiguated is False


def test_prepopulate_no_matches_no_change():
    disc = _make_disc(tracks=[(1, 0, 18000)])
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[]):
        r = prepopulate_from_mb(disc, verbose=False)
    assert r.disc is disc  # same object, not a copy
    assert r.barcode_hints == []
    assert r.match_count == 0
    assert r.isrc_disambiguated is False


def test_prepopulate_returns_barcode_hints_from_all_matches():
    """Hints are drawn from every match, even when len(matches) > 1 skips the merge.

    R16: hints carry their source MB release MBID alongside the barcode.
    Releases that lack an MBID get an empty-string tag (defensive).
    Duplicate (mbid, barcode) pairs are dropped; same barcode under
    different MBIDs is preserved as two separate entries.
    """
    disc = _make_disc(tracks=[(1, 0, 18000)])
    matches = [
        DiscMeta(album="A", mb_release_id="rid-A", catalog="0075992377423"),
        # Duplicate (rid-A, 0075992377423) — dropped
        DiscMeta(album="A2", mb_release_id="rid-A", catalog="0075992377423"),
        # Same barcode, different MBID — kept
        DiscMeta(album="B", mb_release_id="rid-B", catalog="0075992377423"),
        DiscMeta(album="C", mb_release_id="rid-C", catalog="4012345678901"),
        DiscMeta(album="D"),  # no catalog → dropped
    ]
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=matches):
        r = prepopulate_from_mb(disc, verbose=False)
    assert r.disc.album == "Test Album"  # multi-match → no merge (no ISRCs to score)
    assert sorted(r.barcode_hints) == [
        ("rid-A", "0075992377423"),
        ("rid-B", "0075992377423"),
        ("rid-C", "4012345678901"),
    ]
    assert r.match_count == 5
    assert r.isrc_disambiguated is False


# ---------------------------------------------------------------------------
# R1 — ISRC-based multi-match disambiguation
# ---------------------------------------------------------------------------


def _disc_with_isrcs(isrcs_by_track: dict[int, str]) -> RBIDisc:
    """Build a disc with a few tracks, each optionally carrying an ISRC."""
    entries = [
        RBITocEntry(
            track_number=n,
            title=f"Track {n}",
            performer="Artist",
            start_frame=(n - 1) * 10000,
            duration_frames=10000,
            isrc=isrcs_by_track.get(n),
        )
        for n in (1, 2, 3, 4)
    ]
    return RBIDisc(album="Disc Album", artist="Disc Artist", tracks=entries)


def test_score_candidate_by_isrcs_counts_per_track_agreements():
    """Same track number on both sides + same ISRC = 1 point each."""
    from cdda2img.mb_lookup import _score_candidate_by_isrcs

    disc = _disc_with_isrcs({
        1: "AAA0000000001",
        2: "AAA0000000002",
        3: "AAA0000000003",
    })
    meta = DiscMeta(
        tracks=[
            TrackMeta(number=1, isrc="AAA0000000001"),
            TrackMeta(number=2, isrc="AAA0000000002"),
            TrackMeta(number=3, isrc="DIFFERENT_ISRC"),
        ]
    )
    assert _score_candidate_by_isrcs(meta, disc) == 2


def test_score_candidate_by_isrcs_zero_when_disc_has_no_isrcs():
    """No ISRCs on the disc means no evidence; cannot score."""
    from cdda2img.mb_lookup import _score_candidate_by_isrcs

    disc = _disc_with_isrcs({})  # all tracks isrc=None
    meta = DiscMeta(
        tracks=[TrackMeta(number=1, isrc="AAA0000000001")],
    )
    assert _score_candidate_by_isrcs(meta, disc) == 0


def test_score_candidate_by_isrcs_position_mismatch_does_not_score():
    """Same ISRC string on a different track number does NOT score.

    Defends against compilations where two unrelated releases share a few
    ISRC strings on different track positions.
    """
    from cdda2img.mb_lookup import _score_candidate_by_isrcs

    disc = _disc_with_isrcs({1: "AAA0000000001"})
    meta = DiscMeta(
        tracks=[TrackMeta(number=2, isrc="AAA0000000001")],  # same ISRC, wrong track
    )
    assert _score_candidate_by_isrcs(meta, disc) == 0


def test_prepopulate_multiple_matches_isrc_disambiguates_winner():
    """N>1 with a strict ISRC-score winner above the floor → auto-merge."""
    disc = _disc_with_isrcs({
        1: "AAA0000000001",
        2: "AAA0000000002",
        3: "AAA0000000003",
    })
    winner = DiscMeta(
        album="Winner",
        artist="Found",
        mb_release_id="rid-win",
        catalog="0075992377423",
        tracks=[
            TrackMeta(number=1, isrc="AAA0000000001"),
            TrackMeta(number=2, isrc="AAA0000000002"),
            TrackMeta(number=3, isrc="AAA0000000003"),
        ],
    )
    loser = DiscMeta(
        album="Loser",
        artist="Other",
        mb_release_id="rid-lose",
        catalog="4012345678901",
        tracks=[TrackMeta(number=1, isrc="DIFFERENT_ISRC")],
    )
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[loser, winner]):
        r = prepopulate_from_mb(disc, verbose=False)
    # _merge_into_disc preserves existing non-blank disc fields, so verify the
    # winner via fields the input disc did not already supply.
    assert r.disc.album == "Disc Album"
    assert r.disc.catalog == "0075992377423"  # winner's catalog filled in
    assert r.disc.mb_release_id == "rid-win"  # winner's MBID filled in
    assert r.match_count == 2
    assert r.isrc_disambiguated is True


def test_prepopulate_multiple_matches_isrc_tie_no_merge():
    """Two candidates tied at the top score → preserve no-auto-merge fallback."""
    disc = _disc_with_isrcs({
        1: "AAA0000000001",
        2: "AAA0000000002",
        3: "AAA0000000003",
    })
    a = DiscMeta(
        album="A",
        artist="X",
        mb_release_id="rid-a",
        tracks=[
            TrackMeta(number=1, isrc="AAA0000000001"),
            TrackMeta(number=2, isrc="AAA0000000002"),
        ],
    )
    b = DiscMeta(
        album="B",
        artist="Y",
        mb_release_id="rid-b",
        tracks=[
            TrackMeta(number=1, isrc="AAA0000000001"),
            TrackMeta(number=2, isrc="AAA0000000002"),
        ],
    )
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[a, b]):
        r = prepopulate_from_mb(disc, verbose=False)
    assert r.disc.album == "Disc Album"
    assert r.disc.artist == "Disc Artist"  # unchanged: tie means no merge
    assert r.match_count == 2
    assert r.isrc_disambiguated is False


def test_prepopulate_multiple_matches_score_below_floor_no_merge():
    """Top score == 1 (below ``_MIN_ISRC_AGREE=2``) → preserve no-auto-merge."""
    disc = _disc_with_isrcs({1: "AAA0000000001"})
    a = DiscMeta(
        album="A",
        artist="X",
        mb_release_id="rid-a",
        tracks=[TrackMeta(number=1, isrc="AAA0000000001")],  # score = 1
    )
    b = DiscMeta(
        album="B",
        artist="Y",
        mb_release_id="rid-b",
        tracks=[TrackMeta(number=1, isrc="DIFFERENT_ISRC")],  # score = 0
    )
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[a, b]):
        r = prepopulate_from_mb(disc, verbose=False)
    assert r.disc.album == "Disc Album"
    assert r.disc.artist == "Disc Artist"
    assert r.match_count == 2
    assert r.isrc_disambiguated is False


# ---------------------------------------------------------------------------
# R16 — barcode hints round-trip through _collect_barcode_candidates
# ---------------------------------------------------------------------------


def test_collect_barcode_candidates_accepts_r16_tuple_form():
    """The R16 (mbid, barcode) tuple form flows through to the candidate list.

    Confirms the tuple-form barcode_hints produced by prepopulate_from_mb
    is correctly unpacked by _collect_barcode_candidates downstream.
    """
    from cdda2img.cdda2img import _collect_barcode_candidates

    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.catalog = None
    hints = [
        ("rid-A", "0075992377423"),
        ("rid-B", "0081227991159"),
        ("rid-C", "0075992377423"),  # duplicate barcode under different MBID — dropped
    ]
    candidates = _collect_barcode_candidates(disc, hints)
    assert candidates == ["0075992377423", "0081227991159"]


# ---------------------------------------------------------------------------
# R4 — ISRC tally fallback for zero-disc-ID-match case
# ---------------------------------------------------------------------------


def _disc_with_track_isrcs(isrcs: list[str]) -> RBIDisc:
    entries = [
        RBITocEntry(
            track_number=i + 1,
            title=f"T{i + 1}",
            performer="Artist",
            start_frame=i * 10000,
            duration_frames=10000,
            isrc=isrc,
        )
        for i, isrc in enumerate(isrcs)
    ]
    return RBIDisc(album="Test Album", artist="Test Artist", tracks=entries)


def test_r4_isrc_tally_no_isrcs_returns_none():
    """Below the ISRC-bearing-tracks floor → no tally."""
    from cdda2img.mb_lookup import _resolve_via_isrc_tally

    disc = _disc_with_track_isrcs([])
    assert _resolve_via_isrc_tally(disc) is None


def test_r4_isrc_tally_below_floor_returns_none():
    """3 ISRC-bearing tracks but tally fails to reach floor → None."""
    from cdda2img.mb_lookup import _resolve_via_isrc_tally

    disc = _disc_with_track_isrcs(["USAA10100001", "USAA10100002", "USAA10100003"])
    # Each ISRC returns a DIFFERENT release; nothing converges.
    side_effect_map = {
        "USAA10100001": [DiscMeta(mb_release_id="rid-1")],
        "USAA10100002": [DiscMeta(mb_release_id="rid-2")],
        "USAA10100003": [DiscMeta(mb_release_id="rid-3")],
    }
    with patch(
        "cdda2img.mb_lookup.lookup_isrc",
        side_effect=lambda isrc: side_effect_map.get(isrc, []),
    ):
        assert _resolve_via_isrc_tally(disc) is None


def test_r4_isrc_tally_converges_above_floor():
    """When ≥ ceil(N/2) ISRCs converge on the same release → that release wins."""
    from cdda2img.mb_lookup import _resolve_via_isrc_tally

    disc = _disc_with_track_isrcs([
        "USAA10100001",
        "USAA10100002",
        "USAA10100003",
        "USAA10100004",
    ])
    # 3/4 ISRCs point to rid-w; 1 to rid-other.
    winner = DiscMeta(album="Album", mb_release_id="rid-w")
    other = DiscMeta(album="Other", mb_release_id="rid-other")
    isrc_map = {
        "USAA10100001": [winner],
        "USAA10100002": [winner],
        "USAA10100003": [other],
        "USAA10100004": [winner],
    }
    with patch(
        "cdda2img.mb_lookup.lookup_isrc",
        side_effect=lambda isrc: isrc_map.get(isrc, []),
    ):
        result = _resolve_via_isrc_tally(disc)

    assert result is not None
    assert result.mb_release_id == "rid-w"


def test_r4_isrc_tally_tie_returns_none():
    """Two releases tied at the top tally → no auto-merge."""
    from cdda2img.mb_lookup import _resolve_via_isrc_tally

    disc = _disc_with_track_isrcs([
        "USAA10100001",
        "USAA10100002",
        "USAA10100003",
        "USAA10100004",
    ])
    a = DiscMeta(album="A", mb_release_id="rid-a")
    b = DiscMeta(album="B", mb_release_id="rid-b")
    isrc_map = {
        "USAA10100001": [a],
        "USAA10100002": [b],
        "USAA10100003": [a],
        "USAA10100004": [b],
    }
    with patch(
        "cdda2img.mb_lookup.lookup_isrc",
        side_effect=lambda isrc: isrc_map.get(isrc, []),
    ):
        result = _resolve_via_isrc_tally(disc)
    assert result is None


def test_prepopulate_zero_matches_triggers_r4_tally():
    """When lookup_disc_id returns 0 but R4 tally finds a winner → merge."""
    disc = _disc_with_track_isrcs(["USAA10100001", "USAA10100002", "USAA10100003"])
    disc.artist = "Unknown Artist"  # so the merge can fill it
    winner = DiscMeta(
        album="Found Album",
        artist="Found Artist",
        mb_release_id="rid-w",
        catalog="0075992377423",
    )
    with (
        patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[]),
        patch(
            "cdda2img.mb_lookup.lookup_isrc",
            side_effect=lambda isrc: [winner] if isrc.startswith("US") else [],
        ),
    ):
        r = prepopulate_from_mb(disc, verbose=False)
    assert r.disc.artist == "Found Artist"
    assert r.disc.mb_release_id == "rid-w"
    assert r.match_count == 0  # disc-ID returned nothing
    assert r.isrc_disambiguated is False  # R4 path doesn't set this
