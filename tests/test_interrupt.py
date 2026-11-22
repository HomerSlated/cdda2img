"""Ctrl-C reaching an AccuDisc read in flight, and the lookups that no longer hold
the exit (kgr, 2026-11-22).

During ``Device.read`` the main thread is inside libaccudisc, where Python cannot
raise ``KeyboardInterrupt``. The interrupt watcher (a thread woken through
``signal.set_wakeup_fd``) sets the read's AccuDisc ``Cancel`` flag instead."""

from __future__ import annotations

import os
import signal
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from cdda2img import accudisc_reader as ar
from cdda2img import cdda2img as app


class _FakeCancel:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _FakeCancelled(Exception):
    pass


def _module() -> SimpleNamespace:
    return SimpleNamespace(Cancel=_FakeCancel, Cancelled=_FakeCancelled)


def test_every_read_carries_a_registered_cancel_token() -> None:
    module = _module()
    with ar._cancellable(module) as kw:
        token = kw["cancel"]
        assert token in ar._LIVE_CANCELS
    assert token not in ar._LIVE_CANCELS


def test_cancel_reads_reaches_a_read_in_flight_and_surfaces_as_ctrl_c() -> None:
    """The watcher's call, mid-read: the engine then returns CANCELLED, which must
    reach the caller as KeyboardInterrupt. As a RuntimeError the recovery loops
    would take it for a failed attempt and start the next one."""
    module = _module()
    with pytest.raises(KeyboardInterrupt), ar._cancellable(module) as kw:
        ar.cancel_reads()  # what the watcher thread does on SIGINT
        assert kw["cancel"].cancelled
        raise _FakeCancelled


def test_a_cancel_we_did_not_ask_for_stays_a_device_error() -> None:
    """Control: CANCELLED with our flag unset (the sink stopped the read) is not
    turned into a Ctrl-C."""
    module = _module()
    with pytest.raises(_FakeCancelled), ar._cancellable(module):
        raise _FakeCancelled


def test_nothing_is_remembered_for_the_next_read() -> None:
    """A Ctrl-C a prompt took as "back" must not cancel later reads."""
    ar.cancel_reads()
    with ar._cancellable(_module()) as kw:
        assert not kw["cancel"].cancelled


def test_a_binding_without_cancel_gets_no_keyword() -> None:
    with ar._cancellable(SimpleNamespace()) as kw:
        assert kw == {}


def test_a_real_sigint_wakes_the_watcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end through CPython's C-level handler: a SIGINT writes the wakeup
    fd, and the watcher thread cancels the live read's token."""
    fired = threading.Event()
    monkeypatch.setattr(ar, "cancel_reads", fired.set)
    old_fd = signal.set_wakeup_fd(-1)
    try:
        app._install_interrupt_watcher()
        with pytest.raises(KeyboardInterrupt):
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(1)  # the interrupt lands here, in the main thread
        assert fired.wait(2), "the watcher never saw the SIGINT"
    finally:
        signal.set_wakeup_fd(old_fd)


def test_other_signals_do_not_cancel_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: SIGWINCH also writes the wakeup fd (menu_state installs a handler
    for it), and a terminal resize must not stop a rip."""
    fired = threading.Event()
    monkeypatch.setattr(ar, "cancel_reads", fired.set)
    old_handler = signal.signal(signal.SIGWINCH, lambda *_a: None)
    old_fd = signal.set_wakeup_fd(-1)
    try:
        app._install_interrupt_watcher()
        os.kill(os.getpid(), signal.SIGWINCH)
        assert not fired.wait(0.5)
    finally:
        signal.set_wakeup_fd(old_fd)
        signal.signal(signal.SIGWINCH, old_handler)


def test_a_daemon_lookup_returns_its_result_and_its_error() -> None:
    assert app._in_daemon_thread(lambda a, b=0: a + b, 2, b=3).result(2) == 5

    def boom() -> Any:
        msg = "lookup failed"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="lookup failed"):
        app._in_daemon_thread(boom).result(2)


def test_a_stalled_lookup_does_not_hold_the_exit() -> None:
    """The lookup runs on a daemon thread, which interpreter exit abandons; a
    ThreadPoolExecutor's worker is joined and would wait out MusicBrainz's ~5 min
    of retries."""
    release = threading.Event()
    future = app._in_daemon_thread(release.wait)
    worker = next(t for t in threading.enumerate() if t.name == "wait")
    assert worker.daemon
    release.set()
    future.result(2)


class _FakeProbeDevice:
    """A handle with AccuDisc 0.52.0's set_cancel; its probe raises CANCELLED
    when the handle flag is set mid-probe, as the engine does between rungs."""

    def __init__(self, cancel_mid_probe: bool) -> None:
        self.installed: list[Any] = []
        self._cancel_mid_probe = cancel_mid_probe

    def set_cancel(self, token: Any) -> None:
        self.installed.append(token)

    def probe(self) -> str:
        token = self.installed[-1]
        if self._cancel_mid_probe:
            ar.cancel_reads()  # the watcher thread, on Ctrl-C
        if token is not None and token.cancelled:
            raise _FakeCancelled
        return "rungs"


def _handle_module() -> SimpleNamespace:
    module = _module()
    module.features = frozenset({"handle_cancel"})
    return module


def test_the_speed_probe_is_cancelled_through_the_handle_flag() -> None:
    """probe_speed_ladder takes no per-call flag; only the handle flag reaches it."""
    dev = _FakeProbeDevice(cancel_mid_probe=True)
    with pytest.raises(KeyboardInterrupt), ar._handle_cancel(_handle_module(), dev):
        dev.probe()
    assert dev.installed[-1] is None  # removed again: the library never clears it


def test_the_handle_flag_is_installed_and_removed_on_a_clean_probe() -> None:
    dev = _FakeProbeDevice(cancel_mid_probe=False)
    with ar._handle_cancel(_handle_module(), dev):
        assert dev.probe() == "rungs"
    assert len(dev.installed) == 2
    assert dev.installed[0] is not None and dev.installed[1] is None


def test_no_handle_flag_on_an_engine_before_0_52() -> None:
    dev = _FakeProbeDevice(cancel_mid_probe=False)
    with ar._handle_cancel(_module(), dev):  # no "handle_cancel" feature
        pass
    assert dev.installed == []
