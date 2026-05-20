"""
mb_lookup.py — MusicBrainz metadata lookups.

Provides disc ID computation from RBI TOC data and network lookups via musicbrainzngs.
The disc ID is computed in pure Python (no native libdiscid required):
  SHA1 of (first_track_byte, last_track_byte, lead_out_uint32, track_offsets[99] uint32s),
  all big-endian, then base64 with +→. /→_ =→-.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import logging
import struct

import musicbrainzngs  # type: ignore[import-untyped]

from cdda2img.lookup_result import (
    LOUDNESS_WAR_YEAR,
    REMASTER_KEYWORDS,
    REMASTERED_NO,
    REMASTERED_POSSIBLE,
    REMASTERED_UNKNOWN,
    REMASTERED_YES,
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
    """
    data = struct.pack(">BB", first_track, last_track)
    data += struct.pack(">I", lead_out_offset)
    for i in range(99):
        data += struct.pack(">I", track_offsets[i] if i < len(track_offsets) else 0)
    sha1 = hashlib.sha1(data).digest()  # noqa: S324  # MB spec mandates SHA-1
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


def _classify_remaster(
    title: str, original_year: int | None, current_year: int | None
) -> str:
    has_keyword = "remaster" in title.lower()
    if original_year and original_year < LOUDNESS_WAR_YEAR:
        if current_year and current_year >= LOUDNESS_WAR_YEAR:
            return REMASTERED_YES if has_keyword else REMASTERED_POSSIBLE
        return REMASTERED_NO
    if current_year and current_year >= LOUDNESS_WAR_YEAR:
        return REMASTERED_YES if has_keyword else REMASTERED_POSSIBLE
    return REMASTERED_UNKNOWN


def guess_remaster_status(disc: RBIDisc) -> str:
    """Auto-guess remaster status from the disc's populated metadata.

    Called when disc.remastered_source is UNKNOWN after pre-population.
    Makes no network requests.
    """
    title_lower = (disc.album or "").lower()
    has_keyword = any(kw in title_lower for kw in REMASTER_KEYWORDS)
    release_year = _parse_year(disc.release_date)
    original_year = _parse_year(disc.original_release_date)
    if has_keyword:
        return REMASTERED_YES
    if original_year and release_year and original_year < release_year:
        return REMASTERED_YES
    if release_year and release_year >= LOUDNESS_WAR_YEAR:
        return REMASTERED_POSSIBLE
    if release_year and release_year < LOUDNESS_WAR_YEAR:
        return REMASTERED_NO
    return REMASTERED_UNKNOWN


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

    tracks: list[TrackMeta] = []
    for medium in matched_mediums:
        for track in medium.get("track-list") or []:
            recording = track.get("recording") or {}
            isrc_list = recording.get("isrc-list") or []
            length = recording.get("length")
            tracks.append(
                TrackMeta(
                    number=_parse_track_number(track),
                    title=track.get("title") or recording.get("title"),
                    performer=artist or None,
                    isrc=isrc_list[0] if isrc_list else None,
                    duration_ms=int(length) if length else None,
                )
            )

    return DiscMeta(
        album=album_title,
        artist=artist or None,
        catalog=release.get("barcode") or None,
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
        remastered_source=_classify_remaster(
            release.get("title") or "",
            _parse_year(original_date),
            _parse_year(date),
        ),
        source="musicbrainz",
        tracks=tracks,
    )


# ---------------------------------------------------------------------------
# Network lookups
# ---------------------------------------------------------------------------


def lookup_disc_id(disc: RBIDisc) -> list[DiscMeta]:
    """Look up releases on MusicBrainz by Disc ID computed from the disc TOC.

    Returns a list of matching DiscMeta (empty on no match or network error).
    """
    _setup_useragent()
    disc_id_str = disc_id_from_rbi(disc)
    if not disc_id_str:
        return []
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
    return [_parse_release(r, _disc_id=disc_id_str) for r in releases]


def lookup_release(release_id: str, disc_number: int | None = None) -> DiscMeta | None:
    """Fetch a single MusicBrainz release by ID with full track listing.

    *disc_number* selects the matching medium for multi-disc releases so that
    only that disc's tracks are returned.  Pass ``disc.disc_number`` when
    applying to a known disc in a set.

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
    """Text search for releases on MusicBrainz. Returns empty list on error."""
    _setup_useragent()
    log.debug("MusicBrainz text search: %r", query)
    try:
        result = musicbrainzngs.search_releases(query, limit=limit)
    except (musicbrainzngs.ResponseError, musicbrainzngs.NetworkError) as exc:
        log.debug("MusicBrainz text search failed: %s", exc)
        return []
    return [_parse_release(r) for r in (result.get("release-list") or [])]


def lookup_release_group(rg_id: str) -> list[DiscMeta]:
    """Fetch all releases in a MusicBrainz release group, sorted by date (oldest first).

    Used by the 'Find Original Release' menu to browse all pressings in a release group.
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
                    remastered_source=_classify_remaster(
                        release.get("title") or "",
                        _parse_year(original_date),
                        _parse_year(date),
                    ),
                    source="musicbrainz",
                )
            )
    return results


# ---------------------------------------------------------------------------
# Pre-population helpers
# ---------------------------------------------------------------------------


def _merge_into_disc(meta: DiscMeta, disc: RBIDisc) -> RBIDisc:
    """Return a new RBIDisc with None/empty/unknown fields filled from *meta*."""
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
                    isrc=entry.isrc or mt.isrc,
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
        remastered_source=(
            meta.remastered_source
            if disc.remastered_source == REMASTERED_UNKNOWN
            else disc.remastered_source
        ),
        mb_release_id=disc.mb_release_id or meta.mb_release_id or None,
        set_title=disc.set_title or meta.set_title,
    )


def _overwrite_disc(meta: DiscMeta, disc: RBIDisc) -> RBIDisc:
    """Return a new RBIDisc with all non-None *meta* fields replacing disc fields.

    Unlike _merge_into_disc (which keeps existing disc values), this always
    prefers meta's values when set — used for the 'Overwrite All' apply mode.
    """
    meta_by_num = {t.number: t for t in meta.tracks if t.number is not None}
    new_tracks: list[RBITocEntry] = []
    for entry in disc.tracks:
        mt = meta_by_num.get(entry.track_number)
        if mt:
            new_tracks.append(
                RBITocEntry(
                    track_number=entry.track_number,
                    title=mt.title or entry.title,
                    performer=mt.performer or entry.performer,
                    start_frame=entry.start_frame,
                    duration_frames=entry.duration_frames,
                    pregap_frames=entry.pregap_frames,
                    isrc=mt.isrc or entry.isrc,
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
        remastered_source=(
            meta.remastered_source
            if meta.remastered_source != REMASTERED_UNKNOWN
            else disc.remastered_source
        ),
        mb_release_id=meta.mb_release_id or disc.mb_release_id or None,
        set_title=meta.set_title or disc.set_title,
    )


def prepopulate_from_mb(disc: RBIDisc, *, verbose: bool = True) -> RBIDisc:
    """Attempt a silent MusicBrainz Disc ID lookup and fill in missing fields.

    If exactly one match is found, missing fields in *disc* are filled from the
    MusicBrainz result and a summary line is printed (when *verbose* is True).
    On zero or multiple matches (or network error) *disc* is returned unchanged.
    """
    _setup_useragent()
    matches = lookup_disc_id(disc)
    if not matches:
        return disc
    if len(matches) > 1:
        log.debug("MB disc ID returned %d matches; skipping auto-fill", len(matches))
        return disc
    meta = matches[0]
    updated = _merge_into_disc(meta, disc)
    if verbose:
        date_str = f"  ({meta.release_date})" if meta.release_date else ""
        print(f'  MusicBrainz: matched "{meta.album}" by {meta.artist}{date_str}')
    return updated
