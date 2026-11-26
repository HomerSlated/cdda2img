"""The Discogs master screen (trust_model_design.md §12, kgr 2026-11-26).

Fixture: Phil Collins, *No Jacket Required*, as measured live on 2026-11-26.
MusicBrainz release 0a19729e (GB Virgin CDV 2345) links no Discogs release; its
release group links master 98041, which holds 108 CD versions (UK 11, of which 3
are dated, all 1985). The disc in the drive was Discogs 5222631: "No barcode".
Discogs services are faked; nothing here touches the network.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import patch

import pytest

from cdda2img.discogs_lookup import DiscogsVersion, MasterVersions, ReleaseDetails
from cdda2img.menu_state import (
    DiscogsMasterScreen,
    MenuController,
    MenuState,
    _preselected_countries,
)
from cdda2img.rbi_format import RBIDisc

_MASTER = 98041
_COUNTRIES = (
    ("US", 36),
    ("Europe", 18),
    ("UK", 11),
    ("Japan", 9),
    ("UK & Europe", 1),
    ("UK, Europe & US", 1),
    ("Unknown", 1),
)


def _v(rid: int, year: str | None, formats: str = "Album") -> DiscogsVersion:
    return DiscogsVersion(
        rid, "No Jacket Required", "UK", year, "Virgin", "CDV 2345", formats
    )


# Master 98041's eleven UK CDs, in Discogs' order, as measured.
_UK = MasterVersions(
    versions=(
        _v(13748012, "1985"),
        _v(5222631, "1985"),
        _v(10759988, "1985"),
        _v(2118370, None, "Album, Reissue"),
        _v(16271058, None, "Album, Reissue"),
        _v(13176102, None, "Album, Reissue"),
        _v(33611490, None),
        _v(5221810, None, "Album, Reissue"),
        _v(19343422, None, "Album, Reissue"),
        _v(23721866, None),
        _v(14427630, None, "Album, Reissue"),
    ),
    total=11,
    years=(("1985", 3),),
    truncated=False,
)

_DETAILS = ReleaseDetails(
    release_id=5222631,
    master_id=_MASTER,
    title="No Jacket Required",
    country="UK",
    released="1985",
    label="Virgin",
    catno="CDV 2345",
    formats=("CD, Album",),
    barcodes=(),
    identifiers=(
        ("SPARS Code", "AAD", "Back page of booklet"),
        ("Matrix / Runout", "CDV 2345 :   NIMBUS ENGLAND", ""),
    ),
    notes="8 Page sleeve\nNo barcode\nManufactured in the UK",
)


def _disc(country: str = "GB") -> RBIDisc:
    disc = RBIDisc(album="No Jacket Required", artist="Phil Collins")
    disc.mb_release_id = "0a19729e"
    disc.mb_release_group_id = "86b4651b"
    disc.country = country
    return disc


@pytest.fixture
def discogs(monkeypatch):
    """Fakes for every Discogs and MusicBrainz call the screen makes; records
    which countries the versions were asked for."""
    asked: list[tuple[str, ...]] = []
    monkeypatch.setattr("cdda2img.discogs_lookup.is_available", lambda: True)
    monkeypatch.setattr(
        "cdda2img.mb_lookup.discogs_master_of_release_group", lambda _g: _MASTER
    )
    monkeypatch.setattr(
        "cdda2img.discogs_lookup.master_countries", lambda _m: (_COUNTRIES, 108)
    )

    def versions(_m, *, countries=()):
        asked.append(countries)
        return _UK

    monkeypatch.setattr("cdda2img.discogs_lookup.master_versions", versions)
    monkeypatch.setattr("cdda2img.discogs_lookup.release_details", lambda _r: _DETAILS)
    return asked


def _ctl(disc: RBIDisc | None = None, **kw) -> MenuController:
    return MenuController(disc or _disc(), discogs_only=True, **kw)


def _step(ctl: MenuController, *inputs: str) -> None:
    """Paint the top screen, then answer its prompt(s) and apply the result."""
    with ExitStack() as stack:
        stack.enter_context(
            patch("cdda2img.metadata_menu._prompt", side_effect=list(inputs))
        )
        ctl.stack[-1].render(ctl)
        ctl._apply(ctl.stack[-1].handle_input(ctl))


def _screen(ctl: MenuController) -> DiscogsMasterScreen:
    screen = ctl.stack[0]
    assert isinstance(screen, DiscogsMasterScreen)
    return screen


# ---- country pre-selection ------------------------------------------------------


def test_a_gb_pressing_preselects_uk_only() -> None:
    assert _preselected_countries(_COUNTRIES, "GB") == ("UK",)


def test_a_european_pressing_preselects_europe_and_its_joint_regions() -> None:
    """XE: Discogs files the German WEA pressing under "Europe"; "Germany" is not
    a country of this master at all."""
    marked = _preselected_countries(_COUNTRIES, "XE")
    assert "Europe" in marked
    assert "UK & Europe" in marked
    assert "UK" not in marked


@pytest.mark.parametrize("code", [None, "", "XW", "ZZ"])
def test_no_country_or_a_worldwide_one_preselects_nothing(code) -> None:
    assert _preselected_countries(_COUNTRIES, code) == ()


# ---- the walk: country, year, list, details, take ----------------------------------


@pytest.mark.usefixtures("discogs")
def test_the_disc_in_the_drive_is_reached_from_the_master(capsys) -> None:
    ctl = _ctl()
    assert ctl.state is MenuState.DISCOGS_MASTER
    _step(ctl, "")  # Enter = the marked country, UK
    screen = _screen(ctl)
    assert screen.chosen == ("UK",)
    assert screen.phase == "year"
    _step(ctl, "1")  # 1985, undated kept
    assert screen.year == "1985"
    assert screen.phase == "list"
    assert len(screen.shown()) == 11  # 3 dated + 8 undated
    _step(ctl, "2")  # 5222631
    assert ctl.state is MenuState.DISCOGS_RELEASE_DETAIL
    capsys.readouterr()
    _step(ctl, "a")
    out = capsys.readouterr().out
    assert "Barcode   : not listed" in out
    assert "NIMBUS ENGLAND" in out
    assert "https://www.discogs.com/release/5222631" in out
    assert ctl.disc.discogs_release_id == 5222631
    assert ctl.disc.discogs_master_id == _MASTER
    assert ctl.discogs_pick == (5222631, f"{_MASTER}:11/108@UK,1985+undated")
    _step(ctl, "s")
    assert not ctl.stack  # the screen was the root: the run ends


@pytest.mark.usefixtures("discogs")
def test_dropping_undated_versions_leaves_the_dated_three() -> None:
    ctl = _ctl()
    _step(ctl, "")
    _step(ctl, "u")
    _step(ctl, "1")
    assert [v.release_id for v in _screen(ctl).shown()] == [13748012, 5222631, 10759988]
    assert _screen(ctl).filters() == f"{_MASTER}:3/108@UK,1985"


@pytest.mark.usefixtures("discogs")
def test_enter_at_the_year_means_any_year() -> None:
    ctl = _ctl()
    _step(ctl, "")
    _step(ctl, "")
    assert _screen(ctl).year is None
    assert len(_screen(ctl).shown()) == 11
    assert _screen(ctl).filters() == f"{_MASTER}:11/108@UK"


def test_countries_are_asked_for_by_name(discogs) -> None:
    ctl = _ctl()
    _step(ctl, "2,5")  # Europe, UK & Europe
    assert discogs == [("Europe", "UK & Europe")]
    _step(ctl, "c")
    _step(ctl, "a")  # every country
    assert discogs[-1] == ()


@pytest.mark.usefixtures("discogs")
def test_enter_with_nothing_marked_asks_again() -> None:
    ctl = _ctl(_disc(country="XW"))
    _step(ctl, "")
    assert _screen(ctl).phase == "country"
    assert _screen(ctl).versions is None


@pytest.mark.usefixtures("discogs")
def test_quit_restores_what_was_there() -> None:
    disc = _disc()
    disc.discogs_release_id = 5221810
    ctl = _ctl(disc)
    _step(ctl, "")
    _step(ctl, "")
    _step(ctl, "2")
    _step(ctl, "a")
    assert ctl.disc.discogs_release_id == 5222631
    _step(ctl, "q")
    assert ctl.disc.discogs_release_id == 5221810
    assert ctl.discogs_pick is None


@pytest.mark.usefixtures("discogs")
def test_any_key_but_a_leaves_the_details_without_taking_it() -> None:
    ctl = _ctl()
    _step(ctl, "")
    _step(ctl, "")
    _step(ctl, "2")
    _step(ctl, "b")
    assert ctl.state is MenuState.DISCOGS_MASTER
    assert ctl.disc.discogs_release_id is None
    assert ctl.discogs_pick is None


@pytest.mark.usefixtures("discogs")
def test_the_barcode_hit_is_a_marked_row_not_a_pick(capsys) -> None:
    """§12 D6: shown with '=', and nothing is selected until the user takes it."""
    ctl = _ctl(discogs_barcode_hit=5221810)
    _step(ctl, "")
    _step(ctl, "")
    capsys.readouterr()
    ctl.stack[-1].render(ctl)
    out = capsys.readouterr().out
    assert " =5221810" in out
    assert ctl.disc.discogs_release_id is None


@pytest.mark.usefixtures("discogs")
def test_the_list_pages() -> None:
    ctl = _ctl()
    _step(ctl, "")
    _step(ctl, "")
    _step(ctl, "n")
    assert _screen(ctl).page == 1
    _step(ctl, "11")  # the last row, on page 2
    assert ctl.stack[-1].version.release_id == 14427630  # type: ignore[attr-defined]


# ---- no master linked (§12.2) ------------------------------------------------------


def test_with_no_master_linked_a_master_is_searched_for(discogs, monkeypatch) -> None:
    from cdda2img.discogs_lookup import MasterHit

    monkeypatch.setattr(
        "cdda2img.mb_lookup.discogs_master_of_release_group", lambda _g: None
    )
    searched: list[tuple[str, str]] = []

    def search(artist, album):
        searched.append((artist, album))
        return [MasterHit(_MASTER, "Phil Collins - No Jacket Required", "1985")]

    monkeypatch.setattr("cdda2img.discogs_lookup.search_masters", search)
    ctl = _ctl()
    _step(ctl, "f")
    assert _screen(ctl).phase == "master"
    assert searched == [("Phil Collins", "No Jacket Required")]
    _step(ctl, "1")
    assert _screen(ctl).master == _MASTER
    assert _screen(ctl).phase == "country"


# ---- the main menu -----------------------------------------------------------------


@pytest.mark.usefixtures("discogs")
def test_the_main_menu_opens_it_and_it_opens_by_itself_when_asked() -> None:
    ctl = MenuController(_disc())
    assert ctl.state is MenuState.MAIN
    _step(ctl, "d")
    assert ctl.state is MenuState.DISCOGS_MASTER
    opened = MenuController(_disc(), discogs_open=True)
    assert opened.state is MenuState.DISCOGS_MASTER
    assert isinstance(opened.stack[0], type(ctl.stack[0]))  # MainScreen beneath


def test_it_opens_beneath_the_pressing_screen() -> None:
    """The pressing is settled first, so the Discogs screen pre-selects the
    country of the pressing the user picked."""
    from cdda2img.lookup_result import DiscMeta

    pressings = [DiscMeta(mb_release_id="0a19729e"), DiscMeta(mb_release_id="d73bd2e7")]
    ctl = MenuController(_disc(), discogs_open=True, pressing_candidates=pressings)
    assert [s.state for s in ctl.stack] == [
        MenuState.MAIN,
        MenuState.DISCOGS_MASTER,
        MenuState.PRESSING,
    ]


def test_a_pick_is_recorded_in_provenance() -> None:
    from cdda2img.metadata_menu import _record_discogs_pick

    ctl = _ctl()
    ctl.disc.discogs_release_id = 5222631
    ctl.discogs_pick = (5222631, f"{_MASTER}:11/108@UK,1985+undated")
    prov = {"discogs_release": "ambiguous_20+no_link", "discogs_release_barcode": "x"}
    _record_discogs_pick(prov, ctl)
    assert prov["discogs_release"] == "5222631"
    assert prov["discogs_release_via"] == "master_versions"
    assert prov["discogs_master"] == f"{_MASTER}:11/108@UK,1985+undated"
    assert "discogs_release_barcode" not in prov


def test_no_pick_records_nothing() -> None:
    from cdda2img.metadata_menu import _record_discogs_pick

    prov = {"discogs_release": "ambiguous_20+no_link"}
    _record_discogs_pick(prov, _ctl())
    assert prov == {"discogs_release": "ambiguous_20+no_link"}


@pytest.mark.usefixtures("discogs")
def test_the_barcode_legend_appears_only_with_a_barcode_hit(capsys) -> None:
    ctl = _ctl()
    _step(ctl, "")
    _step(ctl, "")
    capsys.readouterr()
    ctl.stack[-1].render(ctl)
    assert "barcode found" not in capsys.readouterr().out


@pytest.mark.usefixtures("discogs")
def test_every_phase_fits_80_columns(capsys) -> None:
    ctl = _ctl(discogs_barcode_hit=5221810)
    for keys in ("", "1", "2"):  # country, year, list, then the details screen
        _step(ctl, keys)
    ctl.stack[-1].render(ctl)
    long = [line for line in capsys.readouterr().out.splitlines() if len(line) > 80]
    assert long == []


@pytest.mark.parametrize("now", [5221810, None])
def test_a_pick_replaced_afterwards_is_not_credited_to_the_screen(now) -> None:
    """Fetch -> Discogs applied another release (5221810), or [u] reset the disc
    (None): the screen did not choose what the disc now carries."""
    from cdda2img.metadata_menu import _record_discogs_pick

    ctl = _ctl()
    ctl.discogs_pick = (5222631, f"{_MASTER}:11/108@UK,1985+undated")
    ctl.disc.discogs_release_id = now
    prov = {"discogs_release": "ambiguous_20+no_link"}
    _record_discogs_pick(prov, ctl)
    assert prov == {"discogs_release": "ambiguous_20+no_link"}


@pytest.mark.usefixtures("discogs")
def test_reset_after_a_pick_records_nothing() -> None:
    from cdda2img.metadata_menu import _record_discogs_pick

    ctl = MenuController(_disc(), discogs_open=True)
    for keys in ("", "", "2", "a", "s"):
        _step(ctl, keys)
    assert ctl.state is MenuState.MAIN
    assert ctl.disc.discogs_release_id == 5222631
    _step(ctl, "u")  # reset to original
    prov: dict[str, str] = {}
    _record_discogs_pick(prov, ctl)
    assert prov == {}
