#!/usr/bin/env python3
"""
trace_metadata_provenance.py — Reproduce the pre-menu provenance of the five core
metadata values that the cdda2img rip/import pipeline renders in the metadata menu
"disc summary": album title, album YEAR, artist, the "Original:" release line, the
MCN (disc.catalog), and per-track ISRCs.

This is a condensed, single-file extract of the *decision logic* in
``cdda2img._run_metadata_lookups`` → ``_merge_into_disc`` /
``_prepopulate_from_discogs`` / ``_r6_acoustid_corroborate`` /
``original_release.find_original_release``, plus the display rules in
``metadata_menu._print_disc_summary`` and ``rbi_format.format_original_fields``.
It does NOT hit the network: you feed it the *resolved* per-service values (what
CD-Text captured, what MB/Discogs/AcoustID/CDDB returned) and it applies the exact
precedence + merge + validation + display semantics the real code uses, so you can
test "who wins" against any combination.

Load-bearing invariants preserved verbatim:

  * Precedence (current code, post-cb4bcc7 CDDB demotion):
        disc-baked CD-Text > MusicBrainz > Discogs > AcoustID > CDDB
    Every merge is FILL-BLANK (``_merge_into_disc``): an existing non-blank field
    wins; a lower-precedence source only fills what nothing richer provided.
  * Album line YEAR = ``year_of(disc.release_date)`` — THIS pressing's year, NOT
    the original-release year. release_date itself is fill-blank merged.
  * Original line = ``format_original_fields(disc_year, found, title, orig_year)``;
    "Unknown, unknown release (unknown year)" iff ``disc_year is None or not found``.
  * MCN: on-disc CATALOG is normalised by ``barcode.normalize_barcode``. All-zeros
    is nulled in the TOC parser. A non-13-digit value is silently length-dropped
    (a "captured then dropped" case). A 13-digit value with a BAD check digit is
    KEPT as a burnable last-resort (never dropped). An unresolved MB multi-match
    deliberately does NOT guess a barcode.
  * ISRC: ``validators.validate_isrc`` (ISO-3901) drops malformed values with a
    WARNING; an absent ISRC is simply blank. MB-side ISRCs were validated at
    ingress; the disc-side ISRC is re-validated at the merge chokepoint.

Usage (from project root):

    # cd-paranoia FALLBACK: no subchannel at all; MB matched a 1986 pressing.
    # This reproduces the ZZ Top "Eliminator" symptom.
    uv run python tools/trace_metadata_provenance.py \\
        --rip-engine cd-paranoia \\
        --mb-album Eliminator --mb-artist "ZZ Top" \\
        --mb-release-date 1986-03 --mb-rg-first-date 1983-03-23 \\
        --mb-cardinality multi --mb-resolved no

    # cdrdao PRIMARY, disc genuinely lacks MCN/ISRC (1980s pressing), single MB hit.
    uv run python tools/trace_metadata_provenance.py \\
        --rip-engine cdrdao \\
        --cdtext-album Eliminator --cdtext-artist "ZZ Top" \\
        --mb-album Eliminator --mb-artist "ZZ Top" \\
        --mb-release-date 1986-03 --mb-rg-first-date 1983-03-23 \\
        --mb-cardinality single --mb-resolved yes

    # captured-but-malformed MCN (check-digit bad → kept; non-13-digit → dropped)
    uv run python tools/trace_metadata_provenance.py \\
        --rip-engine cdrdao --disc-catalog 0090317712345

    # captured-but-malformed ISRC on track 1 (dropped by validate_isrc)
    uv run python tools/trace_metadata_provenance.py \\
        --rip-engine cdrdao --disc-isrc "1=GARBAGE"
"""

from __future__ import annotations

import argparse
import re
import sys

# ---------------------------------------------------------------------------
# Validators — byte-for-byte copies of the project chokepoints (load-bearing).
# ---------------------------------------------------------------------------

_ISRC_REGEX = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")
_ALL_ZEROS_MCN = "0000000000000"


def gtin13_check_digit(twelve_digits: str) -> int:
    """GS1 §1.3.1 Modulo-10 check digit (mirrors validators.gtin13_check_digit)."""
    if len(twelve_digits) != 12 or not twelve_digits.isdigit():
        msg = f"gtin13_check_digit expects 12 digits, got {twelve_digits!r}"
        raise ValueError(msg)
    weighted = sum(
        int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(twelve_digits)
    )
    return (10 - weighted % 10) % 10


def is_valid_gtin13(thirteen_digits: str) -> bool:
    """Mirrors validators.is_valid_gtin13."""
    if len(thirteen_digits) != 13 or not thirteen_digits.isdigit():
        return False
    return int(thirteen_digits[12]) == gtin13_check_digit(thirteen_digits[:12])


def normalize_barcode(
    raw: str | None, *, require_check_digit: bool = True
) -> str | None:
    """Mirrors barcode.normalize_barcode (strip → UPC-A pad → 13-digit → check)."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12:
        digits = "0" + digits
    if len(digits) != 13:
        return None
    if require_check_digit and not is_valid_gtin13(digits):
        return None
    return digits


def validate_isrc(raw: str | None) -> str | None:
    """Mirrors validators.validate_isrc (ISO-3901 structural; silent-drop)."""
    if not raw:
        return None
    candidate = raw.replace("-", "").upper()
    if _ISRC_REGEX.match(candidate):
        return candidate
    return None  # real code logs WARNING here


# ---------------------------------------------------------------------------
# Display helpers — mirror rbi_format / metadata_menu.
# ---------------------------------------------------------------------------


def year_of(date_str: str | None) -> int | None:
    """Mirrors rbi_format.year_of."""
    if not date_str:
        return None
    try:
        return int(str(date_str)[:4])
    except (ValueError, IndexError):
        return None


def format_original(
    disc_year: int | None,
    found: bool,
    title: str | None,
    orig_year: int | None,
) -> str:
    """Mirrors rbi_format.format_original_fields."""
    if disc_year is None or not found:
        return "Original: Unknown, unknown release (unknown year)"
    if disc_year == orig_year:
        return f"Original: Yes, this release ({disc_year})"
    disp_title = title or "unknown release"
    year_disp = orig_year if orig_year is not None else "unknown year"
    return f"Original: No, {disp_title} ({year_disp})"


# ---------------------------------------------------------------------------
# Pipeline model.
# ---------------------------------------------------------------------------

_UNKNOWN = "Unknown Artist"
_R14_PRE_EMPH_YEAR_CAP = 1986
_DERIVATIVE_SECONDARY_TYPES = frozenset({
    "Compilation",
    "Live",
    "Remix",
    "DJ-mix",
    "Demo",
})


def fill_blank(existing: str | None, candidate: str | None) -> str | None:
    """``disc.X or meta.X`` fill-blank merge (mirrors _merge_into_disc fields)."""
    return existing if existing else (candidate or existing)


def fill_artist(existing: str | None, candidate: str | None) -> str | None:
    """Artist fill-blank with the 'Unknown Artist' sentinel treated as blank."""
    if existing and existing != _UNKNOWN:
        return existing
    return candidate or existing


def main(argv: list[str] | None = None) -> int:  # noqa: C901
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--rip-engine",
        choices=["cdrdao", "cd-paranoia"],
        default="cdrdao",
        help="cdrdao captures subchannel+CD-Text; cd-paranoia captures neither",
    )
    # On-disc (CD-Text / Q-channel) captures. Ignored entirely when the engine
    # is cd-paranoia (the fallback returns RBIDisc(album='', artist='') with
    # blank, isrc-less tracks).
    p.add_argument("--cdtext-album", default="")
    p.add_argument("--cdtext-artist", default="")
    p.add_argument("--disc-catalog", default="", help="raw on-disc CATALOG (MCN)")
    p.add_argument(
        "--disc-isrc",
        action="append",
        default=[],
        metavar="N=ISRC",
        help="raw on-disc per-track ISRC, e.g. 1=USRC17600001 (repeatable)",
    )
    p.add_argument(
        "--disc-pre-emphasis",
        action="store_true",
        help="cdrdao saw PRE_EMPHASIS on >=1 track (gates R14 year cap)",
    )
    p.add_argument("--tracks", type=int, default=11, help="track count")
    # MusicBrainz resolved view.
    p.add_argument("--mb-album", default=None)
    p.add_argument("--mb-artist", default=None)
    p.add_argument("--mb-release-date", default=None, help="THIS pressing's date")
    p.add_argument("--mb-catalog", default=None, help="MB pressing barcode (raw)")
    p.add_argument("--mb-rg-first-date", default=None, help="RG first-release-date")
    p.add_argument("--mb-rg-secondary", default="", help="RG secondary type, if any")
    p.add_argument(
        "--mb-cardinality",
        choices=["zero", "single", "multi"],
        default="single",
    )
    p.add_argument(
        "--mb-resolved",
        choices=["yes", "no"],
        default="yes",
        help="for multi: did R1 ISRC/MCN pick a unique pressing?",
    )
    # Discogs / CDDB fill-blank contributors.
    p.add_argument("--discogs-catalog", default=None)
    p.add_argument("--cddb-album", default=None)
    p.add_argument("--cddb-artist", default=None)
    args = p.parse_args(argv)

    notes: list[str] = []

    # --- Stage 0: capture (engine-dependent) ------------------------------
    if args.rip_engine == "cd-paranoia":
        album: str | None = ""
        artist: str | None = ""
        catalog_raw: str | None = None
        disc_isrcs: dict[int, str] = {}
        pre_emph = None
        notes.append(
            "cd-paranoia FALLBACK: disc_reader returns RBIDisc(album='', "
            "artist='') with blank, isrc-less tracks — NO CD-Text, NO MCN, "
            "NO ISRC, NO pre-emphasis captured."
        )
    else:
        album = args.cdtext_album
        artist = args.cdtext_artist
        # TOC parser nulls an all-zeros MCN; otherwise the raw string flows on.
        catalog_raw = (
            args.disc_catalog
            if args.disc_catalog and args.disc_catalog != _ALL_ZEROS_MCN
            else None
        )
        disc_isrcs = {}
        for spec in args.disc_isrc:
            num, _, val = spec.partition("=")
            disc_isrcs[int(num)] = val
        pre_emph = bool(args.disc_pre_emphasis)
        notes.append("cdrdao PRIMARY: subchannel + CD-Text captured as supplied.")

    release_date: str | None = None
    mb_release_group_id: str | None = None
    mb_release_id: str | None = None

    # --- Stage 1: MusicBrainz (fill-blank, highest remote precedence) -----
    mb_supplies_album = False
    if args.mb_cardinality == "zero":
        notes.append("MB: disc-ID unknown to MB (zero matches); nothing merged.")
    elif args.mb_cardinality == "multi" and args.mb_resolved == "no":
        # Agreed-facts meta: NO album, NO catalog, mb_release_id=None. Only the
        # release-group + an agreed year (when every dated candidate agrees).
        mb_release_group_id = "RG-agreed-facts"
        release_date = fill_blank(release_date, args.mb_release_date)
        notes.append(
            "MB: unresolved multi-match → agreed-facts only (RG + agreed year). "
            "No album, no catalog, mb_release_id stays None (no pressing guessed)."
        )
    else:
        # single, or multi resolved by R1.
        album = fill_blank(album, args.mb_album)
        if args.mb_album and (album == args.mb_album):
            mb_supplies_album = True
        artist = fill_artist(artist, args.mb_artist)
        release_date = fill_blank(release_date, args.mb_release_date)
        catalog_raw = catalog_raw or (args.mb_catalog or None)
        mb_release_group_id = "RG-single"
        mb_release_id = "REL-pressing"
        notes.append(
            "MB: single/resolved pressing merged (album/artist/release_date/"
            "catalog fill-blank; mb_release_id + RG set)."
        )

    # --- Stage 2: Discogs (only if an MCN is already chosen) --------------
    chosen_mcn = normalize_barcode(catalog_raw) if catalog_raw else None
    if chosen_mcn is None and catalog_raw:
        # burnable last-resort: 13 numeric digits, bad check digit, kept.
        chosen_mcn = normalize_barcode(catalog_raw, require_check_digit=False)
        if chosen_mcn:
            notes.append(
                f"MCN {catalog_raw!r}: check digit FAILED but 13 numeric digits "
                "→ kept as burnable last-resort (not dropped)."
            )
        else:
            notes.append(
                f"MCN {catalog_raw!r}: not 13 digits after strip/pad → "
                "silently length-dropped by normalize_barcode (captured-then-dropped)."
            )
    catalog = chosen_mcn
    if catalog is None and args.discogs_catalog and chosen_mcn is None:
        notes.append("Discogs: no MCN to query by → catalog stays unfilled.")
    # (When an MCN exists, a single Discogs barcode hit could enrich label/year;
    # the catalog itself is already set from phase A above.)

    # --- Stage 3: CDDB (lowest precedence, fill-blank gap-filler) ----------
    album = fill_blank(album, args.cddb_album)
    artist = fill_artist(artist, args.cddb_artist)

    # --- Per-track ISRC resolution (validate at the merge chokepoint) ------
    track_isrcs: dict[int, str | None] = {}
    for tn in range(1, args.tracks + 1):
        disc_side = validate_isrc(disc_isrcs.get(tn))
        if disc_isrcs.get(tn) and disc_side is None:
            notes.append(
                f"ISRC track {tn}: {disc_isrcs[tn]!r} malformed → dropped by "
                "validate_isrc (captured-then-dropped)."
            )
        # MB-side ISRC only exists on a resolved/single pressing; modelled None
        # here unless you extend the harness — the symptom has none.
        track_isrcs[tn] = disc_side or None

    # --- Original release lookup ------------------------------------------
    orig_found = False
    orig_title: str | None = None
    orig_year: int | None = None
    rg_year = year_of(args.mb_rg_first_date)
    if not mb_release_group_id:
        notes.append("Original: no mb_release_group_id → RG path skipped.")
    elif args.mb_rg_secondary in _DERIVATIVE_SECONDARY_TYPES:
        notes.append(
            f"Original: RG secondary type {args.mb_rg_secondary} is derivative "
            "→ RG rejected."
        )
    elif rg_year is None:
        notes.append("Original: RG has no parseable first-release-date → rejected.")
    elif pre_emph is True and rg_year > _R14_PRE_EMPH_YEAR_CAP:
        notes.append(
            f"Original: R14 reject — pre-emphasis set but RG year {rg_year} > "
            f"{_R14_PRE_EMPH_YEAR_CAP}."
        )
    else:
        # R3 _verify_rg_path_for_disc: mb_release_id None → True (no evidence).
        # A non-disc-ID pressing here is what historically tripped gate-2.
        orig_found = True
        orig_title = args.mb_album or album
        orig_year = rg_year
        notes.append(
            "Original: RG path accepted (R3 gate vacuous when mb_release_id "
            "is None, or passes for a true disc-ID pressing)."
        )

    # --- Render the menu summary lines ------------------------------------
    disc_year = year_of(release_date)
    album_year = disc_year if disc_year is not None else "unknown"

    print(f"rip engine      : {args.rip_engine}")
    print(f"  Album:    {album or '(none)'} ({album_year})")
    print(f"  {format_original(disc_year, orig_found, orig_title, orig_year)}")
    print(f"  Artist:   {artist or '(none)'}")
    print(f"  MCN:      {catalog or '(none)'}")
    n_isrc = sum(1 for v in track_isrcs.values() if v)
    print(f"  ISRCs:    {n_isrc}/{args.tracks} populated")
    print()
    print(f"release_date (Album year source) : {release_date or '(none)'}")
    print(f"mb_release_group_id              : {mb_release_group_id or '(none)'}")
    print(f"mb_release_id                    : {mb_release_id or '(none)'}")
    print(f"mb supplied album                : {mb_supplies_album}")
    print("\nprovenance notes:")
    for note in notes:
        print(f"  - {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
