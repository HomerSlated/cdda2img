"""Discogs country names to the codes an RBI stores (kgr, 2026-11-21)."""

from __future__ import annotations

import pytest

from cdda2img import countries
from cdda2img.countries import discogs_country_code


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("UK", "GB"),  # not ISO "UK": the case that motivated this
        ("US", "US"),
        ("Germany", "DE"),
        ("Belgium", "BE"),
        ("Europe", "XE"),
        ("Worldwide", "XW"),
        ("UK & Europe", "XE"),
        ("Benelux", "XE"),
        ("USSR", "SU"),
        ("Yugoslavia", "YU"),
        ("Czechoslovakia", "XC"),
        ("German Democratic Republic (GDR)", "XG"),
        # spelling folds
        ("uk", "GB"),
        ("Bahamas, The", "BS"),
        ("The Bahamas", "BS"),
        ("Bosnia and Herzegovina", "BA"),
        ("  Trinidad  &  Tobago ", "TT"),
    ],
)
def test_names_become_codes(name, code) -> None:
    assert discogs_country_code(name) == code


@pytest.mark.parametrize(
    "name",
    ["USA & Canada", "Australasia", "South America", "Unknown", "Atlantis", "", None],
)
def test_no_code_is_better_than_a_wrong_one(name) -> None:
    """A region no code describes stays blank; XW would claim a worldwide release."""
    assert discogs_country_code(name) is None


def test_every_code_is_two_capital_letters() -> None:
    """The shape rbi_spec and validation require of ``country``."""
    bad = {
        k: v for k, v in countries._DISCOGS.items() if not (len(v) == 2 and v.isupper())
    }
    assert bad == {}


def test_no_two_spellings_fold_together_with_different_codes() -> None:
    seen: dict[str, str] = {}
    for name, code in countries._DISCOGS.items():
        key = countries._fold(name)
        assert seen.setdefault(key, code) == code, name
