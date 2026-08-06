"""
metadata_menu.py — Interactive metadata confirmation and enrichment menu.

Called at create/import time after silent MB disc ID pre-population.
On non-TTY stdin, returns the disc unchanged.

Menu structure:
  Main: Accept / Fetch / Edit / Find Original Release / Reset / Clear
    Fetch: MusicBrainz text search / Discogs search / AcoustID fingerprint
    Edit:  Album / Artist / Track N (Title / Performer / ISRC)
    Original Release: browse MB release group or search, sorted by date
"""

from __future__ import annotations

import shutil
import wave
from collections.abc import Sequence
from pathlib import Path

from cdda2img.lookup_result import DiscMeta
from cdda2img.rbi_format import (
    CD_FRAMES_PER_SECOND,
    PCM_BIT_DEPTH,
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
    RBIDisc,
    RBITocEntry,
    format_disc_metadata,
)

_W = 78  # display width
_BYTES_PER_FRAME: int = (
    (PCM_SAMPLE_RATE // CD_FRAMES_PER_SECOND) * PCM_CHANNELS * (PCM_BIT_DEPTH // 8)
)


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------


def _hr(char: str = "─") -> None:
    print(char * _W)


def _header(title: str) -> None:
    print()
    _hr("═")
    print(f"  {title}")
    _hr("─")


def _trunc(text: str | None, width: int) -> str:
    if not text:
        return ""
    return text if len(text) <= width else text[: width - 1] + "…"


# Set while a `_prompt` is blocked in ``input()``. The menu's SIGWINCH handler
# (installed by ``menu_state.MenuController`` in TUI mode) reads this to decide
# whether a terminal resize should interrupt the read for an immediate repaint:
# raising only while a read is in flight keeps a resize from aborting network
# I/O that a screen's ``handle_input`` does *after* its prompt.
_AWAITING_INPUT: bool = False


def _prompt(prompt: str) -> str:
    global _AWAITING_INPUT
    _AWAITING_INPUT = True
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return "b"
    finally:
        _AWAITING_INPUT = False


def _prompt_edit(label: str, current: str) -> str:
    val = _prompt(f"  {label} [{current}]: ").strip()
    return val if val else current


def _prompt_search_fields(artist: str, title: str) -> tuple[str, str]:
    """Prompt for artist and title search fields; at least one must remain non-blank."""
    while True:
        new_artist = _prompt(f"  Artist [{artist}]: ").strip()
        new_title = _prompt(f"  Title  [{title}]: ").strip()
        result_artist = new_artist if new_artist else artist
        result_title = new_title if new_title else title
        if result_artist or result_title:
            return result_artist, result_title
        print("  At least one field (artist or title) must be non-blank.")


# ---------------------------------------------------------------------------
# Disc display
# ---------------------------------------------------------------------------


def _print_disc_summary(disc: RBIDisc, *, reserve: int = 15) -> None:
    """Print the disc-metadata header and a height-fitted track list.

    The metadata header block (Album/Artist/Label/Country/Cat. no./MCN/Original/
    Tracks) is the summary's payload and is **always printed in full** — it must
    stay pinned at the top of the alternate screen. It is rendered by the shared
    ``rbi_format.format_disc_metadata`` so the menu, ``list``, and catalogue are
    byte-identical (rbi_spec.md §6.3.2); the menu only prepends its 2-space
    chrome indent. Set / Disc / Low DR are menu-local ancillary header lines.

    The track table below is truncated to whatever vertical space remains after
    reserving ``reserve`` rows for the surrounding screen chrome (title bar +
    menu + prompt) and this summary's own header + table frame. This is what
    stops a long tracklist (e.g. a 19-track compilation on a short terminal)
    from scrolling the header off the top. A truncated list ends with a
    "… and N more" marker; because every frame is a full repaint, growing the
    terminal (the SIGWINCH repaint) simply reflows more rows into view.
    """
    header: list[str] = [
        f"  {line}"
        for line in format_disc_metadata(
            album=disc.album,
            artist=disc.artist,
            release_date=disc.release_date,
            label=disc.label,
            country=disc.country,
            catalog_number=disc.catalog_number,
            mcn=disc.catalog,
            original_release_found=disc.original_release_found,
            original_release_title=disc.original_release_title,
            original_release_year=disc.original_release_year,
            track_count=len(disc.tracks),
        )
    ]
    if disc.set_title:
        header.append(f"  {'Set:':<15}{disc.set_title}")
    if disc.disc_total > 1 or disc.disc_number != 1:
        header.append(f"  {'Disc:':<15}{disc.disc_number} of {disc.disc_total}")
    if disc.low_dynamic_range is not None:
        header.append(f"  {'Low DR:':<15}{'YES' if disc.low_dynamic_range else 'NO'}")
    for line in header:
        print(line)

    if not disc.tracks:
        return

    # Fit the track table into the rows left after the pinned header, the
    # table's own 3-line frame (blank + column header + rule), and the caller's
    # chrome reserve. Bias toward truncation: over-reserving hides a couple of
    # rows (harmless); under-reserving scrolls the header off (the bug).
    term_rows = shutil.get_terminal_size(fallback=(80, 24)).lines
    n = len(disc.tracks)
    budget = term_rows - len(header) - 3 - reserve
    if budget >= n:
        show, hidden = n, 0
    else:
        show = max(1, budget - 1)  # leave one row for the "… and N more" marker
        hidden = n - show

    print()
    print(f"  {'#':>2}  {'Title':<40}  {'ISRC'}")
    print(f"  {'─':>2}  {'─' * 40}  {'─' * 12}")
    for t in disc.tracks[:show]:
        print(f"  {t.track_number:>2}  {_trunc(t.title, 40):<40}  {t.isrc or ''}")
    if hidden:
        print(f"  … and {hidden} more")


def _print_meta_tracks(meta: DiscMeta) -> None:
    if not meta.tracks:
        return
    titled = [t for t in meta.tracks if t.title]
    if titled and len(meta.tracks) == 1:
        print(f"  Track title:   {titled[0].title}")
    else:
        print(f"  Tracks:        {len(meta.tracks)}")


def _print_meta_summary(meta: DiscMeta) -> None:
    print(f"  Album:         {meta.album or '(none)'}")
    if meta.set_title:
        print(f"  Set:           {meta.set_title}")
    if meta.disc_total is not None:
        disc_pos = f"{meta.disc_number}" if meta.disc_number is not None else "?"
        print(f"  Disc:          {disc_pos} of {meta.disc_total}")
    print(f"  Artist:        {meta.artist or '(none)'}")
    if meta.release_date:
        print(f"  Released:      {meta.release_date}")
    if meta.original_release_date and meta.original_release_date != meta.release_date:
        print(f"  Orig. release: {meta.original_release_date}")
    if meta.country:
        print(f"  Country:       {meta.country}")
    if meta.label:
        label_str = meta.label + (
            f"  [{meta.catalog_number}]" if meta.catalog_number else ""
        )
    else:
        label_str = "(none)"
    print(f"  Label:         {label_str}")
    print(f"  Barcode:       {meta.barcode or '(none)'}")
    _print_meta_tracks(meta)


# ---------------------------------------------------------------------------
# Paginated result selection
# ---------------------------------------------------------------------------

_PAGE = 10


def _render_results_page(results: list[DiscMeta], page: int, title: str) -> None:
    """Pure repaint of one page of a paginated DiscMeta result list.

    Side-effect-free except for ``print``: no prompting, no state mutation. Used
    by the native ``menu_state.ResultsScreen`` frame for its page repaint.
    """
    total = len(results)
    total_pages = max(1, (total + _PAGE - 1) // _PAGE)
    start = page * _PAGE
    page_items = results[start : start + _PAGE]

    _header(f"{title}  [{page + 1}/{total_pages}]  ({total} results)")
    print(
        f"  {'#':>3}  {'Type':<6}  {'Trk':>3}  {'Artist':<18}  {'Album':<24}"
        f"  {'Year':<4}  {'Cty':<3}  Label"
    )
    print(
        f"  {'─' * 3}  {'─' * 6}  {'─' * 3}  {'─' * 18}  {'─' * 24}"
        f"  {'─' * 4}  {'─' * 3}  {'─' * 14}"
    )
    for i, m in enumerate(page_items, start=start + 1):
        album_col = m.album or (
            m.tracks[0].title if m.tracks and m.tracks[0].title else None
        )
        # Type/Tracks surface album-vs-single so the right release is pickable
        # (CD singles are valid candidates — shown, not filtered).
        type_col = _trunc(m.primary_type, 6) or "?"
        trk_col = str(m.track_count) if m.track_count is not None else "?"
        print(
            f"  {i:>3}  {type_col:<6}  {trk_col:>3}  {_trunc(m.artist, 18):<18}"
            f"  {_trunc(album_col, 24):<24}  {(m.release_date or '')[:4]:<4}"
            f"  {(m.country or '')[:3]:<3}  {_trunc(m.label, 14)}"
        )
    print()
    nav = []
    if page > 0:
        nav.append("[p] prev")
    if page < total_pages - 1:
        nav.append("[n] next")
    nav.append("[b] back without selecting")
    print("  " + "  ".join(nav))


def _pressing_description(m: DiscMeta) -> str:
    """One-line description of what physically distinguishes this pressing.

    Prefers the annotation's first meaningful line over ``disambiguation``: the
    latter is MB's lossy summary, and on the reference disc it drops the word
    "France" — the single token that identified the user's copy. Falls back to
    disambiguation, then to nothing (a genuinely undescribed pressing, which the
    menu must still show rather than hide).
    """
    from cdda2img.mb_lookup import strip_annotation_markup

    if m.annotation:
        for line in strip_annotation_markup(m.annotation).splitlines():
            if line.strip():
                return line.strip()
    return (m.disambiguation or "").strip()


def _render_pressing_page(
    candidates: list[DiscMeta], page: int, pinned_id: str | None
) -> None:
    """Pure repaint of one page of the N5 alternatives (pressing) picker.

    These candidates all share the disc-ID fingerprint, the album, and (by the
    ``barcode_plurality`` rung) the barcode. They are the same record pressed
    differently, so the table shows the fields that CAN differ and the free text
    that describes the physical object; artist and album are constant across the
    list and would be pure noise in every column.
    """
    total = len(candidates)
    total_pages = max(1, (total + _PAGE - 1) // _PAGE)
    start = page * _PAGE
    page_items = candidates[start : start + _PAGE]

    _header(f"Choose the pressing  [{page + 1}/{total_pages}]  ({total} candidates)")
    print("  MusicBrainz matched this disc's TOC to several pressings of the same")
    print("  release. What separates them is print on the disc and inlay — read")
    print("  yours and pick the match, or decline if none of them fit.")
    print()
    print(
        f"  {'#':>3}  {'MBID':<8}  {'Cty':<3}  {'Year':<4}  {'Cat #':<14}  Description"
    )
    print(f"  {'─' * 3}  {'─' * 8}  {'─' * 3}  {'─' * 4}  {'─' * 14}  {'─' * 34}")
    for i, m in enumerate(page_items, start=start + 1):
        # The pinned row is marked, not hidden and not reordered: the user is
        # told what the ladder guessed so they can confirm or contradict it.
        mark = "*" if m.mb_release_id and m.mb_release_id == pinned_id else " "
        desc = _pressing_description(m) or "(no description)"
        print(
            f"  {i:>2}{mark}  {(m.mb_release_id or '')[:8]:<8}"
            f"  {(m.country or '')[:3]:<3}  {(m.release_date or '')[:4]:<4}"
            f"  {_trunc(m.catalog_number, 14):<14}  {_trunc(desc, 34)}"
        )
    print()
    print("  * = currently selected (picked automatically)")
    print()
    nav = []
    if page > 0:
        nav.append("[p] prev")
    if page < total_pages - 1:
        nav.append("[n] next")
    nav.append("[x] none of these match my disc")
    nav.append("[b] back")
    print("  " + "  ".join(nav))


def _render_pressing_detail(m: DiscMeta, annotation: str | None) -> None:
    """Full detail for one pressing, including the fetched annotation."""
    _header("Pressing detail")
    print(f"  Release   : {m.mb_release_id or '(none)'}")
    print(f"  Album     : {m.album or '—'}")
    print(f"  Country   : {m.country or '—'}     Date: {m.release_date or '—'}")
    print(f"  Label     : {m.label or '—'}")
    print(f"  Cat #     : {m.catalog_number or '—'}")
    print(f"  Barcode   : {m.barcode or '—'}")
    print()
    print(f"  Summary   : {m.disambiguation or '(none)'}")
    print()
    if annotation:
        import textwrap

        from cdda2img.mb_lookup import strip_annotation_markup

        print("  Annotation (matrix / SID codes / plant — check against your disc):")
        for line in strip_annotation_markup(annotation).splitlines():
            if not line:
                print()
                continue
            # Wrap to the menu width rather than letting the terminal do it: a
            # soft-wrapped matrix code splits mid-token at whatever column the
            # window happens to be, and these strings are read character by
            # character against the disc.
            for out in textwrap.wrap(line, width=_W - 4) or [""]:
                print(f"    {out}")
    else:
        print("  Annotation: (none recorded on MusicBrainz)")
    print()
    print("  [a]  This is my disc — select it")
    print("  [b]  Back to the list")


# ---------------------------------------------------------------------------
# Diff and confirm
# ---------------------------------------------------------------------------


# Typographic punctuation MB uses that differs cosmetically from the ASCII the
# TOC stores. Keyed by codepoint ordinal (avoids ambiguous-character string
# literals) and folded before diffing, so a smart-quote-only difference (ASCII
# apostrophe vs U+2019) is not reported as a field that would change.
_TYPO_FOLD: dict[int, str] = {
    0x2019: "'",  # right single quotation mark
    0x2018: "'",  # left single quotation mark
    0x201C: '"',  # left double quotation mark
    0x201D: '"',  # right double quotation mark
    0x2013: "-",  # en dash
    0x2014: "-",  # em dash
    0x2026: "...",  # horizontal ellipsis
}


def _typo_fold(s: str | None) -> str:
    return (s or "").translate(_TYPO_FOLD)


def _show_diff(meta: DiscMeta, disc: RBIDisc) -> None:
    """Print which fields would change when applying *meta* to *disc*."""
    _unknown = "Unknown Artist"
    changes: list[str] = []

    def _cmp(label: str, old: str | None, new: str | None) -> None:
        if new and (not old or old == _unknown) and new != old:
            changes.append(f"  + {label:<28}  (none)  →  {new}")
        elif old and new and _typo_fold(old) != _typo_fold(new):
            changes.append(f"  ~ {label:<28}  {_trunc(old, 20)}  →  {_trunc(new, 20)}")

    _cmp("Album", disc.album, meta.album)
    _cmp("Artist", disc.artist, meta.artist)
    _cmp("Catalog/barcode", disc.catalog, meta.barcode)

    meta_by_num = {t.number: t for t in meta.tracks if t.number is not None}
    for entry in disc.tracks:
        mt = meta_by_num.get(entry.track_number)
        if not mt:
            continue
        _cmp(f"Track {entry.track_number:>2} title", entry.title, mt.title)
        if not entry.isrc and mt.isrc:
            changes.append(
                f"  + Track {entry.track_number:>2} ISRC{' ':<21}  (none)  →  {mt.isrc}"
            )

    if not changes:
        print("  (no fields would change)")
    else:
        for line in changes:
            print(line)


def _confirm_apply(meta: DiscMeta, disc: RBIDisc) -> str | None:
    """Show diff and ask how to apply *meta* to *disc*.

    Returns 'update' (fill blanks only), 'overwrite' (replace all), or None (cancel).
    """
    _header("Preview changes")
    _print_meta_summary(meta)
    print()
    print("  Missing fields that would change:")
    _show_diff(meta, disc)
    print()
    print("  [u]  Update missing fields only")
    print("  [o]  Overwrite all fields (replace existing metadata)")
    print("  [b]  Cancel")
    while True:
        choice = _prompt("  > ").strip().lower()
        if choice == "u":
            return "update"
        if choice == "o":
            return "overwrite"
        if choice == "b":
            return None
        print("  Unknown command.")


# ---------------------------------------------------------------------------
# MusicBrainz search — native screen-stack port (cp3a) lives in menu_state.py
# (MBSearchScreen + ResultsScreen). The legacy _mb_search_menu /
# _mb_select_and_apply blocking loops were removed in cp3a; the apply tail (fetch
# full release before preview, confirm, merge/overwrite, thread mb_rg_id) now
# lives in ResultsScreen._apply_selected(source="mb").
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Discogs search — native screen-stack port (cp3b) lives in menu_state.py
# (DiscogsSearchScreen + ResultsScreen source="discogs"). The legacy
# _discogs_menu / _discogs_execute_search blocking loops were removed in cp3b.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AcoustID sub-menu
# ---------------------------------------------------------------------------


def _pcm_extract_track_wav(
    disc: RBIDisc, pcm_path: Path, track_num: int, out_path: Path
) -> Path | None:
    """Slice track *track_num* from raw s16le *pcm_path* into a WAV at *out_path*.

    Returns *out_path* on success, None if the track is not found or on I/O error.
    """
    track = next((t for t in disc.tracks if t.track_number == track_num), None)
    if track is None:
        return None
    # Read the full track so the WAV header reports the correct duration.
    # AcoustID uses duration (from the WAV header) as a scoring signal;
    # a truncated file causes duration mismatch that suppresses all candidates.
    # fpcalc still caps its own analysis at 120 seconds internally.
    audio_start = (track.start_frame + track.pregap_frames) * _BYTES_PER_FRAME
    try:
        with open(pcm_path, "rb") as f:
            f.seek(audio_start)
            pcm_data = f.read(track.duration_frames * _BYTES_PER_FRAME)
        with wave.open(str(out_path), "wb") as w:
            w.setnchannels(PCM_CHANNELS)
            w.setsampwidth(PCM_BIT_DEPTH // 8)
            w.setframerate(PCM_SAMPLE_RATE)
            w.writeframes(pcm_data)
    except OSError:
        return None
    read_sec = len(pcm_data) / _BYTES_PER_FRAME / CD_FRAMES_PER_SECOND
    print(f"  Track {track_num}: {read_sec:.1f}s extracted @ offset {audio_start:,}")
    return out_path


def _acoustid_fingerprint(
    wav_path: Path, *, track_number: int | None = None
) -> list[DiscMeta]:
    """Fingerprint *wav_path* via AcoustID; return matches (possibly empty).

    Pure of menu navigation: prints progress, runs the lookup, and tags
    single-track results carrying ``number=None`` with *track_number* so the
    title/ISRC merge into the correct disc track. The selection / confirm / apply
    tail lives in ``menu_state.ResultsScreen`` (source="acoustid"). Shared by
    ``AcoustidScreen`` (track-picker) and ``AcoustidFileScreen`` (file path).
    """
    from cdda2img import acoustid_lookup

    print(f"  Fingerprinting {wav_path.name}... (may take a few seconds)")
    results = acoustid_lookup.fingerprint_and_lookup(wav_path, verbose=True)
    if not results:
        return []
    if track_number is not None:
        for result in results:
            if len(result.tracks) == 1 and result.tracks[0].number is None:
                result.tracks[0].number = track_number
    return results


def _render_acoustid_tracklist(disc: RBIDisc) -> None:
    """Pure repaint of the AcoustID track-picker list (header + per-track rows).

    Shared by ``menu_state.AcoustidScreen``; side-effect-free except ``print``.
    """
    _header("AcoustID Fingerprint")
    print(f"  {'#':>3}  {'Duration':>8}  Title")
    print(f"  {'─' * 3}  {'─' * 8}  {'─' * 32}")
    for t in sorted(disc.tracks, key=lambda x: x.track_number):
        mm = int(t.duration_frames / CD_FRAMES_PER_SECOND / 60)
        ss = int(t.duration_frames / CD_FRAMES_PER_SECOND) % 60
        print(
            f"  {t.track_number:>3}  {mm}:{ss:02d}       "
            f"{_trunc(t.title or '(untitled)', 32)}"
        )


# ---------------------------------------------------------------------------
# Fetch sub-menu — fully native (cp3a/cp3b/cp3c). The blocking _fetch_menu loop
# and the per-service _mb_*/_discogs_*/_acoustid_* loops were replaced by
# menu_state screens (FetchScreen → MBSearchScreen / DiscogsSearchScreen /
# AcoustidScreen + the shared ResultsScreen). The pure helpers above
# (_render_results_page, _acoustid_fingerprint, _render_acoustid_tracklist,
# _pcm_extract_track_wav) back those screens.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Find Original Release sub-menu
# ---------------------------------------------------------------------------


def _fetch_releases_for_group(
    disc: RBIDisc, mb_rg_id: str | None
) -> tuple[list[DiscMeta], str | None]:
    """Return (releases, rg_id): fetch by group ID or fall back to text search."""
    from cdda2img.mb_lookup import (
        build_mb_search_query,
        lookup_release_group,
        search_releases,
    )

    if mb_rg_id:
        print(f"  Fetching MusicBrainz release group {mb_rg_id} ...")
        releases = lookup_release_group(mb_rg_id)
        if releases:
            return releases, mb_rg_id
        print("  No releases found in group; falling back to text search.")
    default_query = build_mb_search_query(disc.artist, disc.album)
    query = _prompt_edit("Search query", default_query)
    print(f"\n  Searching MusicBrainz for {query!r} ...")
    return search_releases(query, limit=50), mb_rg_id


def _set_original_manually(disc: RBIDisc) -> RBIDisc:
    """Prompt the user to enter the original release title and year by hand."""
    _header("Set Original Release Manually")
    title = _prompt("  Original album title (blank = none) > ").strip()
    if not title:
        disc.original_release_found = False
        disc.original_release_title = None
        disc.original_release_year = None
        print("  Cleared.")
        return disc
    year: int | None = None
    while True:
        raw = _prompt("  Year of original release (4 digits) > ").strip()
        if raw in ("b", "q", ""):
            return disc
        if len(raw) == 4 and raw.isdigit():
            year = int(raw)
            break
        print("  Enter a 4-digit year.")
    disc.original_release_found = True
    disc.original_release_title = title
    disc.original_release_year = year
    print(f"  Set: {title} ({year})")
    return disc


def _apply_selected_release(disc: RBIDisc, selected: DiscMeta) -> str | None:
    """Apply *selected* as the disc's original release; return its mb_rg_id or None."""
    raw_date = selected.release_date or ""
    year_str = raw_date[:4] if len(raw_date) >= 4 else ""
    year = int(year_str) if year_str.isdigit() else None
    disc.original_release_found = True
    disc.original_release_title = selected.album or disc.album
    disc.original_release_year = year
    if year_str.isdigit():
        disc.original_release_date = year_str
    year_disp = f" ({year})" if year else ""
    print(f"  Applied: {disc.original_release_title}{year_disp}")
    return selected.mb_release_group_id


def _confirm_original(selected: DiscMeta) -> bool:
    """Blocking modal: show *selected* and ask whether to apply it as the original.

    The original-release confirm is the simpler [a]/[b] choice (apply vs. back),
    not the update/overwrite ``_confirm_apply`` used by the whole-disc merges.
    Called from ``menu_state.ResultsScreen._apply_original``.
    """
    _header("Selected Release")
    _print_meta_summary(selected)
    print()
    print("  [a]  Apply as original release")
    print("  [b]  Back to list")
    return _prompt("  > ").strip().lower() == "a"


# ---------------------------------------------------------------------------
# Disc snapshot helpers
# ---------------------------------------------------------------------------


def _clear_disc(disc: RBIDisc) -> RBIDisc:
    """Return a new RBIDisc with all metadata cleared; timing and structure preserved."""
    cleared_tracks = [
        RBITocEntry(
            track_number=t.track_number,
            title="",
            performer="",
            start_frame=t.start_frame,
            duration_frames=t.duration_frames,
            pregap_frames=t.pregap_frames,
            isrc=None,
        )
        for t in disc.tracks
    ]
    return RBIDisc(
        album="",
        artist="",
        disc_number=disc.disc_number,
        disc_total=disc.disc_total,
        set_title=disc.set_title,
        tracks=cleared_tracks,
        # Clearing *metadata* must not reset a *physical* disc property: pre_emphasis
        # is read from the subchannel (R14 year-cap signal), not guessed.
        pre_emphasis=disc.pre_emphasis,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_metadata_menu(
    disc: RBIDisc,
    source_pcm: Path | None = None,
    source_wavs: list[Path] | None = None,
    ar_summary: str | None = None,
    tui: bool = True,
    auto_apply: bool = False,
    pressing_candidates: Sequence[DiscMeta] = (),
    provenance: dict[str, str] | None = None,
) -> RBIDisc:
    """Display current metadata and run the interactive enrichment/confirmation menu.

    *source_pcm* — raw s16le PCM file (import pipeline): enables per-track WAV
    extraction for AcoustID fingerprinting.
    *source_wavs* — per-track WAV list (create pipeline): used directly for
    AcoustID fingerprinting without extraction.
    *ar_summary* — pre-rendered AccurateRip report (rip pipeline). When
    provided, it is stored on the controller (for potential future use)
    but no longer causes a separate pause screen before the main menu.
    *pressing_candidates* — N5: the pressings the §10.3 ladder left tied after
    its last evidence rung. More than one enables the "choose the pressing"
    screen. *provenance* — when given, the pressing outcome
    (``unique``/``auto_tiebreak``/``manual``/``rejected``) and the chosen
    candidate's MB free text are written into it after the menu closes.

    Returns the (possibly updated) RBIDisc. Returns *disc* unchanged when stdin
    is not a TTY (batch/scripted mode).

    Backed by ``menu_state.MenuController`` — a stack of ``Screen`` objects
    pushed/popped by each screen's ``Nav`` intent. Each screen's renderer
    clears the screen and draws from origin (fixed-position / redraw semantics).
    """
    from cdda2img.menu_state import MenuController

    ctl = MenuController(
        disc,
        source_pcm=source_pcm,
        source_wavs=source_wavs,
        ar_summary=ar_summary,
        tui=tui,
        auto_apply=auto_apply,
        pressing_candidates=pressing_candidates,
        pressing_outcome=(provenance or {}).get("release_selection", "unique"),
    )
    disc = ctl.run()
    if provenance is not None:
        _record_pressing_outcome(provenance, ctl)
    return disc


def _record_pressing_outcome(provenance: dict[str, str], ctl) -> None:
    """Write the N5 pressing-provenance keys after the menu closes.

    ``release_selection`` is the claim a future archivist reads to judge how much
    the pinned pressing is worth. It takes four values because two cannot carry
    the distinction: ``unique`` (one candidate — nothing was chosen, so nothing
    can be wrong), ``auto_tiebreak`` (several — the alphabetical MBID sort
    picked), ``manual`` (several — the user picked, holding the disc) and
    ``rejected`` (several were shown and the user said none of them match).
    Only ``auto_tiebreak`` is a guess, and it is precisely the one that must be
    findable.

    Written here, at the point of selection, rather than inferred from the
    ``--auto`` flag: ``_gate_adjusted_auto`` can already demote a run from auto
    to manual mid-disc, so the flag is not a reliable proxy for what happened.
    """
    if not ctl.disc.mb_release_id:
        # No release pinned at all (MB did not know the disc, or every candidate
        # was rejected upstream). There was no pressing selection to describe,
        # and writing `unique` would claim one happened and was unambiguous.
        return
    provenance["release_selection"] = ctl.pressing_outcome
    if ctl.pressing_selected is not None and ctl.pressing_selected.disambiguation:
        # A manual pick replaces the pinned release, so it must replace the
        # description too. The un-picked case is already covered upstream by
        # `_emit_mb_provenance`, which writes it for every path that pins a
        # release — including the single-match path, where this controller has
        # no candidate list to read it from.
        provenance["release_disambiguation"] = ctl.pressing_selected.disambiguation
