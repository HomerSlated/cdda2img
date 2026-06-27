"""cd-paranoia recovery controls: -z retries, -e callback-stream progress,
and exact-frame (sector-range) re-ripping."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

import cdda2img.container as container
import cdda2img.disc_reader as dr


class _FakeProc:
    returncode = 0


def _capture_run(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub query_disc + subprocess.run + wav_to_raw_pcm; capture the argv."""
    calls: dict = {}

    def fake_run(cmd, *a, **k):
        calls["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(dr, "query_disc", lambda d: (0, 999, [(1, 0, 1000)]))
    monkeypatch.setattr(dr.subprocess, "run", fake_run)
    monkeypatch.setattr(container, "wav_to_raw_pcm", lambda *a, **k: None)
    return calls


# ── -z retry budget (attempts per frame) ───────────────────────────────────


def test_retry_flags_default_empty() -> None:
    assert dr._retry_flags(None, False) == []


def test_retry_flags_count_is_attached() -> None:
    # -z takes an optional argument, so the count must be attached (-z40, not -z 40).
    assert dr._retry_flags(40, False) == ["-z40"]


def test_retry_flags_never_skip_is_bare() -> None:
    assert dr._retry_flags(20, True) == ["-z"]  # never_skip wins over a count


def test_rip_disc_max_retries_adds_attached_z(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _capture_run(monkeypatch)
    dr.rip_disc("/dev/sr0", tmp_path / "o.pcm", read_offset=30, max_retries=40)
    cmd = calls["cmd"]
    assert "-z40" in cmd
    assert cmd.index("-z40") < cmd.index("--")


def test_rip_single_track_never_skip_adds_bare_z(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _capture_run(monkeypatch)
    dr.rip_single_track("/dev/sr0", 1, tmp_path / "o.pcm", never_skip=True)
    cmd = calls["cmd"]
    assert "-z" in cmd
    assert cmd.index("-z") < cmd.index("--")


# ── -e callback-stream progress ─────────────────────────────────────────────


class _FakePopen:
    """Fake cd-paranoia: yields canned -e callback lines on stderr, then exits."""

    lines: ClassVar[list[str]] = []
    rc: int = 0
    last_cmd: ClassVar[list[str]] = []

    def __init__(self, cmd, **kw):
        _FakePopen.last_cmd = cmd
        self.stderr = iter(_FakePopen.lines)

    def wait(self) -> int:
        return _FakePopen.rc


def test_progress_parses_wrote_frontier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _FakePopen.lines = [
        "Sending all callback output to stderr for wrapper script\n",
        "##: 0 [read] @ 1176\n",  # read event — ignored (not WROTE)
        "##: 14 [wrote] @ 0\n",  # sector 0 — not > elapsed(0), no emit
        "##: 14 [wrote] @ 1176\n",  # sector 1
        "##: 14 [wrote] @ 588000\n",  # sector 500
        "##: 14 [wrote] @ 588\n",  # backwards — must not emit
        "##: 15 [finished] @ 1175424\n",
    ]
    _FakePopen.rc = 0
    monkeypatch.setattr(dr.subprocess, "Popen", _FakePopen)

    from cdda2img.cdrdao_progress import ProgressUpdate

    seen: list[ProgressUpdate] = []
    wav = tmp_path / "x.wav"
    cmd = ["cd-paranoia", "-d", "/dev/sr0", "--", "1-", str(wav)]
    rc = dr._run_paranoia_with_progress(cmd, wav, 1000, [(1, 0, 1000)], 0, seen.append)

    assert rc == 0
    # -e injected right after the binary name
    assert _FakePopen.last_cmd[1] == "-e"
    # monotonic WROTE frontier (1, 500), backwards dropped, then 100% close (1000)
    assert [u.elapsed_frames for u in seen] == [1, 500, 1000]
    assert seen[-1].fraction == 1.0


def test_progress_failure_no_final_close(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _FakePopen.lines = ["##: 14 [wrote] @ 1176\n"]
    _FakePopen.rc = 1  # non-zero exit → no 100% close emitted
    monkeypatch.setattr(dr.subprocess, "Popen", _FakePopen)

    from cdda2img.cdrdao_progress import ProgressUpdate

    seen: list[ProgressUpdate] = []
    wav = tmp_path / "x.wav"
    cmd = ["cd-paranoia", "-d", "/dev/sr0", "--", "1-", str(wav)]
    rc = dr._run_paranoia_with_progress(cmd, wav, 1000, [(1, 0, 1000)], 0, seen.append)
    assert rc == 1
    assert [u.elapsed_frames for u in seen] == [1]  # only the WROTE line, no close


# ── exact-frame (sector-range) re-rip ───────────────────────────────────────


def test_rip_sector_range_builds_track_relative_span(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "o.pcm"
    calls: dict = {}

    def fake_run(cmd, *a, **k):
        calls["cmd"] = cmd
        out.write_bytes(b"\x00" * (dr._CD_FRAME_BYTES * 50))  # 50 frames produced
        return _FakeProc()

    monkeypatch.setattr(dr.subprocess, "run", fake_run)
    monkeypatch.setattr(container, "wav_to_raw_pcm", lambda *a, **k: None)

    produced = dr.rip_sector_range("/dev/sr0", 8, 100, 150, out, read_offset=30)

    cmd = calls["cmd"]
    assert "8:[.100]-8:[.150]" in cmd
    assert cmd.index("8:[.100]-8:[.150]") > cmd.index("--")
    # returns the ACTUAL frames produced, not the requested width
    assert produced == 50


def test_rip_sector_range_rejects_bad_range(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid frame range"):
        dr.rip_sector_range("/dev/sr0", 8, 150, 100, tmp_path / "o.pcm")


# ── stall-status readout (recovery notes while the bar holds) ────────────────


def test_stall_note_only_for_trouble_events() -> None:
    assert dr._stall_note(0, 100) == ""  # read — silent
    assert dr._stall_note(1, 100) == ""  # verify — silent
    assert dr._stall_note(12, 100) == "read error @ sector 100"
    assert dr._stall_note(6, 100) == "skipped (unreadable) @ sector 100"
    assert dr._stall_note(3, 100) == "repairing jitter @ sector 100"


def test_progress_surfaces_recovery_notes_without_moving_bar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _FakePopen.lines = [
        "##: 14 [wrote] @ 1176\n",  # sector 1 — bar to 1
        "##: 12 [transport error] @ 117600\n",  # READERR @ sector 100 — note, bar holds
        "##: 3 [correction] @ 117600\n",  # FIXUP_ATOM — different note, bar holds
        "##: 12 [transport error] @ 117600\n",  # READERR again — note changes back
        "##: 0 [read] @ 117600\n",  # read — silent, no emit
        "##: 14 [wrote] @ 235200\n",  # sector 200 — bar advances, note STICKS
    ]
    _FakePopen.rc = 0
    monkeypatch.setattr(dr.subprocess, "Popen", _FakePopen)

    from cdda2img.cdrdao_progress import ProgressUpdate

    seen: list[ProgressUpdate] = []
    wav = tmp_path / "x.wav"
    cmd = ["cd-paranoia", "-d", "/dev/sr0", "--", "1-", str(wav)]
    rc = dr._run_paranoia_with_progress(cmd, wav, 1000, [(1, 0, 1000)], 0, seen.append)

    assert rc == 0
    pairs = [(u.elapsed_frames, u.note) for u in seen]
    assert pairs == [
        (1, ""),  # first WROTE
        (1, "read error @ sector 100"),  # bar holds at 1, note set
        (1, "repairing jitter @ sector 100"),  # bar holds, note changes
        (1, "read error @ sector 100"),  # bar holds, note changes back
        (
            200,
            "read error @ sector 100",
        ),  # bar advances; note STICKY (1 < _NOTE_CLEAR_RUN)
        (1000, ""),  # 100% close
    ]


def test_recovery_note_clears_after_sustained_clean_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(dr, "_NOTE_CLEAR_RUN", 2)  # clear after 2 clean sectors
    _FakePopen.lines = [
        "##: 3 [correction] @ 117600\n",  # note set @ sector 100, bar at 0
        "##: 14 [wrote] @ 1176\n",  # sector 1 — clean_run 1 (<2), note rides
        "##: 14 [wrote] @ 2352\n",  # sector 2 — clean_run 2 (>=2), note clears
    ]
    _FakePopen.rc = 0
    monkeypatch.setattr(dr.subprocess, "Popen", _FakePopen)

    from cdda2img.cdrdao_progress import ProgressUpdate

    seen: list[ProgressUpdate] = []
    wav = tmp_path / "x.wav"
    cmd = ["cd-paranoia", "-d", "/dev/sr0", "--", "1-", str(wav)]
    rc = dr._run_paranoia_with_progress(cmd, wav, 1000, [(1, 0, 1000)], 0, seen.append)

    assert rc == 0
    pairs = [(u.elapsed_frames, u.note) for u in seen]
    assert pairs == [
        (0, "repairing jitter @ sector 100"),  # trouble sets the note
        (1, "repairing jitter @ sector 100"),  # 1 clean sector — note rides along
        (2, ""),  # 2nd clean sector hits the threshold — note clears
        (1000, ""),  # 100% close
    ]
