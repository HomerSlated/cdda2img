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
from collections import Counter
from typing import NamedTuple

import musicbrainzngs  # type: ignore[import-untyped]

from cdda2img.lookup_result import (
    DiscMeta,
    TrackMeta,
)
from cdda2img.rbi_format import RBIDisc, RBITocEntry

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
    """
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
    catalog = normalize_barcode(raw_barcode)
    if raw_barcode and not catalog:
        log.info(
            "MB release %r: raw barcode %r failed normalisation (dropped)",
            release.get("id"),
            raw_barcode,
        )

    return DiscMeta(
        album=album_title,
        artist=artist or None,
        catalog=catalog,
        mb_release_id=release.get("id") or None,
        mb_release_group_id=rg.get("id") or None,
        release_date=date or None,
        original_release_date=original_date or None,
        country=release.get("country") or None,
        label=label or None,
        catalog_number=catalog_number or None,
        disc_number=disc_number,
        disc_total=disc_total,
        set_title=set_title,
        source="musicbrainz",
        tracks=tracks,
    )


# ---------------------------------------------------------------------------
# Network lookups
# ---------------------------------------------------------------------------


def lookup_disc_id(disc: RBIDisc) -> list[DiscMeta]:
    """Look up releases on MusicBrainz by Disc ID computed from the disc TOC.

    Returns a list of matching DiscMeta (empty on no match or network error).

    R7: results are cached in ``lookup_cache.db`` with a 30-day TTL.
    Cache hits short-circuit the network call; failures (TTL, parse error,
    sqlite error) degrade silently to a live request.

    R10: when offline mode is active, the function still reads the cache
    (cached responses are usable offline) but never makes a network call.
    """
    from cdda2img.config import is_no_network_active
    from cdda2img.lookup_cache import (
        get_cached_disc_id_lookup,
        put_cached_disc_id_lookup,
    )

    disc_id_str = disc_id_from_rbi(disc)
    if not disc_id_str:
        return []
    cached = get_cached_disc_id_lookup(disc_id_str)
    if cached is not None:
        log.debug("MB disc ID cache hit: %s", disc_id_str)
        return cached
    if is_no_network_active():
        log.debug("MB disc ID offline (cache miss for %s)", disc_id_str)
        return []
    _setup_useragent()
    log.debug("MusicBrainz disc ID lookup: %s", disc_id_str)
    try:
        result = musicbrainzngs.get_releases_by_discid(
            disc_id_str,
            includes=["artists", "recordings", "release-groups", "labels", "isrcs"],
        )
    except musicbrainzngs.ResponseError as exc:
        log.debug("MusicBrainz disc ID lookup failed: %s", exc)
        return []
    except musicbrainzngs.NetworkError as exc:
        log.debug("MusicBrainz network error: %s", exc)
        return []
    releases = (result.get("disc") or {}).get("release-list") or []
    parsed = [_parse_release(r, _disc_id=disc_id_str) for r in releases]
    put_cached_disc_id_lookup(disc_id_str, parsed)
    return parsed


def lookup_release(release_id: str, disc_number: int | None = None) -> DiscMeta | None:
    """Fetch a single MusicBrainz release by ID with full track listing.

    *disc_number* selects the matching medium for multi-disc releases so that
    only that disc's tracks are returned.  Pass ``disc.disc_number`` when
    applying to a known disc in a set.

    Returns None on network/response error or when offline mode is active (R10).
    """
    from cdda2img.config import is_no_network_active

    if is_no_network_active():
        return None
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
    release = result.get("release") or {}
    if not release:
        return None
    return _parse_release(release, _disc_number=disc_number)


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
    """Text search for releases on MusicBrainz. Returns empty list on error or offline (R10)."""
    from cdda2img.config import is_no_network_active

    if is_no_network_active():
        return []
    _setup_useragent()
    log.debug("MusicBrainz text search: %r", query)
    try:
        result = musicbrainzngs.search_releases(query, limit=limit)
    except (musicbrainzngs.ResponseError, musicbrainzngs.NetworkError) as exc:
        log.debug("MusicBrainz text search failed: %s", exc)
        return []
    return [_parse_release(r) for r in (result.get("release-list") or [])]


def search_releases_by_barcode(barcode: str, limit: int = 25) -> list[DiscMeta]:
    """Search MusicBrainz for releases by barcode. Returns [] on error or offline (R10)."""
    from cdda2img.config import is_no_network_active

    if is_no_network_active():
        return []
    _setup_useragent()
    log.debug("MusicBrainz barcode search: %r", barcode)
    try:
        result = musicbrainzngs.search_releases(barcode=barcode, limit=limit)
    except (musicbrainzngs.ResponseError, musicbrainzngs.NetworkError) as exc:
        log.debug("MusicBrainz barcode search failed: %s", exc)
        return []
    return [_parse_release(r) for r in (result.get("release-list") or [])]


def lookup_release_group(rg_id: str) -> list[DiscMeta]:
    """Fetch all releases in a MusicBrainz release group, sorted by date (oldest first).

    Used by the 'Find Original Release' menu to browse all pressings in a release group.
    Returns [] when offline mode is active (R10).
    """
    from cdda2img.config import is_no_network_active

    if is_no_network_active():
        return []
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
    Returns [] when offline mode is active (R10).

    R7: results cached in ``isrc_lookups`` with no TTL — ISRC→recording
    bindings are immutable in practice. Cache reads work in offline mode.
    """
    from cdda2img.config import is_no_network_active
    from cdda2img.lookup_cache import (
        get_cached_isrc_lookup,
        put_cached_isrc_lookup,
    )

    cached = get_cached_isrc_lookup(isrc)
    if cached is not None:
        log.debug("MB ISRC cache hit: %s", isrc)
        return cached
    if is_no_network_active():
        log.debug("MB ISRC offline (cache miss for %s)", isrc)
        return []
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
    put_cached_isrc_lookup(isrc, results)
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
    catalog = disc.catalog or meta.catalog

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

    return RBIDisc(
        album=album,
        artist=artist,
        disc_number=(
            meta.disc_number if meta.disc_number is not None else disc.disc_number
        ),
        disc_total=(
            meta.disc_total if meta.disc_total is not None else disc.disc_total
        ),
        catalog=catalog,
        disc_id=disc.disc_id,
        tracks=new_tracks,
        release_date=disc.release_date or meta.release_date or None,
        original_release_date=disc.original_release_date
        or meta.original_release_date
        or None,
        low_dynamic_range=disc.low_dynamic_range,
        original_release_found=disc.original_release_found,
        original_release_title=disc.original_release_title,
        original_release_year=disc.original_release_year,
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

    return RBIDisc(
        album=meta.album or disc.album,
        artist=meta.artist or disc.artist,
        disc_number=(
            meta.disc_number if meta.disc_number is not None else disc.disc_number
        ),
        disc_total=(
            meta.disc_total if meta.disc_total is not None else disc.disc_total
        ),
        catalog=meta.catalog or disc.catalog,
        disc_id=disc.disc_id,
        tracks=new_tracks,
        release_date=meta.release_date or disc.release_date or None,
        original_release_date=meta.original_release_date
        or disc.original_release_date
        or None,
        low_dynamic_range=disc.low_dynamic_range,
        original_release_found=disc.original_release_found,
        original_release_title=disc.original_release_title,
        original_release_year=disc.original_release_year,
        mb_release_id=meta.mb_release_id or disc.mb_release_id or None,
        mb_release_group_id=meta.mb_release_group_id
        or disc.mb_release_group_id
        or None,
        set_title=meta.set_title or disc.set_title,
    )


# R1: minimum number of agreeing per-track ISRC pairs required to commit a
# multi-match disambiguation. Below this we prefer no-auto-merge (blank but
# correctable) over a confident-but-possibly-wrong choice.
_MIN_ISRC_AGREE = 2


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
    return candidates[top_rid]


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
    if top_score < _MIN_ISRC_AGREE:
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
    """Pick the match whose barcode UNIQUELY equals the disc's Q-channel MCN.

    The MCN read from the disc subchannel is the strongest pressing-level
    signal available, resolved before the MB lookup runs. Candidate ``catalog``
    fields are already normalised EAN-13 (``normalize_barcode`` in
    ``_parse_release``), so we normalise the disc MCN the same way and compare.

    A *unique* barcode hit identifies that exact release — safe to merge in
    full. When the MCN matches **several** candidates (a barcode shared across
    country variants, e.g. DE + XE), the specific pressing is genuinely
    undetermined; we return None so the caller falls back to agreed-facts-only
    population rather than fabricate one pressing's date / release id. Returns
    None when the disc has no MCN or no candidate barcode matches.
    """
    from cdda2img.barcode import normalize_barcode

    mcn = normalize_barcode(disc.catalog)
    if not mcn:
        return None
    hits = [m for m in matches if m.catalog and m.catalog == mcn]
    return hits[0] if len(hits) == 1 else None


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
    """
    winner = _disambiguate_by_isrcs(matches, disc)
    if winner is not None:
        return winner, "isrc"
    winner = _disambiguate_by_mcn(matches, disc)
    if winner is not None:
        return winner, "mcn"
    return None, ""


def _build_agreed_facts_meta(matches: list[DiscMeta], rg_id: str) -> DiscMeta:
    """Synthesise a DiscMeta of ONLY the facts every candidate in *rg_id* agrees on.

    Used when an MB disc-ID multi-match cannot be resolved to a single pressing
    (no ISRC winner, no unique MCN hit). Rather than fabricate a specific
    pressing's details, we populate the unambiguous, album-level facts shared
    by every candidate in the plurality release-group:

      * ``mb_release_group_id`` — always (lets original-release resolve);
      * ``release_date`` — a 4-digit **year** only when every dated candidate
        agrees on it (the disc's own year, e.g. four 1983 pressings ⇒ "1983");
      * per-track ``isrc`` — only where every candidate listing that track
        agrees on a single value.

    Deliberately left None: country, catalogue number, exact date, and
    ``mb_release_id`` — genuinely undetermined across the multi-match, so we do
    not guess them. ``_merge_into_disc`` fills blanks only, so this never
    overwrites a value the disc already carries.
    """
    group = [m for m in matches if m.mb_release_group_id == rg_id]
    years = {m.release_date[:4] for m in group if m.release_date}
    agreed_year = years.pop() if len(years) == 1 else None

    isrc_by_track: dict[int, set[str]] = {}
    for m in group:
        for t in m.tracks:
            if t.number is not None and t.isrc:
                isrc_by_track.setdefault(t.number, set()).add(t.isrc)
    tracks = [
        TrackMeta(number=n, isrc=next(iter(s)))
        for n, s in sorted(isrc_by_track.items())
        if len(s) == 1
    ]
    return DiscMeta(
        mb_release_group_id=rg_id,
        release_date=agreed_year,
        tracks=tracks,
        source="musicbrainz",
    )


class MBPrepopResult(NamedTuple):
    """Aggregate of a MusicBrainz disc-ID prepop run, including diagnostic counts."""

    disc: RBIDisc
    # R16: each hint is (mb_release_id, normalised barcode). Empty-string MBID
    # is acceptable for releases that lack one (defensive); R1 will simply not
    # match such entries. Order follows MB's match order.
    barcode_hints: list[tuple[str, str]]
    match_count: int  # total MB matches returned (0 = disc-ID unknown to MB)
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


def prepopulate_from_mb(disc: RBIDisc, *, verbose: bool = True) -> MBPrepopResult:
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
    matches = lookup_disc_id(disc)
    # R16: tag each hint with its source MBID. Preserve match order while
    # deduplicating exact (mbid, barcode) pairs — different releases with the
    # same barcode keep separate entries so future lookups can disambiguate.
    hints: list[tuple[str, str]] = list(
        dict.fromkeys((m.mb_release_id or "", m.catalog) for m in matches if m.catalog)
    )
    if hints:
        log.debug("MB barcode hints from %d match(es): %s", len(matches), hints)
    if not matches:
        # R4: zero-disc-ID match → try ISRC tally as a fallback.
        winner = _resolve_via_isrc_tally(disc)
        if winner is not None:
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
    if len(matches) > 1:
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
                isrc_disambiguated=(method == "isrc"),
                mb_candidate_album=winner.album,
                mb_candidate_artist=winner.artist,
                meta=winner,
            )
        # No single pressing could be resolved. Rather than guess one, merge
        # only the facts every candidate in the plurality release-group agrees
        # on — year, shared per-track ISRCs, and the release-group itself (so
        # original-release can still resolve). Returned as ``meta`` so the
        # parallel pre-menu path (_finalize_import) re-applies it onto the
        # CDDB-merged disc exactly like a single-match meta.
        rg = _plurality_release_group(matches)
        if rg is not None:
            agreed = _build_agreed_facts_meta(matches, rg)
            disc = _merge_into_disc(agreed, disc)
            log.debug(
                "MB multi-match (%d): merged agreed facts (year=%s, %d ISRCs, rg=%s)",
                len(matches),
                agreed.release_date,
                len(agreed.tracks),
                rg,
            )
            return MBPrepopResult(
                disc, hints, len(matches), isrc_disambiguated=False, meta=agreed
            )
        log.debug("MB disc ID returned %d matches; no plurality RG", len(matches))
        return MBPrepopResult(disc, hints, len(matches), isrc_disambiguated=False)
    meta = matches[0]
    updated = _merge_into_disc(meta, disc)
    if verbose:
        date_str = f"  ({meta.release_date})" if meta.release_date else ""
        print(f'  MusicBrainz: matched "{meta.album}" by {meta.artist}{date_str}')
    return MBPrepopResult(
        updated,
        hints,
        len(matches),
        isrc_disambiguated=False,
        mb_candidate_album=meta.album,
        mb_candidate_artist=meta.artist,
        meta=meta,
    )
