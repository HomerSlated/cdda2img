"""
test_metadata_menu.py — Tests for metadata_menu pure-logic functions and discogs_lookup.

Interactive menu functions require TTY input and are not unit-tested here.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cdda2img.lookup_result import DiscMeta, TrackMeta
from cdda2img.metadata_menu import _clear_disc, _show_diff, _trunc, run_metadata_menu
from cdda2img.rbi_format import RBIDisc, RBITocEntry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _disc(album: str = "Album", artist: str = "Artist", tracks: int = 2) -> RBIDisc:
    entries = [
        RBITocEntry(
            i, f"Track {i}", artist, start_frame=(i - 1) * 10000, duration_frames=10000
        )
        for i in range(1, tracks + 1)
    ]
    return RBIDisc(album=album, artist=artist, tracks=entries)


def _meta(**kw) -> DiscMeta:
    return DiscMeta(**kw)


# ---------------------------------------------------------------------------
# _trunc
# ---------------------------------------------------------------------------


def test_trunc_short():
    assert _trunc("hello", 10) == "hello"


def test_trunc_exact():
    assert _trunc("hello", 5) == "hello"


def test_trunc_long():
    result = _trunc("hello world", 8)
    assert len(result) == 8
    assert result.endswith("…")


def test_trunc_none():
    assert _trunc(None, 10) == ""


# ---------------------------------------------------------------------------
# _show_diff (captured via output)
# ---------------------------------------------------------------------------


def test_show_diff_no_changes(capsys):
    disc = _disc("Same Album", "Same Artist")
    meta = _meta(album="Same Album", artist="Same Artist")
    _show_diff(meta, disc)
    out = capsys.readouterr().out
    assert "no fields" in out


def test_show_diff_album_change(capsys):
    disc = _disc(album="")
    meta = _meta(album="New Album", artist="Artist")
    _show_diff(meta, disc)
    out = capsys.readouterr().out
    assert "Album" in out
    assert "New Album" in out


def test_show_diff_unknown_artist_replaced(capsys):
    disc = _disc(artist="Unknown Artist")
    meta = _meta(artist="Real Artist")
    _show_diff(meta, disc)
    out = capsys.readouterr().out
    assert "Real Artist" in out


def test_show_diff_isrc_added(capsys):
    disc = _disc()
    meta = _meta(
        tracks=[
            TrackMeta(number=1, isrc="BEXX89300001"),
            TrackMeta(number=2, isrc="BEXX89300002"),
        ]
    )
    _show_diff(meta, disc)
    out = capsys.readouterr().out
    assert "BEXX89300001" in out
    assert "BEXX89300002" in out


def test_show_diff_existing_isrc_not_shown(capsys):
    disc = _disc()
    disc.tracks[0].isrc = "EXISTING0001"
    meta = _meta(tracks=[TrackMeta(number=1, isrc="NEWISRC0001")])
    _show_diff(meta, disc)
    out = capsys.readouterr().out
    # existing ISRC is not None, so no "+" line for it
    assert "NEWISRC0001" not in out


def test_show_diff_ignores_typographic_apostrophe(capsys):
    """A title differing only by an ASCII apostrophe vs U+2019 is not flagged."""
    disc = _disc(tracks=1)
    disc.tracks[0].title = "Gimme All Your Lovin'"
    # U+2019 (right single quote) built via chr() to avoid an ambiguous literal.
    meta = _meta(
        tracks=[TrackMeta(number=1, title="Gimme All Your Lovin" + chr(0x2019))]
    )
    _show_diff(meta, disc)
    out = capsys.readouterr().out
    assert "no fields would change" in out


# ---------------------------------------------------------------------------
# run_metadata_menu — non-TTY path
# ---------------------------------------------------------------------------


def test_run_metadata_menu_non_tty_returns_disc_unchanged():
    disc = _disc()
    with patch("sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = False
        result = run_metadata_menu(disc)
    assert result is disc


# ---------------------------------------------------------------------------
# _clear_disc
# ---------------------------------------------------------------------------


def test_clear_disc_wipes_metadata():
    from cdda2img.metadata_menu import _clear_disc

    disc = _disc("My Album", "My Artist")
    disc.tracks[0].isrc = "USTEST000001"
    cleared = _clear_disc(disc)

    assert cleared.album == ""
    assert cleared.artist == ""
    assert cleared.catalog is None
    assert cleared.disc_number == disc.disc_number
    assert cleared.disc_total == disc.disc_total
    assert len(cleared.tracks) == len(disc.tracks)
    for t in cleared.tracks:
        assert t.title == ""
        assert t.performer == ""
        assert t.isrc is None


def test_clear_disc_preserves_timing():
    from cdda2img.metadata_menu import _clear_disc

    disc = _disc()
    cleared = _clear_disc(disc)
    for orig, new in zip(disc.tracks, cleared.tracks):
        assert new.track_number == orig.track_number
        assert new.start_frame == orig.start_frame
        assert new.duration_frames == orig.duration_frames
        assert new.pregap_frames == orig.pregap_frames


def test_clear_disc_preserves_pre_emphasis():
    # BUG-5 regression: clearing metadata must not reset the physical pre_emphasis
    # flag (read from the subchannel, not guessed).
    disc = _disc()
    disc.pre_emphasis = True
    cleared = _clear_disc(disc)
    assert cleared.album == ""
    assert cleared.pre_emphasis is True


# ---------------------------------------------------------------------------
# discogs_lookup — unit tests
# ---------------------------------------------------------------------------


def test_discogs_is_available_false_without_token():
    from cdda2img import discogs_lookup

    with patch.dict("os.environ", {}, clear=True):
        assert not discogs_lookup.is_available()


def test_discogs_is_available_true_with_token():
    from cdda2img import discogs_lookup

    with patch.dict("os.environ", {"DISCOGS_TOKEN": "fake_token"}):
        assert discogs_lookup.is_available()


def test_discogs_search_returns_empty_without_token():
    from cdda2img import discogs_lookup

    with patch.dict("os.environ", {}, clear=True):
        results = discogs_lookup.search_releases("Technotronic")
    assert results == []


def test_discogs_parse_result_artist_album_split():
    from cdda2img.discogs_lookup import _parse_result

    r = SimpleNamespace(
        data={
            "title": "Technotronic - Pump Up the Jam",
            "year": 1989,
            "country": "Belgium",
            "label": ["Epic"],
            "catno": "466247 2",
            "barcode": ["5099747023521"],
        }
    )
    meta = _parse_result(r)
    assert meta.artist == "Technotronic"
    assert meta.album == "Pump Up the Jam"
    assert meta.release_date == "1989"
    assert meta.country == "Belgium"
    assert meta.label == "Epic"
    assert meta.catalog == "5099747023521"
    assert meta.source == "discogs"


def test_discogs_parse_result_no_separator():
    from cdda2img.discogs_lookup import _parse_result

    r = SimpleNamespace(data={"title": "Just An Album", "year": None})
    meta = _parse_result(r)
    assert meta.artist is None
    assert meta.album == "Just An Album"


@pytest.mark.parametrize(
    ("formats", "expected"),
    [
        (["CD", "Album"], "Album"),
        (["CD", "Single"], "Single"),
        (["CD", "Maxi-Single"], "Single"),
        (["Vinyl", "LP", "Album"], "Album"),
        (["CD", "EP"], "EP"),
        (["CD", "Compilation"], "Album"),  # MB-style: Compilation folds to Album
        (["File", "Mini-Album"], "Album"),
        (["Cassette", "Mixtape"], "Album"),
        (["CD"], None),  # bare medium, no descriptor → honest unknown
        ([], None),
        (None, None),  # missing field
        ("Album", None),  # malformed (not a list)
    ],
)
def test_discogs_primary_type_mapping(formats, expected):
    from cdda2img.discogs_lookup import _discogs_primary_type

    assert _discogs_primary_type(formats) == expected


def test_discogs_parse_result_sets_primary_type_from_format():
    from cdda2img.discogs_lookup import _parse_result

    r = SimpleNamespace(
        data={"title": "X - Y", "format": ["CD", "Single"], "year": 1990}
    )
    assert _parse_result(r).primary_type == "Single"


def test_discogs_parse_result_primary_type_none_without_format():
    from cdda2img.discogs_lookup import _parse_result

    r = SimpleNamespace(data={"title": "X - Y"})
    assert _parse_result(r).primary_type is None


def test_discogs_parse_full_release_prefers_scanned_barcode():
    """_parse_full_release picks 'Scanned' barcode over 'Printed' when both present."""
    from cdda2img.discogs_lookup import _parse_full_release

    r = SimpleNamespace(
        data={
            "id": 13837211,
            "title": "Eliminator",
            "artists": [{"name": "ZZ Top", "join": ""}],
            "year": 1983,
            "country": "US",
            "labels": [{"name": "Warner Bros. Records", "catno": "9 23774-2"}],
            "identifiers": [
                {
                    "type": "Barcode",
                    "value": "0 7599-23774-2",
                    "description": "Printed",
                },
                {"type": "Barcode", "value": "075992377423", "description": "Scanned"},
            ],
            "tracklist": [],
        }
    )
    meta = _parse_full_release(r)
    # 12-digit UPC-A is padded to 13-digit GTIN-13 (leading '0').
    assert meta.catalog == "0075992377423"


def test_discogs_parse_full_release_drops_printed_only_invalid_barcode():
    """When only 'Printed' format is available and it cannot normalise, catalog is None."""
    from cdda2img.discogs_lookup import _parse_full_release

    r = SimpleNamespace(
        data={
            "id": 99,
            "title": "Variant Pressing",
            "artists": [{"name": "ZZ Top", "join": ""}],
            "year": 1983,
            "country": "US",
            "labels": [{"name": "Warner Bros. Records", "catno": "9 23774-4"}],
            "identifiers": [
                {
                    "type": "Barcode",
                    "value": "0 7599-23774-4",  # 11 digits after stripping
                    "description": "Printed",
                },
            ],
            "tracklist": [],
        }
    )
    meta = _parse_full_release(r)
    assert meta.catalog is None


def test_discogs_parse_full_release_falls_back_to_first_barcode():
    """_parse_full_release falls back to the first barcode when no 'Scanned' entry."""
    from cdda2img.discogs_lookup import _parse_full_release

    r = SimpleNamespace(
        data={
            "id": 1,
            "title": "Album",
            "artists": [{"name": "Artist", "join": ""}],
            "year": 2000,
            "country": "DE",
            "labels": [],
            "identifiers": [
                {"type": "Barcode", "value": "4012345678901"},
            ],
            "tracklist": [],
        }
    )
    meta = _parse_full_release(r)
    assert meta.catalog == "4012345678901"


def test_normalize_barcode_eleven_digits_returns_none():
    """Printed barcode without check digit (11 digits) is rejected, not silently mangled."""
    from cdda2img.barcode import normalize_barcode

    assert normalize_barcode("0 7599-23774-2") is None


# ---------------------------------------------------------------------------
# _prepopulate_from_discogs — barcode hint fallback
# ---------------------------------------------------------------------------


def test_prepopulate_discogs_no_mcn_no_hints_returns_unchanged():
    """No MCN on disc and no hints → no Discogs query, disc unchanged."""
    from cdda2img.cdda2img import _prepopulate_from_discogs

    disc = _disc()
    with (
        patch("cdda2img.discogs_lookup.is_available", return_value=True),
        patch("cdda2img.discogs_lookup.search_by_barcode") as mock_search,
    ):
        result, _chosen, _hit = _prepopulate_from_discogs(disc, ui=None)
    assert result is disc
    assert result.catalog is None
    mock_search.assert_not_called()


def test_prepopulate_discogs_hint_fires_when_disc_has_no_mcn():
    """No MCN on disc, single hint, Discogs single-result with matching album → enriched."""
    from cdda2img.cdda2img import _prepopulate_from_discogs

    disc = _disc(album="Eliminator", artist="ZZ Top")
    assert disc.catalog is None
    hit = DiscMeta(
        album="Eliminator", artist="ZZ Top", catalog="0075992377423", tracks=[]
    )
    with (
        patch("cdda2img.discogs_lookup.is_available", return_value=True),
        patch(
            "cdda2img.discogs_lookup.search_by_barcode", return_value=[hit]
        ) as mock_search,
    ):
        result, _chosen, _hit = _prepopulate_from_discogs(
            disc, ui=None, barcode_hints=[("", "0075992377423")]
        )
    mock_search.assert_called_once_with("0075992377423")
    assert result.catalog == "0075992377423"


def test_prepopulate_discogs_substring_match_picks_correct_hint():
    """Real ZZ Top Eliminator case: 11-digit raw MCN substring-matches the right EAN.

    Disc Q-channel has a non-standard 11-digit value (printed GTIN-12 without
    check digit). MB returns 2 hints; the raw digits are a substring of the
    correct one, deductively selecting it without needing album matching.
    """
    from cdda2img.cdda2img import _prepopulate_from_discogs

    disc = _disc(album="Eliminator", artist="ZZ Top")
    disc.catalog = "0 7599-23774-2"  # 11 digits stripped = "07599237742"
    # Discogs returns 23 results for the correct EAN — ambiguous, no enrichment.
    ambiguous = [DiscMeta(album=f"Eliminator (Pressing {i})") for i in range(23)]
    with (
        patch("cdda2img.discogs_lookup.is_available", return_value=True),
        patch(
            "cdda2img.discogs_lookup.search_by_barcode", return_value=ambiguous
        ) as mock_search,
    ):
        result, _chosen, _hit = _prepopulate_from_discogs(
            disc,
            ui=None,
            barcode_hints=[("", "0075992377423"), ("", "0081227991159")],
        )
    # Substring match picks "0075992377423" (which contains "07599237742").
    # Discogs returns 23 → no enrichment, but disc.catalog is set deductively.
    mock_search.assert_called_once_with("0075992377423")
    assert result.catalog == "0075992377423"


def test_prepopulate_discogs_substring_skips_wrong_hint():
    """Substring match must not pick the wrong release.

    Hypothetical: raw digits are a substring of hint A but not hint B. Even if
    hint B comes first in the hint list, substring match overrides order.
    """
    from cdda2img.cdda2img import _prepopulate_from_discogs

    disc = _disc(album="X", artist="Y")
    disc.catalog = "9923774"  # only matches the Eliminator EAN, not the other
    with (
        patch("cdda2img.discogs_lookup.is_available", return_value=True),
        patch(
            "cdda2img.discogs_lookup.search_by_barcode", return_value=[]
        ) as mock_search,
    ):
        result, _chosen, _hit = _prepopulate_from_discogs(
            disc,
            ui=None,
            barcode_hints=[("", "0081227991159"), ("", "0075992377423")],
        )
    mock_search.assert_called_once_with("0075992377423")
    assert result.catalog == "0075992377423"


def test_prepopulate_discogs_fallback_to_first_hint_when_no_raw_mcn():
    """No raw barcode + multiple hints → first hint becomes the best-guess MCN.

    "I'd rather have the wrong MCN than none at all" — provenance over blank.
    The user can override via [c] in the menu.
    """
    from cdda2img.cdda2img import _prepopulate_from_discogs

    disc = _disc(album="Eliminator", artist="ZZ Top")
    assert disc.catalog is None
    ambiguous = [DiscMeta(album=f"Eliminator (Pressing {i})") for i in range(23)]
    with (
        patch("cdda2img.discogs_lookup.is_available", return_value=True),
        patch(
            "cdda2img.discogs_lookup.search_by_barcode", return_value=ambiguous
        ) as mock_search,
    ):
        result, _chosen, _hit = _prepopulate_from_discogs(
            disc,
            ui=None,
            barcode_hints=[("", "0075992377423"), ("", "0081227991159")],
        )
    # First hint chosen as best-guess; Discogs returns 23 (no enrichment).
    mock_search.assert_called_once_with("0075992377423")
    assert result.catalog == "0075992377423"


def test_prepopulate_discogs_enrichment_rejects_wrong_album():
    """Phase B enrichment guards against compilation false-matches via album validator.

    The MCN is still set from Phase A, but full-metadata merge is skipped when
    Discogs's single result has a clearly-different album title.
    """
    from cdda2img.cdda2img import _prepopulate_from_discogs

    disc = _disc(album="Eliminator", artist="ZZ Top")
    compilation = [DiscMeta(album="Afterburner / Eliminator", artist="ZZ Top")]
    with (
        patch("cdda2img.discogs_lookup.is_available", return_value=True),
        patch("cdda2img.discogs_lookup.search_by_barcode", return_value=compilation),
    ):
        result, _chosen, _hit = _prepopulate_from_discogs(
            disc, ui=None, barcode_hints=[("", "0081227991159")]
        )
    # MCN set from Phase A (only candidate is the hint); enrichment rejected.
    assert result.catalog == "0081227991159"
    assert result.album == "Eliminator"  # NOT overwritten by compilation


def test_prepopulate_discogs_valid_mcn_querys_once():
    """Valid 13-digit MCN already in disc.catalog → single Discogs query."""
    from cdda2img.cdda2img import _prepopulate_from_discogs

    disc = _disc()
    disc.catalog = "0075992377423"
    with (
        patch("cdda2img.discogs_lookup.is_available", return_value=True),
        patch(
            "cdda2img.discogs_lookup.search_by_barcode", return_value=[]
        ) as mock_search,
    ):
        _prepopulate_from_discogs(disc, ui=None, barcode_hints=[("", "0075992377423")])
    assert mock_search.call_count == 1


def test_albums_match_compilation_separator_asymmetry():
    """Album-match validator for Phase B enrichment guard."""
    from cdda2img.cdda2img import _albums_match

    # Exact / casefold equality
    assert _albums_match("Eliminator", "Eliminator")
    assert _albums_match("Eliminator", "eliminator")
    # Substring without separators — accept (covers reissues, deluxe editions)
    assert _albums_match("Eliminator", "Eliminator (Remastered)")
    assert _albums_match("Eliminator [Deluxe]", "Eliminator")
    # Compilation separator only on one side — reject
    assert not _albums_match("Eliminator", "Afterburner / Eliminator")
    assert not _albums_match("Eliminator", "Eliminator & Other Hits")
    assert not _albums_match("Eliminator", "Eliminator + Bonus Tracks")
    # Empty fields — nothing to compare, allow
    assert _albums_match(None, "Anything")
    assert _albums_match("", "Anything")
    assert _albums_match("Anything", None)


def test_pick_canonical_mcn_substring_match():
    """_pick_canonical_mcn deduces the correct EAN from raw printed barcode digits."""
    from cdda2img.cdda2img import _pick_canonical_mcn
    from cdda2img.rbi_format import RBIDisc

    disc = RBIDisc(album="X", artist="Y", catalog="0 7599-23774-2")
    candidates = ["0081227991159", "0075992377423"]
    assert _pick_canonical_mcn(disc, candidates) == "0075992377423"


def test_pick_canonical_mcn_fallback_to_first():
    """_pick_canonical_mcn falls back to first candidate when no substring match."""
    from cdda2img.cdda2img import _pick_canonical_mcn
    from cdda2img.rbi_format import RBIDisc

    disc = RBIDisc(album="X", artist="Y")  # no catalog
    candidates = ["0075992377423", "0081227991159"]
    assert _pick_canonical_mcn(disc, candidates) == "0075992377423"


def test_pick_canonical_mcn_returns_none_when_no_candidates():
    """_pick_canonical_mcn returns None when there's nothing to choose from."""
    from cdda2img.cdda2img import _pick_canonical_mcn
    from cdda2img.rbi_format import RBIDisc

    disc = RBIDisc(album="X", artist="Y")
    assert _pick_canonical_mcn(disc, []) is None


# ---------------------------------------------------------------------------
# acoustid_lookup — availability
# ---------------------------------------------------------------------------


def test_acoustid_not_available_without_key():
    from cdda2img import acoustid_lookup

    with patch.dict("os.environ", {}, clear=True):
        assert not acoustid_lookup.is_available()


def test_acoustid_reason_no_key():
    from cdda2img import acoustid_lookup

    with patch.dict("os.environ", {}, clear=True):
        reason = acoustid_lookup.unavailability_reason()
    assert "ACOUSTID_API_KEY" in reason


def test_acoustid_fingerprint_returns_empty_when_unavailable(tmp_path):
    from cdda2img import acoustid_lookup

    fake_wav = tmp_path / "test.wav"
    fake_wav.write_bytes(b"\x00" * 44)  # minimal non-empty file
    with patch.dict("os.environ", {}, clear=True):
        results = acoustid_lookup.fingerprint_and_lookup(fake_wav)
    assert results == []


# ---------------------------------------------------------------------------
# acoustid_lookup — fingerprint chain (mocked network)
# ---------------------------------------------------------------------------


def _mb_recording_response(
    recording_id, title, artist_name, release_id, album, date, country="US"
):
    """Build a minimal musicbrainzngs get_recording_by_id response dict."""
    return {
        "recording": {
            "id": recording_id,
            "title": title,
            "artist-credit": [{"artist": {"name": artist_name}, "joinphrase": ""}],
            "isrc-list": ["USTES1700001"],
            "release-list": [
                {
                    "id": release_id,
                    "title": album,
                    "date": date,
                    "country": country,
                    "release-group": {
                        "id": f"rg-{release_id}",
                        "first-release-date": date,
                    },
                }
            ],
        }
    }


def test_acoustid_fingerprint_chains_to_mb():
    """High-score match chains to MB and returns full release metadata."""
    from pathlib import Path

    from cdda2img import acoustid_lookup

    match_data = [(0.9, "rec-uuid-1", "Pump Up the Jam", "Technotronic")]
    mb_resp = _mb_recording_response(
        "rec-uuid-1",
        "Pump Up the Jam",
        "Technotronic",
        "rel-1",
        "Pump Up the Jam",
        "1989",
        "BE",
    )

    with (
        patch.dict("os.environ", {"ACOUSTID_API_KEY": "fake"}),
        patch("acoustid.match", return_value=iter(match_data)),
        patch("musicbrainzngs.get_recording_by_id", return_value=mb_resp),
    ):
        results = acoustid_lookup.fingerprint_and_lookup(Path("/fake/track.wav"))

    assert len(results) == 1
    r = results[0]
    assert r.album == "Pump Up the Jam"
    assert r.artist == "Technotronic"
    assert r.mb_release_id == "rel-1"
    assert r.country == "BE"
    assert r.release_date == "1989"
    assert r.tracks[0].isrc == "USTES1700001"
    assert r.source == "acoustid"


def test_acoustid_chain_drops_malformed_isrc():
    """BUG-4: AcoustID-sourced ISRCs pass through validate_isrc; a malformed value
    (year field not 2 digits) is dropped, not propagated to the TOC ISRC line."""
    from pathlib import Path

    from cdda2img import acoustid_lookup

    match_data = [(0.9, "rec-uuid-1", "Song", "Artist")]
    mb_resp = _mb_recording_response(
        "rec-uuid-1", "Song", "Artist", "rel-1", "Album", "1989"
    )
    mb_resp["recording"]["isrc-list"] = ["USTEST000001"]  # malformed: "T0" year

    with (
        patch.dict("os.environ", {"ACOUSTID_API_KEY": "fake"}),
        patch("acoustid.match", return_value=iter(match_data)),
        patch("musicbrainzngs.get_recording_by_id", return_value=mb_resp),
    ):
        results = acoustid_lookup.fingerprint_and_lookup(Path("/fake/track.wav"))

    assert results[0].tracks[0].isrc is None


def test_acoustid_chain_uses_only_valid_recording_includes():
    """Regression guard: every include passed to get_recording_by_id must be
    valid for the recording endpoint. "release-groups" is NOT — passing it
    raises musicbrainzngs.UsageError, which the broad except in _chain_to_mb
    swallows, silently collapsing every AcoustID match to the bare-track
    fallback (no album/country/Type). This test asserts the includes are a
    subset of the library's own VALID_INCLUDES, so re-adding an invalid one
    fails loudly here instead of in production.

    (Type genuinely cannot be cheaply sourced for AcoustID: primary-type lives
    on the release-group, which the recording endpoint will not embed, so the
    Type column stays "?" for AcoustID rows by design.)"""
    from pathlib import Path

    import musicbrainzngs  # type: ignore[import-untyped]

    from cdda2img import acoustid_lookup

    valid = set(musicbrainzngs.VALID_INCLUDES["recording"])
    match_data = [(0.9, "rec-uuid-1", "Title", "Artist")]
    mb_resp = _mb_recording_response(
        "rec-uuid-1", "Title", "Artist", "rel-1", "Album", "1989"
    )

    with (
        patch.dict("os.environ", {"ACOUSTID_API_KEY": "fake"}),
        patch("acoustid.match", return_value=iter(match_data)),
        patch("musicbrainzngs.get_recording_by_id", return_value=mb_resp) as gr,
    ):
        results = acoustid_lookup.fingerprint_and_lookup(Path("/fake/track.wav"))

    passed = set(gr.call_args.kwargs["includes"])
    assert passed <= valid, f"invalid recording includes: {passed - valid}"
    # a valid lookup carries full release metadata (did not hit the fallback)
    assert results[0].album == "Album"
    assert results[0].primary_type is None  # Type unavailable for AcoustID


@pytest.mark.parametrize(
    ("medium_list", "expected"),
    [
        ([{"track-count": "12"}], 12),  # single-medium album
        ([{"track-count": "2"}], 2),  # 2-track single
        ([{"track-count": "10"}, {"track-count": "8"}], 18),  # 2-disc set summed
        ([{"position": "1"}], None),  # medium with no count → "?"
        ([], None),  # no media
        ([{"track-count": "x"}], None),  # malformed count → "?"
    ],
)
def test_acoustid_release_track_count(medium_list, expected):
    from cdda2img.acoustid_lookup import _release_track_count

    assert _release_track_count({"medium-list": medium_list}) == expected


def test_acoustid_chain_populates_trk_from_media_include():
    """The "media" include (valid for recordings) supplies each release's
    per-medium track count → DiscMeta.track_count, the menu's Trk column and
    the album-vs-single cue. Zero extra requests — folded into the one call."""
    from pathlib import Path

    from cdda2img import acoustid_lookup

    match_data = [(0.9, "rec-uuid-1", "Title", "Artist")]
    mb_resp = _mb_recording_response(
        "rec-uuid-1", "Title", "Artist", "rel-1", "Album", "1989"
    )
    mb_resp["recording"]["release-list"][0]["medium-list"] = [{"track-count": "2"}]

    with (
        patch.dict("os.environ", {"ACOUSTID_API_KEY": "fake"}),
        patch("acoustid.match", return_value=iter(match_data)),
        patch("musicbrainzngs.get_recording_by_id", return_value=mb_resp) as gr,
    ):
        results = acoustid_lookup.fingerprint_and_lookup(Path("/fake/track.wav"))

    assert "media" in gr.call_args.kwargs["includes"]
    assert results[0].track_count == 2  # 2-track single → distinguishable


def test_acoustid_fingerprint_falls_back_on_mb_failure():
    """When the MB follow-up fails, a basic DiscMeta from AcoustID data is returned."""
    from pathlib import Path

    import musicbrainzngs

    from cdda2img import acoustid_lookup

    match_data = [(0.8, "rec-uuid-2", "Some Track", "Some Artist")]

    with (
        patch.dict("os.environ", {"ACOUSTID_API_KEY": "fake"}),
        patch("acoustid.match", return_value=iter(match_data)),
        patch(
            "musicbrainzngs.get_recording_by_id",
            side_effect=musicbrainzngs.NetworkError("timeout"),
        ),
    ):
        results = acoustid_lookup.fingerprint_and_lookup(Path("/fake/track.wav"))

    assert len(results) == 1
    assert results[0].artist == "Some Artist"
    assert results[0].album is None
    assert results[0].tracks[0].title == "Some Track"
    assert results[0].source == "acoustid"


def test_acoustid_fingerprint_filters_low_score():
    """Matches below the 0.5 confidence threshold are discarded before any MB call."""
    from pathlib import Path

    from cdda2img import acoustid_lookup

    match_data = [(0.3, "rec-uuid-low", "Weak Match", "Artist")]

    with (
        patch.dict("os.environ", {"ACOUSTID_API_KEY": "fake"}),
        patch("acoustid.match", return_value=iter(match_data)),
    ):
        results = acoustid_lookup.fingerprint_and_lookup(Path("/fake/track.wav"))

    assert results == []


def test_acoustid_fingerprint_deduplicates_releases():
    """The same release appearing under two recordings is returned only once."""
    from pathlib import Path

    from cdda2img import acoustid_lookup

    shared_release = {
        "id": "shared-rel",
        "title": "Shared Album",
        "date": "1989",
        "country": "US",
        "release-group": {"id": "rg-shared", "first-release-date": "1989"},
    }
    match_data = [
        (0.9, "rec-1", "Title A", "Artist"),
        (0.8, "rec-2", "Title B", "Artist"),
    ]
    resp_1 = {
        "recording": {
            "title": "Title A",
            "artist-credit": [],
            "isrc-list": [],
            "release-list": [shared_release],
        }
    }
    resp_2 = {
        "recording": {
            "title": "Title B",
            "artist-credit": [],
            "isrc-list": [],
            "release-list": [shared_release],
        }
    }

    def mb_side_effect(recording_id, **_kwargs):
        return resp_1 if recording_id == "rec-1" else resp_2

    with (
        patch.dict("os.environ", {"ACOUSTID_API_KEY": "fake"}),
        patch("acoustid.match", return_value=iter(match_data)),
        patch("musicbrainzngs.get_recording_by_id", side_effect=mb_side_effect),
    ):
        results = acoustid_lookup.fingerprint_and_lookup(Path("/fake/track.wav"))

    release_ids = [r.mb_release_id for r in results if r.mb_release_id]
    assert release_ids.count("shared-rel") == 1


# _acoustid_fingerprint — single-track result tagging (cp3c). The select/confirm/
# apply tail moved to menu_state.ResultsScreen(source="acoustid"); see
# test_menu_state.py::test_acoustid_select_confirms_before_fetch_full_when_partial.


def test_acoustid_fingerprint_tags_single_track_result_with_number():
    """track_number tags a single-track number=None result so title/ISRC merge
    into the right disc track in the apply tail."""
    from pathlib import Path

    from cdda2img.lookup_result import DiscMeta, TrackMeta
    from cdda2img.metadata_menu import _acoustid_fingerprint

    result_no_number = DiscMeta(
        artist="Technotronic",
        album="Pump Up the Jam",
        source="acoustid",
        tracks=[TrackMeta(title="Pump Up the Jam", performer="Technotronic")],
    )
    with patch(
        "cdda2img.acoustid_lookup.fingerprint_and_lookup",
        return_value=[result_no_number],
    ):
        out = _acoustid_fingerprint(Path("/fake/t.wav"), track_number=1)
    assert out[0].tracks[0].number == 1  # tagged for the apply-side merge


def test_acoustid_fingerprint_without_track_number_leaves_number_none():
    """Without track_number the single-track result keeps number=None, so the
    apply-side merge (matched by track number) leaves the disc title untouched."""
    from pathlib import Path

    from cdda2img.lookup_result import DiscMeta, TrackMeta
    from cdda2img.metadata_menu import _acoustid_fingerprint

    result_no_number = DiscMeta(
        artist="Technotronic",
        album="Pump Up the Jam",
        source="acoustid",
        tracks=[TrackMeta(title="Pump Up the Jam", performer="Technotronic")],
    )
    with patch(
        "cdda2img.acoustid_lookup.fingerprint_and_lookup",
        return_value=[result_no_number],
    ):
        out = _acoustid_fingerprint(Path("/fake/t.wav"))
    assert out[0].tracks[0].number is None


def test_acoustid_fingerprint_no_matches_returns_empty():
    from pathlib import Path

    from cdda2img.metadata_menu import _acoustid_fingerprint

    with patch("cdda2img.acoustid_lookup.fingerprint_and_lookup", return_value=[]):
        assert _acoustid_fingerprint(Path("/fake/t.wav"), track_number=1) == []


# _pcm_extract_track_wav


def test_pcm_extract_track_wav_writes_correct_frames(tmp_path):
    """Extraction writes the correct number of PCM frames for the target track."""
    import wave

    from cdda2img.metadata_menu import _BYTES_PER_FRAME, _pcm_extract_track_wav
    from cdda2img.rbi_format import RBIDisc, RBITocEntry

    track_frames = 1000  # short track
    disc = RBIDisc(
        album="",
        artist="",
        tracks=[
            RBITocEntry(1, "", "", start_frame=0, duration_frames=track_frames),
            RBITocEntry(
                2, "", "", start_frame=track_frames, duration_frames=track_frames
            ),
        ],
    )
    # Write two tracks of silence as raw PCM
    pcm_path = tmp_path / "disc.pcm"
    pcm_path.write_bytes(bytes(2 * track_frames * _BYTES_PER_FRAME))

    out_path = tmp_path / "track01.wav"
    result = _pcm_extract_track_wav(disc, pcm_path, 1, out_path)

    assert result == out_path
    with wave.open(str(out_path)) as w:
        # getnframes() = audio sample frames; 1000 CD frames x 588 samples/CD frame
        from cdda2img.rbi_format import CD_FRAMES_PER_SECOND, PCM_SAMPLE_RATE

        samples_per_cd_frame = PCM_SAMPLE_RATE // CD_FRAMES_PER_SECOND
        assert w.getnframes() == track_frames * samples_per_cd_frame


def test_pcm_extract_track_wav_returns_none_for_missing_track(tmp_path):
    """Returns None when the requested track number is not in the disc."""
    from cdda2img.metadata_menu import _pcm_extract_track_wav

    disc = RBIDisc(album="", artist="", tracks=[])
    pcm_path = tmp_path / "disc.pcm"
    pcm_path.write_bytes(b"\x00" * 100)

    result = _pcm_extract_track_wav(disc, pcm_path, 99, tmp_path / "t.wav")
    assert result is None


# ---------------------------------------------------------------------------
# The MB Fetch-path contract (earliest-first order + full-fetch-before-preview +
# rg threading) moved to the native screens in cp3a — see test_menu_state.py
# ::test_results_mb_select_fetches_full_before_preview_and_threads_rg and
# ::test_mb_search_s_pushes_results_sorted_earliest_first.
# ---------------------------------------------------------------------------
