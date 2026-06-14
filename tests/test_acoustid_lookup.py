"""Tests for acoustid_lookup.py — release candidate sort order."""

from __future__ import annotations

from cdda2img.acoustid_lookup import _COUNTRY_PREF, _release_sort_key


class TestReleaseSortKey:
    def test_earlier_date_sorts_first(self) -> None:
        releases = [
            {"date": "2010", "country": "US"},
            {"date": "1995", "country": "US"},
            {"date": "2021", "country": "US"},
        ]
        result = sorted(releases, key=_release_sort_key)
        assert [r["date"] for r in result] == ["1995", "2010", "2021"]

    def test_missing_date_sorts_last(self) -> None:
        releases = [
            {"date": "", "country": "US"},
            {"date": "1995", "country": "US"},
        ]
        result = sorted(releases, key=_release_sort_key)
        assert result[0]["date"] == "1995"
        assert result[1]["date"] == ""

    def test_none_date_sorts_last(self) -> None:
        releases = [
            {"country": "US"},  # no "date" key
            {"date": "1995", "country": "US"},
        ]
        result = sorted(releases, key=_release_sort_key)
        assert result[0]["date"] == "1995"

    def test_country_pref_gb_before_us(self) -> None:
        releases = [
            {"date": "1995", "country": "US"},
            {"date": "1995", "country": "GB"},
        ]
        result = sorted(releases, key=_release_sort_key)
        assert result[0]["country"] == "GB"
        assert result[1]["country"] == "US"

    def test_country_pref_us_before_xw(self) -> None:
        releases = [
            {"date": "1995", "country": "XW"},
            {"date": "1995", "country": "US"},
        ]
        result = sorted(releases, key=_release_sort_key)
        assert result[0]["country"] == "US"

    def test_unknown_country_sorts_last(self) -> None:
        releases = [
            {"date": "1995", "country": "DE"},
            {"date": "1995", "country": "GB"},
            {"date": "1995", "country": "XW"},
        ]
        result = sorted(releases, key=_release_sort_key)
        assert result[0]["country"] == "GB"
        assert result[1]["country"] == "XW"
        assert result[2]["country"] == "DE"

    def test_date_beats_country(self) -> None:
        releases = [
            {"date": "1993", "country": "DE"},
            {"date": "1995", "country": "GB"},
        ]
        result = sorted(releases, key=_release_sort_key)
        assert result[0]["date"] == "1993"

    def test_country_pref_constants(self) -> None:
        assert _COUNTRY_PREF["GB"] < _COUNTRY_PREF["US"] < _COUNTRY_PREF["XW"]
