"""
test_lookup_cache.py — R7 SQLite cache round-trip and TTL.
"""

from __future__ import annotations

from unittest.mock import patch

from cdda2img.lookup_cache import (
    _CACHE_TTL_SECONDS,
    _ISRC_CACHE_TTL_SECONDS,
    get_cached_cddb_lookup,
    get_cached_disc_id_lookup,
    get_cached_discogs_barcode,
    get_cached_isrc_lookup,
    put_cached_cddb_lookup,
    put_cached_disc_id_lookup,
    put_cached_discogs_barcode,
    put_cached_isrc_lookup,
)
from cdda2img.lookup_result import DiscMeta, TrackMeta


def test_cache_miss_returns_none() -> None:
    """An empty cache returns None for any key."""
    assert get_cached_disc_id_lookup("unknown-disc-id") is None


def test_cache_round_trip_single_meta() -> None:
    """Write and read back a single DiscMeta (with track listing)."""
    meta = DiscMeta(
        album="Test Album",
        artist="Test Artist",
        catalog="0075992377423",
        mb_release_id="rid-1",
        tracks=[
            TrackMeta(number=1, title="Track 1", isrc="USEE18300025"),
            TrackMeta(number=2, title="Track 2"),
        ],
    )
    put_cached_disc_id_lookup("disc-id-abc", [meta])
    result = get_cached_disc_id_lookup("disc-id-abc")
    assert result is not None
    assert len(result) == 1
    assert result[0].album == "Test Album"
    assert result[0].mb_release_id == "rid-1"
    assert len(result[0].tracks) == 2
    assert result[0].tracks[0].isrc == "USEE18300025"


def test_cache_round_trip_empty_list() -> None:
    """Caching an empty list (0-match disc-id) is also valid."""
    put_cached_disc_id_lookup("disc-id-empty", [])
    result = get_cached_disc_id_lookup("disc-id-empty")
    assert result == []


def test_cache_ttl_expiry_returns_none() -> None:
    """A row older than the TTL is treated as a miss."""
    put_cached_disc_id_lookup("disc-id-old", [DiscMeta(album="X")])

    # Patch time.time() to return a value past the TTL.
    fake_now = 99_999_999_999  # far future
    with patch("cdda2img.lookup_cache.time.time", return_value=fake_now):
        result = get_cached_disc_id_lookup("disc-id-old")
    assert result is None


def test_cache_replaces_on_repeat_write() -> None:
    """Writing twice to the same key keeps the latest version."""
    put_cached_disc_id_lookup("disc-id-replace", [DiscMeta(album="V1")])
    put_cached_disc_id_lookup("disc-id-replace", [DiscMeta(album="V2")])
    result = get_cached_disc_id_lookup("disc-id-replace")
    assert result is not None
    assert result[0].album == "V2"


def test_lookup_disc_id_uses_cache(monkeypatch) -> None:
    """A second lookup_disc_id call for the same disc hits the cache (no network)."""
    from cdda2img.mb_lookup import lookup_disc_id
    from cdda2img.rbi_format import RBIDisc, RBITocEntry

    disc = RBIDisc(
        album="Test",
        artist="Test",
        tracks=[
            RBITocEntry(
                track_number=1,
                title="T",
                performer="P",
                start_frame=0,
                duration_frames=18000,
            )
        ],
    )
    mock_response = {
        "disc": {
            "release-list": [
                {
                    "id": "release-1",
                    "title": "Test Album",
                    "artist-credit": [
                        {"artist": {"name": "Test Artist"}, "joinphrase": ""}
                    ],
                    "release-group": {"id": "rg-1", "first-release-date": "1990"},
                }
            ]
        }
    }
    call_count = {"n": 0}

    def fake_mb(*_args, **_kwargs):
        call_count["n"] += 1
        return mock_response

    monkeypatch.setattr("musicbrainzngs.get_releases_by_discid", fake_mb)
    first = lookup_disc_id(disc)
    second = lookup_disc_id(disc)
    # Network function called exactly once; second call served from cache.
    assert call_count["n"] == 1
    assert len(first) == 1
    assert len(second) == 1
    assert second[0].album == "Test Album"


def test_ttl_constant_is_30_days() -> None:
    """Sanity-pin the documented 30-day TTL."""
    assert _CACHE_TTL_SECONDS == 30 * 86400


def test_isrc_cache_ttl_is_infinite() -> None:
    """ISRC bindings are immutable — the sentinel is None."""
    assert _ISRC_CACHE_TTL_SECONDS is None


# ---------------------------------------------------------------------------
# ISRC cache (infinite TTL)
# ---------------------------------------------------------------------------


def test_isrc_cache_round_trip() -> None:
    put_cached_isrc_lookup("USEE18300025", [DiscMeta(album="X")])
    result = get_cached_isrc_lookup("USEE18300025")
    assert result is not None
    assert result[0].album == "X"


def test_isrc_cache_does_not_expire_with_time() -> None:
    """A row from epoch 0 is still served — ISRC mappings don't decay."""
    put_cached_isrc_lookup("USEE18300026", [DiscMeta(album="Y")])
    fake_future = 99_999_999_999
    with patch("cdda2img.lookup_cache.time.time", return_value=fake_future):
        result = get_cached_isrc_lookup("USEE18300026")
    assert result is not None


# ---------------------------------------------------------------------------
# Discogs barcode cache
# ---------------------------------------------------------------------------


def test_discogs_barcode_round_trip() -> None:
    put_cached_discogs_barcode("0075992377423", [DiscMeta(album="ZZ")])
    result = get_cached_discogs_barcode("0075992377423")
    assert result is not None
    assert result[0].album == "ZZ"


def test_discogs_barcode_ttl_expiry() -> None:
    put_cached_discogs_barcode("0724383697724", [DiscMeta(album="OK")])
    fake_future = 99_999_999_999
    with patch("cdda2img.lookup_cache.time.time", return_value=fake_future):
        result = get_cached_discogs_barcode("0724383697724")
    assert result is None


# ---------------------------------------------------------------------------
# CDDB cache
# ---------------------------------------------------------------------------


def test_cddb_cache_round_trip() -> None:
    put_cached_cddb_lookup("aabbcc01", [DiscMeta(album="Album1")])
    result = get_cached_cddb_lookup("aabbcc01")
    assert result is not None
    assert result[0].album == "Album1"


def test_cddb_cache_caches_empty_too() -> None:
    """Empty CDDB hit is cacheable (no-match-today probably means no-match-tomorrow)."""
    put_cached_cddb_lookup("aabbcc02", [])
    assert get_cached_cddb_lookup("aabbcc02") == []
