"""
test_menu_state.py — MenuController state-transition tests.

The state machine is verified by driving the controller through fixed
input sequences and checking the resulting state / disc / banner.
``input()`` is patched via the underlying ``metadata_menu._prompt`` so
that test fixtures don't depend on a real TTY. Screen-clear is
patched out to keep test output readable.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from cdda2img.lookup_result import DiscMeta, TrackMeta
from cdda2img.menu_state import (
    AcoustidFileScreen,
    AcoustidScreen,
    DiscogsSearchScreen,
    EditDiscPositionScreen,
    EditScreen,
    EditTrackScreen,
    FetchScreen,
    MBSearchScreen,
    MenuController,
    MenuState,
    OriginalReleaseScreen,
    PressingScreen,
    ResultsScreen,
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


def _multitrack_disc(n: int = 3) -> RBIDisc:
    """RBIDisc with *n* tracks numbered 1..n, for track-edit fixtures."""
    return RBIDisc(
        album="Album",
        artist="Artist",
        tracks=[
            RBITocEntry(
                track_number=i,
                title=f"Track {i}",
                performer="Artist",
                start_frame=(i - 1) * 18000,
                duration_frames=18000,
            )
            for i in range(1, n + 1)
        ],
    )


def _step_with(ctl: MenuController, *inputs: str) -> None:
    """Drive one handle_input step on the top screen, patching _prompt.

    Each positional value answers one ``_prompt`` call in order (a screen step
    may prompt once for the choice and again inside ``_prompt_edit``, or twice
    for the two fields EditDiscPositionScreen reads). A single-element list is
    just as valid as a multi-element one for ``side_effect``.
    """
    with patch("cdda2img.metadata_menu._prompt", side_effect=list(inputs)):
        ctl._apply(ctl.stack[-1].handle_input(ctl))


# ---------------------------------------------------------------------------
# Construction / initial state
# ---------------------------------------------------------------------------


def test_initial_state_is_main_when_no_ar_summary() -> None:
    ctl = MenuController(_disc())
    assert ctl.state is MenuState.MAIN


def test_initial_state_is_main_when_ar_summary_provided() -> None:
    """ar_summary is passed through but no longer causes an AR_PAUSE screen."""
    ctl = MenuController(_disc(), ar_summary="AR text here")
    assert ctl.state is MenuState.MAIN


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
# ar_summary param is accepted but no longer drives an AR_PAUSE screen
# ---------------------------------------------------------------------------


def test_ar_summary_param_does_not_create_ar_pause_screen() -> None:
    """ar_summary is stored on the controller but does not push an extra screen."""
    ctl = MenuController(_disc(), ar_summary="AR")
    assert ctl.ar_summary == "AR"
    assert ctl.state is MenuState.MAIN


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
# EDIT / FETCH back-navigation (all native screens now)
# ---------------------------------------------------------------------------


def test_edit_back_returns_to_main() -> None:
    """EditScreen 'b' pops back to MAIN."""
    ctl = MenuController(_disc())
    ctl.stack.append(EditScreen())
    _step_with(ctl, "b")
    assert ctl.state is MenuState.MAIN


def test_main_fetch_pushes_native_fetch_screen() -> None:
    """MAIN [f] now pushes the native FetchScreen (cp3a), not a legacy delegate."""
    ctl = MenuController(_disc())
    with patch("cdda2img.metadata_menu._prompt", return_value="f"):
        ctl._apply(ctl.stack[-1].handle_input(ctl))
    assert isinstance(ctl.stack[-1], FetchScreen)
    assert ctl.state is MenuState.FETCH


def test_fetch_screen_back_returns_to_main() -> None:
    ctl = MenuController(_disc())
    ctl.stack.append(FetchScreen())
    _step_with(ctl, "b")
    assert ctl.state is MenuState.MAIN


def test_fetch_screen_m_pushes_mb_search_seeded_from_disc() -> None:
    """[m] pushes MBSearchScreen, seeded from the disc's artist/title."""
    ctl = MenuController(_disc(album="Seed Album", artist="Seed Artist"))
    ctl.stack.append(FetchScreen())
    _step_with(ctl, "m")
    top = ctl.stack[-1]
    assert isinstance(top, MBSearchScreen)
    assert top.artist_q == "Seed Artist"
    assert top.title_q == "Seed Album"


def test_fetch_screen_d_pushes_native_discogs_seeded_from_disc() -> None:
    """[d] pushes the native DiscogsSearchScreen (cp3b), seeded from the disc."""
    ctl = MenuController(_disc(album="Seed Album", artist="Seed Artist"))
    ctl.stack.append(FetchScreen())
    _step_with(ctl, "d")
    top = ctl.stack[-1]
    assert isinstance(top, DiscogsSearchScreen)
    assert top.artist_q == "Seed Artist"
    assert top.title_q == "Seed Album"


def test_fetch_screen_a_unavailable_sets_banner_and_stays() -> None:
    ctl = MenuController(_disc())
    ctl.stack.append(FetchScreen())
    with (
        patch("cdda2img.acoustid_lookup.is_available", return_value=False),
        patch(
            "cdda2img.acoustid_lookup.unavailability_reason",
            return_value="fpcalc not found",
        ),
    ):
        _step_with(ctl, "a")
    assert ctl.state is MenuState.FETCH
    assert "not available" in ctl.banner.lower()
    assert "fpcalc not found" in ctl.banner


def test_fetch_screen_a_with_wavs_pushes_track_picker() -> None:
    ctl = MenuController(_disc(), source_wavs=[Path("/fake/t1.wav")])
    ctl.stack.append(FetchScreen())
    with patch("cdda2img.acoustid_lookup.is_available", return_value=True):
        _step_with(ctl, "a")
    top = ctl.stack[-1]
    assert isinstance(top, AcoustidScreen)
    assert top.source_wavs == [Path("/fake/t1.wav")]


def test_fetch_screen_a_no_sources_pushes_file_screen() -> None:
    ctl = MenuController(_disc())  # no source_wavs / source_pcm
    ctl.stack.append(FetchScreen())
    with patch("cdda2img.acoustid_lookup.is_available", return_value=True):
        _step_with(ctl, "a")
    assert isinstance(ctl.stack[-1], AcoustidFileScreen)


def test_mb_search_back_pops_to_fetch() -> None:
    ctl = MenuController(_disc())
    ctl.stack.append(FetchScreen())
    ctl.stack.append(MBSearchScreen())
    _step_with(ctl, "b")
    assert ctl.state is MenuState.FETCH


def test_mb_search_no_results_sets_banner_and_stays() -> None:
    ctl = MenuController(_disc())
    ctl.stack.append(MBSearchScreen(artist_q="A", title_q="T"))
    with (
        patch("cdda2img.mb_lookup.search_releases", return_value=[]),
        patch("cdda2img.mb_lookup.build_mb_search_query", return_value="q"),
    ):
        _step_with(ctl, "s")
    assert ctl.state is MenuState.MB_SEARCH
    assert "No results" in ctl.banner


def test_mb_search_query_does_not_drift_after_edit() -> None:
    """The query is instance state seeded at entry; [e] mutates it, and it does
    not silently track a later disc.album change (Trap #3)."""
    ctl = MenuController(_disc(album="Orig", artist="OrigArt"))
    screen = MBSearchScreen(artist_q="OrigArt", title_q="Orig")
    ctl.stack.append(screen)
    with patch(
        "cdda2img.metadata_menu._prompt_search_fields",
        return_value=("NewArt", "NewTitle"),
    ):
        _step_with(ctl, "e")
    # disc changes underneath should not move the query.
    ctl.disc.album = "Something Else"
    assert (screen.artist_q, screen.title_q) == ("NewArt", "NewTitle")


def test_mb_search_s_pushes_results_sorted_earliest_first() -> None:
    """[s] pushes a ResultsScreen with results sorted earliest-release first —
    the legacy _mb_select_and_apply presentation order."""
    ctl = MenuController(_disc())
    ctl.stack.append(MBSearchScreen(artist_q="A", title_q="T"))
    stub84 = DiscMeta(album="E", release_date="1984", mb_release_id="r84")
    stub83 = DiscMeta(album="E", release_date="1983-03-23", mb_release_id="r83")
    with (
        patch("cdda2img.mb_lookup.search_releases", return_value=[stub84, stub83]),
        patch("cdda2img.mb_lookup.build_mb_search_query", return_value="q"),
    ):
        _step_with(ctl, "s")
    top = ctl.stack[-1]
    assert isinstance(top, ResultsScreen)
    assert top.source == "mb"
    assert [m.mb_release_id for m in top.results] == ["r83", "r84"]


def test_results_pagination_and_back() -> None:
    results = [DiscMeta(album=f"R{i}", mb_release_id=str(i)) for i in range(15)]
    ctl = MenuController(_disc())
    screen = ResultsScreen(results, "MusicBrainz Results", "mb")
    ctl.stack.append(screen)
    _step_with(ctl, "n")
    assert screen.page == 1
    _step_with(ctl, "p")
    assert screen.page == 0
    _step_with(ctl, "p")  # already at first page — clamps, no error
    assert screen.page == 0
    _step_with(ctl, "b")
    assert ctl.state is MenuState.MAIN  # popped (MainScreen underneath)


def test_results_invalid_selection_sets_banner_and_stays() -> None:
    results = [DiscMeta(album="R0", mb_release_id="0")]
    ctl = MenuController(_disc())
    ctl.stack.append(ResultsScreen(results, "MusicBrainz Results", "mb"))
    _step_with(ctl, "99")
    assert ctl.state is MenuState.RESULTS
    assert "Invalid" in ctl.banner


def test_results_mb_select_fetches_full_before_preview_and_threads_rg() -> None:
    """The migrated _mb_select_and_apply contract: on select, the full release
    (with ISRCs) is fetched BEFORE the preview, applied, mb_rg_id threaded, and
    the screen pops back to the search frame with an 'Applied.' banner."""
    disc = _disc()  # track 1, blank ISRC
    ctl = MenuController(disc)
    ctl.stack.append(MBSearchScreen())  # the frame we pop back to
    stub83 = DiscMeta(album="E", release_date="1983-03-23", mb_release_id="r83")
    full83 = DiscMeta(
        album="E",
        release_date="1983-03-23",
        mb_release_id="r83",
        mb_release_group_id="rg-e",
        tracks=[TrackMeta(number=1, isrc="USRHD0709703")],
    )
    ctl.stack.append(ResultsScreen([stub83], "MusicBrainz Results", "mb"))
    captured: dict = {}

    def fake_confirm(meta, _disc):
        captured["preview_isrcs"] = [t.isrc for t in meta.tracks]
        return "update"

    with (
        patch("cdda2img.metadata_menu._confirm_apply", side_effect=fake_confirm),
        patch("cdda2img.mb_lookup.lookup_release", return_value=full83),
    ):
        _step_with(ctl, "1")  # select result 1

    assert captured["preview_isrcs"] == ["USRHD0709703"]  # full meta reached preview
    assert ctl.disc.tracks[0].isrc == "USRHD0709703"  # and was applied
    assert ctl.mb_rg_id == "rg-e"  # release-group threaded
    assert ctl.state is MenuState.MB_SEARCH  # popped back to search
    assert ctl.banner == "Applied."


def test_results_mb_select_cancel_applies_nothing() -> None:
    disc = _disc()
    ctl = MenuController(disc)
    ctl.stack.append(MBSearchScreen())
    stub = DiscMeta(album="E", mb_release_id="r", mb_release_group_id="rg")
    ctl.stack.append(ResultsScreen([stub], "MusicBrainz Results", "mb"))
    with (
        patch("cdda2img.metadata_menu._confirm_apply", return_value=None),
        patch("cdda2img.mb_lookup.lookup_release", return_value=stub),
    ):
        _step_with(ctl, "1")
    assert ctl.mb_rg_id is None  # nothing applied
    assert ctl.banner == ""  # no 'Applied.' banner on cancel
    assert ctl.state is MenuState.MB_SEARCH  # still popped back


# ---------------------------------------------------------------------------
# cp3b — Discogs native screens
# ---------------------------------------------------------------------------


def test_discogs_unavailable_renders_help_and_pops_on_any_key() -> None:
    """No DISCOGS_TOKEN → token help, pop on Enter (legacy guard preserved)."""
    ctl = MenuController(_disc())
    ctl.stack.append(FetchScreen())
    ctl.stack.append(DiscogsSearchScreen())
    with patch("cdda2img.discogs_lookup.is_available", return_value=False):
        _step_with(ctl, "")  # any key returns
    assert ctl.state is MenuState.FETCH  # popped back


def test_discogs_back_pops_to_fetch() -> None:
    ctl = MenuController(_disc())
    ctl.stack.append(FetchScreen())
    ctl.stack.append(DiscogsSearchScreen())
    with patch("cdda2img.discogs_lookup.is_available", return_value=True):
        _step_with(ctl, "b")
    assert ctl.state is MenuState.FETCH


def test_discogs_search_pushes_results_unsorted() -> None:
    """[s] pushes ResultsScreen(source='discogs') with results in API order
    (Discogs is not sorted, unlike MB)."""
    ctl = MenuController(_disc())
    ctl.stack.append(DiscogsSearchScreen(artist_q="A", title_q="T"))
    r1 = DiscMeta(album="Z", release_date="1990", discogs_release_id=1)
    r2 = DiscMeta(album="A", release_date="1980", discogs_release_id=2)
    with (
        patch("cdda2img.discogs_lookup.is_available", return_value=True),
        patch("cdda2img.discogs_lookup.search_releases", return_value=[r1, r2]),
    ):
        _step_with(ctl, "s")
    top = ctl.stack[-1]
    assert isinstance(top, ResultsScreen)
    assert top.source == "discogs"
    assert [m.discogs_release_id for m in top.results] == [1, 2]  # unsorted


def test_discogs_no_results_sets_banner_and_stays() -> None:
    ctl = MenuController(_disc())
    ctl.stack.append(DiscogsSearchScreen(artist_q="A", title_q="T"))
    with (
        patch("cdda2img.discogs_lookup.is_available", return_value=True),
        patch("cdda2img.discogs_lookup.search_releases", return_value=[]),
    ):
        _step_with(ctl, "s")
    assert ctl.state is MenuState.DISCOGS
    assert "No results" in ctl.banner


def test_discogs_select_fetches_full_before_confirm_then_applies() -> None:
    """Discogs now fetches the full release BEFORE confirming (parity with MB),
    so the full track listing reaches the preview — the visible payoff of
    "Trk on select". The full meta is applied. No mb_rg_id threading."""
    ctl = MenuController(_disc())
    ctl.stack.append(DiscogsSearchScreen())  # frame popped back to
    stub = DiscMeta(album="E", discogs_release_id=42)  # no tracks
    full = DiscMeta(
        album="E",
        discogs_release_id=42,
        mb_release_group_id="should-not-thread",
        tracks=[TrackMeta(number=1, isrc="USRHD0709703")],
    )
    ctl.stack.append(ResultsScreen([stub], "Discogs Results", "discogs"))
    captured: dict = {}

    def fake_confirm(meta, _disc):
        captured["preview_tracks"] = list(meta.tracks)  # full → 1 track
        return "update"

    with (
        patch("cdda2img.metadata_menu._confirm_apply", side_effect=fake_confirm),
        patch("cdda2img.discogs_lookup.fetch_release", return_value=full),
    ):
        _step_with(ctl, "1")
    # confirmed on the FULL release (fetched first), not the stub
    assert [t.isrc for t in captured["preview_tracks"]] == ["USRHD0709703"]
    assert ctl.disc.tracks[0].isrc == "USRHD0709703"  # full meta applied
    assert ctl.mb_rg_id is None  # Discogs never threads the MB rg
    assert ctl.banner == "Applied."
    assert ctl.state is MenuState.DISCOGS  # popped back to search


# ---------------------------------------------------------------------------
# cp3c — AcoustID native screens
# ---------------------------------------------------------------------------


def test_acoustid_track_picker_back_pops_to_fetch() -> None:
    ctl = MenuController(_multitrack_disc(3), source_wavs=[Path("/x")])
    ctl.stack.append(FetchScreen())
    ctl.stack.append(AcoustidScreen(source_wavs=[Path("/x")]))
    _step_with(ctl, "b")
    assert ctl.state is MenuState.FETCH


def test_acoustid_track_picker_f_pushes_file_screen() -> None:
    ctl = MenuController(_multitrack_disc(3), source_wavs=[Path("/x")])
    ctl.stack.append(AcoustidScreen(source_wavs=[Path("/x")]))
    _step_with(ctl, "f")
    assert isinstance(ctl.stack[-1], AcoustidFileScreen)


def test_acoustid_track_picker_invalid_track_banner() -> None:
    ctl = MenuController(_multitrack_disc(3), source_wavs=[Path("/x")])
    ctl.stack.append(AcoustidScreen(source_wavs=[Path("/x")]))
    _step_with(ctl, "99")  # not a valid track number
    assert ctl.state is MenuState.ACOUSTID
    assert "Invalid" in ctl.banner


def test_acoustid_track_pick_fingerprints_and_pushes_results() -> None:
    """Picking a track resolves its WAV, fingerprints, tags single-track results
    with the track number, and pushes a ResultsScreen(source='acoustid')."""
    wav = Path("/fake/track01.wav")
    ctl = MenuController(_multitrack_disc(3), source_wavs=[wav, wav, wav])
    ctl.stack.append(AcoustidScreen(source_wavs=[wav, wav, wav]))
    match = DiscMeta(
        album="Found", tracks=[TrackMeta(number=None, isrc="USRHD0709703")]
    )
    with (
        patch("pathlib.Path.exists", return_value=True),
        patch("cdda2img.acoustid_lookup.fingerprint_and_lookup", return_value=[match]),
    ):
        _step_with(ctl, "1")
    top = ctl.stack[-1]
    assert isinstance(top, ResultsScreen)
    assert top.source == "acoustid"
    # single-track result tagged with the picked track number (1) before the frame
    assert top.results[0].tracks[0].number == 1


def test_acoustid_no_matches_sets_banner_and_stays() -> None:
    wav = Path("/fake/t.wav")
    ctl = MenuController(_multitrack_disc(2), source_wavs=[wav, wav])
    ctl.stack.append(AcoustidScreen(source_wavs=[wav, wav]))
    with (
        patch("pathlib.Path.exists", return_value=True),
        patch("cdda2img.acoustid_lookup.fingerprint_and_lookup", return_value=[]),
    ):
        _step_with(ctl, "1")
    assert ctl.state is MenuState.ACOUSTID
    assert "No confident matches" in ctl.banner


def test_acoustid_file_screen_blank_path_pops() -> None:
    ctl = MenuController(_disc())
    ctl.stack.append(FetchScreen())
    ctl.stack.append(AcoustidFileScreen())
    _step_with(ctl, "")  # blank path returns
    assert ctl.state is MenuState.FETCH


def test_acoustid_file_screen_missing_file_banner() -> None:
    ctl = MenuController(_disc())
    ctl.stack.append(AcoustidFileScreen())
    with patch("pathlib.Path.exists", return_value=False):
        _step_with(ctl, "/no/such/file.wav")
    assert ctl.state is MenuState.ACOUSTID
    assert "not found" in ctl.banner.lower()


def test_acoustid_select_fetches_full_before_confirm_when_partial() -> None:
    """AcoustID apply tail: fetch-full now runs BEFORE confirm (parity with MB
    and Discogs), so the preview shows the full track listing. Fetch-full fires
    only when the match has fewer tracks than the disc (partial single-track
    stub)."""
    ctl = MenuController(_multitrack_disc(3))
    ctl.stack.append(AcoustidScreen(source_wavs=[Path("/x")]))  # frame popped to
    stub = DiscMeta(
        album="E",
        mb_release_id="r",
        tracks=[TrackMeta(number=1, isrc="AAA000000001")],  # 1 < 3 disc tracks
    )
    full = DiscMeta(
        album="E",
        mb_release_id="r",
        mb_release_group_id="rg",
        tracks=[TrackMeta(number=i, isrc=f"AAA00000000{i}") for i in (1, 2, 3)],
    )
    ctl.stack.append(ResultsScreen([stub], "AcoustID Matches", "acoustid"))
    captured: dict = {}

    def fake_confirm(meta, _disc):
        captured["preview_n"] = len(meta.tracks)  # full → 3 tracks, pre-confirm
        return "update"

    with (
        patch("cdda2img.metadata_menu._confirm_apply", side_effect=fake_confirm),
        patch("cdda2img.mb_lookup.lookup_release", return_value=full) as lr,
    ):
        _step_with(ctl, "1")
    lr.assert_called_once()  # partial stub triggered the full fetch
    assert captured["preview_n"] == 3  # full listing reached the preview
    assert ctl.disc.tracks[1].isrc == "AAA000000002"  # full applied
    assert ctl.mb_rg_id is None  # AcoustID never threads the MB rg
    assert ctl.banner == "Applied."
    assert ctl.state is MenuState.ACOUSTID  # popped back to track picker


# ---------------------------------------------------------------------------
# cp4 — Original-release native screens
# ---------------------------------------------------------------------------


def test_main_r_pushes_native_original_release_screen() -> None:
    """MAIN [r] now pushes the native OriginalReleaseScreen (cp4)."""
    ctl = MenuController(_disc())
    _step_with(ctl, "r")
    assert isinstance(ctl.stack[-1], OriginalReleaseScreen)
    assert ctl.state is MenuState.ORIGINAL_RELEASE


def test_original_release_back_pops_to_main() -> None:
    """[b] is the single exit to MAIN (the hub stays put on every other action)."""
    ctl = MenuController(_disc())
    ctl.stack.append(OriginalReleaseScreen())
    _step_with(ctl, "b")
    assert ctl.state is MenuState.MAIN


def test_original_release_set_manually_stays_and_banners() -> None:
    """[m] runs the blocking _set_original_manually modal, then Stays on the hub
    (deviation from legacy's exit-to-MAIN, for EditScreen-style consistency)."""
    ctl = MenuController(_disc())
    ctl.stack.append(OriginalReleaseScreen())
    edited = _disc()
    edited.original_release_found = True
    edited.original_release_title = "By Hand"
    edited.original_release_year = 1984
    with patch("cdda2img.metadata_menu._set_original_manually", return_value=edited):
        _step_with(ctl, "m")
    assert ctl.disc.original_release_title == "By Hand"
    assert ctl.state is MenuState.ORIGINAL_RELEASE  # hub stays
    assert ctl.banner == "Original release set: By Hand."


def test_original_release_set_manually_blank_title_banners_cleared() -> None:
    """[m] with a blank title clears the fields; the banner reflects the cleared
    state, not a generic 'updated' (the helper's inline print is wiped in TUI)."""
    ctl = MenuController(_disc())
    ctl.disc.original_release_found = True
    ctl.disc.original_release_title = "Was Set"
    ctl.stack.append(OriginalReleaseScreen())
    cleared = _disc()  # original_release_found defaults to False
    with patch("cdda2img.metadata_menu._set_original_manually", return_value=cleared):
        _step_with(ctl, "m")
    assert ctl.disc.original_release_found is False
    assert ctl.banner == "Original release cleared."


def test_original_release_clear_resets_fields_and_stays() -> None:
    """[c] clears the original_release_* fields in place and Stays on the hub."""
    ctl = MenuController(_disc())
    ctl.disc.original_release_found = True
    ctl.disc.original_release_title = "Set Earlier"
    ctl.disc.original_release_year = 1979
    ctl.stack.append(OriginalReleaseScreen())
    _step_with(ctl, "c")
    assert ctl.disc.original_release_found is False
    assert ctl.disc.original_release_title is None
    assert ctl.disc.original_release_year is None
    assert ctl.state is MenuState.ORIGINAL_RELEASE
    assert "cleared" in ctl.banner.lower()


def test_original_release_search_no_results_banners_and_stays() -> None:
    ctl = MenuController(_disc())
    ctl.stack.append(OriginalReleaseScreen())
    with patch(
        "cdda2img.metadata_menu._fetch_releases_for_group",
        return_value=([], None),
    ):
        _step_with(ctl, "s")
    assert ctl.state is MenuState.ORIGINAL_RELEASE
    assert "No results" in ctl.banner


def test_original_release_search_pushes_results_sorted_earliest_first() -> None:
    """[s] pushes ResultsScreen(source='original'), sorted earliest-first so the
    original pressing leads (legacy parity)."""
    ctl = MenuController(_disc())
    ctl.stack.append(OriginalReleaseScreen())
    later = DiscMeta(album="A", release_date="1990", mb_release_group_id="rg-late")
    earlier = DiscMeta(album="A", release_date="1980", mb_release_group_id="rg-early")
    with patch(
        "cdda2img.metadata_menu._fetch_releases_for_group",
        return_value=([later, earlier], None),
    ):
        _step_with(ctl, "s")
    top = ctl.stack[-1]
    assert isinstance(top, ResultsScreen)
    assert top.source == "original"
    assert [m.release_date for m in top.results] == ["1980", "1990"]  # earliest first


def test_original_release_apply_sets_fields_threads_rg_and_pops_to_hub() -> None:
    """Selecting a result → _confirm_original [a] → fields set from the release,
    mb_rg_id threaded, 'Applied.' banner, pop back to the hub."""
    ctl = MenuController(_disc())
    ctl.stack.append(OriginalReleaseScreen())
    selected = DiscMeta(
        album="Original Title", release_date="1981-06-01", mb_release_group_id="rg-o"
    )
    ctl.stack.append(
        ResultsScreen([selected], "Original Release - Earliest First", "original")
    )
    with patch("cdda2img.metadata_menu._confirm_original", return_value=True):
        _step_with(ctl, "1")
    assert ctl.disc.original_release_found is True
    assert ctl.disc.original_release_title == "Original Title"
    assert ctl.disc.original_release_year == 1981
    assert ctl.mb_rg_id == "rg-o"
    assert ctl.banner == "Applied."
    assert ctl.state is MenuState.ORIGINAL_RELEASE  # popped back to the hub


def test_original_release_apply_declined_changes_nothing() -> None:
    """_confirm_original [b] (decline) applies nothing; still pops back (cp3
    decline→pop convention)."""
    ctl = MenuController(_disc())
    ctl.stack.append(OriginalReleaseScreen())
    selected = DiscMeta(album="X", release_date="1981", mb_release_group_id="rg-o")
    ctl.stack.append(
        ResultsScreen([selected], "Original Release - Earliest First", "original")
    )
    with patch("cdda2img.metadata_menu._confirm_original", return_value=False):
        _step_with(ctl, "1")
    assert ctl.disc.original_release_found is False
    assert ctl.mb_rg_id is None
    assert ctl.banner == ""
    assert ctl.state is MenuState.ORIGINAL_RELEASE


# ---------------------------------------------------------------------------
# EditScreen (native) — album / artist / dispatch
# ---------------------------------------------------------------------------


def test_main_e_pushes_native_edit_screen() -> None:
    """'e' at MAIN descends into the native EditScreen (state EDIT)."""
    ctl = MenuController(_disc())
    _step_with(ctl, "e")
    assert ctl.state is MenuState.EDIT
    assert isinstance(ctl.stack[-1], EditScreen)


def test_edit_album_edits_in_place_and_stays() -> None:
    ctl = MenuController(_disc(album="Old"))
    ctl.stack.append(EditScreen())
    _step_with(ctl, "a", "New Album")  # 'a', then the _prompt_edit value
    assert ctl.disc.album == "New Album"
    assert ctl.state is MenuState.EDIT  # stays on the edit screen


def test_edit_artist_edits_in_place_and_stays() -> None:
    ctl = MenuController(_disc(artist="Old Artist"))
    ctl.stack.append(EditScreen())
    _step_with(ctl, "r", "New Artist")
    assert ctl.disc.artist == "New Artist"
    assert ctl.state is MenuState.EDIT


def test_edit_blank_keeps_current_value() -> None:
    """_prompt_edit returns the current value on blank input (quirk preserved)."""
    ctl = MenuController(_disc(album="Keep Me"))
    ctl.stack.append(EditScreen())
    _step_with(ctl, "a", "")
    assert ctl.disc.album == "Keep Me"


def test_edit_d_pushes_disc_position_screen() -> None:
    ctl = MenuController(_disc())
    ctl.stack.append(EditScreen())
    _step_with(ctl, "d")
    assert ctl.state is MenuState.EDIT_DISC_POSITION
    assert isinstance(ctl.stack[-1], EditDiscPositionScreen)


def test_edit_track_command_pushes_track_screen() -> None:
    ctl = MenuController(_multitrack_disc(3))
    ctl.stack.append(EditScreen())
    _step_with(ctl, "t 2")
    assert ctl.state is MenuState.EDIT_TRACK
    top = ctl.stack[-1]
    assert isinstance(top, EditTrackScreen)
    assert top.track_number == 2


def test_edit_track_not_found_sets_banner_and_stays() -> None:
    ctl = MenuController(_multitrack_disc(2))
    ctl.stack.append(EditScreen())
    _step_with(ctl, "t 99")
    assert ctl.state is MenuState.EDIT
    assert "not found" in ctl.banner


def test_edit_track_non_numeric_sets_banner_and_stays() -> None:
    """'t x' is the ValueError path: 'Invalid track number.', distinct message."""
    ctl = MenuController(_multitrack_disc(2))
    ctl.stack.append(EditScreen())
    _step_with(ctl, "t x")
    assert ctl.state is MenuState.EDIT
    assert "Invalid track number" in ctl.banner


def test_edit_no_space_track_token_is_unknown_command() -> None:
    """'t3' (no space) does not match startswith('t '); unknown command."""
    ctl = MenuController(_multitrack_disc(3))
    ctl.stack.append(EditScreen())
    _step_with(ctl, "t3")
    assert ctl.state is MenuState.EDIT
    assert "Unknown" in ctl.banner


def test_edit_unknown_command_sets_banner_and_stays() -> None:
    ctl = MenuController(_disc())
    ctl.stack.append(EditScreen())
    _step_with(ctl, "zzz")
    assert ctl.state is MenuState.EDIT
    assert "Unknown" in ctl.banner


# ---------------------------------------------------------------------------
# EditTrackScreen (native) — title / performer / ISRC
# ---------------------------------------------------------------------------


def test_edit_track_title_and_performer() -> None:
    ctl = MenuController(_multitrack_disc(2))
    ctl.stack.append(EditTrackScreen(1))
    _step_with(ctl, "t", "New Title")
    assert ctl.disc.tracks[0].title == "New Title"
    _step_with(ctl, "p", "New Performer")
    assert ctl.disc.tracks[0].performer == "New Performer"
    assert ctl.state is MenuState.EDIT_TRACK


def test_edit_track_isrc_is_uppercased() -> None:
    ctl = MenuController(_multitrack_disc(1))
    ctl.stack.append(EditTrackScreen(1))
    _step_with(ctl, "i", "gbaye0601498")
    assert ctl.disc.tracks[0].isrc == "GBAYE0601498"


def test_edit_track_isrc_blank_clears_existing() -> None:
    """Blank input clears a non-empty ISRC (the label's promise). Unlike
    title/performer, this branch reads the prompt directly, not via _prompt_edit."""
    ctl = MenuController(_multitrack_disc(1))
    ctl.disc.tracks[0].isrc = "GBAYE0601498"
    ctl.stack.append(EditTrackScreen(1))
    _step_with(ctl, "i", "")
    assert ctl.disc.tracks[0].isrc is None


def test_edit_track_isrc_overwrites_existing() -> None:
    """A non-blank value replaces an existing ISRC (and is uppercased)."""
    ctl = MenuController(_multitrack_disc(1))
    ctl.disc.tracks[0].isrc = "GBAYE0601498"
    ctl.stack.append(EditTrackScreen(1))
    _step_with(ctl, "i", "usrc17607839")
    assert ctl.disc.tracks[0].isrc == "USRC17607839"


def test_edit_track_back_pops() -> None:
    ctl = MenuController(_multitrack_disc(2))
    ctl.stack.append(EditScreen())
    ctl.stack.append(EditTrackScreen(1))
    _step_with(ctl, "b")
    assert ctl.state is MenuState.EDIT


def test_edit_track_vanished_pops() -> None:
    """If the carried track number no longer resolves, the screen pops."""
    ctl = MenuController(_multitrack_disc(2))
    ctl.stack.append(EditScreen())
    ctl.stack.append(EditTrackScreen(2))
    del ctl.disc.tracks[1]  # remove track 2
    _step_with(ctl, "t")  # any input
    assert ctl.state is MenuState.EDIT


# ---------------------------------------------------------------------------
# EditDiscPositionScreen (native) — validation loop as Stay
# ---------------------------------------------------------------------------


def test_disc_position_valid_sets_and_pops() -> None:
    ctl = MenuController(_disc())
    ctl.stack.append(EditScreen())
    ctl.stack.append(EditDiscPositionScreen())
    _step_with(ctl, "1", "2")  # disc 1 of 2
    assert ctl.disc.disc_number == 1
    assert ctl.disc.disc_total == 2
    assert ctl.state is MenuState.EDIT  # popped back
    assert "Set: disc 1 of 2" in ctl.banner


def test_disc_position_invalid_sets_banner_and_stays() -> None:
    """number > total is invalid: banner + Stay (the legacy re-prompt loop)."""
    ctl = MenuController(_disc())
    ctl.stack.append(EditScreen())
    ctl.stack.append(EditDiscPositionScreen())
    _step_with(ctl, "3", "2")  # disc 3 of 2 — invalid
    assert ctl.state is MenuState.EDIT_DISC_POSITION  # stayed
    assert "Invalid" in ctl.banner


def test_disc_position_blank_keeps_current() -> None:
    """Blank / non-digit input keeps the current value (then validates/pops)."""
    ctl = MenuController(_disc())
    ctl.disc.disc_number = 2
    ctl.disc.disc_total = 5
    ctl.stack.append(EditScreen())
    ctl.stack.append(EditDiscPositionScreen())
    _step_with(ctl, "", "")  # keep both
    assert ctl.disc.disc_number == 2
    assert ctl.disc.disc_total == 5
    assert ctl.state is MenuState.EDIT  # current pair is valid → popped


# ---------------------------------------------------------------------------
# Native edit screens — render (content + banner-clear)
# ---------------------------------------------------------------------------


def test_edit_screen_render_shows_content_and_clears_banner(capsys) -> None:
    ctl = MenuController(_disc())
    ctl.banner = "transient"
    EditScreen().render(ctl)
    out = capsys.readouterr().out
    assert "Edit Metadata" in out
    assert "transient" in out  # banner shown this frame
    assert ctl.banner == ""  # ...then cleared


def test_edit_track_screen_render_shows_track_and_clears_banner(capsys) -> None:
    ctl = MenuController(_multitrack_disc(2))
    ctl.banner = "transient"
    EditTrackScreen(1).render(ctl)
    out = capsys.readouterr().out
    assert "Edit Track 1" in out
    assert "Title:" in out  # the track-present branch ran
    assert ctl.banner == ""


def test_edit_track_screen_render_tolerates_vanished_track(capsys) -> None:
    """The `track is not None` render guard: a vanished track still renders."""
    ctl = MenuController(_multitrack_disc(2))
    EditTrackScreen(99).render(ctl)  # no such track
    out = capsys.readouterr().out
    assert "Edit Track 99" in out
    assert "Title:" not in out  # the field block was skipped, no crash


def test_disc_position_screen_render_shows_current_and_clears_banner(capsys) -> None:
    ctl = MenuController(_disc())
    ctl.disc.disc_number = 1
    ctl.disc.disc_total = 2
    ctl.banner = "transient"
    EditDiscPositionScreen().render(ctl)
    out = capsys.readouterr().out
    assert "Current: disc 1 of 2" in out
    assert ctl.banner == ""


# ---------------------------------------------------------------------------
# format_ar_report helper
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
    """tui=False (--no-tui): the menu renders without clearing the screen and
    without the alternate screen buffer, so earlier pipeline output stays in the
    terminal scrollback."""
    ctl = MenuController(_disc(), tui=False)
    with (
        patch("cdda2img.menu_state.sys.stdin.isatty", return_value=True),
        patch("cdda2img.menu_state._clear_screen") as clear,
        patch("cdda2img.menu_state._enter_fullscreen") as enter,
        patch("cdda2img.menu_state._exit_fullscreen") as exit_,
        patch("cdda2img.metadata_menu._prompt", return_value="a"),
    ):
        ctl.run()
    clear.assert_not_called()
    enter.assert_not_called()
    exit_.assert_not_called()


def test_tui_clears_screen_by_default() -> None:
    """tui=True (default): the menu runs on the alternate screen buffer (entered
    once, restored once) and each frame clears + redraws (fixed-position UX)."""
    ctl = MenuController(_disc(), tui=True)
    with (
        patch("cdda2img.menu_state.sys.stdin.isatty", return_value=True),
        patch("cdda2img.menu_state._clear_screen") as clear,
        patch("cdda2img.menu_state._enter_fullscreen") as enter,
        patch("cdda2img.menu_state._exit_fullscreen") as exit_,
        patch("cdda2img.metadata_menu._prompt", return_value="a"),
    ):
        ctl.run()
    clear.assert_called()
    enter.assert_called_once()
    exit_.assert_called_once()


def test_tui_restores_main_screen_on_exception() -> None:
    """The alt buffer must be restored even if a screen raises — otherwise the
    user's terminal is left stuck on the (now-frozen) alt buffer."""
    ctl = MenuController(_disc(), tui=True)
    boom = RuntimeError("render exploded")
    with (
        patch("cdda2img.menu_state.sys.stdin.isatty", return_value=True),
        patch("cdda2img.menu_state._clear_screen"),
        patch("cdda2img.menu_state._enter_fullscreen") as enter,
        patch("cdda2img.menu_state._exit_fullscreen") as exit_,
        patch.object(MenuController, "_step", side_effect=boom),
        pytest.raises(RuntimeError, match="render exploded"),
    ):
        ctl.run()
    enter.assert_called_once()
    exit_.assert_called_once()  # finally ran despite the exception


# ---------------------------------------------------------------------------
# SIGWINCH resize handling
#
# A terminal resize while blocked on a prompt interrupts the read (raising
# _Resized out of input()) so the immediate-mode loop repaints at the new size.
# The handler only fires while a prompt is actually reading, so a resize can
# never abort a screen's post-prompt network I/O.
# ---------------------------------------------------------------------------


def test_winch_handler_raises_only_while_awaiting_input() -> None:
    import signal

    from cdda2img import menu_state as ms
    from cdda2img import metadata_menu as mm

    if not hasattr(signal, "SIGWINCH"):
        pytest.skip("no SIGWINCH on this platform")

    prev = ms._install_winch()
    assert prev is not ms._WINCH_UNSET
    try:
        handler = signal.getsignal(signal.SIGWINCH)
        mm._AWAITING_INPUT = False
        handler(signal.SIGWINCH, None)  # not reading → no repaint interrupt
        mm._AWAITING_INPUT = True
        with pytest.raises(ms._Resized):
            handler(signal.SIGWINCH, None)
    finally:
        mm._AWAITING_INPUT = False
        ms._restore_winch(prev)


def test_winch_handler_disarms_after_raise() -> None:
    """A SIGWINCH burst can't re-enter and raise while the first _Resized unwinds.

    Regression for the rapid-resize crash: the handler clears _AWAITING_INPUT
    before raising, so a second signal arriving during the unwind is a no-op
    until the next input() re-arms it. (The old handler also imported inside
    itself, which corrupted the import machinery under bursts — now resolved
    once at install time.)
    """
    import signal

    from cdda2img import menu_state as ms
    from cdda2img import metadata_menu as mm

    if not hasattr(signal, "SIGWINCH"):
        pytest.skip("no SIGWINCH on this platform")

    prev = ms._install_winch()
    try:
        handler = signal.getsignal(signal.SIGWINCH)
        mm._AWAITING_INPUT = True
        with pytest.raises(ms._Resized):
            handler(signal.SIGWINCH, None)
        assert mm._AWAITING_INPUT is False  # disarmed by the handler
        handler(signal.SIGWINCH, None)  # second signal during unwind: no raise
    finally:
        mm._AWAITING_INPUT = False
        ms._restore_winch(prev)


def test_step_swallows_resized_without_applying_nav() -> None:
    from cdda2img import menu_state as ms

    ctl = MenuController(_disc(), tui=False)

    class _RaisingScreen(ms.Screen):
        state = MenuState.MAIN
        renders = 0

        def render(self, ctl):
            type(self).renders += 1

        def handle_input(self, ctl):
            raise ms._Resized

    ctl.stack = [_RaisingScreen()]
    ctl._step()  # must not propagate _Resized
    assert _RaisingScreen.renders == 1
    assert isinstance(ctl.stack[-1], _RaisingScreen)  # no nav applied


def test_restore_winch_tolerates_unset_sentinel() -> None:
    from cdda2img import menu_state as ms

    ms._restore_winch(ms._WINCH_UNSET)  # no-op, never raises


# ---------------------------------------------------------------------------
# N5 — the pressing (alternatives) menu
#
# This path cannot be exercised by a rip: every container on the shelf is the
# same album, so none of them produces a differing tie size or a blank
# disambiguation. The fixtures below are built to the reference disc's measured
# shape instead — seven candidates, one barcode, six descriptions and one blank.
# ---------------------------------------------------------------------------


def _pressing(mbid: str, country: str = "XE", disamb: str | None = None) -> DiscMeta:
    return DiscMeta(
        album="Tracy Chapman",
        artist="Tracy Chapman",
        barcode="0075596077422",
        mb_release_id=mbid,
        mb_release_group_id="rg-1",
        country=country,
        catalog_number="7559-60774-2",
        disambiguation=disamb,
        source="musicbrainz",
    )


def _seven_pressings() -> list[DiscMeta]:
    return [
        _pressing("65e67d39", disamb='EW 835, upper-case "MADE IN GERMANY", 1999+'),
        _pressing("7531d07c", disamb="WE 851, old Elektra logo, short cat#"),
        _pressing("8e5e097d", disamb="WE 851, no red circle around CD rim"),
        _pressing("928588a5", "FR", disamb='EW 835, mixed-case "Made in Germany"'),
        _pressing("b63ffa5b", disamb="WE 835, newer 'e above E' Elektra logo"),
        _pressing("e6676f25", disamb="WE 835, old Elektra logo, approx. 1994/95"),
        _pressing("e9b905e6", "AU", disamb=None),  # a genuinely undescribed row
    ]


def _pressing_ctl(candidates=None, outcome="auto_tiebreak") -> MenuController:
    disc = _disc()
    disc.mb_release_id = "65e67d39"
    return MenuController(
        disc,
        pressing_candidates=_seven_pressings() if candidates is None else candidates,
        pressing_outcome=outcome,
    )


def test_main_offers_the_pressing_screen_only_when_there_is_a_choice() -> None:
    """One candidate is not a choice; offering a menu there invites the user to
    'confirm' something nothing could have got wrong."""
    with_choice = _pressing_ctl()
    without = _pressing_ctl(candidates=[_pressing("65e67d39")], outcome="unique")

    with patch("cdda2img.metadata_menu._prompt", return_value="s"):
        with_choice._apply(with_choice.stack[-1].handle_input(with_choice))
    assert with_choice.state is MenuState.PRESSING

    _step_with(without, "s")
    # 's' is not a command here: it falls through to the unknown-command banner
    # and the stack does not move.
    assert without.state is MenuState.MAIN
    assert "Unknown command" in without.banner


def test_pressing_screen_shows_every_evidence_rung_survivor() -> None:
    """N5's headline: the menu is NOT the set the ladder narrowed to.

    `preferred_country` would drop the FR and AU rows, leaving five — and those
    two are the only ones carrying a country or date a user could check against
    the sleeve. A preference rung breaks ties in the absence of a human; with a
    human present it hides options on a config setting rather than on evidence
    about the disc.
    """
    ctl = _pressing_ctl()
    _step_with(ctl, "s")
    screen = ctl.stack[-1]
    assert isinstance(screen, PressingScreen)
    assert len(screen.candidates) == 7
    ids = {c.mb_release_id for c in screen.candidates}
    assert {"928588a5", "e9b905e6"} <= ids


def test_pressing_screen_none_of_these_is_its_own_outcome() -> None:
    """A menu without a decline option forces the user to endorse a row that may
    be wrong, converting an honest automatic guess into a false manual
    confirmation — worse provenance than never having asked."""
    ctl = _pressing_ctl()
    _step_with(ctl, "s")
    _step_with(ctl, "x")
    assert ctl.pressing_outcome == "rejected"
    assert ctl.pressing_selected is None
    assert ctl.state is MenuState.MAIN
    # The automatic pick is kept; only the claim about it changes.
    assert ctl.disc.mb_release_id == "65e67d39"


def test_pressing_screen_back_leaves_the_outcome_untouched() -> None:
    """Backing out is not a decision. The recorded claim must stay
    `auto_tiebreak`, not silently become a manual confirmation."""
    ctl = _pressing_ctl()
    _step_with(ctl, "s")
    _step_with(ctl, "b")
    assert ctl.state is MenuState.MAIN
    assert ctl.pressing_outcome == "auto_tiebreak"


def test_pressing_selection_opens_detail_and_applies() -> None:
    """Picking a row opens the detail screen (where the annotation is fetched
    for that row only), and [a] there commits it."""
    ctl = _pressing_ctl()
    _step_with(ctl, "s")
    _step_with(ctl, "5")  # b63ffa5b — kgr's actual disc
    assert ctl.state is MenuState.PRESSING_DETAIL

    with (
        patch(
            "cdda2img.mb_lookup.fetch_annotation",
            return_value="price code '''France WE 835''' on back",
        ),
        patch("cdda2img.mb_lookup.lookup_release", return_value=None),
    ):
        ctl.stack[-1].render(ctl)
        _step_with(ctl, "a")

    assert ctl.pressing_outcome == "manual"
    assert ctl.pressing_selected is not None
    assert ctl.pressing_selected.mb_release_id == "b63ffa5b"
    assert ctl.disc.mb_release_id == "b63ffa5b"


def test_pressing_detail_fetches_the_annotation_once() -> None:
    """The annotation is one request per release at MB's 1 req/s, so a repaint
    (a terminal resize repaints every frame) must not re-request."""
    ctl = _pressing_ctl()
    _step_with(ctl, "s")
    _step_with(ctl, "1")
    with patch("cdda2img.mb_lookup.fetch_annotation", return_value="annot") as fetch:
        ctl.stack[-1].render(ctl)
        ctl.stack[-1].render(ctl)
        ctl.stack[-1].render(ctl)
    assert fetch.call_count == 1


def test_pressing_outcome_recorded_in_provenance() -> None:
    from cdda2img.metadata_menu import _record_pressing_outcome

    ctl = _pressing_ctl()
    ctl.pressing_outcome = "manual"
    ctl.pressing_selected = _pressing("b63ffa5b", disamb="WE 835, newer 'e above E'")
    ctl.disc.mb_release_id = "b63ffa5b"
    prov: dict[str, str] = {}
    _record_pressing_outcome(prov, ctl)
    assert prov["release_selection"] == "manual"
    assert prov["release_disambiguation"] == "WE 835, newer 'e above E'"


def test_pressing_outcome_falls_back_to_the_pinned_candidate() -> None:
    """With no manual pick, the free text recorded is the pinned candidate's —
    so an auto_tiebreak container still states which physical object it claims
    to be, not just an opaque MBID a later MB edit could redefine."""
    from cdda2img.metadata_menu import _record_pressing_outcome

    ctl = _pressing_ctl()
    prov: dict[str, str] = {}
    _record_pressing_outcome(prov, ctl)
    assert prov["release_selection"] == "auto_tiebreak"
    assert prov["release_disambiguation"].startswith("EW 835")


def test_no_pressing_key_when_no_release_was_pinned() -> None:
    """`unique` would claim a pressing selection happened and was unambiguous.
    With no release pinned at all, none happened — so no key, rather than a key
    whose value is indistinguishable from a real single-candidate match."""
    from cdda2img.metadata_menu import _record_pressing_outcome

    ctl = MenuController(_disc(), pressing_candidates=(), pressing_outcome="unique")
    prov: dict[str, str] = {}
    _record_pressing_outcome(prov, ctl)
    assert "release_selection" not in prov
