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


#: The shortest run of digits an MCN must share with a barcode or a catalogue number
#: to count as similar (kgr, 2026-11-28: *Automatic for the People*'s MCN
#: 9362450550209, barcode 0093624505525 and catalogue number 9362-45055-2 all
#: contain 936245055, nine digits, "highly suggestive of a match").
MCN_SHARED_DIGITS = 9

MCN_EXACT = 2  #: the MCN equals the barcode (both normalised): an exact match
MCN_SIMILAR = 1  #: they share a run of MCN_SHARED_DIGITS digits: scored, not proof
MCN_NONE = 0  #: no MCN, or nothing in common: neither for nor against


def _longest_common_run(a: str, b: str) -> int:
    best = 0
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def mcn_similarity(
    mcn: str | None, barcode: str | None, catalog_number: str | None = None
) -> int:
    """How strongly a burned-in MCN points at a release (kgr, 2026-11-28).

    ``MCN_EXACT`` when it equals the release's barcode once both are normalised;
    ``MCN_SIMILAR`` when it shares a run of :data:`MCN_SHARED_DIGITS` digits with
    the barcode or the catalogue number (no standard formula relates them, so
    this raises a candidate's rating and proves nothing); else ``MCN_NONE``. A
    difference is never evidence against a release: an MCN can be unrelated to
    the barcode altogether (the second Tracy Chapman disc)."""
    digits = re.sub(r"\D", "", mcn or "")
    if not digits:
        return MCN_NONE
    norm = normalize_barcode(digits, require_check_digit=False)
    if (
        norm
        and barcode
        and norm == normalize_barcode(barcode, require_check_digit=False)
    ):
        return MCN_EXACT
    for other in (barcode, catalog_number):
        other_digits = re.sub(r"\D", "", other or "")
        if _longest_common_run(digits, other_digits) >= MCN_SHARED_DIGITS:
            return MCN_SIMILAR
    return MCN_NONE
