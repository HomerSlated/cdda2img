"""Speed-laddered AccurateRip recovery loop (_recover_failed_tracks, AccuDisc engine).

The recovery engine re-reads each failed track's raw sector window via
``accudisc_reader.read_span``, AR-verifies the offset-corrected slice, and splices
the VERIFIED corrected bytes into the still-raw disc PCM at ``track_start*2352 +
read_offset*4`` — sample-exact, so neighbouring tracks are never perturbed.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

import cdda2img.accudisc_reader as adr
import cdda2img.accuraterip as ar
from cdda2img.cdda2img import _recover_failed_tracks

_FRAME = 2352  # _R6_BYTES_PER_FRAME
_OFFSET = 30  # samples → 120 bytes

# A 3-track disc; the failed track 2 is interior, spanning LSN 10..20.
_LSNS = [0, 10, 20]
_LAST_LSN = 29  # lead-out LBA 30
_N_TRACKS = 3
_TRACK2_FRAMES = 10  # 20 - 10
_TRACK2_BYTES = _TRACK2_FRAMES * _FRAME


def _failed(*tracks: int) -> list:
    return [types.SimpleNamespace(track=t) for t in tracks]


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    match_speed: int | None,
    fill: bytes = b"\xaa",
    honoured: dict[int | None, int | None] | None = None,
    defeat: str = "done",
) -> dict:
    """Stub `read_span_attempt` (returns canned window bytes) + `match_track_pcm`
    (matches at one speed). Returns a state dict recording the reads.

    *honoured* maps a requested speed to the one the fake drive reports adopting
    (default: the request is honoured); *defeat* is the cache-defeat outcome every
    read reports.

    Patches `read_span_attempt`, which is what `_ladder_attempt` actually calls,
    and returns its `SpanRead`, so a stub left on `read_span_bytes` intercepts
    nothing and fails loudly rather than letting every attempt be recorded as an
    error.
    These stubs patched `read_span` until 2026-08-01 and worked only because
    `read_span_bytes` was then implemented *through* it, via a temp file the
    subprocess needed. Retiring the CLI removed that indirection and the patch
    silently stopped intercepting anything — every attempt raised "the binding is
    required", the recovery loop counted each as a consumed attempt exactly as
    designed, and the tests failed on an empty speed list rather than on an
    error. Patch the function under test, not one it happens to delegate to.
    """
    state: dict = {"speeds": [], "windows": [], "cur": None, "defeat": []}

    def fake_read_span(
        device, start, count, read_speed=None, progress_cb=None, cache_defeat=False
    ):
        state["speeds"].append(read_speed)
        state["defeat"].append(cache_defeat)
        state["windows"].append((start, count))
        state["cur"] = read_speed
        return adr.SpanRead(
            pcm=fill * (count * _FRAME),
            speed_honoured_x=(honoured or {}).get(read_speed, read_speed),
            cache_defeat=defeat if cache_defeat else "off",
        )

    def fake_match(raw, track, n_tracks, responses):
        if match_speed is not None and state["cur"] == match_speed:
            return "aaaaaaaa", "bbbbbbbb", 50, None  # conf_v1=50 → match
        return "aaaaaaaa", "bbbbbbbb", None, None

    monkeypatch.setattr(adr, "read_span_attempt", fake_read_span)
    monkeypatch.setattr(ar, "match_track_pcm", fake_match)
    return state


def _disc_pcm(tmp_path: Path) -> Path:
    pcm = tmp_path / "disc.pcm"
    pcm.write_bytes(bytes((_LAST_LSN + 1) * _FRAME))  # all-zero full-disc RAW PCM
    return pcm


def test_recovery_stops_at_first_match_fastest_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = _patch(monkeypatch, match_speed=8)
    pcm = _disc_pcm(tmp_path)
    ladder = [32, 16, 8, 4]  # fastest first, as rungs_for binds it

    outcomes, _attempts = _recover_failed_tracks(
        "/dev/sr0",
        _failed(2),
        _LSNS,
        _LAST_LSN,
        pcm,
        ["resp"],
        _N_TRACKS,
        ladder,
        2,
        _OFFSET,
        None,
    )

    # fastest→slowest: 32, 16, then 8 matches → stop (no 4, no second sweep)
    assert state["speeds"] == [32, 16, 8]
    assert outcomes == {2: "matched@8X"}
    # +30 offset → the window is the track plus one tail margin sector
    assert state["windows"][0] == (10, 11)
    data = pcm.read_bytes()
    # The verified corrected bytes land at byte_start + offset*4 (sample-exact)…
    splice_lo = 10 * _FRAME + _OFFSET * 4
    assert data[splice_lo : splice_lo + _TRACK2_BYTES] == b"\xaa" * _TRACK2_BYTES
    # …and the first 120 raw bytes of the track's sector range — which feed the
    # PREVIOUS track's corrected tail — are untouched (neighbour protection).
    assert data[10 * _FRAME : splice_lo] == bytes(_OFFSET * 4)
    assert data[splice_lo + _TRACK2_BYTES] == 0  # nothing written past the slice


def test_the_bound_ladder_is_walked_in_the_order_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The shipped default from the profile on: what ``rungs_for`` binds is what
    each pass walks, so PROV ``recovery_ladder`` names the order of the attempts.

    Until 2026-10-22 the loop reversed the list. The test above fed it an
    ascending ladder, the shape the retired ``probe_speed_ladder`` returned, so it
    passed while every real rip walked slowest-first. This one starts where
    production starts.
    """
    from cdda2img.recovery_profile import load_profile, rungs_for

    profile = load_profile("track-ladder")
    rungs = rungs_for(profile, [8, 40, 4, 24, 32])  # admitted, in no order
    assert rungs == [40, 32, 24, 8, 4]
    state = _patch(monkeypatch, match_speed=None)

    outcomes, _attempts = _recover_failed_tracks(
        "/dev/sr0",
        _failed(2),
        _LSNS,
        _LAST_LSN,
        _disc_pcm(tmp_path),
        ["resp"],
        _N_TRACKS,
        rungs,
        profile.passes,
        _OFFSET,
        None,
    )

    assert state["speeds"] == rungs * profile.passes
    assert outcomes == {2: "unrecovered"}


def test_an_ad_ladder_is_walked_as_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--ad-ladder 4,32`` starts slow and then tries fast, a different experiment
    from ``32,4`` (``parse_ad_ladder``). Until 2026-10-22 it ran as ``32,4``."""
    from cdda2img.recovery_profile import ResolvedStrategy, bind_ladder

    bound = bind_ladder(
        ResolvedStrategy("ad-flags", ad_flags={"ladder": "4,32"}), "/dev/null"
    )
    state = _patch(monkeypatch, match_speed=None)

    _recover_failed_tracks(
        "/dev/sr0",
        _failed(2),
        _LSNS,
        _LAST_LSN,
        _disc_pcm(tmp_path),
        ["resp"],
        _N_TRACKS,
        list(bound.ladder),
        2,
        _OFFSET,
        None,
    )

    assert state["speeds"] == [4, 32, 4, 32]


def test_every_ladder_attempt_asks_for_a_cache_defeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """kgr, 2026-10-22: each attempt reads past what the previous one left in the
    drive's cache, or it repeats that read rather than making a new one. The
    first attempt asks too: a neighbouring track's re-read can leave this
    track's head in the cache."""
    state = _patch(monkeypatch, match_speed=None)

    _recover_failed_tracks(
        "/dev/sr0",
        _failed(2),
        _LSNS,
        _LAST_LSN,
        _disc_pcm(tmp_path),
        ["resp"],
        _N_TRACKS,
        [16, 8],
        2,
        _OFFSET,
        None,
    )

    assert state["defeat"] == [True, True, True, True]


def test_recovery_no_match_keeps_original_audio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = _patch(monkeypatch, match_speed=None)  # never matches
    pcm = _disc_pcm(tmp_path)
    ladder = [16, 8, 4]  # fastest first, as bound

    outcomes, _attempts = _recover_failed_tracks(
        "/dev/sr0",
        _failed(2),
        _LSNS,
        _LAST_LSN,
        pcm,
        ["resp"],
        _N_TRACKS,
        ladder,
        3,
        _OFFSET,
        None,
    )

    assert outcomes == {2: "unrecovered"}
    assert len(state["speeds"]) == 3 * 3  # full passes x ladder budget exhausted
    # original audio untouched: the whole file is still zero (no unverified splice)
    assert pcm.read_bytes() == bytes((_LAST_LSN + 1) * _FRAME)


def test_recovery_read_failure_consumes_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A c2read failure (RuntimeError) is one consumed attempt, not an abort."""
    state: dict = {"calls": 0}

    def flaky_read_span(
        device, start, count, read_speed=None, progress_cb=None, cache_defeat=False
    ):
        state["calls"] += 1
        if state["calls"] == 1:
            msg = "accudisc span read failed: boom"
            raise RuntimeError(msg)
        return adr.SpanRead(b"\xaa" * (count * _FRAME), read_speed, "done")

    def fake_match(raw, track, n_tracks, responses):
        return "a", "b", 50, None  # match as soon as a read succeeds

    monkeypatch.setattr(adr, "read_span_attempt", flaky_read_span)
    monkeypatch.setattr(ar, "match_track_pcm", fake_match)
    pcm = _disc_pcm(tmp_path)

    outcomes, _attempts = _recover_failed_tracks(
        "/dev/sr0",
        _failed(2),
        _LSNS,
        _LAST_LSN,
        pcm,
        ["resp"],
        _N_TRACKS,
        [16, 8],
        2,
        _OFFSET,
        None,
    )

    assert state["calls"] == 2  # attempt 1 failed, attempt 2 matched
    assert outcomes == {2: "matched@8X"}  # ladder [16, 8] → 16 failed, 8 won


def test_recovery_last_track_clamps_window_and_splice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Last track + positive offset: the window is clamped at the lead-out (the
    missing tail is zero-padded for verification) and the splice is trimmed to the
    file — nothing is written past EOF, and the file never grows."""
    state = _patch(monkeypatch, match_speed=8)
    pcm = _disc_pcm(tmp_path)

    outcomes, _attempts = _recover_failed_tracks(
        "/dev/sr0",
        _failed(3),
        _LSNS,
        _LAST_LSN,
        pcm,
        ["resp"],
        _N_TRACKS,
        [8],
        1,
        _OFFSET,
        None,
    )

    assert outcomes == {3: "matched@8X"}
    # requested window [20, 31) clamps to [20, 30): count stays inside the disc
    assert state["windows"][0] == (20, 10)
    data = pcm.read_bytes()
    assert len(data) == (_LAST_LSN + 1) * _FRAME  # file did not grow
    splice_lo = 20 * _FRAME + _OFFSET * 4
    assert data[splice_lo:] == b"\xaa" * (len(data) - splice_lo)


def test_recovery_negative_offset_track_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Track 1 + negative offset: the lead margin clamps at LBA 0 (zero-padded
    front) and the splice start clamps at byte 0 with the pad bytes dropped."""
    state = _patch(monkeypatch, match_speed=8)
    pcm = _disc_pcm(tmp_path)

    outcomes, _attempts = _recover_failed_tracks(
        "/dev/sr0",
        _failed(1),
        _LSNS,
        _LAST_LSN,
        pcm,
        ["resp"],
        _N_TRACKS,
        [8],
        1,
        -_OFFSET,
        None,
    )

    assert outcomes == {1: "matched@8X"}
    # requested window [-1, 10) clamps to [0, 10)
    assert state["windows"][0] == (0, 10)
    data = pcm.read_bytes()
    track1_bytes = 10 * _FRAME
    # corrected track 1 occupies raw bytes [0 - 120, track1 - 120) → clamped to
    # [0, track1 - 120); the first 120 corrected bytes (zero-pad) are dropped
    assert data[: track1_bytes - _OFFSET * 4] == b"\xaa" * (track1_bytes - _OFFSET * 4)
    assert data[track1_bytes - _OFFSET * 4 : track1_bytes] == bytes(_OFFSET * 4)


def test_recovery_tui_status_is_per_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The recovery loop owns the status line: a per-attempt banner that the live
    progress callback keeps (only the bar advances)."""
    statuses: list[tuple[str, float]] = []

    class _FakeUI:
        def set_status(
            self, text: str, progress: float = 0.0, detail: str = ""
        ) -> None:
            statuses.append((text, progress))

    def fake_read_span(
        device, start, count, read_speed=None, progress_cb=None, cache_defeat=False
    ):
        if progress_cb is not None:  # emit one mid-window progress event
            progress_cb(5, 10)
        return adr.SpanRead(b"\xaa" * (count * _FRAME), read_speed, "done")

    def fake_match(raw, track, n_tracks, responses):
        return "a", "b", 50, None  # match on the first attempt (fastest speed)

    monkeypatch.setattr(adr, "read_span_attempt", fake_read_span)
    monkeypatch.setattr(ar, "match_track_pcm", fake_match)

    pcm = _disc_pcm(tmp_path)
    ladder = [16, 8, 4]  # as bound; 3 passes x 3 steps = 9 total attempts

    outcomes, _attempts = _recover_failed_tracks(
        "/dev/sr0",
        _failed(2),
        _LSNS,
        _LAST_LSN,
        pcm,
        ["resp"],
        _N_TRACKS,
        ladder,
        3,
        _OFFSET,
        _FakeUI(),  # type: ignore[invalid-argument-type]
    )

    assert outcomes == {2: "matched@16X"}  # fastest-first, matched immediately
    # Attempt banner set (bar reset to 0), then the progress cb keeps the same text
    # while advancing the bar.
    assert statuses[0] == ("Recover track 2 (1/9)", 0.0)
    assert statuses[1][0] == "Recover track 2 (1/9)"
    assert statuses[1][1] == pytest.approx(0.5)  # 5/10 sectors


# ---------------------------------------------------------------------------
# _RecoveryMap — the whole-disc map kept live across the ladder (N2 step 5)
# ---------------------------------------------------------------------------


class _FakeUI:
    """Records set_map calls. The real TerminalUI needs a tty fd."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def set_map(self, damage, *, subq=None, status_width=0, active=None) -> None:
        self.calls.append((damage, status_width, active))


def _recovery_map(**kw):
    from cdda2img.cdda2img import _RecoveryMap

    return _RecoveryMap(**kw)


def test_the_recovery_map_copies_so_the_rips_own_record_survives():
    """The one place something other than the reader writes to a map. The CTDB
    report is drawn later from the rip's damage map, so clearing a recovered
    track must not edit what the drive actually reported."""
    original = bytes(b"\x01" * 100)
    ui = _FakeUI()
    m = _recovery_map(ui=ui, disc_damage=original, status_width=8)

    m.arm(0, 100)
    m.clear(0, 50)

    assert original == b"\x01" * 100, "the caller's map was mutated"
    handed, _, _ = ui.calls[0]
    assert bytes(handed[:50]) == bytes(50)
    assert bytes(handed[50:]) == b"\x01" * 50


def test_arming_hands_over_the_live_region_and_a_pinned_width():
    ui = _FakeUI()
    m = _recovery_map(ui=ui, disc_damage=b"\x00" * 100, status_width=23)

    m.arm(10, 20)
    m.arm(60, 70)

    assert [c[2] for c in ui.calls] == [(10, 20), (60, 70)]
    assert {c[1] for c in ui.calls} == {23}, (
        "width must be the widest banner across ALL tracks — map geometry is "
        "pinned at the first frame, so a later two-digit track number would be "
        "truncated for the rest of the ladder"
    )


def test_release_drops_the_renderers_reference():
    """A map with a live region and no reader behind it reads as a stalled repair."""
    ui = _FakeUI()
    m = _recovery_map(ui=ui, disc_damage=b"\x00" * 10, status_width=4)
    m.arm(0, 5)
    m.release()
    assert ui.calls[-1][0] is None


def test_the_map_is_inert_without_a_ui_or_without_a_captured_map():
    """Both are ordinary states — `--no-tui`, or a read that never handed the
    lanes over — and neither may raise or leave the caller branching."""
    ui = _FakeUI()
    for kw in (
        {"ui": None, "disc_damage": b"\x01" * 10},
        {"ui": ui, "disc_damage": None},
        {"ui": ui, "disc_damage": b""},
    ):
        m = _recovery_map(status_width=4, **kw)
        m.arm(0, 5)
        m.clear(0, 5)
        m.release()
    assert ui.calls == [], "no map was captured, so nothing should be drawn"


def test_the_last_track_window_runs_to_the_lead_out():
    """A following entry does not exist for the last track, so its end comes from
    disc_last_lsn. Getting this wrong leaves the final track — the outer edge,
    where damage concentrates — permanently outside the live region."""
    from cdda2img.cdda2img import _track_sector_window

    lsns = [0, 1000, 2000]
    assert _track_sector_window(lsns, 0, 4999) == (0, 1000)
    assert _track_sector_window(lsns, 1, 4999) == (1000, 2000)
    assert _track_sector_window(lsns, 2, 4999) == (2000, 5000)


# ---------------------------------------------------------------------------
# recovery_attempts_track_<n>: the walk as it ran (kgr, 2026-10-25)
# ---------------------------------------------------------------------------


def _attempts(monkeypatch, tmp_path, ladder, passes, **patch) -> tuple[dict, dict]:
    _patch(monkeypatch, **patch)
    return _recover_failed_tracks(
        "/dev/sr0",
        _failed(2),
        _LSNS,
        _LAST_LSN,
        _disc_pcm(tmp_path),
        ["resp"],
        _N_TRACKS,
        ladder,
        passes,
        _OFFSET,
        None,
    )


def test_every_attempt_is_recorded_in_the_order_it_ran(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A descending ladder with a quantized rung: the record names what each
    attempt asked for and what the drive ran at, in walk order, and ends at the
    match. Reversing the walk, swapping the two speeds or keeping only the match
    each fails here."""
    outcomes, prov = _attempts(
        monkeypatch, tmp_path, [40, 32, 4], 1, match_speed=4, honoured={32: 24}
    )

    assert outcomes == {2: "matched@4X"}  # the requested speed, as before
    assert prov == {"recovery_attempts_track_2": "40>40:done,32>24:done,4>4:done"}


def test_an_unrecovered_track_lists_every_attempt_of_every_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outcomes, prov = _attempts(
        monkeypatch, tmp_path, [16, 8], 2, match_speed=None, defeat="failed"
    )

    assert outcomes == {2: "unrecovered"}
    assert prov == {
        "recovery_attempts_track_2": "16>16:failed,8>8:failed,16>16:failed,8>8:failed"
    }


def test_an_unreported_honoured_speed_is_recorded_as_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _outcomes, prov = _attempts(
        monkeypatch, tmp_path, [8], 1, match_speed=8, honoured={8: None}, defeat="off"
    )

    assert prov == {"recovery_attempts_track_2": "8>?:off"}


def test_a_read_that_raised_is_recorded_where_it_happened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The attempt whose read raised used up a rung, so it stays in the walk."""

    def read(device, start, count, read_speed=None, progress_cb=None, **_kw):
        if read_speed == 32:
            msg = "accudisc span read failed: boom"
            raise RuntimeError(msg)
        return adr.SpanRead(b"\xaa" * (count * _FRAME), read_speed, "done")

    def match(raw, track, n_tracks, responses):
        return "a", "b", None, None

    monkeypatch.setattr(adr, "read_span_attempt", read)
    monkeypatch.setattr(ar, "match_track_pcm", match)

    outcomes, prov = _recover_failed_tracks(
        "/dev/sr0",
        _failed(2),
        _LSNS,
        _LAST_LSN,
        _disc_pcm(tmp_path),
        ["resp"],
        _N_TRACKS,
        [40, 32, 4],
        1,
        _OFFSET,
        None,
    )

    assert outcomes == {2: "unrecovered"}
    assert prov == {"recovery_attempts_track_2": "40>40:done,32:error,4>4:done"}


def test_rip_image_writes_the_attempts_into_prov() -> None:
    """Wiring control: the record is useless if it never leaves the ladder."""
    import inspect

    import cdda2img.cdda2img as C

    src = inspect.getsource(C.rip_image)
    assert "outcomes, attempts_prov = _recover_failed_tracks(" in src
    assert "ladder_prov.update(attempts_prov)" in src
    assert src.index("ladder_prov.update(attempts_prov)") < src.index(
        "provenance.update(ladder_prov)"
    )
