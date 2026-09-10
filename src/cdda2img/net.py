"""net.py — the one network timeout, and how it reaches each HTTP stack.

Three stacks make remote calls, and a timeout binds only if the library passes it
down to the socket. Measured 2026-09-10 against a local server that accepts
connections and never answers, in both the dev venv (urllib3 2.5.0) and the pipx
install (urllib3 2.7.0):

* **urllib** — ``musicbrainzngs``, which has no timeout argument anywhere in its
  API. Bounded by :func:`socket.setdefaulttimeout`, which is therefore the only
  lever, installed once by :func:`install_default_socket_timeout` at the CLI entry
  point.
* **requests** — ``pyacoustid`` and ``discogs_client``. **Not** bounded by the
  socket default: requests always hands urllib3 an explicit value, and
  ``timeout=None`` is passed down as "block forever", overriding the global. Each
  needs the timeout at the call (``acoustid.match(..., timeout=)``; a fetcher
  subclass for Discogs, whose client exposes no timeout at all).
* **explicit** — AccurateRip, CTDB, album art and CDDB already pass their own.

A single ``setdefaulttimeout`` therefore covers one library in three while reading
as complete coverage, which is the trap this module exists to document.

Neither mechanism bounds **name resolution**: ``getaddrinfo`` blocks inside libc on
its own clock (glibc's default is 5 s x 2 attempts per nameserver — the 10 s
resolution failure observed on 2026-09-10). Anything that needs a hard wall-clock
bound has to supply one itself.

**The timeout is per socket operation, not per call.** musicbrainzngs retries a
read timeout 8 times with backoff sleeps of 2..14 s, so 30 s here is roughly five
minutes before one MB request gives up (measured: a 1 s timeout took 64.1 s over 8
connections). That is bounded, which is the goal; it is not fast.
"""

from __future__ import annotations

import socket

NETWORK_TIMEOUT = 30  # seconds, per socket operation


def install_default_socket_timeout(timeout: float = NETWORK_TIMEOUT) -> None:
    """Set the process-wide default socket timeout. Call once, from the entry point.

    This is a global mutation, so — like the logging handler — it belongs at the
    CLI entry point and nowhere else: a library module doing it at import time
    would change the behaviour of every program that imported it. It applies to
    sockets created afterwards, in every thread.
    """
    socket.setdefaulttimeout(timeout)
