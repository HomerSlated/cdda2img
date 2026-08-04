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
