"""Interactive page layout — clear-and-redraw model.

Every page transition calls draw(), which clears the terminal and renders
the full page from scratch. No scrollback; the terminal is always in a
known visual state.

Single-key navigation (cbreak) for menus; input() (cooked) for text fields.

Integrates with TerminalUI: call ui.pause() before entering pages,
ui.resume() after. The dict-based disc/track types match metadata_menu.py
conventions; use RBIDisc.to_dict() or build the dict manually when calling
from the live pipeline.
"""

from __future__ import annotations

import shutil
import sys
import termios
import tty
from typing import Any

# ── terminal helpers ──────────────────────────────────────────────────────────


def _cols() -> int:
    return shutil.get_terminal_size().columns - 1


def _clear() -> None:
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def draw(lines: list[str], prompt: str = "  > ") -> None:
    """Clear the terminal and render *lines*, leaving the cursor after *prompt*."""
    _clear()
    sys.stdout.write("\n".join(lines))
    sys.stdout.write(f"\n{prompt}")
    sys.stdout.flush()


def getch() -> str:
    """Read one character in cbreak, without echo."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _trunc(text: str | None, width: int) -> str:
    if not text:
        return ""
    return text if len(text) <= width else text[: width - 1] + "…"


# ── layout primitives ─────────────────────────────────────────────────────────


def _hr(char: str = "─") -> str:
    return char * _cols()


def header(title: str) -> list[str]:
    return [_hr("═"), f"  {title}", _hr()]


def kv_block(fields: list[tuple[str, str]]) -> list[str]:
    label_w = max((len(k) for k, _ in fields), default=0)
    return [f"  {k + ':':<{label_w + 1}}  {v}" for k, v in fields]


def track_table(tracks: list[dict[str, Any]]) -> list[str]:
    cols = _cols()
    n = len(tracks)
    num_w = max(1, len(str(n)))
    has_isrc = any(t.get("isrc") for t in tracks)
    isrc_w = 12 if has_isrc else 0
    title_w = max(20, cols - 2 - num_w - 2 - (isrc_w + 2 if has_isrc else 0))

    hdr = f"  {'#':>{num_w}}  {'Title':<{title_w}}"
    sep = f"  {'─' * num_w}  {'─' * title_w}"
    if has_isrc:
        hdr += f"  {'ISRC':<{isrc_w}}"
        sep += f"  {'─' * isrc_w}"

    lines = [hdr, sep]
    for t in tracks[:20]:
        row = (
            f"  {t['num']:>{num_w}}  {_trunc(t.get('title') or '', title_w):<{title_w}}"
        )
        if has_isrc:
            row += f"  {t.get('isrc') or '':<{isrc_w}}"
        lines.append(row)
    if n > 20:
        lines.append(f"  … and {n - 20} more")
    return lines


def compact_actions(items: list[tuple[str, str]]) -> list[str]:
    """Single-line footer — for familiar short-key menus."""
    parts = [f"[{key}] {desc}" for key, desc in items]
    return [_hr(), "  " + "   ".join(parts)]


def full_actions(items: list[tuple[str, str]]) -> list[str]:
    """Multi-line footer — for sub-menus with longer descriptions."""
    return [_hr()] + [f"  [{key}]  {desc}" for key, desc in items]


# ── page builders ─────────────────────────────────────────────────────────────


def page_metadata(disc: dict[str, Any]) -> list[str]:
    fields: list[tuple[str, str]] = []
    if disc.get("album"):
        fields.append(("Album", disc["album"]))
    if disc.get("artist"):
        fields.append(("Artist", disc["artist"]))
    if disc.get("catalog"):
        fields.append(("Catalog", disc["catalog"]))
    if disc.get("year"):
        fields.append(("Year", disc["year"]))
    fields.append(("Tracks", str(len(disc.get("tracks", [])))))

    return [
        *header("Metadata"),
        *kv_block(fields),
        "",
        *track_table(disc.get("tracks", [])),
        *compact_actions([
            ("a", "Accept"),
            ("f", "Fetch"),
            ("e", "Edit"),
            ("r", "Release"),
            ("u", "Reset"),
            ("c", "Clear"),
        ]),
    ]


def page_edit(disc: dict[str, Any]) -> list[str]:
    fields: list[tuple[str, str]] = [
        ("Album", disc.get("album") or "(none)"),
        ("Artist", disc.get("artist") or "(none)"),
    ]
    return [
        *header("Edit Metadata"),
        *kv_block(fields),
        *full_actions([
            ("a", "Edit album title"),
            ("r", "Edit artist"),
            ("d", "Edit disc number / total"),
            ("t N", "Edit track N  (e.g.  t 3)"),
            ("b", "Back"),
        ]),
    ]


def page_edit_track(track: dict[str, Any]) -> list[str]:
    fields: list[tuple[str, str]] = [
        ("Title", track.get("title") or "(none)"),
        ("ISRC", track.get("isrc") or "(none)"),
    ]
    return [
        *header(f"Edit Track {track.get('num', '?')}"),
        *kv_block(fields),
        *full_actions([
            ("t", "Edit title"),
            ("i", "Edit ISRC"),
            ("b", "Back"),
        ]),
    ]


def page_fetch() -> list[str]:
    return [
        *header("Fetch Metadata"),
        *full_actions([
            ("m", "MusicBrainz text search"),
            ("d", "Discogs search"),
            ("a", "AcoustID fingerprint"),
            ("b", "Back"),
        ]),
    ]


def page_results(title: str, results: list[dict[str, Any]]) -> list[str]:
    cols = _cols()
    artist_w = min(22, cols // 4)
    album_w = max(16, cols - artist_w - 4 - 3 - 12)

    hdr = (
        f"  {'#':>3}  {' Artist':<{artist_w}}  {'Album':<{album_w}}"
        f"  {'Year':<4}  {'Cty'}"
    )
    sep = f"  {'─' * 3}  {'─' * artist_w}  {'─' * album_w}  {'─' * 4}  {'─' * 3}"
    rows = []
    for i, r in enumerate(results[:10], 1):
        rows.append(
            f"  {i:>3}  {_trunc(r.get('artist'), artist_w):<{artist_w}}"
            f"  {_trunc(r.get('album'), album_w):<{album_w}}"
            f"  {(r.get('year') or ''):<4}"
            f"  {(r.get('country') or '')[:3]}"
        )
    return [
        *header(f"{title}  ({len(results)} results)"),
        hdr,
        sep,
        *rows,
        *full_actions([("b", "Back without selecting")]),
    ]
