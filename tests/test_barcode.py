"""Tests for barcode normalisation (GS1 §1.3.1 / R13)."""

from cdda2img.barcode import normalize_barcode


def test_normalize_barcode_strict_by_default():
    # normalize_barcode keeps its R13 check-digit gate.
    assert normalize_barcode("0093624877721") == "0093624877721"
    assert normalize_barcode("0093624877720") is None  # bad check digit


def test_normalize_barcode_pads_upc_a():
    # 12-digit UPC-A is padded with a leading zero to GTIN-13.
    assert normalize_barcode("075992377423") == "0075992377423"


def test_normalize_barcode_burnable_skips_check_digit():
    # require_check_digit=False yields any 13-digit numeric value (burnable MCN).
    assert (
        normalize_barcode("0093624877720", require_check_digit=False) == "0093624877720"
    )
    assert normalize_barcode("not-a-barcode", require_check_digit=False) is None
