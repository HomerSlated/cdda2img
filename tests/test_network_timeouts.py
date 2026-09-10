"""Every remote call must be bounded (net.py).

One global cannot do it: ``socket.setdefaulttimeout`` bounds urllib (musicbrainzngs)
and is ignored by requests (pyacoustid, discogs_client). So there are three
mechanisms, and these tests pin each one *at our call site* — via ``call_args``,
never the source text — and prove the premise each rests on against a local
server that accepts connections and never sends a byte.

The requests premise test is also the negative control for the whole design: if a
future requests/urllib3 starts honouring the socket default, it fails, and the
per-call timeouts become candidates for deletion rather than silent duplication.
"""

from __future__ import annotations

import argparse
import socket
import threading
import time
import urllib.request
from unittest.mock import MagicMock, patch

import pytest
import requests
from requests.exceptions import Timeout

from cdda2img import discogs_lookup, net


@pytest.fixture
def restore_socket_default():
    """The socket default is process-global: never let a test leak it."""
    before = socket.getdefaulttimeout()
    yield
    socket.setdefaulttimeout(before)


@pytest.fixture
def black_hole():
    """URL of a local server that accepts TCP connections and never responds.

    That is the shape of a stalled upstream. Accepted connections are held open
    for the test and closed at teardown, which also releases any client still
    blocked on one.
    """
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(0.1)
    held: list[socket.socket] = []
    stop = threading.Event()

    def _accept() -> None:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            held.append(conn)

    thread = threading.Thread(target=_accept, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.getsockname()[1]}/"
    stop.set()
    thread.join(1)
    for conn in held:
        conn.close()
    srv.close()


# ---------------------------------------------------------------------------
# Mechanism 1: the process-wide socket default (musicbrainzngs)
# ---------------------------------------------------------------------------


def test_main_installs_the_default_socket_timeout(restore_socket_default):
    """The CLI entry point, not any module import, sets the socket default."""
    from cdda2img import cdda2img as app

    socket.setdefaulttimeout(None)
    args = argparse.Namespace(verbose=False, cmd="setup")
    with (
        patch.object(app, "parse_args", return_value=args),
        patch.object(app, "_install_log_handler"),
        patch.object(app, "_dispatch") as dispatch,
    ):
        app.main()

    dispatch.assert_called_once_with(args)
    assert socket.getdefaulttimeout() == net.NETWORK_TIMEOUT


def test_importing_the_package_does_not_set_the_default(restore_socket_default):
    """A library that mutated the socket default on import would change the
    behaviour of every program importing it — the rule the entry point obeys."""
    import importlib

    socket.setdefaulttimeout(None)
    importlib.reload(net)
    assert socket.getdefaulttimeout() is None


def test_socket_default_bounds_the_urllib_opener(restore_socket_default, black_hole):
    """Premise: musicbrainzngs reads through ``build_opener().open()``."""
    net.install_default_socket_timeout(0.3)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        urllib.request.build_opener().open(black_hole).read()
    assert time.monotonic() - started < 3


def test_requests_ignores_the_socket_default(restore_socket_default, black_hole):
    """Premise, and the negative control: why AcoustID and Discogs need their own.

    With the default at 0.2 s, a plain ``requests.get`` is still blocked well after
    it would have timed out. Teardown closes the held connection, which lets the
    worker thread finish.
    """
    net.install_default_socket_timeout(0.2)
    outcome: list[BaseException | None] = []

    def _call() -> None:
        try:
            requests.get(black_hole)  # noqa: S113 - the unbounded call IS the control
            outcome.append(None)
        except BaseException as exc:
            outcome.append(exc)

    worker = threading.Thread(target=_call, daemon=True)
    worker.start()
    worker.join(1.2)
    assert worker.is_alive(), f"requests honoured the socket default: {outcome}"


# ---------------------------------------------------------------------------
# Mechanism 2: per-call timeout (pyacoustid)
# ---------------------------------------------------------------------------


def test_acoustid_match_receives_the_timeout(monkeypatch, tmp_path):
    from cdda2img import acoustid_lookup

    monkeypatch.setenv("ACOUSTID_API_KEY", "test-key")
    with patch("acoustid.match", return_value=[]) as match:
        acoustid_lookup.fingerprint_and_lookup(tmp_path / "track.wav")

    assert match.called
    assert match.call_args.kwargs["timeout"] == net.NETWORK_TIMEOUT


# ---------------------------------------------------------------------------
# Mechanism 3: a timeout-carrying fetcher (discogs_client)
# ---------------------------------------------------------------------------


def test_discogs_client_uses_the_timeout_fetcher(monkeypatch):
    monkeypatch.setenv("DISCOGS_TOKEN", "tok")
    client = discogs_lookup._get_client()
    assert client is not None

    with patch("requests.request") as request:
        request.return_value = MagicMock(content=b"{}", status_code=200)
        client._fetcher.fetch(client, "GET", "https://api.discogs.com/x")

    kwargs = request.call_args.kwargs
    assert kwargs["timeout"] == net.NETWORK_TIMEOUT
    assert kwargs["params"] == {"token": "tok"}


def test_stock_discogs_fetcher_still_has_no_timeout():
    """Control: the reason the subclass exists. If upstream ever adds a timeout,
    this fails and the subclass should be reconsidered rather than kept by habit."""
    from discogs_client.fetchers import UserTokenRequestsFetcher

    with patch("requests.request") as request:
        request.return_value = MagicMock(content=b"{}", status_code=200)
        UserTokenRequestsFetcher("tok").fetch(None, "GET", "https://api.discogs.com/x")

    assert "timeout" not in request.call_args.kwargs


def test_discogs_fetcher_is_bounded_end_to_end(monkeypatch, black_hole):
    """Behavioural: the kwarg actually bounds a real requests call."""
    monkeypatch.setattr(discogs_lookup, "NETWORK_TIMEOUT", 0.3)
    fetcher = discogs_lookup._timeout_fetcher_class()("tok")
    started = time.monotonic()
    with pytest.raises(Timeout):
        fetcher.fetch(None, "GET", black_hole)
    assert time.monotonic() - started < 3


# ---------------------------------------------------------------------------
# Explicit timeouts that already existed
# ---------------------------------------------------------------------------


def test_album_art_timeout_follows_the_shared_constant():
    """The pre-rip banner joins its worker on HTTP_TIMEOUT + 5, so the two must agree."""
    from cdda2img import album_art

    assert album_art.HTTP_TIMEOUT == net.NETWORK_TIMEOUT
