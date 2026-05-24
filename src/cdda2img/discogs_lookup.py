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
import re

from cdda2img.lookup_result import DiscMeta, TrackMeta
from cdda2img.mb_lookup import _classify_remaster, _parse_year

log = logging.getLogger(__name__)

_USER_AGENT = f"cdda2img/{importlib.metadata.version('cdda2img')} +https://github.com/HomerSlated/cdda2img"


def is_available() -> bool:
    """Return True if DISCOGS_TOKEN is set in the environment."""
    return bool(os.environ.get("DISCOGS_TOKEN"))


def normalize_barcode(raw: str | None) -> str | None:
    """Normalize a raw barcode to GTIN-13 (EAN-13), or return None.

    Strips non-digit characters; pads a 12-digit UPC-A with a leading '0'
    (GS1 §1.3.1 Table 1-9: GTIN-12 → GTIN-13). Rejects anything that isn't
    exactly 13 digits after stripping and padding.
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12:
        digits = "0" + digits
    return digits if len(digits) == 13 else None


def _get_client():
    token = os.environ.get("DISCOGS_TOKEN", "")
    if not token:
        return None
    try:
        import discogs_client  # type: ignore[import-untyped]

        return discogs_client.Client(_USER_AGENT, user_token=token)
    except ImportError:
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
        remastered_source=_classify_remaster(album, None, _parse_year(year)),
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
        remastered_source=_classify_remaster(album, None, _parse_year(year)),
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
    """Lookup by barcode (EAN-13 / UPC). Returns [] if token not set or on error."""
    client = _get_client()
    if not client:
        return []
    try:
        results = client.search(barcode, type="release")
        page1 = results.page(1)
        return [_parse_result(r) for r in page1[:25]]
    except Exception as exc:
        log.debug("Discogs barcode search failed: %s", exc)
        return []
