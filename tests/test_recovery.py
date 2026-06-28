"""Speed-laddered AccurateRip recovery loop (_recover_failed_tracks)."""

from __future__ import annotations

import types
from pathlib import Path

import pytest

import cdda2img.accuraterip as ar
import cdda2img.disc_reader as dr
from cdda2img.cdda2img import _recover_failed_tracks

_FRAME = 2352  # _R6_BYTES_PER_FRAME

# A 3-track disc; the failed track 2 is interior, spanning LSN 10..20.
_LSNS = [0, 10, 20]
_LAST_LSN = 29
_N_TRACKS = 3
_TRACK2_FRAMES = 10  # 20 - 10
_TRACK2_BYTES = _TRACK2_FRAMES * _FRAME


def _failed(*tracks: int) -> list:
    return [types.SimpleNamespace(track=t) for t in tracks]


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    match_speed: int | None,
    out_frames: int = _TRACK2_FRAMES,
) -> dict:
    """Stub rip_single_track (writes canned bytes) + match_track_pcm (matches at one speed).

    Returns a state dict recording the speeds tried and the rip kwargs.
    """
    state: dict = {"speeds": [], "kwargs": [], "cur": None}

    def fake_rip(device, t, out, **kw):
        state["speeds"].append(kw["read_speed"])
        state["kwargs"].append(kw)
        state["cur"] = kw["read_speed"]
        # Canned audio whose byte length is out_frames frames.
        Path(out).write_bytes(b"\xaa" * (out_frames * _FRAME))
        return out_frames

    def fake_match(raw, track, n_tracks, responses):
        if match_speed is not None and state["cur"] == match_speed:
            return "aaaaaaaa", "bbbbbbbb", 50, None  # conf_v1=50 → match
        return "aaaaaaaa", "bbbbbbbb", None, None

    monkeypatch.setattr(dr, "rip_single_track", fake_rip)
    monkeypatch.setattr(ar, "match_track_pcm", fake_match)
    return state


def _disc_pcm(tmp_path: Path) -> Path:
    pcm = tmp_path / "disc.pcm"
    pcm.write_bytes(bytes((_LAST_LSN + 1) * _FRAME))  # all-zero full-disc PCM
    return pcm


def test_recovery_stops_at_first_match_fastest_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = _patch(monkeypatch, match_speed=8)
    pcm = _disc_pcm(tmp_path)
    ladder = [4, 8, 16, 32]  # ascending; loop sweeps reversed (fastest first)

    ok, outcomes = _recover_failed_tracks(
        "/dev/sr0",
        _failed(2),
        _LSNS,
        _LAST_LSN,
        pcm,
        ["resp"],
        _N_TRACKS,
        ladder,
        2,
        30,
        None,
    )

    assert ok is True
    # fastest→slowest: 32, 16, then 8 matches → stop (no 4, no second sweep)
    assert state["speeds"] == [32, 16, 8]
    assert outcomes == {2: "matched@8X"}
    # the loop must not restore speed between attempts
    assert all(kw["restore_speed"] is False for kw in state["kwargs"])
    # the matched bytes were spliced into track 2's byte range
    spliced = pcm.read_bytes()[10 * _FRAME : 20 * _FRAME]
    assert spliced == b"\xaa" * _TRACK2_BYTES


def test_recovery_no_match_keeps_cdrdao_audio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = _patch(monkeypatch, match_speed=None)  # never matches
    pcm = _disc_pcm(tmp_path)
    ladder = [4, 8, 16]

    ok, outcomes = _recover_failed_tracks(
        "/dev/sr0",
        _failed(2),
        _LSNS,
        _LAST_LSN,
        pcm,
        ["resp"],
        _N_TRACKS,
        ladder,
        3,
        30,
        None,
    )

    assert ok is True
    assert outcomes == {2: "unrecovered"}
    assert len(state["speeds"]) == 3 * 3  # full passes x ladder budget exhausted
    # cdrdao audio untouched: track 2 region is still zero (no unverified splice)
    assert pcm.read_bytes()[10 * _FRAME : 20 * _FRAME] == bytes(_TRACK2_BYTES)


def test_recovery_boundary_mismatch_signals_full_rerip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # cd-paranoia returns a different frame count than cdrdao → structural disagreement.
    state = _patch(monkeypatch, match_speed=None, out_frames=_TRACK2_FRAMES + 1)
    pcm = _disc_pcm(tmp_path)

    ok, outcomes = _recover_failed_tracks(
        "/dev/sr0",
        _failed(2),
        _LSNS,
        _LAST_LSN,
        pcm,
        ["resp"],
        _N_TRACKS,
        [8, 16],
        2,
        30,
        None,
    )

    assert ok is False  # caller takes the full-disc re-rip fallback
    assert outcomes == {}  # bailed before recording an outcome
    assert len(state["speeds"]) == 1  # bailed on the very first attempt
