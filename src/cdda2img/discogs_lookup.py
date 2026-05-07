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

from cdda2img.lookup_result import DiscMeta
from cdda2img.mb_lookup import _classify_remaster, _parse_year

log = logging.getLogger(__name__)

_USER_AGENT = f"cdda2img/{importlib.metadata.version('cdda2img')} +https://github.com/HomerSlated/cdda2img"


def is_available() -> bool:
    """Return True if DISCOGS_TOKEN is set in the environment."""
    return bool(os.environ.get("DISCOGS_TOKEN"))


def _get_client():
    token = os.environ.get("DISCOGS_TOKEN", "")
    if not token:
        return None
    try:
        import warnings

        # discogs_client 2.3.0 uses \w in a non-raw string (fetchers.py:102);
        # Python 3.12+ flags this as SyntaxWarning on first module compilation.
        # Remove this filter once the upstream library ships a fix.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore", category=SyntaxWarning, module=r"discogs_client\..*"
            )
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
    barcode = barcodes[0] if barcodes else ""

    return DiscMeta(
        album=album or None,
        artist=artist or None,
        catalog=barcode or None,
        release_date=year,
        country=str(country) if country else None,
        label=str(label) if label else None,
        catalog_number=catalog_number or None,
        remastered_source=_classify_remaster(album, None, _parse_year(year)),
        source="discogs",
    )


def search_releases(query: str, limit: int = 25) -> list[DiscMeta]:
    """Text search for releases on Discogs. Returns [] if token not set or on error."""
    client = _get_client()
    if not client:
        return []
    try:
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
