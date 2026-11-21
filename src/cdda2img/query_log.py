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
