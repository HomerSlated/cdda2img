"""
discogs_lookup.py — Discogs metadata lookups.

Requires DISCOGS_TOKEN environment variable (free personal access token from
discogs.com/settings/developers). Returns empty lists silently when the token
is absent so callers need not branch on availability.
"""

from __future__ import annotations

import importlib.metadata
import logging
import os

from cdda2img.barcode import normalize_barcode
from cdda2img.lookup_result import DiscMeta, TrackMeta

log = logging.getLogger(__name__)

_USER_AGENT = f"cdda2img/{importlib.metadata.version('cdda2img')} +https://github.com/HomerSlated/cdda2img"


def is_available() -> bool:
    """Return True if DISCOGS_TOKEN is set in the environment.

    R10: returns False unconditionally when ``Config.no_network_services``
    is True (offline mode).
    """
    from cdda2img.config import is_no_network_active

    if is_no_network_active():
        return False
    return bool(os.environ.get("DISCOGS_TOKEN"))


def lookup_master_year(release_id: int) -> int | None:
    """R11: return the year of the Discogs master's main_release for *release_id*.

    Walks ``release.master.main_release.year`` via the discogs_client API.
    Returns None when:
      * Discogs is unavailable (no token).
      * The release has no associated master (e.g. one-off release).
      * The master has no main_release (rare).
      * Any API call raises.

    One extra Discogs API call beyond the original release fetch.
    """
    client = _get_client()
    if client is None:
        return None
    try:
        release = client.release(release_id)
        master = release.master
        if master is None:
            return None
        main_release = master.main_release
        if main_release is None:
            return None
        # main_release is a Release object; fetch its year via the same
        # discogs_client property accessor used elsewhere.
        year_raw = main_release.fetch("year")
        if not year_raw:
            return None
        return int(year_raw)
    except Exception as exc:
        log.debug("Discogs master year lookup failed for %s: %s", release_id, exc)
        return None


def _get_client():
    token = os.environ.get("DISCOGS_TOKEN", "")
    if not token:
        return None
    try:
        import discogs_client  # type: ignore[import-untyped]

        return discogs_client.Client(_USER_AGENT, user_token=token)
    except ImportError:
        return None


def _discogs_primary_type(formats) -> str | None:
    """Map a Discogs search-stub ``format`` list to an MB-style primary type.

    Discogs encodes format descriptors as a list of strings on the search stub,
    e.g. ``["CD", "Album"]`` or ``["CD", "Single"]``. We fold these to the same
    vocabulary MusicBrainz uses for its release-group primary type
    (Album / Single / EP) so the menu's Type column reads consistently across
    sources. Discogs "Compilation" (a *secondary* type in MB, where the primary
    stays "Album") folds to Album for that consistency. Returns None when no
    descriptor is recognised or the field is missing/malformed (→ "?" in the
    menu, an honest unknown).
    """
    if not isinstance(formats, list):
        return None
    tokens = {str(f).strip().lower() for f in formats if f}
    if "single" in tokens or "maxi-single" in tokens:
        return "Single"
    if "ep" in tokens:
        return "EP"
    if tokens & {"album", "lp", "mini-album", "compilation", "mixtape"}:
        return "Album"
    return None


def _parse_result(r) -> DiscMeta:
    """Parse a Discogs search result object into a DiscMeta.

    Search results return a flattened dict via r.data; full Release objects have
    richer nested structures. We handle both shapes defensively.
    """
    data: dict = getattr(r, "data", {}) or {}

    raw_title = data.get("title") or getattr(r, "title", "") or ""
    if " - " in raw_title:
        parts = raw_title.split(" - ", 1)
        artist, album = parts[0].strip(), parts[1].strip()
    else:
        artist, album = "", raw_title

    year_raw = data.get("year") or getattr(r, "year", None)
    year = str(int(year_raw)) if year_raw else None

    country = data.get("country") or getattr(r, "country", None)

    # labels: search result returns list[str]; full release returns list[Label]
    label_raw = data.get("label") or []
    label = label_raw[0] if isinstance(label_raw, list) and label_raw else ""
    if isinstance(label, dict):
        label = label.get("name", "")
    catalog_number = data.get("catno") or ""

    barcodes: list = data.get("barcode") or []
    barcode = next((n for n in (normalize_barcode(b) for b in barcodes) if n), None)

    release_id = data.get("id") or getattr(r, "id", None)
    return DiscMeta(
        album=album or None,
        artist=artist or None,
        catalog=barcode,
        discogs_release_id=int(release_id) if release_id else None,
        release_date=year,
        country=str(country) if country else None,
        label=str(label) if label else None,
        catalog_number=catalog_number or None,
        primary_type=_discogs_primary_type(data.get("format")),
        source="discogs",
    )


def _discogs_join_artists(artists: list) -> str:
    """Build a display name from a Discogs artists list (full release shape)."""
    parts: list[str] = []
    for a in artists:
        if not isinstance(a, dict):
            continue
        parts.append(a.get("anv") or a.get("name") or "")
        join = a.get("join") or ""
        if join == ",":
            parts.append(", ")
        elif join:
            parts.append(f" {join.strip()} ")
    return "".join(parts).strip().rstrip(",").strip()


def _discogs_parse_tracklist(tracklist: list) -> list[TrackMeta]:
    """Convert a Discogs full-release tracklist to TrackMeta objects."""
    tracks: list[TrackMeta] = []
    seq = 0
    for t in tracklist:
        if not isinstance(t, dict) or t.get("type_") == "heading":
            continue
        seq += 1
        pos = t.get("position") or ""
        try:
            number = int(pos)
        except (ValueError, TypeError):
            number = seq  # vinyl-style positions (A1, B2) → sequential fallback
        tracks.append(TrackMeta(number=number, title=t.get("title") or None))
    return tracks


def _parse_full_release(r) -> DiscMeta:
    """Parse a full Discogs Release object (fetched by ID, not from search).

    Full releases have a separate artists list and a flat tracklist, unlike search
    stubs which encode "Artist - Album" in a single title field with no tracklist.
    """
    data: dict = getattr(r, "data", {}) or {}

    album = data.get("title") or ""
    artist = _discogs_join_artists(data.get("artists") or [])

    year_raw = data.get("year")
    year = str(int(year_raw)) if year_raw else None

    country = data.get("country") or None

    labels: list = data.get("labels") or []
    label = ""
    catalog_number = ""
    if labels and isinstance(labels[0], dict):
        label = labels[0].get("name") or ""
        catalog_number = labels[0].get("catno") or ""

    barcode_idents = [
        ident
        for ident in (data.get("identifiers") or [])
        if isinstance(ident, dict) and (ident.get("type") or "").lower() == "barcode"
    ]
    scanned = [
        ident
        for ident in barcode_idents
        if "scanned" in (ident.get("description") or "").lower()
    ]
    # Prefer Scanned; within each group take the first value that normalises.
    barcode = next(
        (n for n in (normalize_barcode(i.get("value")) for i in scanned) if n), None
    ) or next(
        (n for n in (normalize_barcode(i.get("value")) for i in barcode_idents) if n),
        None,
    )

    return DiscMeta(
        album=album or None,
        artist=artist or None,
        catalog=barcode,
        discogs_release_id=int(data["id"]) if data.get("id") else None,
        release_date=year,
        country=str(country) if country else None,
        label=str(label) if label else None,
        catalog_number=catalog_number or None,
        source="discogs",
        tracks=_discogs_parse_tracklist(data.get("tracklist") or []),
    )


def fetch_release(release_id: int) -> DiscMeta | None:
    """Fetch a full Discogs release by integer ID and return its complete metadata.

    Returns None when the token is absent, the library is unavailable, or on error.
    Use this after the user selects a search stub to populate the track listing.
    """
    client = _get_client()
    if not client:
        return None
    try:
        r = client.release(release_id)
        r.refresh()  # client.release() returns a stub; refresh() fetches the full data
        return _parse_full_release(r)
    except Exception as exc:
        log.debug("Discogs release fetch failed for %d: %s", release_id, exc)
        return None


def search_releases(
    query: str = "",
    *,
    artist: str = "",
    release_title: str = "",
    limit: int = 25,
) -> list[DiscMeta]:
    """Search for releases on Discogs.

    When *artist* or *release_title* are provided they are passed as structured
    field parameters (``artist=``, ``release_title=``), which produce more precise
    results than the free-text ``q=`` form.  Falls back to ``q=query`` when
    neither structured field is set.
    """
    client = _get_client()
    if not client:
        return []
    try:
        if artist or release_title:
            results = client.search(
                type="release", artist=artist, release_title=release_title
            )
        else:
            results = client.search(query, type="release")
        page1 = results.page(1)
        return [_parse_result(r) for r in page1[:limit]]
    except Exception as exc:
        log.debug("Discogs search failed: %s", exc)
        return []


def search_by_barcode(barcode: str) -> list[DiscMeta]:
    """Lookup by barcode (EAN-13 / UPC). Returns [] if token not set or on error.

    R7: results cached in ``discogs_barcode`` with a 30-day TTL.
    """
    from cdda2img.lookup_cache import (
        get_cached_discogs_barcode,
        put_cached_discogs_barcode,
    )

    cached = get_cached_discogs_barcode(barcode)
    if cached is not None:
        log.debug("Discogs barcode cache hit: %s", barcode)
        return cached
    client = _get_client()
    if not client:
        return []
    try:
        results = client.search(barcode, type="release")
        page1 = results.page(1)
        parsed = [_parse_result(r) for r in page1[:25]]
    except Exception as exc:
        log.debug("Discogs barcode search failed: %s", exc)
        return []
    put_cached_discogs_barcode(barcode, parsed)
    return parsed
