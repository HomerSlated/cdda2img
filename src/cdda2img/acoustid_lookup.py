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
from cdda2img.validators import validate_isrc

log = logging.getLogger(__name__)

_MAX_RECORDINGS = 5  # cap on recording matches to avoid excessive MB queries
_SCORE_THRESHOLD = 0.5

# Release paging for the recording->releases browse (see _browse_releases_for_recording).
# 100 is MusicBrainz's own per-request maximum. The page cap is a runaway guard for a
# recording carried by hundreds of releases, NOT a result cap: at 1 req/s (R15) an
# uncapped walk could add a minute to a rip. It binds far above the normal case (the
# reference disc's busiest recording has 43), and when it does bind the shortfall is
# logged at WARNING — because a truncated set reads exactly like a genuine miss, which
# is the defect this whole function exists to fix.
_BROWSE_PAGE_SIZE = 100
_MAX_RELEASE_PAGES = 5

_COUNTRY_PREF: dict[str, int] = {"GB": 0, "US": 1, "XW": 2}


def _release_sort_key(r: dict) -> tuple[str, int]:
    return (r.get("date") or "9999", _COUNTRY_PREF.get(r.get("country") or "", 3))


def is_available() -> bool:
    """Return True when pyacoustid, libchromaprint, and ACOUSTID_API_KEY are all present."""
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


def _release_track_count(release: dict) -> int | None:
    """Total track count for an MB release dict fetched with inc=media.

    Sums the per-medium ``track-count`` from the ``medium-list`` (the field the
    ``media`` include populates — there is no release-level
    ``medium-track-count`` on this endpoint). Returns None when no medium
    carries a usable count, so the menu shows "?" rather than a wrong number.
    This is the album-vs-single cue for AcoustID rows (Type is unavailable —
    see the release loop): a 2-track single reads "2", an album reads "12".
    """
    total = 0
    seen = False
    for md in release.get("medium-list") or []:
        tc = md.get("track-count")
        if tc:
            try:
                total += int(tc)
            except (ValueError, TypeError):
                continue
            else:
                seen = True
    return total if seen else None


def _browse_releases_for_recording(
    recording_id: str, *, verbose: bool = False
) -> list[dict]:
    """Every release carrying *recording_id*, with release-group and media embedded.

    **Why this is not ``get_recording_by_id(..., includes=["releases"])``.** That
    endpoint silently truncates its embedded release list to **25** while reporting
    the true total in ``release-count`` — measured 25 of 43 on the reference disc,
    with the disc's own release among the 18 that were cut. The truncation made
    ``acoustid_corroborates`` a false ``NO`` on every container ripped before
    2026-08-06 (TODO N3). It also embeds an **empty** release-group stub, so
    ``_acoustid_gate`` (§10.4) could never fire: it builds its comparison set from
    ``release-group.id``, which was absent on 0/43 rows.

    The browse endpoint fixes both — it pages to the full count, and
    ``release-groups`` is a valid include here, so the gate has evidence on both
    sides for the first time.

    Costs one request per page *in addition to* the recording lookup, which is still
    needed for the recording-level fields (title, artist credit, ISRCs) that a
    release browse does not return. Returns ``[]`` on any MB failure: the caller
    then degrades to a recording-level result rather than losing the match entirely.
    """
    import musicbrainzngs  # type: ignore[import-untyped]

    releases: list[dict] = []
    total = 0
    for _page in range(_MAX_RELEASE_PAGES):
        try:
            result = musicbrainzngs.browse_releases(
                recording=recording_id,
                includes=["release-groups", "media"],
                limit=_BROWSE_PAGE_SIZE,
                offset=len(releases),
            )
        except Exception as exc:
            log.debug("MB release browse for %s failed: %s", recording_id, exc)
            if verbose:
                print(f"    MB browse {recording_id[:8]}…: FAILED ({exc})")
            return releases
        batch = result.get("release-list") or []
        releases.extend(batch)
        total = result.get("release-count") or len(releases)
        # An empty page terminates too: without it a server that reports a count
        # larger than it will serve would spin to the page cap on every recording.
        if not batch or len(releases) >= total:
            return releases

    log.warning(
        "MB release browse for %s hit the %d-page cap: %d of %d releases. "
        "AcoustID corroboration for this recording is testing a truncated set.",
        recording_id,
        _MAX_RELEASE_PAGES,
        len(releases),
        total,
    )
    return releases


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
                # Recording-level fields only. "releases"/"media" used to ride
                # along here; they were moved to _browse_releases_for_recording
                # because this endpoint truncates the embedded release list to 25
                # and embeds an empty release-group stub (TODO N3). This call is
                # still required — the browse endpoint returns releases, not the
                # recording's title, artist credit or ISRCs.
                includes=["artists", "isrcs"],
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
        # Validate at the AcoustID ingress: this DiscMeta is applied directly on
        # single-track discs (no re-parse through the validated MB path), so a
        # malformed value would otherwise reach the TOC ISRC line.
        isrc = validate_isrc(isrc_list[0]) if isrc_list else None
        releases = _browse_releases_for_recording(recording_id, verbose=verbose)

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

        releases = sorted(releases, key=_release_sort_key)
        for release in releases:
            rid = release.get("id")
            if not rid or rid in seen_releases:
                continue
            seen_releases.add(rid)
            # The release-group is now genuinely populated (43/43 measured), where
            # under the old `inc=releases` stub it was empty on every row. That is
            # the fix that lets `_acoustid_gate` (§10.4) fire at all — it compares
            # release-GROUP ids because AcoustID is edition-blind, and it had never
            # had a non-empty set to compare against.
            #
            # Consequence beyond the gate: `mb_release_group_id` below is no longer
            # always None on AcoustID menu rows. Deliberate — it is the release's
            # own correct rg id. In practice `menu_state._apply_acoustid` refetches
            # the full release (an AcoustID stub carries one track, so the
            # partial-stub branch fires on every multi-track disc) and the stub's
            # value is discarded; it reaches the disc only on a single-track disc,
            # where a correct rg id beats the None it used to write.
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
                    # primary_type is intentionally left unset even though the
                    # browse endpoint now embeds a real release-group that carries
                    # it. Populating it would make AcoustID propose a field, and
                    # `resolver_adapter` requires (and asserts by test) that these
                    # rows propose nothing — the corroborate path merges no
                    # metadata by design. So the Type column stays "?" for AcoustID
                    # rows; Trk (below) remains the album-vs-single cue.
                    track_count=_release_track_count(release),
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
