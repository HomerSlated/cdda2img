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

import tempfile
import wave
from pathlib import Path

from cdda2img.lookup_result import DiscMeta
from cdda2img.rbi_format import (
    CD_FRAMES_PER_SECOND,
    PCM_BIT_DEPTH,
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
    RBIDisc,
    RBITocEntry,
    format_original,
    year_of,
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


def _prompt(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return "b"


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


def _print_disc_summary(disc: RBIDisc) -> None:
    # "Album:" carries THIS release's year; "Original:" (immediately below)
    # answers whether this disc is the original. Labels are padded to the
    # width of "Original:" so the values align.
    y = year_of(disc.release_date)
    album_year = y if y is not None else "unknown"
    print(f"  {'Album:':<9} {disc.album or '(none)'} ({album_year})")
    if disc.set_title:
        print(f"  {'Set:':<9} {disc.set_title}")
    print(f"  {format_original(disc)}")
    print(f"  {'Artist:':<9} {disc.artist or '(none)'}")
    print(f"  {'MCN:':<9} {disc.catalog or '(none)'}")
    if disc.disc_total > 1 or disc.disc_number != 1:
        print(f"  {'Disc:':<9} {disc.disc_number} of {disc.disc_total}")
    print(f"  {'Tracks:':<9} {len(disc.tracks)}")
    if disc.low_dynamic_range is not None:
        print(f"  {'Low DR:':<9} {'YES' if disc.low_dynamic_range else 'NO'}")
    if disc.tracks:
        print()
        print(f"  {'#':>2}  {'Title':<40}  {'ISRC'}")
        print(f"  {'─':>2}  {'─' * 40}  {'─' * 12}")
        for t in disc.tracks[:20]:
            print(f"  {t.track_number:>2}  {_trunc(t.title, 40):<40}  {t.isrc or ''}")
        if len(disc.tracks) > 20:
            print(f"  … and {len(disc.tracks) - 20} more")


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
    print(f"  MCN:           {meta.catalog or '(none)'}")
    _print_meta_tracks(meta)


# ---------------------------------------------------------------------------
# Paginated result selection
# ---------------------------------------------------------------------------

_PAGE = 10


def _render_results_page(results: list[DiscMeta], page: int, title: str) -> None:
    """Pure repaint of one page of a paginated DiscMeta result list.

    Side-effect-free except for ``print``: no prompting, no state mutation. Shared
    by the legacy blocking ``_select_from_results`` loop and the native
    ``menu_state.ResultsScreen`` frame, so both render byte-identical pages.
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


def _select_from_results(
    results: list[DiscMeta], title: str = "Results"
) -> DiscMeta | None:
    """Display a paginated list of DiscMeta; return user selection or None (back).

    Legacy blocking loop, still used by the Discogs / AcoustID / original-release
    flows pending their own screen-stack ports (cp3b / cp3c / cp4). The MB path
    now uses the native ``ResultsScreen``; both share ``_render_results_page``.
    """
    total = len(results)
    total_pages = max(1, (total + _PAGE - 1) // _PAGE)
    page = 0

    while True:
        _render_results_page(results, page, title)

        choice = _prompt(f"  Select 1-{total}: ").strip().lower()
        if choice == "n" and page < total_pages - 1:
            page += 1
        elif choice == "p" and page > 0:
            page -= 1
        elif choice == "b":
            return None
        else:
            try:
                idx = int(choice) - 1
                if 0 <= idx < total:
                    return results[idx]
                print("  Invalid selection.")
            except ValueError:
                pass


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
    _cmp("Catalog/barcode", disc.catalog, meta.catalog)

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
# Discogs search sub-menu
# ---------------------------------------------------------------------------


def _discogs_execute_search(
    disc: RBIDisc,
    use_barcode: bool,
    *,
    artist: str = "",
    release_title: str = "",
    barcode: str = "",
) -> RBIDisc:
    """Run one Discogs search (barcode or structured artist/title) and apply if confirmed."""
    from cdda2img import discogs_lookup
    from cdda2img.barcode import normalize_barcode
    from cdda2img.mb_lookup import _merge_into_disc, _overwrite_disc

    if use_barcode:
        effective = barcode or disc.catalog or ""
        normalized = normalize_barcode(effective) or effective
        label = f"barcode {normalized!r}"
        results = discogs_lookup.search_by_barcode(normalized)
    else:
        label = f"artist={artist!r} title={release_title!r}"
        results = discogs_lookup.search_releases(
            artist=artist, release_title=release_title
        )
    print(f"\n  Searching Discogs for {label} ...")
    if not results:
        print("  No results found.")
        return disc
    selected = _select_from_results(results, "Discogs Results")
    if selected is not None:
        mode = _confirm_apply(selected, disc)
        if mode:
            if selected.discogs_release_id and not selected.tracks:
                print("  Fetching full track listing from Discogs...")
                full = discogs_lookup.fetch_release(selected.discogs_release_id)
                if full and (full.album or full.tracks):
                    selected = full
            disc = (
                _merge_into_disc(selected, disc)
                if mode == "update"
                else _overwrite_disc(selected, disc)
            )
            print("  Applied.")
    return disc


def _discogs_menu(
    disc: RBIDisc, seed_artist: str = "", seed_title: str = ""
) -> RBIDisc:
    from cdda2img import discogs_lookup

    if not discogs_lookup.is_available():
        _header("Discogs Search")
        print("  Discogs requires a free personal access token.")
        print("  Set DISCOGS_TOKEN in your environment.")
        print("  Obtain one at: discogs.com/settings/developers")
        _prompt("  [Enter to return] ")
        return disc

    artist_q = disc.artist or seed_artist
    title_q = disc.album or seed_title

    while True:
        _header("Discogs Search")
        print(f"  Artist: {artist_q or '(none)'}")
        print(f"  Title:  {title_q or '(none)'}")
        print()
        print("  [s]  Search with current fields")
        print("  [e]  Edit artist / title")
        print("  [c]  Search by UPC/barcode")
        print("  [b]  Back")
        choice = _prompt("  > ").strip().lower()

        if choice == "b":
            return disc
        elif choice == "e":
            artist_q, title_q = _prompt_search_fields(artist_q, title_q)
        elif choice == "s":
            disc = _discogs_execute_search(
                disc, use_barcode=False, artist=artist_q, release_title=title_q
            )
        elif choice == "c":
            current = disc.catalog or ""
            raw = _prompt(f"  UPC/barcode [{current}]: ").strip()
            effective = raw or current
            if effective:
                disc = _discogs_execute_search(
                    disc, use_barcode=True, barcode=effective
                )
            else:
                print("  No barcode to search.")
        else:
            print("  Unknown command.")


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


def _acoustid_run_one(
    disc: RBIDisc, wav_path: Path, *, track_number: int | None = None
) -> RBIDisc:
    """Fingerprint *wav_path* via AcoustID, show results, apply if confirmed.

    When *track_number* is given, single-track results with number=None are
    tagged with that number so title/ISRC merge into the correct disc track.
    Returns the (possibly updated) disc; identity unchanged means no change applied.
    """
    from cdda2img import acoustid_lookup
    from cdda2img.mb_lookup import _merge_into_disc, _overwrite_disc, lookup_release

    print(f"  Fingerprinting {wav_path.name}... (may take a few seconds)")
    results = acoustid_lookup.fingerprint_and_lookup(wav_path, verbose=True)
    if not results:
        print("  No confident matches found.")
        print(
            "  (Ensure fpcalc/libchromaprint is on PATH and ACOUSTID_API_KEY is set.)"
        )
        return disc
    if track_number is not None:
        for result in results:
            if len(result.tracks) == 1 and result.tracks[0].number is None:
                result.tracks[0].number = track_number
    selected = _select_from_results(results, "AcoustID Matches")
    if selected is not None:
        mode = _confirm_apply(selected, disc)
        if mode:
            if selected.mb_release_id and len(selected.tracks) < len(disc.tracks):
                print("  Fetching full track listing from MusicBrainz...")
                full = lookup_release(
                    selected.mb_release_id, disc_number=disc.disc_number
                )
                if full and (full.album or full.tracks):
                    selected = full
            updated = (
                _merge_into_disc(selected, disc)
                if mode == "update"
                else _overwrite_disc(selected, disc)
            )
            print("  Applied.")
            return updated
    return disc


def _acoustid_file_loop(disc: RBIDisc) -> RBIDisc:
    """Prompt for a file path; loop until Enter with no path."""
    while True:
        path_str = _prompt("  Audio file path (or Enter to return): ").strip()
        if not path_str:
            return disc
        wav_path = Path(path_str)
        if not wav_path.exists():
            print(f"  File not found: {path_str}")
            continue
        num_str = _prompt("  Track number (or Enter to skip): ").strip()
        track_num = int(num_str) if num_str.isdigit() else None
        disc = _acoustid_run_one(disc, wav_path, track_number=track_num)


def _acoustid_pcm_loop(disc: RBIDisc, source_pcm: Path) -> RBIDisc:
    """Per-track fingerprint loop — extracts each track on demand from *source_pcm*."""
    valid_nums = {t.track_number for t in disc.tracks}
    wav_cache: dict[int, Path] = {}

    with tempfile.TemporaryDirectory(prefix="cdda2img_aid_") as td:
        tmp_dir = Path(td)
        while True:
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
            print()
            print("  Enter track number, [f] for file path, or [b] to return:")
            choice = _prompt("  > ").strip().lower()

            if choice == "b":
                return disc
            if choice == "f":
                disc = _acoustid_file_loop(disc)
                continue
            if not choice.isdigit() or int(choice) not in valid_nums:
                print("  Invalid selection.")
                continue
            track_num = int(choice)
            if track_num not in wav_cache:
                out_path = tmp_dir / f"track{track_num:02d}.wav"
                extracted = _pcm_extract_track_wav(
                    disc, source_pcm, track_num, out_path
                )
                if not extracted:
                    print(f"  Could not extract track {track_num}.")
                    continue
                wav_cache[track_num] = extracted
            disc = _acoustid_run_one(disc, wav_cache[track_num], track_number=track_num)


def _acoustid_wavs_loop(disc: RBIDisc, source_wavs: list[Path]) -> RBIDisc:
    """Per-track fingerprint loop — uses pre-transcoded WAV files from the create pipeline."""
    valid_nums = {t.track_number for t in disc.tracks}
    while True:
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
        print()
        print("  Enter track number, [f] for file path, or [b] to return:")
        choice = _prompt("  > ").strip().lower()

        if choice == "b":
            return disc
        if choice == "f":
            disc = _acoustid_file_loop(disc)
            continue
        if not choice.isdigit() or int(choice) not in valid_nums:
            print("  Invalid selection.")
            continue
        track_num = int(choice)
        idx = track_num - 1
        if idx >= len(source_wavs):
            print(f"  No WAV file for track {track_num}.")
            continue
        wav_path = source_wavs[idx]
        if not wav_path.exists():
            print(f"  WAV file not found: {wav_path.name}")
            continue
        disc = _acoustid_run_one(disc, wav_path, track_number=track_num)


def _acoustid_menu(
    disc: RBIDisc,
    source_pcm: Path | None = None,
    source_wavs: list[Path] | None = None,
) -> RBIDisc:
    from cdda2img import acoustid_lookup

    if not acoustid_lookup.is_available():
        _header("AcoustID Fingerprint")
        print(f"  Not available: {acoustid_lookup.unavailability_reason()}")
        _prompt("  [Enter to return] ")
        return disc

    if source_wavs and disc.tracks:
        return _acoustid_wavs_loop(disc, source_wavs)
    if source_pcm and source_pcm.exists() and disc.tracks:
        return _acoustid_pcm_loop(disc, source_pcm)

    _header("AcoustID Fingerprint")
    return _acoustid_file_loop(disc)


# ---------------------------------------------------------------------------
# Fetch sub-menu — native screen-stack port (cp3a): the blocking _fetch_menu
# loop was replaced by menu_state.FetchScreen, which dispatches MusicBrainz to
# the native MBSearchScreen and (pending cp3b/cp3c) Discogs/AcoustID to the
# legacy _discogs_menu / _acoustid_menu blocking helpers below.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Edit sub-menu
# ---------------------------------------------------------------------------


def _edit_disc_position(disc: RBIDisc) -> RBIDisc:
    """Prompt for disc number and total; validate and update disc in place."""
    _header("Edit Disc Position")
    print(f"  Current: disc {disc.disc_number} of {disc.disc_total}")
    print()
    while True:
        raw_num = _prompt(f"  Disc number [{disc.disc_number}]: ").strip()
        num = int(raw_num) if raw_num.isdigit() else disc.disc_number
        raw_total = _prompt(f"  Total discs [{disc.disc_total}]: ").strip()
        total = int(raw_total) if raw_total.isdigit() else disc.disc_total
        if num < 1 or total < 1 or num > total:
            print(f"  Invalid: disc {num} of {total} — number must be 1..total.")
            continue
        disc.disc_number = num
        disc.disc_total = total
        print(f"  Set: disc {num} of {total}.")
        return disc


def _edit_menu(disc: RBIDisc) -> RBIDisc:
    while True:
        _header("Edit Metadata")
        _print_disc_summary(disc)
        print()
        print("  [a]   Edit album title")
        print("  [r]   Edit artist")
        print("  [d]   Edit disc number / total")
        print("  [t N] Edit track N  (e.g.  t 3)")
        print("  [b]   Back")
        choice = _prompt("  > ").strip().lower()

        if choice == "b":
            return disc
        elif choice == "a":
            disc.album = _prompt_edit("Album title", disc.album or "")
        elif choice == "r":
            disc.artist = _prompt_edit("Artist", disc.artist or "")
        elif choice == "d":
            disc = _edit_disc_position(disc)
        elif choice.startswith("t "):
            try:
                num = int(choice[2:].strip())
                disc = _edit_track(disc, num)
            except ValueError:
                print("  Invalid track number.")
        else:
            print("  Unknown command.")


def _edit_track(disc: RBIDisc, track_number: int) -> RBIDisc:
    track = next((t for t in disc.tracks if t.track_number == track_number), None)
    if not track:
        print(f"  Track {track_number} not found.")
        return disc
    while True:
        _header(f"Edit Track {track_number}")
        print(f"  Title:     {track.title}")
        print(f"  Performer: {track.performer}")
        print(f"  ISRC:      {track.isrc or '(none)'}")
        print()
        print("  [t]  Edit title")
        print("  [p]  Edit performer")
        print("  [i]  Edit ISRC")
        print("  [b]  Back")
        choice = _prompt("  > ").strip().lower()

        if choice == "b":
            return disc
        elif choice == "t":
            track.title = _prompt_edit("Title", track.title)
        elif choice == "p":
            track.performer = _prompt_edit("Performer", track.performer)
        elif choice == "i":
            raw = _prompt_edit(
                "ISRC (12 chars, blank to clear)", track.isrc or ""
            ).upper()
            track.isrc = raw if raw else None
        else:
            print("  Unknown command.")


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


def _search_and_select_original(
    disc: RBIDisc, mb_rg_id: str | None
) -> tuple[bool, str | None]:
    """Run the MB search/select loop.

    Returns ``(applied, new_mb_rg_id)``.  ``applied=True`` means the user
    pressed [a] on a result — the menu should exit even when the chosen
    release carries no RG id of its own.  ``applied=False`` means the user
    backed out without applying anything.
    """
    releases, mb_rg_id = _fetch_releases_for_group(disc, mb_rg_id)
    if not releases:
        print("  No results found.")
        _prompt("  [Enter to return] ")
        return (False, mb_rg_id)

    releases_sorted = sorted(releases, key=lambda m: m.release_date or "9999")
    print(f"\n  {len(releases_sorted)} release(s) found, sorted earliest first.")

    while True:
        selected = _select_from_results(
            releases_sorted, "Original Release - Earliest First"
        )
        if selected is None:
            return (False, mb_rg_id)

        _header("Selected Release")
        _print_meta_summary(selected)
        print()
        print("  [a]  Apply as original release")
        print("  [b]  Back to list")
        sel_choice = _prompt("  > ").strip().lower()

        if sel_choice == "a":
            new_rg = _apply_selected_release(disc, selected)
            return (True, new_rg or mb_rg_id)


def _original_release_menu(
    disc: RBIDisc, mb_rg_id: str | None
) -> tuple[RBIDisc, str | None]:
    while True:
        _header("Find Original Release")
        print("  [s]  Search MusicBrainz")
        print("  [m]  Set manually")
        print("  [c]  Clear")
        print("  [b]  Back")
        choice = _prompt("  > ").strip().lower()

        if choice in ("b", "q", ""):
            return disc, mb_rg_id
        if choice == "m":
            disc = _set_original_manually(disc)
            return disc, mb_rg_id
        if choice == "c":
            disc.original_release_found = False
            disc.original_release_title = None
            disc.original_release_year = None
            print("  Cleared.")
            return disc, mb_rg_id
        if choice != "s":
            print("  Enter s, m, c, or b.")
            continue

        applied, mb_rg_id = _search_and_select_original(disc, mb_rg_id)
        if applied:
            return disc, mb_rg_id
        # Otherwise loop back to the top menu.


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
) -> RBIDisc:
    """Display current metadata and run the interactive enrichment/confirmation menu.

    *source_pcm* — raw s16le PCM file (import pipeline): enables per-track WAV
    extraction for AcoustID fingerprinting.
    *source_wavs* — per-track WAV list (create pipeline): used directly for
    AcoustID fingerprinting without extraction.
    *ar_summary* — pre-rendered AccurateRip report (rip pipeline). When
    provided, an AR_PAUSE state is shown before the main menu so the user
    can review the verification before editing metadata.

    Returns the (possibly updated) RBIDisc. Returns *disc* unchanged when stdin
    is not a TTY (batch/scripted mode).

    Backed by ``menu_state.MenuController`` — the top-level event loop is
    a state machine over ``MenuState``. Each state's renderer clears the
    screen and draws from origin (fixed-position / redraw semantics).
    """
    from cdda2img.menu_state import MenuController

    return MenuController(
        disc,
        source_pcm=source_pcm,
        source_wavs=source_wavs,
        ar_summary=ar_summary,
        tui=tui,
    ).run()
