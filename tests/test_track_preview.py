"""
test_track_preview.py — Unit tests for the rip-pipeline track-1 audio preview.

The preview is cosmetic: its defining property is that it never breaks a rip.
These tests cover the graceful-degradation paths (missing tools, internal
failures resolving to None rather than an exception) and TrackPreview.stop().
"""

import subprocess

from cdda2img import track_preview
from cdda2img.track_preview import TrackPreview, start_preview


def test_returns_none_when_tools_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(track_preview.shutil, "which", lambda _name: None)
    assert start_preview("/dev/sr0", tmp_path) is None


def test_swallows_internal_errors(tmp_path, monkeypatch) -> None:
    # Tools appear installed, but the grab fails — start_preview must return
    # None and never propagate, so the rip continues unaffected.
    monkeypatch.setattr(track_preview.shutil, "which", lambda _name: "/usr/bin/x")

    def _boom(*_args, **_kwargs):
        msg = "simulated drive failure"
        raise RuntimeError(msg)

    monkeypatch.setattr(track_preview, "_grab_and_play", _boom)
    assert start_preview("/dev/sr0", tmp_path) is None


def test_stop_terminates_playback_and_removes_wav(tmp_path) -> None:
    wav = tmp_path / "preview.wav"
    wav.write_bytes(b"\x00" * 64)
    proc = subprocess.Popen(["sleep", "30"])  # noqa: S607  # LINT-017

    preview = TrackPreview(proc, wav)
    preview.stop()
    preview.stop()  # idempotent — a second call must not raise

    assert proc.poll() is not None  # process terminated
    assert not wav.exists()  # temp WAV cleaned up
