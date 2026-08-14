"""N8 — a log record emitted while the TUI is live must not strand a frame.

The mechanism (measured under a pty 2026-08-10): ``TerminalUI._frame`` repaints
by rewinding ``_prev_height - 1`` lines and erasing to the screen bottom, where
``_prev_height`` is the TUI's model of its own region. Any write it did not make
moves the cursor without updating that model, so the next rewind lands short and
the previous frame is never erased — one stranded progress bar per stray write.

**What makes the assertion here discriminating.** ``_frame`` legitimately emits
both ``"\\r\\x1b[J"`` (when it drew one line) and ``"\\x1b[<n>A\\r\\x1b[J"`` (when it
drew more), so grepping for the short form matches healthy frames too. The
invariant that actually separates the two cases is the *rewind distance* against
the number of lines really on screen: a stray write adds newlines the TUI never
counted, so its next rewind is short by exactly that many.

Every test that asserts the invariant is paired with a negative control running
the same harness over a raw ``sys.stderr`` write, because an assertion that
cannot fail proves nothing about the one that passes.
"""

from __future__ import annotations

import io
import logging
import os
import pty
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from cdda2img import terminal_ui

_REWIND = re.compile(r"\x1b\[(\d+)A\r\x1b\[J|\r\x1b\[J")


def _frames(stream: str) -> list[tuple[int, str]]:
    """Split a captured TUI stream into ``(rewind_distance, payload)`` frames.

    The first frame has no rewind and is reported as distance 0.
    """
    out: list[tuple[int, str]] = []
    pos, rewind = 0, 0
    for m in _REWIND.finditer(stream):
        out.append((rewind, stream[pos : m.start()]))
        rewind = int(m.group(1)) if m.group(1) else 0
        pos = m.end()
    out.append((rewind, stream[pos:]))
    return out


def _first_short_rewind(stream: str) -> tuple[int, int] | None:
    """``(expected, actual)`` for the first frame whose rewind undershoots.

    A frame that drew N screen lines must be rewound by N-1 to land back on its
    own first line. Counting newlines in the payload counts what was *really*
    written, including anything the TUI did not know about.
    """
    frames = _frames(stream)
    for (_, payload), (rewind, _) in zip(frames, frames[1:]):
        on_screen = payload.count("\n") + 1
        if rewind != on_screen - 1:
            return on_screen - 1, rewind
    return None


_HARNESS = r"""
import logging, sys, time
sys.path.insert(0, {src!r})
from cdda2img.terminal_ui import TerminalUI, TuiLogHandler

mode = sys.argv[1]
log = logging.getLogger("harness")
root = logging.getLogger()
root.setLevel(logging.WARNING)
if mode == "handler":
    h = TuiLogHandler()
    h.setFormatter(logging.Formatter("  %(levelname)s: %(message)s"))
    root.addHandler(h)
# mode == "stray": no handler, so logging.lastResort writes to stderr — the
# pre-fix behaviour, and the negative control.

ui = TerminalUI().start()
ui.set_status("Ripping", 0.25)
time.sleep(0.35)
log.warning("a stray record")
time.sleep(0.35)
ui.set_status("Ripping", 0.50)
time.sleep(0.35)
ui.stop()
"""


def _run_under_pty(mode: str) -> str:
    """Run the harness on a real pty and return everything it wrote."""
    src = str(Path(__file__).resolve().parents[1] / "src")
    script = _HARNESS.format(src=src)
    controller, worker = pty.openpty()
    try:
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", script, mode],
            stdin=worker,
            stdout=worker,
            stderr=worker,
            close_fds=True,
        )
        os.close(worker)
        chunks = []
        while True:
            try:
                data = os.read(controller, 65536)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
        proc.wait(timeout=30)
    finally:
        os.close(controller)
    return b"".join(chunks).decode("utf-8", "replace")


needs_pty = pytest.mark.skipif(
    not hasattr(pty, "openpty"), reason="no pty on this platform"
)


@needs_pty
def test_negative_control_a_stray_write_strands_a_frame() -> None:
    """The probe must be able to fail, or its passing counterpart proves nothing.

    With no handler installed, `logging.lastResort` — a WARNING-level stderr
    handler — writes the record straight past the TUI. That is exactly the
    default-verbosity behaviour before N8, so this is the bug itself, reproduced.
    """
    stream = _run_under_pty("stray")
    assert "a stray record" in stream, "harness did not emit the record"
    assert _first_short_rewind(stream) is not None, (
        "expected a short rewind from the stray write, found none — "
        "the harness is no longer reproducing the defect"
    )


@needs_pty
def test_a_record_routed_through_the_handler_leaves_the_repaint_intact() -> None:
    """The same harness with `TuiLogHandler` installed: the record goes through
    `add_output`, so it is part of the frame the TUI counted."""
    stream = _run_under_pty("handler")
    assert "a stray record" in stream, "record was lost, not merely rerouted"
    short = _first_short_rewind(stream)
    assert short is None, (
        f"rewind undershot: expected {short and short[0]}, got {short and short[1]}"
    )


# ── the handler in isolation ─────────────────────────────────────────────────


class _FakeUI:
    """Only the two methods the handler touches."""

    def __init__(self, paused: bool = False) -> None:
        self.lines: list[str] = []
        self._paused = paused

    def add_output(self, text: str) -> None:
        self.lines.append(text)

    def is_paused(self) -> bool:
        return self._paused


def _record(msg: str = "hello", level: int = logging.WARNING) -> logging.LogRecord:
    return logging.LogRecord("t", level, __file__, 1, msg, None, None)


@pytest.fixture
def _clean_root():
    """Save and restore global logging state — these tests mutate it by design."""
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    yield root
    root.handlers[:] = saved
    root.setLevel(level)


def test_a_running_tui_gets_the_record(monkeypatch) -> None:
    ui = _FakeUI()
    monkeypatch.setattr(terminal_ui, "_ACTIVE", ui)
    h = terminal_ui.TuiLogHandler()
    h.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

    h.emit(_record("q stream unusable"))

    assert ui.lines == ["WARNING: q stream unusable"]


def test_no_tui_falls_through_to_the_stream(monkeypatch) -> None:
    monkeypatch.setattr(terminal_ui, "_ACTIVE", None)
    buf = io.StringIO()
    h = terminal_ui.TuiLogHandler(stream=buf)
    h.setFormatter(logging.Formatter("%(message)s"))

    h.emit(_record("no tui here"))

    assert buf.getvalue() == "no tui here\n"


def test_a_paused_tui_falls_through_to_the_stream(monkeypatch) -> None:
    """`pause()` clears the region and zeroes `_prev_height`, so the terminal is
    genuinely the caller's again and a write cannot corrupt anything. Holding the
    record back until resume would reorder it against the interactive output it
    belongs beside."""
    ui = _FakeUI(paused=True)
    monkeypatch.setattr(terminal_ui, "_ACTIVE", ui)
    buf = io.StringIO()
    h = terminal_ui.TuiLogHandler(stream=buf)
    h.setFormatter(logging.Formatter("%(message)s"))

    h.emit(_record("during a menu"))

    assert ui.lines == []
    assert buf.getvalue() == "during a menu\n"


def test_a_ui_that_logs_from_add_output_does_not_recurse(monkeypatch) -> None:
    """`add_output` does not log, but it sets `_tick`, which wakes the renderer —
    so anything the renderer touches that logs re-enters here. The guard is
    thread-local so one thread's recursion cannot mute another thread's records.
    """
    h = terminal_ui.TuiLogHandler(stream=io.StringIO())
    h.setFormatter(logging.Formatter("%(message)s"))
    depth = {"n": 0, "max": 0}

    class _Recursive(_FakeUI):
        def add_output(self, text: str) -> None:
            depth["n"] += 1
            depth["max"] = max(depth["max"], depth["n"])
            h.emit(_record("re-entered"))
            depth["n"] -= 1
            self.lines.append(text)

    monkeypatch.setattr(terminal_ui, "_ACTIVE", _Recursive())
    h.emit(_record("outer"))

    assert depth["max"] == 1, "the guard did not stop re-entry"


def test_handle_error_writes_nothing(monkeypatch, capsys) -> None:
    """The default `handleError` prints a traceback to stderr — which is the very
    stray write this class exists to prevent, arriving at the worst moment."""

    class _Exploding(_FakeUI):
        def add_output(self, text: str) -> None:
            raise RuntimeError("boom")

    monkeypatch.setattr(terminal_ui, "_ACTIVE", _Exploding())
    h = terminal_ui.TuiLogHandler()
    h.setFormatter(logging.Formatter("%(message)s"))

    h.emit(_record("x"))  # must not raise

    assert capsys.readouterr().err == ""


def test_stop_only_releases_its_own_claim(monkeypatch) -> None:
    """Two TUIs are not a supported arrangement, but clearing unconditionally
    would let a stopped one silently mute a live one's records."""
    live = _FakeUI()
    monkeypatch.setattr(terminal_ui, "_ACTIVE", live)
    other = terminal_ui.TerminalUI.__new__(terminal_ui.TerminalUI)
    other._lock = threading.Lock()
    other._st = terminal_ui._St.STOPPED

    other.stop()

    assert terminal_ui.active_ui() is live


def test_the_entry_point_installs_exactly_one_handler(_clean_root) -> None:
    """Being the only handler is the mechanism, not a detail: a second one
    writing the same record to stderr would strand a frame whatever this one
    did. Installing any handler also retires `logging.lastResort`, which fires
    only on an empty handler list."""
    from cdda2img.cdda2img import _install_log_handler

    _clean_root.handlers[:] = []
    _install_log_handler(verbose=False)

    assert len(_clean_root.handlers) == 1
    assert isinstance(_clean_root.handlers[0], terminal_ui.TuiLogHandler)


def test_verbose_changes_the_level_not_the_route(_clean_root) -> None:
    """`--verbose` never reaches the pipelines — it is consumed at `main()` — so
    it sets the root level and the format here, and where records go stays the
    handler's single decision."""
    from cdda2img.cdda2img import _install_log_handler

    _clean_root.handlers[:] = []
    _install_log_handler(verbose=True)

    assert _clean_root.level == logging.DEBUG
    assert len(_clean_root.handlers) == 1
    assert isinstance(_clean_root.handlers[0], terminal_ui.TuiLogHandler)


def test_default_verbosity_leaves_the_root_level_alone(_clean_root) -> None:
    """WARNING is the level `logging.lastResort` already filtered at, so the set
    of records reaching the terminal at default verbosity is unchanged — only
    their route is."""
    from cdda2img.cdda2img import _install_log_handler

    _clean_root.handlers[:] = []
    _clean_root.setLevel(logging.WARNING)
    _install_log_handler(verbose=False)

    assert _clean_root.level == logging.WARNING
