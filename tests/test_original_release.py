"""
test_original_release.py — original-release lookup unit tests.

All MB calls are mocked; no network access.
"""

from __future__ import annotations

from unittest.mock import patch

from cdda2img.original_release import (
    _parse_year,
    find_original_release,
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


def test_returns_false_without_rg_id():
    disc = _disc(rg_id=None)
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
    with patch(
        "cdda2img.original_release._fetch_release_group",
        return_value=_rg(secondary=["Compilation"]),
    ):
        assert find_original_release(disc) == (False, None, None)


def test_rejects_live_secondary_type():
    disc = _disc()
    with patch(
        "cdda2img.original_release._fetch_release_group",
        return_value=_rg(secondary=["Live"]),
    ):
        assert find_original_release(disc) == (False, None, None)


def test_rejects_when_disc_is_already_the_original():
    """ZZ Top Eliminator 1983 disc shouldn't claim to have an earlier 1983 self."""
    disc = _disc(album="Eliminator", release_date="1983")
    with patch(
        "cdda2img.original_release._fetch_release_group",
        return_value=_rg(title="Eliminator", first_date="1983"),
    ):
        assert find_original_release(disc) == (False, None, None)


def test_rejects_when_first_release_date_is_empty():
    disc = _disc()
    with patch(
        "cdda2img.original_release._fetch_release_group",
        return_value=_rg(first_date=""),
    ):
        assert find_original_release(disc) == (False, None, None)


def test_rejects_when_mb_fetch_fails():
    disc = _disc()
    with patch("cdda2img.original_release._fetch_release_group", return_value=None):
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
    with patch("cdda2img.original_release._fetch_release_group", return_value=None):
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


def test_prov_writes_mb_release_group_id():
    from cdda2img.cdda2img import _add_release_provenance

    disc = RBIDisc(album="A", artist="B", mb_release_group_id="rg-uuid-1")
    prov: dict[str, str] = {}
    _add_release_provenance(prov, disc)
    assert prov["mb_release_group_id"] == "rg-uuid-1"
