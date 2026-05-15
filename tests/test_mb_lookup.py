"""
test_mb_lookup.py — MusicBrainz lookup tests.

Pure computation tests (compute_disc_id, disc_id_from_rbi) need no mocking.
Network lookup tests mock musicbrainzngs to avoid any network dependency.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from cdda2img.lookup_result import (
    REMASTERED_NO,
    REMASTERED_POSSIBLE,
    REMASTERED_UNKNOWN,
    REMASTERED_YES,
    DiscMeta,
    TrackMeta,
)
from cdda2img.mb_lookup import (
    _classify_remaster,
    _merge_into_disc,
    _overwrite_disc,
    _parse_release,
    _parse_year,
    compute_disc_id,
    disc_id_from_rbi,
    guess_remaster_status,
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
# _parse_year / _classify_remaster
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


@pytest.mark.parametrize(
    "title,orig_year,current_year,expected",
    [
        # Original pre-war, current pre-war -> NO
        ("Album", 1985, 1990, REMASTERED_NO),
        # Original pre-war, current post-war, no keyword -> POSSIBLE
        ("Album", 1985, 2003, REMASTERED_POSSIBLE),
        # Original pre-war, current post-war, with keyword -> YES
        ("Album (Remastered)", 1985, 2003, REMASTERED_YES),
        # No original date, current post-war -> POSSIBLE
        ("Album", None, 2005, REMASTERED_POSSIBLE),
        # No original date, current post-war, keyword -> YES
        ("Album Remaster", None, 2005, REMASTERED_YES),
        # No dates -> UNKNOWN
        ("Album", None, None, REMASTERED_UNKNOWN),
    ],
)
def test_classify_remaster(title, orig_year, current_year, expected):
    assert _classify_remaster(title, orig_year, current_year) == expected


# ---------------------------------------------------------------------------
# guess_remaster_status
# ---------------------------------------------------------------------------


def _disc_with(
    album: str | None = None,
    release_date: str | None = None,
    original_release_date: str | None = None,
) -> RBIDisc:
    return RBIDisc(
        album=album or "",
        artist="Artist",
        release_date=release_date,
        original_release_date=original_release_date,
    )


@pytest.mark.parametrize(
    "album,release,original,expected",
    [
        # Keyword match → YES regardless of dates
        ("Album (Remastered)", "2003", None, REMASTERED_YES),
        ("Deluxe Edition", "1990", None, REMASTERED_YES),
        ("25th Anniversary", None, None, REMASTERED_YES),
        ("Reissue 2010", "2010", None, REMASTERED_YES),
        ("Expanded Edition", "2020", "1985", REMASTERED_YES),
        # Original year earlier than release year, no keyword → YES
        ("Album", "2003", "1985", REMASTERED_YES),
        # Release post-LOUDNESS_WAR_YEAR, no original, no keyword → POSSIBLE
        ("Album", "2005", None, REMASTERED_POSSIBLE),
        ("Album", "1994", None, REMASTERED_POSSIBLE),
        # Release pre-LOUDNESS_WAR_YEAR, no keyword → NO
        ("Album", "1990", None, REMASTERED_NO),
        ("Album", "1993", None, REMASTERED_NO),
        # No release date → UNKNOWN
        ("Album", None, None, REMASTERED_UNKNOWN),
        # Empty / None album with no dates → UNKNOWN
        ("", None, None, REMASTERED_UNKNOWN),
    ],
)
def test_guess_remaster_status(album, release, original, expected):
    disc = _disc_with(album=album, release_date=release, original_release_date=original)
    assert guess_remaster_status(disc) == expected


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
    meta = _make_meta_disc()
    disc = _make_disc(tracks=[(1, 0, 10000), (2, 10000, 9000)])
    disc.tracks[0].isrc = "EXISTING0001"
    result = _merge_into_disc(meta, disc)
    assert result.tracks[0].isrc == "EXISTING0001"


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
    """No disc-list match falls back to all mediums with no disc position set."""
    meta = _parse_release(_MOCK_BOXSET_RELEASE, _disc_id="NONEXISTENT_ID")
    assert meta.disc_number is None
    assert meta.disc_total is None
    assert meta.set_title is None
    assert meta.album == "The Complete Studio Albums"
    assert len(meta.tracks) == 2  # both mediums flattened


def test_parse_release_no_disc_id_flattens_all_mediums():
    """Without _disc_id, all mediums are returned (text-search path)."""
    meta = _parse_release(_MOCK_BOXSET_RELEASE)
    assert meta.disc_number is None
    assert meta.disc_total is None
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
        result = prepopulate_from_mb(disc, verbose=False)
    assert result.artist == "Found Artist"
    assert result.album == "Test Album"  # existing album preserved


def test_prepopulate_multiple_matches_no_change():
    disc = _make_disc(tracks=[(1, 0, 18000)])
    matches = [DiscMeta(album="A"), DiscMeta(album="B")]
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=matches):
        result = prepopulate_from_mb(disc, verbose=False)
    assert result.album == "Test Album"  # unchanged


def test_prepopulate_no_matches_no_change():
    disc = _make_disc(tracks=[(1, 0, 18000)])
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[]):
        result = prepopulate_from_mb(disc, verbose=False)
    assert result is disc  # same object, not a copy
