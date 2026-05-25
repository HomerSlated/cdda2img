"""
original_release.py — best-effort lookup of an album's earliest release.

Populates ``disc.original_release_*`` fields on an :class:`RBIDisc` by querying
MusicBrainz's release-group endpoint.  When the disc's MB release-group is an
``Album`` (without ``Compilation`` / ``Live`` / ``Remix`` / etc. secondary
types) and ``first-release-date`` resolves, this tells us when the same logical
album first appeared and what its canonical title was.

A title-fuzz fallback (against the artist's catalogue) is planned but not yet
wired up; see ``private/research/incoming/original-release-detection.md`` for
the deny-list/allow-list research that fallback will depend on.
"""

from __future__ import annotations

import logging

from cdda2img.rbi_format import RBIDisc

log = logging.getLogger(__name__)

# Release-group secondary types that disqualify a release group as the
# "original release" of a studio album.  A disc whose release group is
# tagged Compilation/Live/Remix/etc. is itself a derivative work — the
# "earliest release date" of that release group is the earliest date of the
# derivative, not of the underlying songs.
_DERIVATIVE_SECONDARY_TYPES: frozenset[str] = frozenset({
    "Compilation",
    "Live",
    "Remix",
    "DJ-mix",
    "Mixtape/Street",
    "Demo",
    "Interview",
    "Audiobook",
    "Audio drama",
    "Spokenword",
})


def _parse_year(date_str: str | None) -> int | None:
    """Return the 4-digit year from an MB-style date, or None.

    Accepts ``YYYY``, ``YYYY-MM``, ``YYYY-MM-DD``, empty string, or None.
    """
    if not date_str:
        return None
    head = date_str[:4]
    if head.isdigit():
        return int(head)
    return None


def _fetch_release_group(rg_id: str):  # type: ignore[no-untyped-def]
    """Return the raw MB release-group dict for *rg_id*, or None on error.

    Centralised so the network/error handling is isolated from the logic.
    """
    import musicbrainzngs  # type: ignore[import-untyped]

    from cdda2img.mb_lookup import _setup_useragent

    _setup_useragent()
    try:
        result = musicbrainzngs.get_release_group_by_id(rg_id, includes=["releases"])
    except (musicbrainzngs.ResponseError, musicbrainzngs.NetworkError) as exc:
        log.debug("MB release-group lookup %s failed: %s", rg_id, exc)
        return None
    return result.get("release-group") or None


def find_original_release(disc: RBIDisc) -> tuple[bool, str | None, int | None]:
    """Look up the earliest release of *disc*'s release group.

    Returns ``(found, title, year)``.  ``found=False`` means the lookup did
    not produce a usable answer — *not* a guarantee that no earlier release
    exists.

    Side effects: none.  Caller is responsible for assigning the result onto
    the RBIDisc.  This makes the function easy to test and idempotent.
    """
    rg_id = disc.mb_release_group_id
    if not rg_id:
        return (False, None, None)

    rg = _fetch_release_group(rg_id)
    if rg is None:
        return (False, None, None)

    # Reject derivative release groups.  We still want to record first-release-date
    # on these for context elsewhere, but they are not "original releases" in the
    # sense this field is meant to capture.
    secondary = set(rg.get("secondary-type-list") or [])
    if secondary & _DERIVATIVE_SECONDARY_TYPES:
        log.debug("RG %s rejected (secondary types: %s)", rg_id, sorted(secondary))
        return (False, None, None)

    first_date = rg.get("first-release-date") or ""
    year = _parse_year(first_date)
    if year is None:
        return (False, None, None)

    title = rg.get("title") or disc.album

    # Don't claim a separate "original" when the disc itself appears to be it:
    # if disc.release_date's year matches first-release-date's year AND title
    # matches the release-group title, the disc IS the original.  Setting
    # found=True with the same title/year would be technically correct but
    # cosmetically useless — the value of this field is to point at *another*
    # release.
    disc_year = _parse_year(disc.release_date) or _parse_year(
        disc.original_release_date
    )
    if (
        disc_year == year
        and (disc.album or "").strip().lower() == title.strip().lower()
    ):
        return (False, None, None)

    return (True, title, year)


def populate_original_release(disc: RBIDisc) -> None:
    """Convenience wrapper: call :func:`find_original_release` and assign to disc.

    Skips the lookup when the user has already set ``original_release_found``
    (e.g. via the metadata menu) — manual overrides win.
    """
    if disc.original_release_found:
        return
    found, title, year = find_original_release(disc)
    if found:
        disc.original_release_found = True
        disc.original_release_title = title
        disc.original_release_year = year
