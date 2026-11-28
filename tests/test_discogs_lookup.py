"""Tests for discogs_lookup.search_by_barcode pagination.

The Discogs client is mocked: these pin how many pages we read and what we do
with them, not the network shape. Barcode search is unranked, so "the first
page" is an arbitrary subset rather than the best matches — the defect these
cover is treating that subset as the whole answer.
"""

from __future__ import annotations

from unittest.mock import patch
from urllib.parse import quote_plus

import pytest

from cdda2img import discogs_lookup


class _FakeResult:
    """Minimal stand-in for a discogs_client search result row."""

    def __init__(self, release_id: int) -> None:
        self.data = {
            "id": release_id,
            "title": "Tracy Chapman - Tracy Chapman",
            "country": "Europe",
            "catno": "7559-60774-2",
        }


class _FakePaged:
    """Stand-in for the paginated search object; records which pages were read."""

    def __init__(self, per_page: int, total: int) -> None:
        self._per_page = per_page
        self._total = total
        self.pages = (total + per_page - 1) // per_page
        self.read: list[int] = []

    def page(self, n: int) -> list[_FakeResult]:
        self.read.append(n)
        start = (n - 1) * self._per_page
        end = min(start + self._per_page, self._total)
        return [_FakeResult(1000 + i) for i in range(start, end)]


def _search(paged: _FakePaged):
    client = type("C", (), {"search": lambda _self, *a, **k: paged})()
    with patch.object(discogs_lookup, "_get_client", return_value=client):
        return discogs_lookup.search_by_barcode("0075596077422")


def test_search_by_barcode_reads_beyond_the_first_page() -> None:
    # Measured on Tracy Chapman: 68 releases share one barcode, and the release
    # MusicBrainz links to is not among the first 25. Truncating to page 1 made
    # the right answer invisible rather than merely lower-ranked.
    paged = _FakePaged(per_page=50, total=68)
    out = _search(paged)
    assert len(out) == 68
    assert paged.read == [1, 2]


def test_search_by_barcode_returns_every_row_of_a_single_page() -> None:
    # The old code sliced page 1 to 25 even when the page held more.
    paged = _FakePaged(per_page=50, total=40)
    out = _search(paged)
    assert len(out) == 40
    assert paged.read == [1]


def test_search_by_barcode_caps_pathological_result_sets() -> None:
    # A compilation series can share one EAN across hundreds of pressings; the
    # cap bounds the request count without affecting any realistic disc.
    paged = _FakePaged(per_page=50, total=5000)
    out = _search(paged)
    assert paged.read == list(range(1, discogs_lookup._BARCODE_SEARCH_MAX_PAGES + 1))
    assert len(out) == 50 * discogs_lookup._BARCODE_SEARCH_MAX_PAGES


def test_search_by_barcode_returns_empty_on_error() -> None:
    class _Boom:
        pages = 2

        def page(self, _n):
            msg = "network"
            raise RuntimeError(msg)

    assert _search(_Boom()) == []  # type: ignore[arg-type]


# ---- master versions and release details (trust_model_design.md §12) ---------
#
# Shapes as Discogs returned them for master 98041 (No Jacket Required) on
# 2026-11-26, including its quirk: facet ``value`` arrives URL-encoded.


class _FakeClient:
    """Answers ``_get`` from *pages* by URL order, and ``release`` with a record
    whose ``data`` is fixed. Records every URL asked for."""

    def __init__(self, pages=None, release_data=None) -> None:
        self.pages = list(pages or [])
        self.urls: list[str] = []
        self._data = release_data or {}

    def _get(self, url):
        self.urls.append(url)
        return self.pages[len(self.urls) - 1]

    def release(self, _rid):
        data = self._data
        return type("R", (), {"data": data, "refresh": lambda _s: None})()


def _facets(countries=(), years=()):
    def values(pairs):
        return [{"title": t, "value": quote_plus(t), "count": n} for t, n in pairs]

    return [
        {"id": "country", "values": values(countries)},
        {"id": "released", "values": values(years)},
    ]


def _page(ids, page, pages, items, *, years=(), countries=()):
    return {
        "pagination": {"page": page, "pages": pages, "items": items},
        "filter_facets": _facets(countries, years),
        "versions": [
            {
                "id": i,
                "title": "No Jacket Required",
                "country": "UK",
                "released": "1985" if i == 5222631 else "0",
                "label": "Virgin",
                "catno": "CDV 2345",
                "format": "Album",
            }
            for i in ids
        ],
    }


def _with(client):
    return patch.object(discogs_lookup, "_get_client", return_value=client)


def test_master_countries_takes_titles_not_the_encoded_values() -> None:
    client = _FakeClient([
        _page([], 1, 108, 108, countries=[("UK", 11), ("UK & Europe", 1)])
    ])
    with _with(client):
        got = discogs_lookup.master_countries(98041)
    assert got == ((("UK", 11), ("UK & Europe", 1)), 108)
    assert "per_page=1" in client.urls[0]
    assert "format=CD" in client.urls[0]


def test_master_versions_filters_by_country_on_the_server_and_pages() -> None:
    client = _FakeClient([
        _page([5222631, 5221810], 1, 2, 3, years=[("1985", 1)]),
        _page([19343422], 2, 2, 3),
    ])
    with _with(client):
        got = discogs_lookup.master_versions(98041, countries=("UK & Europe",))
    assert got is not None
    assert [v.release_id for v in got.versions] == [5222631, 5221810, 19343422]
    assert got.versions[0].year == "1985"
    assert got.versions[1].year is None  # Discogs "0" = unknown
    assert got.total == 3
    assert got.years == (("1985", 1),)
    assert got.undated == 2
    assert all("country=UK+%26+Europe" in u for u in client.urls)  # encoded once
    assert all("/masters/98041/versions" in u for u in client.urls)


def test_master_versions_never_filters_by_year() -> None:
    """Discogs' year filter drops undated versions: 60 of 108 here (§12 D4)."""
    client = _FakeClient([_page([1], 1, 1, 1)])
    with _with(client):
        discogs_lookup.master_versions(98041, countries=("UK",))
    assert "released=" not in client.urls[0]


def test_master_versions_sums_several_countries_and_drops_repeats() -> None:
    client = _FakeClient([
        _page([1, 2], 1, 1, 2, years=[("1985", 2)]),
        _page([2, 3], 1, 1, 2, years=[("1985", 1), ("1986", 1)]),
    ])
    with _with(client):
        got = discogs_lookup.master_versions(98041, countries=("Europe", "UK & Europe"))
    assert got is not None
    assert [v.release_id for v in got.versions] == [1, 2, 3]
    assert got.total == 4
    assert got.years == (("1985", 3), ("1986", 1))


def test_master_versions_reports_when_the_page_guard_binds() -> None:
    n = discogs_lookup._MASTER_VERSIONS_MAX_PAGES
    client = _FakeClient([_page([i], i, n + 3, n + 3) for i in range(1, n + 1)])
    with _with(client):
        got = discogs_lookup.master_versions(98041)
    assert got is not None
    assert got.truncated
    assert len(client.urls) == n


def test_master_versions_is_none_on_error() -> None:
    with _with(_FakeClient([])):  # IndexError inside _get
        assert discogs_lookup.master_versions(98041) is None


def test_release_details_keeps_what_separates_pressings() -> None:
    data = {
        "id": 5222631,
        "master_id": 98041,
        "title": "No Jacket Required",
        "country": "UK",
        "released": "1985",
        "labels": [{"name": "Virgin", "catno": "CDV 2345"}],
        "formats": [{"name": "CD", "descriptions": ["Album"]}],
        "identifiers": [
            {
                "type": "SPARS Code",
                "value": "AAD",
                "description": "Back page of booklet",
            },
            {"type": "Matrix / Runout", "value": "CDV 2345 :   NIMBUS ENGLAND"},
        ],
        "notes": "8 Page sleeve\nNo barcode\n",
    }
    with _with(_FakeClient(release_data=data)):
        d = discogs_lookup.release_details(5222631)
    assert d is not None
    assert d.master_id == 98041
    assert d.barcodes == ()
    assert d.formats == ("CD, Album",)
    assert ("Matrix / Runout", "CDV 2345 :   NIMBUS ENGLAND", "") in d.identifiers
    assert d.notes == "8 Page sleeve\nNo barcode"


def test_release_details_lists_barcodes_apart_from_other_identifiers() -> None:
    data = {
        "identifiers": [
            {"type": "Barcode", "value": "5012981234522"},
            {"type": "SPARS Code", "value": "ADD"},
        ],
        "master_id": 0,  # Discogs' "no master"
    }
    with _with(_FakeClient(release_data=data)):
        d = discogs_lookup.release_details(5221810)
    assert d is not None
    assert d.barcodes == ("5012981234522",)
    assert d.identifiers == (("SPARS Code", "ADD", ""),)
    assert d.master_id is None


def test_a_fetched_release_carries_its_master() -> None:
    data = {"id": 5222631, "title": "No Jacket Required", "master_id": 98041}
    with _with(_FakeClient(release_data=data)):
        got = discogs_lookup.fetch_release(5222631)
    assert got is not None
    assert got.discogs_master_id == 98041


def test_search_masters_reads_the_first_page() -> None:
    class _Hit:
        def __init__(self, i, title, year):
            self.data = {"id": i, "title": title, "year": year}

    paged = type(
        "P",
        (),
        {
            "page": lambda _s, _n: [
                _Hit(98041, "Phil Collins - No Jacket Required", 1985)
            ]
        },
    )()
    client = type("C", (), {"search": lambda _s, *a, **k: paged})()
    with _with(client):
        hits = discogs_lookup.search_masters("Phil Collins", "No Jacket Required")
    assert hits == [
        discogs_lookup.MasterHit(98041, "Phil Collins - No Jacket Required", "1985")
    ]


# ---- catalogue fields a Discogs pick may overwrite (kgr, 2026-11-26) -----------


def _details(
    *,
    country: str = "",
    released: str = "",
    labels: tuple[tuple[str, str], ...] = (),
    barcodes: tuple[str, ...] = (),
) -> discogs_lookup.ReleaseDetails:
    return discogs_lookup.ReleaseDetails(
        release_id=1,
        master_id=None,
        title="",
        country=country,
        released=released,
        label="",
        catno="",
        formats=(),
        barcodes=barcodes,
        identifiers=(),
        notes="",
        labels=labels,
    )


def test_catalogue_fields_takes_what_discogs_states() -> None:
    d = _details(
        country="UK",
        released="1985-03-00",
        labels=(("Virgin", "CDV 2345"), ("Other", "X1")),
        barcodes=("5 012981 234522",),
    )
    assert d.catalogue_fields() == {
        "label": "Virgin",
        "catalog_number": "CDV 2345",
        "country": "GB",
        "barcode": "5012981234522",
        "release_date": "1985",  # the year only (kgr, 2026-11-28)
    }


def test_catalogue_fields_leaves_out_what_discogs_leaves_blank() -> None:
    d = _details(
        country="Unknown",  # no ISO code: left out, never blanked
        released="0",
        labels=(("", "none"),),
        barcodes=("none",),
    )
    assert d.catalogue_fields() == {}


@pytest.mark.parametrize(
    ("released", "expected"),
    [
        ("1985", "1985"),
        ("1985-03-21", "1985-03-21"),
        ("1985-03-00", "1985-03"),
        ("1985-00-00", "1985"),
        ("0", None),
        ("", None),
        ("85", None),
    ],
)
def test_discogs_dates_drop_their_placeholders(released, expected) -> None:
    assert discogs_lookup._discogs_date(released) == expected


def test_master_original_reads_title_and_year() -> None:
    client = _FakeClient([
        {"title": "Brothers In Arms", "year": 1985},
        {"title": "X", "year": 0},
    ])
    with _with(client):
        assert discogs_lookup.master_original(23684) == ("Brothers In Arms", 1985)
        assert discogs_lookup.master_original(1) is None  # Discogs' 0 = no year
    assert client.urls[0] == "https://api.discogs.com/masters/23684"


def test_track_count_and_media_from_a_release() -> None:
    from cdda2img.discogs_lookup import _media, _track_count

    tracklist = [
        {"type_": "heading", "title": "Side A"},
        {"type_": "track", "title": "One"},
        {"type_": "index", "sub_tracks": [{"type_": "track"}, {"type_": "track"}]},
        {"title": "untyped counts as a track"},
    ]
    assert _track_count(tracklist) == 4
    assert _track_count([]) is None
    assert _media([{"name": "CD", "qty": "2"}, {"name": "DVD", "qty": "1"}]) == 3
    assert _media([{"name": "CD"}]) is None
