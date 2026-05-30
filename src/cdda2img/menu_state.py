"""
menu_state.py — Top-level state-machine controller for the metadata menu.

The pre-state-machine ``metadata_menu.run_metadata_menu`` used a single
procedural loop with `print` lines that scrolled the terminal. This
module replaces that loop with an explicit state graph:

    AR_PAUSE ──> MAIN ──> EDIT ──> MAIN
                    │     FETCH ──> MAIN
                    │     ORIGINAL_RELEASE ──> MAIN
                    │     RESET   ──> MAIN
                    │     CLEAR   ──> MAIN
                    └──> DONE  (accept)

Each top-level state owns a renderer that clears the screen and draws
from the top — the "fixed position / redraw" property the user asked
for. Sub-menu logic (edit a single field, pick a search result, walk
through an MB release group) is still implemented by helpers in
``metadata_menu`` for now; the controller calls them as state actions
and re-renders the top-level frame on return. Per-sub-state renderers
are a follow-up.

A new state, ``AR_PAUSE``, displays the AccurateRip verification
summary on its own page before the user enters the main menu. The
controller skips it when ``ar_summary`` is None (import / create
pipelines, which don't run AR).

stdin-not-a-tty → ``run()`` returns the disc unchanged. The state
machine never executes.
"""

from __future__ import annotations

import copy
import sys
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cdda2img.rbi_format import RBIDisc

# ANSI screen-clear + cursor-home. Works on every terminal we care about
# (xterm-256color, linux, screen, tmux). Falls back to a blank line on
# pathological terminals — harmless.
_CLEAR_SCREEN = "\033[2J\033[H"


def _clear_screen() -> None:
    sys.stdout.write(_CLEAR_SCREEN)
    sys.stdout.flush()


class MenuState(Enum):
    """Top-level menu states."""

    AR_PAUSE = auto()
    MAIN = auto()
    EDIT = auto()
    FETCH = auto()
    ORIGINAL_RELEASE = auto()
    DONE = auto()


class MenuController:
    """State-machine controller for the metadata menu.

    Constructed once per menu invocation. Owns the working disc, the
    user's undo savepoint, the seed search fields (immutable across
    edits — so that "search again" doesn't drift after an edit), the
    MB release-group ID threaded between Fetch and Original-Release,
    and the AR summary for the AR_PAUSE page.

    The ``run()`` method drives the state machine to ``DONE`` and
    returns the final disc.
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
        # When False (--no-tui), the menu renders by appending to the
        # terminal scrollback instead of clearing+redrawing each frame, so
        # earlier pipeline output (MB match line, etc.) stays capturable.
        # The menu is still interactive — only the screen-clear is dropped.
        self.tui = tui
        self.mb_rg_id: str | None = None
        # The seed search fields are anchored to the disc state at the
        # time the menu started, not the live disc.album/artist. That way
        # "Search again" after the user edited the title still uses the
        # original CD-Text / CDDB / MB value as the search seed.
        self.seed_artist: str = disc.artist or ""
        self.seed_title: str = disc.album or ""
        self.state: MenuState = (
            MenuState.AR_PAUSE if ar_summary is not None else MenuState.MAIN
        )
        # Transient message banner — shown once on the next render then
        # cleared. Used by "Reset to original" and "Cleared all metadata"
        # confirmations.
        self.banner: str = ""

    def run(self) -> RBIDisc:
        """Drive the state machine until DONE. Returns the final disc."""
        if not sys.stdin.isatty():
            return self.disc
        while self.state is not MenuState.DONE:
            if self.tui:
                _clear_screen()
            self._render()
            self._transition()
        return self.disc

    # -----------------------------------------------------------------
    # Render dispatch
    # -----------------------------------------------------------------

    def _render(self) -> None:
        if self.state is MenuState.AR_PAUSE:
            self._render_ar_pause()
        elif self.state is MenuState.MAIN:
            self._render_main()
        # EDIT / FETCH / ORIGINAL_RELEASE delegate to legacy helpers
        # that do their own (procedural) rendering; the top-level frame
        # is redrawn when control returns to MAIN.

    def _render_main(self) -> None:
        from cdda2img.metadata_menu import _hr, _print_disc_summary

        print()
        _hr("═")
        print("  Metadata")
        _hr("─")
        _print_disc_summary(self.disc)
        print()
        if self.banner:
            print(f"  ! {self.banner}")
            print()
            self.banner = ""
        print("  [a]  Accept and continue")
        print("  [f]  Fetch metadata from remote services")
        print("  [e]  Edit metadata")
        print("  [r]  Find original release")
        print("  [u]  Reset to original (undo all changes this session)")
        print("  [c]  Clear all metadata")
        print()

    def _render_ar_pause(self) -> None:
        from cdda2img.metadata_menu import _hr

        print()
        _hr("═")
        print("  AccurateRip Verification")
        _hr("─")
        print()
        for line in (self.ar_summary or "").splitlines():
            print(f"  {line}")
        print()
        _hr("─")
        print()

    # -----------------------------------------------------------------
    # Input + transition dispatch
    # -----------------------------------------------------------------

    def _transition(self) -> None:
        if self.state is MenuState.AR_PAUSE:
            self._handle_ar_pause_input()
        elif self.state is MenuState.MAIN:
            self._handle_main_input()
        elif self.state is MenuState.EDIT:
            self._handle_edit_state()
        elif self.state is MenuState.FETCH:
            self._handle_fetch_state()
        elif self.state is MenuState.ORIGINAL_RELEASE:
            self._handle_original_release_state()

    def _handle_ar_pause_input(self) -> None:
        from cdda2img.metadata_menu import _prompt

        _prompt("  Press Enter to continue to the metadata menu > ")
        self.state = MenuState.MAIN

    def _handle_main_input(self) -> None:
        from cdda2img.metadata_menu import _clear_disc, _prompt

        choice = _prompt("  > ").strip().lower()
        if choice == "a":
            self.state = MenuState.DONE
        elif choice == "f":
            self.state = MenuState.FETCH
        elif choice == "e":
            self.state = MenuState.EDIT
        elif choice == "r":
            self.state = MenuState.ORIGINAL_RELEASE
        elif choice == "u":
            self.disc = copy.deepcopy(self._original_disc)
            self.mb_rg_id = None
            self.banner = "Reset to original metadata."
        elif choice == "c":
            self.disc = _clear_disc(self.disc)
            self.mb_rg_id = None
            self.banner = "All metadata cleared."
        else:
            self.banner = "Unknown command. Use a / f / e / r / u / c."

    def _handle_edit_state(self) -> None:
        from cdda2img.metadata_menu import _edit_menu

        self.disc = _edit_menu(self.disc)
        self.state = MenuState.MAIN

    def _handle_fetch_state(self) -> None:
        from cdda2img.metadata_menu import _fetch_menu

        self.disc, self.mb_rg_id = _fetch_menu(
            self.disc,
            self.mb_rg_id,
            source_pcm=self.source_pcm,
            source_wavs=self.source_wavs,
            seed_artist=self.seed_artist,
            seed_title=self.seed_title,
        )
        self.state = MenuState.MAIN

    def _handle_original_release_state(self) -> None:
        from cdda2img.metadata_menu import _original_release_menu

        self.disc, self.mb_rg_id = _original_release_menu(self.disc, self.mb_rg_id)
        self.state = MenuState.MAIN
