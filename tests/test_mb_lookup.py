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
    _agreed_tracks,
    _agreed_value,
    _build_agreed_facts_meta,
    _disambiguate_by_mcn,
    _find_disc_medium,
    _is_consistent,
    _merge_into_disc,
    _overwrite_disc,
    _parse_release,
    _parse_year,
    _plurality_release_group,
    _resolve_via_isrc_tally,
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


def test_parse_release_duration_uses_track_length_not_recording_length():
    """duration_ms must come from per-medium track.length (TOC-derived), not
    recording.length (shared canonical value).

    For a disc-ID-matched release, track.length agrees with the physical TOC
    to within rounding (it was set from that TOC); recording.length can be off
    by seconds and was the source of the R3 sum-of-durations false-reject
    (ZZ Top *Eliminator*: 11 tracks, sum(track.length)=2721.0s matched the disc
    at 2720.2s, but sum(recording.length)=2710.8s tripped the ±2s gate). When
    no track-level length exists, duration_ms stays None (no fallback to
    recording.length) so the R3 gate skips on no-evidence rather than comparing
    against the wrong quantity.
    """
    release = {
        "id": "mock-uuid",
        "title": "Eliminator",
        "date": "1983",
        "medium-count": 1,
        "artist-credit": [{"artist": {"name": "ZZ Top"}, "joinphrase": ""}],
        "release-group": {"id": "rg", "first-release-date": "1983"},
        "medium-list": [
            {
                "position": "1",
                "track-list": [
                    {
                        "number": "1",
                        "length": "248000",  # TOC-derived — the right field
                        "recording": {
                            "title": "Sharp Dressed Man",
                            "length": "243000",  # canonical — must NOT be used
                        },
                    },
                    {
                        "number": "2",
                        # no track-level length → duration_ms is None, no fallback
                        "recording": {"title": "Legs", "length": "260000"},
                    },
                ],
            }
        ],
    }
    meta = _parse_release(release)
    assert meta.tracks[0].duration_ms == 248000  # track.length, not 243000
    assert meta.tracks[1].duration_ms is None  # no fallback to recording.length


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
# Release-group adoption from an un-disambiguated multi-match
# ---------------------------------------------------------------------------


def test_plurality_release_group_picks_strict_winner():
    matches = [
        DiscMeta(album="A", mb_release_group_id="rg-elim"),
        DiscMeta(album="A", mb_release_group_id="rg-elim"),
        DiscMeta(album="A", mb_release_group_id="rg-elim"),
        DiscMeta(album="2-in-1", mb_release_group_id="rg-comp"),
    ]
    assert _plurality_release_group(matches) == "rg-elim"


def test_plurality_release_group_tie_returns_none():
    matches = [
        DiscMeta(album="A", mb_release_group_id="rg-x"),
        DiscMeta(album="B", mb_release_group_id="rg-y"),
    ]
    assert _plurality_release_group(matches) is None


def test_plurality_release_group_no_rg_returns_none():
    assert _plurality_release_group([DiscMeta(album="A"), DiscMeta(album="B")]) is None


def test_prepopulate_multimatch_adopts_plurality_release_group():
    """A multi-match the ISRC disambiguator can't break still yields the album's
    release-group (plurality), so original-release can resolve pre-menu.

    Mirrors ZZ Top *Eliminator*: four pressings share one RG, a 2-in-1 comp is a
    fifth RG, and blank ISRCs leave the pressing un-disambiguated.
    """
    disc = _make_disc(tracks=[(1, 0, 18000)])
    assert disc.mb_release_group_id is None
    matches = [
        DiscMeta(album="Eliminator", mb_release_id="r1", mb_release_group_id="rg-elim"),
        DiscMeta(album="Eliminator", mb_release_id="r2", mb_release_group_id="rg-elim"),
        DiscMeta(album="Eliminator", mb_release_id="r3", mb_release_group_id="rg-elim"),
        DiscMeta(album="Eliminator", mb_release_id="r4", mb_release_group_id="rg-elim"),
        DiscMeta(album="2 in 1", mb_release_id="r5", mb_release_group_id="rg-comp"),
    ]
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=matches):
        r = prepopulate_from_mb(disc, verbose=False)
    # Pressing-level fields stay from the disc (no merge); only the RG is adopted.
    assert r.disc.album == "Test Album"
    assert r.disc.mb_release_group_id == "rg-elim"
    assert r.match_count == 5
    assert r.isrc_disambiguated is False


def test_prepopulate_multimatch_rg_tie_leaves_group_unset():
    disc = _make_disc(tracks=[(1, 0, 18000)])
    matches = [
        DiscMeta(album="X", mb_release_group_id="rg-x"),
        DiscMeta(album="Y", mb_release_group_id="rg-y"),
    ]
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=matches):
        r = prepopulate_from_mb(disc, verbose=False)
    assert r.disc.mb_release_group_id is None


# ---------------------------------------------------------------------------
# MCN/barcode multi-match disambiguation
# ---------------------------------------------------------------------------

_ELIMINATOR_MCN = "0075992377423"  # ZZ Top - Eliminator, EU 1983 (valid GTIN-13)


def test_disambiguate_by_mcn_matches_barcode():
    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.catalog = _ELIMINATOR_MCN
    matches = [
        DiscMeta(album="Other", catalog="4012345678901", mb_release_id="r0"),
        DiscMeta(album="Eliminator", catalog=_ELIMINATOR_MCN, mb_release_id="r1"),
    ]
    w = _disambiguate_by_mcn(matches, disc)
    assert w is not None and w.mb_release_id == "r1"


def test_disambiguate_by_mcn_shared_barcode_returns_none():
    """A barcode shared by two pressings (DE + XE) is not uniquely identifying,
    so we return None rather than fabricate one pressing's date / release id."""
    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.catalog = _ELIMINATOR_MCN
    matches = [
        DiscMeta(
            album="E", catalog=_ELIMINATOR_MCN, release_date="1983", mb_release_id="xe"
        ),
        DiscMeta(
            album="E",
            catalog=_ELIMINATOR_MCN,
            release_date="1983-11-18",
            mb_release_id="de",
        ),
    ]
    assert _disambiguate_by_mcn(matches, disc) is None


def test_disambiguate_by_mcn_no_match_or_no_mcn_returns_none():
    disc = _make_disc(tracks=[(1, 0, 18000)])
    matches = [DiscMeta(album="E", catalog=_ELIMINATOR_MCN, mb_release_id="r1")]
    # No MCN on the disc → None.
    assert _disambiguate_by_mcn(matches, disc) is None
    # MCN present but matches no candidate barcode → None.
    disc.catalog = "5099747023521"  # valid GTIN-13, not among candidates
    assert _disambiguate_by_mcn(matches, disc) is None


def test_prepopulate_multimatch_unique_mcn_picks_pressing():
    """A disc MCN that matches exactly ONE candidate barcode pins that pressing,
    so the full pressing (exact date + release id) is merged."""
    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.catalog = _ELIMINATOR_MCN
    matches = [
        DiscMeta(
            album="E", mb_release_id="us", mb_release_group_id="rg-e"
        ),  # no barcode
        DiscMeta(
            album="E",
            catalog=_ELIMINATOR_MCN,
            release_date="1983-11-18",
            mb_release_id="de",
            mb_release_group_id="rg-e",
        ),
    ]
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=matches):
        r = prepopulate_from_mb(disc, verbose=False)
    assert r.disc.release_date == "1983-11-18"  # unique barcode → exact pressing
    assert r.disc.mb_release_id == "de"
    assert r.isrc_disambiguated is False


def test_prepopulate_multimatch_shared_barcode_agreed_facts_only():
    """A barcode shared by two pressings is not uniquely identifying. Fill only
    the agreed year + release-group; leave exact date / release id blank."""
    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.catalog = _ELIMINATOR_MCN
    matches = [
        DiscMeta(album="E", mb_release_id="us", mb_release_group_id="rg-e"),
        DiscMeta(
            album="E",
            catalog=_ELIMINATOR_MCN,
            release_date="1983-11-18",
            mb_release_id="de",
            mb_release_group_id="rg-e",
        ),
        DiscMeta(
            album="E",
            catalog=_ELIMINATOR_MCN,
            release_date="1983",
            mb_release_id="xe",
            mb_release_group_id="rg-e",
        ),
    ]
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=matches):
        r = prepopulate_from_mb(disc, verbose=False)
    assert r.disc.release_date == "1983"  # agreed YEAR only, not "1983-11-18"
    assert r.disc.mb_release_group_id == "rg-e"
    assert r.disc.mb_release_id is None  # specific pressing left undetermined
    assert r.isrc_disambiguated is False


def test_prepopulate_multimatch_agreed_facts_fills_year_and_shared_isrcs():
    """The Eliminator case: no disc MCN, no ISRC winner, but all candidates in
    the plurality release-group agree on the year and per-track ISRCs → fill
    those, leave the pressing (date / release id) blank."""
    disc = _make_disc(tracks=[(1, 0, 18000), (2, 18000, 20000)])
    # No disc.catalog (this pressing has no Q-channel MCN).
    shared_tracks = [
        TrackMeta(number=1, isrc="USRHD0709703"),
        TrackMeta(number=2, isrc="USWB10301935"),
    ]
    matches = [
        DiscMeta(
            album="E",
            release_date="1983-03-23",
            mb_release_id="us",
            mb_release_group_id="rg-e",
            tracks=shared_tracks,
        ),
        DiscMeta(
            album="E",
            release_date="1983-11-18",
            mb_release_id="de",
            mb_release_group_id="rg-e",
            tracks=shared_tracks,
        ),
        DiscMeta(
            album="comp",
            release_date="2008",
            mb_release_id="cmp",
            mb_release_group_id="rg-comp",
            tracks=shared_tracks,
        ),
    ]
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=matches):
        r = prepopulate_from_mb(disc, verbose=False)
    assert r.disc.release_date == "1983"  # both Eliminator pressings agree
    assert r.disc.mb_release_group_id == "rg-e"
    assert r.disc.mb_release_id is None  # no pressing fabricated
    assert r.disc.tracks[0].isrc == "USRHD0709703"  # shared ISRCs filled
    assert r.disc.tracks[1].isrc == "USWB10301935"


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
    # Loser is *consistent* (no contradicting ISRC — Unit G would otherwise drop
    # it) but covers none of the disc's ISRCs, so it scores 0 and R1 picks the
    # winner. This keeps the test exercising R1, not the Unit-G pre-filter.
    loser = DiscMeta(
        album="Loser",
        artist="Other",
        mb_release_id="rid-lose",
        tracks=[],
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
    """Top score == 1 (below ``_MIN_ISRC_AGREE=2``) → preserve no-auto-merge.

    Both candidates are *consistent* (b's track ISRC is blank, not contradicting,
    so Unit G keeps it) and carry no release-group, so neither R1 nor the
    agreed-facts fallback can pick a winner → disc unchanged.
    """
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
        tracks=[],  # consistent (blank), score = 0
    )
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[a, b]):
        r = prepopulate_from_mb(disc, verbose=False)
    assert r.disc.album == "Disc Album"
    assert r.disc.artist == "Disc Artist"
    assert r.match_count == 2
    assert r.rejected_inconsistent == 0
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


def test_collect_barcode_candidates_valid_ondisc_mcn_leads():
    """A check-digit-valid on-disc MCN ranks first (gospel + clean)."""
    from cdda2img.cdda2img import _collect_barcode_candidates

    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.catalog = "0075992377423"  # valid GTIN-13
    candidates = _collect_barcode_candidates(disc, [("rid-A", "0081227991159")])
    assert candidates == ["0075992377423", "0081227991159"]


def test_collect_barcode_candidates_invalid_ondisc_mcn_is_last_resort():
    """An on-disc MCN with a bad check digit is kept but ranked BELOW valid hints.

    It is burnable (13 numeric digits) so we never drop it, but a clean MB
    barcode hint must win — a check-digit failure on the Q-channel MCN is
    usually a read error.
    """
    from cdda2img.cdda2img import _collect_barcode_candidates

    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.catalog = "1234567890123"  # 13 digits, wrong check digit
    candidates = _collect_barcode_candidates(disc, [("rid-A", "0081227991159")])
    # Valid hint first; burnable-but-invalid on-disc MCN as last resort.
    assert candidates == ["0081227991159", "1234567890123"]


def test_collect_barcode_candidates_invalid_ondisc_mcn_kept_when_sole():
    """With no other candidate, the burnable invalid-check-digit MCN is used."""
    from cdda2img.cdda2img import _collect_barcode_candidates

    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.catalog = "1234567890123"  # 13 digits, wrong check digit
    candidates = _collect_barcode_candidates(disc, [])
    assert candidates == ["1234567890123"]


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
    assert result.album == "Album"  # the convergent winner was picked
    # F-002: the recording-level release id is nulled (not disc-ID-verified).
    assert result.mb_release_id is None


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
    # F-002: the R4 tally merges album/artist/RG but must NOT write a
    # recording-level mb_release_id as authoritative release provenance.
    assert r.disc.mb_release_id is None
    assert r.match_count == 0  # disc-ID returned nothing
    assert r.isrc_disambiguated is False  # R4 path doesn't set this


# ---------------------------------------------------------------------------
# Unit C — correctness fixes (agent audit, Priority #1)
# ---------------------------------------------------------------------------


def test_merge_preserves_pre_emphasis():
    """C1/F-001: pre_emphasis (gates the R14 year cap) survives a merge."""
    meta = _make_meta_disc()
    disc = _make_disc(tracks=[(1, 0, 10000), (2, 10000, 9000)])
    disc.pre_emphasis = True
    result = _merge_into_disc(meta, disc)
    assert result.pre_emphasis is True


def test_overwrite_preserves_pre_emphasis_and_discogs():
    """C1/F-001: overwrite previously dropped pre_emphasis AND discogs_release_id."""
    meta = _make_meta_disc()
    disc = _make_disc(tracks=[(1, 0, 10000), (2, 10000, 9000)])
    disc.pre_emphasis = True
    disc.discogs_release_id = 4567
    result = _overwrite_disc(meta, disc)
    assert result.pre_emphasis is True
    assert result.discogs_release_id == 4567


def test_resolve_via_isrc_tally_nulls_recording_level_release_id():
    """C2/F-002: the ISRC-tally winner must not carry a recording-level release id."""
    disc = _make_disc(tracks=[(1, 0, 10000), (2, 10000, 9000), (3, 19000, 8000)])
    for i, t in enumerate(disc.tracks):
        t.isrc = f"USXX101000{i + 1:02d}"
    winner = DiscMeta(
        album="A",
        artist="B",
        mb_release_id="recording-level-release-uuid",
        mb_release_group_id="rg-uuid",
    )
    with patch("cdda2img.mb_lookup.lookup_isrc", return_value=[winner]):
        result = _resolve_via_isrc_tally(disc)
    assert result is not None
    assert result.mb_release_id is None  # nulled — not disc-ID-verified
    assert (
        result.mb_release_group_id == "rg-uuid"
    )  # RG kept for original-release lookup


def test_find_disc_medium_selects_correct_medium():
    """C3/F-003: with disc-list present, the matching medium is chosen, not flattened."""
    medium_list = [
        {"position": "1", "disc-list": [{"id": "disc-A"}], "track-list": []},
        {"position": "2", "disc-list": [{"id": "disc-B"}], "track-list": []},
    ]
    assert _find_disc_medium(medium_list, "disc-B") is medium_list[1]
    assert _find_disc_medium(medium_list, "disc-A") is medium_list[0]
    assert _find_disc_medium(medium_list, "disc-Z") is None


def test_lookup_disc_id_omits_discids_include():
    """Regression: "discids" must NOT be requested on the /discid endpoint.

    MB rejects the /discid lookup with HTTP 400 when "discids" is in the inc
    list (it is only valid on /release). The earlier code requested it (F-003)
    and the 400 was swallowed as "no match", silently breaking every disc-ID
    lookup and forcing the whole pipeline onto the CDDB fallback. The matching
    medium's disc-list is populated by the /discid endpoint regardless, so
    _find_disc_medium still works (see test_find_disc_medium_*).
    """
    disc = _make_disc(tracks=[(1, 0, 12345), (2, 12345, 6789)])
    captured: dict = {}

    def _fake(disc_id_str, includes):
        captured["includes"] = includes
        return {"disc": {"release-list": []}}

    with (
        patch("musicbrainzngs.get_releases_by_discid", side_effect=_fake),
        patch("cdda2img.lookup_cache.get_cached_disc_id_lookup", return_value=None),
        patch("cdda2img.lookup_cache.put_cached_disc_id_lookup"),
    ):
        lookup_disc_id(disc)
    assert "discids" not in captured["includes"]


def test_lookup_disc_id_400_logs_warning_not_silent(caplog):
    """A non-404 ResponseError (e.g. a 400 from a bad include) must be loud.

    Swallowing a 400 as a clean "no match" is exactly what hid the discids
    regression for so long. 404 stays quiet (a real "disc not in MB").
    """
    import logging

    import musicbrainzngs

    disc = _make_disc(tracks=[(1, 0, 12345), (2, 12345, 6789)])

    class _Cause(Exception):
        code = 400

    err = musicbrainzngs.ResponseError(cause=_Cause())

    with (
        patch("musicbrainzngs.get_releases_by_discid", side_effect=err),
        patch("cdda2img.lookup_cache.get_cached_disc_id_lookup", return_value=None),
        caplog.at_level(logging.WARNING, logger="cdda2img.mb_lookup"),
    ):
        assert lookup_disc_id(disc) == []
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_compute_disc_id_rejects_too_many_offsets():
    with pytest.raises(ValueError):
        compute_disc_id(1, 99, [150] * 100, 300000)


def test_compute_disc_id_rejects_negative_offset():
    with pytest.raises(ValueError):
        compute_disc_id(1, 2, [150, -5], 300000)


def test_compute_disc_id_rejects_out_of_range_track_numbers():
    with pytest.raises(ValueError):
        compute_disc_id(0, 2, [150, 10000], 300000)
    with pytest.raises(ValueError):
        compute_disc_id(1, 100, [150, 10000], 300000)


# ---------------------------------------------------------------------------
# Unit G — consistency gate (_is_consistent)
# ---------------------------------------------------------------------------


def test_is_consistent_blank_overlap_passes_vacuously():
    disc = _make_disc(tracks=[(1, 0, 18000)])  # no catalog, no track ISRC
    assert _is_consistent(DiscMeta(catalog="0093624877721"), disc) is True


def test_is_consistent_contradicting_mcn_rejected():
    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.catalog = "093624877721"  # American Idiot original
    assert _is_consistent(DiscMeta(catalog="0093624922315"), disc) is False  # reissue


def test_is_consistent_fuzzy_mcn_accepts_partial():
    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.catalog = "093624877721"  # printed GTIN-12
    assert _is_consistent(DiscMeta(catalog="0093624877721"), disc) is True  # EAN-13


def test_is_consistent_contradicting_isrc_rejected():
    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.tracks[0].isrc = "USRHD0709703"
    meta = DiscMeta(tracks=[TrackMeta(number=1, isrc="GBXXX1234567")])
    assert _is_consistent(meta, disc) is False


def test_is_consistent_matching_isrc_passes():
    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.tracks[0].isrc = "USRHD0709703"
    meta = DiscMeta(tracks=[TrackMeta(number=1, isrc="USRHD0709703")])
    assert _is_consistent(meta, disc) is True


# ---------------------------------------------------------------------------
# Unit G — prepopulate_from_mb consistency pre-filter
# ---------------------------------------------------------------------------


def test_prepopulate_single_match_contradicting_isrc_blanks():
    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.tracks[0].isrc = "USRHD0709703"
    match = DiscMeta(
        album="Wrong",
        release_date="1999",
        tracks=[TrackMeta(number=1, isrc="GBXXX1234567")],
    )
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[match]):
        r = prepopulate_from_mb(disc, verbose=False)
    assert r.disc.album == "Test Album"  # unchanged — wrong record rejected
    assert r.disc.release_date is None
    assert r.match_count == 0
    assert r.rejected_inconsistent == 1
    assert r.meta is None


def test_prepopulate_single_match_contradicting_mcn_blanks():
    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.catalog = "093624877721"
    match = DiscMeta(album="Reissue", catalog="0093624922315", release_date="2015")
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[match]):
        r = prepopulate_from_mb(disc, verbose=False)
    assert r.disc.release_date is None
    assert r.match_count == 0
    assert r.rejected_inconsistent == 1


def test_prepopulate_consistent_single_still_merges():
    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.catalog = "093624877721"  # printed GTIN-12 of the original
    match = DiscMeta(
        album="E",
        catalog="0093624877721",  # EAN-13 — fuzzy-matches
        release_date="1983-04-26",
        mb_release_id="orig",
    )
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[match]):
        r = prepopulate_from_mb(disc, verbose=False)
    assert r.disc.release_date == "1983-04-26"
    assert r.match_count == 1
    assert r.rejected_inconsistent == 0


def test_prepopulate_multimatch_drops_inconsistent_then_resolves():
    """Two pressings share the disc MCN (→ agreed-facts year); a reissue with a
    different barcode is dropped and counted."""
    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.catalog = "093624877721"
    s1 = DiscMeta(
        album="E",
        catalog="0093624877721",
        release_date="1983-04-26",
        mb_release_id="a",
        mb_release_group_id="rg-e",
    )
    s2 = DiscMeta(
        album="E",
        catalog="0093624877721",
        release_date="1983-11-18",
        mb_release_id="b",
        mb_release_group_id="rg-e",
    )
    bad = DiscMeta(
        album="Wrong",
        catalog="0093624922315",
        release_date="2015",
        mb_release_id="bad",
        mb_release_group_id="rg-x",
    )
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[s1, s2, bad]):
        r = prepopulate_from_mb(disc, verbose=False)
    assert r.rejected_inconsistent == 1
    assert r.match_count == 2
    assert r.disc.release_date == "1983"  # agreed year over the 2 survivors
    assert r.disc.mb_release_group_id == "rg-e"
    assert r.disc.mb_release_id is None


def test_prepopulate_r4_tally_winner_gated_by_consistency():
    """Zero disc-ID matches → R4 ISRC tally fires, but a tally winner whose MCN
    contradicts the disc MCN is still rejected (advisor #1)."""
    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.catalog = "093624877721"
    disc.tracks[0].isrc = "USRHD0709703"
    winner = DiscMeta(
        album="Wrong",
        catalog="0093624922315",  # contradicts the disc MCN
        release_date="2015",
        tracks=[TrackMeta(number=1, isrc="USRHD0709703")],  # ISRC half agrees
    )
    with (
        patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[]),
        patch("cdda2img.mb_lookup._resolve_via_isrc_tally", return_value=winner),
    ):
        r = prepopulate_from_mb(disc, verbose=False)
    assert r.disc.release_date is None  # winner rejected on MCN contradiction
    assert r.disc.album == "Test Album"
    assert r.meta is None


# ---------------------------------------------------------------------------
# #3-a Unit A — agreed-facts widening (album/artist/title) + MCN-matched subset
# ---------------------------------------------------------------------------


def test_agreed_value_unanimous_returns_value():
    assert _agreed_value(["A", "A", "A"]) == "A"


def test_agreed_value_ignores_blanks_among_agreers():
    """A blank/None is no evidence: a single distinct non-blank still wins."""
    assert _agreed_value(["A", None, "", "A"]) == "A"


def test_agreed_value_disagreement_returns_none():
    assert _agreed_value(["A", "B"]) is None


def test_agreed_value_all_blank_returns_none():
    assert _agreed_value([None, "", None]) is None


def test_agreed_tracks_isrc_and_title_decided_independently():
    """A track with a unanimous ISRC but a split title keeps the ISRC, drops the
    title (and vice versa) — the two fields are gated separately per track."""
    group = [
        DiscMeta(
            tracks=[
                TrackMeta(number=1, title="Song One", isrc="USRHD0709703"),
                TrackMeta(number=2, title="Song Two", isrc="USWB10301935"),
            ]
        ),
        DiscMeta(
            tracks=[
                TrackMeta(number=1, title="Song One", isrc="USRHD0709703"),
                TrackMeta(number=2, title="Song 2 (remaster)", isrc="USWB10301935"),
            ]
        ),
    ]
    tracks = {t.number: t for t in _agreed_tracks(group)}
    assert tracks[1].title == "Song One"  # unanimous title kept
    assert tracks[1].isrc == "USRHD0709703"
    assert tracks[2].title is None  # split title dropped
    assert tracks[2].isrc == "USWB10301935"  # unanimous ISRC kept


def test_build_agreed_facts_widens_album_artist_and_titles():
    """The Unit A widening: album, artist and per-track title now populate when
    the whole group agrees (previously only RG/year/ISRC were extracted)."""
    group = [
        DiscMeta(
            album="American Idiot",
            artist="Green Day",
            release_date="2004-09-20",
            mb_release_group_id="rg-ai",
            tracks=[TrackMeta(number=1, title="American Idiot")],
        ),
        DiscMeta(
            album="American Idiot",
            artist="Green Day",
            release_date="2004-09-21",  # different exact date, same year
            mb_release_group_id="rg-ai",
            tracks=[TrackMeta(number=1, title="American Idiot")],
        ),
    ]
    meta = _build_agreed_facts_meta(group, "rg-ai")
    assert meta.album == "American Idiot"
    assert meta.artist == "Green Day"
    assert meta.release_date == "2004"  # year only, not the split exact date
    assert meta.tracks[0].title == "American Idiot"
    assert meta.mb_release_id is None  # pressing still undetermined


def test_build_agreed_facts_album_disagreement_stays_none():
    """Disagreement on a field ⇒ that field is left None, never fabricated."""
    group = [
        DiscMeta(album="American Idiot", mb_release_group_id="rg-ai"),
        DiscMeta(album="American Idiot (Deluxe)", mb_release_group_id="rg-ai"),
    ]
    meta = _build_agreed_facts_meta(group, "rg-ai")
    assert meta.album is None


def test_prepop_multimatch_mcn_subset_excludes_blank_barcode_variant():
    """The American Idiot fix: a same-RG variant with a BLANK barcode passes the
    Unit-G gate vacuously and would break album unanimity — but the MCN-matched
    subset narrowing drops it, so the agreed album resolves to the original.

    Without narrowing, the variant's divergent album would collapse the agreed
    album to None (two distinct values); with it, only the two barcode-proven
    originals contribute → "American Idiot" fills the blank disc album.
    """
    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.album = ""  # blank so the agreed album can fill through (fill-blanks)
    disc.catalog = "093624877721"
    orig1 = DiscMeta(
        album="American Idiot",
        artist="Green Day",
        catalog="0093624877721",  # fuzzy-matches the disc MCN
        release_date="2004-09-20",
        mb_release_id="o1",
        mb_release_group_id="rg-ai",
    )
    orig2 = DiscMeta(
        album="American Idiot",
        artist="Green Day",
        catalog="093624877721",
        release_date="2004-11-18",
        mb_release_id="o2",
        mb_release_group_id="rg-ai",
    )
    blank_variant = DiscMeta(
        album="American Idiot: The Ultimate American Idiot",
        artist="Green Day",
        catalog=None,  # blank → vacuously consistent, NOT identity-proven
        release_date="2015",
        mb_release_id="v",
        mb_release_group_id="rg-ai",
    )
    with patch(
        "cdda2img.mb_lookup.lookup_disc_id",
        return_value=[orig1, orig2, blank_variant],
    ):
        r = prepopulate_from_mb(disc, verbose=False)
    assert r.rejected_inconsistent == 0  # blank barcode is not a contradiction
    assert r.match_count == 3  # all three survive Unit G
    assert r.disc.album == "American Idiot"  # narrowing excluded the variant
    assert r.disc.release_date == "2004"  # agreed year over the two originals
    assert r.disc.mb_release_id is None


def test_prepop_multimatch_no_positive_mcn_falls_back_to_full_set():
    """When the disc has an MCN but NO candidate barcode matches (MB lists none),
    the subset falls back to the full consistent set — RG plurality still holds,
    so the agreed album/year are taken over every candidate."""
    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.album = ""
    disc.catalog = "093624877721"
    matches = [
        DiscMeta(
            album="American Idiot",
            catalog=None,
            release_date="2004",
            mb_release_id="a",
            mb_release_group_id="rg-ai",
        ),
        DiscMeta(
            album="American Idiot",
            catalog=None,
            release_date="2004",
            mb_release_id="b",
            mb_release_group_id="rg-ai",
        ),
    ]
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=matches):
        r = prepopulate_from_mb(disc, verbose=False)
    assert r.match_count == 2
    assert r.disc.album == "American Idiot"  # full set used, both agree
    assert r.disc.release_date == "2004"


# ---------------------------------------------------------------------------
# Stage 7: last-resort duration match
# ---------------------------------------------------------------------------


def _raw_release(
    release_id: str,
    *,
    track_lengths: list[int | None] | None = None,
    recording_lengths: list[int | None] | None = None,
    album: str = "An Album",
    artist: str = "An Artist",
) -> dict:
    """Build a raw MB release dict with per-track ``length`` and/or
    ``recording.length``. A None entry omits that field for that track."""
    n = len(track_lengths or recording_lengths or [])
    tracks: list[dict] = []
    for i in range(n):
        track: dict = {"number": str(i + 1), "recording": {"title": f"T{i + 1}"}}
        if track_lengths is not None and track_lengths[i] is not None:
            track["length"] = str(track_lengths[i])
        if recording_lengths is not None and recording_lengths[i] is not None:
            track["recording"]["length"] = str(recording_lengths[i])
        tracks.append(track)
    return {
        "id": release_id,
        "title": album,
        "artist-credit": [{"artist": {"name": artist}, "joinphrase": ""}],
        "medium-list": [{"track-list": tracks}],
    }


def test_sum_track_lengths_all_present():
    from cdda2img.mb_lookup import _sum_track_lengths

    r = _raw_release("x", track_lengths=[200000, 180000, 240000])
    assert _sum_track_lengths(r) == 620000


def test_sum_track_lengths_missing_one_returns_none():
    from cdda2img.mb_lookup import _sum_track_lengths

    r = _raw_release("x", track_lengths=[200000, None, 240000])
    assert _sum_track_lengths(r) is None


def test_sum_track_lengths_empty_returns_none():
    from cdda2img.mb_lookup import _sum_track_lengths

    assert _sum_track_lengths({"medium-list": []}) is None


def test_sum_recording_lengths_all_present():
    from cdda2img.mb_lookup import _sum_recording_lengths

    r = _raw_release("x", recording_lengths=[210000, 190000])
    assert _sum_recording_lengths(r) == 400000


def test_sum_recording_lengths_missing_one_returns_none():
    from cdda2img.mb_lookup import _sum_recording_lengths

    r = _raw_release("x", recording_lengths=[210000, None])
    assert _sum_recording_lengths(r) is None


def test_pick_duration_match_closest_track_length_wins():
    from cdda2img.mb_lookup import pick_duration_match

    near = _raw_release("near", track_lengths=[300000, 300000])  # 600000
    far = _raw_release("far", track_lengths=[300000, 360000])  # 660000
    winner = pick_duration_match(
        [far, near], program_anchor_ms=602000, audio_anchor_ms=0
    )
    assert winner is not None
    assert winner["id"] == "near"


def test_pick_duration_match_rejects_beyond_tolerance():
    from cdda2img.mb_lookup import pick_duration_match

    r = _raw_release("x", track_lengths=[300000, 300000])  # 600000
    # 100s off the anchor — well beyond the 15s gross-mismatch gate.
    assert pick_duration_match([r], program_anchor_ms=700000, audio_anchor_ms=0) is None


def test_pick_duration_match_prefers_track_pool_over_recording_pool():
    """A track.length candidate wins even when a recording.length candidate is
    numerically closer — the two conventions are never mixed in one ranking."""
    from cdda2img.mb_lookup import pick_duration_match

    track_cand = _raw_release("track", track_lengths=[300000, 305000])  # 605000
    rec_cand = _raw_release("rec", recording_lengths=[300000, 300000])  # 600000
    winner = pick_duration_match(
        [rec_cand, track_cand],
        program_anchor_ms=600000,  # rec is exact, track is 5s off — yet track wins
        audio_anchor_ms=600000,
    )
    assert winner is not None
    assert winner["id"] == "track"


def test_pick_duration_match_falls_to_recording_pool_when_no_track_length():
    from cdda2img.mb_lookup import pick_duration_match

    rec_near = _raw_release("near", recording_lengths=[300000, 300000])  # 600000
    rec_far = _raw_release("far", recording_lengths=[300000, 360000])  # 660000
    winner = pick_duration_match(
        [rec_far, rec_near],
        program_anchor_ms=0,  # no track.length candidates → program anchor unused
        audio_anchor_ms=601000,
    )
    assert winner is not None
    assert winner["id"] == "near"


def test_pick_duration_match_empty_returns_none():
    from cdda2img.mb_lookup import pick_duration_match

    assert pick_duration_match([], program_anchor_ms=1, audio_anchor_ms=1) is None


def _dm_disc(
    album: str = "Match Album",
    artist: str = "Match Artist",
    n: int = 2,
    dur_frames: int = 15000,
) -> RBIDisc:
    entries = [
        RBITocEntry(
            track_number=i + 1,
            title=f"T{i + 1}",
            performer=artist,
            start_frame=i * dur_frames,
            duration_frames=dur_frames,
        )
        for i in range(n)
    ]
    return RBIDisc(album=album, artist=artist, tracks=entries)


def test_duration_match_lookup_offline_returns_none():
    from cdda2img.mb_lookup import duration_match_lookup

    disc = _dm_disc()
    with patch("cdda2img.config.is_no_network_active", return_value=True):
        assert duration_match_lookup(disc) is None


def test_duration_match_lookup_no_album_or_artist_returns_none():
    from cdda2img.mb_lookup import duration_match_lookup

    disc = _dm_disc(album="", artist="")
    with patch("cdda2img.config.is_no_network_active", return_value=False):
        assert duration_match_lookup(disc) is None


def test_duration_match_lookup_prefilters_by_track_count_and_picks():
    from cdda2img.mb_lookup import duration_match_lookup

    # 2 tracks x 15000 frames = 30000 frames = 400000 ms program anchor.
    disc = _dm_disc(n=2, dur_frames=15000)
    stub_match = DiscMeta(mb_release_id="match", track_count=2)
    stub_wrong_count = DiscMeta(mb_release_id="wrong", track_count=3)
    raw_match = _raw_release("match", track_lengths=[200000, 200000])  # 400000

    fetched: list[str] = []

    def fake_fetch(rid: str) -> dict | None:
        fetched.append(rid)
        return raw_match if rid == "match" else None

    with (
        patch("cdda2img.config.is_no_network_active", return_value=False),
        patch(
            "cdda2img.mb_lookup.search_releases",
            return_value=[stub_wrong_count, stub_match],
        ),
        patch("cdda2img.mb_lookup._fetch_release_raw", side_effect=fake_fetch),
    ):
        meta = duration_match_lookup(disc)
    assert meta is not None
    assert meta.mb_release_id == "match"
    assert fetched == ["match"]  # the wrong-track-count stub is never fetched


def test_duration_match_lookup_rejects_when_no_candidate_in_tolerance():
    from cdda2img.mb_lookup import duration_match_lookup

    disc = _dm_disc(n=2, dur_frames=15000)  # 400000 ms anchor
    stub = DiscMeta(mb_release_id="off", track_count=2)
    raw_off = _raw_release("off", track_lengths=[300000, 300000])  # 600000 — 200s off

    with (
        patch("cdda2img.config.is_no_network_active", return_value=False),
        patch("cdda2img.mb_lookup.search_releases", return_value=[stub]),
        patch("cdda2img.mb_lookup._fetch_release_raw", return_value=raw_off),
    ):
        assert duration_match_lookup(disc) is None
