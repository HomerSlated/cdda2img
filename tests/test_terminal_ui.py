"""Unit tests for TerminalUI's header region and cursor arithmetic.

``_build`` is a pure render method, so we exercise it directly on a bare
instance (``object.__new__``) — the real ``__init__`` needs a tty fd
(``termios.tcgetattr``), which pytest's captured stdin does not provide.
"""

import os
import threading
from unittest import mock

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
    ui._map = None
    ui._map_q = None
    ui._map_active = None
    ui._map_cols = 0
    ui._map_sw = 0
    ui._map_dw = 0
    ui._map_colour = False
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


# ── the disc map replaces the bar ─────────────────────────────────────────────


def _map_ui(damage: bytearray, prog: float, cols: int = 100) -> tuple[TerminalUI, str]:
    ui = _bare_ui()
    ui._status = "Reading"
    ui._prog = prog
    ui._detail = f"({round(prog * len(damage))}/{len(damage)})"
    ui.set_map(damage)
    with mock.patch(
        "cdda2img.terminal_ui.shutil.get_terminal_size",
        return_value=os.terminal_size((cols, 24)),
    ):
        return ui, ui._build(0, 0)


def test_the_map_replaces_the_bar_when_one_is_set():
    damage = bytearray(1000)
    damage[400:500] = b"\x01" * 100
    _, line = _map_ui(damage, 1.0)
    assert "▒" in line  # damage is drawn
    assert "█" in line  # so is the healthy remainder
    assert "░" not in line  # nothing is unread at 100%


def test_unread_sectors_are_drawn_as_unread_not_as_clean():
    _, line = _map_ui(bytearray(1000), 0.25)
    assert "░" in line


def test_the_map_line_does_not_change_width_as_the_counter_gains_a_digit():
    """The bug the bench found, in its production form.

    ``detail`` goes from ``(99999/204143)`` to ``(100000/204143)`` mid-rip. If
    the line's widths float, that one extra character re-buckets every cell and
    already-drawn damage jumps a column. Sizing the detail field to the largest
    value it can ever hold pins it.
    """
    damage = bytearray(204143)
    widths = set()
    for done in (99999, 100000, 100001):
        ui = _bare_ui()
        ui._status = "Reading"
        ui._prog = done / len(damage)
        ui._detail = f"({done}/{len(damage)})"
        ui.set_map(damage)
        with mock.patch(
            "cdda2img.terminal_ui.shutil.get_terminal_size",
            return_value=os.terminal_size((100, 24)),
        ):
            widths.add(len(ui._build(0, 0)))
    assert len(widths) == 1


def test_a_narrower_terminal_clips_cells_rather_than_rebucketing():
    """Re-bucketing on resize would rewrite history; clipping only hides the
    right-hand end, which the percentage and the counter still report."""
    damage = bytearray(1000)
    ui, first = _map_ui(damage, 1.0, cols=100)
    pinned = ui._map_cols
    with mock.patch(
        "cdda2img.terminal_ui.shutil.get_terminal_size",
        return_value=os.terminal_size((60, 24)),
    ):
        narrow = ui._build(0, 0)
    assert ui._map_cols == pinned  # geometry did NOT re-pin
    assert narrow.count("█") < first.count("█")


def test_clearing_the_map_puts_the_plain_bar_back():
    ui, _ = _map_ui(bytearray(1000), 0.5)
    ui.set_map(None)
    with mock.patch(
        "cdda2img.terminal_ui.shutil.get_terminal_size",
        return_value=os.terminal_size((100, 24)),
    ):
        line = ui._build(0, 0)
    assert "▒" not in line
    assert "█" in line  # the plain bar's fill character


def test_a_mono_map_carries_no_escape_sequences(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    damage = bytearray(1000)
    damage[0:500] = b"\x01" * 500
    _, line = _map_ui(damage, 1.0)
    assert "\033[38;5;" not in line
    assert "▒" in line and "█" in line  # shape still separates the states
