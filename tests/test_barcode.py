"""Tests for barcode normalisation and fuzzy MCN matching (Unit M)."""

import pytest

from cdda2img.barcode import mcn_matches, normalize_barcode


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("0093624877721", "0093624877721"),  # identical EAN-13
        ("093624877721", "0093624877721"),  # printed GTIN-12 vs its EAN-13
        ("009362487772", "0093624877721"),  # missing check digit (12 vs 13)
        ("36248777", "0093624877721"),  # 8-digit partial service record
        ("0093624", "0093624999999"),  # 7-digit company-prefix partial (permissive)
    ],
)
def test_mcn_matches_true(a, b):
    assert mcn_matches(a, b) is True
    assert mcn_matches(b, a) is True  # symmetric


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # The motivating American Idiot case: original disc MCN vs reissue barcode.
        # Common prefix is only 6 digits ("093624"); neither is a substring of the
        # other, so the contradiction is correctly detected.
        ("093624877721", "093624922315"),
        ("123456", "1234567890123"),  # <7-digit run — coincidence guard
        ("", "0093624877721"),  # blank on one side
        (None, "0093624877721"),
        ("0093624877721", None),
        ("1112223334445", "9998887776665"),  # wholly different
    ],
)
def test_mcn_matches_false(a, b):
    assert mcn_matches(a, b) is False


def test_normalize_barcode_still_strict_by_default():
    # mcn_matches is permissive; normalize_barcode keeps its R13 check-digit gate.
    assert normalize_barcode("0093624877721") == "0093624877721"
    assert normalize_barcode("0093624877720") is None  # bad check digit
