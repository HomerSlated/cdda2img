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
    labels=(("Virgin", "CDV 2345"),),
)


def _disc(country: str = "GB") -> RBIDisc:
    disc = RBIDisc(album="No Jacket Required", artist="Phil Collins")
    disc.mb_release_id = "0a19729e"
    disc.mb_release_group_id = "86b4651b"
    disc.country = country
    return disc


asked_formats: list[list[str]] = []  # the Discogs formats the fixture was asked for


@pytest.fixture
def discogs(monkeypatch):
    """Fakes for every Discogs and MusicBrainz call the screen makes; records
    which countries the versions were asked for."""
    asked: list[tuple[str, ...]] = []
    formats: list[str] = []
    asked_formats.clear()
    asked_formats.append(formats)
    monkeypatch.setattr("cdda2img.discogs_lookup.is_available", lambda: True)
    monkeypatch.setattr(
        "cdda2img.mb_lookup.discogs_master_of_release_group", lambda _g: _MASTER
    )

    def countries(_m, *, fmt="CD"):
        formats.append(fmt)
        return _COUNTRIES, 108

    monkeypatch.setattr("cdda2img.discogs_lookup.master_countries", countries)

    def versions(_m, *, countries=(), fmt="CD"):
        asked.append(countries)
        formats.append(fmt)
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


# ---- a pick overwrites the catalogue fields Discogs states (kgr, 2026-11-26) ------


def _mb_pressing_disc() -> RBIDisc:
    """The disc as the MusicBrainz GB pressing left it before the Discogs pick."""
    disc = _disc()
    disc.label = "Virgin Records"
    disc.catalog_number = "CDV2345"
    disc.release_date = "1985-02"
    disc.barcode = "5012981234522"  # MusicBrainz's: the reissue's barcode
    return disc


@pytest.mark.usefixtures("discogs")
def test_a_pick_overwrites_only_the_fields_discogs_states() -> None:
    from cdda2img.metadata_menu import _record_discogs_pick

    ctl = _ctl(_mb_pressing_disc())
    for keys in ("", "1", "2", "a"):
        _step(ctl, keys)
    assert ctl.disc.label == "Virgin"
    assert ctl.disc.catalog_number == "CDV 2345"
    assert ctl.disc.country == "GB"
    assert ctl.disc.release_date == "1985"
    # 5222631 lists no barcode: a blank never erases, so MusicBrainz's stays.
    assert ctl.disc.barcode == "5012981234522"
    prov: dict[str, str] = {}
    _record_discogs_pick(prov, ctl)
    assert prov["discogs_overwrote"] == "label,catalog_number,country,release_date"


@pytest.mark.usefixtures("discogs")
def test_quit_puts_the_overwritten_fields_back() -> None:
    ctl = _ctl(_mb_pressing_disc())
    for keys in ("", "1", "2", "a", "q"):
        _step(ctl, keys)
    assert ctl.disc.label == "Virgin Records"
    assert ctl.disc.release_date == "1985-02"
    assert ctl.discogs_fields == ()


def test_a_pick_with_no_details_changes_only_the_ids(discogs, monkeypatch) -> None:
    monkeypatch.setattr("cdda2img.discogs_lookup.release_details", lambda _r: None)
    ctl = _ctl(_mb_pressing_disc())
    for keys in ("", "1", "2", "a"):
        _step(ctl, keys)
    assert ctl.disc.discogs_release_id == 5222631
    assert ctl.disc.label == "Virgin Records"
    assert ctl.discogs_fields == ()


# ---- digital media lists the master's File versions (kgr, 2026-11-26) --------------


@pytest.mark.usefixtures("discogs")
def test_digital_media_asks_discogs_for_file_versions(capsys) -> None:
    ctl = _ctl(discogs_fmt="File")
    _step(ctl, "")
    out = capsys.readouterr().out
    assert asked_formats[-1] == ["File", "File"]  # countries, then versions
    assert "108 File releases" in out


@pytest.mark.usefixtures("discogs")
def test_a_pressed_disc_asks_for_cds() -> None:
    ctl = _ctl()
    _step(ctl, "")
    assert asked_formats[-1] == ["CD", "CD"]


@pytest.mark.usefixtures("discogs")
def test_the_main_menu_entry_keeps_the_format() -> None:
    ctl = MenuController(_disc(), discogs_fmt="File")
    _step(ctl, "d")
    screen = ctl.stack[-1]
    assert isinstance(screen, DiscogsMasterScreen)
    assert screen.fmt == "File"


# ---- long lists page (kgr, 2026-11-26: the first rows scrolled off the top) -------

_MANY = (*((f"Country{i:02d}", 40 - i) for i in range(1, 26)), ("UK", 11))


def _long_ctl(monkeypatch, country="GB") -> MenuController:
    monkeypatch.setattr(
        "cdda2img.discogs_lookup.master_countries", lambda _m, *, fmt="CD": (_MANY, 400)
    )
    return _ctl(_disc(country=country))


@pytest.mark.usefixtures("discogs")
def test_a_long_country_list_pages_and_opens_on_the_marked_country(
    monkeypatch, capsys
) -> None:
    ctl = _long_ctl(monkeypatch)
    ctl.stack[-1].render(ctl)
    out = capsys.readouterr().out
    assert "[3/3]" in out  # UK is row 26, on page 3
    assert " 26* UK (11)" in out
    assert "Country01" not in out
    assert all(len(line) <= 80 for line in out.splitlines())


@pytest.mark.usefixtures("discogs")
def test_country_pages_turn_and_numbers_stay_global(monkeypatch, capsys) -> None:
    ctl = _long_ctl(monkeypatch, country="XW")  # nothing marked: page 1
    ctl.stack[-1].render(ctl)
    assert "  1  Country01 (39)" in capsys.readouterr().out
    _step(ctl, "n")
    capsys.readouterr()
    ctl.stack[-1].render(ctl)
    out = capsys.readouterr().out
    assert " 11  Country11 (29)" in out
    assert "Country01" not in out
    _step(ctl, "p")
    assert _screen(ctl).cpage == 0
    _step(ctl, "p")  # already on the first page
    assert _screen(ctl).cpage == 0
    _step(ctl, "12")  # a row on another page is still chosen by its number
    assert _screen(ctl).chosen == ("Country12",)


@pytest.mark.usefixtures("discogs")
def test_a_long_year_list_pages(monkeypatch, capsys) -> None:
    years = tuple((str(1985 + i), 1) for i in range(15))
    many = MasterVersions(_UK.versions, 11, years, False)
    monkeypatch.setattr(
        "cdda2img.discogs_lookup.master_versions",
        lambda _m, *, countries=(), fmt="CD": many,
    )
    ctl = _ctl()
    _step(ctl, "")
    capsys.readouterr()
    ctl.stack[-1].render(ctl)
    out = capsys.readouterr().out
    assert "[1/2]" in out
    assert "1999" not in out
    _step(ctl, "n")
    _step(ctl, "13")  # 1997, on page 2
    assert _screen(ctl).year == "1997"


# ---- beneath a pinned pressing, contradicting versions are hidden (§13.6) ----------


def _gb_pressing():
    from cdda2img.lookup_result import DiscMeta

    return DiscMeta(
        mb_release_id="0a19729e",
        barcode="5012981234522",
        catalog_number="CDV 2345",
        label="Virgin",
        country="GB",
        release_date="1985",
    )


def _mixed_versions():
    return MasterVersions(
        versions=(
            _v(2118370, None, "Album, Reissue"),  # undated: compatible, agrees on less
            _v(5222631, "1985"),
            DiscogsVersion(
                1923531, "No Jacket Required", "UK", "1985", "WEA", "251 699-2", "Album"
            ),
            DiscogsVersion(
                7777777,
                "No Jacket Required",
                "UK",
                "1990",
                "Virgin",
                "CDV 2345",
                "Album",
            ),
        ),
        total=4,
        years=(("1985", 2), ("1990", 1)),
        truncated=False,
    )


@pytest.mark.usefixtures("discogs")
def test_versions_contradicting_the_pressing_are_hidden_and_ranked(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        "cdda2img.discogs_lookup.master_versions",
        lambda _m, *, countries=(), fmt="CD": _mixed_versions(),
    )
    ctl = MenuController(
        _disc(), discogs_only=True, pressing_candidates=[_gb_pressing()]
    )
    _step(ctl, "")
    _step(ctl, "")  # any year
    screen = _screen(ctl)
    # 1923531: catalogue number contradicts; 7777777: year (1990) contradicts.
    assert [v.release_id for v in screen.shown()] == [5222631, 2118370]
    capsys.readouterr()
    ctl.stack[-1].render(ctl)
    assert "2 hidden: they contradict your pressing" in capsys.readouterr().out
    _step(ctl, "m")
    assert len(screen.shown()) == 4
    assert screen.filters().endswith(",unmatched")


@pytest.mark.usefixtures("discogs")
def test_without_a_pinned_pressing_nothing_is_hidden(monkeypatch) -> None:
    monkeypatch.setattr(
        "cdda2img.discogs_lookup.master_versions",
        lambda _m, *, countries=(), fmt="CD": _mixed_versions(),
    )
    ctl = _ctl()
    _step(ctl, "")
    _step(ctl, "")
    assert len(_screen(ctl).shown()) == 4


# ---- the details page pages (kgr, 2026-11-26: it overflowed the screen) -------------


def _long_details(monkeypatch):
    from dataclasses import replace

    notes = "\n".join(f"Note line {i}: " + "word " * 20 for i in range(1, 31))
    monkeypatch.setattr(
        "cdda2img.discogs_lookup.release_details",
        lambda _r: replace(_DETAILS, notes=notes),
    )


def _size(monkeypatch, lines: int) -> None:
    import os

    monkeypatch.setattr(
        "shutil.get_terminal_size", lambda _f=None: os.terminal_size((80, lines))
    )


@pytest.mark.usefixtures("discogs")
def test_long_details_page_to_the_terminal(monkeypatch, capsys) -> None:
    _long_details(monkeypatch)
    _size(monkeypatch, 24)
    ctl = _ctl()
    for keys in ("", "", "2"):
        _step(ctl, keys)
    capsys.readouterr()
    ctl.stack[-1].render(ctl)
    first = capsys.readouterr().out.splitlines()
    assert any("[1/" in ln for ln in first[:5])  # the page number, in the title
    assert len(first) <= 24
    assert all(len(ln) <= 80 for ln in first)
    assert "[n] next" in first[-1]
    _step(ctl, "n")
    capsys.readouterr()
    ctl.stack[-1].render(ctl)
    second = capsys.readouterr().out
    assert "[p] prev" in second
    assert "Title     :" not in second  # the top of the page scrolled away
    _step(ctl, "a")  # taking it works from any page
    assert ctl.disc.discogs_release_id == 5222631


@pytest.mark.usefixtures("discogs")
def test_a_taller_terminal_shows_more_per_page(monkeypatch, capsys) -> None:
    _long_details(monkeypatch)
    ctl = _ctl()
    for keys in ("", "", "2"):
        _step(ctl, keys)
    _size(monkeypatch, 24)
    capsys.readouterr()
    ctl.stack[-1].render(ctl)
    short = len(capsys.readouterr().out.splitlines())
    _size(monkeypatch, 60)
    ctl.stack[-1].render(ctl)
    tall = len(capsys.readouterr().out.splitlines())
    assert tall > short


@pytest.mark.usefixtures("discogs")
def test_a_short_release_has_no_pager(capsys) -> None:
    ctl = _ctl()
    for keys in ("", "", "2"):
        _step(ctl, keys)
    capsys.readouterr()
    ctl.stack[-1].render(ctl)
    out = capsys.readouterr().out
    assert "[n] next" not in out
    assert "[1/" not in out
