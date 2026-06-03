"""
test_original_release.py — original-release lookup unit tests.

All MB calls are mocked; no network access.
"""

from __future__ import annotations

from unittest.mock import patch

from cdda2img.lookup_result import DiscMeta, TrackMeta
from cdda2img.original_release import (
    _deny_match,
    _normalise_title,
    _parse_year,
    _qualified_fuzzy_candidates,
    find_original_release,
    find_original_release_fuzzy,
    populate_original_release,
)
from cdda2img.rbi_format import RBIDisc


def _disc(
    album: str = "Album",
    artist: str = "Artist",
    rg_id: str | None = "rg-uuid-1",
    release_date: str | None = "2009",
) -> RBIDisc:
    return RBIDisc(
        album=album,
        artist=artist,
        mb_release_group_id=rg_id,
        release_date=release_date,
    )


def _rg(
    title: str = "Album",
    first_date: str = "1983",
    secondary: list[str] | None = None,
) -> dict:
    return {
        "title": title,
        "first-release-date": first_date,
        "secondary-type-list": secondary or [],
    }


# ---------------------------------------------------------------------------
# _parse_year
# ---------------------------------------------------------------------------


def test_parse_year_full_date():
    assert _parse_year("1983-06-15") == 1983


def test_parse_year_year_only():
    assert _parse_year("1983") == 1983


def test_parse_year_none_and_empty():
    assert _parse_year(None) is None
    assert _parse_year("") is None


def test_parse_year_garbage():
    assert _parse_year("not-a-date") is None


# ---------------------------------------------------------------------------
# find_original_release — primary MB release-group path
# ---------------------------------------------------------------------------


# Helper: short-circuit the fuzzy fallback so we test the primary path in
# isolation. Tests that exercise the fuzzy path mock search_releases directly.
def _no_fuzzy():
    return patch(
        "cdda2img.original_release.find_original_release_fuzzy",
        return_value=(False, None, None),
    )


def test_returns_false_without_rg_id():
    disc = _disc(rg_id=None)
    with _no_fuzzy():
        assert find_original_release(disc) == (False, None, None)


def test_returns_earliest_release_from_rg():
    disc = _disc(album="Eliminator (Remastered)", release_date="2008")
    with patch(
        "cdda2img.original_release._fetch_release_group",
        return_value=_rg(title="Eliminator", first_date="1983"),
    ):
        found, title, year = find_original_release(disc)
    assert found is True
    assert title == "Eliminator"
    assert year == 1983


def test_rejects_compilation_secondary_type():
    disc = _disc()
    with (
        patch(
            "cdda2img.original_release._fetch_release_group",
            return_value=_rg(secondary=["Compilation"]),
        ),
        _no_fuzzy(),
    ):
        assert find_original_release(disc) == (False, None, None)


def test_rejects_live_secondary_type():
    disc = _disc()
    with (
        patch(
            "cdda2img.original_release._fetch_release_group",
            return_value=_rg(secondary=["Live"]),
        ),
        _no_fuzzy(),
    ):
        assert find_original_release(disc) == (False, None, None)


def test_returns_trio_even_when_disc_is_the_original():
    """When disc IS the original, find_original_release still returns the trio.

    The display layer is responsible for rendering "This release (year)"
    vs "Original: title (year)" based on title+year match.
    """
    disc = _disc(album="Eliminator", release_date="1983")
    with patch(
        "cdda2img.original_release._fetch_release_group",
        return_value=_rg(title="Eliminator", first_date="1983"),
    ):
        found, title, year = find_original_release(disc)
    assert found is True
    assert title == "Eliminator"
    assert year == 1983


def test_rejects_when_first_release_date_is_empty():
    disc = _disc()
    with (
        patch(
            "cdda2img.original_release._fetch_release_group",
            return_value=_rg(first_date=""),
        ),
        _no_fuzzy(),
    ):
        assert find_original_release(disc) == (False, None, None)


def test_rejects_when_mb_fetch_fails():
    disc = _disc()
    with (
        patch("cdda2img.original_release._fetch_release_group", return_value=None),
        _no_fuzzy(),
    ):
        assert find_original_release(disc) == (False, None, None)


# ---------------------------------------------------------------------------
# populate_original_release
# ---------------------------------------------------------------------------


def test_populate_assigns_fields_on_match():
    disc = _disc(album="Album (Deluxe)", release_date="2010")
    with patch(
        "cdda2img.original_release._fetch_release_group",
        return_value=_rg(title="Album", first_date="1985"),
    ):
        populate_original_release(disc)
    assert disc.original_release_found is True
    assert disc.original_release_title == "Album"
    assert disc.original_release_year == 1985


def test_populate_preserves_manual_override():
    """If the user set original_release_found via the menu, the lookup is skipped."""
    disc = _disc()
    disc.original_release_found = True
    disc.original_release_title = "User Override Title"
    disc.original_release_year = 1970
    with patch("cdda2img.original_release._fetch_release_group") as mock_fetch:
        populate_original_release(disc)
        mock_fetch.assert_not_called()
    assert disc.original_release_title == "User Override Title"
    assert disc.original_release_year == 1970


def test_populate_leaves_disc_unchanged_on_no_match():
    disc = _disc()
    with (
        patch("cdda2img.original_release._fetch_release_group", return_value=None),
        _no_fuzzy(),
    ):
        populate_original_release(disc)
    assert disc.original_release_found is False
    assert disc.original_release_title is None
    assert disc.original_release_year is None


# ---------------------------------------------------------------------------
# PROV writer (_add_release_provenance) — load-bearing for the refactor
# ---------------------------------------------------------------------------


def test_prov_writes_low_dr_yes():
    from cdda2img.cdda2img import _add_release_provenance

    disc = RBIDisc(album="A", artist="B", low_dynamic_range=True)
    prov: dict[str, str] = {}
    _add_release_provenance(prov, disc)
    assert prov["low_dynamic_range"] == "YES"


def test_prov_writes_low_dr_no():
    from cdda2img.cdda2img import _add_release_provenance

    disc = RBIDisc(album="A", artist="B", low_dynamic_range=False)
    prov: dict[str, str] = {}
    _add_release_provenance(prov, disc)
    assert prov["low_dynamic_range"] == "NO"


def test_prov_omits_low_dr_when_none():
    from cdda2img.cdda2img import _add_release_provenance

    disc = RBIDisc(album="A", artist="B")  # low_dynamic_range defaults to None
    prov: dict[str, str] = {}
    _add_release_provenance(prov, disc)
    assert "low_dynamic_range" not in prov


def test_prov_writes_original_release_trio_when_found():
    from cdda2img.cdda2img import _add_release_provenance

    disc = RBIDisc(
        album="Album (Deluxe Edition)",
        artist="Artist",
        original_release_found=True,
        original_release_title="Album",
        original_release_year=1983,
    )
    prov: dict[str, str] = {}
    _add_release_provenance(prov, disc)
    assert prov["original_release_found"] == "YES"
    assert prov["original_release_title"] == "Album"
    assert prov["original_release_year"] == "1983"


def test_prov_omits_original_release_when_not_found():
    from cdda2img.cdda2img import _add_release_provenance

    disc = RBIDisc(album="A", artist="B")  # original_release_found defaults to False
    prov: dict[str, str] = {}
    _add_release_provenance(prov, disc)
    assert "original_release_found" not in prov
    assert "original_release_title" not in prov
    assert "original_release_year" not in prov


# ---------------------------------------------------------------------------
# Title-fuzz fallback (Phase 3b)
# ---------------------------------------------------------------------------


def test_normalise_strips_remastered_suffix():
    assert _normalise_title("Eliminator (Remastered)") == "eliminator"


def test_normalise_strips_year_qualified_remaster():
    assert _normalise_title("OK Computer (1997 Remaster)") == "ok computer"


def test_normalise_strips_deluxe_anniversary():
    assert _normalise_title("Album (20th Anniversary Deluxe Edition)") == "album"


def test_normalise_strips_leading_the():
    assert _normalise_title("The Beatles") == "beatles"


def test_normalise_strips_disc_tag():
    assert _normalise_title("White Album (Disc 1)") == "white album"


def test_deny_roman_numeral_asymmetric():
    assert _deny_match("Led Zeppelin", "Led Zeppelin II") is not None


def test_deny_roman_numeral_different():
    assert _deny_match("Symphony III", "Symphony IV") is not None


def test_deny_arabic_suffix_different():
    assert _deny_match("Greatest Hits 1", "Greatest Hits 2") is not None


def test_deny_volume_different():
    assert _deny_match("Greatest Hits Vol. 1", "Greatest Hits Vol. 2") is not None


def test_deny_volume_spelled_matches_numeral():
    # "Vol. Two" and "Volume 2" should be equivalent → no deny
    assert _deny_match("Songs Vol. Two", "Songs Volume 2") is None


def test_deny_re_recording_blocks():
    assert _deny_match("Red (Taylor's Version)", "Red") is not None


def test_deny_live_vs_studio_blocks():
    assert _deny_match("Album", "Album (Live at Wembley)") is not None


def test_deny_clean_pair_passes():
    assert _deny_match("Eliminator (Remastered)", "Eliminator") is None


# These exercise the live fuzzy scorer _qualified_fuzzy_candidates (it returns
# every qualifying DiscMeta, sorted earliest-year-then-score). Migrated from the
# removed _best_fuzzy_match duplicate (Unit P2).


def _fuzz_metas(specs: list[tuple[str, int]]) -> list[DiscMeta]:
    return [
        DiscMeta(album=title, original_release_date=str(year)) for title, year in specs
    ]


def test_fuzzy_matches_remaster_to_original():
    hits = _qualified_fuzzy_candidates(
        "Eliminator (2008 Remaster)",
        _fuzz_metas([("Eliminator", 1983), ("Eliminator (2008 Remaster)", 2008)]),
    )
    assert hits  # at least one qualifies
    assert hits[0].album == "Eliminator"  # earliest year wins
    assert _parse_year(hits[0].original_release_date) == 1983


def test_fuzzy_rejects_sequel_via_denylist():
    # "Led Zeppelin" should NOT match "Led Zeppelin II" even though token
    # overlap is high — deny-list catches it.
    hits = _qualified_fuzzy_candidates(
        "Led Zeppelin", _fuzz_metas([("Led Zeppelin II", 1969), ("Led Zeppelin", 1969)])
    )
    assert [h.album for h in hits] == ["Led Zeppelin"]


def test_fuzzy_returns_none_below_cutoff():
    hits = _qualified_fuzzy_candidates(
        "OK Computer", _fuzz_metas([("OK Human", 2019), ("Burn The Witch", 2016)])
    )
    assert hits == []


def test_fuzzy_prefers_earliest_year():
    hits = _qualified_fuzzy_candidates(
        "Album", _fuzz_metas([("Album", 2009), ("Album", 1985), ("Album", 1995)])
    )
    assert hits
    assert _parse_year(hits[0].original_release_date) == 1985


def test_find_original_release_uses_fuzzy_when_no_rg():
    """When disc has no mb_release_group_id, fall through to fuzzy search."""
    from cdda2img.lookup_result import DiscMeta

    disc = _disc(album="Eliminator (2008 Remaster)", rg_id=None)
    fake_search = [
        DiscMeta(
            album="Eliminator",
            mb_release_group_id="rg-x",
            release_date="1983",
            original_release_date="1983",
        )
    ]
    with patch("cdda2img.mb_lookup.search_releases", return_value=fake_search):
        found, title, year = find_original_release(disc)
    assert found is True
    assert title == "Eliminator"
    assert year == 1983


def test_find_original_release_fuzzy_empty_search_returns_false():
    disc = _disc(rg_id=None, album="Unknown Album")
    with patch("cdda2img.mb_lookup.search_releases", return_value=[]):
        assert find_original_release_fuzzy(disc) == (False, None, None)


def test_find_original_release_fuzzy_missing_artist_or_album():
    disc = RBIDisc(album="", artist="", mb_release_group_id=None)
    assert find_original_release_fuzzy(disc) == (False, None, None)


# ---------------------------------------------------------------------------
# PROV writer (continued)
# ---------------------------------------------------------------------------


def test_prov_writes_mb_release_group_id():
    from cdda2img.cdda2img import _add_release_provenance

    disc = RBIDisc(album="A", artist="B", mb_release_group_id="rg-uuid-1")
    prov: dict[str, str] = {}
    _add_release_provenance(prov, disc)
    assert prov["mb_release_group_id"] == "rg-uuid-1"


# ---------------------------------------------------------------------------
# R3 — Track-set / runtime verifier
# ---------------------------------------------------------------------------


def _disc_with_tracks(
    track_specs: list[tuple[int, int, str, str | None]],
    *,
    album: str = "Album",
    artist: str = "Artist",
    rg_id: str | None = "rg-uuid-1",
    release_id: str | None = "rel-uuid-1",
) -> RBIDisc:
    """Build an RBIDisc with tracks: (number, duration_frames, title, isrc)."""
    from cdda2img.rbi_format import RBITocEntry

    return RBIDisc(
        album=album,
        artist=artist,
        mb_release_group_id=rg_id,
        mb_release_id=release_id,
        tracks=[
            RBITocEntry(
                track_number=n,
                title=title,
                performer=artist,
                start_frame=0,
                duration_frames=dur,
                isrc=isrc,
            )
            for n, dur, title, isrc in track_specs
        ],
    )


def _meta_with_tracks(
    track_specs: list[tuple[int, int | None, str | None, str | None]],
    *,
    album: str = "Album",
    mb_release_id: str = "rel-meta-1",
) -> DiscMeta:
    """Build a DiscMeta with tracks: (number, duration_ms, title, isrc)."""
    return DiscMeta(
        album=album,
        mb_release_id=mb_release_id,
        tracks=[
            TrackMeta(number=n, duration_ms=dur, title=title, isrc=isrc)
            for n, dur, title, isrc in track_specs
        ],
    )


def test_r3_verifier_accepts_empty_tracklists() -> None:
    """Both sides empty → no evidence to reject → True (innocent until guilty)."""
    from cdda2img.original_release import _verify_release_matches_disc

    disc = _disc()  # empty tracks
    meta = _meta_with_tracks([])
    assert _verify_release_matches_disc(meta, disc) is True


def test_r3_verifier_rejects_track_count_mismatch() -> None:
    """Track-count is a hard gate when both sides have tracks."""
    from cdda2img.original_release import _verify_release_matches_disc

    disc = _disc_with_tracks([(1, 18000, "T1", None), (2, 18000, "T2", None)])
    meta = _meta_with_tracks([
        (1, 240000, "T1", None),
        (2, 240000, "T2", None),
        (3, 200000, "T3", None),
    ])
    assert _verify_release_matches_disc(meta, disc) is False


def test_r3_verifier_accepts_track_count_match_no_other_data() -> None:
    """Matching track counts + no durations / ISRCs / titles → pass."""
    from cdda2img.original_release import _verify_release_matches_disc

    disc = _disc_with_tracks([(1, 0, "", None), (2, 0, "", None)])
    meta = _meta_with_tracks([(1, None, None, None), (2, None, None, None)])
    assert _verify_release_matches_disc(meta, disc) is True


def test_r3_verifier_accepts_durations_within_tolerance() -> None:
    """Sum-of-durations within ±2 s → pass."""
    from cdda2img.original_release import _verify_release_matches_disc

    # 75 frames = 1 s. disc total = 600 frames = 8 s. meta total = 9000 ms = 9 s.
    disc = _disc_with_tracks([(1, 300, "Same", None), (2, 300, "Same", None)])
    meta = _meta_with_tracks([(1, 5000, "Same", None), (2, 4000, "Same", None)])
    # diff = 1 s, well within 2 s tolerance
    assert _verify_release_matches_disc(meta, disc) is True


def test_r3_verifier_rejects_durations_outside_tolerance() -> None:
    """Sum-of-durations beyond ±2 s → reject (positive evidence)."""
    from cdda2img.original_release import _verify_release_matches_disc

    # disc total = 8 s. meta total = 20 s. diff = 12 s, well past 2 s.
    disc = _disc_with_tracks([(1, 300, "Same", None), (2, 300, "Same", None)])
    meta = _meta_with_tracks([(1, 10000, "Same", None), (2, 10000, "Same", None)])
    assert _verify_release_matches_disc(meta, disc) is False


def test_r3_verifier_rejects_isrc_total_disagreement() -> None:
    """When both sides have ≥2 ISRCs and zero agree → reject."""
    from cdda2img.original_release import _verify_release_matches_disc

    disc = _disc_with_tracks([
        (1, 18000, "T1", "USAA10100001"),
        (2, 18000, "T2", "USAA10100002"),
    ])
    meta = _meta_with_tracks([
        (1, None, None, "USBB10100001"),
        (2, None, None, "USBB10100002"),
    ])
    assert _verify_release_matches_disc(meta, disc) is False


def test_r3_verifier_accepts_isrc_partial_agreement() -> None:
    """≥1 ISRC agrees (even partial) → not zero score → pass that gate."""
    from cdda2img.original_release import _verify_release_matches_disc

    disc = _disc_with_tracks([
        (1, 18000, "T1", "USAA10100001"),
        (2, 18000, "T2", "USAA10100002"),
    ])
    meta = _meta_with_tracks([
        (1, None, None, "USAA10100001"),
        (2, None, None, "USBB10100002"),
    ])
    assert _verify_release_matches_disc(meta, disc) is True


def test_r3_verifier_rejects_title_fuzz_below_cutoff() -> None:
    """Aggregate token_set_ratio < 80 across paired titles → reject."""
    from cdda2img.original_release import _verify_release_matches_disc

    disc = _disc_with_tracks([
        (1, 18000, "Sharp Dressed Man", None),
        (2, 18000, "Gimme All Your Lovin'", None),
    ])
    meta = _meta_with_tracks([
        (1, None, "Bohemian Rhapsody", None),
        (2, None, "Wish You Were Here", None),
    ])
    assert _verify_release_matches_disc(meta, disc) is False


def test_r3_verifier_accepts_title_fuzz_above_cutoff() -> None:
    """Aggregate token_set_ratio ≥ 80 → pass."""
    from cdda2img.original_release import _verify_release_matches_disc

    disc = _disc_with_tracks([
        (1, 18000, "Sharp Dressed Man", None),
        (2, 18000, "Gimme All Your Lovin'", None),
    ])
    meta = _meta_with_tracks([
        (1, None, "Sharp Dressed Man (Remaster)", None),
        (2, None, "Gimme All Your Lovin' (Remaster)", None),
    ])
    assert _verify_release_matches_disc(meta, disc) is True


def test_r3_rg_path_passes_when_disc_has_no_release_id() -> None:
    """No mb_release_id → can't verify → no evidence to reject → pass."""
    from cdda2img.original_release import _verify_rg_path_for_disc

    disc = RBIDisc(
        album="A", artist="B", mb_release_group_id="rg-1", mb_release_id=None
    )
    assert _verify_rg_path_for_disc(disc) is True


def test_r3_rg_path_passes_when_lookup_release_fails() -> None:
    """Network failure during release fetch ≠ evidence of mismatch → pass."""
    from cdda2img.original_release import _verify_rg_path_for_disc

    disc = RBIDisc(
        album="A", artist="B", mb_release_group_id="rg-1", mb_release_id="rel-1"
    )
    with patch("cdda2img.mb_lookup.lookup_release", return_value=None):
        assert _verify_rg_path_for_disc(disc) is True


def test_r3_rg_path_rejects_on_track_count_mismatch() -> None:
    """A successful fetch with a hard mismatch → reject (R3 fires)."""
    from cdda2img.original_release import _verify_rg_path_for_disc

    disc = _disc_with_tracks(
        [(1, 300, "T1", None), (2, 300, "T2", None)], release_id="rel-1"
    )
    meta = _meta_with_tracks(
        [(1, None, None, None)],  # 1 track in meta vs 2 in disc
        mb_release_id="rel-1",
    )
    with patch("cdda2img.mb_lookup.lookup_release", return_value=meta):
        assert _verify_rg_path_for_disc(disc) is False


def test_r3_fuzzy_loop_drops_unverified_candidates() -> None:
    """If the earliest candidate fails verify, fall through to the next."""
    from cdda2img.lookup_result import DiscMeta
    from cdda2img.original_release import find_original_release_fuzzy

    disc = _disc_with_tracks(
        [(1, 300, "T1", None), (2, 300, "T2", None)],
        album="Album",
        artist="Artist",
        rg_id=None,
        release_id=None,
    )
    # Two candidates from MB search, both at year=1983 (sorted by year asc).
    # First one's full release has 5 tracks → fails verify; second one matches.
    bad = DiscMeta(
        album="Album",
        mb_release_id="rel-bad",
        mb_release_group_id="rg-bad",
        original_release_date="1983",
    )
    good = DiscMeta(
        album="Album",
        mb_release_id="rel-good",
        mb_release_group_id="rg-good",
        original_release_date="1985",
    )

    bad_full = _meta_with_tracks(
        [(i + 1, None, None, None) for i in range(5)], mb_release_id="rel-bad"
    )
    good_full = _meta_with_tracks(
        [(1, None, None, None), (2, None, None, None)], mb_release_id="rel-good"
    )

    def fake_lookup_release(rid: str, disc_number: int | None = None):
        return {"rel-bad": bad_full, "rel-good": good_full}.get(rid)

    with (
        patch("cdda2img.mb_lookup.search_releases", return_value=[bad, good]),
        patch("cdda2img.mb_lookup.lookup_release", side_effect=fake_lookup_release),
    ):
        found, title, year = find_original_release_fuzzy(disc)

    assert found is True
    assert title == "Album"
    assert year == 1985  # 'bad' rejected, 'good' (1985) accepted


# ---------------------------------------------------------------------------
# R14 — Pre-emphasis year upper-bound
# ---------------------------------------------------------------------------


def test_r14_rg_path_rejects_year_after_1986_when_pre_emphasis():
    """Disc with PRE_EMPHASIS but RG year > 1986 → reject and fall through."""
    disc = _disc(rg_id="rg-1")
    disc.pre_emphasis = True
    with (
        patch(
            "cdda2img.original_release._fetch_release_group",
            return_value=_rg(first_date="1995"),
        ),
        _no_fuzzy(),
    ):
        found, _title, _year = find_original_release(disc)
    assert found is False


def test_r14_rg_path_accepts_pre_1987_year_when_pre_emphasis():
    """Disc with PRE_EMPHASIS and RG year ≤ 1986 → accept."""
    disc = _disc(rg_id="rg-1", release_date="1985")
    disc.pre_emphasis = True
    with (
        patch(
            "cdda2img.original_release._fetch_release_group",
            return_value=_rg(first_date="1985"),
        ),
        _no_fuzzy(),
    ):
        found, _title, year = find_original_release(disc)
    assert found is True
    assert year == 1985


def test_r14_rg_path_year_unchanged_when_no_pre_emphasis():
    """Disc without PRE_EMPHASIS → R14 cap doesn't fire even on late year."""
    disc = _disc(rg_id="rg-1")
    disc.pre_emphasis = False
    with (
        patch(
            "cdda2img.original_release._fetch_release_group",
            return_value=_rg(first_date="2009"),
        ),
        _no_fuzzy(),
    ):
        found, _title, year = find_original_release(disc)
    assert found is True
    assert year == 2009


def test_r14_fuzzy_path_filters_late_candidates():
    """Fuzzy candidates with year > 1986 are skipped when pre_emphasis=True."""
    from cdda2img.lookup_result import DiscMeta

    disc = _disc_with_tracks(
        [(1, 300, "T1", None)],
        album="Album",
        artist="Artist",
        rg_id=None,
        release_id=None,
    )
    disc.pre_emphasis = True
    # 2 candidates: 2009 (would be the earliest qualifying without R14)
    # and 1986 (under the cap).
    late = DiscMeta(
        album="Album",
        mb_release_id="rel-late",
        mb_release_group_id="rg-late",
        original_release_date="2009",
    )
    early = DiscMeta(
        album="Album",
        mb_release_id="rel-early",
        mb_release_group_id="rg-early",
        original_release_date="1986",
    )

    early_full = _meta_with_tracks([(1, None, None, None)], mb_release_id="rel-early")

    def fake_lookup_release(rid: str, disc_number: int | None = None):
        return early_full if rid == "rel-early" else None

    with (
        patch("cdda2img.mb_lookup.search_releases", return_value=[late, early]),
        patch("cdda2img.mb_lookup.lookup_release", side_effect=fake_lookup_release),
    ):
        found, _title, year = find_original_release_fuzzy(disc)
    assert found is True
    assert year == 1986  # late (2009) skipped by R14


# ---------------------------------------------------------------------------
# R14 — toc_parser PRE_EMPHASIS detection
# ---------------------------------------------------------------------------


def test_r14_toc_parser_detects_pre_emphasis():
    """parse_toc sets pre_emphasis=True when any track has PRE_EMPHASIS."""
    from cdda2img.toc_parser import parse_toc

    toc_text = b"""CD_DA

// Track 1
TRACK AUDIO
NO PRE_EMPHASIS
FILE "data.bin" 0 04:00:00

// Track 2
TRACK AUDIO
PRE_EMPHASIS
FILE "data.bin" 04:00:00 04:00:00
"""
    parsed = parse_toc(toc_text)
    assert parsed.pre_emphasis is True


def test_r14_toc_parser_no_pre_emphasis_when_all_negate():
    """All NO PRE_EMPHASIS tracks → pre_emphasis=False."""
    from cdda2img.toc_parser import parse_toc

    toc_text = b"""CD_DA

// Track 1
TRACK AUDIO
NO PRE_EMPHASIS
FILE "data.bin" 0 04:00:00

// Track 2
TRACK AUDIO
NO PRE_EMPHASIS
FILE "data.bin" 04:00:00 04:00:00
"""
    parsed = parse_toc(toc_text)
    assert parsed.pre_emphasis is False


def test_r14_toc_parser_no_tracks_returns_none():
    """parse_toc with no tracks returns pre_emphasis=None (unknown)."""
    from cdda2img.toc_parser import parse_toc

    parsed = parse_toc(b"CD_DA\n")
    assert parsed.pre_emphasis is None


# ---------------------------------------------------------------------------
# R14 — PROV emission for pre_emphasis
# ---------------------------------------------------------------------------


def test_r14_prov_emits_yes_when_pre_emphasis_true():
    from cdda2img.cdda2img import _add_release_provenance

    disc = RBIDisc(album="A", artist="B")
    disc.pre_emphasis = True
    prov: dict[str, str] = {}
    _add_release_provenance(prov, disc)
    assert prov["pre_emphasis"] == "YES"


def test_r14_prov_emits_no_when_pre_emphasis_false():
    from cdda2img.cdda2img import _add_release_provenance

    disc = RBIDisc(album="A", artist="B")
    disc.pre_emphasis = False
    prov: dict[str, str] = {}
    _add_release_provenance(prov, disc)
    assert prov["pre_emphasis"] == "NO"


def test_r14_prov_omits_when_pre_emphasis_none():
    """None = not captured; the PROV key is omitted entirely."""
    from cdda2img.cdda2img import _add_release_provenance

    disc = RBIDisc(album="A", artist="B")  # pre_emphasis defaults to None
    prov: dict[str, str] = {}
    _add_release_provenance(prov, disc)
    assert "pre_emphasis" not in prov


# ---------------------------------------------------------------------------
# R6 — pre-menu AcoustID corroboration (guard only)
# ---------------------------------------------------------------------------


def test_r6_no_op_when_acoustid_unavailable(tmp_path):
    """When AcoustID is not available, the helper is a no-op (no PROV key)."""
    from cdda2img.cdda2img import _r6_acoustid_corroborate

    disc = _disc_with_tracks([(1, 75, "T1", None)])
    pcm = tmp_path / "disc.pcm"
    pcm.write_bytes(bytes(75 * 2352))
    prov: dict[str, str] = {}
    with patch("cdda2img.acoustid_lookup.is_available", return_value=False):
        result = _r6_acoustid_corroborate(disc, pcm, prov, ui=None)
    assert result is disc
    assert "acoustid_corroborates" not in prov


def test_r6_yes_when_acoustid_agrees_with_prepop(tmp_path):
    """AcoustID best hit's mb_release_id matches disc.mb_release_id → YES."""
    from cdda2img.cdda2img import _r6_acoustid_corroborate
    from cdda2img.lookup_result import DiscMeta

    disc = _disc_with_tracks([(1, 75, "T1", None)])
    disc.mb_release_id = "rid-match"
    pcm = tmp_path / "disc.pcm"
    pcm.write_bytes(bytes(75 * 2352))
    prov: dict[str, str] = {}
    with (
        patch("cdda2img.acoustid_lookup.is_available", return_value=True),
        patch(
            "cdda2img.acoustid_lookup.fingerprint_and_lookup",
            return_value=[DiscMeta(mb_release_id="rid-match")],
        ),
    ):
        _r6_acoustid_corroborate(disc, pcm, prov, ui=None)
    assert prov.get("acoustid_corroborates") == "YES"


def test_r6_no_when_acoustid_disagrees_with_prepop(tmp_path):
    """AcoustID best hit disagrees with disc.mb_release_id → NO."""
    from cdda2img.cdda2img import _r6_acoustid_corroborate
    from cdda2img.lookup_result import DiscMeta

    disc = _disc_with_tracks([(1, 75, "T1", None)])
    disc.mb_release_id = "rid-prepop"
    pcm = tmp_path / "disc.pcm"
    pcm.write_bytes(bytes(75 * 2352))
    prov: dict[str, str] = {}
    with (
        patch("cdda2img.acoustid_lookup.is_available", return_value=True),
        patch(
            "cdda2img.acoustid_lookup.fingerprint_and_lookup",
            return_value=[DiscMeta(mb_release_id="rid-different")],
        ),
    ):
        _r6_acoustid_corroborate(disc, pcm, prov, ui=None)
    assert prov.get("acoustid_corroborates") == "NO"


def test_r6_merge_never_sets_pressing_mb_release_id(tmp_path):
    """AcoustID must corroborate the album but NEVER claim a pressing.

    Fingerprints identify *recordings*, which are shared across every pressing
    in a release-group — so AcoustID can confirm ``mb_release_group_id`` but can
    never identify ``mb_release_id``. Agreed-facts (a multi-match disc-ID result)
    deliberately leaves ``mb_release_id=None`` while setting the RG; before this
    fix, R6 overwrote it with a fingerprint-chosen in-RG release, which both
    fabricated a pressing the disc-ID never confirmed and broke the R3
    original-release verify's precondition (ZZ Top *Eliminator*: AcoustID picked
    ``20f8ccf4``, an in-RG pressing with rounded track lengths 20 s short of the
    disc, and the verify then rejected the correct RG).
    """
    from cdda2img.cdda2img import _r6_acoustid_corroborate
    from cdda2img.lookup_result import DiscMeta

    disc = _disc_with_tracks([(1, 75, "T1", None)])
    disc.mb_release_id = None  # agreed-facts leaves the pressing undetermined
    disc.mb_release_group_id = "rg-from-disc-id"  # but the album IS identified
    pcm = tmp_path / "disc.pcm"
    pcm.write_bytes(bytes(75 * 2352))
    prov: dict[str, str] = {}
    acoustid_hit = DiscMeta(
        mb_release_id="rid-acoustid-guess",  # an in-RG pressing AcoustID guessed
        mb_release_group_id="rg-from-disc-id",
    )
    with (
        patch("cdda2img.acoustid_lookup.is_available", return_value=True),
        patch(
            "cdda2img.acoustid_lookup.fingerprint_and_lookup",
            return_value=[acoustid_hit],
        ),
    ):
        result = _r6_acoustid_corroborate(disc, pcm, prov, ui=None)
    # The invariant: album corroborated, pressing never claimed.
    assert result.mb_release_id is None
    assert result.mb_release_group_id == "rg-from-disc-id"
    assert prov.get("acoustid_corroborates") == "YES"


# ---------------------------------------------------------------------------
# R9 — Inter-service CDDB↔MB disagreement detection
# ---------------------------------------------------------------------------


def test_r9_no_disagreement_when_titles_match():
    """Identical titles → no disagreement key in PROV."""
    from cdda2img.cdda2img import _emit_r9_disagreement

    prov: dict[str, str] = {}
    _emit_r9_disagreement(prov, "Eliminator", "ZZ Top", "Eliminator", "ZZ Top")
    assert "disagreement_cddb_mb" not in prov


def test_r9_no_disagreement_when_only_suffix_differs():
    """'Eliminator' vs 'Eliminator (Remastered)' → no disagreement (allow-list)."""
    from cdda2img.cdda2img import _emit_r9_disagreement

    prov: dict[str, str] = {}
    _emit_r9_disagreement(
        prov, "Eliminator", "ZZ Top", "Eliminator (Remastered)", "ZZ Top"
    )
    assert "disagreement_cddb_mb" not in prov


def test_r9_disagreement_on_album_only():
    """Different albums but same artist → 'album'."""
    from cdda2img.cdda2img import _emit_r9_disagreement

    prov: dict[str, str] = {}
    _emit_r9_disagreement(prov, "Different Album", "ZZ Top", "Eliminator", "ZZ Top")
    assert prov.get("disagreement_cddb_mb") == "album"


def test_r9_disagreement_on_both():
    """Both album and artist differ → 'album,artist'."""
    from cdda2img.cdda2img import _emit_r9_disagreement

    prov: dict[str, str] = {}
    _emit_r9_disagreement(prov, "Album A", "Artist X", "Album B", "Artist Y")
    assert prov.get("disagreement_cddb_mb") == "album,artist"


def test_r9_skips_when_pre_mb_artist_is_unknown_sentinel():
    """'Unknown Artist' is the raw default, not a CDDB answer → don't compare."""
    from cdda2img.cdda2img import _emit_r9_disagreement

    prov: dict[str, str] = {}
    _emit_r9_disagreement(prov, None, "Unknown Artist", "Album", "Real Artist")
    assert "disagreement_cddb_mb" not in prov


def test_r9_normalises_nfc_and_casefold():
    """Unicode + case-only differences are not disagreement."""
    from cdda2img.cdda2img import _emit_r9_disagreement

    prov: dict[str, str] = {}
    _emit_r9_disagreement(prov, "Eliminator", "ZZ TOP", "eliminator", "zz top")
    assert "disagreement_cddb_mb" not in prov


# ---------------------------------------------------------------------------
# R11 — Discogs master-release corroboration
# ---------------------------------------------------------------------------


def test_r11_corroborated_when_years_agree():
    """Discogs master year matches MB original year → corroborated."""
    from cdda2img.cdda2img import _r11_corroborate_with_discogs_master

    disc = RBIDisc(album="A", artist="B")
    disc.original_release_found = True
    disc.original_release_year = 1983
    disc.discogs_release_id = 42
    prov: dict[str, str] = {}
    with patch("cdda2img.discogs_lookup.lookup_master_year", return_value=1983):
        _r11_corroborate_with_discogs_master(disc, prov)
    assert prov.get("original_release_corroborated") == "discogs,mb"
    assert disc.original_release_year == 1983


def test_r11_disagreement_when_years_differ_prefer_earlier():
    """Discogs master year < MB → emit disagreement + prefer Discogs's earlier year."""
    from cdda2img.cdda2img import _r11_corroborate_with_discogs_master

    disc = RBIDisc(album="A", artist="B")
    disc.original_release_found = True
    disc.original_release_year = 1985
    disc.discogs_release_id = 42
    prov: dict[str, str] = {}
    with patch("cdda2img.discogs_lookup.lookup_master_year", return_value=1983):
        _r11_corroborate_with_discogs_master(disc, prov)
    assert prov.get("original_release_disagreement") == "discogs:1983|mb:1985"
    assert disc.original_release_year == 1983  # earlier wins


def test_r11_disagreement_keeps_mb_when_mb_is_earlier():
    """MB year < Discogs master → still flag disagreement but keep MB's earlier year."""
    from cdda2img.cdda2img import _r11_corroborate_with_discogs_master

    disc = RBIDisc(album="A", artist="B")
    disc.original_release_found = True
    disc.original_release_year = 1983
    disc.discogs_release_id = 42
    prov: dict[str, str] = {}
    with patch("cdda2img.discogs_lookup.lookup_master_year", return_value=1985):
        _r11_corroborate_with_discogs_master(disc, prov)
    assert prov.get("original_release_disagreement") == "discogs:1985|mb:1983"
    assert disc.original_release_year == 1983  # MB still earlier — unchanged


def test_r11_no_op_when_mb_did_not_find_original():
    """No corroboration when populate_original_release didn't produce a year."""
    from cdda2img.cdda2img import _r11_corroborate_with_discogs_master

    disc = RBIDisc(album="A", artist="B")
    disc.original_release_found = False
    disc.discogs_release_id = 42
    prov: dict[str, str] = {}
    with patch("cdda2img.discogs_lookup.lookup_master_year", return_value=1983):
        _r11_corroborate_with_discogs_master(disc, prov)
    assert "original_release_corroborated" not in prov
    assert "original_release_disagreement" not in prov


def test_r11_no_op_when_no_discogs_release_id():
    """No corroboration when disc has no Discogs release ID."""
    from cdda2img.cdda2img import _r11_corroborate_with_discogs_master

    disc = RBIDisc(album="A", artist="B")
    disc.original_release_found = True
    disc.original_release_year = 1983
    disc.discogs_release_id = None
    prov: dict[str, str] = {}
    _r11_corroborate_with_discogs_master(disc, prov)
    assert "original_release_corroborated" not in prov


def test_r11_silent_on_discogs_lookup_failure():
    """lookup_master_year returns None → no PROV key, disc.year unchanged."""
    from cdda2img.cdda2img import _r11_corroborate_with_discogs_master

    disc = RBIDisc(album="A", artist="B")
    disc.original_release_found = True
    disc.original_release_year = 1983
    disc.discogs_release_id = 42
    prov: dict[str, str] = {}
    with patch("cdda2img.discogs_lookup.lookup_master_year", return_value=None):
        _r11_corroborate_with_discogs_master(disc, prov)
    assert "original_release_corroborated" not in prov
    assert disc.original_release_year == 1983


# ---------------------------------------------------------------------------
# R12 — lookup_status mapping
# ---------------------------------------------------------------------------


def test_r12_status_disabled_when_not_attempted():
    from cdda2img.cdda2img import _r12_status

    assert _r12_status(attempted=False, has_data=False, errored=False) == "disabled"
    # When not attempted, has_data / errored don't matter:
    assert _r12_status(attempted=False, has_data=True, errored=True) == "disabled"


def test_r12_status_down_on_error():
    from cdda2img.cdda2img import _r12_status

    assert _r12_status(attempted=True, has_data=False, errored=True) == "down"


def test_r12_status_empty_on_no_data():
    from cdda2img.cdda2img import _r12_status

    assert _r12_status(attempted=True, has_data=False, errored=False) == "empty"


def test_r12_status_ok_on_data():
    from cdda2img.cdda2img import _r12_status

    assert _r12_status(attempted=True, has_data=True, errored=False) == "OK"


# ---------------------------------------------------------------------------
# P1 — thread prepop meta into the RG verify (no redundant lookup_release)
# ---------------------------------------------------------------------------


def test_p1_verify_meta_skips_lookup_release() -> None:
    """P1: a verify_meta matching disc.mb_release_id is used directly — the RG
    verify makes no lookup_release round-trip."""
    from cdda2img.original_release import _verify_rg_path_for_disc

    disc = _disc_with_tracks([(1, 1000, "Song", None)], release_id="rel-1")
    meta = _meta_with_tracks([(1, None, "Song", None)], mb_release_id="rel-1")
    with patch("cdda2img.mb_lookup.lookup_release") as m:
        result = _verify_rg_path_for_disc(disc, verify_meta=meta)
    m.assert_not_called()
    assert result is True


def test_p1_verify_meta_mismatch_falls_back_to_live_fetch() -> None:
    """P1 safety: a verify_meta for a DIFFERENT release is ignored; the disc's
    own release is fetched live so correctness never depends on threading."""
    from cdda2img.original_release import _verify_rg_path_for_disc

    disc = _disc_with_tracks([(1, 1000, "Song", None)], release_id="rel-1")
    other = _meta_with_tracks([(1, None, "Song", None)], mb_release_id="rel-OTHER")
    live = _meta_with_tracks([(1, None, "Song", None)], mb_release_id="rel-1")
    with patch("cdda2img.mb_lookup.lookup_release", return_value=live) as m:
        result = _verify_rg_path_for_disc(disc, verify_meta=other)
    m.assert_called_once()
    assert result is True


def test_p1_populate_threads_verify_meta_end_to_end() -> None:
    """P1 plumbing: populate_original_release threads verify_meta all the way to
    the RG verify, so a disc-ID-matched disc makes no lookup_release round-trip.

    Guards the 3->2 MB-call claim against a future hop dropping the argument
    (which the live-fetch fallback would otherwise hide)."""
    disc = _disc_with_tracks([(1, 1000, "Song", None)], release_id="rel-1")
    verify_meta = _meta_with_tracks([(1, None, "Song", None)], mb_release_id="rel-1")
    with (
        patch(
            "cdda2img.original_release._fetch_release_group",
            return_value={
                "title": "Album",
                "first-release-date": "1983",
                "secondary-type-list": [],
            },
        ),
        patch("cdda2img.mb_lookup.lookup_release") as m,
    ):
        populate_original_release(disc, verify_meta=verify_meta)
    m.assert_not_called()
    assert disc.original_release_found is True
    assert disc.original_release_year == 1983
