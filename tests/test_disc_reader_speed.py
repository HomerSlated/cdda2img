"""cd-paranoia read-speed flag (-S) wiring in the fallback rip paths."""

from __future__ import annotations

from pathlib import Path

import pytest

import cdda2img.container as container
import cdda2img.disc_reader as dr
import cdda2img.drive_speed as ds


class _FakeProc:
    returncode = 0


def _capture(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub query_disc + subprocess.run + wav_to_raw_pcm; capture the argv."""
    calls: dict = {}

    def fake_run(cmd, *a, **k):
        calls["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(dr, "query_disc", lambda d: (1, 100, [(8, 50, 51)]))
    monkeypatch.setattr(dr.subprocess, "run", fake_run)
    monkeypatch.setattr(container, "wav_to_raw_pcm", lambda *a, **k: None)
    # The read_speed paths fire a post-rip speed restore (cdrdao drive-info) — neutralise
    # it here; the dedicated restore behaviour is covered in test_drive_speed.py.
    monkeypatch.setattr(ds, "restore_drive_speed", lambda dev: None)
    return calls


def test_single_track_default_omits_speed_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _capture(monkeypatch)
    dr.rip_single_track("/dev/sr0", 8, tmp_path / "o.pcm", read_offset=30)
    assert "-S" not in calls["cmd"]


def test_single_track_read_speed_adds_S(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _capture(monkeypatch)
    dr.rip_single_track("/dev/sr0", 8, tmp_path / "o.pcm", read_offset=30, read_speed=1)
    cmd = calls["cmd"]
    assert cmd[cmd.index("-S") + 1] == "1"
    # -S must precede the "--" terminator so it is parsed as an option.
    assert cmd.index("-S") < cmd.index("--")


def test_rip_disc_default_omits_speed_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _capture(monkeypatch)
    dr.rip_disc("/dev/sr0", tmp_path / "o.pcm", read_offset=30)
    assert "-S" not in calls["cmd"]


def test_rip_disc_read_speed_adds_S(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _capture(monkeypatch)
    dr.rip_disc("/dev/sr0", tmp_path / "o.pcm", read_offset=30, read_speed=1)
    cmd = calls["cmd"]
    assert cmd[cmd.index("-S") + 1] == "1"
    assert cmd.index("-S") < cmd.index("--")
