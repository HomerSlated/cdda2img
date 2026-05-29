"""
test_validators.py — R13 format / check-digit validator tests.

Covers ``gtin13_check_digit`` (pure math), ``is_valid_gtin13`` (gate), and
``validate_isrc`` (structure check + normalisation).
"""

from __future__ import annotations

import pytest

from cdda2img.validators import (
    gtin13_check_digit,
    is_valid_gtin13,
    validate_isrc,
)

# ---------------------------------------------------------------------------
# gtin13_check_digit — pure math
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "twelve,expected",
    [
        ("509974702352", 1),  # Technotronic "Pump Up the Jam" → 5099747023521
        ("007599237742", 3),  # ZZ Top "Eliminator"            → 0075992377423
        ("072438369772", 4),  # Radiohead "OK Computer"        → 0724383697724
        ("000000000000", 0),  # All-zero edge case (10 - 0 % 10) % 10 = 0
    ],
)
def test_gtin13_check_digit_known_vectors(twelve: str, expected: int) -> None:
    """Real-world EAN-13 barcodes match GS1 §1.3.1 Modulo-10."""
    assert gtin13_check_digit(twelve) == expected


def test_gtin13_check_digit_rejects_wrong_length() -> None:
    """gtin13_check_digit is strict about the 12-digit contract."""
    with pytest.raises(ValueError):
        gtin13_check_digit("12345")
    with pytest.raises(ValueError):
        gtin13_check_digit("1234567890123")  # 13 digits


def test_gtin13_check_digit_rejects_non_digit() -> None:
    """A letter in the input is a programmer error, not a runtime case."""
    with pytest.raises(ValueError):
        gtin13_check_digit("12345678901A")


# ---------------------------------------------------------------------------
# is_valid_gtin13 — boolean gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "5099747023521",  # Technotronic
        "0075992377423",  # ZZ Top
        "0724383697724",  # Radiohead
    ],
)
def test_is_valid_gtin13_accepts_known_good(code: str) -> None:
    assert is_valid_gtin13(code) is True


@pytest.mark.parametrize(
    "code",
    [
        "5099747023520",  # check digit off by one
        "1234567890123",  # check digit 3 (correct = 8)
        "5099767013432",  # check digit 2 (correct = 4) — found in fixtures
        "9999999999999",  # check digit 9 (correct = 4)
    ],
)
def test_is_valid_gtin13_rejects_wrong_check_digit(code: str) -> None:
    assert is_valid_gtin13(code) is False


@pytest.mark.parametrize(
    "code",
    [
        "",
        "1234",
        "12345678901",  # 11 digits
        "12345678901234",  # 14 digits
        "abc1234567890",  # letters
        "1234567 89012",  # whitespace
    ],
)
def test_is_valid_gtin13_rejects_malformed(code: str) -> None:
    assert is_valid_gtin13(code) is False


# ---------------------------------------------------------------------------
# validate_isrc — structural + normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("USEE18300025", "USEE18300025"),  # ZZ Top — Sharp Dressed Man
        ("BEXX89300001", "BEXX89300001"),  # Technotronic — Pump Up the Jam
        ("GBAYE9300001", "GBAYE9300001"),  # NRG fixture
        # Hyphen normalisation (some display formats include them):
        ("US-EE1-83-00025", "USEE18300025"),
        # Lowercase normalisation (some upstream sources):
        ("usee18300025", "USEE18300025"),
    ],
)
def test_validate_isrc_accepts_and_normalises(raw: str, expected: str) -> None:
    assert validate_isrc(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "ABCDEF",  # too short
        "ABCDEF1234567",  # too long
        "1234567890AB",  # country code is digits
        "USEE18300A25",  # designation has a letter
        "U1EE18300025",  # country code has a digit
        "US@@18300025",  # special chars in registrant
    ],
)
def test_validate_isrc_rejects_malformed(raw: str | None) -> None:
    assert validate_isrc(raw) is None


def test_validate_isrc_logs_warning_on_malformed(caplog) -> None:
    """A structurally-malformed (non-empty) ISRC produces a WARNING log."""
    import logging

    with caplog.at_level(logging.WARNING, logger="cdda2img.validators"):
        result = validate_isrc("US@@18300025")
    assert result is None
    assert any("malformed ISRC" in rec.getMessage() for rec in caplog.records)


def test_validate_isrc_blank_does_not_log(caplog) -> None:
    """Empty / None ISRC is a no-op, not a malformed input — no log."""
    import logging

    with caplog.at_level(logging.WARNING, logger="cdda2img.validators"):
        validate_isrc(None)
        validate_isrc("")
    assert caplog.records == []
