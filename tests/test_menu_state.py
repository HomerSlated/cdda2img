"""
test_menu_state.py — MenuController state-transition tests.

The state machine is verified by driving the controller through fixed
input sequences and checking the resulting state / disc / banner.
``input()`` is patched via the underlying ``metadata_menu._prompt`` so
that test fixtures don't depend on a real TTY. Screen-clear is
patched out to keep test output readable.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from cdda2img.menu_state import (
    LegacyDelegateScreen,
    MenuController,
    MenuState,
)
from cdda2img.rbi_format import RBIDisc, RBITocEntry


def _disc(album: str = "Album", artist: str = "Artist") -> RBIDisc:
    """Build a minimal RBIDisc for menu fixtures."""
    return RBIDisc(
        album=album,
        artist=artist,
        tracks=[
            RBITocEntry(
                track_number=1,
                title="Track 1",
                performer=artist,
                start_frame=0,
                duration_frames=18000,
            )
        ],
    )


# ---------------------------------------------------------------------------
# Construction / initial state
# ---------------------------------------------------------------------------


def test_initial_state_is_main_when_no_ar_summary() -> None:
    ctl = MenuController(_disc())
    assert ctl.state is MenuState.MAIN


def test_initial_state_is_ar_pause_when_ar_summary_provided() -> None:
    ctl = MenuController(_disc(), ar_summary="AR text here")
    assert ctl.state is MenuState.AR_PAUSE


def test_seed_search_fields_anchored_to_initial_disc() -> None:
    """seed_artist/seed_title don't drift when disc.album/artist changes later."""
    ctl = MenuController(_disc(album="Initial Album", artist="Initial Artist"))
    ctl.disc.album = "Edited Album"
    ctl.disc.artist = "Edited Artist"
    assert ctl.seed_title == "Initial Album"
    assert ctl.seed_artist == "Initial Artist"


def test_undo_savepoint_is_deep_copied() -> None:
    """Editing disc.tracks must not mutate the undo savepoint."""
    ctl = MenuController(_disc())
    ctl.disc.tracks[0].title = "Edited Title"
    assert ctl._original_disc.tracks[0].title == "Track 1"


# ---------------------------------------------------------------------------
# Run with non-TTY → no-op
# ---------------------------------------------------------------------------


def test_run_returns_disc_unchanged_when_not_a_tty() -> None:
    disc = _disc()
    ctl = MenuController(disc)
    with patch("cdda2img.menu_state.sys.stdin.isatty", return_value=False):
        result = ctl.run()
    assert result is disc
    # State should never have left its initial value.
    assert ctl.state is MenuState.MAIN


# ---------------------------------------------------------------------------
# AR_PAUSE → MAIN transition
# ---------------------------------------------------------------------------


def test_ar_pause_transitions_to_main_on_keypress() -> None:
    """Any input at AR_PAUSE transitions to MAIN (current contract)."""
    ctl = MenuController(_disc(), ar_summary="AR")
    # Simulate stdin.isatty=True and a single Enter at the AR_PAUSE prompt,
    # then 'a' at MAIN to accept and exit.
    with (
        patch("cdda2img.menu_state.sys.stdin.isatty", return_value=True),
        patch("cdda2img.menu_state._clear_screen"),
        patch("cdda2img.metadata_menu._prompt", side_effect=["", "a"]),
    ):
        ctl.run()
    assert ctl.state is MenuState.DONE


# ---------------------------------------------------------------------------
# MAIN dispatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,expected_next",
    [
        ("a", MenuState.DONE),
        ("f", MenuState.FETCH),
        ("e", MenuState.EDIT),
        ("r", MenuState.ORIGINAL_RELEASE),
    ],
)
def test_main_choice_transitions_to_expected_state(
    key: str, expected_next: MenuState
) -> None:
    """One input event from MAIN drives the stack to the right target screen."""
    ctl = MenuController(_disc())
    with patch("cdda2img.metadata_menu._prompt", return_value=key):
        ctl._apply(ctl.stack[-1].handle_input(ctl))
    assert ctl.state is expected_next


def test_main_undo_resets_disc_and_clears_mb_rg_id() -> None:
    """Choice 'u' (undo) restores disc from the savepoint and clears mb_rg_id."""
    ctl = MenuController(_disc())
    ctl.disc.album = "Edited"
    ctl.mb_rg_id = "some-rg-id"
    with patch("cdda2img.metadata_menu._prompt", return_value="u"):
        ctl._apply(ctl.stack[-1].handle_input(ctl))
    assert ctl.disc.album == "Album"  # restored from savepoint
    assert ctl.mb_rg_id is None
    assert "Reset" in ctl.banner


def test_main_clear_metadata_resets_fields() -> None:
    ctl = MenuController(_disc(album="Filled", artist="Also Filled"))
    with patch("cdda2img.metadata_menu._prompt", return_value="c"):
        ctl._apply(ctl.stack[-1].handle_input(ctl))
    # _clear_disc blanks album/artist/MCN — verify a representative field.
    assert ctl.disc.album == ""
    assert "cleared" in ctl.banner.lower()


def test_main_unknown_command_sets_banner_and_stays() -> None:
    ctl = MenuController(_disc())
    with patch("cdda2img.metadata_menu._prompt", return_value="zzz"):
        ctl._apply(ctl.stack[-1].handle_input(ctl))
    assert ctl.state is MenuState.MAIN
    assert "Unknown" in ctl.banner


def test_banner_clears_on_next_render() -> None:
    ctl = MenuController(_disc())
    ctl.banner = "stale"
    ctl.stack[-1].render(ctl)
    assert ctl.banner == ""


# ---------------------------------------------------------------------------
# EDIT / FETCH / ORIGINAL_RELEASE delegate states
# ---------------------------------------------------------------------------


def test_edit_state_returns_to_main() -> None:
    """The EDIT delegate screen runs _edit_menu, then pops back to MAIN."""
    ctl = MenuController(_disc())
    ctl.stack.append(LegacyDelegateScreen(MenuState.EDIT))
    fake_edited = _disc(album="Edited via _edit_menu")
    with patch("cdda2img.metadata_menu._edit_menu", return_value=fake_edited):
        ctl._apply(ctl.stack[-1].handle_input(ctl))
    assert ctl.state is MenuState.MAIN
    assert ctl.disc.album == "Edited via _edit_menu"


def test_fetch_state_returns_to_main_and_threads_rg_id() -> None:
    ctl = MenuController(_disc())
    ctl.stack.append(LegacyDelegateScreen(MenuState.FETCH))
    fake_edited = _disc(album="Fetched")
    with patch(
        "cdda2img.metadata_menu._fetch_menu",
        return_value=(fake_edited, "new-rg-id"),
    ):
        ctl._apply(ctl.stack[-1].handle_input(ctl))
    assert ctl.state is MenuState.MAIN
    assert ctl.disc.album == "Fetched"
    assert ctl.mb_rg_id == "new-rg-id"


def test_original_release_state_returns_to_main_and_threads_rg_id() -> None:
    ctl = MenuController(_disc())
    ctl.stack.append(LegacyDelegateScreen(MenuState.ORIGINAL_RELEASE))
    fake_edited = _disc(album="With Original Release")
    with patch(
        "cdda2img.metadata_menu._original_release_menu",
        return_value=(fake_edited, "rg-x"),
    ):
        ctl._apply(ctl.stack[-1].handle_input(ctl))
    assert ctl.state is MenuState.MAIN
    assert ctl.mb_rg_id == "rg-x"


# ---------------------------------------------------------------------------
# Format-AR helper feeds AR_PAUSE
# ---------------------------------------------------------------------------


def test_format_ar_report_returns_string() -> None:
    """format_ar_report returns a string usable as ar_summary."""
    from cdda2img.accuraterip import ARTrackResult, format_ar_report

    results = [
        ARTrackResult(
            track=1,
            v1_crc="deadbeef",
            v2_crc="cafebabe",
            confidence_v1=14,
            confidence_v2=None,
            max_confidence=136,
        )
    ]
    text = format_ar_report(results, read_offset=30)
    assert isinstance(text, str)
    assert "AccurateRip:" in text
    assert "Track  1" in text


def test_format_ar_report_handles_disc_not_in_database() -> None:
    from cdda2img.accuraterip import ARTrackResult, format_ar_report

    results = [
        ARTrackResult(
            track=1,
            v1_crc="00000000",
            v2_crc="00000000",
            confidence_v1=None,
            confidence_v2=None,
            max_confidence=None,
        )
    ]
    text = format_ar_report(results)
    assert "not found" in text


def test_format_ar_report_empty_returns_empty_string() -> None:
    from cdda2img.accuraterip import format_ar_report

    assert format_ar_report([]) == ""


def test_format_ar_report_shows_both_v1_and_v2_confidence() -> None:
    # Regression: the old `if v1 … elif v2` chain hid v2's (usually higher)
    # confidence whenever both matched — the normal success case. Both must show.
    from cdda2img.accuraterip import ARTrackResult, format_ar_report

    results = [
        ARTrackResult(
            track=1,
            v1_crc="76e30f97",
            v2_crc="ad4a33e8",
            confidence_v1=57,
            confidence_v2=113,
            max_confidence=146,
        )
    ]
    text = format_ar_report(results)
    assert "v1=76e30f97 [57]" in text
    assert "v2=ad4a33e8 [113]" in text
    assert "OK" in text


def test_format_ar_report_min_confidence_uses_best_per_track() -> None:
    # Footer floor-of-trust = the weakest track's *stronger* variant (max of
    # v1/v2 per track, then min across tracks) — not v1-first as before.
    from cdda2img.accuraterip import ARTrackResult, format_ar_report

    results = [
        ARTrackResult(
            track=1,
            v1_crc="aaaaaaaa",
            v2_crc="bbbbbbbb",
            confidence_v1=57,
            confidence_v2=113,
            max_confidence=146,
        ),
        ARTrackResult(
            track=2,
            v1_crc="cccccccc",
            v2_crc="dddddddd",
            confidence_v1=44,
            confidence_v2=90,
            max_confidence=146,
        ),
    ]
    text = format_ar_report(results)
    assert "2/2 tracks verified (min confidence 90)" in text


def test_format_ar_report_partial_mismatch_row_and_footer() -> None:
    from cdda2img.accuraterip import ARTrackResult, format_ar_report

    results = [
        ARTrackResult(
            track=1,
            v1_crc="aaaaaaaa",
            v2_crc="bbbbbbbb",
            confidence_v1=57,
            confidence_v2=113,
            max_confidence=146,
        ),
        ARTrackResult(
            track=2,
            v1_crc="cccccccc",
            v2_crc="dddddddd",
            confidence_v1=None,
            confidence_v2=None,
            max_confidence=146,
        ),
    ]
    text = format_ar_report(results)
    assert "[113]" in text  # the matched track still shows v2 confidence
    assert "MISMATCH (max 146)" in text
    assert "1/2 tracks verified (1 mismatch)" in text


# ---------------------------------------------------------------------------
# --no-tui: screen-clear gating
# ---------------------------------------------------------------------------


def test_no_tui_skips_screen_clear() -> None:
    """tui=False (--no-tui): the menu renders without clearing the screen, so
    earlier pipeline output stays in the terminal scrollback."""
    ctl = MenuController(_disc(), tui=False)
    with (
        patch("cdda2img.menu_state.sys.stdin.isatty", return_value=True),
        patch("cdda2img.menu_state._clear_screen") as clear,
        patch("cdda2img.metadata_menu._prompt", return_value="a"),
    ):
        ctl.run()
    clear.assert_not_called()


def test_tui_clears_screen_by_default() -> None:
    """tui=True (default): each frame clears + redraws (fixed-position UX)."""
    ctl = MenuController(_disc(), tui=True)
    with (
        patch("cdda2img.menu_state.sys.stdin.isatty", return_value=True),
        patch("cdda2img.menu_state._clear_screen") as clear,
        patch("cdda2img.metadata_menu._prompt", return_value="a"),
    ):
        ctl.run()
    clear.assert_called()
