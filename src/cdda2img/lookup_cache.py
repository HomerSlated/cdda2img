"""
lookup_cache.py — SQLite cache for remote lookups (R7).

Four tables, all following the same ``(key, fetched_at, payload)`` shape:

  * ``disc_id_lookups``  — MB disc-ID  →  DiscMeta[]      (30-day TTL)
  * ``isrc_lookups``     — MB ISRC     →  DiscMeta[]      (infinite TTL,
        because the ISRC→recording mapping is immutable in practice)
  * ``discogs_barcode``  — barcode     →  DiscMeta[]      (30-day TTL)
  * ``cddb_lookups``     — CDDB id     →  DiscMeta[]      (30-day TTL)

The cache co-locates with `drive_offsets.db` under XDG data home but
lives in a separate SQLite file (`lookup_cache.db`) so that a cache
delete / corruption / TTL eviction can never damage the authoritative
AccurateRip drive catalogue.

Cache semantics:
  * **TTL:** per-table (see above). Stale entries are treated as misses
    and over-written on the next successful fetch.
  * **Failure-tolerant:** cache open / read / write errors degrade to
    "behave as if uncached". The cache is never authoritative — a miss
    always falls through to a live query.
  * **Empty results are cacheable.** An MB disc-ID that returns 0
    matches today will almost certainly return 0 again tomorrow;
    caching the empty list saves the network round-trip.
  * **Payload versioning.** Each row's payload is wrapped with a format
    version (``_PAYLOAD_VERSION``). A row whose version differs from the
    running code's — including legacy *unversioned* rows written before
    this mechanism existed — is treated as a miss and re-fetched. Bump
    the version whenever the serialised ``DiscMeta`` shape *or the parse
    semantics that produce it* change, so that a parser fix is never
    silently defeated by stale cached output for up to the TTL. The bump
    is global across all four tables; if a future change ever touches a
    single parser only, prefer a per-table version here over a global
    bump so the infinite-TTL ISRC cache (the one place a re-fetch is not
    cheap) is not needlessly wiped.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cdda2img.lookup_result import DiscMeta

log = logging.getLogger(__name__)

# 30 days. Matches the drive-offsets catalogue cooldown. Applies to
# disc-ID, Discogs-barcode, and CDDB tables.
_CACHE_TTL_SECONDS = 30 * 86400

# ISRC mappings are immutable in practice (an ISRC is bound to a single
# recording for its lifetime). A None TTL is the in-band signal for
# "never expires"; consumers must handle None explicitly.
_ISRC_CACHE_TTL_SECONDS: int | None = None

# Cache payload format version. Stored alongside the data in every row
# (see the module docstring). Increment this on any change to the
# serialised DiscMeta shape OR to the parse semantics feeding the cache —
# a mismatch (including legacy unversioned rows) is treated as a miss.
_PAYLOAD_VERSION = 1


def cache_db_path() -> Path:
    """``$XDG_DATA_HOME/cdda2img/lookup_cache.db``."""
    from cdda2img.db import data_dir

    return data_dir() / "lookup_cache.db"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS disc_id_lookups (
    mb_disc_id TEXT PRIMARY KEY,
    fetched_at INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS isrc_lookups (
    isrc TEXT PRIMARY KEY,
    fetched_at INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discogs_barcode (
    barcode TEXT PRIMARY KEY,
    fetched_at INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cddb_lookups (
    cddb_disc_id TEXT PRIMARY KEY,
    fetched_at INTEGER NOT NULL,
    payload TEXT NOT NULL
);
"""


def _open_cache_db() -> sqlite3.Connection:
    """Open the cache DB (creating it and the schema on first use)."""
    path = cache_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def _serialise_meta(meta: DiscMeta) -> dict:
    return dataclasses.asdict(meta)


def _deserialise_meta(d: dict) -> DiscMeta:
    from cdda2img.lookup_result import DiscMeta, TrackMeta

    tracks_data = d.pop("tracks", [])
    return DiscMeta(
        **d,
        tracks=[TrackMeta(**t) for t in tracks_data],
    )


def _get_generic(
    table: str, key_col: str, key: str, ttl_seconds: int | None
) -> list[DiscMeta] | None:
    """Generic cache read. *ttl_seconds=None* means "never expires"."""
    try:
        conn = _open_cache_db()
    except sqlite3.Error as exc:
        log.warning("lookup_cache open failed: %s", exc)
        return None
    try:
        row = conn.execute(
            f"SELECT fetched_at, payload FROM {table} WHERE {key_col} = ?",  # noqa: S608
            (key,),
        ).fetchone()
        if row is None:
            return None
        fetched_at, payload = row
        if ttl_seconds is not None and time.time() - fetched_at > ttl_seconds:
            return None
        try:
            parsed = json.loads(payload)
            # Version gate: a non-dict payload is a legacy unversioned row
            # (bare list); a dict with a different "v" was written by code
            # with incompatible parse/serialise semantics. Either is a miss.
            if not isinstance(parsed, dict) or parsed.get("v") != _PAYLOAD_VERSION:
                return None
            return [_deserialise_meta(d) for d in parsed["data"]]
        except (TypeError, ValueError, KeyError) as exc:
            log.warning(
                "lookup_cache deserialise failed for %s/%s: %s", table, key, exc
            )
            return None
    finally:
        conn.close()


def _put_generic(table: str, key_col: str, key: str, metas: list[DiscMeta]) -> None:
    """Generic cache write. Over-writes any prior entry."""
    try:
        conn = _open_cache_db()
    except sqlite3.Error as exc:
        log.warning("lookup_cache open failed (write): %s", exc)
        return
    try:
        payload = json.dumps({
            "v": _PAYLOAD_VERSION,
            "data": [_serialise_meta(m) for m in metas],
        })
        conn.execute(
            f"INSERT OR REPLACE INTO {table} "  # noqa: S608
            f"({key_col}, fetched_at, payload) VALUES (?, ?, ?)",
            (key, int(time.time()), payload),
        )
        conn.commit()
    except sqlite3.Error as exc:
        log.warning("lookup_cache write failed for %s/%s: %s", table, key, exc)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Per-table thin wrappers — each pins (table, key_col, ttl).
# ---------------------------------------------------------------------------


def get_cached_disc_id_lookup(mb_disc_id: str) -> list[DiscMeta] | None:
    """R7: cached MB disc-ID lookup. 30-day TTL."""
    return _get_generic("disc_id_lookups", "mb_disc_id", mb_disc_id, _CACHE_TTL_SECONDS)


def put_cached_disc_id_lookup(mb_disc_id: str, metas: list[DiscMeta]) -> None:
    _put_generic("disc_id_lookups", "mb_disc_id", mb_disc_id, metas)


def get_cached_isrc_lookup(isrc: str) -> list[DiscMeta] | None:
    """R7: cached MB ISRC lookup. Infinite TTL — ISRC mappings are immutable."""
    return _get_generic("isrc_lookups", "isrc", isrc, _ISRC_CACHE_TTL_SECONDS)


def put_cached_isrc_lookup(isrc: str, metas: list[DiscMeta]) -> None:
    _put_generic("isrc_lookups", "isrc", isrc, metas)


def get_cached_discogs_barcode(barcode: str) -> list[DiscMeta] | None:
    """R7: cached Discogs barcode lookup. 30-day TTL."""
    return _get_generic("discogs_barcode", "barcode", barcode, _CACHE_TTL_SECONDS)


def put_cached_discogs_barcode(barcode: str, metas: list[DiscMeta]) -> None:
    _put_generic("discogs_barcode", "barcode", barcode, metas)


def get_cached_cddb_lookup(cddb_disc_id: str) -> list[DiscMeta] | None:
    """R7: cached CDDB disc-ID lookup. 30-day TTL."""
    return _get_generic(
        "cddb_lookups", "cddb_disc_id", cddb_disc_id, _CACHE_TTL_SECONDS
    )


def put_cached_cddb_lookup(cddb_disc_id: str, metas: list[DiscMeta]) -> None:
    _put_generic("cddb_lookups", "cddb_disc_id", cddb_disc_id, metas)
