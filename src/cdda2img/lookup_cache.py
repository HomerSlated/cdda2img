"""
lookup_cache.py — SQLite cache for remote lookups (R7).

Scoped to **MB disc-ID lookups** in the initial implementation. The other
three caches planned by the analysis report — ISRC lookups, Discogs
barcode lookups, CDDB disc-ID lookups — follow the same shape and can
be added later by replicating the `disc_id_lookups` table pattern.

The cache co-locates with `drive_offsets.db` under XDG data home but
lives in a separate SQLite file (`lookup_cache.db`) so that a cache
delete / corruption / TTL eviction can never damage the authoritative
AccurateRip drive catalogue.

Cache semantics:
  * **TTL:** 30 days. Stale entries are treated as misses and over-written
    on the next successful fetch.
  * **Failure-tolerant:** cache open / read / write errors degrade to
    "behave as if uncached". The cache is never authoritative — a miss
    always falls through to a live MB query.
  * **Empty results are cacheable.** An MB disc-ID that returns 0 matches
    today will almost certainly return 0 again tomorrow; caching the empty
    list saves the network round-trip.
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

# 30 days. Matches the drive-offsets catalogue cooldown.
_CACHE_TTL_SECONDS = 30 * 86400


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


def get_cached_disc_id_lookup(mb_disc_id: str) -> list[DiscMeta] | None:
    """Return the cached lookup result for *mb_disc_id*, or None.

    Returns None on cache miss, TTL expiry, deserialisation failure, or
    any sqlite error — callers MUST treat None as "no cache" and fall
    through to a live network query.
    """
    try:
        conn = _open_cache_db()
    except sqlite3.Error as exc:
        log.warning("lookup_cache open failed: %s", exc)
        return None
    try:
        row = conn.execute(
            "SELECT fetched_at, payload FROM disc_id_lookups WHERE mb_disc_id = ?",
            (mb_disc_id,),
        ).fetchone()
        if row is None:
            return None
        fetched_at, payload = row
        if time.time() - fetched_at > _CACHE_TTL_SECONDS:
            return None
        try:
            data = json.loads(payload)
            return [_deserialise_meta(d) for d in data]
        except (TypeError, ValueError, KeyError) as exc:
            log.warning("lookup_cache deserialise failed for %s: %s", mb_disc_id, exc)
            return None
    finally:
        conn.close()


def put_cached_disc_id_lookup(mb_disc_id: str, metas: list[DiscMeta]) -> None:
    """Write *metas* to the cache for *mb_disc_id*. Over-writes any prior entry."""
    try:
        conn = _open_cache_db()
    except sqlite3.Error as exc:
        log.warning("lookup_cache open failed (write): %s", exc)
        return
    try:
        payload = json.dumps([_serialise_meta(m) for m in metas])
        conn.execute(
            "INSERT OR REPLACE INTO disc_id_lookups "
            "(mb_disc_id, fetched_at, payload) VALUES (?, ?, ?)",
            (mb_disc_id, int(time.time()), payload),
        )
        conn.commit()
    except sqlite3.Error as exc:
        log.warning("lookup_cache write failed for %s: %s", mb_disc_id, exc)
    finally:
        conn.close()
