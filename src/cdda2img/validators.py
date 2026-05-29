"""
validators.py — Shared format / check-digit validators (R13).

Two validators, both narrow scope:
  * ``is_valid_gtin13`` — GS1 §1.3.1 Modulo-10 check digit (EAN-13 / UPC-A).
    Used by ``discogs_lookup.normalize_barcode`` to reject 13-digit strings
    whose check digit is wrong (e.g. typo in a manual override).
  * ``validate_isrc`` — ISO 3901 structural check
    (``^[A-Z]{2}[A-Z0-9]{3}\\d{7}$``). Used by ``mb_lookup`` to drop
    malformed ISRCs at network-ingress and merge sites.

Both validators silent-drop (return ``None`` / ``False``) on failure and
log at ``WARNING`` level when the input *looks* structured but fails
validation — matching the rest of the pipeline's "confidence over
coverage" pattern (better blank than wrong).
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

_ISRC_REGEX = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")


def gtin13_check_digit(twelve_digits: str) -> int:
    """Compute the GS1 §1.3.1 Modulo-10 check digit for a 12-digit input.

    Position 1 (leftmost) gets weight 1, position 2 weight 3, alternating
    through position 12. Sum the products, then the check digit is
    ``(10 - sum % 10) % 10``. Caller is responsible for ensuring the
    input is exactly 12 digits — passing anything else raises ``ValueError``.
    """
    if len(twelve_digits) != 12 or not twelve_digits.isdigit():
        msg = f"gtin13_check_digit expects 12 digits, got {twelve_digits!r}"
        raise ValueError(msg)
    weighted = sum(
        int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(twelve_digits)
    )
    return (10 - weighted % 10) % 10


def is_valid_gtin13(thirteen_digits: str) -> bool:
    """Return True iff *thirteen_digits* is a 13-digit string with a valid check digit.

    Used as a final gate after ``normalize_barcode`` has stripped non-digits
    and applied UPC-A → GTIN-13 padding. Non-13-digit / non-digit input
    returns False without raising — callers want a clean boolean here.
    """
    if len(thirteen_digits) != 13 or not thirteen_digits.isdigit():
        return False
    return int(thirteen_digits[12]) == gtin13_check_digit(thirteen_digits[:12])


def validate_isrc(raw: str | None) -> str | None:
    """Return *raw* normalised to ISO 3901, or None if invalid.

    Normalisation: strip hyphens (some sources include them for human
    display) and uppercase the alpha prefix. The result must match
    ``^[A-Z]{2}[A-Z0-9]{3}\\d{7}$`` — 2-letter country, 3-char
    alphanumeric registrant, 7-digit year+designation. Returns None
    (with a WARNING-level log) on any structural failure.
    """
    if not raw:
        return None
    candidate = raw.replace("-", "").upper()
    if _ISRC_REGEX.match(candidate):
        return candidate
    log.warning("Rejecting malformed ISRC: %r", raw)
    return None
