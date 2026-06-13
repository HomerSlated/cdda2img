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

Every sub-menu is now a native screen stack: EDIT (:class:`EditScreen` →
:class:`EditTrackScreen` / :class:`EditDiscPositionScreen`), FETCH
(:class:`FetchScreen` → :class:`MBSearchScreen` / :class:`DiscogsSearchScreen` /
:class:`AcoustidScreen` → :class:`ResultsScreen`), and ORIGINAL_RELEASE
(:class:`OriginalReleaseScreen` → :class:`ResultsScreen`). Nested result pages
participate in the stack, so the legacy procedural-loop bridge is gone.

stdin-not-a-tty → ``run()`` returns the disc unchanged; the loop never runs.
"""

from __future__ import annotations

import copy
import sys
import tempfile
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

# Alternate screen buffer (DECSET 1049), the same mechanism vim/less/htop use.
# TUI mode runs the whole menu on the alt buffer so per-frame `\033[2J` redraws
# never pollute the main scrollback: `\033[2J` only erases the visible viewport
# and — on xterm/VTE — *saves* the erased lines into scrollback, so a clear-and-
# redraw menu on the main buffer accumulates its header up the scrollback on
# every repaint (the bug this fixes). On exit, 1049l restores the main buffer
# exactly as it was, preserving the pre-menu pipeline output (MB match line, AR
# report) that `--no-tui` exists to keep capturable.
_ENTER_FULLSCREEN = "\033[?1049h"
_EXIT_FULLSCREEN = "\033[?1049l"


def _clear_screen() -> None:
    sys.stdout.write(_CLEAR_SCREEN)
    sys.stdout.flush()


def _enter_fullscreen() -> None:
    sys.stdout.write(_ENTER_FULLSCREEN)
    sys.stdout.flush()


def _exit_fullscreen() -> None:
    sys.stdout.write(_EXIT_FULLSCREEN)
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
    DISCOGS = auto()
    ACOUSTID = auto()
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
            return Push(OriginalReleaseScreen())
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

    Native port of the legacy procedural Edit-metadata loop. Album and
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

    Native port of the legacy per-track edit loop. The track is re-resolved from
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
            # ISRC is the one field where blank means *clear*, not *keep*, so it
            # reads the prompt directly instead of via _prompt_edit (which returns
            # the current value on blank — the right idiom for title/performer but
            # wrong here). The current ISRC, echoed in the render above, is shown
            # in the prompt too when set.
            shown = f" [{track.isrc}]" if track.isrc else ""
            raw = _prompt(f"  ISRC (12 chars, blank to clear){shown}: ").strip().upper()
            track.isrc = raw or None
            return Stay()
        ctl.banner = "Unknown command."
        return Stay()


class EditDiscPositionScreen(Screen):
    """Edit disc number / total, with a validation loop expressed as ``Stay``.

    Native port of the legacy disc-position edit loop. Both fields are read
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

    Native port of the legacy ``metadata_menu._fetch_menu`` loop (cp3a/b/c).
    MusicBrainz, Discogs and AcoustID are all native screens now — [m]/[d]/[a]
    push the corresponding search / track-picker screens.
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

    def _push_acoustid(self, ctl: MenuController) -> Nav:
        from cdda2img import acoustid_lookup

        if not acoustid_lookup.is_available():
            ctl.banner = (
                f"AcoustID not available: {acoustid_lookup.unavailability_reason()}"
            )
            return Stay()
        # Same dispatch as the legacy _acoustid_menu: pre-transcoded WAVs (create
        # pipeline) → on-demand PCM extraction (rip/import) → file-path entry.
        if ctl.disc.tracks and ctl.source_wavs:
            return Push(AcoustidScreen(source_wavs=ctl.source_wavs))
        if ctl.disc.tracks and ctl.source_pcm and ctl.source_pcm.exists():
            return Push(AcoustidScreen(source_pcm=ctl.source_pcm))
        return Push(AcoustidFileScreen())

    def handle_input(self, ctl: MenuController) -> Nav:
        from cdda2img.metadata_menu import _prompt

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
            return Push(
                DiscogsSearchScreen(
                    artist_q=ctl.disc.artist or ctl.seed_artist,
                    title_q=ctl.disc.album or ctl.seed_title,
                )
            )
        if choice == "a":
            return self._push_acoustid(ctl)
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


class DiscogsSearchScreen(Screen):
    """Discogs search — the "enter query" frame (cp3b).

    Mirrors :class:`MBSearchScreen`: artist/title query as instance state seeded
    at entry, mutated only by [e]. When Discogs is unavailable (no DISCOGS_TOKEN)
    the screen renders the token help and pops on any key, preserving the legacy
    ``_discogs_menu`` guard. [s]/[c] run the search in ``handle_input`` and push a
    :class:`ResultsScreen` with ``source="discogs"``.
    """

    state = MenuState.DISCOGS

    def __init__(self, artist_q: str = "", title_q: str = "") -> None:
        self.artist_q = artist_q
        self.title_q = title_q

    def render(self, ctl: MenuController) -> None:
        from cdda2img import discogs_lookup
        from cdda2img.metadata_menu import _header

        _header("Discogs Search")
        if not discogs_lookup.is_available():
            print("  Discogs requires a free personal access token.")
            print("  Set DISCOGS_TOKEN in your environment.")
            print("  Obtain one at: discogs.com/settings/developers")
            return
        print(f"  Artist: {self.artist_q or '(none)'}")
        print(f"  Title:  {self.title_q or '(none)'}")
        print()
        if ctl.banner:
            print(f"  ! {ctl.banner}")
            print()
            ctl.banner = ""
        print("  [s]  Search with current fields")
        print("  [e]  Edit artist / title")
        print("  [c]  Search by UPC/barcode")
        print("  [b]  Back")

    def handle_input(self, ctl: MenuController) -> Nav:
        from cdda2img import discogs_lookup
        from cdda2img.barcode import normalize_barcode
        from cdda2img.metadata_menu import _prompt, _prompt_search_fields

        if not discogs_lookup.is_available():
            _prompt("  [Enter to return] ")
            return Pop()
        choice = _prompt("  > ").strip().lower()
        if choice == "b":
            return Pop()
        if choice == "e":
            self.artist_q, self.title_q = _prompt_search_fields(
                self.artist_q, self.title_q
            )
            return Stay()
        if choice == "s":
            label = f"artist={self.artist_q!r} title={self.title_q!r}"
            print(f"\n  Searching Discogs for {label} ...")
            results = discogs_lookup.search_releases(
                artist=self.artist_q, release_title=self.title_q
            )
            if not results:
                ctl.banner = "No results found."
                return Stay()
            return Push(ResultsScreen(results, "Discogs Results", "discogs"))
        if choice == "c":
            current = ctl.disc.catalog or ""
            raw = _prompt(f"  UPC/barcode [{current}]: ").strip()
            effective = raw or current
            if not effective:
                ctl.banner = "No barcode to search."
                return Stay()
            normalized = normalize_barcode(effective) or effective
            print(f"\n  Searching Discogs for barcode {normalized!r} ...")
            results = discogs_lookup.search_by_barcode(normalized)
            if not results:
                ctl.banner = "No results found."
                return Stay()
            return Push(ResultsScreen(results, "Discogs Results", "discogs"))
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
        """Source-specific apply tail (cp3a MB, cp3b Discogs, cp3c AcoustID,
        cp4 original-release)."""
        if self.source == "mb":
            self._apply_mb(ctl, selected)
        elif self.source == "discogs":
            self._apply_discogs(ctl, selected)
        elif self.source == "acoustid":
            self._apply_acoustid(ctl, selected)
        elif self.source == "original":
            self._apply_original(ctl, selected)

    def _apply_mb(self, ctl: MenuController, selected: DiscMeta) -> None:
        from cdda2img.mb_lookup import _merge_into_disc, _overwrite_disc, lookup_release
        from cdda2img.metadata_menu import _confirm_apply

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

    def _apply_discogs(self, ctl: MenuController, selected: DiscMeta) -> None:
        from cdda2img import discogs_lookup
        from cdda2img.mb_lookup import _merge_into_disc, _overwrite_disc
        from cdda2img.metadata_menu import _confirm_apply

        # Fetch the full release (track listing) BEFORE previewing, so the
        # confirm diff shows the real track count — the visible payoff of
        # "Trk on select". This brings Discogs to parity with the MB path
        # (_apply_mb); the prior confirm-before-fetch order only ever previewed
        # the stub. One fetch per pick — never a per-row fetch to fill the list.
        if selected.discogs_release_id and not selected.tracks:
            print("  Fetching full track listing from Discogs...")
            full = discogs_lookup.fetch_release(selected.discogs_release_id)
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
        ctl.banner = "Applied."

    def _apply_acoustid(self, ctl: MenuController, selected: DiscMeta) -> None:
        from cdda2img.mb_lookup import _merge_into_disc, _overwrite_disc, lookup_release
        from cdda2img.metadata_menu import _confirm_apply

        # AcoustID results are tagged with the track number before this frame
        # (see _acoustid_fingerprint). Fetch-full fires when the match is a
        # partial single-track stub (fewer tracks than the disc), and now runs
        # BEFORE the confirm so the preview shows the real track count (parity
        # with the MB and Discogs paths). No rg threading. One fetch per pick.
        if selected.mb_release_id and len(selected.tracks) < len(ctl.disc.tracks):
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
        ctl.banner = "Applied."

    def _apply_original(self, ctl: MenuController, selected: DiscMeta) -> None:
        # Original-release is not a whole-disc merge: it only sets the disc's
        # original_release_* fields. The confirm is the simpler [a]/[b] modal
        # (_confirm_original), not the update/overwrite _confirm_apply. Threads
        # the chosen release's MB rg id back, matching the legacy apply.
        from cdda2img.metadata_menu import _apply_selected_release, _confirm_original

        if not _confirm_original(selected):
            return
        new_rg = _apply_selected_release(ctl.disc, selected)
        ctl.mb_rg_id = new_rg or ctl.mb_rg_id
        ctl.banner = "Applied."


class AcoustidScreen(Screen):
    """AcoustID per-track fingerprint picker (cp3c).

    The "search" frame for AcoustID: renders the disc track list and, on a track
    number, resolves a WAV (pre-transcoded ``source_wavs`` or on-demand PCM
    extraction from ``source_pcm``), fingerprints it, and pushes a
    :class:`ResultsScreen` (``source="acoustid"``). [f] descends to
    :class:`AcoustidFileScreen`; [b] backs out. The results frame pops back here,
    which re-renders the list — the legacy track-picker loop.

    PCM mode lazily creates a ``TemporaryDirectory`` for extracted track WAVs and
    caches them by track number for the screen's lifetime. The directory is
    cleaned up by ``TemporaryDirectory``'s finalizer when the screen is popped and
    garbage-collected (prompt under CPython refcounting).
    """

    state = MenuState.ACOUSTID

    def __init__(
        self,
        *,
        source_wavs: list[Path] | None = None,
        source_pcm: Path | None = None,
    ) -> None:
        self.source_wavs = source_wavs
        self.source_pcm = source_pcm
        self._tmp: tempfile.TemporaryDirectory | None = None
        self._wav_cache: dict[int, Path] = {}

    def render(self, ctl: MenuController) -> None:
        from cdda2img.metadata_menu import _render_acoustid_tracklist

        _render_acoustid_tracklist(ctl.disc)
        print()
        if ctl.banner:
            print(f"  ! {ctl.banner}")
            print()
            ctl.banner = ""
        print("  Enter track number, [f] for file path, or [b] to return:")

    def _resolve_wav(self, ctl: MenuController, track_num: int) -> Path | None:
        """Resolve the WAV for *track_num*, setting a banner + returning None on
        failure. WAVs mode indexes ``source_wavs``; PCM mode extracts + caches."""
        from cdda2img.metadata_menu import _pcm_extract_track_wav

        if self.source_wavs is not None:
            idx = track_num - 1
            if idx >= len(self.source_wavs):
                ctl.banner = f"No WAV file for track {track_num}."
                return None
            wav_path = self.source_wavs[idx]
            if not wav_path.exists():
                ctl.banner = f"WAV file not found: {wav_path.name}"
                return None
            return wav_path
        if self.source_pcm is None:  # neither source set — nothing to extract
            return None
        if track_num in self._wav_cache:
            return self._wav_cache[track_num]
        if self._tmp is None:
            self._tmp = tempfile.TemporaryDirectory(prefix="cdda2img_aid_")
        out_path = Path(self._tmp.name) / f"track{track_num:02d}.wav"
        extracted = _pcm_extract_track_wav(
            ctl.disc, self.source_pcm, track_num, out_path
        )
        if not extracted:
            ctl.banner = f"Could not extract track {track_num}."
            return None
        self._wav_cache[track_num] = extracted
        return extracted

    def handle_input(self, ctl: MenuController) -> Nav:
        from cdda2img.metadata_menu import _acoustid_fingerprint, _prompt

        valid_nums = {t.track_number for t in ctl.disc.tracks}
        choice = _prompt("  > ").strip().lower()
        if choice == "b":
            return Pop()
        if choice == "f":
            return Push(AcoustidFileScreen())
        if not choice.isdigit() or int(choice) not in valid_nums:
            ctl.banner = "Invalid selection."
            return Stay()
        track_num = int(choice)
        wav_path = self._resolve_wav(ctl, track_num)
        if wav_path is None:
            return Stay()  # banner set by _resolve_wav
        results = _acoustid_fingerprint(wav_path, track_number=track_num)
        if not results:
            ctl.banner = "No confident matches found (check fpcalc / ACOUSTID_API_KEY)."
            return Stay()
        return Push(ResultsScreen(results, "AcoustID Matches", "acoustid"))


class AcoustidFileScreen(Screen):
    """AcoustID fingerprint from an arbitrary audio file path (cp3c).

    The file-path entry frame: prompts for a path (blank pops back) and an
    optional track number, fingerprints, and pushes a :class:`ResultsScreen`.
    Used both as the file-only mode (no PCM/WAVs available) and the [f] descent
    from :class:`AcoustidScreen`.
    """

    state = MenuState.ACOUSTID

    def render(self, ctl: MenuController) -> None:
        from cdda2img.metadata_menu import _header

        _header("AcoustID Fingerprint")
        if ctl.banner:
            print(f"  ! {ctl.banner}")
            print()
            ctl.banner = ""

    def handle_input(self, ctl: MenuController) -> Nav:
        from cdda2img.metadata_menu import _acoustid_fingerprint, _prompt

        path_str = _prompt("  Audio file path (or Enter to return): ").strip()
        if not path_str:
            return Pop()
        wav_path = Path(path_str)
        if not wav_path.exists():
            ctl.banner = f"File not found: {path_str}"
            return Stay()
        num_str = _prompt("  Track number (or Enter to skip): ").strip()
        track_num = int(num_str) if num_str.isdigit() else None
        results = _acoustid_fingerprint(wav_path, track_number=track_num)
        if not results:
            ctl.banner = "No confident matches found (check fpcalc / ACOUSTID_API_KEY)."
            return Stay()
        return Push(ResultsScreen(results, "AcoustID Matches", "acoustid"))


class OriginalReleaseScreen(Screen):
    """Find-original-release hub (cp4). Native port of ``_original_release_menu``.

    A persistent hub, mirroring :class:`EditScreen`: every action returns here
    (``Stay``) re-rendering the live "Current:" line, and ``[b]`` is the single
    exit to MAIN. ``[m]`` (set manually) and ``[c]`` (clear) are inline mutations
    — bounded blocking modals run in ``handle_input``, then ``Stay`` + banner.
    ``[s]`` fetches MusicBrainz releases (by threaded rg id, else a prompted text
    search — both inside ``_fetch_releases_for_group``) and pushes a paginated
    :class:`ResultsScreen` with ``source="original"``; its apply tail
    (``_apply_original``) pops back here, so the applied original shows in the
    re-rendered "Current:" line.

    Deviation from legacy (noted): the procedural ``_original_release_menu``
    exited to MAIN after a manual-set / clear / apply. The native hub stays put
    for consistency with EditScreen and the cp3 apply-destination convention; the
    user leaves with ``[b]``.
    """

    state = MenuState.ORIGINAL_RELEASE

    def render(self, ctl: MenuController) -> None:
        from cdda2img.metadata_menu import _header

        _header("Find Original Release")
        d = ctl.disc
        if d.original_release_found and d.original_release_title:
            year = f" ({d.original_release_year})" if d.original_release_year else ""
            print(f"  Current: {d.original_release_title}{year}")
        else:
            print("  Current: (none set)")
        print()
        if ctl.banner:
            print(f"  ! {ctl.banner}")
            print()
            ctl.banner = ""
        print("  [s]  Search MusicBrainz")
        print("  [m]  Set manually")
        print("  [c]  Clear")
        print("  [b]  Back")

    def handle_input(self, ctl: MenuController) -> Nav:
        from cdda2img.metadata_menu import (
            _fetch_releases_for_group,
            _prompt,
            _set_original_manually,
        )

        choice = _prompt("  > ").strip().lower()
        if choice in ("b", "q", ""):
            return Pop()
        if choice == "m":
            # Derive the banner from post-call state: _set_original_manually
            # may set OR clear (blank title), and its own inline prints are
            # wiped by the TUI screen-clear, so the banner must not lie.
            ctl.disc = _set_original_manually(ctl.disc)
            ctl.banner = (
                f"Original release set: {ctl.disc.original_release_title}."
                if ctl.disc.original_release_found
                else "Original release cleared."
            )
            return Stay()
        if choice == "c":
            ctl.disc.original_release_found = False
            ctl.disc.original_release_title = None
            ctl.disc.original_release_year = None
            ctl.banner = "Cleared."
            return Stay()
        if choice != "s":
            ctl.banner = "Enter s, m, c, or b."
            return Stay()
        releases, _ = _fetch_releases_for_group(ctl.disc, ctl.mb_rg_id)
        if not releases:
            ctl.banner = "No results found."
            return Stay()
        # Earliest-first so the original pressing leads (legacy parity).
        releases_sorted = sorted(releases, key=lambda m: m.release_date or "9999")
        return Push(
            ResultsScreen(
                releases_sorted, "Original Release - Earliest First", "original"
            )
        )


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
        auto_apply: bool = False,
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
        # When True (STRONG recommendation), skip the interactive prompt and
        # return the disc as-is. The caller is responsible for printing a brief
        # confirmation line before invoking the menu.
        self.auto_apply = auto_apply
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
        """Drive the stack until accepted. Returns the final disc.

        In TUI mode the whole menu runs on the alternate screen buffer so the
        per-frame clear+redraw never leaks header copies into the main
        scrollback; the main buffer is restored on exit (including on an
        exception, via ``finally``).
        """
        if not sys.stdin.isatty() or self.auto_apply:
            return self.disc
        if self.tui:
            _enter_fullscreen()
        try:
            while not self.done and self.stack:
                self._step()
        finally:
            if self.tui:
                _exit_fullscreen()
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
