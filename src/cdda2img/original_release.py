"""
original_release.py — best-effort lookup of an album's earliest release.

Populates ``disc.original_release_*`` fields on an :class:`RBIDisc` by:

1. **Primary**: MusicBrainz release-group lookup. When the disc carries an
   MB release-group MBID (from disc-ID prepop or Discogs cross-reference) and
   the group is an ``Album`` (without ``Compilation`` / ``Live`` / ``Remix``
   etc. secondary types), ``first-release-date`` and the RG title are the
   authoritative answer.

2. **Fallback**: title-fuzz against the artist's release-group catalogue via
   MB text search. Allow-list strips reissue tokens (Remastered / Anniversary
   Edition / Deluxe / etc.), deny-list rejects sibling-album false positives
   (Roman numerals, Vol.N, Live vs studio, re-recordings). Matches scored
   with ``rapidfuzz.fuzz.token_set_ratio`` at cutoff 88; earliest match wins.

The full research and rationale lives at
``private/research/incoming/original-release-detection.md``.
"""

from __future__ import annotations

import logging
import re
import unicodedata

from cdda2img.rbi_format import RBIDisc

log = logging.getLogger(__name__)

# Release-group secondary types that disqualify a release group as the
# "original release" of a studio album.  A disc whose release group is
# tagged Compilation/Live/Remix/etc. is itself a derivative work — the
# "earliest release date" of that release group is the earliest date of the
# derivative, not of the underlying songs.
_DERIVATIVE_SECONDARY_TYPES: frozenset[str] = frozenset({
    "Compilation",
    "Live",
    "Remix",
    "DJ-mix",
    "Mixtape/Street",
    "Demo",
    "Interview",
    "Audiobook",
    "Audio drama",
    "Spokenword",
})


def _parse_year(date_str: str | None) -> int | None:
    """Return the 4-digit year from an MB-style date, or None.

    Accepts ``YYYY``, ``YYYY-MM``, ``YYYY-MM-DD``, empty string, or None.
    """
    if not date_str:
        return None
    head = date_str[:4]
    if head.isdigit():
        return int(head)
    return None


def _fetch_release_group(rg_id: str):  # type: ignore[no-untyped-def]
    """Return the raw MB release-group dict for *rg_id*, or None on error.

    Centralised so the network/error handling is isolated from the logic.
    """
    import musicbrainzngs  # type: ignore[import-untyped]

    from cdda2img.mb_lookup import _setup_useragent

    _setup_useragent()
    try:
        result = musicbrainzngs.get_release_group_by_id(rg_id, includes=["releases"])
    except (musicbrainzngs.ResponseError, musicbrainzngs.NetworkError) as exc:
        log.debug("MB release-group lookup %s failed: %s", rg_id, exc)
        return None
    return result.get("release-group") or None


def _find_original_release_via_rg(
    disc: RBIDisc,
) -> tuple[bool, str | None, int | None]:
    """Primary path: MB release-group lookup by MBID."""
    rg_id = disc.mb_release_group_id
    if not rg_id:
        return (False, None, None)

    rg = _fetch_release_group(rg_id)
    if rg is None:
        return (False, None, None)

    # Reject derivative release groups (Compilation, Live, Remix, etc.) —
    # their "first release date" is the earliest derivative date, not the
    # underlying album's first appearance.
    secondary = set(rg.get("secondary-type-list") or [])
    if secondary & _DERIVATIVE_SECONDARY_TYPES:
        log.debug("RG %s rejected (secondary types: %s)", rg_id, sorted(secondary))
        return (False, None, None)

    first_date = rg.get("first-release-date") or ""
    year = _parse_year(first_date)
    if year is None:
        return (False, None, None)

    title = rg.get("title") or disc.album
    return (True, title, year)


def find_original_release(disc: RBIDisc) -> tuple[bool, str | None, int | None]:
    """Look up the earliest known release of *disc*'s album.

    Returns ``(found, title, year)``. ``found=True`` means we have a usable
    answer; the trio is always populated together. The display layer decides
    whether to render this as "This release ($year)" (when title+year match
    the disc itself) or "Original: $title ($year)" (when they differ).

    ``found=False`` means neither lookup path produced a usable answer —
    *not* a guarantee that no earlier release exists.

    Two-tier lookup:
      1. Primary — MB release-group via :func:`_find_original_release_via_rg`.
         Used when ``disc.mb_release_group_id`` is set and the group is a
         non-derivative album.
      2. Fallback — title-fuzz search via :func:`find_original_release_fuzzy`.
         Used when the primary path returns nothing.

    Side effects: none. Caller is responsible for assigning the result.
    """
    found, title, year = _find_original_release_via_rg(disc)
    if found:
        return (True, title, year)
    return find_original_release_fuzzy(disc)


def populate_original_release(disc: RBIDisc) -> None:
    """Convenience wrapper: call :func:`find_original_release` and assign to disc.

    Skips the lookup when the user has already set ``original_release_found``
    (e.g. via the metadata menu) — manual overrides win.
    """
    if disc.original_release_found:
        return
    found, title, year = find_original_release(disc)
    if found:
        disc.original_release_found = True
        disc.original_release_title = title
        disc.original_release_year = year


# ---------------------------------------------------------------------------
# Title-fuzz fallback (Phase 3b)
# ---------------------------------------------------------------------------
#
# Used when MB has no release-group ID for the disc — typically discs not in
# the MB disc-ID database, where only artist+title text search resolves.
#
# Algorithm and threshold come from:
#   private/research/incoming/original-release-detection.md §6


# Allow-list (longer-first to avoid "Deluxe" eating part of "Super Deluxe").
_REISSUE_ALLOWLIST: list[str] = [
    "super deluxe edition",
    "super deluxe",
    "deluxe edition",
    "deluxe",
    "expanded edition",
    "expanded",
    "special edition",
    "collector's edition",
    "collectors edition",
    "limited edition",
    "ltd. ed.",
    "ltd ed",
    "anniversary edition",
    "anniversary",
    "bonus track version",
    "bonus track edition",
    "bonus tracks",
    "remastered",
    "remaster",
    "reissue",
    "re-issue",
    "reissued",
    "repackage",
    "repackaged",
    "director's cut",
    "directors cut",
    "japanese version",
    "japan edition",
    "japanese edition",
    "uk version",
    "uk edition",
    "us version",
    "us edition",
    "european version",
    "european edition",
    "mono version",
    "stereo version",
    "mono",
    "stereo",
    "hdcd",
    "sacd hybrid",
    "sacd",
    "dvd-audio",
    "dvd-a",
    "blu-ray audio",
    "mfsl",
    "mfsl edition",
    "mobile fidelity",
    "original master recording",
    "ultradisc ii",
    "digipak",
    "digipack",
    "slipcase",
    "jewel case",
    "promo",
    "promotional",
    "hi-res",
    "24-bit remastered",
    "24-bit remaster",
    "24/96",
    "24/192",
    "16-bit",
]

_YEAR_QUALIFIER = re.compile(
    r"[\(\[]\s*(?:19|20)\d{2}\s+"
    r"(?:remaster(?:ed)?|reissue|mix|version|edition|remix|stereo|mono)"
    r"\s*[\)\]]",
    re.IGNORECASE,
)
_QUALIFIER_YEAR_FIRST = re.compile(
    r"[\(\[]\s*"
    r"(?:remaster(?:ed)?|reissue|mix|version|edition|remix|stereo|mono)"
    r"\s+(?:19|20)\d{2}\s*[\)\]]",
    re.IGNORECASE,
)
_DISC_TAG = re.compile(r"[\(\[]\s*(?:disc|cd|disk)\s+\d+\s*[\)\]]", re.IGNORECASE)

# Deny-list patterns.
_ROMAN = re.compile(r"\s+(X{0,3}(?:IX|IV|V?I{0,3}))$", re.IGNORECASE)
_ARABIC_SUFFIX = re.compile(r"\s+(\d{1,3})$")
_VOLUME = re.compile(
    r"[,\s]+(?:vol(?:ume)?\.?|pt\.?|part|chapter|chap\.?)\s+"
    r"(\d+|[IVXLCDM]+|one|two|three|four|five|six|seven|eight|nine|ten)\b",
    re.IGNORECASE,
)
_LIVE = re.compile(
    r"(?:^|\s|\()(?:live(?:\s+at|\s+in|\s+from)?|in\s+concert|"
    r"unplugged|acoustic\s+sessions?)\b",
    re.IGNORECASE,
)
_RE_RECORDING = re.compile(
    r"\((?:taylor's\s+version|re-?recorded(?:\s+\d{4})?|"
    r"new\s+(?:version|recording|master)|\d{4}\s+version)\)",
    re.IGNORECASE,
)

_FUZZ_SCORE_CUTOFF = 88


def _normalise_title(title: str) -> str:
    """Reduce an album title to its comparable stem (lowercase, allow-list stripped)."""
    s = unicodedata.normalize("NFKC", title)
    s = _YEAR_QUALIFIER.sub(" ", s)
    s = _QUALIFIER_YEAR_FIRST.sub(" ", s)
    s = _DISC_TAG.sub(" ", s)
    for token in _REISSUE_ALLOWLIST:
        # Allow the token after word-boundary or any of  ( [ - : ,
        pattern = re.compile(
            r"(?:^|[\s\(\[\-:,])" + re.escape(token) + r"(?=$|[\s\)\]\-:,])",
            re.IGNORECASE,
        )
        s = pattern.sub(" ", s)
    s = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", s)
    s = re.sub(r"^the\s+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip().lower()


_VOL_SPELLED = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}  # fmt: skip
_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


def _normalise_vol_token(tok: str) -> int | None:
    """Convert '2', 'II', 'Two' → 2; None if unparseable."""
    tok = tok.strip().lower()
    if tok in _VOL_SPELLED:
        return _VOL_SPELLED[tok]
    if tok.isdigit():
        return int(tok)
    if all(c in _ROMAN_VALUES for c in tok):
        n, prev = 0, 0
        for c in reversed(tok):
            v = _ROMAN_VALUES[c]
            n += v if v >= prev else -v
            prev = v
        return n
    return None


def _deny_match(disc_title: str, candidate_title: str) -> str | None:
    """Return reason-string if the pair must NOT be matched, else None."""
    if _RE_RECORDING.search(disc_title) or _RE_RECORDING.search(candidate_title):
        return "re-recording (treat as own original)"

    if bool(_LIVE.search(disc_title)) != bool(_LIVE.search(candidate_title)):
        return "live/studio mismatch"

    d_roman = _ROMAN.search(disc_title)
    c_roman = _ROMAN.search(candidate_title)
    if d_roman and c_roman and d_roman.group(1).upper() != c_roman.group(1).upper():
        return "different roman-numeral suffixes"
    if bool(d_roman) != bool(c_roman):
        return "asymmetric roman-numeral suffix"

    d_arab = _ARABIC_SUFFIX.search(disc_title)
    c_arab = _ARABIC_SUFFIX.search(candidate_title)
    if d_arab and c_arab and d_arab.group(1) != c_arab.group(1):
        return "different arabic-numeral suffixes"

    d_vol = _VOLUME.search(disc_title)
    c_vol = _VOLUME.search(candidate_title)
    if d_vol and c_vol:
        d_n = _normalise_vol_token(d_vol.group(1))
        c_n = _normalise_vol_token(c_vol.group(1))
        if d_n is not None and c_n is not None and d_n != c_n:
            return "different volume/part numbers"

    return None


def _best_fuzzy_match(
    disc_title: str, candidates: list[tuple[str, int]]
) -> tuple[str, int, float] | None:
    """Return earliest non-denied candidate scoring ≥ cutoff, else None.

    *candidates* is ``[(title, year), …]``. Scoring uses
    ``rapidfuzz.fuzz.token_set_ratio`` on the normalised titles.
    """
    from rapidfuzz import fuzz

    disc_norm = _normalise_title(disc_title)
    if not disc_norm:
        return None
    qualified: list[tuple[str, int, float]] = []
    for title, year in candidates:
        if _deny_match(disc_title, title):
            continue
        cand_norm = _normalise_title(title)
        if not cand_norm:
            continue
        score = fuzz.token_set_ratio(disc_norm, cand_norm)
        if score >= _FUZZ_SCORE_CUTOFF:
            qualified.append((title, year, score))
    if not qualified:
        return None
    # Earliest year wins; tie-break on highest score.
    qualified.sort(key=lambda t: (t[1], -t[2]))
    return qualified[0]


def _gather_artist_catalogue_via_mb(artist: str, album: str) -> list[tuple[str, int]]:
    """Best-effort artist catalogue fetch via MB text search.

    Returns a list of ``(title, year)`` from MB releases matching the
    artist+album query, deduped by release-group and sorted oldest-first.
    Returns empty list on lookup failure.
    """
    from cdda2img.mb_lookup import build_mb_search_query, search_releases

    query = build_mb_search_query(artist, album)
    try:
        releases = search_releases(query, limit=50)
    except Exception as exc:
        log.debug("MB artist-catalogue search failed for %r: %s", query, exc)
        return []

    seen_rg: set[str] = set()
    out: list[tuple[str, int]] = []
    for r in releases:
        rg_id = r.mb_release_group_id
        if rg_id and rg_id in seen_rg:
            continue
        if rg_id:
            seen_rg.add(rg_id)
        # Use original_release_date (release-group first-release-date) where
        # available; fall back to per-release date for stub results.
        year = _parse_year(r.original_release_date) or _parse_year(r.release_date)
        if year is None or not r.album:
            continue
        out.append((r.album, year))
    return out


def find_original_release_fuzzy(
    disc: RBIDisc,
) -> tuple[bool, str | None, int | None]:
    """Title-fuzz fallback for the case where MB has no release-group hit.

    Searches MB by artist+title text, normalises and scores candidates per
    the research-derived algorithm, returns the earliest match passing all
    deny-list rules with score ≥ cutoff.
    """
    if not (disc.artist and disc.album):
        return (False, None, None)
    catalogue = _gather_artist_catalogue_via_mb(disc.artist, disc.album)
    if not catalogue:
        return (False, None, None)
    hit = _best_fuzzy_match(disc.album, catalogue)
    if hit is None:
        return (False, None, None)
    title, year, _score = hit
    return (True, title, year)
