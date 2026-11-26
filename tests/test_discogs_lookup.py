"""Tests for discogs_lookup.search_by_barcode pagination.

The Discogs client is mocked: these pin how many pages we read and what we do
with them, not the network shape. Barcode search is unranked, so "the first
page" is an arbitrary subset rather than the best matches — the defect these
cover is treating that subset as the whole answer.
"""

from __future__ import annotations

from unittest.mock import patch

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


# ---- master versions and release details (No Jacket Required, 2026-11-26) ----


class _FakeClient:
    """Answers ``_get`` with one versions page per call, and ``release`` with a
    record whose ``data`` is fixed."""

    def __init__(self, pages=None, release_data=None) -> None:
        self.pages = pages or []
        self.urls: list[str] = []
        self._data = release_data or {}

    def _get(self, url):
        self.urls.append(url)
        return self.pages[len(self.urls) - 1]

    def release(self, _rid):
        data = self._data
        return type("R", (), {"data": data, "refresh": lambda _s: None})()


def _versions_page(ids, page, pages):
    return {
        "pagination": {"page": page, "pages": pages},
        "versions": [
            {
                "id": i,
                "title": "No Jacket Required",
                "country": "UK",
                "released": "1985" if i == 5222631 else "0",
                "label": "Virgin",
                "catno": "CDV 2345",
            }
            for i in ids
        ],
    }


def test_master_versions_reads_every_page_and_maps_the_country() -> None:
    client = _FakeClient([
        _versions_page([5222631, 5221810], 1, 2),
        _versions_page([19343422], 2, 2),
    ])
    with patch.object(discogs_lookup, "_get_client", return_value=client):
        got = discogs_lookup.master_versions(98041)
    assert [g.discogs_release_id for g in got] == [5222631, 5221810, 19343422]
    assert {g.country for g in got} == {"GB"}  # Discogs "UK"
    assert got[0].release_date == "1985"
    assert got[1].release_date is None  # Discogs "0" = unknown
    assert all("format=CD" in u and "/masters/98041/" in u for u in client.urls)
    assert len(client.urls) == 2


def test_master_versions_is_empty_on_error() -> None:
    client = _FakeClient([])  # IndexError inside _get
    with patch.object(discogs_lookup, "_get_client", return_value=client):
        assert discogs_lookup.master_versions(98041) == []


def test_release_details_says_what_separates_pressings() -> None:
    data = {
        "formats": [{"name": "CD", "descriptions": ["Album"]}],
        "identifiers": [
            {"type": "SPARS Code", "value": "AAD"},
            {"type": "Matrix / Runout", "value": "CDV 2345 :   NIMBUS ENGLAND"},
        ],
        "notes": "8 Page sleeve\nNo barcode\n\nManufactured in the UK",
    }
    client = _FakeClient(release_data=data)
    with patch.object(discogs_lookup, "_get_client", return_value=client):
        lines = discogs_lookup.release_details(5222631)
    assert lines[0] == (
        "CD, Album · barcode: not listed · matrix: CDV 2345 :   NIMBUS ENGLAND"
    )
    assert lines[1] == "8 Page sleeve No barcode Manufactured in the UK"


def test_release_details_names_a_listed_barcode_and_cuts_long_notes() -> None:
    data = {
        "formats": [{"name": "CD", "descriptions": ["Album", "Reissue"]}],
        "identifiers": [{"type": "Barcode", "value": "5012981234522"}],
        "notes": "x" * 500,
    }
    client = _FakeClient(release_data=data)
    with patch.object(discogs_lookup, "_get_client", return_value=client):
        lines = discogs_lookup.release_details(5221810)
    assert lines[0] == "CD, Album, Reissue · barcode: 5012981234522"
    assert len(lines[1]) == 240
