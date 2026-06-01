#!/usr/bin/env python3
"""
trace_album.py — Reproduce the album-title precedence that backs the "Album:" line.

This is a condensed, single-purpose extract of the cdda2img metadata pipeline. It
models ONLY the operations that decide which string ends up in ``RBIDisc.album`` —
the value rendered by ``metadata_menu._print_disc_summary`` as the album-title
component of the ``Album:`` line. The trailing ``(year)`` suffix, the ``Original:``
line, and every other field are out of scope.

The load-bearing logic for the album title is *merge precedence*, nothing else:

  * Every automatic, pre-menu lookup (CD-Text seed, CDDB, MusicBrainz, AcoustID,
    Discogs) fills ``album`` ONLY when the disc's existing ``album`` is blank.
    The rule is literally ``album = disc.album if disc.album else (meta.album or
    disc.album)`` — see ``cddb.prepopulate_from_cddb`` and
    ``mb_lookup._merge_into_disc``. "First non-blank wins."
  * The interactive metadata menu is the only place a *non-blank* album is
    replaced: Edit-album (direct set), Clear (set ""), Reset/undo (restore the
    menu-start snapshot), and Fetch with the "Overwrite all" mode
    (``mb_lookup._overwrite_disc``, which prefers ``meta.album`` when set).
  * An unresolved MB disc-ID multi-match contributes NO album: the agreed-facts
    meta leaves ``album=None`` (``mb_lookup._build_agreed_facts_meta``), so MB's
    title only flows on a resolved single pressing (single match, R1 ISRC/MCN
    disambiguation, or R4 ISRC tally).

Pipeline divergence (album seed origin and which lookups run):

  * rip    — seed = cdrdao CD-Text TITLE (PTI 0x80); blank-fill order
              CD-Text > CDDB > MB (Discogs/AcoustID merge album-level only).
  * import — seed = per-reader CD-Text/metadata parse; MB only (CDDB skipped).
  * create — seed = file tags (mutagen, ``metadata.derive_album_info``); NO
              network lookups at all — only the menu can change the album.

This tool does not hit the network; it takes the *resolved* lookup album values
as inputs so you can test the precedence directly against any combination.

Usage (from project root):
    # rip path: CD-Text blank, CDDB and MB both return a title
    uv run python tools/trace_album.py rip \\
        --cdtext "" --cddb "American Idiot" \\
        --mb "American Idiot: The Ultimate American Idiot"

    # rip path: CD-Text already carries the reissue title (it wins, lookups ignored)
    uv run python tools/trace_album.py rip \\
        --cdtext "American Idiot: The Ultimate American Idiot" \\
        --cddb "American Idiot" --mb "American Idiot"

    # import path (no CDDB): seed from the image's own CD-Text/metadata
    uv run python tools/trace_album.py import --seed "" --mb "Some Album"

    # create path (file tags only, no network)
    uv run python tools/trace_album.py create --tag-album "Some Album"

    # apply a menu mutation after the auto-merge resolves
    uv run python tools/trace_album.py rip --cdtext "" --mb "Reissue (2015)" \\
        --menu overwrite --menu-album "Original 2004 Title"
"""

from __future__ import annotations

import argparse
import sys


def fill_blank(existing: str, candidate: str | None) -> str:
    """Non-blank-wins merge (``_merge_into_disc`` / ``prepopulate_from_cddb``).

    Mirrors ``album = disc.album if disc.album else (meta.album or disc.album)``:
    the candidate only takes effect when *existing* is blank.
    """
    return existing if existing else (candidate or existing)


def overwrite(existing: str, candidate: str | None) -> str:
    """Overwrite-all merge (``_overwrite_disc``): ``meta.album or disc.album``."""
    return (candidate or "") or existing


def resolve_seed_rip(cdtext: str, cddb: str | None, mb: str | None) -> tuple[str, str]:
    """Rip blank-fill chain: CD-Text seed > CDDB > MB. Returns (album, winner)."""
    album = cdtext  # parsed_to_rbi_disc(album=parsed.title) — CD-Text PTI 0x80
    winner = "CD-Text (PTI 0x80 TITLE)" if album else "(none)"
    after_cddb = fill_blank(album, cddb)
    if after_cddb != album:
        winner = "CDDB"
    album = after_cddb
    after_mb = fill_blank(album, mb)
    if after_mb != album:
        winner = "MusicBrainz disc-ID (resolved pressing)"
    album = after_mb
    return album, winner


def resolve_seed_import(seed: str, mb: str | None) -> tuple[str, str]:
    """Import blank-fill chain: per-reader seed > MB (CDDB skipped)."""
    album = seed
    winner = "image CD-Text / metadata" if album else "(none)"
    after_mb = fill_blank(album, mb)
    if after_mb != album:
        winner = "MusicBrainz disc-ID (resolved pressing)"
    return after_mb, winner


def resolve_seed_create(tag_album: str) -> tuple[str, str]:
    """Create chain: file tags only, no network lookups."""
    return tag_album, "file tags (mutagen)" if tag_album else "(none)"


def apply_menu(
    album: str,
    snapshot: str,
    action: str | None,
    menu_album: str | None,
) -> tuple[str, str]:
    """Apply one interactive-menu mutation. Returns (album, description)."""
    if action is None:
        return album, "no menu mutation (auto-resolved value accepted)"
    if action == "edit":
        # metadata_menu._edit_menu: disc.album = _prompt_edit(...) — direct set.
        return (menu_album or album), "menu Edit-album (direct overwrite)"
    if action == "clear":
        # menu_state.MainScreen [c] → _clear_disc → album=""
        return "", 'menu Clear (album set to "")'
    if action == "reset":
        # menu_state.MainScreen [u] → restore menu-start snapshot
        return snapshot, "menu Reset/undo (restore menu-start snapshot)"
    if action == "overwrite":
        # Fetch → _confirm_apply 'o' → _overwrite_disc (meta.album or disc.album)
        return overwrite(album, menu_album), "menu Fetch-Overwrite (_overwrite_disc)"
    msg = f"unknown menu action: {action!r}"
    raise SystemExit(msg)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="path", required=True)

    rip = sub.add_parser("rip", help="rip path (CD-Text > CDDB > MB)")
    rip.add_argument("--cdtext", default="", help="cdrdao CD-Text TITLE seed")
    rip.add_argument("--cddb", default=None, help="resolved CDDB album, if any")
    rip.add_argument("--mb", default=None, help="resolved MB pressing album, if any")

    imp = sub.add_parser("import", help="import path (seed > MB; no CDDB)")
    imp.add_argument("--seed", default="", help="image CD-Text/metadata album seed")
    imp.add_argument("--mb", default=None, help="resolved MB pressing album, if any")

    cre = sub.add_parser("create", help="create path (file tags only, no network)")
    cre.add_argument("--tag-album", default="", help="album from file tags")

    for sp in (rip, imp, cre):
        sp.add_argument(
            "--menu",
            choices=["edit", "clear", "reset", "overwrite"],
            default=None,
            help="optional interactive-menu mutation to apply after auto-resolve",
        )
        sp.add_argument(
            "--menu-album",
            default=None,
            help="album value supplied by the menu (edit/overwrite)",
        )

    args = p.parse_args(argv)

    if args.path == "rip":
        album, winner = resolve_seed_rip(args.cdtext, args.cddb, args.mb)
    elif args.path == "import":
        album, winner = resolve_seed_import(args.seed, args.mb)
    else:
        album, winner = resolve_seed_create(args.tag_album)

    # The menu-start snapshot is the auto-resolved album (undo restores this).
    snapshot = album
    final, menu_desc = apply_menu(album, snapshot, args.menu, args.menu_album)

    print(f"path            : {args.path}")
    print(f"auto-resolved   : {album!r}  (winner: {winner})")
    print(f"menu mutation   : {menu_desc}")
    print(f"rendered album  : {final or '(none)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
