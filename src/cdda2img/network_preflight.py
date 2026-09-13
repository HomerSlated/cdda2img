"""network_preflight.py — check the services a pipeline needs before investing in it.

An archiver with no access to its provenance sources is producing unverified,
unidentified containers, and the most expensive moment to discover that is after
a twenty-minute read. So ``create``, ``rip`` and ``import`` probe the services they
depend on first, before a drive spins or a file is transcoded.

**What counts as an answer.** A probe succeeds when the *service itself* replies
with an HTTP status — never a ping (ICMP is commonly blocked) and never a bare TCP
connect (AcoustID sits behind a CDN whose edge accepts connections while the origin
is down). Any 2xx, 3xx or 4xx is an answer. A 5xx is not — it is the server, or a
CDN in front of it, saying it cannot answer — **except** MusicBrainz's 503, which is
its documented rate-limit response and means "reachable, busy". Redirects are not
followed: a 3xx already proves the server answered, and MusicBrainz's ``/ws/2/``
redirect target is a documentation page. CDDB is probed by its protocol greeting.

**Bounded, including DNS.** Each probe has a socket timeout, but ``getaddrinfo``
runs on libc's own clock (glibc: 5 s x 2 attempts per nameserver) and no socket
timeout reaches it. The whole check is therefore bounded by one wall-clock
deadline: probes run on daemon threads, and any still running at the deadline is
reported as not having answered. A daemon thread left blocked in the resolver does
not hold the process open.

**Policy** (agreed with kgr 2026-09-10). Services are *required* or *advisory* per
pipeline: ``rip`` needs AccurateRip (verification) and MusicBrainz (identification);
``create`` and ``import`` need MusicBrainz. An advisory service that is down is
reported and the run continues. A required one that is down stops the run: on a
TTY without ``--auto`` the user chooses retry / continue / abort; otherwise the run
refuses to start unless ``--allow-offline`` is given. Whatever happens, the outcome
is recorded in PROV as ``network_preflight`` (rbi_spec §6.3.1).
"""

from __future__ import annotations

import importlib.metadata
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

PROBE_TIMEOUT = 5.0  # socket timeout for a single probe
DEADLINE = 8.0  # wall-clock bound for the whole check, resolver included
SLOW_SECONDS = 3.0  # answered, but slowly enough to be worth saying

_USER_AGENT = (
    f"cdda2img/{importlib.metadata.version('cdda2img')}"
    " +https://github.com/HomerSlated/cdda2img"
)


class NetworkUnavailable(RuntimeError):
    """A required service is down and the run is not allowed to continue."""


@dataclass(frozen=True)
class Service:
    key: str  # PROV token
    name: str  # for people
    targets: tuple[str, ...]  # URLs, or ``cddb://host:port``; the first to answer wins
    required: bool = False
    busy_on_503: bool = False


@dataclass(frozen=True)
class ProbeResult:
    service: Service
    state: str  # "ok" | "slow" | "busy" | "down"
    detail: str
    seconds: float | None = None

    @property
    def reachable(self) -> bool:
        return self.state != "down"


@dataclass(frozen=True)
class PreflightOutcome:
    results: tuple[ProbeResult, ...]

    @property
    def down(self) -> tuple[ProbeResult, ...]:
        return tuple(r for r in self.results if not r.reachable)

    @property
    def required_down(self) -> tuple[ProbeResult, ...]:
        return tuple(r for r in self.down if r.service.required)

    @property
    def prov_value(self) -> str:
        down = self.down
        return "ok" if not down else "degraded:" + ",".join(r.service.key for r in down)


MUSICBRAINZ = Service(
    "musicbrainz", "MusicBrainz", ("https://musicbrainz.org/ws/2/",), busy_on_503=True
)
ACCURATERIP = Service(
    "accuraterip",
    "AccurateRip",
    ("https://www.accuraterip.com/", "http://www.accuraterip.com/"),
)
CTDB = Service("ctdb", "CUETools DB", ("http://db.cuetools.net/",))
COVERART = Service("coverart", "Cover Art Archive", ("https://coverartarchive.org/",))
ACOUSTID = Service("acoustid", "AcoustID", ("https://api.acoustid.org/v2/lookup",))
DISCOGS = Service("discogs", "Discogs", ("https://api.discogs.com/",))


def services_for(pipeline: str, *, cddb_server: str | None) -> list[Service]:
    """The services *pipeline* uses, marked required or advisory.

    AcoustID and Discogs are included only when configured: a service the run will
    not call is not a service whose absence matters.
    """
    from cdda2img import acoustid_lookup, discogs_lookup
    from cdda2img.cddb import _resolve_server

    host, port = _resolve_server(cddb_server)
    cddb = Service("cddb", "CDDB", (f"cddb://{host}:{port}",))
    optional = [
        *([ACOUSTID] if acoustid_lookup.is_available() else []),
        *([DISCOGS] if discogs_lookup.is_available() else []),
    ]
    if pipeline == "rip":
        required = [ACCURATERIP, MUSICBRAINZ]
        advisory = [CTDB, COVERART, cddb, *optional]
    elif pipeline == "import":
        required = [MUSICBRAINZ]
        advisory = [COVERART, cddb, *optional]
    elif pipeline == "create":
        required = [MUSICBRAINZ]
        advisory = [s for s in optional if s is ACOUSTID]
    else:
        msg = f"no network preflight defined for pipeline {pipeline!r}"
        raise ValueError(msg)
    return [replace(s, required=True) for s in required] + advisory


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn a 3xx into an HTTPError: it is already the answer we came for."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _describe(err: object) -> str:
    if isinstance(err, socket.gaierror):
        return "DNS lookup failed"
    if isinstance(err, TimeoutError):
        return "timed out"
    if isinstance(err, ConnectionRefusedError):
        return "connection refused"
    if isinstance(err, ssl.SSLError):
        return "TLS failure"
    if isinstance(err, OSError) and err.strerror:
        return err.strerror
    return str(err) or type(err).__name__


def _probe_http(
    url: str, timeout: float, *, busy_on_503: bool
) -> tuple[str, str, float | None]:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})  # noqa: S310
    started = time.monotonic()
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            code = resp.status
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
    except urllib.error.URLError as exc:
        return "down", _describe(exc.reason), None
    except OSError as exc:  # a read timeout surfaces raw, not wrapped in URLError
        return "down", _describe(exc), None
    elapsed = time.monotonic() - started
    if code >= 500:
        if code == 503 and busy_on_503:
            return "busy", "HTTP 503 (rate limited)", elapsed
        return "down", f"HTTP {code}", elapsed
    return ("slow" if elapsed > SLOW_SECONDS else "ok"), f"HTTP {code}", elapsed


def _probe_cddb(host: str, port: int, timeout: float) -> tuple[str, str, float | None]:
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            greeting = sock.recv(128)
    except OSError as exc:
        return "down", _describe(exc), None
    elapsed = time.monotonic() - started
    # CDDBP greets 200 (read/write) or 201 (read-only); 43x refuses the session.
    if greeting.startswith(b"20"):
        return ("slow" if elapsed > SLOW_SECONDS else "ok"), "CDDBP greeting", elapsed
    return "down", f"refused session: {greeting[:40]!r}", elapsed


def probe_service(service: Service, timeout: float = PROBE_TIMEOUT) -> ProbeResult:
    """Probe each target in turn; the first that answers decides."""
    state, detail, seconds = "down", "no targets", None
    for target in service.targets:
        if target.startswith("cddb://"):
            host, _, port = target.removeprefix("cddb://").rpartition(":")
            state, detail, seconds = _probe_cddb(host, int(port), timeout)
        else:
            state, detail, seconds = _probe_http(
                target, timeout, busy_on_503=service.busy_on_503
            )
        if state != "down":
            break
    return ProbeResult(service, state, detail, seconds)


def probe_all(
    services: Sequence[Service],
    *,
    probe: Callable[[Service], ProbeResult] = probe_service,
    deadline: float = DEADLINE,
) -> tuple[ProbeResult, ...]:
    """Probe *services* in parallel, returning within *deadline* regardless of DNS."""
    results: dict[int, ProbeResult] = {}

    def _run(index: int, service: Service) -> None:
        results[index] = probe(service)

    threads = [
        threading.Thread(target=_run, args=(i, s), daemon=True)
        for i, s in enumerate(services)
    ]
    for thread in threads:
        thread.start()
    end = time.monotonic() + deadline
    for thread in threads:
        thread.join(max(0.0, end - time.monotonic()))
    got = dict(results)  # a straggler may still write; snapshot what arrived in time
    return tuple(
        got.get(i) or ProbeResult(s, "down", f"no answer within {deadline:.0f} s")
        for i, s in enumerate(services)
    )


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def _names(results: Sequence[ProbeResult]) -> str:
    return ", ".join(r.service.name for r in results)


def format_report(outcome: PreflightOutcome) -> list[str]:
    """Lines describing a check in which something did not answer."""
    down = outcome.down
    if len(down) == len(outcome.results):
        if all(r.detail == "DNS lookup failed" for r in down):
            headline = (
                "  No network: every service is unreachable and DNS lookups are "
                "failing — the WAN or local DNS looks down."
            )
        else:
            headline = "  No network: every service is unreachable."
    else:
        headline = "  Some network services are not answering:"
    lines = [headline]
    for r in outcome.results:
        marker = "  (required)" if r.service.required else ""
        lines.append(f"    {r.state:<5} {r.service.name:<18} {r.detail}{marker}")
    return lines


def _prompt(input_fn: Callable[[str], str] = input) -> str:
    """Ask what to do about a required service being down. Empty input aborts."""
    while True:
        answer = input_fn("  [r]etry, [c]ontinue anyway, or [a]bort? [r/c/A] ")
        answer = answer.strip().lower()
        if answer in ("r", "retry"):
            return "retry"
        if answer in ("c", "continue"):
            return "continue"
        if answer in ("", "a", "abort"):
            return "abort"


def _proceed(
    outcome: PreflightOutcome,
    *,
    interactive: bool,
    allow_offline: bool,
    prompt: Callable[[], str],
    out: Callable[[str], None],
) -> bool:
    """True to proceed, False to probe again; raises when the run must not start."""
    for line in format_report(outcome):
        out(line)
    if not outcome.required_down:
        out(f"  Continuing without: {_names(outcome.down)}")
        return True
    required = _names(outcome.required_down)
    if interactive:
        choice = prompt()
        if choice == "retry":
            return False
        if choice == "continue":
            out("  Continuing; the container will record what was unavailable.")
            return True
        msg = f"aborted: required network service(s) unavailable: {required}"
        raise NetworkUnavailable(msg)
    if allow_offline:
        out(
            "  --allow-offline: continuing; the container will record what was unavailable."
        )
        return True
    msg = (
        f"required network service(s) unavailable: {required}. Re-run once they "
        "recover, or pass --allow-offline to proceed and record the gap in the container."
    )
    raise NetworkUnavailable(msg)


def run_preflight(
    services: Sequence[Service],
    *,
    interactive: bool,
    allow_offline: bool,
    probe: Callable[[Service], ProbeResult] = probe_service,
    prompt: Callable[[], str] = _prompt,
    out: Callable[[str], None] = print,
    deadline: float = DEADLINE,
) -> PreflightOutcome:
    """Check *services*, apply the policy, and return what was found.

    Raises :class:`NetworkUnavailable` when a required service is down and the run
    may not continue (a non-interactive run without *allow_offline*, or the user
    chose to abort).
    """
    while True:
        started = time.monotonic()
        outcome = PreflightOutcome(probe_all(services, probe=probe, deadline=deadline))
        if not outcome.down:
            n = len(outcome.results)
            out(
                f"  Network check: {n}/{n} services answered "
                f"({time.monotonic() - started:.1f} s)"
            )
            for r in outcome.results:
                if r.state in ("slow", "busy"):
                    out(f"    note: {r.service.name} — {r.state} ({r.detail})")
            return outcome
        if _proceed(
            outcome,
            interactive=interactive,
            allow_offline=allow_offline,
            prompt=prompt,
            out=out,
        ):
            return outcome
