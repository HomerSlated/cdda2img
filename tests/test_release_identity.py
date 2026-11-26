"""Settling the pressing after the menu (trust_model_design.md §13, kgr 2026-11-26).

Data as measured on Dire Straits *Brothers in Arms*: five MusicBrainz pressings, four
sharing barcode 042282449924 and catalogue number 824 499-2, one dated (1986); only
584fc120 links a Discogs release (2750309); Discogs master 23684. Every lookup is faked.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cdda2img import cdda2img as app
from cdda2img import rip_abort as ra
from cdda2img.discogs_lookup import DiscogsVersion, MasterVersions, ReleaseDetails
from cdda2img.lookup_result import DiscMeta
from cdda2img.rbi_format import RBIDisc

_RG = "b02f651e"


def _aborted(exc: pytest.ExceptionInfo) -> ra.RipAborted:
    assert isinstance(exc.value, ra.RipAborted)
    return exc.value


_MASTER = 23684


def _p(
    mbid,
    *,
    barcode="042282449924",
    catno="824 499-2",
    label="Vertigo",
    country="XE",
    date=None,
):
    return DiscMeta(
        album="Brothers in Arms",
        artist="Dire Straits",
        mb_release_id=mbid,
        mb_release_group_id=_RG,
        barcode=barcode,
        catalog_number=catno,
        label=label,
        country=country,
        release_date=date,
    )


BIA = [
    _p(
        "4c618c14",
        barcode="075992526425",
        catno="9 25264-2",
        label="Warner Bros. Records",
        country="US",
        date="1985-05-15",
    ),
    _p("5a5330cd", label="Phonogram"),
    _p("aa230c42", country="DE", date="1986"),
    _p("584fc120", label="Phonogram"),
    _p("7f90585e", country=None),
]


def _v(rid, *, catno="824 499-2", label="Vertigo", country="France", year=None):
    return DiscogsVersion(rid, "Brothers In Arms", country, year, label, catno, "Album")


def _details(
    rid,
    *,
    barcodes=("042282449924",),
    catno="824 499-2",
    label="Vertigo",
    country="France",
    released="",
    master=_MASTER,
):
    return ReleaseDetails(
        release_id=rid,
        master_id=master,
        title="Brothers In Arms",
        country=country,
        released=released,
        label=label,
        catno=catno,
        formats=("CD, Album",),
        barcodes=tuple(barcodes),
        identifiers=(),
        notes="",
        labels=((label, catno),),
    )


def _disc(mbid="584fc120", **kw) -> RBIDisc:
    disc = RBIDisc(album="Brothers in Arms", artist="Dire Straits")
    disc.mb_release_id = mbid
    disc.mb_release_group_id = _RG
    disc.barcode = kw.get("barcode", "042282449924")
    disc.catalog_number = kw.get("catno", "824 499-2")
    disc.label = kw.get("label", "Phonogram")
    disc.country = kw.get("country", "XE")
    disc.release_date = kw.get("date")
    return disc


@pytest.fixture
def services(monkeypatch):
    """Fakes for every lookup; returns knobs the tests turn."""
    knobs = SimpleNamespace(
        links={"584fc120": 2750309},
        reverse={2750309: ["584fc120-full"]},
        versions=[_v(2750309), _v(1111111, country="Germany")],
        truncated=False,
        details={
            2750309: _details(2750309),
            1111111: _details(1111111, country="Germany"),
        },
        pressing_menu=[],
    )
    monkeypatch.setattr("cdda2img.discogs_lookup.is_available", lambda: True)
    monkeypatch.setattr(
        "cdda2img.mb_lookup.discogs_link_and_barcode",
        lambda mbid: (knobs.links.get(mbid), None),
    )
    monkeypatch.setattr(
        "cdda2img.mb_lookup.discogs_master_of_release_group", lambda _g: _MASTER
    )
    monkeypatch.setattr(
        "cdda2img.mb_lookup.mb_releases_linking_discogs",
        lambda rid: knobs.reverse.get(rid, []),
    )
    monkeypatch.setattr(
        "cdda2img.discogs_lookup.master_versions",
        lambda _m, *, countries=(), fmt="CD": MasterVersions(
            tuple(knobs.versions), len(knobs.versions), (), knobs.truncated
        ),
    )
    monkeypatch.setattr(
        "cdda2img.discogs_lookup.release_details", lambda rid: knobs.details.get(rid)
    )

    def pressing_menu(disc, candidates, *, tui, provenance):
        knobs.pressing_menu.append([c.mb_release_id for c in candidates])
        if knobs.pick_pressing:
            meta = next(c for c in candidates if c.mb_release_id == knobs.pick_pressing)
            disc.mb_release_id = meta.mb_release_id
            disc.catalog_number = meta.catalog_number
            provenance["release_selection"] = "manual"
        return disc

    knobs.pick_pressing = None
    monkeypatch.setattr("cdda2img.metadata_menu.run_pressing_menu", pressing_menu)
    return knobs


# ---- unattended (§13.5) --------------------------------------------------------------


def test_brothers_in_arms_unattended_is_the_release_group_only(services) -> None:
    """Several pairs (the link, and the shared catalogue number on four pressings):
    nothing can be corroborated, so no pressing and no pressing-level field the
    candidates do not all state alike."""
    disc, prov = _disc(), {"release_selection": "auto_tiebreak"}
    disc = app._pair_unattended(disc, prov, BIA, "CD")
    assert disc.mb_release_id is None
    assert prov["release_selection"] == "release_group_only"
    assert int(prov["release_pairs"]) > 1
    assert disc.catalog_number is None  # the US pressing's differs
    assert disc.barcode is None
    assert disc.release_date is None  # 1985-05-15 against 1986
    assert disc.discogs_release_id is None
    assert disc.discogs_master_id == _MASTER
    assert prov["discogs_release"] == f"master_only:{_MASTER}"
    assert disc.mb_release_group_id == _RG


def test_a_unique_pair_is_taken_and_fills_the_pressing(services) -> None:
    services.links = {}
    services.versions = [
        _v(7, catno="9 25264-2", label="Warner Bros.", country="US", year="1985")
    ]
    services.details = {
        7: _details(
            7,
            barcodes=(),
            catno="9 25264-2",
            label="Warner Bros.",
            country="US",
            released="1985-05-15",
        )
    }
    two = [BIA[0], BIA[1]]
    disc = _disc(mbid="5a5330cd")
    prov: dict[str, str] = {}
    disc = app._pair_unattended(disc, prov, two, "CD")
    assert disc.mb_release_id == "4c618c14"
    assert disc.discogs_release_id == 7
    assert prov["release_selection"] == "paired"
    assert prov["release_pair"].startswith("4c618c14|7:agreed:")
    assert prov["release_pairs"] == "1"


def test_a_pair_adopts_only_fields_the_pressing_lacks(services) -> None:
    services.links = {}
    services.versions = [_v(7, catno="9 25264-2", label="Warner Bros.", country="US")]
    services.details = {
        7: _details(
            7,
            barcodes=("075992526425",),
            catno="9 25264-2",
            label="Warner Bros.",
            country="US",
            released="1985-05-15",
        )
    }
    pressing = _p(
        "4c618c14",
        barcode=None,
        catno="9 25264-2",
        label="Warner Bros. Records",
        country="US",
        date=None,
    )
    other = _p("5a5330cd")
    disc = _disc(
        mbid="4c618c14",
        barcode=None,
        catno="9 25264-2",
        label="Warner Bros. Records",
        country="US",
    )
    prov: dict[str, str] = {}
    disc = app._pair_unattended(disc, prov, [pressing, other], "CD")
    assert disc.barcode == "0075992526425"  # GTIN-13
    assert disc.release_date == "1985-05-15"
    assert disc.label == "Warner Bros. Records"  # stated by the pressing: kept
    assert prov["release_adopted"] == "barcode:discogs,release_date:discogs"
    assert prov["barcode_source"] == "adopted"


def test_a_truncated_search_cannot_claim_a_unique_pair(services) -> None:
    services.links = {}
    services.truncated = True
    services.versions = [_v(7, catno="9 25264-2", label="Warner Bros.", country="US")]
    prov: dict[str, str] = {}
    disc = app._pair_unattended(_disc(), prov, [BIA[0], BIA[1]], "CD")
    assert prov["release_pairs"] == "1+"
    assert prov["release_selection"] == "release_group_only"
    assert disc.mb_release_id is None


def test_release_group_only_keeps_what_every_pressing_states_alike(services) -> None:
    services.links = {"5a5330cd": 1, "584fc120": 2}  # two pairs: no unique one
    four = [p for p in BIA if p.mb_release_id != "4c618c14"]
    disc = app._pair_unattended(_disc(), {}, four, "CD")
    assert disc.catalog_number == "824 499-2"
    assert disc.barcode == "042282449924"
    assert disc.release_date is None  # three of four undated


def test_an_adopted_or_unanimous_barcode_never_becomes_the_mcn() -> None:
    for source in ("adopted", "unanimous"):
        disc = RBIDisc(album="A", artist="B")
        disc.barcode = "042282449924"
        prov = {"barcode_source": source}
        app._finalize_identifiers(prov, disc)
        assert disc.catalog is None
        assert "mcn_source" not in prov
    # Control: a confirmed pressing's barcode still makes one.
    disc = RBIDisc(album="A", artist="B")
    disc.barcode = "042282449924"
    prov = {}
    app._finalize_identifiers(prov, disc)
    assert disc.catalog == "0042282449924"
    assert prov["mcn_source"] == "barcode_derived"


def test_an_on_disc_mcn_that_differs_from_the_barcode_is_kept() -> None:
    """kgr: at least one disc carries an MCN completely different from its barcode.
    The MCN is the disc's own reading; the barcode never replaces it."""
    disc = RBIDisc(album="A", artist="B")
    disc.catalog = "0000000000017"
    disc.barcode = "042282449924"
    prov: dict[str, str] = {}
    app._finalize_identifiers(prov, disc)
    assert disc.catalog == "0000000000017"
    assert prov["mcn_source"] == "disc"
    assert prov["barcode"] == "042282449924"


# ---- interactive (§13.6) -------------------------------------------------------------


def _picked(prov=None, **kw):
    disc = _disc(**kw)
    disc.discogs_release_id = 2750309
    disc.discogs_master_id = _MASTER
    base = {
        "discogs_release_via": "master_versions",
        "release_selection": "auto_tiebreak",
    }
    base.update(prov or {})
    return disc, base


def test_nothing_to_do_unless_the_discogs_release_was_picked(services) -> None:
    disc = _disc()
    prov = {"discogs_release_via": "mb_link"}
    assert app._pair_after_pick(disc, prov, BIA, tui=False) is disc
    assert "release_pair" not in prov


def test_the_reverse_link_derives_the_pressing(services) -> None:
    services.reverse = {2750309: ["584fc120"]}
    disc, prov = _picked(mbid="5a5330cd")
    disc = app._pair_after_pick(disc, prov, BIA, tui=False)
    assert disc.mb_release_id == "584fc120"
    assert prov["release_selection"] == "derived"
    assert prov["release_pair"] == "584fc120|2750309:reverse_link"
    assert disc.discogs_release_id == 2750309  # the user's pick stands
    assert disc.country == "FR"  # Discogs' stated field laid back on top


def test_a_unique_field_match_derives_the_pressing(services) -> None:
    services.reverse = {}
    services.details = {
        2750309: _details(
            2750309,
            barcodes=("075992526425",),
            catno="9 25264-2",
            label="Warner Bros.",
            country="US",
        )
    }
    disc, prov = _picked()
    disc = app._pair_after_pick(disc, prov, BIA, tui=False)
    assert disc.mb_release_id == "4c618c14"
    assert prov["release_pair"] == "4c618c14|2750309:fields"


def test_several_fitting_pressings_are_asked_about(services) -> None:
    """Brothers in Arms: the shared barcode and catalogue number fit three
    pressings; Germany (aa230c42) contradicts France and the US one everything."""
    services.reverse = {}
    services.pick_pressing = "5a5330cd"
    disc, prov = _picked()
    disc = app._pair_after_pick(disc, prov, BIA, tui=False)
    assert services.pressing_menu == [["5a5330cd", "584fc120", "7f90585e"]]
    assert disc.mb_release_id == "5a5330cd"
    assert prov["release_selection"] == "manual"
    assert disc.country == "FR"  # the Discogs pick still wins over the pressing


def test_no_pressing_picked_leaves_the_release_group_and_the_discogs_pick(
    services,
) -> None:
    services.reverse = {}
    disc, prov = _picked()
    disc = app._pair_after_pick(disc, prov, BIA, tui=False)
    assert disc.mb_release_id is None
    assert prov["release_selection"] == "release_group_only"
    assert disc.discogs_release_id == 2750309  # the user's Discogs pick is kept
    assert disc.barcode == "042282449924"  # stated by that pick: kept
    assert prov.get("barcode_source") != "unanimous"


def test_a_pressing_the_user_also_picked_is_only_checked(services, capsys) -> None:
    services.details = {2750309: _details(2750309, released="1985")}
    disc, prov = _picked({"release_selection": "manual"}, mbid="aa230c42")
    app._pair_after_pick(disc, prov, BIA, tui=False)
    assert prov["release_pair"].startswith("aa230c42|2750309:contradicted:")
    assert "disagree on country, year" in capsys.readouterr().out
    assert services.pressing_menu == []


# ---- the year and the identity gate --------------------------------------------------


def test_a_known_year_is_never_asked_for() -> None:
    disc = _disc(date="1985")
    asked: list[str] = []
    got = app._settle_release_year(
        disc, {}, ask=True, input_fn=lambda p: asked.append(p) or ""
    )
    assert got is disc
    assert asked == []


def test_a_missing_year_is_asked_for_and_checked() -> None:
    answers = iter(["85", "1975", "1985"])
    prov: dict[str, str] = {}
    disc = app._settle_release_year(
        _disc(), prov, ask=True, input_fn=lambda _p: next(answers)
    )
    assert disc.release_date == "1985"
    assert prov["release_date_source"] == "user"


@pytest.mark.parametrize("answer", ["", EOFError])
def test_declining_the_year_leaves_it_missing(answer) -> None:
    def ask(_p):
        if answer is EOFError:
            raise EOFError
        return answer

    disc = app._settle_release_year(_disc(), {}, ask=True, input_fn=ask)
    assert disc.release_date is None


def test_unattended_never_asks() -> None:
    disc = app._settle_release_year(_disc(), {}, ask=False, input_fn=pytest.fail)
    assert disc.release_date is None


def _complete(disc: RBIDisc) -> RBIDisc:
    disc.original_release_found = True
    disc.original_release_title = "Brothers in Arms"
    disc.original_release_year = 1985
    return disc


def test_a_complete_identity_passes() -> None:
    app._require_identity_complete(_complete(_disc(date="1985")), {})


def test_no_year_aborts() -> None:
    from cdda2img import query_log

    query_log.start()  # no outage recorded by an earlier test
    with pytest.raises(ra.RipAborted) as exc:
        app._require_identity_complete(_complete(_disc()), {})
    assert _aborted(exc).code == ra.EXIT_INVESTIGATE


def test_no_original_release_aborts_and_says_why() -> None:
    from cdda2img import query_log

    query_log.start()  # no outage recorded by an earlier test
    with pytest.raises(ra.RipAborted) as exc:
        app._require_identity_complete(
            _disc(date="1985"), {"original_release_rejected": "title_fuzz"}
        )
    assert _aborted(exc).code == ra.EXIT_INVESTIGATE
    assert "title_fuzz" in " ".join(_aborted(exc).details)


def test_an_original_release_lost_to_an_outage_is_retry_later() -> None:
    from cdda2img import query_log

    query_log.start()
    query_log.record("MusicBrainz", "search_releases(...)", "error: NetworkError: 503")
    with pytest.raises(ra.RipAborted) as exc:
        app._require_identity_complete(_disc(date="1985"), {})
    assert _aborted(exc).code == ra.EXIT_RETRY_LATER
