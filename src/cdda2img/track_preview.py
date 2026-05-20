"""
track_preview.py — background audio preview for the rip pipeline.

Grabs track 1 of the disc via cd-paranoia to a temporary WAV, then plays it on
a loop in the background (ffplay) while the rest of the rip runs — through the
metadata menu, loudness analysis and container build, until the rip ends.

Purely cosmetic: every failure path is swallowed so a rip is never affected.
Because there is a single optical drive, the track-1 grab must complete before
the main cdrdao rip starts; playback then overlaps everything that follows.

Public interface:
    preview = start_preview(device, work_dir, progress_cb=...) -> TrackPreview | None
    preview.stop()
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

_BYTES_PER_FRAME = 2352  # one CD sector / frame (588 stereo s16 sample-pairs)
_WAV_HEADER = 44  # canonical PCM WAV header cd-paranoia writes before the data
_POLL_S = 0.15  # file-size poll interval while grabbing track 1
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
    if shutil.which("cd-paranoia") is None or shutil.which("ffplay") is None:
        log.info("track preview skipped — cd-paranoia or ffplay not installed")
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
    from cdda2img.disc_reader import query_disc

    _, _, tracks = query_disc(device)
    track1_frames = tracks[0][2]  # (track_number, first_lsn, length_frames)

    wav_path = work_dir / _PREVIEW_WAV
    _grab_track1(device, wav_path, track1_frames, progress_cb)

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
    track1_frames: int,
    progress_cb: Callable[[int, int], None] | None,
) -> None:
    """Rip track 1 to *wav_path* via cd-paranoia.

    Progress is derived by polling the growing WAV file size against the known
    track length — robust and tool-agnostic, unlike parsing cd-paranoia's
    per-sector progress display. ``-Z`` disables paranoia: this is a throwaway
    preview, so speed beats jitter correction.
    """
    cmd = ["cd-paranoia", "-d", device, "-Z", "--", "1", str(wav_path)]
    proc = subprocess.Popen(  # noqa: S603  # LINT-012
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    while proc.poll() is None:
        if progress_cb is not None:
            done = wav_path.stat().st_size if wav_path.exists() else 0
            done_frames = max(0, done - _WAV_HEADER) // _BYTES_PER_FRAME
            progress_cb(min(done_frames, track1_frames), track1_frames)
        time.sleep(_POLL_S)
    if proc.returncode != 0:
        msg = f"cd-paranoia track-1 grab exited with code {proc.returncode}"
        raise RuntimeError(msg)
    if progress_cb is not None:
        progress_cb(track1_frames, track1_frames)
