"""
test_original_release.py — original-release lookup unit tests.

All MB calls are mocked; no network access.
"""

from __future__ import annotations

from unittest.mock import patch

from cdda2img.original_release import (
    _best_fuzzy_match,
    _deny_match,
    _normalise_title,
    _parse_year,
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


def test_fuzzy_matches_remaster_to_original():
    hit = _best_fuzzy_match(
        "Eliminator (2008 Remaster)",
        [("Eliminator", 1983), ("Eliminator (2008 Remaster)", 2008)],
    )
    assert hit is not None
    title, year, _ = hit
    assert title == "Eliminator"
    assert year == 1983


def test_fuzzy_rejects_sequel_via_denylist():
    # "Led Zeppelin" should NOT match "Led Zeppelin II" even though token
    # overlap is high — deny-list catches it.
    hit = _best_fuzzy_match(
        "Led Zeppelin", [("Led Zeppelin II", 1969), ("Led Zeppelin", 1969)]
    )
    assert hit is not None
    assert hit[0] == "Led Zeppelin"


def test_fuzzy_returns_none_below_cutoff():
    hit = _best_fuzzy_match(
        "OK Computer", [("OK Human", 2019), ("Burn The Witch", 2016)]
    )
    assert hit is None


def test_fuzzy_prefers_earliest_year():
    hit = _best_fuzzy_match(
        "Album",
        [("Album", 2009), ("Album", 1985), ("Album", 1995)],
    )
    assert hit is not None
    assert hit[1] == 1985


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
