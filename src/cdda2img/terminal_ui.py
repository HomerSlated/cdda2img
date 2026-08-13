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

from cdda2img import disc_map

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
        self._header: list[str] = []
        self._output: list[str] = []

        # Disc map. _map is the live per-sector C2 damage buffer owned by the
        # reader; the rest is geometry pinned once, for the reason in _build().
        self._map: bytearray | None = None
        self._map_q: bytearray | None = None
        self._map_active: tuple[int, int] | None = None
        self._map_cols = 0
        self._map_sw = 0
        self._map_dw = 0
        self._map_colour = False

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

    def set_map(
        self,
        damage: bytearray | None,
        *,
        subq: bytearray | None = None,
        status_width: int = 0,
        active: tuple[int, int] | None = None,
    ) -> None:
        """Make the progress bar *be* the disc map, or put the plain bar back.

        *damage* and *subq* are the reader's live per-sector lanes — one byte per
        sector, set as sectors are read. They are passed by reference and polled
        by the renderer; they are never copied, so they must stay valid until
        ``set_map(None)``. *subq* is ``None`` when the engine cannot supply a Q
        verdict, and the map then draws one lane rather than drawing Q healthy.

        *status_width* is the widest status text this read will ever show, and
        the caller must supply it because only the caller knows the phases. It
        cannot be measured from the current text: **every width on the map line
        is pinned for the life of the read**, so whichever phase happened to be
        showing at the first frame would otherwise fix the column count forever
        and truncate every longer one.

        *active* is a ``[lo, hi)`` sector range under active repair — the AR
        recovery ladder's current track window. Passing it does two things, and
        the second is the one that makes the first work: those cells draw as
        ``REREADING``, **and the frontier is taken as the whole map** rather than
        from ``prog``. During recovery ``prog`` measures progress through one
        *track*, so the ordinary ``frontier = prog * len(damage)`` would collapse
        the whole-disc map to a sliver and redraw it from the left on every
        attempt — exactly the "bar that restarts per attempt" this replaces.

        Safe without a lock, and deliberately so: the reader is the only writer,
        each byte is written once, and the renderer only ever reads bytes below
        the frontier it was told about. A frame caught mid-chunk is a frame that
        renders slightly less of the disc, which is what "in progress" means.
        The recovery caller is a second writer, but it writes only between
        attempts (never during a read) and only zeroes — a frame catching a
        half-cleared track shows a repair partly done, which it is.
        """
        with self._slk:
            self._map = damage
            self._map_q = subq
            self._map_active = active
            # Geometry is pinned on the NEXT frame, once the terminal width is
            # known. Reset here so a second read re-pins rather than inheriting
            # the first one's layout.
            self._map_cols = 0
            self._map_colour = disc_map.colour_enabled()
            self._map_dw = (2 * len(str(len(damage))) + 3) if damage else 0
            self._map_sw = status_width
        with self._lock:
            if self._st == _St.RUNNING:
                self._tick.set()

    def set_header(self, lines: list[str]) -> None:
        """Replace the fixed header region rendered *above* the progress line.

        Unlike add_output() (which appends below the spinner), the header sits at
        the top of the TUI area and is repainted every frame, so a caller can
        update a header line live — e.g. fill in the disc title once a background
        lookup returns. Note the header is part of the TUI region and is cleared
        on pause()/stop(), so it is for transient, during-run context only.
        """
        with self._slk:
            self._header = list(lines)
        with self._lock:
            if self._st == _St.RUNNING:
                self._tick.set()

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
            header = list(self._header)
            output = list(self._output)
            damage = self._map
            damage_q = self._map_q
            map_active = self._map_active
            map_cols = self._map_cols
            map_sw = self._map_sw
            map_dw = self._map_dw
            map_colour = self._map_colour

        cols = shutil.get_terminal_size().columns - 1
        det = prog >= 0.0

        max_s = max(8, cols // 3)
        if len(status) > max_s:
            status = status[: max_s - 1] + "…"

        if damage is not None and det:
            progress_line = self._build_map(
                sp=SPINNER[frame % len(SPINNER)],
                status=status,
                prog=prog,
                detail=detail,
                cols=cols,
                damage=damage,
                damage_q=damage_q,
                active=map_active,
                map_cols=map_cols,
                map_sw=map_sw,
                map_dw=map_dw,
                colour=map_colour,
            )
            return self._frame(header, progress_line, output)

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
        return self._frame(header, progress_line, output)

    def _build_map(
        self,
        *,
        sp: str,
        status: str,
        prog: float,
        detail: str,
        cols: int,
        damage: bytearray,
        damage_q: bytearray | None,
        active: tuple[int, int] | None,
        map_cols: int,
        map_sw: int,
        map_dw: int,
        colour: bool,
    ) -> str:
        """The progress line rendered as a disc map.

        **Every width on this line is pinned for the life of the read**, and that
        is the whole design, not tidiness. A cell's sector span is
        ``len(damage) // width``, so a one-column change re-buckets every cell
        and already-drawn damage jumps to a different column — the map appears to
        rewrite its own history. The plain bar is immune (one number, redrawn),
        which is why the layout could safely float before and cannot now.

        Two things move a width here and both are ordinary: the sector counter
        gains a digit (``(99999/204143)`` → ``(100000/204143)``), and the
        terminal is resized mid-rip. The first is pinned away by sizing *detail*
        to the largest value it can ever hold; the second cannot be, so a
        narrowed terminal **clips cells off the right** rather than re-bucketing.
        Clipping loses the least: the map's content is left-weighted, and the
        frontier is also reported numerically as a percentage and a count.
        """
        if map_cols <= 0:
            # First frame with a map: pin the geometry against this terminal.
            # map_sw comes from set_map() — the WIDEST text this read can show,
            # not the one that happens to be on screen now. Measuring it here
            # would pin the column count to whichever phase won the race.
            map_sw = max(map_sw, len(status))
            map_cols = max(4, cols - 12 - map_sw - (3 + map_dw))
            with self._slk:
                self._map_cols = map_cols
                self._map_sw = map_sw

        avail = max(0, cols - 12 - map_sw - (3 + map_dw))
        visible = max(0, min(map_cols, avail))
        # An active repair region means the first pass is long finished, so the
        # frontier is the whole map: `prog` now measures one TRACK and would
        # otherwise shrink the disc to a sliver and redraw it per attempt.
        frontier = len(damage) if active else round(prog * len(damage))
        # Each lane gets its OWN severity calibration. C2's healthy baseline is
        # zero; Q's is a few per cent of CRC-bad frames on a perfectly good disc.
        # Sharing one table painted a clean Tracy Chapman entirely orange.
        cells = disc_map.cells_from_damage(damage, frontier, map_cols, active=active)
        q_cells = (
            disc_map.cells_from_damage(
                damage_q,
                frontier,
                map_cols,
                bands=disc_map.SUBQ_RAMP_BANDS,
                active=active,
            )
            if damage_q is not None
            else None
        )
        bar = disc_map.render(
            cells[:visible],
            colour=colour,
            q_cells=None if q_cells is None else q_cells[:visible],
        )
        pct = f"{min(prog, 1.0) * 100:5.1f}%"
        return f"{sp}  {status:<{map_sw}.{map_sw}s} {bar}  {pct}   {detail:<{map_dw}}"

    def _frame(self, header: list[str], progress_line: str, output: list[str]) -> str:
        """Wrap the rendered lines in the cursor motion that repaints in place."""
        all_lines = [*header, progress_line, *output]
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
