"""Field-by-field MusicBrainz/Discogs matching (trust_model_design.md §13).

Records as measured live on 2026-11-26: Dire Straits *Brothers in Arms* (four of five
MusicBrainz pressings share barcode and catalogue number, one is dated) and Phil
Collins *No Jacket Required* (the GB pressing's MusicBrainz barcode belongs to a
reissue; the disc kgr held, Discogs 5222631, lists none).
"""

from __future__ import annotations

import pytest

from cdda2img import release_match as rm
from cdda2img.release_match import ReleaseRecord


def _mb(ident, *, barcode=None, catno=None, labels=(), country=None, year=None):
    return ReleaseRecord(
        "mb",
        ident,
        barcodes=(barcode,) if barcode else (),
        catnos=(catno,) if catno else (),
        labels=tuple(labels),
        country=country,
        year=year,
    )


def _dg(ident, *, barcode=None, catno=None, labels=(), country=None, year=None):
    return ReleaseRecord(
        "discogs",
        ident,
        barcodes=(barcode,) if barcode else (),
        catnos=(catno,) if catno else (),
        labels=tuple(labels),
        country=country,
        year=year,
    )


# Brothers in Arms, MusicBrainz, as measured.
BIA = [
    _mb(
        "4c618c14",
        barcode="075992526425",
        catno="9 25264-2",
        labels=["Warner Bros. Records"],
        country="US",
        year="1985-05-15",
    ),
    _mb(
        "5a5330cd",
        barcode="042282449924",
        catno="824 499-2",
        labels=["Phonogram", "Vertigo"],
        country="XE",
    ),
    _mb(
        "aa230c42",
        barcode="042282449924",
        catno="824 499-2",
        labels=["Vertigo"],
        country="DE",
        year="1986",
    ),
    _mb(
        "584fc120",
        barcode="042282449924",
        catno="824 499-2",
        labels=["Phonogram", "Vertigo"],
        country="XE",
    ),
    _mb("7f90585e", barcode="042282449924", catno="824 499-2", labels=["Vertigo"]),
]


# ---- normalisation -----------------------------------------------------------------


def test_catalogue_numbers_fold_spacing_and_punctuation() -> None:
    assert rm.norm_catnos(["824 499-2"]) == rm.norm_catnos(["824499-2"]) == {"8244992"}
    assert rm.norm_catnos(["CDV 2345, 0777 7 86402 2 6"]) == {"cdv2345", "077778640226"}


@pytest.mark.parametrize("absent", ["", "none", "None", "N/A", "-", None])
def test_absent_values_say_nothing(absent) -> None:
    assert rm.norm_catnos([absent]) == frozenset()
    assert rm.norm_barcodes([absent]) == frozenset()
    assert rm.norm_year(absent) is None


def test_labels_fold_corporate_suffixes() -> None:
    assert rm.norm_labels(["Warner Bros. Records"]) == rm.norm_labels(["warner bros"])
    assert rm.norm_labels(["Virgin Records Ltd."]) == {"virgin"}


@pytest.mark.parametrize(
    ("value", "places"),
    [
        ("GB", {"GB"}),  # a MusicBrainz code
        ("UK", {"GB"}),  # a Discogs name, not the code "UK"
        ("Europe", {"XE"}),
        ("UK, Europe & US", {"GB", "XE", "US"}),
        ("Unknown", None),
        ("XW", None),
        (None, None),
    ],
)
def test_country_places(value, places) -> None:
    got = rm.norm_country(value)
    assert (got if got is None else set(got)) == places


# ---- comparison --------------------------------------------------------------------


def test_europe_contains_its_countries_but_not_others() -> None:
    eu = _mb("a", country="XE")
    assert rm.compare(eu, _dg("1", country="France")).agreed == ("country",)
    assert rm.compare(eu, _dg("2", country="UK")).agreed == ("country",)
    assert rm.compare(eu, _dg("3", country="US")).contradicted == ("country",)
    assert rm.compare(_mb("b", country="GB"), _dg("4", country="US")).contradicted == (
        "country",
    )


def test_absence_neither_confirms_nor_refutes() -> None:
    """kgr: 'no barcode' means 'none submitted'."""
    c = rm.compare(
        _mb("a", barcode="5012981234522", catno="CDV 2345"),
        _dg("5222631", catno="CDV 2345"),
    )
    assert c.agreed == ("catno",)
    assert c.contradicted == ()
    assert c.sufficient


def test_a_contradiction_rejects_whatever_agrees() -> None:
    c = rm.compare(
        _mb("a", barcode="042282449924", catno="824 499-2", year="1986"),
        _dg("1", barcode="042282449924", catno="824 499-2", year="1985"),
    )
    assert c.contradicted == ("year",)
    assert not c.compatible
    assert not c.sufficient


def test_weak_fields_alone_are_not_sufficient() -> None:
    """§13.3: country, year and label are shared by too many pressings."""
    c = rm.compare(
        _mb("a", labels=["Virgin"], country="GB", year="1985"),
        _dg("1", labels=["Virgin"], country="UK", year="1985"),
    )
    assert set(c.agreed) == {"label", "country", "year"}
    assert c.compatible
    assert not c.sufficient


def test_brothers_in_arms_pressings_cannot_be_told_apart_by_fields() -> None:
    """A Discogs release carrying the shared barcode and catalogue number pairs
    with every compatible pressing: the field match finds four, not one."""
    discogs = _dg(
        "2750309",
        barcode="042282449924",
        catno="824 499-2",
        labels=["Vertigo"],
        country="France",
    )
    pairs = rm.matching_pairs(BIA, [discogs])
    # aa230c42 is DE: France does not contain Germany, so its country contradicts.
    assert sorted(a.ident for a, _b, _c in pairs) == [
        "584fc120",
        "5a5330cd",
        "7f90585e",
    ]


def test_a_discogs_year_separates_the_dated_pressing() -> None:
    discogs = _dg("x", catno="824 499-2", country="Germany", year="1985")
    pairs = rm.matching_pairs(BIA, [discogs])
    assert "aa230c42" not in {a.ident for a, _b, _c in pairs}  # 1986 contradicts 1985


def test_ranking_drops_contradictions_and_puts_the_best_first() -> None:
    anchor = _mb(
        "0a19729e",
        barcode="5012981234522",
        catno="CDV 2345",
        labels=["Virgin"],
        country="GB",
        year="1985",
    )
    others = [
        _dg(
            "2118370",
            catno="CDV 2345, 0777 7 86402 2 6",
            labels=["Virgin"],
            country="UK",
        ),
        _dg("5222631", catno="CDV 2345", labels=["Virgin"], country="UK", year="1985"),
        _dg(
            "1923531", catno="251 699-2", labels=["WEA"], country="Germany", year="1985"
        ),
    ]
    ranked = rm.rank_compatible(anchor, others)
    assert [o.ident for o, _c in ranked] == ["5222631", "2118370"]


def test_unanimous_keeps_only_an_uncontradicted_value() -> None:
    assert rm.unanimous([r.year for r in BIA], rm.norm_year) is None  # 1985 vs 1986
    four = [r for r in BIA if r.ident != "4c618c14"]
    got = rm.unanimous([r.catnos[0] for r in four], lambda v: rm.norm_catnos([v]))
    assert got == "824 499-2"
    assert rm.unanimous([None, "", None], str) is None


def test_describe_for_provenance() -> None:
    ok = rm.compare(_mb("a", catno="CDV 2345"), _dg("1", catno="CDV2345"))
    bad = rm.compare(_mb("a", year="1985"), _dg("1", year="1986"))
    assert ok.describe() == "agreed:catno"
    assert bad.describe() == "contradicted:year"


def test_require_all_drops_a_value_only_some_records_state() -> None:
    """Release-group-only (§13.5): one dated pressing of seven dates none of them."""
    assert rm.unanimous(["1988", None, None], rm.norm_year) == "1988"
    assert rm.unanimous(["1988", None, None], rm.norm_year, require_all=True) is None
    assert rm.unanimous(["1988", "1988"], rm.norm_year, require_all=True) == "1988"


def test_a_label_difference_never_rejects() -> None:
    """Brothers in Arms: the disc-ID response keeps MusicBrainz's first label
    (Phonogram) of two (Phonogram, Vertigo)."""
    c = rm.compare(
        _mb("5a5330cd", catno="824 499-2", labels=["Phonogram"]),
        _dg("2750309", catno="824 499-2", labels=["Vertigo"]),
    )
    assert c.contradicted == ()
    assert c.agreed == ("catno",)
    assert c.sufficient


# ---- contradiction-only evidence (kgr, 2026-11-28) -------------------------------------


def _mbx(**kw) -> ReleaseRecord:
    return ReleaseRecord("mb", "m", barcodes=("0093624505525",), **kw)


def _dgx(**kw) -> ReleaseRecord:
    return ReleaseRecord("discogs", "1", barcodes=("0093624505525",), **kw)


@pytest.mark.parametrize(
    ("a", "b", "contradicts"),
    [
        ("Automatic for the People", "Automatic For The People", False),
        (
            "Automatic for the People",
            "Automatic For The People (25th Anniversary)",
            False,
        ),
        (
            "Automatic for the People",
            "Automatic For The People = オートマチック",
            False,
        ),
        ("Rock & Roll", "Rock and Roll", False),
        ("Automatic for the People", "Out of Time", True),
        (None, "Out of Time", False),  # absent is silent
    ],
)
def test_a_title_only_ever_contradicts(a, b, contradicts) -> None:
    c = rm.compare(_mbx(title=a), _dgx(title=b))
    assert ("title" in c.contradicted) is contradicts
    assert "title" not in c.agreed  # agreement never makes a match


@pytest.mark.parametrize(
    ("a", "b", "contradicts"),
    [
        ("R.E.M.", "R.E.M.", False),
        ("Genesis", "Genesis (2)", False),  # Discogs' own numbering
        ("Various Artists", "Various", False),
        ("Simon & Garfunkel", "Simon / Garfunkel", False),
        ("R.E.M.", "Dire Straits", True),
    ],
)
def test_an_artist_only_ever_contradicts(a, b, contradicts) -> None:
    c = rm.compare(_mbx(artist=a), _dgx(artist=b))
    assert ("artist" in c.contradicted) is contradicts


@pytest.mark.parametrize(
    ("mb", "dg", "contradicts"),
    [
        ((12, 1), (12, 1), False),
        ((12, 1), (13, 1), True),
        ((12, 2), (24, 2), False),  # two media: MusicBrainz counts one of them
        ((12, None), (13, 1), False),  # media unknown: not compared
    ],
)
def test_a_track_count_contradicts_only_on_one_medium(mb, dg, contradicts) -> None:
    c = rm.compare(_mbx(tracks=mb[0], media=mb[1]), _dgx(tracks=dg[0], media=dg[1]))
    assert ("tracks" in c.contradicted) is contradicts


def test_a_contradiction_only_field_breaks_a_sufficient_pair() -> None:
    """The barcode agrees, so the pair was sufficient; a different album title
    now breaks it. Control: the same pair without titles stays sufficient."""
    assert rm.compare(_mbx(), _dgx()).sufficient
    assert not rm.compare(_mbx(title="Out of Time"), _dgx(title="Monster")).sufficient


def test_a_contradiction_only_field_never_makes_a_pair() -> None:
    a = ReleaseRecord("mb", "m", title="X", artist="Y", tracks=10, media=1)
    b = ReleaseRecord("discogs", "1", title="X", artist="Y", tracks=10, media=1)
    assert not rm.compare(a, b).sufficient
