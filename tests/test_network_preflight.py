"""Network pre-flight: probe classification, the DNS-proof deadline, and the policy.

Probes are exercised against real local servers (HTTP, a black hole, a refused
port, a CDDB greeter) rather than mocks, because the classification *is* the
behaviour: what counts as an answer. The policy is exercised with an injected probe
so it needs no network at all. Each "not an answer" assertion sits beside its
control — the same server shape that *is* an answer.
"""

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import pytest

from cdda2img import network_preflight as npf
from cdda2img.network_preflight import (
    NetworkUnavailable,
    ProbeResult,
    Service,
    probe_all,
    probe_service,
    run_preflight,
    services_for,
)

# ---------------------------------------------------------------------------
# Local servers
# ---------------------------------------------------------------------------


@contextmanager
def _http_server(status: int, headers: tuple[tuple[str, str], ...] = ()):
    hits = {"n": 0}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits["n"] += 1
            self.send_response(status)
            for key, value in headers:
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/", hits
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def _tcp_server(greeting: bytes | None):
    """Accepts connections; sends *greeting* (or nothing: a black hole)."""
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
            if greeting is not None:
                conn.sendall(greeting)
            held.append(conn)

    thread = threading.Thread(target=_accept, daemon=True)
    thread.start()
    try:
        yield srv.getsockname()[1]
    finally:
        stop.set()
        thread.join(1)
        for conn in held:
            conn.close()
        srv.close()


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _svc(*targets: str, busy_on_503: bool = False, required: bool = False) -> Service:
    return Service(
        "svc", "Svc", tuple(targets), required=required, busy_on_503=busy_on_503
    )


# ---------------------------------------------------------------------------
# HTTP classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [200, 404, 403])
def test_2xx_and_4xx_are_answers(status):
    with _http_server(status) as (url, _hits):
        result = probe_service(_svc(url), timeout=2)
    assert result.state == "ok"
    assert result.detail == f"HTTP {status}"


def test_a_redirect_is_an_answer_and_is_not_followed():
    """MusicBrainz's /ws/2/ answers 301 to a documentation page: don't fetch it."""
    with _http_server(301, (("Location", "/elsewhere"),)) as (url, hits):
        result = probe_service(_svc(url), timeout=2)
    assert result.state == "ok"
    assert result.detail == "HTTP 301"
    assert hits["n"] == 1


@pytest.mark.parametrize("status", [500, 502, 504])
def test_5xx_is_not_an_answer(status):
    with _http_server(status) as (url, _hits):
        result = probe_service(_svc(url), timeout=2)
    assert result.state == "down"
    assert not result.reachable


def test_musicbrainz_503_reads_busy_not_down():
    """MB rate-limits with 503: a quick second run must not fail a healthy MB."""
    with _http_server(503) as (url, _hits):
        busy = probe_service(_svc(url, busy_on_503=True), timeout=2)
        down = probe_service(_svc(url), timeout=2)  # control: same 503, no flag
    assert (busy.state, busy.reachable) == ("busy", True)
    assert (down.state, down.reachable) == ("down", False)


def test_black_hole_times_out_within_the_probe_timeout():
    with _tcp_server(greeting=None) as port:
        started = time.monotonic()
        result = probe_service(_svc(f"http://127.0.0.1:{port}/"), timeout=0.3)
    assert result.state == "down"
    assert result.detail == "timed out"
    assert time.monotonic() - started < 3


def test_refused_port_is_named():
    result = probe_service(_svc(f"http://127.0.0.1:{_closed_port()}/"), timeout=2)
    assert (result.state, result.detail) == ("down", "connection refused")


def test_dns_failure_is_named(monkeypatch):
    def _fail(*_args, **_kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _fail)
    result = probe_service(_svc("https://musicbrainz.invalid/"), timeout=2)
    assert (result.state, result.detail) == ("down", "DNS lookup failed")


def test_second_target_answers_when_the_first_does_not():
    """AccurateRip is probed over HTTPS then HTTP, mirroring _fetch_ar's fallback."""
    with _http_server(200) as (url, _hits):
        result = probe_service(
            _svc(f"http://127.0.0.1:{_closed_port()}/", url), timeout=2
        )
    assert result.state == "ok"


# ---------------------------------------------------------------------------
# CDDB greeting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("greeting", "state"),
    [
        (b"201 test CDDBP server v1 ready\r\n", "ok"),
        (b"200 test CDDBP server v1 ready\r\n", "ok"),
        (b"433 No connections allowed\r\n", "down"),
    ],
)
def test_cddb_is_judged_by_its_greeting(greeting, state):
    with _tcp_server(greeting) as port:
        result = probe_service(_svc(f"cddb://127.0.0.1:{port}"), timeout=2)
    assert result.state == state


# ---------------------------------------------------------------------------
# The deadline bounds what socket timeouts cannot: the resolver
# ---------------------------------------------------------------------------


def test_a_hung_resolver_is_bounded_by_the_deadline(monkeypatch):
    def _hang(*_args, **_kwargs):
        time.sleep(2)
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

    monkeypatch.setattr(socket, "getaddrinfo", _hang)
    started = time.monotonic()
    (result,) = probe_all([_svc("https://musicbrainz.org/ws/2/")], deadline=0.4)
    assert time.monotonic() - started < 1.5
    assert result.state == "down"
    assert "no answer within" in result.detail


def test_probe_all_runs_in_parallel():
    def _slow(service: Service) -> ProbeResult:
        time.sleep(0.4)
        return ProbeResult(service, "ok", "HTTP 200")

    started = time.monotonic()
    results = probe_all([_svc("a"), _svc("b"), _svc("c")], probe=_slow, deadline=5)
    assert [r.state for r in results] == ["ok", "ok", "ok"]
    assert time.monotonic() - started < 1.0  # serial would be 1.2 s


# ---------------------------------------------------------------------------
# Which services, and which are required
# ---------------------------------------------------------------------------


def _keys(services, *, required: bool) -> set[str]:
    return {s.key for s in services if s.required is required}


@pytest.fixture
def lookups_available(monkeypatch):
    from cdda2img import acoustid_lookup, discogs_lookup

    monkeypatch.setattr(acoustid_lookup, "is_available", lambda: True)
    monkeypatch.setattr(discogs_lookup, "is_available", lambda: True)


def test_rip_requires_accuraterip_and_musicbrainz(lookups_available):
    services = services_for("rip", cddb_server="example.org:1234")
    assert _keys(services, required=True) == {"accuraterip", "musicbrainz"}
    assert _keys(services, required=False) == {
        "ctdb",
        "coverart",
        "cddb",
        "acoustid",
        "discogs",
    }
    cddb = next(s for s in services if s.key == "cddb")
    assert cddb.targets == ("cddb://example.org:1234",)


def test_import_and_create_require_only_musicbrainz(lookups_available):
    imp = services_for("import", cddb_server=None)
    assert _keys(imp, required=True) == {"musicbrainz"}
    assert "accuraterip" not in {s.key for s in imp}

    create = services_for("create", cddb_server=None)
    assert _keys(create, required=True) == {"musicbrainz"}
    # create reads art from file tags and runs no CDDB, Discogs or AR lookup
    assert _keys(create, required=False) == {"acoustid"}


def test_unconfigured_lookups_are_not_probed(monkeypatch):
    from cdda2img import acoustid_lookup, discogs_lookup

    monkeypatch.setattr(acoustid_lookup, "is_available", lambda: False)
    monkeypatch.setattr(discogs_lookup, "is_available", lambda: False)
    keys = {s.key for s in services_for("rip", cddb_server=None)}
    assert not keys & {"acoustid", "discogs"}


# ---------------------------------------------------------------------------
# Policy (injected probe — no network)
# ---------------------------------------------------------------------------

MB = Service("musicbrainz", "MusicBrainz", ("x",), required=True)
DG = Service("discogs", "Discogs", ("x",))


def _probe_with(states: dict[str, str]):
    calls = {"n": 0}

    def _probe(service: Service) -> ProbeResult:
        calls["n"] += 1
        state = states[service.key]
        return ProbeResult(
            service, state, "HTTP 200" if state != "down" else "timed out"
        )

    return _probe, calls


def _run(states, *, interactive=False, allow_offline=False, prompt=None):
    probe, calls = _probe_with(states)
    lines: list[str] = []
    outcome = run_preflight(
        [MB, DG],
        interactive=interactive,
        allow_offline=allow_offline,
        probe=probe,
        prompt=prompt or MagicMock(side_effect=AssertionError("must not prompt")),
        out=lines.append,
    )
    return outcome, lines, calls


def test_all_answering_proceeds_quietly():
    outcome, lines, _ = _run({"musicbrainz": "ok", "discogs": "ok"})
    assert outcome.prov_value == "ok"
    assert len(lines) == 1 and "2/2 services answered" in lines[0]


def test_advisory_down_warns_and_continues_even_headless():
    outcome, lines, _ = _run({"musicbrainz": "ok", "discogs": "down"})
    assert outcome.prov_value == "degraded:discogs"
    assert any("Continuing without: Discogs" in ln for ln in lines)


def test_required_down_headless_refuses_and_names_the_override():
    with pytest.raises(NetworkUnavailable, match="--allow-offline"):
        _run({"musicbrainz": "down", "discogs": "ok"})


def test_required_down_headless_with_override_proceeds_and_records_it():
    outcome, _lines, _ = _run(
        {"musicbrainz": "down", "discogs": "ok"}, allow_offline=True
    )
    assert outcome.prov_value == "degraded:musicbrainz"


def test_interactive_retry_probes_again():
    states = {"musicbrainz": "down", "discogs": "ok"}
    prompt = MagicMock(side_effect=lambda: states.update(musicbrainz="ok") or "retry")
    outcome, _lines, calls = _run(states, interactive=True, prompt=prompt)
    assert prompt.call_count == 1
    assert calls["n"] == 4  # two services, probed twice
    assert outcome.prov_value == "ok"


def test_interactive_continue_records_the_gap():
    outcome, _lines, _ = _run(
        {"musicbrainz": "down", "discogs": "ok"},
        interactive=True,
        prompt=MagicMock(return_value="continue"),
    )
    assert outcome.prov_value == "degraded:musicbrainz"


def test_interactive_abort_raises():
    with pytest.raises(NetworkUnavailable, match="aborted"):
        _run(
            {"musicbrainz": "down", "discogs": "ok"},
            interactive=True,
            prompt=MagicMock(return_value="abort"),
        )


def test_interactive_prompt_is_not_offered_when_only_advisory_is_down():
    _run(
        {"musicbrainz": "ok", "discogs": "down"},
        interactive=True,
        prompt=MagicMock(side_effect=AssertionError("must not prompt")),
    )


@pytest.mark.parametrize(
    ("answers", "choice"),
    [(["r"], "retry"), (["C"], "continue"), ([""], "abort"), (["?", "a"], "abort")],
)
def test_prompt_parsing(answers, choice):
    feed = iter(answers)
    assert npf._prompt(lambda _msg: next(feed)) == choice


def test_wan_down_headline_when_every_lookup_fails_dns():
    outcome = npf.PreflightOutcome((
        ProbeResult(MB, "down", "DNS lookup failed"),
        ProbeResult(DG, "down", "DNS lookup failed"),
    ))
    assert "WAN or local DNS looks down" in npf.format_report(outcome)[0]


# ---------------------------------------------------------------------------
# Dispatch wiring: the check runs before the pipeline, and its result reaches PROV
# ---------------------------------------------------------------------------


def _dispatch_create(monkeypatch, tmp_path, *extra: str):
    from cdda2img import cdda2img as app

    monkeypatch.setattr("sys.argv", ["cdda2img", "create", str(tmp_path), *extra])
    args = app.parse_args()
    cfg = MagicMock(auto=False, cddb_server="gnudb.gnudb.org:8880")
    with (
        patch("cdda2img.config.load_config", return_value=cfg),
        patch.object(
            app, "_network_preflight", return_value="degraded:acoustid"
        ) as pre,
        patch.object(app, "create_image") as create,
    ):
        app._dispatch(args)
    return pre, create


def test_create_runs_the_preflight_and_passes_its_result(monkeypatch, tmp_path):
    pre, create = _dispatch_create(monkeypatch, tmp_path)
    assert pre.call_args.args == ("create",)
    assert pre.call_args.kwargs["allow_offline"] is False
    assert create.call_args.kwargs["network_preflight"] == "degraded:acoustid"


def test_allow_offline_reaches_the_preflight(monkeypatch, tmp_path):
    pre, _create = _dispatch_create(monkeypatch, tmp_path, "--allow-offline")
    assert pre.call_args.kwargs["allow_offline"] is True


def test_import_info_does_not_touch_the_network(monkeypatch, tmp_path):
    from cdda2img import cdda2img as app

    monkeypatch.setattr("sys.argv", ["cdda2img", "import", "--info", str(tmp_path)])
    args = app.parse_args()
    with (
        patch.object(app, "_network_preflight") as pre,
        patch.object(app, "info_image"),
    ):
        app._dispatch(args)
    pre.assert_not_called()
