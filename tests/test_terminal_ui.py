"""Unit tests for TerminalUI's header region and cursor arithmetic.

``_build`` is a pure render method, so we exercise it directly on a bare
instance (``object.__new__``) — the real ``__init__`` needs a tty fd
(``termios.tcgetattr``), which pytest's captured stdin does not provide.
"""

import threading

from cdda2img.terminal_ui import TerminalUI, _St


def _bare_ui() -> TerminalUI:
    ui = object.__new__(TerminalUI)
    ui._slk = threading.Lock()
    ui._lock = threading.Lock()
    ui._tick = threading.Event()
    ui._st = _St.STOPPED
    ui._status = "Ripping"
    ui._prog = 0.5
    ui._detail = ""
    ui._header = []
    ui._output = []
    ui._prev_height = 0
    return ui


def test_header_renders_above_progress_line():
    ui = _bare_ui()
    ui._header = ["   Drive:       PLEXTOR", "   Disc:        Eliminator - ZZ Top"]
    lines = ui._build(0, 0).split("\n")
    assert lines[0] == "   Drive:       PLEXTOR"
    assert lines[1] == "   Disc:        Eliminator - ZZ Top"
    assert "Ripping" in lines[2]  # progress line comes after the header
    assert ui._prev_height == 3  # 2 header + 1 progress


def test_header_counts_into_reposition_rewind():
    # A previous 3-line frame must be rewound by prev_height-1 lines.
    ui = _bare_ui()
    ui._prev_height = 3
    ui._header = ["a", "b"]
    out = ui._build(0, 0)
    assert out.startswith("\033[2A\r\033[J")
    assert ui._prev_height == 3  # 2 header + 1 progress


def test_set_header_replaces_and_is_isolated():
    ui = _bare_ui()
    src = ["x", "y"]
    ui.set_header(src)
    assert ui._header == ["x", "y"]
    src.append("z")  # mutating the caller's list must not leak in
    assert ui._header == ["x", "y"]
    ui.set_header([])  # empty clears the region
    assert ui._header == []


def test_no_header_is_unchanged_behaviour():
    ui = _bare_ui()
    lines = ui._build(0, 0).split("\n")
    assert "Ripping" in lines[0]  # progress line is first when no header
    assert ui._prev_height == 1
