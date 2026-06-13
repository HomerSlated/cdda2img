"""
catalogue_menu.py — interactive disc catalogue browser (``d`` subcommand).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from cdda2img.rbi_format import format_original_fields

# ---------------------------------------------------------------------------
# Terminal helpers (same conventions as metadata_menu.py)
# ---------------------------------------------------------------------------

_W = 78
_PAGE = 10


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


def _parse_selection_range(s: str, total: int) -> list[int]:
    """Parse a 1-based selection string into a list of 0-based indices.

    Accepts a single integer ("3") or a range ("1-3", "1 - 3", "1- 3", "1 -3").
    Returns an empty list for invalid input or values outside 1..total.
    """
    s = s.strip()
    m = re.match(r"^(\d+)\s*-\s*(\d+)$", s)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
        return [i - 1 for i in range(lo, hi + 1) if 1 <= i <= total]
    try:
        idx = int(s) - 1
    except ValueError:
        return []
    return [idx] if 0 <= idx < total else []


def _prompt(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return "q"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_dur(frames: int) -> str:
    """Format a frame count as M:SS."""
    total_sec = frames // 75
    return f"{total_sec // 60}:{total_sec % 60:02d}"


def _fmt_size(n: int) -> str:
    """Format a byte count as a human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n}{unit}"
        n //= 1024
    return f"{n}TB"


# ---------------------------------------------------------------------------
# Summary page
# ---------------------------------------------------------------------------


def _show_summary(conn: object) -> None:
    import sqlite3

    assert isinstance(conn, sqlite3.Connection)  # noqa: S101

    owner_row = conn.execute(
        "SELECT value FROM db_meta WHERE key='owner_name'"
    ).fetchone()
    owner = (owner_row[0] or "").strip() if owner_row else ""

    row = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT artist), MIN(registered_at), MAX(registered_at) "
        "FROM catalogue"
    ).fetchone()
    disc_count, artist_count, oldest, newest = row or (0, 0, None, None)

    track_count = conn.execute("SELECT COUNT(*) FROM catalogue_tracks").fetchone()[0]

    title = (
        f"{owner}'s Disc Catalogue — Summary" if owner else "Disc Catalogue — Summary"
    )
    _header(title)
    print(f"  Discs:    {disc_count}")
    print(f"  Tracks:   {track_count}")
    print(f"  Artists:  {artist_count}")
    if oldest and newest:
        print(f"  Earliest: {oldest[:10]}")
        print(f"  Latest:   {newest[:10]}")

    if disc_count == 0:
        print()
        print("  Catalogue is empty.")
        return

    print()
    print("  Top artists:")
    rows = conn.execute(
        "SELECT artist, COUNT(*) AS n FROM catalogue GROUP BY artist ORDER BY n DESC LIMIT 5"
    ).fetchall()
    for artist, n in rows:
        print(f"    {n:>3}  {artist}")


# ---------------------------------------------------------------------------
# Search and results
# ---------------------------------------------------------------------------


def _summary_prompt() -> str:
    """Show action prompt on the summary page. Returns 'search' or 'quit'."""
    print()
    print("  [s] search  [q] quit")
    while True:
        choice = _prompt("  > ").strip().lower()
        if choice in ("s", ""):
            return "search"
        if choice == "q":
            return "quit"


def _search_loop(conn: object) -> str:
    """Drive search/results loop. Returns 'summary' (blank Enter) or 'quit' (q/EOF)."""
    while True:
        _header("Disc Catalogue — Search")
        print("  Enter search terms (artist, album, year, or track title).")
        print("  Leave blank and press Enter to return to the summary.")
        query = _prompt("  > ").strip()
        if not query:
            return "summary"
        if query == "q":
            return "quit"

        result = _run_search(conn, query)
        if result != "search":
            return "quit"


def _run_search(conn: object, query: str) -> str | None:
    """Execute search and display paginated results. Returns 'search' to loop."""
    import sqlite3

    assert isinstance(conn, sqlite3.Connection)  # noqa: S101

    like = f"%{query}%"
    rows = conn.execute(
        "SELECT DISTINCT c.id, c.artist, c.album, c.year, c.disc_number, c.disc_total, c.track_count "
        "FROM catalogue c "
        "LEFT JOIN catalogue_tracks ct ON ct.catalogue_id = c.id "
        "WHERE c.artist LIKE ? OR c.album LIKE ? OR CAST(c.year AS TEXT) LIKE ? OR ct.title LIKE ? "
        "ORDER BY c.artist, c.album, c.disc_number",
        (like, like, like, like),
    ).fetchall()

    if not rows:
        print(f"\n  No results for {query!r}.")
        _prompt("  [Enter to search again] ")
        return "search"

    return _results_loop(conn, rows, query)


def _results_loop(conn: object, rows: list, query: str) -> str | None:  # noqa: C901
    """Paginate *rows*; let user select a record or go back/search again."""
    total = len(rows)
    total_pages = max(1, (total + _PAGE - 1) // _PAGE)
    page = 0

    while True:
        start = page * _PAGE
        page_items = rows[start : start + _PAGE]

        _header(f"Results for {query!r}  [{page + 1}/{total_pages}]  ({total} found)")
        print(f"  {'#':>3}  {'Artist':<24}  {'Album':<28}  {'Year':<4}  Trk")
        print(f"  {'─' * 3}  {'─' * 24}  {'─' * 28}  {'─' * 4}  {'─' * 3}")
        for i, row in enumerate(page_items, start=start + 1):
            _, artist, album, year, dn, dt, n_tracks = row
            disc_str = f" ({dn}/{dt})" if dt and dt > 1 else ""
            print(
                f"  {i:>3}  {_trunc(artist, 24):<24}  "
                f"{_trunc((album or '') + disc_str, 28):<28}  "
                f"{(str(year) if year else ''):>4}  {n_tracks:>3}"
            )

        print()
        nav = []
        if page > 0:
            nav.append("[p] prev")
        if page < total_pages - 1:
            nav.append("[n] next")
        nav.append("[s] new search")
        nav.append("[d] delete")
        nav.append("[q] quit")
        print("  " + "  ".join(nav))

        choice = _prompt(f"  Select 1-{total} or command: ").strip().lower()
        if choice == "n" and page < total_pages - 1:
            page += 1
        elif choice == "p" and page > 0:
            page -= 1
        elif choice in ("s", ""):
            return "search"
        elif choice == "q":
            return None
        elif choice == "d":
            del_str = _prompt(f"  Delete which entry (1-{total})? ").strip()
            del_indices = _parse_selection_range(del_str, total)
            if not del_indices:
                print("  Invalid selection.")
            else:
                import sqlite3

                assert isinstance(conn, sqlite3.Connection)  # noqa: S101
                to_delete = [rows[i] for i in del_indices]
                if len(to_delete) == 1:
                    label = f"{to_delete[0][1] or ''} — {to_delete[0][2] or ''}"
                else:
                    label = f"{len(to_delete)} entries"
                confirm = _prompt(f"  Delete {label!r}? [y/N] ").strip().lower()
                if confirm == "y":
                    ids_to_delete = {r[0] for r in to_delete}
                    with conn:
                        for rec_id in ids_to_delete:
                            conn.execute("DELETE FROM catalogue WHERE id=?", (rec_id,))
                    rows = [r for r in rows if r[0] not in ids_to_delete]
                    total = len(rows)
                    total_pages = max(1, (total + _PAGE - 1) // _PAGE)
                    page = min(page, max(0, total_pages - 1))
                    n = len(ids_to_delete)
                    print(f"  Deleted {n} {'entry' if n == 1 else 'entries'}.")
                    if total == 0:
                        _prompt("  No results remain. [Enter to search again] ")
                        return "search"
        else:
            try:
                idx = int(choice) - 1
                if 0 <= idx < total:
                    cat_id = rows[idx][0]
                    _show_record(conn, cat_id)
                else:
                    print("  Invalid selection.")
            except ValueError:
                print("  Invalid selection.")


# ---------------------------------------------------------------------------
# Record detail view
# ---------------------------------------------------------------------------


def _show_record(conn: object, catalogue_id: int) -> None:  # noqa: C901
    import sqlite3

    assert isinstance(conn, sqlite3.Connection)  # noqa: S101

    row = conn.execute(
        "SELECT album, artist, year, disc_number, disc_total, "
        "track_count, mcn, low_dynamic_range, original_release_found, "
        "original_release_title, original_release_year, "
        "mode, source, ripper, drive, "
        "rg_album_gain, rg_album_peak, rg_album_range, "
        "file_basename, file_path, file_size, registered_at, created_by "
        "FROM catalogue WHERE id=?",
        (catalogue_id,),
    ).fetchone()
    if row is None:
        print("  Record not found.")
        return

    (
        album,
        artist,
        year,
        disc_number,
        disc_total,
        _track_count,
        mcn,
        low_dynamic_range,
        original_release_found,
        original_release_title,
        original_release_year,
        mode,
        source,
        ripper,
        drive,
        rg_gain,
        rg_peak,
        rg_range,
        file_basename,
        file_path,
        file_size,
        registered_at,
        created_by,
    ) = row

    disc_str = (
        f" (disc {disc_number}/{disc_total})" if disc_total and disc_total > 1 else ""
    )
    year_str = f" ({year})" if year else ""

    _header(f"{artist} — {album}{year_str}{disc_str}")
    if mcn:
        print(f"  MCN:           {mcn}")
    if low_dynamic_range is not None:
        print(f"  Low DR:        {'YES' if low_dynamic_range else 'NO'}")
    if original_release_found:
        # Extract value from the canonical "Original:  <value>" form and re-align
        # to the 15-char label column used by all other fields in this view.
        orig_text = format_original_fields(
            year, True, original_release_title, original_release_year
        )
        _, _, orig_value = orig_text.partition(":  ")
        print(f"  {'Original:':<15}{orig_value}")
    if mode and mode != "?":
        print(f"  Mode:          {mode}")
    if source:
        print(f"  Source:        {source}")
    if ripper:
        print(f"  Ripper:        {ripper}")
    if drive:
        print(f"  Drive:         {drive}")
    if rg_gain is not None:
        print(
            f"  RG album gain: {rg_gain:+.2f} dB  peak {rg_peak:.6f}  LRA {rg_range:.1f}"
        )
    print(f"  File:          {file_basename}  ({_fmt_size(file_size)})")
    print(f"  Path:          {file_path}")
    print(f"  Registered:    {registered_at[:19]}")
    if created_by:
        print(f"  Created by:    {created_by}")

    tracks = conn.execute(
        "SELECT track_number, title, duration_frames, "
        "rg_track_gain, rg_track_peak, rg_track_range, "
        "ar_v1_crc, ar_v2_crc, ar_status, ar_confidence "
        "FROM catalogue_tracks WHERE catalogue_id=? ORDER BY track_number",
        (catalogue_id,),
    ).fetchall()

    if not tracks:
        _prompt("\n  [Enter to return] ")
        return

    total_tracks = len(tracks)
    total_pages = max(1, (total_tracks + _PAGE - 1) // _PAGE)
    page = 0

    while True:
        start = page * _PAGE
        page_items = tracks[start : start + _PAGE]

        print()
        print(f"  {'#':>2}  {'Title':<36}  {'Dur':>5}  {'AR':>3}  Conf")
        print(f"  {'─' * 2}  {'─' * 36}  {'─' * 5}  {'─' * 3}  {'─' * 4}")
        for t in page_items:
            tnum, title, dur, _, _, _, _, _, ar_status, ar_conf = t
            dur_str = _fmt_dur(dur)
            ar_str = (ar_status or "   ")[:3]
            conf_str = str(ar_conf) if ar_conf is not None else ""
            print(
                f"  {tnum:>2}  {_trunc(title, 36):<36}  {dur_str:>5}  {ar_str:<3}  {conf_str}"
            )

        print()
        nav = []
        if page > 0:
            nav.append("[p] prev")
        if page < total_pages - 1:
            nav.append("[n] next")
        nav.append("[b] back to results")
        print("  " + "  ".join(nav))
        choice = _prompt("  > ").strip().lower()
        if choice == "n" and page < total_pages - 1:
            page += 1
        elif choice == "p" and page > 0:
            page -= 1
        else:
            return


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_catalogue_menu(catalogue_path: Path | None = None) -> None:
    """Interactive disc catalogue browser."""
    if not sys.stdin.isatty():
        print("Catalogue menu requires an interactive terminal.", file=sys.stderr)
        return

    if catalogue_path is None:
        import contextlib

        with contextlib.suppress(Exception):
            from cdda2img.config import load_config

            catalogue_path = load_config().catalogue_path

    from cdda2img.catalogue import catalogue_db_path, open_catalogue_db

    db_path = catalogue_path or catalogue_db_path()

    if not db_path.exists():
        print(f"  Catalogue database not found: {db_path}")
        print("  It will be created automatically on first rip/import/create.")
        return

    conn = open_catalogue_db(db_path)
    try:
        while True:
            _show_summary(conn)
            if _summary_prompt() == "quit":
                break
            if _search_loop(conn) == "quit":
                break
    finally:
        conn.close()
