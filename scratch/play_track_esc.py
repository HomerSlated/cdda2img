#!/usr/bin/env python3
"""Prototype: rip the first CD track and play it; press ESC to cancel.

Background thread: cd-paranoia → temp WAV → pw-play
Main thread:       cbreak keyboard capture, ESC terminates both.
"""

import select
import subprocess
import sys
import tempfile
import termios
import threading
import tty
from pathlib import Path

DEVICE = "/dev/sr0"
TRACK = 1

_stop = threading.Event()
_done = threading.Event()
_proc_lock = threading.Lock()
_proc: subprocess.Popen | None = None


def _set_proc(p: subprocess.Popen | None) -> None:
    global _proc
    with _proc_lock:
        _proc = p


def _kill_proc() -> None:
    with _proc_lock:
        p = _proc
    if p is not None and p.poll() is None:
        p.terminate()
        p.wait()


def _worker(wav_path: Path) -> None:
    try:
        print(f"Ripping track {TRACK} from {DEVICE}...")
        rip = subprocess.Popen(
            ["cd-paranoia", "-d", DEVICE, "-q", str(TRACK), str(wav_path)],
        )
        _set_proc(rip)
        rip.wait()
        _set_proc(None)

        if _stop.is_set() or rip.returncode != 0:
            return

        size_mb = wav_path.stat().st_size / 1024 / 1024
        print(f"  Ripped {size_mb:.1f} MiB  Playing...")

        play = subprocess.Popen(["pw-play", str(wav_path)], stderr=subprocess.DEVNULL)
        _set_proc(play)
        play.wait()
        _set_proc(None)

        if not _stop.is_set():
            print("Done.")
    finally:
        _done.set()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="cdplay_") as tmpdir:
        wav_path = Path(tmpdir) / f"track{TRACK:02d}.wav"

        t = threading.Thread(target=_worker, args=(wav_path,), daemon=True)
        t.start()

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        print("Press ESC to stop.")
        try:
            tty.setcbreak(fd)
            while not _done.is_set():
                readable, _, _ = select.select([sys.stdin], [], [], 0.1)
                if readable:
                    ch = sys.stdin.read(1)
                    if (
                        ch == "\x1b"
                    ):  # ESC (arrow keys also prefix with \x1b — fine for prototype)
                        _stop.set()
                        _kill_proc()
                        break
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

        t.join()


if __name__ == "__main__":
    main()
