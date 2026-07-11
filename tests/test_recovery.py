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
) -> dict:
    """Stub c2_reader.read_span (writes canned window bytes) + match_track_pcm
    (matches at one speed). Returns a state dict recording the reads."""
    state: dict = {"speeds": [], "windows": [], "cur": None}

    def fake_read_span(device, start, count, out, read_speed=None, progress_cb=None):
        state["speeds"].append(read_speed)
        state["windows"].append((start, count))
        state["cur"] = read_speed
        Path(out).write_bytes(fill * (count * _FRAME))

    def fake_match(raw, track, n_tracks, responses):
        if match_speed is not None and state["cur"] == match_speed:
            return "aaaaaaaa", "bbbbbbbb", 50, None  # conf_v1=50 → match
        return "aaaaaaaa", "bbbbbbbb", None, None

    monkeypatch.setattr(adr, "read_span", fake_read_span)
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
    ladder = [4, 8, 16, 32]  # ascending; loop sweeps reversed (fastest first)

    outcomes = _recover_failed_tracks(
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


def test_recovery_no_match_keeps_original_audio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = _patch(monkeypatch, match_speed=None)  # never matches
    pcm = _disc_pcm(tmp_path)
    ladder = [4, 8, 16]

    outcomes = _recover_failed_tracks(
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

    def flaky_read_span(device, start, count, out, read_speed=None, progress_cb=None):
        state["calls"] += 1
        if state["calls"] == 1:
            msg = "c2read span read failed (exit 1): boom"
            raise RuntimeError(msg)
        Path(out).write_bytes(b"\xaa" * (count * _FRAME))

    def fake_match(raw, track, n_tracks, responses):
        return "a", "b", 50, None  # match as soon as a read succeeds

    monkeypatch.setattr(adr, "read_span", flaky_read_span)
    monkeypatch.setattr(ar, "match_track_pcm", fake_match)
    pcm = _disc_pcm(tmp_path)

    outcomes = _recover_failed_tracks(
        "/dev/sr0",
        _failed(2),
        _LSNS,
        _LAST_LSN,
        pcm,
        ["resp"],
        _N_TRACKS,
        [8, 16],
        2,
        _OFFSET,
        None,
    )

    assert state["calls"] == 2  # attempt 1 failed, attempt 2 matched
    assert outcomes == {2: "matched@8X"}  # ladder [8, 16] reversed → 16 failed, 8 won


def test_recovery_last_track_clamps_window_and_splice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Last track + positive offset: the window is clamped at the lead-out (the
    missing tail is zero-padded for verification) and the splice is trimmed to the
    file — nothing is written past EOF, and the file never grows."""
    state = _patch(monkeypatch, match_speed=8)
    pcm = _disc_pcm(tmp_path)

    outcomes = _recover_failed_tracks(
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

    outcomes = _recover_failed_tracks(
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

    def fake_read_span(device, start, count, out, read_speed=None, progress_cb=None):
        if progress_cb is not None:  # emit one mid-window progress event
            progress_cb(5, 10)
        Path(out).write_bytes(b"\xaa" * (count * _FRAME))

    def fake_match(raw, track, n_tracks, responses):
        return "a", "b", 50, None  # match on the first attempt (fastest speed)

    monkeypatch.setattr(adr, "read_span", fake_read_span)
    monkeypatch.setattr(ar, "match_track_pcm", fake_match)

    pcm = _disc_pcm(tmp_path)
    ladder = [4, 8, 16]  # 3 passes x 3 steps = 9 total attempts

    outcomes = _recover_failed_tracks(
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
