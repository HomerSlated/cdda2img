"""
barcode.py — MCN / EAN-13 / UPC-A normalisation.

Moved out of ``discogs_lookup.py`` so that callers (cdrdao/nrg/ddp/ccd
parsers, TOC writer, metadata menu, MB lookup) need not depend on the
Discogs module just to validate a barcode string.

``normalize_barcode`` is the single chokepoint for accepting a raw
catalogue string into the pipeline:

  1. Strip non-digit characters.
  2. Pad 12-digit UPC-A to 13-digit GTIN-13 with a leading ``0``
     (GS1 §1.3.1 Table 1-9).
  3. Reject anything that isn't exactly 13 digits.
  4. R13: reject 13-digit candidates whose GS1 §1.3.1 check digit is
     wrong (catches typos and digit transpositions).

Steps 1-3 are silent — the input could be plain garbage. Step 4 logs
at WARNING level because the input *looks* structured but is data-wrong.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)


def normalize_barcode(raw: str | None) -> str | None:
    """Normalize a raw barcode to GTIN-13 (EAN-13), or return None.

    Strips non-digit characters; pads a 12-digit UPC-A with a leading
    ``0`` (GS1 §1.3.1 Table 1-9). Rejects anything that isn't exactly
    13 digits after stripping and padding. R13: also rejects 13-digit
    candidates whose GS1 §1.3.1 check digit is wrong.
    """
    from cdda2img.validators import is_valid_gtin13

    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12:
        digits = "0" + digits
    if len(digits) != 13:
        return None
    if not is_valid_gtin13(digits):
        log.warning("Rejecting barcode with invalid check digit: %r", raw)
        return None
    return digits
