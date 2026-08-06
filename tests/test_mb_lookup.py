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
    _disambiguate_by_isrcs,
    _find_disc_medium,
    _is_consistent,
    _merge_into_disc,
    _overwrite_disc,
    _parse_release,
    _parse_year,
    _plurality_release_group,
    _plurality_release_group_by_barcode,
    _resolve_via_isrc_tally,
    _select_release_lexicographic,
    compute_disc_id,
    disc_id_from_rbi,
    discogs_link_and_barcode,
    lookup_disc_id,
    prepopulate_from_mb,
)
from cdda2img.rbi_format import RBIDisc, RBITocEntry


@pytest.fixture(autouse=True)
def _clear_mb_caches():
    """Reset the process-lifetime disc-ID cache between tests (OPT-1).

    The cache is meant to persist for a process; in a test session it would
    otherwise leak a cached result into later tests that reuse the same disc-ID.
    """
    from cdda2img import mb_lookup

    mb_lookup._DISC_ID_CACHE.clear()
    yield
    mb_lookup._DISC_ID_CACHE.clear()


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
    """Lead-out is the last track's absolute end + 150 (== total_frames + 150 only
    because track 1 starts at frame 0 here; see the track1-head-offset test)."""
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


def test_disc_id_from_rbi_track1_head_offset_uses_last_track_end():
    """Track 1 not starting at PCM frame 0 must not shorten the lead-out.

    Regression for the ABBA Gold (1974) bug: this pressing has a 33-frame lead
    offset before track 1 (start_frame=33). The lead-out must be the LAST track's
    absolute end (start+pregap+duration+150), NOT disc.total_frames+150 — the
    latter omits that 33-frame head, yielding a wrong SHA-1, a spurious MB 404,
    and a silent fall-through to CDDB's unreliable DYEAR. The exact disc ID is
    the real MusicBrainz one, live-confirmed to return the 4 ABBA Gold releases.
    """
    # Real INDEX-01 offsets (absolute, +150) from the disc; contiguous pregap=0.
    offsets = [
        183,
        17545,
        35720,
        54045,
        70058,
        90633,
        109695,
        130995,
        153145,
        167283,
        182408,
        206883,
        225858,
        245570,
        267208,
        281883,
        299733,
        317733,
        335083,
    ]
    lead_out = 347358
    starts = [o - 150 for o in offsets]
    durs = [offsets[i + 1] - offsets[i] for i in range(len(offsets) - 1)]
    durs.append(lead_out - offsets[-1])
    disc = _make_disc(tracks=[(i + 1, starts[i], durs[i]) for i in range(len(offsets))])
    assert disc.tracks[0].start_frame == 33  # the head offset that broke the id
    assert disc_id_from_rbi(disc) == "xu6JNKjjqvue0dEfEKJ5d7Ffipw-"
    # The old total_frames+150 formula would have produced a different (wrong) id.
    assert disc_id_from_rbi(disc) != compute_disc_id(
        1, 19, offsets, disc.total_frames + 150
    )


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
        barcode="5099747023521",
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
        DiscMeta(album="A", mb_release_id="rid-A", barcode="0075992377423"),
        # Duplicate (rid-A, 0075992377423) — dropped
        DiscMeta(album="A2", mb_release_id="rid-A", barcode="0075992377423"),
        # Same barcode, different MBID — kept
        DiscMeta(album="B", mb_release_id="rid-B", barcode="0075992377423"),
        DiscMeta(album="C", mb_release_id="rid-C", barcode="4012345678901"),
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


# ---------------------------------------------------------------------------
# _plurality_release_group_by_barcode — RG-tie tiebreak (user-approved 2026-07-09)
# ---------------------------------------------------------------------------


def test_barcode_plurality_breaks_even_rg_split():
    """ABBA Gold: 4 releases split 2/2 across two RGs; the plurality barcode
    (held by both releases of one RG) pins that RG."""
    matches = [
        DiscMeta(album="Gold", mb_release_group_id="rg-gold", barcode="0731451700729"),
        DiscMeta(album="Gold", mb_release_group_id="rg-gold", barcode="0731451700729"),
        DiscMeta(album="Fvr", mb_release_group_id="rg-fvr", barcode="0731453308329"),
        DiscMeta(album="Fvr", mb_release_group_id="rg-fvr", barcode="0731453335523"),
    ]
    assert _plurality_release_group(matches) is None  # 2/2 RG tie
    assert _plurality_release_group_by_barcode(matches) == "rg-gold"


def test_barcode_plurality_none_when_barcode_spans_two_rgs():
    """A barcode plurality that does not resolve to a single RG pins nothing."""
    matches = [
        DiscMeta(album="A", mb_release_group_id="rg-x", barcode="0731451700729"),
        DiscMeta(album="B", mb_release_group_id="rg-y", barcode="0731451700729"),
        DiscMeta(album="C", mb_release_group_id="rg-z", barcode="4012345678901"),
    ]
    assert _plurality_release_group_by_barcode(matches) is None


def test_barcode_plurality_none_on_barcode_tie():
    matches = [
        DiscMeta(album="A", mb_release_group_id="rg-x", barcode="0731451700729"),
        DiscMeta(album="B", mb_release_group_id="rg-y", barcode="4012345678901"),
    ]
    assert _plurality_release_group_by_barcode(matches) is None


def test_barcode_plurality_needs_two_agreeing():
    """A barcode held by a single release is not a plurality (floor of 2)."""
    matches = [
        DiscMeta(album="A", mb_release_group_id="rg-x", barcode="0731451700729"),
        DiscMeta(album="B", mb_release_group_id="rg-y"),  # blank barcode
    ]
    assert _plurality_release_group_by_barcode(matches) is None


def test_prepopulate_multimatch_barcode_plurality_pins_pressing():
    """End-to-end: an even RG split with a barcode plurality merges the winning
    pressing (release_date set) rather than declining. Regression for ABBA Gold
    being tagged 1974 (CDDB) because MB applied nothing on the 2/2 RG tie."""
    # Blank baseline (as a no-CD-Text disc arrives), so fill-blank can set album.
    disc = RBIDisc(album="", artist="", tracks=[RBITocEntry(1, "", "", 0, 18000)])
    matches = [
        DiscMeta(
            album="Gold: Greatest Hits",
            artist="ABBA",
            mb_release_id="us92",
            mb_release_group_id="rg-gold",
            barcode="0731451700729",
            country="US",
            release_date="1992",
        ),
        DiscMeta(
            album="Gold: Greatest Hits",
            artist="ABBA",
            mb_release_id="gb92",
            mb_release_group_id="rg-gold",
            barcode="0731451700729",
            country="GB",
            release_date="1992",
        ),
        DiscMeta(
            album="Forever Gold",
            artist="ABBA",
            mb_release_id="gb96a",
            mb_release_group_id="rg-fvr",
            barcode="0731453308329",
            country="GB",
            release_date="1996",
        ),
        DiscMeta(
            album="Forever Gold",
            artist="ABBA",
            mb_release_id="gb96b",
            mb_release_group_id="rg-fvr",
            barcode="0731453335523",
            country="GB",
            release_date="1996",
        ),
    ]
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=matches):
        r = prepopulate_from_mb(disc, verbose=False, preferred_country=["GB", "US"])
    assert r.disc.album == "Gold: Greatest Hits"
    assert r.disc.release_date == "1992"  # was CDDB's 1974 before the fix
    assert r.disc.country == "GB"  # preferred_country broke the pressing tie
    assert r.selected_release_id == "gb92"
    assert r.release_selected_via == "preferred_country"


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
# Multi-match release selection (barcode plurality + lexicographic rung)
# ---------------------------------------------------------------------------

_ELIMINATOR_MCN = "0075992377423"  # ZZ Top - Eliminator, EU 1983 (valid GTIN-13)


def test_prepopulate_multimatch_barcode_plurality_picks_pressing():
    """A candidate carrying a service barcode outranks a barcodeless sibling on the
    barcode_plurality rung (the on-disc MCN plays no part, §1a), so the barcoded
    pressing's exact date + release id are merged."""
    disc = _make_disc(tracks=[(1, 0, 18000)])
    matches = [
        DiscMeta(
            album="E", mb_release_id="us", mb_release_group_id="rg-e"
        ),  # no barcode
        DiscMeta(
            album="E",
            barcode=_ELIMINATOR_MCN,
            release_date="1983-11-18",
            mb_release_id="de",
            mb_release_group_id="rg-e",
        ),
    ]
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=matches):
        r = prepopulate_from_mb(disc, verbose=False)
    assert r.disc.release_date == "1983-11-18"  # the barcoded pressing won
    assert r.disc.mb_release_id == "de"
    assert r.release_selected_via == "barcode_plurality"
    assert r.isrc_disambiguated is False


def test_prepopulate_multimatch_rung_pins_earliest_in_shared_barcode_rg():
    """§10.3 rung: two pressings share a barcode and a barcodeless sibling does not.
    The barcoded pair wins the plurality tier (so ``via`` is ``barcode_plurality``);
    within that pair the earliest release_date pins the winner. The on-disc MCN is
    not consulted (§1a) — selection rests on the candidates' own barcodes."""
    disc = _make_disc(tracks=[(1, 0, 18000)])
    matches = [
        DiscMeta(album="E", mb_release_id="us", mb_release_group_id="rg-e"),
        DiscMeta(
            album="E",
            barcode=_ELIMINATOR_MCN,
            release_date="1983-11-18",
            mb_release_id="de",
            mb_release_group_id="rg-e",
        ),
        DiscMeta(
            album="E",
            barcode=_ELIMINATOR_MCN,
            release_date="1983",
            mb_release_id="xe",
            mb_release_group_id="rg-e",
        ),
    ]
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=matches):
        r = prepopulate_from_mb(disc, verbose=False)
    assert r.disc.release_date == "1983"  # the pinned 'xe' pressing's own date
    assert r.disc.mb_release_group_id == "rg-e"
    assert r.disc.mb_release_id == "xe"  # earliest date within the barcoded pair
    # 'us' (no barcode) loses the plurality tier, so that is the deciding key.
    assert r.release_selected_via == "barcode_plurality"
    assert r.isrc_disambiguated is False


def test_prepopulate_multimatch_rung_pins_earliest_and_fills_shared_isrcs():
    """The Eliminator case: no disc MCN, no ISRC winner. The rung narrows to the
    plurality release-group and pins the earliest pressing; its per-track ISRCs
    (shared across the group) fill the disc."""
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
    assert r.disc.release_date == "1983-03-23"  # pinned 'us' (earliest) pressing
    assert r.disc.mb_release_group_id == "rg-e"
    assert r.disc.mb_release_id == "us"  # earliest date over the comp's 2008
    assert r.release_selected_via == "date"
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
        barcode="0075992377423",
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
    assert r.disc.barcode == "0075992377423"  # winner's barcode filled in
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
# R1 — ratio threshold scales with ISRC evidence (BEETS-4)
# ---------------------------------------------------------------------------


def _disc_n_isrcs(n: int) -> RBIDisc:
    """Build an RBIDisc with *n* tracks all carrying ISRCs."""
    return RBIDisc(
        album="Album",
        artist="Artist",
        tracks=[
            RBITocEntry(
                track_number=i,
                title=f"T{i}",
                performer="Artist",
                start_frame=(i - 1) * 10000,
                duration_frames=10000,
                isrc=f"AAAAA{i:07d}",
            )
            for i in range(1, n + 1)
        ],
    )


def _winner_meta(n_agree: int) -> DiscMeta:
    """DiscMeta that matches the first *n_agree* tracks of _disc_n_isrcs."""
    return DiscMeta(
        album="Album",
        mb_release_id="rid-win",
        tracks=[
            TrackMeta(number=i, isrc=f"AAAAA{i:07d}") for i in range(1, n_agree + 1)
        ],
    )


def _loser_meta() -> DiscMeta:
    return DiscMeta(album="Loser", mb_release_id="rid-lose", tracks=[])


def test_disambiguate_ratio_3_isrc_tracks_floor_still_applies():
    """n_isrc=3 → threshold=max(2, ceil(1.8))=2; score=2 is enough to win."""
    disc = _disc_n_isrcs(3)
    winner = _winner_meta(2)
    assert _disambiguate_by_isrcs([winner, _loser_meta()], disc) is winner


def test_disambiguate_ratio_10_isrc_tracks_threshold_6():
    """n_isrc=10 → threshold=max(2, ceil(6.0))=6; score=5 is no longer enough."""
    disc = _disc_n_isrcs(10)
    winner = _winner_meta(5)
    assert _disambiguate_by_isrcs([winner, _loser_meta()], disc) is None


def test_disambiguate_ratio_20_isrc_tracks_threshold_12():
    """n_isrc=20 → threshold=max(2, ceil(12.0))=12; score=11 is no longer enough."""
    disc = _disc_n_isrcs(20)
    winner = _winner_meta(11)
    assert _disambiguate_by_isrcs([winner, _loser_meta()], disc) is None


def test_disambiguate_ratio_zero_isrcs_floor_preserved():
    """n_isrc=0 → threshold=max(2,0)=2; score=0 stays below floor → None."""
    disc = _disc_n_isrcs(0)  # disc has no tracks, no ISRCs
    winner = _winner_meta(0)  # no ISRCs to match either
    assert _disambiguate_by_isrcs([winner, _loser_meta()], disc) is None


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


def test_collect_barcode_candidates_ignores_ondisc_mcn():
    """The on-disc MCN never seeds the candidate list (§1a) — only MB hints do
    when no service barcode is already set. Even a check-digit-valid MCN is
    excluded: the MCN is archival, not a lookup key."""
    from cdda2img.cdda2img import _collect_barcode_candidates

    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.catalog = "0075992377423"  # valid GTIN-13 MCN — must NOT appear
    candidates = _collect_barcode_candidates(disc, [("rid-A", "0081227991159")])
    assert candidates == ["0081227991159"]


def test_collect_barcode_candidates_ondisc_mcn_only_yields_empty():
    """A disc whose only identifier is a readable MCN gets no candidate (and so no
    Discogs query). The MCN-never-seeds-a-lookup rule, end to end."""
    from cdda2img.cdda2img import _collect_barcode_candidates

    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.catalog = "0075992377423"  # valid MCN, but archival only
    candidates = _collect_barcode_candidates(disc, [])
    assert candidates == []


def test_collect_barcode_candidates_disc_barcode_leads():
    """An already-set service barcode leads, then MB hints (deduped)."""
    from cdda2img.cdda2img import _collect_barcode_candidates

    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.catalog = "0075992377423"  # MCN ignored
    disc.barcode = "5099749994027"  # real service barcode — leads
    candidates = _collect_barcode_candidates(disc, [("rid-A", "0081227991159")])
    assert candidates == ["5099749994027", "0081227991159"]


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
        barcode="0075992377423",
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

    with patch("musicbrainzngs.get_releases_by_discid", side_effect=_fake):
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
    assert _is_consistent(DiscMeta(barcode="0093624877721"), disc) is True


def test_is_consistent_contradicting_mcn_not_rejected():
    """The on-disc MCN is archival-only and never gates consistency (§1a): a
    candidate whose barcode diverges from the MCN is NOT rejected on that basis.
    Only the per-track ISRC (exact, same-namespace) can veto."""
    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.catalog = "093624877721"  # American Idiot original
    assert _is_consistent(DiscMeta(barcode="0093624922315"), disc) is True  # reissue


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


def test_prepopulate_single_match_contradicting_mcn_merges():
    """The Tracy Chapman live bug in miniature: a single disc-ID match whose
    barcode diverges from the on-disc MCN is NO LONGER vetoed (§1a) — it merges.
    The MCN cannot reject a stronger identifier (the geometric disc-ID match)."""
    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.catalog = "093624877721"
    match = DiscMeta(album="Reissue", barcode="0093624922315", release_date="2015")
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[match]):
        r = prepopulate_from_mb(disc, verbose=False)
    assert r.disc.release_date == "2015"
    assert r.match_count == 1
    assert r.rejected_inconsistent == 0


def test_prepopulate_consistent_single_still_merges():
    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.catalog = "093624877721"  # printed GTIN-12 of the original
    match = DiscMeta(
        album="E",
        barcode="0093624877721",  # EAN-13 — fuzzy-matches
        release_date="1983-04-26",
        mb_release_id="orig",
    )
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[match]):
        r = prepopulate_from_mb(disc, verbose=False)
    assert r.disc.release_date == "1983-04-26"
    assert r.match_count == 1
    assert r.rejected_inconsistent == 0


def test_prepopulate_multimatch_resolves_via_release_group_plurality():
    """Three disc-ID matches, all consistent (the MCN no longer vetoes, §1a). A
    reissue in a different release-group is excluded from the winner by
    release-group *plurality* (not Unit-G), then the rung pins the earliest
    survivor of the plurality group."""
    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.catalog = "093624877721"
    s1 = DiscMeta(
        album="E",
        barcode="0093624877721",
        release_date="1983-04-26",
        mb_release_id="a",
        mb_release_group_id="rg-e",
    )
    s2 = DiscMeta(
        album="E",
        barcode="0093624877721",
        release_date="1983-11-18",
        mb_release_id="b",
        mb_release_group_id="rg-e",
    )
    bad = DiscMeta(
        album="Wrong",
        barcode="0093624922315",
        release_date="2015",
        mb_release_id="bad",
        mb_release_group_id="rg-x",
    )
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[s1, s2, bad]):
        r = prepopulate_from_mb(disc, verbose=False)
    assert r.rejected_inconsistent == 0  # MCN no longer vetoes 'bad'
    assert r.match_count == 3  # all three consistent now; rg-x excluded at selection
    assert r.disc.release_date == "1983-04-26"  # pinned earliest survivor ('a')
    assert r.disc.mb_release_group_id == "rg-e"
    assert r.disc.mb_release_id == "a"
    assert r.release_selected_via == "date"


def test_prepopulate_r4_tally_winner_kept_despite_mcn_divergence():
    """Zero disc-ID matches → R4 ISRC tally fires. The MCN is archival-only and no
    longer gates the R4 path either (§1a — supersedes the earlier R4 MCN gate):
    the tally winner is kept because its per-track ISRC agrees; the MCN divergence
    is irrelevant."""
    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.catalog = "093624877721"
    disc.tracks[0].isrc = "USRHD0709703"
    winner = DiscMeta(
        album="Reissue",
        barcode="0093624922315",  # diverges from the on-disc MCN — no longer gates
        release_date="2015",
        tracks=[TrackMeta(number=1, isrc="USRHD0709703")],  # ISRC agrees
    )
    with (
        patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[]),
        patch("cdda2img.mb_lookup._resolve_via_isrc_tally", return_value=winner),
    ):
        r = prepopulate_from_mb(disc, verbose=False)
    assert r.disc.release_date == "2015"  # winner kept; ISRC agrees, MCN ignored


# ---------------------------------------------------------------------------
# #3-a Barcode plurality excludes a blank-barcode TOC-collision variant
# ---------------------------------------------------------------------------


def test_prepop_multimatch_barcode_plurality_excludes_blank_barcode_variant():
    """The American Idiot fix, now via the SOUND mechanism: a same-RG variant with a
    BLANK barcode passes the (per-track-ISRC-only) consistency gate vacuously and
    would break album unanimity — but it loses the barcode_plurality rung to the two
    barcode-carrying originals, so the agreed album resolves to the original.

    This is the same outcome the old MCN-narrowing produced, but driven by the
    candidates' own service barcodes (same-namespace) rather than the cross-namespace
    on-disc MCN (§1a). The disc has no MCN at all here, to make that explicit.
    """
    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.album = ""  # blank so the agreed album can fill through (fill-blanks)
    orig1 = DiscMeta(
        album="American Idiot",
        artist="Green Day",
        barcode="0093624877721",
        release_date="2004-09-20",
        mb_release_id="o1",
        mb_release_group_id="rg-ai",
    )
    orig2 = DiscMeta(
        album="American Idiot",
        artist="Green Day",
        barcode="0093624877721",  # same normalised barcode as orig1 (plurality x2)
        release_date="2004-11-18",
        mb_release_id="o2",
        mb_release_group_id="rg-ai",
    )
    blank_variant = DiscMeta(
        album="American Idiot: The Ultimate American Idiot",
        artist="Green Day",
        barcode=None,  # blank → loses the plurality rung
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
    assert r.match_count == 3  # all three survive the consistency gate
    assert r.disc.album == "American Idiot"  # plurality excluded the variant
    # The TOC-collision variant loses the plurality tier; among the two barcoded
    # originals the earliest date pins the winner.
    assert r.disc.release_date == "2004-09-20"  # pinned 'o1' (earliest original)
    assert r.disc.mb_release_id == "o1"
    assert r.release_selected_via == "barcode_plurality"


def test_prepop_multimatch_no_barcodes_uses_full_set_by_rg_plurality():
    """When no candidate carries a barcode, the full consistent set feeds RG
    plurality, so the agreed album/year are taken over every candidate."""
    disc = _make_disc(tracks=[(1, 0, 18000)])
    disc.album = ""
    matches = [
        DiscMeta(
            album="American Idiot",
            barcode=None,
            release_date="2004",
            mb_release_id="a",
            mb_release_group_id="rg-ai",
        ),
        DiscMeta(
            album="American Idiot",
            barcode=None,
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


def test_duration_match_lookup_no_album_or_artist_returns_none():
    from cdda2img.mb_lookup import duration_match_lookup

    disc = _dm_disc(album="", artist="")
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
        patch("cdda2img.mb_lookup.search_releases", return_value=[stub]),
        patch("cdda2img.mb_lookup._fetch_release_raw", return_value=raw_off),
    ):
        assert duration_match_lookup(disc) is None


# ---------------------------------------------------------------------------
# OPT-1 — in-process disc-ID lookup cache
# ---------------------------------------------------------------------------


def test_lookup_disc_id_caches_success(monkeypatch: pytest.MonkeyPatch) -> None:
    # A successful disc-ID lookup is cached process-wide; the second call (banner
    # then finalize) is served without a network round-trip.
    from cdda2img import mb_lookup

    mb_lookup._DISC_ID_CACHE.clear()
    calls = {"n": 0}

    def _fake(*_a, **_k):
        calls["n"] += 1
        return {"disc": {"release-list": [{"id": "rel-1"}]}}

    monkeypatch.setattr(mb_lookup, "disc_id_from_rbi", lambda _d: "DISCID-OK")
    monkeypatch.setattr(mb_lookup, "_setup_useragent", lambda: None)
    monkeypatch.setattr(
        mb_lookup, "_parse_release", lambda _r, _disc_id=None: DiscMeta(album="A")
    )
    monkeypatch.setattr(mb_lookup.musicbrainzngs, "get_releases_by_discid", _fake)

    disc = RBIDisc(album="x", artist="y")
    r1 = mb_lookup.lookup_disc_id(disc)
    r2 = mb_lookup.lookup_disc_id(disc)
    assert calls["n"] == 1  # second call served from cache
    assert r1 == r2 == [DiscMeta(album="A")]


def test_lookup_disc_id_caches_404_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    # A 404 ("not in MB") is a definitive answer — the empty list is cached.
    import musicbrainzngs

    from cdda2img import mb_lookup

    mb_lookup._DISC_ID_CACHE.clear()
    calls = {"n": 0}

    def _raise_404(*_a, **_k):
        calls["n"] += 1
        raise musicbrainzngs.ResponseError(cause=type("C", (), {"code": 404})())

    monkeypatch.setattr(mb_lookup, "disc_id_from_rbi", lambda _d: "DISCID-404")
    monkeypatch.setattr(mb_lookup, "_setup_useragent", lambda: None)
    monkeypatch.setattr(mb_lookup.musicbrainzngs, "get_releases_by_discid", _raise_404)

    disc = RBIDisc(album="x", artist="y")
    assert mb_lookup.lookup_disc_id(disc) == []
    assert mb_lookup.lookup_disc_id(disc) == []
    assert calls["n"] == 1  # 404 cached, not re-queried


def test_lookup_disc_id_does_not_cache_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A transient network error must NOT be cached — the next call retries.
    import musicbrainzngs

    from cdda2img import mb_lookup

    mb_lookup._DISC_ID_CACHE.clear()
    calls = {"n": 0}

    def _raise_net(*_a, **_k):
        calls["n"] += 1
        raise musicbrainzngs.NetworkError("boom")

    monkeypatch.setattr(mb_lookup, "disc_id_from_rbi", lambda _d: "DISCID-NET")
    monkeypatch.setattr(mb_lookup, "_setup_useragent", lambda: None)
    monkeypatch.setattr(mb_lookup.musicbrainzngs, "get_releases_by_discid", _raise_net)

    disc = RBIDisc(album="x", artist="y")
    assert mb_lookup.lookup_disc_id(disc) == []
    assert mb_lookup.lookup_disc_id(disc) == []
    assert calls["n"] == 2  # transient error not cached


# ---------------------------------------------------------------------------
# §10.3 — lexicographic release-selection rung
# ---------------------------------------------------------------------------


def _cand(mbid, *, catalog=None, country=None, date=None, rg="rg"):
    """Terse DiscMeta candidate for rung tests (all share one release-group)."""
    return DiscMeta(
        album="A",
        artist="B",
        barcode=catalog,
        country=country,
        release_date=date,
        mb_release_id=mbid,
        mb_release_group_id=rg,
    )


def test_rung_empty_returns_none():
    disc = RBIDisc(album="A", artist="B")
    sel = _select_release_lexicographic([], disc, [])
    assert sel.winner is None
    assert sel.via is None
    assert sel.tied_after is None
    assert sel.menu_candidates == ()


def test_rung_key1_barcode_plurality_wins():
    disc = RBIDisc(album="A", artist="B")
    cands = [
        _cand("a", catalog="0042284229821"),  # shared barcode (x2)
        _cand("b", catalog="0042284229821"),
        _cand("c", catalog="0099999999996"),  # unique barcode
    ]
    sel = _select_release_lexicographic(cands, disc, [])
    assert sel.winner is not None
    assert sel.winner.mb_release_id in {"a", "b"}  # plurality tier
    assert sel.via == "barcode_plurality"
    # N4: barcode_plurality narrowed to the 2-strong tier; `mbid` then had to
    # arbitrate between those two. The menu would show exactly that tier.
    assert sel.tied_after == "barcode_plurality:2"
    assert {c.mb_release_id for c in sel.menu_candidates} == {"a", "b"}


def test_rung_key2_preferred_country_within_barcode_tier():
    # All share a barcode (plurality tie); preferred_country breaks it.
    disc = RBIDisc(album="A", artist="B")
    cands = [
        _cand("us", catalog="0042284229821", country="US"),
        _cand("gb", catalog="0042284229821", country="GB"),
        _cand("de", catalog="0042284229821", country="DE"),
    ]
    sel = _select_release_lexicographic(cands, disc, ["GB", "XE", "US"])
    assert sel.winner is not None
    assert sel.winner.mb_release_id == "gb"
    assert sel.via == "preferred_country"
    # Country genuinely determined it here (GB is unique), so no tie remained.
    assert sel.tied_after == "preferred_country:1"
    # ...but all three still share the barcode, so the MENU shows all three:
    # a preference rung must not remove a row a human is looking at (N5).
    assert len(sel.menu_candidates) == 3


def test_rung_preferred_country_only_within_barcode_tier():
    # Endorsed consequence (§9.5): a uniquely-barcoded preferred-country pressing
    # still ranks BELOW the common-barcode tier — plurality outranks country.
    disc = RBIDisc(album="A", artist="B")
    cands = [
        _cand("common1", catalog="0042284229821", country="US"),
        _cand("common2", catalog="0042284229821", country="US"),
        _cand("rareGB", catalog="0099999999996", country="GB"),  # unique barcode
    ]
    sel = _select_release_lexicographic(cands, disc, ["GB"])
    assert sel.winner is not None
    assert sel.winner.mb_release_id in {"common1", "common2"}  # not rareGB


def test_rung_key3_earliest_date_wins():
    disc = RBIDisc(album="A", artist="B")
    cands = [
        _cand("late", date="1987-06-01"),
        _cand("early", date="1987-03-09"),
        _cand("nodate"),
    ]
    sel = _select_release_lexicographic(cands, disc, [])
    assert sel.winner is not None
    assert sel.winner.mb_release_id == "early"
    assert sel.via == "date"
    assert sel.tied_after == "date:1"  # earliest date was unique


def test_rung_key4_mbid_terminal_deterministic():
    # Nothing distinguishes the candidates but the release-id; the result is
    # deterministic (lexicographically smallest mbid) and via == "mbid".
    disc = RBIDisc(album="A", artist="B")
    cands = [_cand("zzz"), _cand("aaa"), _cand("mmm")]
    sel = _select_release_lexicographic(cands, disc, [])
    assert sel.winner is not None
    assert sel.winner.mb_release_id == "aaa"
    assert sel.via == "mbid"
    # N4: nothing above the terminal key varied, so nothing narrowed and all
    # three were arbitrated alphabetically. `none:3` says so out loud.
    assert sel.tied_after == "none:3"
    assert len(sel.menu_candidates) == 3


def test_rung_reports_the_tie_the_mbid_sort_actually_broke():
    """N4 regression, built to the reference disc's measured shape.

    Seven album-consistent candidates, all sharing one barcode and none carrying
    a date. ``preferred_country`` drops two (FR, AU) and leaves FIVE tied; the
    terminal ``mbid`` sort then picks the alphabetically-first among those five.

    ``release_selected_via`` reads ``preferred_country`` — naming a rung that
    eliminated but did not select — and reads *identically* whether that rung
    determined the winner or merely narrowed to a tie. That ambiguity is the
    whole defect: on the real disc it hid the fact that the pressing was chosen
    by alphabetical accident, and the choice was wrong in seven containers.
    ``tied_after`` distinguishes the two cases; ``via`` alone never could.
    """
    disc = RBIDisc(album="A", artist="B")
    bc = "0075596077422"
    cands = [
        _cand("b63ffa5b", catalog=bc, country="XE"),
        _cand("8e5e097d", catalog=bc, country="XE"),
        _cand("e6676f25", catalog=bc, country="XE"),
        _cand("65e67d39", catalog=bc, country="XE"),
        _cand("7531d07c", catalog=bc, country="XE"),
        _cand("928588a5", catalog=bc, country="FR"),
        # The AU pressing is the only one carrying a date — a live detail, and a
        # trap. `date` therefore VARIES across all seven while narrowing nothing,
        # because country eliminates this row one rung earlier and the five
        # survivors are all dateless. An implementation that asks "does this key
        # vary across the candidate set" (which is how `via` is defined) credits
        # `date` with the narrowing and reports `date:5`. Measured against live
        # MusicBrainz, that is wrong; the real last discriminator is country.
        _cand("e9b905e6", catalog=bc, country="AU", date="1988-04"),
    ]
    sel = _select_release_lexicographic(cands, disc, ["GB", "XE", "US"])

    assert sel.winner is not None
    assert sel.winner.mb_release_id == "65e67d39"  # '6' < '7' < '8' < 'b' < 'e'
    assert sel.via == "preferred_country"  # the historical, ambiguous answer
    assert sel.tied_after == "preferred_country:5"  # the honest one

    # N5: the menu must offer all SEVEN — country is a preference, and the two it
    # drops are exactly the rows carrying distinguishing information a user could
    # check against the sleeve.
    assert len(sel.menu_candidates) == 7
    assert {"928588a5", "e9b905e6"} <= {c.mb_release_id for c in sel.menu_candidates}


def test_rung_tied_after_separates_determined_from_arbitrary():
    """The two outcomes `via` conflates, side by side: same rung named, opposite
    meanings. Only the `:n` tells them apart."""
    disc = RBIDisc(album="A", artist="B")
    bc = "0075596077422"

    determined = _select_release_lexicographic(
        [_cand("z", catalog=bc, country="GB"), _cand("a", catalog=bc, country="DE")],
        disc,
        ["GB"],
    )
    # Same rung varies, but this time it leaves TWO candidates tied at the top —
    # the shape that made `via` misleading. A third, non-preferred candidate is
    # what makes the country key vary at all; without it `via` would read `mbid`
    # and would not be ambiguous.
    arbitrary = _select_release_lexicographic(
        [
            _cand("z", catalog=bc, country="GB"),
            _cand("a", catalog=bc, country="GB"),
            _cand("m", catalog=bc, country="DE"),
        ],
        disc,
        ["GB"],
    )

    assert determined.winner is not None
    assert arbitrary.winner is not None
    assert determined.winner.mb_release_id == "z"  # country picked it
    assert arbitrary.winner.mb_release_id == "a"  # the alphabet picked it
    assert determined.via == arbitrary.via == "preferred_country"  # indistinguishable
    assert determined.tied_after == "preferred_country:1"
    assert arbitrary.tied_after == "preferred_country:2"


def test_prepopulate_rung_preferred_country_threads_through():
    """End-to-end (advisor #1): a Config.preferred_country value, threaded through
    prepopulate_from_mb → _prepop_multimatch → the rung, actually changes the
    pinned release among shared-barcode pressings and records the via. Guards the
    whole threading chain — a dropped kwarg would still type-check but fail here.
    """
    disc = _make_disc(tracks=[(1, 0, 18000)])  # no on-disc MCN
    bc = "0042284229821"
    matches = [
        DiscMeta(
            album="A",
            barcode=bc,
            country="US",
            mb_release_id="us",
            mb_release_group_id="rg",
        ),
        DiscMeta(
            album="A",
            barcode=bc,
            country="GB",
            mb_release_id="gb",
            mb_release_group_id="rg",
        ),
        DiscMeta(
            album="A",
            barcode=bc,
            country="DE",
            mb_release_id="de",
            mb_release_group_id="rg",
        ),
    ]
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=matches):
        r = prepopulate_from_mb(disc, verbose=False, preferred_country=["GB"])
    assert r.disc.mb_release_id == "gb"  # GB preference broke the barcode tie
    assert r.disc.country == "GB"
    assert r.release_selected_via == "preferred_country"
    # Same candidates, no preference -> falls through to the terminal mbid key.
    disc2 = _make_disc(tracks=[(1, 0, 18000)])
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=matches):
        r2 = prepopulate_from_mb(disc2, verbose=False, preferred_country=[])
    assert r2.release_selected_via == "mbid"
    assert r2.disc.mb_release_id == "de"  # lexicographically smallest id


# ---------------------------------------------------------------------------
# §10.3.1 discogs_link_and_barcode — MB->Discogs url-rel + barcode (one fetch)
# ---------------------------------------------------------------------------


def _rel_response(barcode, url_rels):
    return {"release": {"barcode": barcode, "url-relation-list": url_rels}}


def test_discogs_link_and_barcode_extracts_id_and_barcode():
    resp = _rel_response(
        "042284229821",
        [
            {"type": "amazon asin", "target": "https://www.amazon.com/x"},
            {"type": "discogs", "target": "https://www.discogs.com/release/1198146"},
        ],
    )
    with patch("musicbrainzngs.get_release_by_id", return_value=resp):
        discogs_id, barcode = discogs_link_and_barcode("some-mbid")
    assert discogs_id == 1198146
    assert barcode == "0042284229821"


def test_discogs_link_and_barcode_release_url_with_slug():
    # Discogs release URLs may carry a trailing slug; the id is still extracted.
    resp = _rel_response(
        "042284229821",
        [{"type": "discogs", "target": "https://www.discogs.com/release/1198146-U2"}],
    )
    with patch("musicbrainzngs.get_release_by_id", return_value=resp):
        discogs_id, _ = discogs_link_and_barcode("some-mbid")
    assert discogs_id == 1198146


def test_discogs_link_and_barcode_no_discogs_release_link():
    # A discogs *master* URL has no /release/<id> -> no id (but barcode stands).
    resp = _rel_response(
        "042284229821",
        [{"type": "discogs", "target": "https://www.discogs.com/master/12345"}],
    )
    with patch("musicbrainzngs.get_release_by_id", return_value=resp):
        discogs_id, barcode = discogs_link_and_barcode("some-mbid")
    assert discogs_id is None
    assert barcode == "0042284229821"


def test_discogs_link_and_barcode_no_barcode():
    resp = _rel_response(
        None,
        [{"type": "discogs", "target": "https://www.discogs.com/release/1198146"}],
    )
    with patch("musicbrainzngs.get_release_by_id", return_value=resp):
        discogs_id, barcode = discogs_link_and_barcode("some-mbid")
    assert discogs_id == 1198146
    assert barcode is None


def test_discogs_link_and_barcode_network_error_returns_none_none():
    import musicbrainzngs

    err = musicbrainzngs.NetworkError("timeout")
    with patch("musicbrainzngs.get_release_by_id", side_effect=err):
        assert discogs_link_and_barcode("some-mbid") == (None, None)
