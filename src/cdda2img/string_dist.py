"""string_dist — pattern-weighted Levenshtein string distance.

Ported from beets/autotag/distance.py (MIT licence, beetbox/beets).
Adapted for cdda2img: uses rapidfuzz instead of jellyfish, and
unicodedata NFKD ASCII-fold instead of unidecode.

``string_dist(a, b)`` returns a float in [0, 1] where 0 means identical
and 1 means maximally different, with reduced weight for common
innocuous differences (leading articles, "(feat. ...)", "(EP)", etc.).
"""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz.distance import Levenshtein

# Words that can appear at the end of a title via "Title, The" → normalised
# back to "The Title" before comparison.
_SD_END_WORDS = ("the", "a", "an")

# (pattern, weight): portions matching *pattern* are removed from both
# strings before the Levenshtein is applied, contributing *weight* of
# the resulting distance improvement rather than the full 1.0. A weight
# of 0.0 means the matched portion is completely ignored.
_SD_PATTERNS = (
    (r"^the ", 0.1),
    (r"[\[\(]?(ep|single)[\]\)]?", 0.0),
    (r"[\[\(]?(featuring|feat|ft)[\. :].+", 0.1),
    (r"\(.*?\)", 0.3),
    (r"\[.*?\]", 0.3),
    (r"(, )?(pt\.|part) .+", 0.2),
)

# Simple substitutions applied before pattern processing.
_SD_REPLACE = ((r"&", "and"),)


def _ascii_fold(s: str) -> str:
    """NFKD decompose + drop non-ASCII — adequate for Latin-script titles."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


def _string_dist_basic(s1: str, s2: str) -> float:
    """Normalized Levenshtein on stripped, lowercased, ASCII-folded strings.

    Non-alphanumeric characters are removed so punctuation differences
    (apostrophes, hyphens, accents) do not dominate the score.
    Returns 0.0 when both strings are empty after stripping.
    """
    s1 = re.sub(r"[^a-z0-9]", "", _ascii_fold(s1).lower())
    s2 = re.sub(r"[^a-z0-9]", "", _ascii_fold(s2).lower())
    if not s1 and not s2:
        return 0.0
    return Levenshtein.normalized_distance(s1, s2)


def string_dist(s1: str | None, s2: str | None) -> float:
    """Return an intuitive edit distance (0=identical, 1=maximally different).

    Handles common innocuous differences between music metadata sources:
    - Leading articles ("The", "A", "An") moved to end via comma convention
    - "& " → "and "
    - "(feat. ...)", "(EP)", "(Pt. N)", parenthetical remarks: reduced weight
    - Unicode/ASCII normalisation and case folding

    Either argument being None is treated as maximally different from a
    non-None value; two None arguments score 0.0 (both unknown → agree).
    """
    if s1 is None and s2 is None:
        return 0.0
    if s1 is None or s2 is None:
        return 1.0

    s1 = s1.lower()
    s2 = s2.lower()

    # "Something, The" → "the something" (undo end-word convention)
    for word in _SD_END_WORDS:
        suffix = f", {word}"
        if s1.endswith(suffix):
            s1 = f"{word} {s1[: -len(suffix)]}"
        if s2.endswith(suffix):
            s2 = f"{word} {s2[: -len(suffix)]}"

    for pat, repl in _SD_REPLACE:
        s1 = re.sub(pat, repl, s1)
        s2 = re.sub(pat, repl, s2)

    # For each SD_PATTERN: if stripping it from both strings reduces the
    # Levenshtein distance, accept the reduction at *weight* of its value.
    # Accumulate partial penalties and walk both strings down in parallel.
    base_dist = _string_dist_basic(s1, s2)
    penalty = 0.0
    for pat, weight in _SD_PATTERNS:
        c1 = re.sub(pat, "", s1)
        c2 = re.sub(pat, "", s2)
        if c1 == s1 and c2 == s2:
            continue  # pattern not present in either string
        case_dist = _string_dist_basic(c1, c2)
        delta = max(0.0, base_dist - case_dist)
        if delta == 0.0:
            continue
        s1, s2 = c1, c2
        base_dist = case_dist
        penalty += weight * delta

    return base_dist + penalty
