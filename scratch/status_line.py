#!/usr/bin/env python3
"""Prototype: single-line status TUI — spinner + status text + progress bar.

Simulates a realistic pipeline to exercise the rendering.
ESC or natural completion exits.

Layout (one overwritten line):
    ⠹  Ripping track 3/12 ████████░░░░░░░░  42%
    │  │                  │                │
    │  status             bar (fills gap)  pct (4 chars) or blank
    spinner + 2 spaces
"""

import select
import shutil
import sys
import termios
import threading
import tty

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_FULL = "█"
_EMPTY = "░"

_status = "Starting…"
_progress = 0.0  # 0.0–1.0 = determinate; negative = indeterminate
_lock = threading.Lock()
_exit = threading.Event()


def set_status(text: str, progress: float = 0.0) -> None:
    with _lock:
        global _status, _progress
        _status = text
        _progress = progress


def _build_line(frame: int, ind_pos: int) -> str:
    with _lock:
        status = _status
        progress = _progress

    cols = shutil.get_terminal_size().columns - 1
    det = progress >= 0.0

    # Truncate status to at most one-third of the terminal
    max_status = max(8, cols // 3)
    if len(status) > max_status:
        status = status[: max_status - 1] + "…"

    # Fixed overhead: spinner(1) + "  "(2) + " "(1) + "  "(2) + pct(4) = 10
    bar_width = max(4, cols - 10 - len(status))
    spinner = SPINNER_FRAMES[frame % len(SPINNER_FRAMES)]
    pct_str = f"{int(progress * 100):>3}%" if det else "    "

    if det:
        filled = round(progress * bar_width)
        bar = _FULL * filled + _EMPTY * (bar_width - filled)
    else:
        seg = min(max(4, bar_width // 8), bar_width)
        span = bar_width - seg
        if span == 0:
            bar = _FULL * bar_width
        else:
            pos = ind_pos % (2 * span)
            pos = 2 * span - pos if pos > span else pos
            bar = _EMPTY * pos + _FULL * seg + _EMPTY * (bar_width - pos - seg)

    return f"\r{spinner}  {status} {bar}  {pct_str}"


def _renderer() -> None:
    frame = ind_pos = 0
    while not _exit.is_set():
        sys.stdout.write(_build_line(frame, ind_pos))
        sys.stdout.flush()
        _exit.wait(0.1)
        frame += 1
        ind_pos += 1
    cols = shutil.get_terminal_size().columns - 1
    sys.stdout.write("\r" + " " * cols + "\r")
    sys.stdout.flush()


def _simulate() -> None:
    """Fake pipeline stages to exercise the status line."""
    stages = [
        ("Reading disc TOC", 0.8, True),
        ("Ripping track  1/12", 3.5, True),
        ("Ripping track  2/12", 3.2, True),
        ("Ripping track  3/12", 2.9, True),
        ("Ripping track  4/12", 3.1, True),
        ("Looking up MusicBrainz", 1.5, False),
        ("Querying AcoustID", 1.0, False),
        ("Building container", 2.0, True),
        ("Writing catalogue entry", 0.5, True),
    ]
    steps = 40
    for label, duration, det in stages:
        if _exit.is_set():
            return
        if det:
            for i in range(steps + 1):
                if _exit.is_set():
                    return
                set_status(label, i / steps)
                _exit.wait(duration / steps)
        else:
            set_status(label, -1.0)
            _exit.wait(duration)
    _exit.set()


def main() -> None:
    renderer = threading.Thread(target=_renderer, daemon=True)
    sim = threading.Thread(target=_simulate, daemon=True)
    renderer.start()
    sim.start()

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

    renderer.join()
    sim.join()


if __name__ == "__main__":
    main()
