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
from typing import TYPE_CHECKING

from cdda2img.rbi_format import RBIDisc

if TYPE_CHECKING:
    from cdda2img.lookup_result import DiscMeta

log = logging.getLogger(__name__)

# R3: tolerances for the track-set / runtime verifier. Duration is gated on the
# SUM across all tracks, not per-track: MB recording lengths and disc frame
# durations routinely differ by tens of ms per track (rounding, pre-gap
# accounting), so a tight per-track gate would false-reject correct matches —
# the sum absorbs that noise while still catching a genuinely different
# tracklist. (A dead _R3_PER_TRACK_TOLERANCE_MS constant lived here; F-005.)
_R3_SUM_DURATION_TOLERANCE_MS = 2_000  # ±2 seconds across all tracks
_R3_TITLE_FUZZ_CUTOFF = 80  # aggregate token_set_ratio across the tracklist

# R14: pre-emphasis ≈ early 1980s. After 1986 it's extremely rare in
# new releases — a candidate year above this with disc.pre_emphasis=True
# is almost certainly the wrong identification.
_R14_PRE_EMPH_YEAR_CAP = 1986

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


# ---------------------------------------------------------------------------
# R3 — Track-set / runtime verification
# ---------------------------------------------------------------------------


def _verify_release_matches_disc(
    meta: DiscMeta, disc: RBIDisc, *, check_durations: bool = True
) -> bool:
    """R3: conjunctive verifier — does *meta* plausibly describe *disc*?

    Four gates, all skip-on-no-evidence (innocent until proven guilty):

    * **Track count** — exact match required when both sides have ≥1 track.
      Empty-on-either-side skips (can't compare what isn't there). This is
      the strictest single signal — a track-count mismatch is positive
      evidence that we're looking at the wrong release.
    * **Sum-of-durations** — within ±2 s when both sides have lengths.
      Catches structural mismatches (different versions, extra/missing
      track) even when track counts agree. Only valid when *meta* is the
      disc's **own** pressing (track.length agrees with the physical TOC to
      within rounding); across *different* pressings of the same album the
      per-track lengths legitimately drift by tens of seconds, so callers
      comparing the disc against an arbitrary release in a group must pass
      ``check_durations=False`` (see :func:`find_original_release_fuzzy`).
      Disabling it is safe because it is the only pressing-*specific* gate —
      track count, ISRC, and title are all pressing-stable.
    * **Per-track ISRC** — agreement at matching track numbers when both
      sides have ISRCs. Reuses R1's helper. Positive evidence of identity
      when present; absent ISRCs skip.
    * **Per-track title fuzzy** — aggregate token_set_ratio ≥ 80 across
      tracks where both sides have titles. Skip when either side has no
      titles or no overlap.

    Returns True iff no gate produces positive evidence of a mismatch.
    "Confidence over coverage": when in doubt, accept (the upstream
    identification was probably right). When we have a hard contradiction,
    reject.
    """
    from cdda2img.mb_lookup import _score_candidate_by_isrcs

    # Gate 1: track count. Only fires when both sides have tracks.
    if disc.tracks and meta.tracks and len(disc.tracks) != len(meta.tracks):
        log.debug(
            "R3 reject (track count): disc has %d tracks, meta has %d (%s)",
            len(disc.tracks),
            len(meta.tracks),
            meta.mb_release_id or "<no MBID>",
        )
        return False

    # Gate 2: sum-of-durations within ±2 s. Frame_to_ms via 1000/75 ≈ 13.33.
    # Pressing-specific — skipped when verifying a cross-pressing candidate.
    disc_sum_ms = sum(
        int(t.duration_frames * 1000 / 75) for t in disc.tracks if t.duration_frames
    )
    meta_sum_ms = sum(t.duration_ms or 0 for t in meta.tracks if t.duration_ms)
    if check_durations and disc_sum_ms > 0 and meta_sum_ms > 0:
        diff = abs(disc_sum_ms - meta_sum_ms)
        if diff > _R3_SUM_DURATION_TOLERANCE_MS:
            log.debug(
                "R3 reject (sum durations): disc %.1fs, meta %.1fs (%s)",
                disc_sum_ms / 1000,
                meta_sum_ms / 1000,
                meta.mb_release_id or "<no MBID>",
            )
            return False

    # Gate 3: ISRC overlap. Use R1's helper. Score == 0 means either side
    # had no ISRCs, or none agreed — only reject when we have ISRCs on
    # both sides AND none agreed (strong contradiction).
    disc_isrc_count = sum(1 for t in disc.tracks if t.isrc)
    meta_isrc_count = sum(1 for t in meta.tracks if t.isrc)
    if disc_isrc_count >= 2 and meta_isrc_count >= 2:
        score = _score_candidate_by_isrcs(meta, disc)
        if score == 0:
            log.debug(
                "R3 reject (ISRC overlap): %d / %d disc ISRCs scored zero "
                "against meta (%s)",
                disc_isrc_count,
                meta_isrc_count,
                meta.mb_release_id or "<no MBID>",
            )
            return False

    # Gate 4: aggregate title fuzzy ≥ _R3_TITLE_FUZZ_CUTOFF. Skip when either
    # side has no titles for ≥2 tracks (need at least some overlap to score).
    title_score = _aggregate_title_fuzz_score(meta, disc)
    if title_score is not None and title_score < _R3_TITLE_FUZZ_CUTOFF:
        log.debug(
            "R3 reject (title fuzz): aggregate score %d < %d (%s)",
            title_score,
            _R3_TITLE_FUZZ_CUTOFF,
            meta.mb_release_id or "<no MBID>",
        )
        return False

    return True


def _aggregate_title_fuzz_score(meta: DiscMeta, disc: RBIDisc) -> int | None:
    """Mean token_set_ratio across paired track titles, or None on no overlap.

    Pairs by track number. Only scores pairs where both sides have a title;
    returns None when fewer than 2 paired titles exist (insufficient signal).
    """
    from rapidfuzz import fuzz

    meta_titles = {t.number: t.title for t in meta.tracks if t.number is not None}
    pairs: list[int] = []
    for entry in disc.tracks:
        if not entry.title:
            continue
        mt = meta_titles.get(entry.track_number)
        if not mt:
            continue
        pairs.append(int(fuzz.token_set_ratio(entry.title.lower(), mt.lower())))
    if len(pairs) < 2:
        return None
    return sum(pairs) // len(pairs)


def _verify_rg_path_for_disc(
    disc: RBIDisc, verify_meta: DiscMeta | None = None
) -> bool:
    """R3 gate for the RG-primary path.

    Fast path: verify the disc's own MB release MBID (set by MB disc-ID
    prepop) against the disc. The RG identification came from this MBID
    — if the disc verifies against it, the RG is correct. If it doesn't,
    the RG identification was wrong upstream; fall through to fuzzy.

    When ``disc.mb_release_id`` is unset or the MB fetch fails, return
    True (no evidence to reject — the RG answer stands). Network failure
    is not evidence of mismatch.

    P1: *verify_meta* is the already-parsed ``DiscMeta`` from the disc-ID
    prepop. When it is the release this gate would otherwise re-fetch (same
    ``mb_release_id``), it is used directly — saving one MB round-trip at the
    1 req/s rate limit. Any mismatch (or absence) falls back to the live fetch,
    so correctness never depends on the caller threading it.
    """
    if disc.mb_release_id is None:
        return True
    if verify_meta is not None and verify_meta.mb_release_id == disc.mb_release_id:
        meta: DiscMeta | None = verify_meta
    else:
        from cdda2img.mb_lookup import lookup_release

        meta = lookup_release(disc.mb_release_id, disc_number=disc.disc_number)
    if meta is None:
        return True
    return _verify_release_matches_disc(meta, disc)


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


def _resolve_rg_original(
    rg_id: str, disc: RBIDisc
) -> tuple[str | None, int | None] | None:
    """Resolve ``(original title, original year)`` from an MB release group.

    Shared by both lookup paths: the RG-primary path and the title-fuzz
    fallback both need the release group's ``first-release-date`` to answer
    "what is the *original* release", but they discover *rg_id* differently
    (disc-ID prepop vs. text search). Centralising this is also what fixes the
    fuzzy path: MB text-search stubs never carry the release-group
    first-release-date, so the year MUST come from a release-group fetch, not
    the stub's ``original_release_date`` (which is always ``None`` there).

    Returns ``None`` when the group is unusable as an original — a derivative
    secondary type (Compilation / Live / Remix / …), an unparseable
    first-release date, or an R14 pre-emphasis year-cap violation.

    Does NOT verify the disc tracklist against a release — that gate belongs to
    the caller, because the two paths verify *different* releases (the disc's
    own MBID vs. each fuzzy candidate's fetched release).
    """
    rg = _fetch_release_group(rg_id)
    if rg is None:
        return None

    # Reject derivative release groups (Compilation, Live, Remix, etc.) —
    # their "first release date" is the earliest derivative date, not the
    # underlying album's first appearance.
    secondary = set(rg.get("secondary-type-list") or [])
    if secondary & _DERIVATIVE_SECONDARY_TYPES:
        log.debug("RG %s rejected (secondary types: %s)", rg_id, sorted(secondary))
        return None

    year = _parse_year(rg.get("first-release-date") or "")
    if year is None:
        return None

    # R14: pre-emphasis effectively died with the early-80s catalogue.
    # When the disc has pre-emphasis but the RG says > 1986, the
    # identification is almost certainly wrong (RG describes a digital-only
    # reissue that wouldn't carry pre-emphasis).
    if disc.pre_emphasis is True and year > _R14_PRE_EMPH_YEAR_CAP:
        log.debug(
            "R14 reject RG: disc has PRE_EMPHASIS but RG year %d > %d (%s)",
            year,
            _R14_PRE_EMPH_YEAR_CAP,
            rg_id,
        )
        return None

    return (rg.get("title") or disc.album, year)


def _find_original_release_via_rg(
    disc: RBIDisc, verify_meta: DiscMeta | None = None
) -> tuple[bool, str | None, int | None]:
    """Primary path: MB release-group lookup by the disc's own MBID.

    R3: verifies the disc tracklist matches the disc's own MB release (via
    ``_verify_rg_path_for_disc``) before accepting the RG first-release-date.
    A mismatch indicates the RG identification was wrong upstream (e.g. a
    different disc with similar TOC matched the same MB disc-ID); fall through
    to fuzzy.
    """
    rg_id = disc.mb_release_group_id
    if not rg_id:
        return (False, None, None)
    resolved = _resolve_rg_original(rg_id, disc)
    if resolved is None:
        return (False, None, None)
    # R3: gate the RG identification against the disc tracklist.
    if not _verify_rg_path_for_disc(disc, verify_meta):
        return (False, None, None)
    title, year = resolved
    return (True, title, year)


def find_original_release(
    disc: RBIDisc, verify_meta: DiscMeta | None = None
) -> tuple[bool, str | None, int | None]:
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
    found, title, year = _find_original_release_via_rg(disc, verify_meta)
    if found:
        return (True, title, year)
    return find_original_release_fuzzy(disc)


def populate_original_release(
    disc: RBIDisc, verify_meta: DiscMeta | None = None
) -> None:
    """Convenience wrapper: call :func:`find_original_release` and assign to disc.

    Skips the lookup when the user has already set ``original_release_found``
    (e.g. via the metadata menu) — manual overrides win.

    P1: *verify_meta* (the disc-ID prepop ``DiscMeta``) is threaded to the RG
    verify so it does not re-fetch the disc's own release; see
    :func:`_verify_rg_path_for_disc`.
    """
    if disc.original_release_found:
        return
    found, title, year = find_original_release(disc, verify_meta)
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


def _gather_artist_catalogue_metas_via_mb(artist: str, album: str) -> list[DiscMeta]:
    """R3 fuzzy-path helper: returns *DiscMeta* objects (preserves release IDs).

    Deduped by release-group, sorted by release date ascending (a *proxy*:
    MB text-search stubs do not carry the release-group ``first-release-date``,
    so the true original year is resolved per-candidate from the release group
    in :func:`find_original_release_fuzzy` — not here). An album title is
    required; the per-stub ``original_release_date`` deliberately is **not**
    (it is structurally always ``None`` on the text-search path, so filtering
    on it discarded every candidate and made the whole fuzzy path dead code).
    """
    from cdda2img.mb_lookup import build_mb_search_query, search_releases

    query = build_mb_search_query(artist, album)
    try:
        releases = search_releases(query, limit=50)
    except Exception as exc:
        log.debug("MB artist-catalogue search failed for %r: %s", query, exc)
        return []

    seen_rg: set[str] = set()
    out: list[DiscMeta] = []
    for r in releases:
        if not r.album:
            continue
        rg_id = r.mb_release_group_id
        if rg_id and rg_id in seen_rg:
            continue
        if rg_id:
            seen_rg.add(rg_id)
        out.append(r)
    out.sort(key=lambda m: m.release_date or "9999")
    return out


def _qualified_fuzzy_candidates(
    disc_title: str, metas: list[DiscMeta]
) -> list[DiscMeta]:
    """Return *metas* that pass the deny-list + fuzzy-cutoff, sorted by year then score.

    Returns *every* qualifying candidate (not just the top pick) so the caller
    can iterate and verify each via ``_verify_release_matches_disc`` until one
    passes — the R3 fuzzy path.
    """
    from rapidfuzz import fuzz

    disc_norm = _normalise_title(disc_title)
    if not disc_norm:
        return []
    qualified: list[tuple[int, float, DiscMeta]] = []
    for meta in metas:
        title = meta.album
        if not title:
            continue
        if _deny_match(disc_title, title):
            continue
        cand_norm = _normalise_title(title)
        if not cand_norm:
            continue
        score = fuzz.token_set_ratio(disc_norm, cand_norm)
        if score < _FUZZ_SCORE_CUTOFF:
            continue
        year = _parse_year(meta.original_release_date) or 9999
        qualified.append((year, score, meta))
    qualified.sort(key=lambda t: (t[0], -t[1]))
    return [m for _y, _s, m in qualified]


def find_original_release_fuzzy(
    disc: RBIDisc,
) -> tuple[bool, str | None, int | None]:
    """Title-fuzz fallback for the case where MB has no release-group hit.

    Searches MB by artist+title text, normalises and scores candidates per
    the research-derived algorithm, returns the earliest match passing all
    deny-list rules with score ≥ cutoff *and* (R3) ``_verify_release_matches_disc``
    against the disc tracklist. Each candidate verification costs one MB
    release lookup; the loop stops at the first verified candidate.
    """
    if not (disc.artist and disc.album):
        return (False, None, None)
    metas = _gather_artist_catalogue_metas_via_mb(disc.artist, disc.album)
    if not metas:
        return (False, None, None)
    qualified = _qualified_fuzzy_candidates(disc.album, metas)
    if not qualified:
        return (False, None, None)

    from cdda2img.mb_lookup import lookup_release

    for meta in qualified:
        rg_id = meta.mb_release_group_id
        if not rg_id:
            # No release group → no way to the *original* year. The stub's own
            # date is the matched pressing's, not the album's first release.
            continue
        # The original title+year come from the release GROUP, not the search
        # stub (whose ``original_release_date`` is always None here). This also
        # applies the derivative-secondary-type and R14 pre-emphasis gates.
        resolved = _resolve_rg_original(rg_id, disc)
        if resolved is None:
            continue
        title, year = resolved
        # R3: fetch the candidate release and verify against the disc.
        # Stub metadata from search_releases lacks per-track durations /
        # ISRCs / titles, so the verifier would skip every gate; only the
        # fetched release has enough data to gate meaningfully.
        #
        # check_durations=False: this release is an *arbitrary* pressing of
        # the group, not the disc's own disc-ID-matched pressing, so its
        # per-track lengths drift from the disc by tens of seconds (the same
        # cross-pressing drift ef428fc identified). Track count + ISRC +
        # per-track title (all pressing-stable) still gate the match.
        if meta.mb_release_id is None:
            # No way to fetch full release data → can't verify → trust the
            # candidate (skip-on-no-evidence applies at the meta-level too).
            return (True, title, year)
        full = lookup_release(meta.mb_release_id, disc_number=disc.disc_number)
        if full is None:
            # Network failure ≠ evidence of mismatch — accept.
            return (True, title, year)
        if _verify_release_matches_disc(full, disc, check_durations=False):
            return (True, title, year)
    return (False, None, None)
