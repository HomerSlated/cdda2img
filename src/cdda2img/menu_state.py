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

EDIT / FETCH / ORIGINAL_RELEASE are presently bridged by
:class:`LegacyDelegateScreen`, which calls the existing procedural helpers in
``metadata_menu``. Migrating each to native screens (so nested result/track
pages also participate in the stack) is the remaining work — see TODO.

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
    FETCH = auto()
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
            return Push(LegacyDelegateScreen(MenuState.FETCH))
        if choice == "e":
            return Push(LegacyDelegateScreen(MenuState.EDIT))
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


class LegacyDelegateScreen(Screen):
    """Checkpoint-1 bridge for the not-yet-migrated sub-menus.

    Until EDIT / FETCH / ORIGINAL_RELEASE are ported to native screens, this
    calls the existing procedural helper in ``metadata_menu`` — which runs its
    own render+input loop and returns when the user backs out. ``render`` is a
    no-op (the helper draws itself); the work happens once in ``handle_input``,
    after which we pop back to the parent. Behaviour-preserving.
    """

    def __init__(self, state: MenuState) -> None:
        self.state = state

    def render(self, ctl: MenuController) -> None:
        # The legacy helper renders itself inside handle_input.
        return

    def handle_input(self, ctl: MenuController) -> Nav:
        from cdda2img.metadata_menu import (
            _edit_menu,
            _fetch_menu,
            _original_release_menu,
        )

        if self.state is MenuState.EDIT:
            ctl.disc = _edit_menu(ctl.disc)
        elif self.state is MenuState.FETCH:
            ctl.disc, ctl.mb_rg_id = _fetch_menu(
                ctl.disc,
                ctl.mb_rg_id,
                source_pcm=ctl.source_pcm,
                source_wavs=ctl.source_wavs,
                seed_artist=ctl.seed_artist,
                seed_title=ctl.seed_title,
            )
        elif self.state is MenuState.ORIGINAL_RELEASE:
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
