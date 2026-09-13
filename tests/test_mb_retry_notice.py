"""The MusicBrainz retry notice: why a lookup is taking minutes, in 26 characters.

musicbrainzngs retries a timed-out or 5xx request 8 times inside ``_safe_read``,
sleeping 2..14 s between attempts, and that count is a private default with no
public setter. A request therefore takes 56 s to fail when every attempt gets a
503 (measured), and ~5 minutes when the connection stalls, and
until 2026-09-13 the only sign was a spinner. The library does log each retry, at
INFO, straight after a record naming the cause; ``mb_lookup.RetryNotice`` turns
those into a status line.

The first two tests are controls on the LIBRARY, not on us: the notice is built on
its log strings and its retry count, and if either changes these fail rather than
the notice silently going quiet or counting to the wrong number.
"""

from __future__ import annotations

import inspect
import logging
import socket
import threading

import musicbrainzngs.musicbrainz as mbz  # type: ignore[import-untyped]
import pytest

from cdda2img import mb_lookup, terminal_ui


class _FakeUI:
    def __init__(self, *, paused: bool = False) -> None:
        self.texts: list[str] = []
        self._paused = paused

    def is_paused(self) -> bool:
        return self._paused

    def set_status_text(self, text: str) -> None:
        self.texts.append(text)


def _rec(msg: str) -> logging.LogRecord:
    return logging.LogRecord(
        "musicbrainzngs", logging.INFO, __file__, 1, msg, None, None
    )


def test_the_library_still_makes_eight_attempts() -> None:
    params = inspect.signature(mbz._safe_read).parameters
    assert params["max_retries"].default == mb_lookup.MB_ATTEMPTS


def test_the_library_logs_the_cause_then_the_retry(caplog) -> None:
    class _Resp:
        def read(self) -> bytes:
            return b"ok"

    class _Opener:
        calls = 0

        def open(self, _req: object, _body: object = None) -> _Resp:
            self.calls += 1
            if self.calls == 1:
                msg = "timed out"
                raise socket.timeout(msg)
            return _Resp()

    with caplog.at_level(logging.INFO, logger="musicbrainzngs"):
        body = mbz._safe_read(_Opener(), object(), max_retries=2, retry_delay_delta=0.0)
    assert body == b"ok"
    msgs = [r.getMessage() for r in caplog.records if r.name == "musicbrainzngs"]
    assert msgs == ["socket timeout", "retrying after delay (#1)"]


@pytest.mark.parametrize(
    ("cause", "retry", "text"),
    [
        ("socket timeout", 1, "MusicBrainz slow: try 2/8"),
        ("HTTP error 503", 2, "MusicBrainz busy: try 3/8"),
        ("unknown HTTP error 429", 1, "MusicBrainz busy: try 2/8"),
    ],
)
def test_a_running_tui_shows_the_notice_as_its_status(
    monkeypatch, cause: str, retry: int, text: str
) -> None:
    ui = _FakeUI()
    monkeypatch.setattr(terminal_ui, "_ACTIVE", ui)
    h = mb_lookup.RetryNotice()
    h.handle(_rec(cause))
    h.handle(_rec(f"retrying after delay (#{retry})"))
    assert ui.texts == [text]


def test_without_a_tui_the_notice_is_its_own_line(monkeypatch, capsys) -> None:
    monkeypatch.setattr(terminal_ui, "_ACTIVE", None)
    h = mb_lookup.RetryNotice()
    h.handle(_rec("socket timeout"))
    h.handle(_rec("retrying after delay (#1)"))
    assert capsys.readouterr().out == "  MusicBrainz slow: try 2/8\n"


def test_a_paused_tui_gets_a_line_not_a_status(monkeypatch, capsys) -> None:
    """Paused means a menu owns the terminal; writing a line is safe, and a status
    update would be invisible until resume."""
    ui = _FakeUI(paused=True)
    monkeypatch.setattr(terminal_ui, "_ACTIVE", ui)
    mb_lookup.RetryNotice().handle(_rec("retrying after delay (#1)"))
    assert ui.texts == []
    assert "MusicBrainz slow: try 2/8" in capsys.readouterr().out


def test_other_library_records_produce_no_notice(monkeypatch, capsys) -> None:
    ui = _FakeUI()
    monkeypatch.setattr(terminal_ui, "_ACTIVE", ui)
    mb_lookup.RetryNotice().handle(_rec("GET request for https://musicbrainz.org/"))
    assert ui.texts == []
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("busy", [True, False])
def test_every_notice_fits_the_status_column_of_an_80_column_terminal(
    busy: bool,
) -> None:
    budget = max(8, (80 - 1) // 3)  # TerminalUI._build: cols = width - 1; cols // 3
    for retry in range(1, mb_lookup.MB_ATTEMPTS):
        assert len(mb_lookup.retry_notice_text(retry, busy=busy)) <= budget


def test_set_status_text_keeps_progress_and_detail() -> None:
    """The notice must not turn an indeterminate bar into 0.0% (set_status would)."""
    ui = object.__new__(terminal_ui.TerminalUI)
    ui._slk = threading.Lock()
    ui._status, ui._prog, ui._detail = "Querying MusicBrainz…", -1.0, "(1/2)"
    ui.set_status_text("MusicBrainz slow: try 2/8")
    assert (ui._status, ui._prog, ui._detail) == (
        "MusicBrainz slow: try 2/8",
        -1.0,
        "(1/2)",
    )


@pytest.fixture
def _clean_logging():
    root, mb = logging.getLogger(), logging.getLogger("musicbrainzngs")
    saved = (root.handlers[:], root.level, mb.handlers[:], mb.level)
    yield root, mb
    root.handlers[:], mb.handlers[:] = saved[0], saved[2]
    root.setLevel(saved[1])
    mb.setLevel(saved[3])


def test_the_entry_point_shows_the_notice_and_keeps_the_chatter_off_the_terminal(
    _clean_logging, monkeypatch, capsys
) -> None:
    """Lowering the musicbrainzngs logger to INFO is what lets the notice see the
    retries, and it also lets every INFO record propagate to the terminal handler,
    which ignores the root's level. The negative half: stderr stays empty."""
    from cdda2img.cdda2img import _install_log_handler

    root, mb = _clean_logging
    root.handlers[:], mb.handlers[:] = [], []
    root.setLevel(logging.WARNING)
    mb.setLevel(logging.NOTSET)
    monkeypatch.setattr(terminal_ui, "_ACTIVE", None)

    _install_log_handler(verbose=False)
    _install_log_handler(verbose=False)  # a second install must not double the notice
    assert sum(isinstance(h, mb_lookup.RetryNotice) for h in mb.handlers) == 1

    mb.info("socket timeout")
    mb.info("retrying after delay (#1)")
    captured = capsys.readouterr()
    assert captured.out == "  MusicBrainz slow: try 2/8\n"
    assert captured.err == ""
