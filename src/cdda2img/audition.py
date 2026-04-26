"""
Audio auditioning tool — compare unprocessed, normalised, and ReplayGain samples.

Usage:
    uv run python -m cdda2img.audition <audio_file>

Prepares three 10-second samples centred on the loudest passage, then lets you
audition each one interactively.

Keys:
    1 / 2 / 3   Play the corresponding sample (interrupts current playback)
    p           Pause / resume
    q           Quit  (prints the full path of the last file played)
"""

from __future__ import annotations

import select
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import tty
from pathlib import Path

import av
import numpy as np
from ffmpeg_normalize import FFmpegNormalize

from cdda2img.replaygain import analyse as _rg_analyse
from cdda2img.replaygain import embed_rg_tags as _rg_embed_tags

PCM_RATE = 44_100
SAMPLE_DURATION = 10  # seconds
RG_REFERENCE = -18.0  # LUFS (ReplayGain 2.0 / ITU-R BS.1770-3)


# ---------------------------------------------------------------------------
# Audio analysis
# ---------------------------------------------------------------------------


def find_loudest_start(path: Path) -> float:
    """Decode *path* to mono and return the start time (s) of the 10-second
    window centred on the sample with the highest peak amplitude."""
    container = av.open(str(path))
    stream = container.streams.audio[0]
    resampler = av.AudioResampler(format="fltp", layout="mono", rate=PCM_RATE)
    chunks: list[np.ndarray] = []

    for packet in container.demux(stream):
        for frame in packet.decode():
            for rf in resampler.resample(frame):  # type: ignore[arg-type]  # LINT-002: audio stream yields AudioFrame; stubs over-broad
                chunks.append(rf.to_ndarray()[0])

    for rf in resampler.resample(None):  # flush
        chunks.append(rf.to_ndarray()[0])

    container.close()

    if not chunks:
        return 0.0

    audio = np.concatenate(chunks)
    window = SAMPLE_DURATION * PCM_RATE

    if len(audio) <= window:
        return 0.0

    peak_frame = int(np.argmax(np.abs(audio)))
    start = max(0, peak_frame - window // 2)
    start = min(start, len(audio) - window)
    return start / PCM_RATE


def _window_frames(
    in_c,
    in_stream,
    start: float,
    end: float,
    resampler: av.AudioResampler,  # in_c: av.InputContainer (av.container not re-exported by stubs)
):
    """Yield resampled audio frames from *in_c* in the half-open interval [start, end) seconds."""
    in_c.seek(int(start * 1_000_000))  # AV_TIME_BASE units (µs); lands on prior keyframe
    for packet in in_c.demux(in_stream):
        done = False
        for frame in packet.decode():
            t = frame.time
            if t is not None and t >= end:
                done = True
                break
            if t is None or t < start:
                continue
            yield from resampler.resample(frame)
        if done:
            break
    yield from resampler.resample(None)  # flush resampler after loop exits


def extract_clip(src: Path, start: float, dest: Path) -> None:
    """Cut SAMPLE_DURATION seconds from *src* at *start* into a FLAC with no metadata."""
    resampler = av.AudioResampler(format="s16", layout="stereo", rate=PCM_RATE)
    with av.open(str(src)) as in_c:
        in_stream = in_c.streams.audio[0]
        with av.open(str(dest), "w", format="flac") as out_c:
            out_stream = out_c.add_stream("flac", rate=PCM_RATE)
            for rf in _window_frames(in_c, in_stream, start, start + SAMPLE_DURATION, resampler):
                for out_packet in out_stream.encode(rf):
                    out_c.mux(out_packet)
            for out_packet in out_stream.encode(None):
                out_c.mux(out_packet)


def normalize_clip(src: Path, dest: Path) -> None:
    """Normalise *src* to RG_REFERENCE LUFS (EBU R128) and write FLAC with no metadata."""
    norm = FFmpegNormalize(
        normalization_type="ebu",
        target_level=RG_REFERENCE,
        auto_lower_loudness_target=True,
        keep_loudness_range_target=True,
        audio_codec="flac",
        extra_output_options=["-map_metadata", "-1"],
        progress=False,
    )
    norm.add_media_file(str(src), str(dest))
    norm.run_normalization()


# ---------------------------------------------------------------------------
# Playback — ffplay subprocess with SIGSTOP / SIGCONT for pause/resume
# ---------------------------------------------------------------------------


class Player:
    """Wraps an ffplay subprocess for interruptible, pausable file playback."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._paused = False
        self._last: Path | None = None

    @property
    def last(self) -> Path | None:
        """Path of the last file that was played (survives stop())."""
        return self._last

    def play(self, path: Path, gain_db: float = 0.0) -> None:
        """Start playback of *path*, applying an optional gain offset in dB."""
        self._stop_proc()
        self._last = path
        self._paused = False
        cmd = ["ffplay", "-nodisp", "-loop", "0", "-loglevel", "quiet"]
        if gain_db:
            cmd += ["-af", f"volume={gain_db:.2f}dB"]
        cmd.append(str(path))
        self._proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)  # noqa: S603  # LINT-008

    def toggle_pause(self) -> bool:
        """Pause if playing, resume if paused. Returns True if now paused."""
        if not self._proc or self._proc.poll() is not None:
            return False
        if self._paused:
            self._proc.send_signal(signal.SIGCONT)
            self._paused = False
        else:
            self._proc.send_signal(signal.SIGSTOP)
            self._paused = True
        return self._paused

    def is_active(self) -> bool:
        """True while the subprocess is alive (playing or paused)."""
        return self._proc is not None and self._proc.poll() is None

    def is_paused(self) -> bool:
        return self._paused

    def _stop_proc(self) -> None:
        if self._proc is not None:
            if self._paused:
                self._proc.send_signal(signal.SIGCONT)
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
            self._paused = False

    def stop(self) -> None:
        self._stop_proc()

    def __del__(self) -> None:
        self._stop_proc()


# ---------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------


def _getch(timeout: float = 0.25) -> str | None:
    """Read one character from stdin without echo, or None if *timeout* expires."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        return sys.stdin.read(1) if ready else None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _print_status(msg: str) -> None:
    print(f"  → {msg}")


def _handle_key(key: str, player: Player, options: dict[str, tuple[Path, float, str]]) -> bool:
    """Process one keypress. Returns True to signal quit."""
    if key in ("1", "2", "3"):
        path, gain, desc = options[key]
        player.play(path, gain_db=gain)
        _print_status(f"playing [{key}] {desc}  (looping)")
    elif key == "p":
        if player.is_active() or player.is_paused():
            now_paused = player.toggle_pause()
            _print_status("paused" if now_paused else "resumed")
    elif key in ("q", "\x03"):
        return True
    return False


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] in ("-h", "--help"):
        prog = "uv run python -m cdda2img.audition"
        print(f"Usage: {prog} <audio_file>")
        sys.exit(0)

    src = Path(sys.argv[1]).resolve()
    if not src.exists():
        print(f"Error: {src}: no such file")
        sys.exit(1)

    tmp = Path(tempfile.mkdtemp(prefix="cdda2img_audition_"))
    f_raw = tmp / "sample_unprocessed.flac"
    f_norm = tmp / "sample_normalized.flac"
    f_rg = tmp / "sample_replaygain.flac"

    print(f"\nSource : {src}")
    print(f"Samples: {tmp}\n")

    # --- Preparation phase ---
    print("Preparing samples:")

    print("  Scanning for loudest passage ...", end="", flush=True)
    start = find_loudest_start(src)
    print(f" {start:.1f} s")

    print("  Extracting 10-second clip      ...", end="", flush=True)
    extract_clip(src, start, f_raw)
    print(" done")

    print("  Normalising (EBU R128 -18 LUFS)...", end="", flush=True)
    normalize_clip(f_raw, f_norm)
    print(" done")

    print("  Computing ReplayGain 2.0       ...", end="", flush=True)
    rg_result = _rg_analyse([f_raw])
    track_rg = rg_result.tracks[0]
    shutil.copy2(f_raw, f_rg)
    _rg_embed_tags(rg_result, [f_rg])
    print(f" gain {track_rg.gain:+.2f} dB  peak {track_rg.peak:.4f}  LRA {track_rg.lra:.1f} LU")

    # Each entry: (path, gain_db_for_playback, display_description)
    options: dict[str, tuple[Path, float, str]] = {
        "1": (f_raw, 0.0, "unprocessed"),
        "2": (f_norm, 0.0, "normalised (EBU R128 -18 LUFS)"),
        "3": (f_rg, track_rg.gain, f"ReplayGain ({track_rg.gain:+.2f} dB applied)"),
    }

    print(f"""
  [1] {f_raw.name}
  [2] {f_norm.name}   — EBU R128 -18 LUFS
  [3] {f_rg.name}     — RG tags: TRACK_GAIN {track_rg.gain:+.2f} dB

  1/2/3 play · p pause/resume · q quit
""")

    player = Player()

    try:
        while True:
            key = _getch(timeout=0.25)
            if key is None:
                continue
            if _handle_key(key, player, options):
                break
    except KeyboardInterrupt:
        pass
    finally:
        player.stop()

    print()
    if player.last:
        print(player.last)


if __name__ == "__main__":
    main()
