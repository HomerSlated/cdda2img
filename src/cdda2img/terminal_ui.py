"""Terminal state machine: single-line progress renderer with pause/resume.

TerminalUI owns:
  • The renderer thread  (spinner + status + progress bar + output region, 100ms refresh)
  • Terminal mode        (cbreak while RUNNING; cooked while PAUSED or STOPPED)

Synchronisation between threads uses two events:
  _tick — main thread wakes the renderer immediately on a state change,
          rather than waiting up to 100ms for it to wake on its own.
  _idle — renderer signals "I've finished my last write and am no longer
          touching stdout". The caller waits on this before drawing
          interactive content, eliminating the flicker race.

Caller API:
    ui = TerminalUI()
    ui.start()                              # enter cbreak, start renderer
    ui.set_status("Ripping…", 0.3,
                  detail="(54000/204143)")  # thread-safe progress update
    ui.add_output("  CDDB: matched …")     # append a line to the output region
    ui.clear_output()                       # clear the output region
    ui.pause()                              # stop renderer, restore cooked terminal
    # … interactive I/O …
    ui.resume()                             # re-enter cbreak, restart renderer
    ui.stop()                               # stop renderer permanently, restore terminal
    # or: use as context manager (start/stop automatically)

Output region:
    Lines added via add_output() appear below the progress line. The renderer
    redraws the whole area each frame using ANSI cursor-up + erase-to-bottom,
    so lines stay in a fixed position on screen. _prev_height tracks how many
    lines were drawn last frame so the cursor can rewind correctly.
"""

from __future__ import annotations

import shutil
import sys
import termios
import threading
import tty
from enum import Enum, auto

SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_FULL = "█"
_EMPTY = "░"
_MAX_OUTPUT_LINES = 20


class _St(Enum):
    RUNNING = auto()
    PAUSED = auto()
    STOPPED = auto()


class TerminalUI:
    def __init__(self) -> None:
        self._st = _St.STOPPED
        self._lock = threading.Lock()
        self._tick = threading.Event()  # wakes renderer on state change
        self._idle = threading.Event()  # renderer signals it is no longer writing

        self._slk = threading.Lock()
        self._status = ""
        self._prog = 0.0
        self._detail = ""
        self._output: list[str] = []

        # Number of lines written in the last render frame (renderer thread only).
        # Used by _clear_region() to rewind the cursor to the top of the TUI area.
        self._prev_height = 0

        self._fd = sys.stdin.fileno()
        self._saved = termios.tcgetattr(self._fd)
        self._thread: threading.Thread | None = None

    # ── public API ───────────────────────────────────────────────────────────

    def start(self) -> TerminalUI:
        tty.setcbreak(self._fd)
        with self._lock:
            self._st = _St.RUNNING
        self._idle.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        with self._lock:
            if self._st == _St.STOPPED:
                return
            self._st = _St.STOPPED
        self._tick.set()  # wake renderer from its sleep or its PAUSED wait
        self._idle.wait()  # wait until renderer has written its last byte
        if self._thread:
            self._thread.join()
            self._thread = None
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)

    def pause(self) -> None:
        """Stop the renderer and restore the terminal to cooked mode.

        Blocks until the renderer has confirmed it has stopped writing.
        Safe to call from any thread.
        """
        with self._lock:
            self._st = _St.PAUSED
        self._tick.set()  # wake renderer immediately
        self._idle.wait()  # wait for renderer to reach its idle point
        self._idle.clear()  # arm for next pause or stop
        self._clear_region()
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)

    def resume(self) -> None:
        """Re-enter cbreak and restart the renderer."""
        tty.setcbreak(self._fd)
        with self._lock:
            self._st = _St.RUNNING
        self._tick.set()  # kick the renderer out of its PAUSED wait

    def set_status(self, text: str, progress: float = 0.0, detail: str = "") -> None:
        """Update the progress line. *detail* (e.g. "(54000/204143)") appears after the %."""
        with self._slk:
            self._status = text
            self._prog = progress
            self._detail = detail

    def add_output(self, text: str) -> None:
        """Append *text* (may contain newlines) to the output region below the progress line."""
        lines = text.splitlines() if "\n" in text else [text]
        with self._slk:
            self._output.extend(lines)
            if len(self._output) > _MAX_OUTPUT_LINES:
                del self._output[:-_MAX_OUTPUT_LINES]
        with self._lock:
            if self._st == _St.RUNNING:
                self._tick.set()

    def clear_output(self) -> None:
        """Clear the output region below the progress line."""
        with self._slk:
            self._output.clear()
        with self._lock:
            if self._st == _St.RUNNING:
                self._tick.set()

    def is_paused(self) -> bool:
        with self._lock:
            return self._st == _St.PAUSED

    def is_stopped(self) -> bool:
        with self._lock:
            return self._st == _St.STOPPED

    def __enter__(self) -> TerminalUI:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.stop()

    # ── renderer thread ───────────────────────────────────────────────────────

    def _loop(self) -> None:
        frame = ind = 0
        while True:
            with self._lock:
                st = self._st

            if st == _St.STOPPED:
                self._clear_region()
                self._idle.set()  # tell caller we're done writing
                return

            if st == _St.PAUSED:
                self._idle.set()  # tell caller we've stopped writing
                self._tick.wait()  # sleep until resume() or stop() kicks us
                self._tick.clear()
                continue  # re-check state at top of loop

            # RUNNING — write one frame
            sys.stdout.write(self._build(frame, ind))
            sys.stdout.flush()
            self._tick.wait(0.1)  # sleep 100ms, or wake immediately on kick
            self._tick.clear()
            frame += 1
            ind += 1

    # ── rendering ─────────────────────────────────────────────────────────────

    def _build(self, frame: int, ind: int) -> str:
        with self._slk:
            status = self._status
            prog = self._prog
            detail = self._detail
            output = list(self._output)

        cols = shutil.get_terminal_size().columns - 1
        det = prog >= 0.0

        max_s = max(8, cols // 3)
        if len(status) > max_s:
            status = status[: max_s - 1] + "…"

        # Fixed overhead per line:
        #   spinner(1) + "  "(2) + " "(1) + "  "(2) + pct(6) = 12
        #   + optional "   "(3) + detail chars
        detail_w = (3 + len(detail)) if detail else 0
        bw = max(4, cols - 12 - len(status) - detail_w)
        sp = SPINNER[frame % len(SPINNER)]
        pct = f"{min(prog, 1.0) * 100:5.1f}%" if det else "      "

        if det:
            n = round(prog * bw)
            bar = _FULL * n + _EMPTY * (bw - n)
        else:
            seg = min(max(4, bw // 8), bw)
            span = bw - seg
            if span == 0:
                bar = _FULL * bw
            else:
                pos = ind % (2 * span)
                pos = 2 * span - pos if pos > span else pos
                bar = _EMPTY * pos + _FULL * seg + _EMPTY * (bw - pos - seg)

        pct_part = f"{pct}   {detail}" if detail else pct
        progress_line = f"{sp}  {status} {bar}  {pct_part}"

        all_lines = [progress_line, *output]
        new_height = len(all_lines)

        # Rewind cursor to the top of the TUI area, then erase to screen bottom.
        ph = self._prev_height
        if ph == 0:
            reposition = ""
        elif ph == 1:
            reposition = "\r\033[J"
        else:
            reposition = f"\033[{ph - 1}A\r\033[J"

        self._prev_height = new_height
        return reposition + "\n".join(all_lines)

    def _clear_region(self) -> None:
        """Erase the entire TUI area and reset cursor to the start of the progress line."""
        ph = self._prev_height
        if ph > 1:
            sys.stdout.write(f"\033[{ph - 1}A\r\033[J")
        elif ph == 1:
            sys.stdout.write("\r\033[J")
        # ph == 0: nothing was rendered; nothing to clear
        sys.stdout.flush()
        self._prev_height = 0
