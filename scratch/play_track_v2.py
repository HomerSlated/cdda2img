#!/usr/bin/env python3
"""Prototype: parallel rip/play/re-rip with braille spinner and ESC cancel.

Thread 1 (spinner):  braille frames on stdout until _exit fires
Thread 2 (rip1):     cd-paranoia → temp WAV; signals _rip1_done on completion
Thread 3 (play):     waits for _rip1_done, then pw-play temp WAV
Thread 4 (rip2):     waits for _rip1_done, second read pass (discarded); sets _exit on completion
Main     (keyboard): cbreak stdin, ESC sets _exit
Exit condition:      rip2 completion OR ESC
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
SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_POLL = 0.05  # seconds between subprocess poll ticks

_exit = threading.Event()
_rip1_done = threading.Event()


def _run_until_exit(proc: subprocess.Popen) -> None:
    """Poll *proc*; terminate it early if _exit fires before natural completion."""
    while proc.poll() is None:
        if _exit.wait(_POLL):
            proc.terminate()
            proc.wait()
            return


def _spinner() -> None:
    i = 0
    while not _exit.is_set():
        sys.stdout.write(f"\r{SPINNER_FRAMES[i % len(SPINNER_FRAMES)]}")
        sys.stdout.flush()
        _exit.wait(0.1)
        i += 1
    sys.stdout.write("\r \r")
    sys.stdout.flush()


def _rip1(wav_path: Path) -> None:
    try:
        proc = subprocess.Popen(
            ["cd-paranoia", "-d", DEVICE, "-q", str(TRACK), str(wav_path)],
            stderr=subprocess.DEVNULL,
        )
        _run_until_exit(proc)
    finally:
        _rip1_done.set()  # unblock play + rip2 even if cancelled


def _play(wav_path: Path) -> None:
    _rip1_done.wait()
    if _exit.is_set():
        return
    proc = subprocess.Popen(["pw-play", str(wav_path)], stderr=subprocess.DEVNULL)
    _run_until_exit(proc)


def _rip2() -> None:
    _rip1_done.wait()
    if _exit.is_set():
        return
    proc = subprocess.Popen(
        ["cd-paranoia", "-d", DEVICE, "-r", "-q", str(TRACK), "-"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _run_until_exit(proc)
    if not _exit.is_set():
        _exit.set()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="cdplay_") as tmpdir:
        wav_path = Path(tmpdir) / f"track{TRACK:02d}.wav"

        threads = [
            threading.Thread(target=_spinner, daemon=True),
            threading.Thread(target=_rip1, args=(wav_path,), daemon=True),
            threading.Thread(target=_play, args=(wav_path,), daemon=True),
            threading.Thread(target=_rip2, daemon=True),
        ]
        for t in threads:
            t.start()

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not _exit.is_set():
                readable, _, _ = select.select([sys.stdin], [], [], 0.1)
                if readable:
                    if sys.stdin.read(1) == "\x1b":
                        _exit.set()
                        break
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

        for t in threads:
            t.join()


if __name__ == "__main__":
    main()
