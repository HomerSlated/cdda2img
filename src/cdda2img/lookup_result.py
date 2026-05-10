"""
lookup_result.py — Shared data types for remote metadata lookups.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Remaster status constants stored in RBI provenance metadata
REMASTERED_UNKNOWN = "UNKNOWN"
REMASTERED_NO = "NO"
REMASTERED_POSSIBLE = "POSSIBLE"
REMASTERED_YES = "YES"

# Conservative Loudness War inflection point for CD
LOUDNESS_WAR_YEAR = 1994


@dataclass
class TrackMeta:
    """Metadata for a single track returned by a remote lookup."""

    number: int | None = None
    title: str | None = None
    performer: str | None = None
    isrc: str | None = None
    duration_ms: int | None = None  # from MB, for track length verification


@dataclass
class DiscMeta:
    """Rich disc metadata returned by a remote lookup service."""

    album: str | None = None
    artist: str | None = None
    catalog: str | None = None  # MCN / EAN-13 / barcode
    mb_disc_id: str | None = None  # computed SHA1 disc ID
    mb_release_id: str | None = None  # MusicBrainz release UUID
    mb_release_group_id: str | None = None  # MusicBrainz release group UUID
    discogs_release_id: int | None = None  # Discogs integer release ID
    release_date: str | None = None  # YYYY, YYYY-MM, or YYYY-MM-DD
    original_release_date: str | None = None
    country: str | None = None
    label: str | None = None
    catalog_number: str | None = None  # label catalogue number (e.g. "XYZ-001")
    remastered_source: str = REMASTERED_UNKNOWN
    source: str = (
        "unknown"  # "cdtext" | "embedded" | "musicbrainz" | "discogs" | "manual"
    )
    tracks: list[TrackMeta] = field(default_factory=list)


def merge_disc_meta(base: DiscMeta, update: DiscMeta) -> DiscMeta:
    """Return a new DiscMeta with None fields in *base* filled from *update*.

    Existing non-None values in *base* are never overwritten.
    Track lists use *base* if non-empty, otherwise *update*.
    Remaster status uses *base* unless it is UNKNOWN.
    """
    scalar_fields = (
        "album",
        "artist",
        "catalog",
        "mb_disc_id",
        "mb_release_id",
        "mb_release_group_id",
        "discogs_release_id",
        "release_date",
        "original_release_date",
        "country",
        "label",
        "catalog_number",
    )
    kwargs: dict = {f: getattr(base, f) or getattr(update, f) for f in scalar_fields}
    kwargs["source"] = base.source
    kwargs["remastered_source"] = (
        update.remastered_source
        if base.remastered_source == REMASTERED_UNKNOWN
        else base.remastered_source
    )
    kwargs["tracks"] = base.tracks if base.tracks else update.tracks
    return DiscMeta(**kwargs)
