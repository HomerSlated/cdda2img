"""
acoustid_lookup.py — AcoustID audio fingerprint lookups (optional).

Requires all three of the following to be available:
  - pyacoustid Python package  (uv add pyacoustid)
  - libchromaprint native library  (apt install libchromaprint-dev)
  - ACOUSTID_API_KEY environment variable  (free at acoustid.org)

When unavailable, all functions return empty lists so callers need not branch.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from cdda2img.lookup_result import DiscMeta

log = logging.getLogger(__name__)

_MAX_RECORDINGS = 5  # cap on recording matches to avoid excessive MB queries
_SCORE_THRESHOLD = 0.5


def is_available() -> bool:
    """Return True when pyacoustid, libchromaprint, and ACOUSTID_API_KEY are all present.

    R10: returns False unconditionally when offline mode is active.
    """
    from cdda2img.config import is_no_network_active

    if is_no_network_active():
        return False
    if not os.environ.get("ACOUSTID_API_KEY"):
        return False
    try:
        import acoustid  # type: ignore[import-untyped]  # noqa: F401
    except ImportError:
        return False
    else:
        return True


def unavailability_reason() -> str:
    """Return a human-readable explanation of why AcoustID is not available."""
    if not os.environ.get("ACOUSTID_API_KEY"):
        return "ACOUSTID_API_KEY not set (register free at acoustid.org)"
    try:
        import acoustid  # type: ignore[import-untyped]  # noqa: F401
    except ImportError:
        return "pyacoustid not installed — run: uv add pyacoustid\n  Also needs: apt install libchromaprint-dev"
    return ""


def _chain_to_mb(top: list, *, verbose: bool = False) -> list[DiscMeta]:
    """Query MusicBrainz for each (score, recording_id, title, artist) tuple in *top*.

    Returns DiscMeta items per unique release; falls back to artist/title-only on MB error.
    """
    import musicbrainzngs  # type: ignore[import-untyped]

    from cdda2img.lookup_result import TrackMeta
    from cdda2img.mb_lookup import _artist_credit_name

    results: list[DiscMeta] = []
    seen_releases: set[str] = set()

    for _score, recording_id, fallback_title, fallback_artist in top:
        try:
            mb_result = musicbrainzngs.get_recording_by_id(
                recording_id,
                # "release-groups" folds into this same request (zero extra
                # queries) and is what populates the per-release primary type.
                includes=["artists", "releases", "isrcs", "release-groups"],
            )
        except Exception as exc:
            log.debug("MB recording lookup for %s failed: %s", recording_id, exc)
            if verbose:
                print(f"    MB {recording_id[:8]}…: FAILED ({exc})")
            results.append(
                DiscMeta(
                    artist=fallback_artist or None,
                    source="acoustid",
                    tracks=[
                        TrackMeta(
                            title=fallback_title or None,
                            performer=fallback_artist or None,
                        )
                    ],
                )
            )
            continue

        recording = mb_result.get("recording") or {}
        rec_artist = (
            _artist_credit_name(recording.get("artist-credit") or [])
            or fallback_artist
            or None
        )
        rec_title = recording.get("title") or fallback_title or None
        isrc_list = recording.get("isrc-list") or []
        isrc = isrc_list[0] if isrc_list else None
        releases = recording.get("release-list") or []

        if verbose:
            print(
                f"    MB {recording_id[:8]}…: '{rec_title}' — {len(releases)} release(s)"
            )

        if not releases:
            results.append(
                DiscMeta(
                    artist=rec_artist,
                    source="acoustid",
                    tracks=[
                        TrackMeta(title=rec_title, performer=rec_artist, isrc=isrc)
                    ],
                )
            )
            continue

        for release in releases:
            rid = release.get("id")
            if not rid or rid in seen_releases:
                continue
            seen_releases.add(rid)
            rg = release.get("release-group") or {}
            date = release.get("date") or ""
            original_date = rg.get("first-release-date") or ""
            results.append(
                DiscMeta(
                    album=release.get("title") or None,
                    artist=rec_artist,
                    mb_release_id=rid,
                    mb_release_group_id=rg.get("id") or None,
                    release_date=date or None,
                    original_release_date=original_date or None,
                    country=release.get("country") or None,
                    primary_type=rg.get("primary-type") or rg.get("type") or None,
                    source="acoustid",
                    tracks=[
                        TrackMeta(title=rec_title, performer=rec_artist, isrc=isrc)
                    ],
                )
            )

    return results


def fingerprint_and_lookup(wav_path: Path, *, verbose: bool = False) -> list[DiscMeta]:
    """Fingerprint *wav_path* via Chromaprint, query AcoustID, then chain to MusicBrainz.

    Each AcoustID recording match is followed up with a MusicBrainz recording lookup
    to retrieve release-level metadata (album, country, label, ISRC).  Falls back to
    a basic AcoustID-only DiscMeta when the MB lookup fails for a recording.

    Returns [] on fingerprint error or when unavailable; callers need not branch.
    When *verbose* is True, fingerprint errors are printed to stdout.
    """
    if not is_available():
        return []
    api_key = os.environ.get("ACOUSTID_API_KEY", "")

    try:
        import acoustid  # type: ignore[import-untyped]

        raw_matches = list(acoustid.match(api_key, str(wav_path)))
    except Exception as exc:
        log.debug("AcoustID fingerprint failed for %s: %s", wav_path, exc)
        if verbose:
            print(f"  Fingerprint error: {exc}")
        return []

    if verbose:
        if not raw_matches:
            print("  AcoustID returned no candidates.")
        else:
            best = max(raw_matches, key=lambda m: m[0])
            print(
                f"  AcoustID: {len(raw_matches)} candidate(s); "
                f"best score {best[0]:.2f} — '{best[2]}' by {best[3]}"
            )

    seen_recs: set[str] = set()
    top = []
    for score, recording_id, title, artist in raw_matches:
        if score < _SCORE_THRESHOLD or not recording_id or recording_id in seen_recs:
            continue
        seen_recs.add(recording_id)
        top.append((score, recording_id, title, artist))
        if len(top) >= _MAX_RECORDINGS:
            break

    if not top:
        return []

    return _chain_to_mb(top, verbose=verbose)
