"""
menu_state.py — Screen-stack controller for the metadata menu.

The menu is modelled as a stack of **screens**. The top of the stack is the
active page; descending into a sub-menu pushes a screen, backing out pops it,
and accepting at the root exits. Each screen is a pure repaint (``render``)
plus one input step (``handle_input``) that returns a *navigation intent*
(:class:`Push` / :class:`Pop` / :class:`Done` / :class:`Stay`) — the controller,
not the screen, mutates the stack. That keeps screens free of stack plumbing
and unit-testable in isolation.

    [MainScreen]                      ← root; "accept" → Done (exit)
    [MainScreen, ARPauseScreen]       ← AR summary shown first; Enter pops it
    [MainScreen, <sub-menu screen>]   ← Fetch / Edit / Original-release

``render`` runs every loop iteration after a screen-clear, giving the
"fixed-position / redraw" property; so all prompting, I/O, and state mutation
live in ``handle_input``. ``--no-tui`` drops the screen-clear only (output
appends to scrollback and stays capturable); the menu is still interactive.

EDIT is a native screen stack (:class:`EditScreen` → :class:`EditTrackScreen` /
:class:`EditDiscPositionScreen`). FETCH / ORIGINAL_RELEASE are still bridged by
:class:`LegacyDelegateScreen`, which calls the existing procedural helpers in
``metadata_menu``. Migrating those to native screens (so nested result pages
also participate in the stack) is the remaining work — see TODO.

stdin-not-a-tty → ``run()`` returns the disc unchanged; the loop never runs.
"""

from __future__ import annotations

import copy
import sys
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cdda2img.lookup_result import DiscMeta
    from cdda2img.rbi_format import RBIDisc

# ANSI screen-clear + cursor-home. Works on every terminal we care about
# (xterm-256color, linux, screen, tmux). Falls back to a blank line on
# pathological terminals — harmless.
_CLEAR_SCREEN = "\033[2J\033[H"


def _clear_screen() -> None:
    sys.stdout.write(_CLEAR_SCREEN)
    sys.stdout.flush()


class MenuState(Enum):
    """Identity of each screen — retained for inspection and the ``.state`` shim."""

    AR_PAUSE = auto()
    MAIN = auto()
    EDIT = auto()
    EDIT_TRACK = auto()
    EDIT_DISC_POSITION = auto()
    FETCH = auto()
    MB_SEARCH = auto()
    RESULTS = auto()
    ORIGINAL_RELEASE = auto()
    DONE = auto()


# ---------------------------------------------------------------------------
# Navigation intents
#
# A screen's handle_input returns one of these; the controller applies it to
# the stack. Screens never touch the stack directly — this is what makes a
# screen testable as "feed input → assert returned intent + controller mutation".
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Push:
    """Descend into a child screen."""

    screen: Screen


@dataclass(frozen=True)
class Pop:
    """Return to the parent screen (back out one level)."""


@dataclass(frozen=True)
class Done:
    """Accept and exit the whole menu."""


@dataclass(frozen=True)
class Stay:
    """Re-render the current screen (e.g. after setting a transient banner)."""


Nav = Push | Pop | Done | Stay


class Screen:
    """One menu page: a pure repaint plus one input step.

    ``render`` runs every loop iteration after a screen-clear, so it MUST be a
    side-effect-free repaint. All prompting, network I/O, and state mutation
    belong in ``handle_input``, which returns a :data:`Nav` intent.
    """

    #: Screen identity — surfaced by ``MenuController.state``.
    state: MenuState

    def render(self, ctl: MenuController) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def handle_input(self, ctl: MenuController) -> Nav:  # pragma: no cover
        raise NotImplementedError


class MainScreen(Screen):
    """Top-level metadata menu (the stack root)."""

    state = MenuState.MAIN

    def render(self, ctl: MenuController) -> None:
        from cdda2img.metadata_menu import _hr, _print_disc_summary

        print()
        _hr("═")
        print("  Metadata")
        _hr("─")
        _print_disc_summary(ctl.disc)
        print()
        if ctl.banner:
            print(f"  ! {ctl.banner}")
            print()
            ctl.banner = ""
        print("  [a]  Accept and continue")
        print("  [f]  Fetch metadata from remote services")
        print("  [e]  Edit metadata")
        print("  [r]  Find original release")
        print("  [u]  Reset to original (undo all changes this session)")
        print("  [c]  Clear all metadata")
        print()

    def handle_input(self, ctl: MenuController) -> Nav:
        from cdda2img.metadata_menu import _clear_disc, _prompt

        choice = _prompt("  > ").strip().lower()
        if choice == "a":
            return Done()
        if choice == "f":
            return Push(FetchScreen())
        if choice == "e":
            return Push(EditScreen())
        if choice == "r":
            return Push(LegacyDelegateScreen(MenuState.ORIGINAL_RELEASE))
        if choice == "u":
            ctl.disc = copy.deepcopy(ctl._original_disc)
            ctl.mb_rg_id = None
            ctl.banner = "Reset to original metadata."
            return Stay()
        if choice == "c":
            ctl.disc = _clear_disc(ctl.disc)
            ctl.mb_rg_id = None
            ctl.banner = "All metadata cleared."
            return Stay()
        ctl.banner = "Unknown command. Use a / f / e / r / u / c."
        return Stay()


class ARPauseScreen(Screen):
    """AccurateRip verification summary, shown before the main menu."""

    state = MenuState.AR_PAUSE

    def render(self, ctl: MenuController) -> None:
        from cdda2img.metadata_menu import _hr

        print()
        _hr("═")
        print("  AccurateRip Verification")
        _hr("─")
        print()
        for line in (ctl.ar_summary or "").splitlines():
            print(f"  {line}")
        print()
        _hr("─")
        print()

    def handle_input(self, ctl: MenuController) -> Nav:
        from cdda2img.metadata_menu import _prompt

        _prompt("  Press Enter to continue to the metadata menu > ")
        return Pop()


class EditScreen(Screen):
    """Edit-metadata sub-menu: album, artist, disc position, per-track edits.

    Native port of the legacy ``metadata_menu._edit_menu`` loop. Album and
    artist are edited inline (one ``_prompt_edit`` → ``Stay``); disc position
    and per-track edits descend into their own screens via ``Push``. The legacy
    loop's "re-render after each action" is the controller's ``Stay`` re-render.
    """

    state = MenuState.EDIT

    def render(self, ctl: MenuController) -> None:
        from cdda2img.metadata_menu import _header, _print_disc_summary

        _header("Edit Metadata")
        _print_disc_summary(ctl.disc)
        print()
        if ctl.banner:
            print(f"  ! {ctl.banner}")
            print()
            ctl.banner = ""
        print("  [a]   Edit album title")
        print("  [r]   Edit artist")
        print("  [d]   Edit disc number / total")
        print("  [t N] Edit track N  (e.g.  t 3)")
        print("  [b]   Back")

    def handle_input(self, ctl: MenuController) -> Nav:
        from cdda2img.metadata_menu import _prompt, _prompt_edit

        choice = _prompt("  > ").strip().lower()
        if choice == "b":
            return Pop()
        if choice == "a":
            ctl.disc.album = _prompt_edit("Album title", ctl.disc.album or "")
            return Stay()
        if choice == "r":
            ctl.disc.artist = _prompt_edit("Artist", ctl.disc.artist or "")
            return Stay()
        if choice == "d":
            return Push(EditDiscPositionScreen())
        if choice.startswith("t "):
            try:
                num = int(choice[2:].strip())
            except ValueError:
                ctl.banner = "Invalid track number."
                return Stay()
            track = next((t for t in ctl.disc.tracks if t.track_number == num), None)
            if track is None:
                ctl.banner = f"Track {num} not found."
                return Stay()
            return Push(EditTrackScreen(num))
        ctl.banner = "Unknown command."
        return Stay()


class EditTrackScreen(Screen):
    """Per-track edit page: title, performer, ISRC. Carries the track number.

    Native port of ``metadata_menu._edit_track``. The track is re-resolved from
    ``ctl.disc.tracks`` each step (tracks don't reorder mid-edit, so this is the
    same object the legacy helper held). If the track has vanished, pops back.
    """

    state = MenuState.EDIT_TRACK

    def __init__(self, track_number: int) -> None:
        self.track_number = track_number

    def _resolve(self, ctl: MenuController):  # -> RBITocEntry | None
        return next(
            (t for t in ctl.disc.tracks if t.track_number == self.track_number),
            None,
        )

    def render(self, ctl: MenuController) -> None:
        from cdda2img.metadata_menu import _header

        track = self._resolve(ctl)
        _header(f"Edit Track {self.track_number}")
        if track is not None:
            print(f"  Title:     {track.title}")
            print(f"  Performer: {track.performer}")
            print(f"  ISRC:      {track.isrc or '(none)'}")
        print()
        if ctl.banner:
            print(f"  ! {ctl.banner}")
            print()
            ctl.banner = ""
        print("  [t]  Edit title")
        print("  [p]  Edit performer")
        print("  [i]  Edit ISRC")
        print("  [b]  Back")

    def handle_input(self, ctl: MenuController) -> Nav:
        from cdda2img.metadata_menu import _prompt, _prompt_edit

        track = self._resolve(ctl)
        if track is None:
            return Pop()
        choice = _prompt("  > ").strip().lower()
        if choice == "b":
            return Pop()
        if choice == "t":
            track.title = _prompt_edit("Title", track.title)
            return Stay()
        if choice == "p":
            track.performer = _prompt_edit("Performer", track.performer)
            return Stay()
        if choice == "i":
            # Behaviour-preserving quirk: _prompt_edit returns the current value
            # on blank input, so a non-empty ISRC cannot be cleared here despite
            # the label. Fixing that is a separate, deliberate change (see TODO).
            raw = _prompt_edit("ISRC (12 chars, blank to clear)", track.isrc or "")
            raw = raw.upper()
            track.isrc = raw if raw else None
            return Stay()
        ctl.banner = "Unknown command."
        return Stay()


class EditDiscPositionScreen(Screen):
    """Edit disc number / total, with a validation loop expressed as ``Stay``.

    Native port of ``metadata_menu._edit_disc_position``. Both fields are read
    in one ``handle_input`` step; invalid input (number/total < 1 or
    number > total) sets a banner and stays (the legacy ``while True`` re-prompt);
    a valid pair is applied and we pop back to :class:`EditScreen`.
    """

    state = MenuState.EDIT_DISC_POSITION

    def render(self, ctl: MenuController) -> None:
        from cdda2img.metadata_menu import _header

        _header("Edit Disc Position")
        print(f"  Current: disc {ctl.disc.disc_number} of {ctl.disc.disc_total}")
        print()
        if ctl.banner:
            print(f"  ! {ctl.banner}")
            print()
            ctl.banner = ""

    def handle_input(self, ctl: MenuController) -> Nav:
        from cdda2img.metadata_menu import _prompt

        disc = ctl.disc
        raw_num = _prompt(f"  Disc number [{disc.disc_number}]: ").strip()
        num = int(raw_num) if raw_num.isdigit() else disc.disc_number
        raw_total = _prompt(f"  Total discs [{disc.disc_total}]: ").strip()
        total = int(raw_total) if raw_total.isdigit() else disc.disc_total
        if num < 1 or total < 1 or num > total:
            ctl.banner = f"Invalid: disc {num} of {total} — number must be 1..total."
            return Stay()
        disc.disc_number = num
        disc.disc_total = total
        ctl.banner = f"Set: disc {num} of {total}."
        return Pop()


class FetchScreen(Screen):
    """Fetch-metadata sub-menu: MusicBrainz / Discogs / AcoustID.

    Native port of the legacy ``metadata_menu._fetch_menu`` loop (cp3a).
    MusicBrainz is fully ported — [m] pushes :class:`MBSearchScreen`. Discogs and
    AcoustID still call the legacy blocking helpers (``_discogs_menu`` /
    ``_acoustid_menu``) as a bounded leaf interaction in ``handle_input``, pending
    their own screen ports (cp3b / cp3c).
    """

    state = MenuState.FETCH

    def render(self, ctl: MenuController) -> None:
        from cdda2img.metadata_menu import _header

        _header("Fetch Metadata")
        if ctl.banner:
            print(f"  ! {ctl.banner}")
            print()
            ctl.banner = ""
        print("  [m]  MusicBrainz text search")
        print("  [d]  Discogs search")
        print("  [a]  AcoustID fingerprint")
        print("  [b]  Back")

    def handle_input(self, ctl: MenuController) -> Nav:
        from cdda2img.metadata_menu import _acoustid_menu, _discogs_menu, _prompt

        choice = _prompt("  > ").strip().lower()
        if choice == "b":
            return Pop()
        if choice == "m":
            return Push(
                MBSearchScreen(
                    artist_q=ctl.disc.artist or ctl.seed_artist,
                    title_q=ctl.disc.album or ctl.seed_title,
                )
            )
        if choice == "d":
            ctl.disc = _discogs_menu(
                ctl.disc, seed_artist=ctl.seed_artist, seed_title=ctl.seed_title
            )
            return Stay()
        if choice == "a":
            ctl.disc = _acoustid_menu(
                ctl.disc, source_pcm=ctl.source_pcm, source_wavs=ctl.source_wavs
            )
            return Stay()
        ctl.banner = "Unknown command."
        return Stay()


class MBSearchScreen(Screen):
    """MusicBrainz search — the "enter query" frame (cp3a).

    Carries the artist/title query as instance state, seeded once at entry from
    the disc / controller seed fields and mutated only by the [e] edit action — so
    it does NOT drift to a post-apply ``disc.album`` (the query you searched with
    stays put even after a result is applied to the disc underneath). Executing a
    search pushes :class:`ResultsScreen` (the "pick result" frame); results are
    sorted earliest-first so the original pressing leads, matching the legacy
    ``_mb_select_and_apply`` order.
    """

    state = MenuState.MB_SEARCH

    def __init__(self, artist_q: str = "", title_q: str = "") -> None:
        self.artist_q = artist_q
        self.title_q = title_q

    def render(self, ctl: MenuController) -> None:
        from cdda2img.metadata_menu import _header

        _header("MusicBrainz Search")
        print(f"  Artist: {self.artist_q or '(none)'}")
        print(f"  Title:  {self.title_q or '(none)'}")
        print()
        if ctl.banner:
            print(f"  ! {ctl.banner}")
            print()
            ctl.banner = ""
        print("  [s]  Search with current fields")
        print("  [e]  Edit artist / title")
        print("  [u]  Search by UPC/barcode")
        print("  [b]  Back")

    def _mb_results(self, results: list[DiscMeta]) -> Push:
        # Earliest-first so the original pressing leads (legacy parity).
        results_sorted = sorted(results, key=lambda m: m.release_date or "9999")
        return Push(ResultsScreen(results_sorted, "MusicBrainz Results", "mb"))

    def handle_input(self, ctl: MenuController) -> Nav:
        from cdda2img.mb_lookup import (
            build_mb_search_query,
            search_releases,
            search_releases_by_barcode,
        )
        from cdda2img.metadata_menu import _prompt, _prompt_search_fields

        choice = _prompt("  > ").strip().lower()
        if choice == "b":
            return Pop()
        if choice == "e":
            self.artist_q, self.title_q = _prompt_search_fields(
                self.artist_q, self.title_q
            )
            return Stay()
        if choice == "u":
            current = ctl.disc.catalog or ""
            raw = _prompt(f"  UPC/barcode [{current}]: ").strip()
            effective = raw or current
            if not effective:
                ctl.banner = "No barcode to search."
                return Stay()
            print(f"\n  Searching MusicBrainz by barcode {effective!r} ...")
            results = search_releases_by_barcode(effective)
            if not results:
                ctl.banner = "No results found."
                return Stay()
            return self._mb_results(results)
        if choice == "s":
            query = build_mb_search_query(self.artist_q, self.title_q)
            print(f"\n  Searching MusicBrainz for {query!r} ...")
            results = search_releases(query)
            if not results:
                ctl.banner = "No results found."
                return Stay()
            return self._mb_results(results)
        ctl.banner = "Unknown command."
        return Stay()


class ResultsScreen(Screen):
    """Paginated result picker — the "pick result" frame (cp3a).

    A real render+one-keystroke frame: the page index is screen state, ``render``
    is a pure repaint (shared ``metadata_menu._render_results_page``), and each
    ``handle_input`` consumes one keystroke (n/p paginate; a number selects; b
    backs out). Selecting a result runs the source-specific apply tail (fetch the
    full release, confirm the diff, merge/overwrite) and pops back to the search
    frame — the legacy "one select+apply, then return to search" flow. Persistent
    feedback ("Applied.") is set as ``ctl.banner`` so it survives the screen-clear
    on the frame we pop to (a plain print would be wiped in TUI mode).
    """

    state = MenuState.RESULTS

    def __init__(self, results: list[DiscMeta], title: str, source: str) -> None:
        self.results = results
        self.title = title
        self.source = source
        self.page = 0

    def render(self, ctl: MenuController) -> None:
        from cdda2img.metadata_menu import _render_results_page

        _render_results_page(self.results, self.page, self.title)
        if ctl.banner:
            print()
            print(f"  ! {ctl.banner}")
            ctl.banner = ""

    def handle_input(self, ctl: MenuController) -> Nav:
        from cdda2img.metadata_menu import _PAGE, _prompt

        total = len(self.results)
        total_pages = max(1, (total + _PAGE - 1) // _PAGE)
        choice = _prompt(f"  Select 1-{total}: ").strip().lower()
        if choice == "n":
            if self.page < total_pages - 1:
                self.page += 1
            return Stay()
        if choice == "p":
            if self.page > 0:
                self.page -= 1
            return Stay()
        if choice == "b":
            return Pop()
        try:
            idx = int(choice) - 1
        except ValueError:
            ctl.banner = "Invalid selection."
            return Stay()
        if not 0 <= idx < total:
            ctl.banner = "Invalid selection."
            return Stay()
        self._apply_selected(ctl, self.results[idx])
        return Pop()

    def _apply_selected(self, ctl: MenuController, selected: DiscMeta) -> None:
        """Source-specific apply tail. cp3a implements MB; cp3b/cp3c extend."""
        from cdda2img.mb_lookup import _merge_into_disc, _overwrite_disc, lookup_release
        from cdda2img.metadata_menu import _confirm_apply

        if self.source == "mb":
            # Search hits are stubs (no track listing / ISRCs). Fetch the full
            # release BEFORE previewing so the diff reflects what will be applied.
            if selected.mb_release_id and not selected.tracks:
                print("  Fetching full track listing from MusicBrainz...")
                full = lookup_release(
                    selected.mb_release_id, disc_number=ctl.disc.disc_number
                )
                if full and (full.album or full.tracks):
                    selected = full
            mode = _confirm_apply(selected, ctl.disc)
            if not mode:
                return
            ctl.disc = (
                _merge_into_disc(selected, ctl.disc)
                if mode == "update"
                else _overwrite_disc(selected, ctl.disc)
            )
            ctl.mb_rg_id = selected.mb_release_group_id or ctl.mb_rg_id
            ctl.banner = "Applied."


class LegacyDelegateScreen(Screen):
    """Checkpoint-1 bridge for the not-yet-migrated sub-menus.

    Until ORIGINAL_RELEASE is ported to a native screen (cp4), this calls the
    existing procedural helper in ``metadata_menu`` — which runs its own
    render+input loop and returns when the user backs out. ``render`` is a no-op
    (the helper draws itself); the work happens once in ``handle_input``, after
    which we pop back to the parent. Behaviour-preserving. EDIT (cp2) and FETCH
    (cp3a) are now native screens and no longer routed through here.
    """

    def __init__(self, state: MenuState) -> None:
        self.state = state

    def render(self, ctl: MenuController) -> None:
        # The legacy helper renders itself inside handle_input.
        return

    def handle_input(self, ctl: MenuController) -> Nav:
        from cdda2img.metadata_menu import _original_release_menu

        if self.state is MenuState.ORIGINAL_RELEASE:
            ctl.disc, ctl.mb_rg_id = _original_release_menu(ctl.disc, ctl.mb_rg_id)
        return Pop()


class MenuController:
    """Screen-stack controller for the metadata menu.

    Constructed once per menu invocation. Owns the working disc, the user's
    undo savepoint, the seed search fields (immutable across edits — so "search
    again" doesn't drift after an edit), the MB release-group ID threaded
    between Fetch and Original-Release, and the AR summary for the AR_PAUSE page.

    ``run()`` drives the stack until accepted and returns the final disc.
    """

    def __init__(
        self,
        disc: RBIDisc,
        *,
        source_pcm: Path | None = None,
        source_wavs: list[Path] | None = None,
        ar_summary: str | None = None,
        tui: bool = True,
    ) -> None:
        self.disc: RBIDisc = disc
        self._original_disc: RBIDisc = copy.deepcopy(disc)  # undo savepoint
        self.source_pcm = source_pcm
        self.source_wavs = source_wavs
        self.ar_summary = ar_summary
        # When False (--no-tui), the menu renders by appending to the terminal
        # scrollback instead of clearing+redrawing each frame, so earlier
        # pipeline output (MB match line, etc.) stays capturable. The menu is
        # still interactive — only the screen-clear is dropped.
        self.tui = tui
        self.mb_rg_id: str | None = None
        # Seed search fields anchored to the disc state at menu start, not the
        # live disc.album/artist — so "Search again" after an edit still uses
        # the original CD-Text / CDDB / MB value as the seed.
        self.seed_artist: str = disc.artist or ""
        self.seed_title: str = disc.album or ""
        # Transient message banner — shown once on the next render then cleared.
        self.banner: str = ""
        # True once the user accepts at the root; ends run().
        self.done: bool = False
        # Screen stack: MAIN at the root, AR_PAUSE pushed on top when present.
        self.stack: list[Screen] = [MainScreen()]
        if ar_summary is not None:
            self.stack.append(ARPauseScreen())

    @property
    def state(self) -> MenuState:
        """Identity of the active screen (``DONE`` once accepted). Read-only."""
        if self.done:
            return MenuState.DONE
        return self.stack[-1].state

    def run(self) -> RBIDisc:
        """Drive the stack until accepted. Returns the final disc."""
        if not sys.stdin.isatty():
            return self.disc
        while not self.done and self.stack:
            self._step()
        return self.disc

    def _step(self) -> None:
        """Render the active screen and apply its navigation intent."""
        top = self.stack[-1]
        if self.tui:
            _clear_screen()
        top.render(self)
        self._apply(top.handle_input(self))

    def _apply(self, nav: Nav) -> None:
        """Apply a navigation intent to the stack."""
        if isinstance(nav, Push):
            self.stack.append(nav.screen)
        elif isinstance(nav, Pop):
            self.stack.pop()
        elif isinstance(nav, Done):
            self.done = True
        # Stay: no stack change; the loop re-renders the same screen.
