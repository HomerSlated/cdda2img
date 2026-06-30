"""
mb_lookup.py — MusicBrainz metadata lookups.

Provides disc ID computation from RBI TOC data and network lookups via musicbrainzngs.
The disc ID is computed in pure Python (no native libdiscid required), per the
MusicBrainz spec at https://musicbrainz.org/doc/Disc_ID_Calculation:

  1. Build an 804-character ASCII string concatenating:
       - first_track  as 2-char  uppercase hex
       - last_track   as 2-char  uppercase hex
       - lead_out_lba as 8-char  uppercase hex (zero-padded)
       - 99 x track_lba as 8-char uppercase hex each (zero-padded; unused slots = 0)
  2. SHA-1 over those 804 ASCII bytes.
  3. Base64-encode with the URL-safe variant: '+'→'.', '/'→'_', '='→'-'.

Critically, the hash input is the *ASCII hex text*, not the raw binary integers.
A SHA-1 over raw 402-byte binary input produces a valid-looking but incorrect
disc-ID that MB will never match (silent failure mode — burned us once already).
"""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import logging
import math
import re
from collections import Counter
from dataclasses import replace
from typing import NamedTuple

import musicbrainzngs  # type: ignore[import-untyped]

from cdda2img.lookup_result import (
    DiscMeta,
    TrackMeta,
)
from cdda2img.rbi_format import CD_FRAMES_PER_SECOND, RBIDisc, RBITocEntry

log = logging.getLogger(__name__)

_LEAD_IN_SECTORS = 150  # standard 2-second Red Book lead-in


_useragent_set = False


def _setup_useragent() -> None:
    global _useragent_set
    if _useragent_set:
        return
    from cdda2img.config import load_config

    cfg = load_config()
    musicbrainzngs.set_useragent(
        "cdda2img",
        importlib.metadata.version("cdda2img"),
        cfg.contact_email or None,
    )
    # R15: pin the rate limit explicitly so the project does not silently
    # inherit a future library default change. MB's documented limit is
    # 1 request per second per host.
    musicbrainzngs.set_rate_limit(limit_or_interval=1.0, new_requests=1)
    _useragent_set = True


# ---------------------------------------------------------------------------
# Disc ID computation
# ---------------------------------------------------------------------------


def compute_disc_id(
    first_track: int,
    last_track: int,
    track_offsets: list[int],
    lead_out_offset: int,
) -> str:
    """Compute a MusicBrainz Disc ID from TOC data.

    All offsets are absolute LBA sectors; track 1 conventionally starts at 150.
    *track_offsets* must be in track-number order; up to 99 entries.

    The hash input is an 804-char uppercase-hex ASCII string (NOT raw bytes) —
    see this module's docstring for the full spec. Verified against libdiscid
    output for ZZ Top *Eliminator* (EU 1983, MB pressing nRQLbh4...).

    Raises ValueError on inputs the fixed-width hex format cannot represent
    faithfully: track numbers outside 1..99, more than 99 offsets, or any
    negative offset (a negative ``{:08X}`` emits a sign and breaks the 804-char
    layout, silently producing a wrong — but plausible — disc ID; F-007).
    """
    if not 1 <= first_track <= 99 or not 1 <= last_track <= 99:
        msg = f"track numbers must be in 1..99, got {first_track}..{last_track}"
        raise ValueError(msg)
    if len(track_offsets) > 99:
        msg = f"at most 99 track offsets supported, got {len(track_offsets)}"
        raise ValueError(msg)
    if lead_out_offset < 0 or any(o < 0 for o in track_offsets):
        msg = "track and lead-out offsets must be non-negative"
        raise ValueError(msg)
    parts = [f"{first_track:02X}", f"{last_track:02X}", f"{lead_out_offset:08X}"]
    for i in range(99):
        parts.append(f"{(track_offsets[i] if i < len(track_offsets) else 0):08X}")
    sha1 = hashlib.sha1("".join(parts).encode("ascii")).digest()  # noqa: S324
    b64 = base64.b64encode(sha1).decode("ascii")
    return b64.replace("+", ".").replace("/", "_").replace("=", "-")


def disc_id_from_rbi(disc: RBIDisc) -> str | None:
    """Compute the MusicBrainz Disc ID for an RBIDisc, or None if no tracks."""
    if not disc.tracks:
        return None
    tracks = sorted(disc.tracks, key=lambda t: t.track_number)
    # INDEX 01 (audio start) per track = start_frame + pregap_frames + lead-in
    offsets = [t.start_frame + t.pregap_frames + _LEAD_IN_SECTORS for t in tracks]
    lead_out = disc.total_frames + _LEAD_IN_SECTORS
    return compute_disc_id(
        first_track=tracks[0].track_number,
        last_track=tracks[-1].track_number,
        track_offsets=offsets,
        lead_out_offset=lead_out,
    )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_year(date_str: str | None) -> int | None:
    if not date_str:
        return None
    try:
        return int(str(date_str)[:4])
    except (ValueError, IndexError):
        return None


def _artist_credit_name(artist_credits: list) -> str:
    parts: list[str] = []
    for credit in artist_credits:
        if isinstance(credit, dict):
            parts.append(credit.get("artist", {}).get("name", ""))
            parts.append(credit.get("joinphrase", ""))
    return "".join(parts).strip()


def _find_disc_medium(medium_list: list[dict], disc_id: str) -> dict | None:
    """Return the first medium whose disc-list contains *disc_id*, or None."""
    for medium in medium_list:
        for d in medium.get("disc-list") or []:
            if d.get("id") == disc_id:
                return medium
    return None


def _select_medium_by_position(
    medium_list: list[dict], disc_number: int
) -> dict | None:
    """Return the medium at *disc_number* (1-based position), or None."""
    for medium in medium_list:
        if int(medium.get("position") or 0) == disc_number:
            return medium
    return None


def _resolve_matched_mediums(
    medium_list: list[dict],
    album_title: str | None,
    disc_id: str | None,
    disc_number_hint: int | None,
) -> tuple[list[dict], int | None, str | None, str | None]:
    """Return (mediums, disc_number, album_title, set_title) for the best-matching medium.

    Selects by disc-id when available, then by position hint, then falls back to
    returning all mediums flat (single-disc releases or stub results).
    """
    if disc_id:
        matched = _find_disc_medium(medium_list, disc_id)
    elif disc_number_hint is not None:
        matched = _select_medium_by_position(medium_list, disc_number_hint)
    else:
        return medium_list, None, album_title, None

    if matched is None:
        return medium_list, None, album_title, None

    pos = int(matched.get("position") or 0)
    disc_number = pos or None
    medium_title = matched.get("title") or ""
    set_title: str | None = None
    if medium_title and medium_title != album_title:
        set_title = album_title
        album_title = medium_title
    return [matched], disc_number, album_title, set_title


def _parse_track_number(track: dict) -> int | None:
    """Return the 1-based track number from a MB track dict.

    Falls back to sequential position for vinyl-style labels (A1, B2, …).
    """
    try:
        return int(track.get("number") or 0) or None
    except (ValueError, TypeError):
        pass
    try:
        return int(track.get("position") or 0) or None
    except (ValueError, TypeError):
        return None


def _parse_release(
    release: dict,
    _disc_id: str | None = None,
    _disc_number: int | None = None,
) -> DiscMeta:
    """Parse a MusicBrainz release dict into a DiscMeta.

    *disc_total* (medium-count) is always populated when the release has
    multiple mediums — it is a property of the release, not the disc match.

    When *_disc_id* is provided (disc-ID lookup path), the matching medium is
    located by walking medium-list/disc-list.  When *_disc_number* is provided
    (text-search path on a known multi-disc release), the medium at that
    1-based position is selected instead.  Without either, all mediums are
    flattened — suitable for single-disc releases or stub results.
    """
    artist = _artist_credit_name(release.get("artist-credit", []))
    date = release.get("date") or ""
    rg = release.get("release-group") or {}
    original_date = rg.get("first-release-date") or ""

    label_infos = release.get("label-info-list") or []
    label = ""
    catalog_number = ""
    if label_infos:
        first = label_infos[0]
        if first.get("label"):
            label = first["label"].get("name", "")
        catalog_number = first.get("catalog-number") or ""

    medium_list = release.get("medium-list") or []
    total = int(release.get("medium-count") or 0)
    disc_total: int | None = total or None
    album_title: str | None = release.get("title") or None
    matched_mediums, disc_number, album_title, set_title = _resolve_matched_mediums(
        medium_list, album_title, _disc_id, _disc_number
    )

    from cdda2img.validators import validate_isrc

    tracks: list[TrackMeta] = []
    for medium in matched_mediums:
        for track in medium.get("track-list") or []:
            recording = track.get("recording") or {}
            isrc_list = recording.get("isrc-list") or []
            # Per-medium track length (TOC-derived) — NOT recording.length.
            # track.length is set from the TOC of the disc used to add this
            # release, so for a disc-ID-matched release it agrees with the
            # physical TOC to within rounding/pregap; recording.length is a
            # shared canonical value that can come from a different pressing
            # and is off by seconds (R3 sum-of-durations false-reject source).
            # No fallback to recording.length: a missing track.length leaves
            # duration_ms=None so the R3 gate skips on no-evidence rather than
            # comparing against the wrong quantity.
            length = track.get("length")
            # R13: structure-check each ISRC at MB ingress; malformed entries
            # are dropped (with a WARNING log) rather than propagated through
            # the rest of the pipeline.
            isrc = validate_isrc(isrc_list[0]) if isrc_list else None
            tracks.append(
                TrackMeta(
                    number=_parse_track_number(track),
                    title=track.get("title") or recording.get("title"),
                    performer=artist or None,
                    isrc=isrc,
                    duration_ms=int(length) if length else None,
                )
            )

    from cdda2img.barcode import normalize_barcode

    raw_barcode = release.get("barcode") or ""
    barcode = normalize_barcode(raw_barcode)
    if raw_barcode and not barcode:
        log.info(
            "MB release %r: raw barcode %r failed normalisation (dropped)",
            release.get("id"),
            raw_barcode,
        )

    return DiscMeta(
        album=album_title,
        artist=artist or None,
        barcode=barcode,
        mb_release_id=release.get("id") or None,
        mb_release_group_id=rg.get("id") or None,
        release_date=date or None,
        original_release_date=original_date or None,
        country=release.get("country") or None,
        label=label or None,
        catalog_number=catalog_number or None,
        primary_type=rg.get("primary-type") or rg.get("type") or None,
        track_count=(
            len(tracks)
            if tracks
            else (
                int(release["medium-track-count"])
                if release.get("medium-track-count")
                else None
            )
        ),
        disc_number=disc_number,
        disc_total=disc_total,
        set_title=set_title,
        source="musicbrainz",
        tracks=tracks,
    )


# ---------------------------------------------------------------------------
# Network lookups
# ---------------------------------------------------------------------------


# OPT-1: process-lifetime cache for MB disc-ID lookups. The pre-rip banner
# (_preview_worker) and finalization (prepopulate_from_mb) query the *same* disc-ID
# seconds apart; this dedups that round-trip. Only **definitive** answers are cached
# — a successful response, including a legitimate empty list / 404 "not in MB".
# Transient network/HTTP errors are never cached, so a blip can't poison the second
# call. No TTL; discarded on process exit. Clearable via _DISC_ID_CACHE.clear().
_DISC_ID_CACHE: dict[str, list[DiscMeta]] = {}


def lookup_disc_id(disc: RBIDisc) -> list[DiscMeta]:
    """Look up releases on MusicBrainz by Disc ID computed from the disc TOC.

    Returns a list of matching DiscMeta (empty on no match or network error).
    """
    disc_id_str = disc_id_from_rbi(disc)
    if not disc_id_str:
        return []
    cached = _DISC_ID_CACHE.get(disc_id_str)
    if cached is not None:
        log.debug("MusicBrainz disc ID lookup: %s (cached)", disc_id_str)
        return cached
    _setup_useragent()
    log.debug("MusicBrainz disc ID lookup: %s", disc_id_str)
    try:
        result = musicbrainzngs.get_releases_by_discid(
            disc_id_str,
            # NB: do NOT add "discids" here. It is a valid include on the
            # /release endpoint, but the /discid endpoint rejects it with HTTP
            # 400 Bad Request — which the except-clause below then swallowed as
            # "no match", silently breaking *every* disc-ID lookup and forcing
            # the whole pipeline onto the CDDB fallback (this was the F-003
            # regression). The matching medium's disc-list is populated by the
            # /discid endpoint regardless (we are querying *by* disc id), so
            # _find_disc_medium still selects the right medium on multi-disc
            # releases without it.
            includes=[
                "artists",
                "recordings",
                "release-groups",
                "labels",
                "isrcs",
            ],
        )
    except musicbrainzngs.ResponseError as exc:
        # A 404 is a legitimate "disc not in MB". Anything else (e.g. a 400 from
        # a bad include, a 5xx) is NOT a real negative — surface it loudly so a
        # request-shape regression can never again masquerade as "no match".
        code = getattr(getattr(exc, "cause", None), "code", None)
        if code == 404:
            log.debug("MusicBrainz disc ID %s not found (404)", disc_id_str)
            _DISC_ID_CACHE[disc_id_str] = []  # legitimate negative — cache it
            return []
        log.warning(
            "MusicBrainz disc ID lookup error (HTTP %s) — treating as no "
            "match, but this is not a clean negative: %s",
            code,
            exc,
        )
        return []  # transient/unknown error — do NOT cache
    except musicbrainzngs.NetworkError as exc:
        log.debug("MusicBrainz network error: %s", exc)
        return []  # transient — do NOT cache
    releases = (result.get("disc") or {}).get("release-list") or []
    parsed = [_parse_release(r, _disc_id=disc_id_str) for r in releases]
    _DISC_ID_CACHE[disc_id_str] = parsed
    return parsed


def _fetch_release_raw(release_id: str) -> dict | None:
    """Fetch a single MusicBrainz release by ID and return the **raw** release
    dict (track-list with both per-track ``length`` and ``recording.length``).

    ``lookup_release`` parses this into a ``DiscMeta`` and deliberately drops
    ``recording.length`` (see ``_parse_release``). The stage-7 duration matcher
    needs the raw form so it can read ``recording.length`` self-contained,
    without that canonical-but-noisy value ever reaching ``TrackMeta.duration_ms``
    or the R3 sum-of-durations gate.

    Returns None on network/response error.
    """
    _setup_useragent()
    log.debug("MusicBrainz release lookup: %s", release_id)
    try:
        result = musicbrainzngs.get_release_by_id(
            release_id,
            includes=["artists", "recordings", "release-groups", "labels", "isrcs"],
        )
    except (musicbrainzngs.ResponseError, musicbrainzngs.NetworkError) as exc:
        log.debug("MusicBrainz release lookup for %s failed: %s", release_id, exc)
        return None
    return result.get("release") or None


def lookup_release(release_id: str, disc_number: int | None = None) -> DiscMeta | None:
    """Fetch a single MusicBrainz release by ID with full track listing.

    *disc_number* selects the matching medium for multi-disc releases so that
    only that disc's tracks are returned.  Pass ``disc.disc_number`` when
    applying to a known disc in a set.

    Returns None on network/response error.
    """
    release = _fetch_release_raw(release_id)
    if not release:
        return None
    return _parse_release(release, _disc_number=disc_number)


_DISCOGS_RELEASE_RE = re.compile(r"/release/(\d+)")


def discogs_link_and_barcode(release_id: str) -> tuple[int | None, str | None]:
    """Return ``(discogs_release_id, mb_barcode)`` for an MB release.

    One ``inc=url-rels`` fetch yields both halves of the §10.3.1 cross-source
    barcode check: the release's own (normalised) ``barcode`` is a top-level
    field returned regardless of includes, and the ``discogs`` url-relation's
    target URL carries the linked Discogs release id (``…/release/<id>``).

    Returns ``(None, ...)`` when there is no Discogs *release* link (a master /
    artist / label URL has no ``/release/<id>`` and is skipped), and
    ``(..., None)`` when MB has no barcode. ``(None, None)`` on network/response
    error. Callers compare the two barcodes only when both ids are present.
    """
    _setup_useragent()
    log.debug("MusicBrainz url-rels lookup: %s", release_id)
    try:
        result = musicbrainzngs.get_release_by_id(release_id, includes=["url-rels"])
    except (musicbrainzngs.ResponseError, musicbrainzngs.NetworkError) as exc:
        log.debug("MusicBrainz url-rels lookup for %s failed: %s", release_id, exc)
        return None, None

    from cdda2img.barcode import normalize_barcode

    release = result.get("release") or {}
    mb_barcode = normalize_barcode(release.get("barcode") or "")
    discogs_id: int | None = None
    for rel in release.get("url-relation-list") or []:
        if (rel.get("type") or "").lower() != "discogs":
            continue
        m = _DISCOGS_RELEASE_RE.search(rel.get("target") or "")
        if m:
            discogs_id = int(m.group(1))
            break
    return discogs_id, mb_barcode


def _mb_lucene_escape(value: str) -> str:
    """Escape backslash and double-quote for use inside a Lucene quoted string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_mb_search_query(artist: str | None, album: str | None) -> str:
    """Build a MusicBrainz Lucene query from separate artist and album strings.

    Uses field-qualified quoted terms so MB's engine does not misinterpret a
    plain "Artist Album" string as a single release-title search. Falls back to
    a plain joined string when only one field is present.
    """
    parts: list[str] = []
    if album and album.strip():
        parts.append(f'release:"{_mb_lucene_escape(album.strip())}"')
    if artist and artist.strip():
        parts.append(f'artist:"{_mb_lucene_escape(artist.strip())}"')
    if len(parts) == 2:
        return f"{parts[0]} AND {parts[1]}"
    return " ".join(parts)


def search_releases(query: str, limit: int = 25) -> list[DiscMeta]:
    """Text search for releases on MusicBrainz. Returns empty list on error."""
    _setup_useragent()
    log.debug("MusicBrainz text search: %r", query)
    try:
        result = musicbrainzngs.search_releases(query, limit=limit)
    except (musicbrainzngs.ResponseError, musicbrainzngs.NetworkError) as exc:
        log.debug("MusicBrainz text search failed: %s", exc)
        return []
    return [_parse_release(r) for r in (result.get("release-list") or [])]


def search_releases_by_barcode(barcode: str, limit: int = 25) -> list[DiscMeta]:
    """Search MusicBrainz for releases by barcode. Returns [] on error."""
    _setup_useragent()
    log.debug("MusicBrainz barcode search: %r", barcode)
    try:
        result = musicbrainzngs.search_releases(barcode=barcode, limit=limit)
    except (musicbrainzngs.ResponseError, musicbrainzngs.NetworkError) as exc:
        log.debug("MusicBrainz barcode search failed: %s", exc)
        return []
    return [_parse_release(r) for r in (result.get("release-list") or [])]


# ---------------------------------------------------------------------------
# Stage 7: last-resort duration match
# ---------------------------------------------------------------------------
#
# When no higher source (CD-Text, MB disc-ID, Discogs, AcoustID, CDDB) has
# identified the release in MusicBrainz, fall back to whipper's trick: find the
# MB release whose total duration best matches the physical disc. This is the
# weakest, lowest-precedence guess in the "Guess the Album" pipeline — the user
# is the final arbiter in the metadata menu.
#
# Two duration conventions, anchored separately (a constant offset would not
# change the argmin winner, only the absolute accept/reject gate):
#   - track.length  — TOC-derived per-medium span that INCLUDES the following
#     track's pregap; sums to the program span → compare against the
#     pregap-inclusive RBIDisc.total_frames.
#   - recording.length — canonical pure-audio value; sums to audio-only →
#     compare against sum(duration_frames). Used only as a fallback for the
#     rare release whose medium carries no per-track length (digital/manual
#     entry), and read here self-contained so it never leaks into duration_ms.

# Generous gate: argmin already picks the closest candidate, so this only
# rejects gross (off-by-minutes) mismatches. Tune via real-world testing /
# bug reports, not from first principles.
_DURATION_MATCH_TOLERANCE_MS = 15_000
# Cap the per-candidate fetch-full fan-out (MB is pinned to 1 req/s, R15).
_DURATION_MATCH_MAX_FETCH = 8


def _sum_track_lengths(release: dict) -> int | None:
    """Sum of per-medium ``track.length`` over every track (ms), or None if any
    track lacks it. TOC-derived program span — includes inter-track pregaps."""
    total = 0
    seen = False
    for medium in release.get("medium-list") or []:
        for track in medium.get("track-list") or []:
            length = track.get("length")
            if not length:
                return None
            total += int(length)
            seen = True
    return total if seen else None


def _sum_recording_lengths(release: dict) -> int | None:
    """Sum of per-recording ``recording.length`` over every track (ms), or None
    if any track lacks it. Canonical pure-audio value, read self-contained so it
    never reaches ``TrackMeta.duration_ms`` or the R3 gate."""
    total = 0
    seen = False
    for medium in release.get("medium-list") or []:
        for track in medium.get("track-list") or []:
            recording = track.get("recording") or {}
            length = recording.get("length")
            if not length:
                return None
            total += int(length)
            seen = True
    return total if seen else None


def pick_duration_match(
    releases: list[dict],
    *,
    program_anchor_ms: int,
    audio_anchor_ms: int,
    tolerance_ms: int = _DURATION_MATCH_TOLERANCE_MS,
) -> dict | None:
    """Pure selection: of *releases* (raw MB dicts), return the one whose total
    duration best matches the physical disc, or None if the best is off by more
    than *tolerance_ms*.

    track.length-scored candidates form the preferred pool (scored against the
    pregap-inclusive *program_anchor_ms*); the recording.length pool (scored
    against the audio-only *audio_anchor_ms*) is consulted only when no candidate
    has a complete track.length set. The two conventions are never mixed into one
    ranking — they are not comparable on a single scale.
    """
    track_pool = [
        (abs(total - program_anchor_ms), r)
        for r in releases
        if (total := _sum_track_lengths(r)) is not None
    ]
    rec_pool = [
        (abs(total - audio_anchor_ms), r)
        for r in releases
        if _sum_track_lengths(r) is None
        and (total := _sum_recording_lengths(r)) is not None
    ]
    pool = track_pool or rec_pool
    if not pool:
        return None
    delta, winner = min(pool, key=lambda pair: pair[0])
    if delta > tolerance_ms:
        return None
    return winner


def duration_match_lookup(disc: RBIDisc, *, verbose: bool = False) -> DiscMeta | None:
    """Stage 7 last-resort source: identify a release by matching the physical
    disc's total duration against MusicBrainz text-search candidates.

    Fires only as a last resort (the caller gates on ``disc.mb_release_id is
    None``). Requires an album or artist to search with.
    Returns a parsed ``DiscMeta`` for the winner, or None.
    """
    if not (disc.album or disc.artist):
        return None
    query = build_mb_search_query(disc.artist, disc.album)
    if not query:
        return None
    # Stubs carry track_count but no track-list. Pre-filter by track count —
    # a stronger gross discriminator than duration, and it slashes the
    # per-candidate fetch-full fan-out before paying the 1-req/s cost.
    stubs = search_releases(query)
    candidates = [
        s for s in stubs if s.mb_release_id and s.track_count == disc.track_count
    ][:_DURATION_MATCH_MAX_FETCH]
    if not candidates:
        return None
    raw_releases: list[dict] = []
    for c in candidates:
        if c.mb_release_id is None:  # narrowed by the filter above; satisfies ty
            continue
        raw = _fetch_release_raw(c.mb_release_id)
        if raw is not None:
            raw_releases.append(raw)
    program_anchor_ms = round(disc.total_frames / CD_FRAMES_PER_SECOND * 1000)
    audio_anchor_ms = round(
        sum(t.duration_frames for t in disc.tracks) / CD_FRAMES_PER_SECOND * 1000
    )
    winner = pick_duration_match(
        raw_releases,
        program_anchor_ms=program_anchor_ms,
        audio_anchor_ms=audio_anchor_ms,
    )
    if winner is None:
        return None
    meta = _parse_release(winner)
    if verbose:
        print(
            f'  MB duration-match: "{meta.album}" by {meta.artist} '
            f"(last resort, lowest priority)"
        )
    return meta


def lookup_release_group(rg_id: str) -> list[DiscMeta]:
    """Fetch all releases in a MusicBrainz release group, sorted by date (oldest first).

    Used by the 'Find Original Release' menu to browse all pressings in a release group.
    Returns [] on network error.
    """
    _setup_useragent()
    log.debug("MusicBrainz release group lookup: %s", rg_id)
    try:
        result = musicbrainzngs.get_release_group_by_id(
            rg_id,
            includes=["releases", "artists"],
        )
    except (musicbrainzngs.ResponseError, musicbrainzngs.NetworkError) as exc:
        log.debug("MusicBrainz release group lookup failed: %s", exc)
        return []
    rg = result.get("release-group") or {}
    first_date = rg.get("first-release-date") or ""
    artist_credit = rg.get("artist-credit") or []
    releases = rg.get("release-list") or []
    # Inject release group data into each stub release so _parse_release can classify remaster
    for r in releases:
        r.setdefault("release-group", {"id": rg_id, "first-release-date": first_date})
        r.setdefault("artist-credit", artist_credit)
    parsed = [_parse_release(r) for r in releases]
    return sorted(parsed, key=lambda m: m.release_date or "9999")


def lookup_isrc(isrc: str) -> list[DiscMeta]:
    """Look up releases on MusicBrainz by a single ISRC code.

    Returns a list of DiscMeta for releases that contain a recording with this ISRC.
    Results are basic (no per-track tracklist) due to the two-step lookup.
    Returns [] on network error.
    """
    _setup_useragent()
    log.debug("MusicBrainz ISRC lookup: %s", isrc)
    try:
        result = musicbrainzngs.get_recordings_by_isrc(
            isrc,
            includes=["artists", "releases", "release-groups"],
        )
    except (musicbrainzngs.ResponseError, musicbrainzngs.NetworkError) as exc:
        log.debug("MusicBrainz ISRC lookup for %s failed: %s", isrc, exc)
        return []
    recordings = (result.get("isrc") or {}).get("recording-list") or []
    seen: set[str] = set()
    results: list[DiscMeta] = []
    for recording in recordings:
        artist = _artist_credit_name(recording.get("artist-credit") or [])
        for release in recording.get("release-list") or []:
            rid = release.get("id")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            rg = release.get("release-group") or {}
            date = release.get("date") or ""
            original_date = rg.get("first-release-date") or ""
            results.append(
                DiscMeta(
                    album=release.get("title") or None,
                    artist=artist or None,
                    mb_release_id=rid,
                    mb_release_group_id=rg.get("id") or None,
                    release_date=date or None,
                    original_release_date=original_date or None,
                    source="musicbrainz",
                )
            )
    return results


# ---------------------------------------------------------------------------
# Pre-population helpers
# ---------------------------------------------------------------------------


def _merge_into_disc(meta: DiscMeta, disc: RBIDisc) -> RBIDisc:
    """Return a new RBIDisc with None/empty/unknown fields filled from *meta*."""
    from cdda2img.validators import validate_isrc

    _unknown = "Unknown Artist"
    album = disc.album if disc.album else (meta.album or disc.album)
    artist = (
        disc.artist
        if disc.artist and disc.artist != _unknown
        else (meta.artist or disc.artist)
    )
    # catalog is the on-disc MCN: archival only, NEVER filled from a service
    # barcode (cross-namespace). The service UPC/EAN goes to its own `barcode`
    # field, fill-blank. See docs/reference/identifier_trust_model.md §1a.
    catalog = disc.catalog
    barcode = disc.barcode or meta.barcode

    meta_by_num = {t.number: t for t in meta.tracks if t.number is not None}
    new_tracks: list[RBITocEntry] = []
    for entry in disc.tracks:
        mt = meta_by_num.get(entry.track_number)
        if mt:
            # R13: validate the raw-side ISRC at the merge chokepoint; if it
            # fails (malformed input from a foreign-image parser), fall back
            # to the meta-side ISRC which was already validated at MB ingress.
            entry_isrc = validate_isrc(entry.isrc) if entry.isrc else None
            new_tracks.append(
                RBITocEntry(
                    track_number=entry.track_number,
                    title=entry.title if entry.title else (mt.title or entry.title),
                    performer=(
                        entry.performer
                        if entry.performer and entry.performer != _unknown
                        else (mt.performer or entry.performer)
                    ),
                    start_frame=entry.start_frame,
                    duration_frames=entry.duration_frames,
                    pregap_frames=entry.pregap_frames,
                    isrc=entry_isrc or mt.isrc,
                )
            )
        else:
            new_tracks.append(entry)

    # F-001: build with dataclasses.replace so disc-only fields (pre_emphasis —
    # which gates the R14 ≤1986 cap — low_dynamic_range, original_release_*,
    # cdtext_catalog_ref) are carried over verbatim and can never be silently
    # dropped when RBIDisc gains a field. Only the merged fields are named.
    return replace(
        disc,
        album=album,
        artist=artist,
        disc_number=(
            meta.disc_number if meta.disc_number is not None else disc.disc_number
        ),
        disc_total=(
            meta.disc_total if meta.disc_total is not None else disc.disc_total
        ),
        catalog=catalog,
        barcode=barcode,
        tracks=new_tracks,
        release_date=disc.release_date or meta.release_date or None,
        catalog_number=disc.catalog_number or meta.catalog_number or None,
        label=disc.label or meta.label or None,
        country=disc.country or meta.country or None,
        original_release_date=disc.original_release_date
        or meta.original_release_date
        or None,
        mb_release_id=disc.mb_release_id or meta.mb_release_id or None,
        mb_release_group_id=disc.mb_release_group_id
        or meta.mb_release_group_id
        or None,
        discogs_release_id=disc.discogs_release_id or meta.discogs_release_id,
        set_title=disc.set_title or meta.set_title,
    )


def _overwrite_disc(meta: DiscMeta, disc: RBIDisc) -> RBIDisc:
    """Return a new RBIDisc with all non-None *meta* fields replacing disc fields.

    Unlike _merge_into_disc (which keeps existing disc values), this always
    prefers meta's values when set — used for the 'Overwrite All' apply mode.
    """
    from cdda2img.validators import validate_isrc

    meta_by_num = {t.number: t for t in meta.tracks if t.number is not None}
    new_tracks: list[RBITocEntry] = []
    for entry in disc.tracks:
        mt = meta_by_num.get(entry.track_number)
        if mt:
            # R13: validate the raw-side ISRC; mt.isrc was already validated
            # at MB ingress.
            entry_isrc = validate_isrc(entry.isrc) if entry.isrc else None
            new_tracks.append(
                RBITocEntry(
                    track_number=entry.track_number,
                    title=mt.title or entry.title,
                    performer=mt.performer or entry.performer,
                    start_frame=entry.start_frame,
                    duration_frames=entry.duration_frames,
                    pregap_frames=entry.pregap_frames,
                    isrc=mt.isrc or entry_isrc,
                )
            )
        else:
            new_tracks.append(entry)

    # F-001: dataclasses.replace preserves disc-only fields (pre_emphasis etc.).
    # The original hand-built RBIDisc here dropped both pre_emphasis AND
    # discogs_release_id; the latter is restored explicitly with overwrite's
    # meta-first preference.
    return replace(
        disc,
        album=meta.album or disc.album,
        artist=meta.artist or disc.artist,
        disc_number=(
            meta.disc_number if meta.disc_number is not None else disc.disc_number
        ),
        disc_total=(
            meta.disc_total if meta.disc_total is not None else disc.disc_total
        ),
        # catalog (MCN) is archival; meta carries no MCN, so it is untouched even
        # under overwrite. The service barcode takes meta-priority into `barcode`.
        catalog=disc.catalog,
        barcode=meta.barcode or disc.barcode,
        tracks=new_tracks,
        release_date=meta.release_date or disc.release_date or None,
        catalog_number=meta.catalog_number or disc.catalog_number or None,
        label=meta.label or disc.label or None,
        country=meta.country or disc.country or None,
        original_release_date=meta.original_release_date
        or disc.original_release_date
        or None,
        mb_release_id=meta.mb_release_id or disc.mb_release_id or None,
        mb_release_group_id=meta.mb_release_group_id
        or disc.mb_release_group_id
        or None,
        discogs_release_id=meta.discogs_release_id or disc.discogs_release_id,
        set_title=meta.set_title or disc.set_title,
    )


def strip_pressing_mbid(meta: DiscMeta) -> DiscMeta:
    """C2 chokepoint: null a *recording-level* match's pressing ``mb_release_id``.

    Sources that identify a *recording* rather than a disc-ID-verified pressing —
    the per-track ISRC tally (R4), the stage-7 duration matcher, and AcoustID —
    must not bake their ``mb_release_id`` into ``disc`` as if it were
    disc-ID-proven (defect class C2). This is the single place that strips it;
    every non-disc-ID merge path routes its ``DiscMeta`` through here before
    ``_merge_into_disc``. ``mb_release_group_id`` is deliberately preserved — the
    original-release lookup needs it, and a release *group* is not pressing-level.

    Disc-ID matches must NOT use this: their ``mb_release_id`` is legitimately
    pressing-level (every candidate shares the disc-ID fingerprint).
    """
    return replace(meta, mb_release_id=None)


# R1: minimum number of agreeing per-track ISRC pairs required to commit a
# multi-match disambiguation. Below this we prefer no-auto-merge (blank but
# correctable) over a confident-but-possibly-wrong choice.
_MIN_ISRC_AGREE = 2
_ISRC_AGREE_RATIO: float = 0.6  # R1: scales threshold with available ISRC evidence


def _score_candidate_by_isrcs(meta: DiscMeta, disc: RBIDisc) -> int:
    """Count per-track ISRC agreements between *meta* and *disc*.

    A point is scored when *both* sides have an ISRC for the same track
    number and the ISRC strings are equal. Tracks where either side lacks
    an ISRC contribute nothing. Returns 0 when no ISRC pairs exist, which
    `_disambiguate_by_isrcs` reads as "no evidence, do not auto-merge".
    """
    disc_isrcs = {t.track_number: t.isrc for t in disc.tracks if t.isrc}
    if not disc_isrcs:
        return 0
    meta_isrcs = {
        t.number: t.isrc for t in meta.tracks if t.number is not None and t.isrc
    }
    return sum(1 for tn, isrc in disc_isrcs.items() if meta_isrcs.get(tn) == isrc)


# R4: minimum ISRC-bearing tracks required before the zero-match tally
# fires. Below this the signal is too noisy (sparse ISRC data can land
# many false-positive convergences).
_R4_MIN_ISRC_BEARING_TRACKS = 3


def _resolve_via_isrc_tally(disc: RBIDisc) -> DiscMeta | None:
    """R4: zero-disc-ID-match fallback via per-track ISRC release tally.

    Fires only when ``lookup_disc_id`` returned no matches *and* the disc
    has at least ``_R4_MIN_ISRC_BEARING_TRACKS`` ISRC-bearing tracks. For
    each ISRC, ``lookup_isrc`` is called and the returned release MBIDs
    are tallied. The release with the strictly highest tally wins, but
    only when it converges across ≥ ceil(N/2) of the ISRC-bearing tracks
    (where N is the number of ISRC-bearing tracks). Ties or sub-threshold
    top tallies return None.

    Cost: N sequential MB calls at 1 req/s (~20 s for a 20-track disc).
    A successful tally feeds the existing barcode-hint / RG-MBID pipeline
    via ``_merge_into_disc``, exactly as the single-disc-ID match would.
    """
    isrcs = [t.isrc for t in disc.tracks if t.isrc]
    if len(isrcs) < _R4_MIN_ISRC_BEARING_TRACKS:
        return None
    tally: dict[str, int] = {}
    candidates: dict[str, DiscMeta] = {}
    for isrc in isrcs:
        responses = lookup_isrc(isrc)
        seen: set[str] = set()
        for r in responses:
            rid = r.mb_release_id
            if rid is None or rid in seen:
                continue
            seen.add(rid)
            tally[rid] = tally.get(rid, 0) + 1
            candidates.setdefault(rid, r)
    if not tally:
        return None
    ranked = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
    top_rid, top_count = ranked[0]
    floor = max(_R4_MIN_ISRC_BEARING_TRACKS, (len(isrcs) + 1) // 2)
    if top_count < floor:
        log.debug(
            "R4: top tally %d for %s is below floor %d (%d ISRCs queried)",
            top_count,
            top_rid,
            floor,
            len(isrcs),
        )
        return None
    if len(ranked) > 1 and ranked[1][1] == top_count:
        log.debug("R4: tied top tally %d — no auto-merge", top_count)
        return None
    # F-002 / C2: the tally key (and thus candidate.mb_release_id) is a
    # *recording*'s release, reached via ISRC — not a disc-ID-verified release
    # for THIS disc. Route through the C2 chokepoint so it is never written as
    # authoritative release provenance; the release-group id is kept.
    return strip_pressing_mbid(candidates[top_rid])


def _disambiguate_by_isrcs(matches: list[DiscMeta], disc: RBIDisc) -> DiscMeta | None:
    """Pick the strict ISRC-score winner from MB's multi-match response.

    A unique candidate with score >= ``_MIN_ISRC_AGREE`` and a strictly
    higher score than every other candidate wins. Ties at the top score or
    a top score below the floor return None — the caller preserves the
    no-auto-merge fallback. Zero new API calls: `lookup_disc_id` already
    requested ``recordings`` + ``isrcs`` for every candidate.
    """
    scored = [(_score_candidate_by_isrcs(m, disc), i, m) for i, m in enumerate(matches)]
    scored.sort(key=lambda t: (-t[0], t[1]))
    top_score, _, top_meta = scored[0]
    n_isrc_tracks = sum(1 for t in disc.tracks if t.isrc)
    threshold = max(_MIN_ISRC_AGREE, math.ceil(_ISRC_AGREE_RATIO * n_isrc_tracks))
    if top_score < threshold:
        return None
    if len(scored) > 1 and scored[1][0] == top_score:
        return None
    return top_meta


def _plurality_release_group(matches: list[DiscMeta]) -> str | None:
    """Return the release-group shared by a strict plurality of *matches*.

    When the pressing-level disambiguator (R1) cannot pick a single release
    from a multi-match, the matches still usually agree on one release-group
    (the album itself). Adopting that RG lets the original-release lookup
    resolve the album's first release pre-menu without committing to any one
    pressing's metadata.

    Returns None when no release-group holds a unique maximum count (a tie is
    treated as "no evidence — prefer no answer") or when no match carries an
    RG id. Derivative groups (Compilation, Live, …) need no special handling
    here: ``_find_original_release_via_rg`` re-validates and rejects them.
    """
    counts = Counter(m.mb_release_group_id for m in matches if m.mb_release_group_id)
    if not counts:
        return None
    ranked = counts.most_common()
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


def _disambiguate_by_mcn(matches: list[DiscMeta], disc: RBIDisc) -> DiscMeta | None:
    """Pick the match whose barcode UNIQUELY matches the disc's Q-channel MCN.

    The MCN read from the disc subchannel is the strongest pressing-level
    signal available, resolved before the MB lookup runs. Comparison is fuzzy
    (``barcode.mcn_matches``): services store partial / check-digit-free
    barcodes, so exact EAN-13 equality misses real matches (Unit M).

    A *unique* barcode hit identifies that exact release — safe to merge in
    full. When the MCN matches **several** candidates (a barcode shared across
    country variants, e.g. DE + XE), the specific pressing is genuinely
    undetermined; we return None so the caller falls back to agreed-facts-only
    population rather than fabricate one pressing's date / release id. Returns
    None when the disc has no MCN or no candidate barcode matches.

    NB after the Unit-G consistency pre-filter (``prepopulate_from_mb``), every
    surviving candidate already fuzzy-matches a non-blank disc MCN, so on a
    multi-survivor this returns None by construction (all hit ⇒ not unique) and
    resolution falls through to agreed-facts — exactly the intended behaviour.
    """
    from cdda2img.barcode import mcn_matches

    if not disc.catalog:
        return None
    hits = [m for m in matches if mcn_matches(disc.catalog, m.barcode)]
    return hits[0] if len(hits) == 1 else None


def _is_consistent(meta: DiscMeta, disc: RBIDisc) -> bool:
    """True unless *meta* contradicts the disc's per-track ISRCs.

    The disc's per-track ISRCs are loaded before MB is consulted; a candidate that
    disagrees with one is the *wrong record* (Unit G): reject it wholesale rather
    than merge a contradiction. Free text (album/artist/titles) is never gated
    here — only the objective per-track ISRC.

    The on-disc MCN is **not** gated here. It and the service-stored barcode are
    different identifiers in different namespaces, and the MCN is archival-only —
    never used for disambiguation (see ``docs/reference/identifier_trust_model.md``
    §1a). A cross-namespace MCN/barcode comparison must therefore never veto a
    candidate; the per-track ISRC veto is exact and same-namespace, so it stays.

      * per-track ISRC — matched by track number; both non-blank and unequal ⇒
        inconsistent (exact: ISRC is a fixed 12-char ISO-3901 code).

    Blank on either side is no evidence, never a contradiction. A candidate with
    no overlapping non-blank ISRC passes vacuously (consistent until proven wrong).
    """
    disc_isrcs = {t.track_number: t.isrc for t in disc.tracks if t.isrc}
    if disc_isrcs:
        meta_isrcs = {
            t.number: t.isrc for t in meta.tracks if t.number is not None and t.isrc
        }
        for tn, isrc in disc_isrcs.items():
            other = meta_isrcs.get(tn)
            if other is not None and other != isrc:
                return False
    return True


def _resolve_multimatch(
    matches: list[DiscMeta], disc: RBIDisc
) -> tuple[DiscMeta | None, str]:
    """Resolve an MB disc-ID multi-match to one pressing using all pre-MB signals.

    Priority order (strongest deterministic signal last-resort-free first):
      1. ``isrc`` — per-track ISRC agreement (R1).
      2. ``mcn``  — the disc's Q-channel MCN against candidate barcodes.

    Returns ``(winner, method)`` with method ``"isrc"`` / ``"mcn"`` / ``""``
    (no resolution). Pressing-level callers can then ``_merge_into_disc`` the
    winner so ``release_date`` (hence the disc's own year) becomes known.

    Deliberate ordering (Q3): ISRC is tried *before* the MCN/barcode even though
    the barcode is the more obviously "pressing-level" id. ``_disambiguate_by_isrcs``
    only returns a candidate that is the *strict, unique* high scorer at
    >= _MIN_ISRC_AGREE agreeing per-track ISRCs — recording-identity evidence
    that cannot tie. A barcode, by contrast, is routinely *shared* across country
    variants (DE + XE + …), so ``_disambiguate_by_mcn`` returns None on a
    multi-hit; trying it first would not resolve those and would add no safety.
    The strict-uniqueness of the ISRC winner is what makes ISRC-first safe.
    """
    winner = _disambiguate_by_isrcs(matches, disc)
    if winner is not None:
        return winner, "isrc"
    winner = _disambiguate_by_mcn(matches, disc)
    if winner is not None:
        return winner, "mcn"
    return None, ""


class MBPrepopResult(NamedTuple):
    """Aggregate of a MusicBrainz disc-ID prepop run, including diagnostic counts."""

    disc: RBIDisc
    # R16: each hint is (mb_release_id, normalised barcode). Empty-string MBID
    # is acceptable for releases that lack one (defensive); R1 will simply not
    # match such entries. Order follows MB's match order.
    barcode_hints: list[tuple[str, str]]
    # Usable matches: MB matches that survived the Unit-G consistency filter
    # (0 = disc-ID unknown to MB *or* every candidate contradicted a gospel
    # on-disc id). See ``rejected_inconsistent`` to tell those two apart.
    match_count: int
    # Unit G: count of MB matches discarded for contradicting a non-blank
    # on-disc MCN / per-track ISRC. Surfaced in PROV as ``mb_rejected_inconsistent``.
    rejected_inconsistent: int = 0
    # R1: True iff len(matches) > 1 and ISRC scoring picked a strict winner
    # that was then auto-merged. Surfaces as PROV ``multi_match_isrc_disambiguated``.
    isrc_disambiguated: bool = False
    # R9: (album, artist) of the MB candidate that drove the merge. None when
    # no single candidate was selected (zero matches, sub-threshold tally,
    # etc.). Callers can compare these against the pre-MB disc state to
    # detect CDDB↔MB disagreement.
    mb_candidate_album: str | None = None
    mb_candidate_artist: str | None = None
    # R8: the winning candidate DiscMeta itself. None when no candidate
    # was picked (zero matches, ambiguous multi-match, etc.). Lets the
    # parallel pre-menu pipeline re-apply ``_merge_into_disc(meta, ...)``
    # on top of a CDDB-merged disc.
    meta: DiscMeta | None = None
    # §10.3: the key that broke the tie when the lexicographic release-selection
    # rung pinned one of several album-consistent pressings — ``mcn`` |
    # ``barcode_plurality`` | ``preferred_country`` | ``date`` | ``mbid``. None
    # when no rung selection ran (single match / ISRC / MCN winner upstream).
    # Surfaces in PROV as ``release_selected_via``.
    release_selected_via: str | None = None
    # B-2 (trust_model_design.md §11.2): the Layer-1 *selected pressing* release id,
    # exposed as an explicit eager gating signal decoupled from the mutated disc.
    # Set at the ``prepopulate_from_mb`` chokepoint to the merged
    # ``disc.mb_release_id`` (so it equals what the mid-pipeline gates read today,
    # by construction). The stage-7 gate, AcoustID corroboration and the §10.3.1
    # Discogs-barcode check read THIS instead of ``disc.mb_release_id`` so they keep
    # working once B-4 stops the mutate-as-you-go merge. ``None`` when no pressing
    # was pinned (zero/inconsistent match, or a recording-level fallback that routed
    # through ``strip_pressing_mbid``) — exactly the condition the stage-7 gate fires on.
    selected_release_id: str | None = None


def _prepop_zero_match(
    disc: RBIDisc, hints: list[tuple[str, str]], *, verbose: bool
) -> MBPrepopResult:
    """R4: disc-ID unknown to MB → ISRC-tally fallback, gated by consistency.

    The tally winner's ISRC half agrees with the disc by construction, but a
    contradicting on-disc MCN must still reject it (advisor #1).
    """
    winner = _resolve_via_isrc_tally(disc)
    if winner is not None and _is_consistent(winner, disc):
        updated = _merge_into_disc(winner, disc)
        if verbose:
            date_str = f"  ({winner.release_date})" if winner.release_date else ""
            print(
                f'  MusicBrainz: matched "{winner.album}" by {winner.artist}'
                f"{date_str} (via ISRC tally)"
            )
        return MBPrepopResult(
            updated,
            hints,
            0,
            isrc_disambiguated=False,
            mb_candidate_album=winner.album,
            mb_candidate_artist=winner.artist,
            meta=winner,
        )
    return MBPrepopResult(disc, hints, 0, isrc_disambiguated=False)


def _select_release_lexicographic(
    candidates: list[DiscMeta],
    disc: RBIDisc,
    preferred_country: list[str],
) -> tuple[DiscMeta | None, str | None]:
    """Pick one pressing from several album-consistent candidates by a pure
    lexicographic key chain (trust_model_design.md §10.3). Returns
    ``(winner, via)`` where *via* names the key that put the winner ahead of the
    runner-up:

      (0) ``mcn``               — barcode positively matches the on-disc MCN
      (1) ``barcode_plurality`` — the most common normalised barcode wins
      (2) ``preferred_country`` — user config ranking (priority, NOT a filter)
      (3) ``date``              — earliest ``release_date``
      (4) ``mbid``              — terminal, deterministic tiebreak

    No candidate is discarded; the top of the ranking is pinned. *candidates*
    must already be the album-consistent set (the plurality release-group), so
    this only chooses the *pressing*, never the album — pinning ``mb_release_id``
    here is legitimate (every candidate shares the disc-ID fingerprint).
    """
    from cdda2img.barcode import mcn_matches, normalize_barcode

    if not candidates:
        return None, None

    def _norm(cat: str | None) -> str | None:
        return normalize_barcode(cat, require_check_digit=False) if cat else None

    counts: Counter[str] = Counter()
    for c in candidates:
        nb = _norm(c.barcode)
        if nb:
            counts[nb] += 1

    pref = [code.upper() for code in preferred_country]

    def _key(c: DiscMeta) -> tuple[int, int, int, str, str]:
        nb = _norm(c.barcode)
        k_mcn = 0 if (disc.catalog and mcn_matches(disc.catalog, c.barcode)) else 1
        k_plur = -(counts[nb] if nb else 0)  # more common -> smaller -> first
        country = (c.country or "").upper()
        k_country = pref.index(country) if country in pref else len(pref)
        k_date = c.release_date or "9999"  # missing date sorts last
        k_mbid = c.mb_release_id or "~"  # '~' (0x7e) sorts after digits/letters
        return (k_mcn, k_plur, k_country, k_date, k_mbid)

    keys = [_key(c) for c in candidates]
    winner = candidates[keys.index(min(keys))]
    # *via* = the highest-priority key on which the candidates actually vary. The
    # winner is the lexicographic minimum, so at the first key with any variation
    # it necessarily holds the best value — that key is what decided the ranking.
    # When nothing varies above the terminal id, report "mbid" (arbitrary but
    # deterministic).
    via_names = ("mcn", "barcode_plurality", "preferred_country", "date", "mbid")
    via = via_names[-1]
    for i, name in enumerate(via_names):
        if len({k[i] for k in keys}) > 1:
            via = name
            break
    return winner, via


def _prepop_multimatch(
    matches: list[DiscMeta],
    disc: RBIDisc,
    hints: list[tuple[str, str]],
    rejected: int,
    *,
    verbose: bool,
    preferred_country: list[str],
) -> MBPrepopResult:
    """Resolve a consistent MB disc-ID multi-match: ISRC/MCN winner, else the
    lexicographic release-selection rung over the album's plurality release-group
    (§10.3) — pins the best pressing rather than declining to choose."""
    winner, method = _resolve_multimatch(matches, disc)
    if winner is not None:
        updated = _merge_into_disc(winner, disc)
        if verbose:
            date_str = f"  ({winner.release_date})" if winner.release_date else ""
            via = "ISRC disambiguation" if method == "isrc" else "MCN/barcode match"
            print(
                f'  MusicBrainz: matched "{winner.album}" by {winner.artist}'
                f"{date_str} (via {via})"
            )
        return MBPrepopResult(
            updated,
            hints,
            len(matches),
            rejected_inconsistent=rejected,
            isrc_disambiguated=(method == "isrc"),
            mb_candidate_album=winner.album,
            mb_candidate_artist=winner.artist,
            meta=winner,
        )
    # No ISRC/MCN winner. Identify the album by plurality release-group, then pin
    # the best *pressing* within it via the lexicographic rung (§10.3). This
    # replaces the older "decline to pin" agreed-facts merge: the choice is a
    # defensible best guess (preference-driven, PROV-recorded, user-correctable),
    # and the pinned release still carries its release-group so original-release
    # resolution is unaffected. ``meta=winner`` lets the parallel pre-menu path
    # (_finalize_import) re-apply it onto the CDDB-merged disc like a single.
    #
    # Accepted reduction in conservatism (advisor, 2026-06-20): the old agreed-
    # facts merge blanked album/title on *intra-group* disagreement (a deluxe /
    # reissue title variant within the same RG); the rung instead commits the
    # winner's title unconditionally. Acceptable under the best-guess model — the
    # menu corrects it interactively and the pick is deterministic in --auto —
    # but it IS a real change for the no-human-in-the-loop path.
    #
    # Unit A: when the disc carries an MCN, narrow to the candidates whose barcode
    # positively matches it — identity proven. Survivors with a *blank* barcode
    # passed the Unit-G gate only vacuously and are dropped once a positively-
    # matching subset exists. Fall back to the full consistent set when none match
    # (e.g. MB carries no barcodes) — RG plurality still holds.
    from cdda2img.barcode import mcn_matches

    subset = matches
    if disc.catalog:
        mcn_hits = [m for m in matches if mcn_matches(disc.catalog, m.barcode)]
        if mcn_hits:
            subset = mcn_hits
    rg = _plurality_release_group(subset)
    if rg is not None:
        rg_subset = [m for m in subset if m.mb_release_group_id == rg]
        winner, via = _select_release_lexicographic(rg_subset, disc, preferred_country)
        if winner is not None:
            disc = _merge_into_disc(winner, disc)
            log.debug(
                "MB multi-match (%d): pinned release %s via %s (rg=%s)",
                len(matches),
                winner.mb_release_id,
                via,
                rg,
            )
            return MBPrepopResult(
                disc,
                hints,
                len(matches),
                rejected_inconsistent=rejected,
                isrc_disambiguated=False,
                mb_candidate_album=winner.album,
                mb_candidate_artist=winner.artist,
                meta=winner,
                release_selected_via=via,
            )
    log.debug("MB disc ID returned %d matches; no plurality RG", len(matches))
    return MBPrepopResult(disc, hints, len(matches), rejected_inconsistent=rejected)


def prepopulate_from_mb(
    disc: RBIDisc,
    *,
    verbose: bool = True,
    preferred_country: list[str] | None = None,
) -> MBPrepopResult:
    """Public entry: run the MB disc-ID prepop, then expose the Layer-1 selected
    pressing as ``selected_release_id`` (B-2, §11.2).

    The capture is done **once here**, from the merged ``result.disc.mb_release_id``,
    so it equals what the mid-pipeline gates read today across *every* sub-path
    (single match, lexicographic multimatch winner, the ISRC-tally fallback which
    returns ``strip_pressing_mbid(...)`` → ``None``, and the
    baseline-already-had-an-mbid case). That is the whole point of the chokepoint:
    no need to thread the value through the four ``MBPrepopResult`` construction
    sites, and no risk of a sub-path setting it inconsistently.
    """
    result = _prepopulate_from_mb(
        disc, verbose=verbose, preferred_country=preferred_country
    )
    return result._replace(selected_release_id=result.disc.mb_release_id)


def _prepopulate_from_mb(
    disc: RBIDisc,
    *,
    verbose: bool = True,
    preferred_country: list[str] | None = None,
) -> MBPrepopResult:
    """Attempt a silent MusicBrainz Disc ID lookup and fill in missing fields.

    If exactly one match is found, missing fields in *disc* are filled from
    the MusicBrainz result and a summary line is printed (when *verbose* is
    True). When MB returns multiple matches, an ISRC-tally disambiguator
    (R1) tries to pick a unique winner using the per-track ISRCs already
    fetched in the same response — no extra network calls. On zero matches,
    a tie, a sub-threshold top score, or a network error, *disc* is
    returned unchanged.

    *barcode_hints* is a list of ``(mb_release_id, barcode)`` tuples drawn
    from **every** match (R16), suitable for seeding a downstream Discogs
    search and for resolving R1's winning candidate to its deterministic
    barcode without a second MB call. *match_count* lets the caller
    distinguish "MB knows this disc but lacks barcodes" from "MB doesn't
    know it" in diagnostic output. *isrc_disambiguated* records whether
    the multi-match path was resolved by R1.
    """
    _setup_useragent()
    raw_matches = lookup_disc_id(disc)
    # Unit G: a candidate that contradicts a non-blank on-disc MCN / per-track
    # ISRC is the wrong record — drop it before any resolution runs. The disc's
    # objective ids are gospel; rejecting the whole record (rather than merging a
    # contradiction) is the "prefer no-answer over wrong-answer" principle.
    matches = [m for m in raw_matches if _is_consistent(m, disc)]
    rejected = len(raw_matches) - len(matches)
    if rejected:
        log.debug(
            "MB disc ID: dropped %d/%d match(es) inconsistent with on-disc MCN/ISRC",
            rejected,
            len(raw_matches),
        )
    # R16: tag each hint with its source MBID. Preserve match order while
    # deduplicating exact (mbid, barcode) pairs — different releases with the
    # same barcode keep separate entries so future lookups can disambiguate.
    # Hints come from the *consistent* set only: a rejected record's barcode
    # contradicts the disc and must not seed the downstream canonical-MCN pick.
    hints: list[tuple[str, str]] = list(
        dict.fromkeys((m.mb_release_id or "", m.barcode) for m in matches if m.barcode)
    )
    if hints:
        log.debug("MB barcode hints from %d match(es): %s", len(matches), hints)
    if not raw_matches:
        return _prepop_zero_match(disc, hints, verbose=verbose)
    if not matches:
        # MB knew the disc-ID but every candidate contradicts a gospel on-disc
        # id → leave the fields blank for AcoustID / the manual menu to fill.
        # (This also closes the old single-match no-cross-check gap.) We do NOT
        # fall through to the R4 tally here: the disc-ID lookup already spoke.
        return MBPrepopResult(
            disc, hints, 0, rejected_inconsistent=rejected, isrc_disambiguated=False
        )
    if len(matches) > 1:
        return _prepop_multimatch(
            matches,
            disc,
            hints,
            rejected,
            verbose=verbose,
            preferred_country=preferred_country or [],
        )
    meta = matches[0]
    updated = _merge_into_disc(meta, disc)
    if verbose:
        date_str = f"  ({meta.release_date})" if meta.release_date else ""
        print(f'  MusicBrainz: matched "{meta.album}" by {meta.artist}{date_str}')
    return MBPrepopResult(
        updated,
        hints,
        len(matches),
        rejected_inconsistent=rejected,
        isrc_disambiguated=False,
        mb_candidate_album=meta.album,
        mb_candidate_artist=meta.artist,
        meta=meta,
    )
