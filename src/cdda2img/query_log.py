"""A per-run record of every online query and what came back, for the failure report.

kgr, 2026-11-21: a rip that cannot build a complete archive aborts, and the user
diagnoses it. "Nothing found" is a failure too, and "this would not be the first
time we searched for the wrong thing, or made the right query using the wrong
search parameters". So the report must carry the **exact** query and the
response, which only the call site knows. Each lookup records itself here.

**Process-global, not a contextvar.** CDDB and MusicBrainz run on
``ThreadPoolExecutor`` workers, and ``submit`` does not copy the caller's
context, so a contextvar would silently lose the two queries most likely to be
wrong. One rip runs per process, so one list guarded by a lock is exact.

**Credentials never reach the log.** The exact query of an AcoustID lookup
carries the API key, and a Discogs request carries the token. :func:`redact`
runs on everything recorded, and is the only path in.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass

#: Response text kept per record. A dBAR body can be up to 1 MB, and a report
#: nobody can read is no report; larger bodies keep their size and BLAKE3.
MAX_DETAIL = 4096

_LOCK = threading.Lock()
_RECORDS: list[QueryRecord] = []

#: Query-string keys and header names whose values are credentials.
_SECRET_KEYS = ("client", "key", "token", "apikey", "api_key", "authorization")
_SECRET_RE = re.compile(
    r"(?i)\b("
    + "|".join(_SECRET_KEYS)
    + r")(\s*[=:]\s*)(Discogs\s+token=)?([^\s&,;\"']+)"
)


@dataclass(frozen=True)
class QueryRecord:
    """One query and its answer.

    *outcome* is the short verdict (``200``, ``404``, ``no match``, ``timeout``,
    ``network: ...``); *detail* is the response itself, capped at
    :data:`MAX_DETAIL` characters.
    """

    service: str
    query: str
    outcome: str
    detail: str = ""


def redact(text: str) -> str:
    """*text* with every credential value replaced by ``<redacted>``."""
    return _SECRET_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{m.group(3) or ''}<redacted>", text
    )


def summarise_bytes(body: bytes) -> str:
    """A binary or oversized response as its size and BLAKE3."""
    import blake3

    return f"{len(body)} bytes, b3sum={blake3.blake3(body).hexdigest()}"


def start() -> None:
    """Begin a run: forget every earlier record."""
    with _LOCK:
        _RECORDS.clear()


def record(service: str, query: str, outcome: str, detail: str = "") -> None:
    """Record one query. Credentials are redacted; *detail* is capped."""
    detail = redact(detail)
    if len(detail) > MAX_DETAIL:
        import blake3

        full = detail.encode()
        detail = (
            detail[:MAX_DETAIL]
            + f"\n[... truncated: {len(full)} bytes in all,"
            + f" b3sum={blake3.blake3(full).hexdigest()}]"
        )
    with _LOCK:
        _RECORDS.append(QueryRecord(service, redact(query), redact(outcome), detail))


def records() -> list[QueryRecord]:
    """Every record since :func:`start`, in the order they were made."""
    with _LOCK:
        return list(_RECORDS)


def _render(value: object) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else repr(value)


def call(service: str, fn, *args, **kwargs):
    """Call ``fn(*args, **kwargs)`` and record it: the call as the query, and the
    result (or the exception) as the answer. Re-raises whatever *fn* raised.

    For a client library whose own HTTP is not ours to reach (musicbrainzngs),
    the call and its arguments are the exact query we made: they determine the
    URL, and a wrong argument is what the report exists to expose. Never pass a
    credential positionally: only ``key=value`` text is redacted.
    """
    name = getattr(fn, "__name__", str(fn))
    params = [repr(a) for a in args] + [f"{k}={v!r}" for k, v in kwargs.items()]
    query = f"{name}({', '.join(params)})"
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:
        record(service, query, f"error: {type(exc).__name__}: {exc}")
        raise
    record(service, query, "answered", _render(result))
    return result


def errored(service: str) -> bool:
    """Whether any query to *service* in this run failed to get an answer.

    What turns a service's missing data into exit 2 (re-run when it answers)
    rather than 3 (investigate): several lookups return an empty result on a
    network failure, so their own status cannot tell the two apart.
    """
    with _LOCK:
        return any(r.service == service and _unanswered(r.outcome) for r in _RECORDS)


def mark() -> int:
    """A position in the log, for :func:`errored_since`."""
    with _LOCK:
        return len(_RECORDS)


def errored_since(service: str, position: int) -> bool:
    """Whether a query to *service* recorded after *position* went unanswered: for
    a caller whose own lookups return the same empty value on an error as on a
    genuine "none" (``mb_lookup.discogs_link_and_barcode``), and which must not
    read an earlier failure of the run as its own."""
    with _LOCK:
        return any(
            r.service == service and _unanswered(r.outcome) for r in _RECORDS[position:]
        )


#: An HTTP status that means "not now", not "nothing found": rate limited, or a
#: server error. Measured 2026-11-21: Discogs answers a burst of lookups with
#: ``429 You are making requests too quickly``, and its client then returns an
#: empty result, which read as a genuine miss (exit 3) instead of exit 2.
_NOT_NOW = re.compile(r"HTTP (429|5\d\d)\b")


def _unanswered(outcome: str) -> bool:
    return outcome.startswith(("error", "network")) or bool(_NOT_NOW.match(outcome))
