"""
lookup_result.py — Shared data types for remote metadata lookups.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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
    primary_type: str | None = None  # MB release-group primary type: Album/Single/EP
    track_count: int | None = None  # tracks on the matched medium (search-stub hint)
    disc_number: int | None = (
        None  # 1-based position in a multi-disc set; None = unknown
    )
    disc_total: int | None = None  # total discs in set; None = unknown
    set_title: str | None = (
        None  # box set / release title when disc has its own album title
    )
    source: str = (
        "unknown"  # "cdtext" | "embedded" | "musicbrainz" | "discogs" | "manual"
    )
    tracks: list[TrackMeta] = field(default_factory=list)
