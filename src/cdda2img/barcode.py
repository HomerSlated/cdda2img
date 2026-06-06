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
at DEBUG level: the input *looks* structured but is data-wrong, yet this
is routine when scanning third-party metadata (e.g. an MB release with a
typo'd barcode) and must not surface during normal rip operations. Use
``-v`` to see it.

Steps 1-3 are exactly what cdrdao enforces when burning a CATALOG (13
digits, all numeric — ``trackdb/Toc.cc:Toc::catalog``); step 4 is *our*
extra integrity check. ``require_check_digit=False`` runs only steps 1-3,
yielding a "burnable" MCN that cdrdao will accept even if its GS1 check
digit is wrong. This is for the on-disc (gospel) MCN: a check-digit
failure there is usually a Q-channel read error, so callers prefer a
check-digit-valid alternative when one exists but must never *drop* a
burnable disc-baked MCN that has no replacement.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# Below this many digits, a substring match is too likely to be a coincidence
# (a short numeric run shared between two unrelated barcodes). Seven is the
# same floor `_pick_canonical_mcn` has always used for its raw-digit match.
_MIN_MCN_SUBSTRING_DIGITS = 7


def mcn_matches(a: str | None, b: str | None) -> bool:
    """Fuzzy MCN/barcode equality: shorter digit-run is a substring of the longer.

    Exact 13-digit equality is the *wrong* test at every MCN comparison site: no
    metadata service reliably stores the full 13-digit MCN. They hold the printed
    GTIN-12 barcode (no leading zero, no check digit), a prefix of it, or a
    partial record. A printed GTIN-12 is a prefix of the GTIN-12 that is itself a
    suffix of the EAN-13, so a contiguous-substring test bridges all three forms.

    Both sides are reduced to digits (non-digits stripped; no check-digit
    requirement — a check-digit failure must not block a match, the on-disc MCN
    is gospel even when its Q-channel read mangled the check digit). A match
    requires the shorter run to be at least ``_MIN_MCN_SUBSTRING_DIGITS`` digits
    and to appear contiguously in the longer. Blank on either side ⇒ False
    (no evidence is not a match).

    Deliberately permissive: two different EAN-13s from one label share a 7+
    digit GS1 company prefix and will match here. That is acceptable — callers
    use this to *reject* contradictions and to *rank*, never to assert identity
    on its own, so a false-positive degrades to "no contradiction" / "no unique
    winner", never to a wrong merge.
    """
    da = re.sub(r"\D", "", a) if a else ""
    db = re.sub(r"\D", "", b) if b else ""
    if not da or not db:
        return False
    short, long = (da, db) if len(da) <= len(db) else (db, da)
    if len(short) < _MIN_MCN_SUBSTRING_DIGITS:
        return False
    return short in long


def normalize_barcode(
    raw: str | None, *, require_check_digit: bool = True
) -> str | None:
    """Normalize a raw barcode to GTIN-13 (EAN-13), or return None.

    Strips non-digit characters; pads a 12-digit UPC-A with a leading
    ``0`` (GS1 §1.3.1 Table 1-9). Rejects anything that isn't exactly
    13 digits after stripping and padding. R13: also rejects 13-digit
    candidates whose GS1 §1.3.1 check digit is wrong.

    *require_check_digit=False* skips the R13 check-digit step, returning
    any 13-digit numeric value — the form cdrdao will accept at burn time.
    Use it only for the on-disc (gospel) MCN; third-party sources keep the
    default strict check.
    """
    from cdda2img.validators import is_valid_gtin13

    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12:
        digits = "0" + digits
    if len(digits) != 13:
        return None
    if require_check_digit and not is_valid_gtin13(digits):
        log.debug("Rejecting barcode with invalid check digit: %r", raw)
        return None
    return digits
