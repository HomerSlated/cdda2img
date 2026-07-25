"""
track_preview.py — background audio preview for the rip pipeline.

Grabs track 1 of the disc via AccuDisc to a temporary WAV, then plays it on a
loop in the background (ffplay) while the rest of the rip runs — through the
metadata menu, loudness analysis and container build, until the rip ends.

Purely cosmetic: every failure path is swallowed so a rip is never affected.
Because there is a single optical drive, the track-1 grab must complete before
the main rip starts; playback then overlaps everything that follows.

Public interface:
    preview = start_preview(device, work_dir, progress_cb=...) -> TrackPreview | None
    preview.stop()
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

_PREVIEW_WAV = "preview_track01.wav"


class TrackPreview:
    """Handle for the looping background playback. Call stop() to end it."""

    def __init__(self, proc: subprocess.Popen[bytes], wav_path: Path) -> None:
        self._proc = proc
        self._wav_path = wav_path

    def stop(self) -> None:
        """Terminate playback and delete the temp WAV. Safe to call repeatedly."""
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        self._wav_path.unlink(missing_ok=True)


def start_preview(
    device: str,
    work_dir: Path,
    progress_cb: Callable[[int, int], None] | None = None,
) -> TrackPreview | None:
    """Grab track 1 from *device* and start looping background playback.

    Returns a TrackPreview handle, or None when the preview cannot run — this
    is a cosmetic feature, so every failure is swallowed and the rip proceeds
    unaffected. *progress_cb(done_frames, total_frames)* is invoked during the
    grab so the caller can drive a progress bar.
    """
    if shutil.which("ffplay") is None:
        log.info("track preview skipped — ffplay not installed")
        return None
    try:
        return _grab_and_play(device, work_dir, progress_cb)
    except Exception as exc:
        log.warning("track preview unavailable: %s", exc)
    return None


def _grab_and_play(
    device: str,
    work_dir: Path,
    progress_cb: Callable[[int, int], None] | None,
) -> TrackPreview:
    from cdda2img.accudisc_reader import read_toc

    geom = read_toc(device)
    if not geom.track_lsns:
        msg = "no audio tracks reported"
        raise RuntimeError(msg)
    start = geom.track_lsns[0]
    end = geom.track_lsns[1] if len(geom.track_lsns) > 1 else geom.disc_last_lsn + 1
    track1_frames = max(0, end - start)

    wav_path = work_dir / _PREVIEW_WAV
    _grab_track1(device, wav_path, start, track1_frames, progress_cb)

    cmd = ["ffplay", "-nodisp", "-loop", "0", "-loglevel", "quiet", str(wav_path)]
    proc = subprocess.Popen(  # noqa: S603  # LINT-008
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return TrackPreview(proc, wav_path)


def _grab_track1(
    device: str,
    wav_path: Path,
    start_lba: int,
    track1_frames: int,
    progress_cb: Callable[[int, int], None] | None,
) -> None:
    """Grab track 1 to *wav_path* via ``accudisc read`` (M5).

    AccuDisc reports real per-sector progress on its machine fd, so this no
    longer polls a growing file — the callback is driven by the reader itself.
    The read is raw (uncorrected) and no C2/sub is captured: this is a throwaway
    preview, so speed beats fidelity, and nothing here reaches the container.
    """
    from cdda2img.accudisc_reader import read_span
    from cdda2img.container import _write_wav_header

    pcm_path = wav_path.with_suffix(".pcm")
    try:
        read_span(device, start_lba, track1_frames, pcm_path, progress_cb=progress_cb)
        data_len = pcm_path.stat().st_size
        with wav_path.open("wb") as f_out:
            _write_wav_header(f_out, data_len, 44100, 2, 16)
            with pcm_path.open("rb") as f_in:
                shutil.copyfileobj(f_in, f_out)
    finally:
        pcm_path.unlink(missing_ok=True)
    if progress_cb is not None:
        progress_cb(track1_frames, track1_frames)
