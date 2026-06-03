#!/usr/bin/env python3
"""
demo_title_fuzz.py — step-by-step trace of the title-fuzz fallback path in
`cdda2img.original_release.find_original_release_fuzzy`.

The fuzzy fallback is the second tier of `find_original_release()`. It fires
when the primary release-group lookup yields nothing (e.g. the disc has no
MB release-group ID, or the group was rejected as derivative). The fallback
searches MB by artist+title, normalises every candidate against an allow-list
of reissue tokens, rejects sibling-album false positives via a deny-list,
scores survivors with `rapidfuzz.fuzz.token_set_ratio`, and picks the earliest
candidate scoring at or above the cutoff (88 by default).

This tool walks each stage of that pipeline and prints:

  1. Input disc (album, artist, mode)
  2. The MusicBrainz search query string built by `build_mb_search_query`
  3. Candidate releases fetched (mock by default; --live hits real MB)
  4. Per-candidate normalisation: original → normalised stem
  5. Per-candidate deny-list result (None = ok, else reason)
  6. Per-candidate fuzz score against the disc title's normalised stem
  7. The final pick (earliest-year wins; tie-break on highest score)
  8. What `populate_original_release()` would assign to the RBIDisc

Usage:
  uv run python tools/demo_title_fuzz.py
    # Default: ZZ Top "Eliminator (2008 Remaster)" with canned MB candidates

  uv run python tools/demo_title_fuzz.py --album "Album" --artist "Artist"
    # Synthetic mock candidates derived from the album name

  uv run python tools/demo_title_fuzz.py --live --album "..." --artist "..."
    # Hit the real MusicBrainz API (subject to 1 req/s rate limit).

Notes:
  - Imports underscored internals from `cdda2img.original_release` on purpose;
    a tracer's job is to inspect implementation, not respect privacy.
  - The script does not call `find_original_release_fuzzy()` directly — that
    would only print the winner. It replicates the scoring loop in user-space
    so every candidate's verdict is visible. The helpers it calls
    (`_normalise_title`, `_deny_match`) are the real implementation.
"""

from __future__ import annotations

import argparse

from cdda2img.original_release import (
    _FUZZ_SCORE_CUTOFF,
    _deny_match,
    _normalise_title,
)
from cdda2img.rbi_format import RBIDisc

_MOCK_CANDIDATES: dict[tuple[str, str], list[tuple[str, int]]] = {
    ("zz top", "eliminator"): [
        ("Eliminator", 1983),
        ("Eliminator (2008 Remaster)", 2008),
        ("Eliminator (Deluxe Edition)", 2008),
        ("Live at Rockpalast 1980", 2014),
        ("Greatest Hits", 1992),
        ("The Best of ZZ Top", 1977),
    ],
    ("led zeppelin", "led zeppelin"): [
        ("Led Zeppelin", 1969),
        ("Led Zeppelin II", 1969),
        ("Led Zeppelin (Remastered)", 1994),
        ("Led Zeppelin III", 1970),
    ],
    ("radiohead", "ok computer"): [
        ("OK Computer", 1997),
        ("OK Computer (1997 Remaster)", 2009),
        ("OK Computer OKNOTOK 1997 2017", 2017),
        ("Pablo Honey", 1993),
    ],
}


def _hr(title: str) -> None:
    print()
    print("─" * 78)
    print(f" {title}")
    print("─" * 78)


def _build_query(album: str, artist: str) -> str:
    from cdda2img.mb_lookup import build_mb_search_query

    return build_mb_search_query(artist, album)


def _mock_candidates(album: str, artist: str) -> list[tuple[str, int]]:
    key = (artist.lower().strip(), album.lower().strip().split("(")[0].strip())
    if key in _MOCK_CANDIDATES:
        return _MOCK_CANDIDATES[key]
    stem = album.split("(")[0].strip() or album
    return [
        (stem, 1985),
        (f"{stem} (Remastered)", 2010),
        (f"{stem} (Deluxe Edition)", 2015),
        (f"{stem} Vol. 2", 1987),
        ("Some Unrelated Album", 1980),
    ]


def _live_candidates(album: str, artist: str) -> list[tuple[str, int]]:
    from cdda2img.original_release import (
        _gather_artist_catalogue_metas_via_mb,
        _parse_year,
    )

    metas = _gather_artist_catalogue_metas_via_mb(artist, album)
    return [
        (m.album, _parse_year(m.original_release_date) or 0) for m in metas if m.album
    ]


def trace(album: str, artist: str, live: bool) -> None:  # noqa: C901
    _hr("1. Input")
    print(f"  Album:   {album!r}")
    print(f"  Artist:  {artist!r}")
    print(f"  Mode:    {'LIVE MusicBrainz' if live else 'MOCK candidates'}")

    _hr("2. MusicBrainz search query (built by build_mb_search_query)")
    query = _build_query(album, artist)
    print(f"  {query}")

    _hr("3. Candidate releases fetched")
    candidates = (
        _live_candidates(album, artist) if live else _mock_candidates(album, artist)
    )
    if not candidates:
        print("  No candidates returned.")
        print("  → find_original_release_fuzzy returns (False, None, None).")
        return
    print(f"  {len(candidates)} candidate(s):")
    for i, (t, y) in enumerate(candidates, 1):
        print(f"    [{i:>2}] ({y})  {t}")

    _hr("4. Per-candidate normalisation (_normalise_title)")
    disc_norm = _normalise_title(album)
    print(f"  Disc title:  {album!r}")
    print(f"  → normalised → {disc_norm!r}")
    print()
    print(f"  {'#':>3}  {'year':<5}  {'original title':<44}  normalised")
    print(f"  {'─' * 3}  {'─' * 5}  {'─' * 44}  {'─' * 22}")
    for i, (t, y) in enumerate(candidates, 1):
        print(f"  {i:>3}  {y:<5}  {t[:42]:<44}  {_normalise_title(t)!r}")

    _hr("5. Per-candidate deny-list filter (_deny_match)")
    print(f"  {'#':>3}  {'candidate':<44}  verdict")
    print(f"  {'─' * 3}  {'─' * 44}  {'─' * 26}")
    accepted: list[tuple[str, int]] = []
    for i, (t, y) in enumerate(candidates, 1):
        reason = _deny_match(album, t)
        if reason:
            print(f"  {i:>3}  {t[:42]:<44}  BLOCKED — {reason}")
        else:
            print(f"  {i:>3}  {t[:42]:<44}  ok")
            accepted.append((t, y))

    if not accepted:
        print()
        print("  All candidates blocked by deny-list.")
        print("  → find_original_release_fuzzy returns (False, None, None).")
        return

    _hr(
        f"6. Fuzz scoring (rapidfuzz.fuzz.token_set_ratio, cutoff = {_FUZZ_SCORE_CUTOFF})"
    )
    from rapidfuzz import fuzz

    print(f"  {'#':>3}  {'year':<5}  score   {'≥ cutoff?':<11}  candidate")
    print(f"  {'─' * 3}  {'─' * 5}  {'─' * 5}   {'─' * 11}  {'─' * 40}")
    qualified: list[tuple[str, int, float]] = []
    for i, (t, y) in enumerate(accepted, 1):
        score = fuzz.token_set_ratio(disc_norm, _normalise_title(t))
        verdict = "yes" if score >= _FUZZ_SCORE_CUTOFF else "no"
        print(f"  {i:>3}  {y:<5}  {score:>5.0f}   {verdict:<11}  {t}")
        if score >= _FUZZ_SCORE_CUTOFF:
            qualified.append((t, y, score))

    _hr("7. Final pick (earliest-year wins; tie-break on highest score)")
    if not qualified:
        print("  No candidate met the cutoff.")
        print("  → find_original_release_fuzzy returns (False, None, None).")
        winner: tuple[str, int, float] | None = None
    else:
        qualified.sort(key=lambda t: (t[1], -t[2]))
        winner = qualified[0]
        print("  Qualified pool (after sort: year asc, score desc):")
        for t, y, s in qualified:
            marker = "  ←" if (t, y, s) == winner else ""
            print(f"    ({y})  score {s:>5.0f}   {t}{marker}")
        print()
        print(f"  Pick: title={winner[0]!r}, year={winner[1]}, score={winner[2]:.0f}")

    _hr("8. What populate_original_release() would assign to the RBIDisc")
    disc = RBIDisc(album=album, artist=artist)
    print("  BEFORE:")
    print(f"    disc.original_release_found = {disc.original_release_found}")
    print(f"    disc.original_release_title = {disc.original_release_title!r}")
    print(f"    disc.original_release_year  = {disc.original_release_year}")
    if winner is None:
        print()
        print("  populate_original_release() leaves the disc unchanged.")
        return
    disc.original_release_found = True
    disc.original_release_title = winner[0]
    disc.original_release_year = winner[1]
    print()
    print("  AFTER:")
    print(f"    disc.original_release_found = {disc.original_release_found}")
    print(f"    disc.original_release_title = {disc.original_release_title!r}")
    print(f"    disc.original_release_year  = {disc.original_release_year}")
    print()
    print("  In a real rip, this drives the 'Original:' / 'This release' line")
    print("  rendered by cdda2img list and the catalogue browser.")


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--album",
        default="Eliminator (2008 Remaster)",
        help="Disc album title (default: canonical ZZ Top example)",
    )
    p.add_argument(
        "--artist",
        default="ZZ Top",
        help="Disc artist (default: ZZ Top)",
    )
    p.add_argument(
        "--live",
        action="store_true",
        help="Use the real MusicBrainz API (rate limited to 1 req/s)",
    )
    args = p.parse_args()
    trace(args.album, args.artist, args.live)


if __name__ == "__main__":
    main()
