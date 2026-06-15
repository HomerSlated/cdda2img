"""
cddb.py — CDDB disc ID computation and TCP query.

Public interface:
    compute_cddb_disc_id(track_lsns, disc_last_lsn) -> str
    query_cddb(track_lsns, disc_last_lsn, server=None) -> list[DiscMeta]

CDDB results are merged into the disc by the caller at LOWEST precedence
(``cdda2img._run_metadata_lookups`` applies them last via ``_merge_into_disc``),
because freedb's flat "Artist / Title" TTITLE cannot cleanly separate a track
title from its performer. There is deliberately no high-trust apply helper here.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import logging
import socket
import time

from cdda2img.lookup_result import DiscMeta, TrackMeta

log = logging.getLogger(__name__)

_DEFAULT_SERVER = "gnudb.gnudb.org"
_DEFAULT_PORT = 8880
_TIMEOUT = 10  # seconds per socket operation
_CLIENT_NAME = "cdda2img"
# #3-d: a cold-connect or mid-session TCP flake raises OSError and would
# otherwise return [] — indistinguishable from a legitimate "disc not in DB".
# Retry the whole query a few times before giving up; a transport failure is
# never cached (only the protocol-level 202 no-match is), so a flake cannot
# poison the cache with a false negative.
_CONNECT_ATTEMPTS = 3
_RETRY_BACKOFF_S = 0.5


# ---------------------------------------------------------------------------
# Disc ID computation
# ---------------------------------------------------------------------------


def _digit_sum(n: int) -> int:
    return sum(int(d) for d in str(n))


def compute_cddb_disc_id(track_lsns: list[int], disc_last_lsn: int) -> str:
    """Return the 8-hex-digit CDDB disc ID.

    track_lsns: libcdio LSNs (one per track, in order)
    disc_last_lsn: LSN of the last audio sector on the disc
    """
    n = len(track_lsns)
    offsets = [lsn + 150 for lsn in track_lsns]  # LSN → absolute CD frame
    offset_secs = [off // 75 for off in offsets]  # frames → whole seconds
    checksum = sum(_digit_sum(s) for s in offset_secs) % 255
    # CDDB rounds each endpoint to seconds independently, then subtracts.
    # `(leadout - start) // 75` is NOT equivalent when the floor remainders
    # cross: e.g. Sheryl Crow has track_lsns[0]=33 and leadout=270475, so
    # 33//75 + (leadout-33)//75 = 0 + 3605 but leadout//75 - 33//75 = 3606.
    # The latter is what freedb/cd-discid/whipper compute.
    total_secs = (disc_last_lsn + 1) // 75 - track_lsns[0] // 75
    disc_id = (checksum << 24) | (total_secs << 8) | n
    return f"{disc_id:08x}"


# ---------------------------------------------------------------------------
# TCP CDDB session
# ---------------------------------------------------------------------------


class _CddbSession:
    """Thin CDDB TCP session wrapper."""

    def __init__(self, host: str, port: int) -> None:
        self._sock = socket.create_connection((host, port), timeout=_TIMEOUT)
        self._file = self._sock.makefile("r", encoding="utf-8", errors="replace")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._sock.sendall(b"quit\r\n")
        self._sock.close()

    def __enter__(self) -> _CddbSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        self.close()

    def _send(self, line: str) -> None:
        self._sock.sendall((line + "\r\n").encode("utf-8"))

    def readline(self) -> str:
        return self._file.readline().strip()

    def cmd(self, line: str) -> str:
        """Send *line* and return the first response line (stripped)."""
        self._send(line)
        return self.readline()

    def read_until_dot(self) -> list[str]:
        """Read lines until a bare '.' line (or EOF); return them without the dot."""
        lines: list[str] = []
        while True:
            raw = self._file.readline()
            if not raw or raw.rstrip("\r\n") == ".":
                break
            lines.append(raw.rstrip("\r\n"))
        return lines


# ---------------------------------------------------------------------------
# XMCD parsing
# ---------------------------------------------------------------------------


def _parse_xmcd(lines: list[str], n_tracks: int) -> DiscMeta:
    """Extract disc and track metadata from XMCD data lines."""
    # Keys may appear on successive lines — concatenate values (XMCD spec §3.5)
    fields: dict[str, str] = {}
    for line in lines:
        if line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        k = key.strip()
        fields[k] = fields.get(k, "") + val

    dtitle = fields.get("DTITLE", "")
    if " / " in dtitle:
        artist, _, album = dtitle.partition(" / ")
    else:
        artist = ""
        album = dtitle

    year = fields.get("DYEAR", "").strip()

    tracks: list[TrackMeta] = []
    for i in range(n_tracks):
        raw = fields.get(f"TTITLE{i}", "").strip()
        # freedb TTITLE may carry a per-track artist as "Artist / Title" (gnudb
        # uses this even for single-artist discs, where retrobridge/MB did not).
        # Split on the FIRST " / " only, so a medley title that itself contains
        # " / " survives intact in the title part; no separator -> all title.
        performer, sep, title = raw.partition(" / ")
        if sep:
            tracks.append(
                TrackMeta(
                    number=i + 1,
                    title=title.strip() or None,
                    performer=performer.strip() or None,
                )
            )
        else:
            tracks.append(TrackMeta(number=i + 1, title=raw or None))

    return DiscMeta(
        album=album.strip() or None,
        artist=artist.strip() or None,
        release_date=year or None,
        source="cddb",
        tracks=tracks,
    )


# ---------------------------------------------------------------------------
# Public query interface
# ---------------------------------------------------------------------------


def _resolve_server(server: str | None) -> tuple[str, int]:
    if not server:
        return _DEFAULT_SERVER, _DEFAULT_PORT
    host, _, port_s = server.rpartition(":")
    port = int(port_s) if port_s else _DEFAULT_PORT
    return (host or server, port)


def _collect_candidates(
    code: str, rest: str, sess: _CddbSession
) -> list[tuple[str, str]]:
    """Parse the query response body into (category, discid) pairs."""
    if code == "200":
        parts = rest.split(None, 2)
        return [(parts[0], parts[1])] if len(parts) >= 2 else []
    raw_list = sess.read_until_dot()
    candidates: list[tuple[str, str]] = []
    for line in raw_list:
        parts = line.split(None, 2)
        if len(parts) >= 2:
            candidates.append((parts[0], parts[1]))
    return candidates


def query_cddb(
    track_lsns: list[int],
    disc_last_lsn: int,
    server: str | None = None,
) -> list[DiscMeta]:
    """Query a CDDB server and return matching DiscMeta objects.

    Returns an empty list on network error, no match, or unexpected response.
    *server* should be "host:port"; defaults to gnudb.gnudb.org:8880.
    Returns [] on network error.
    """
    disc_id = compute_cddb_disc_id(track_lsns, disc_last_lsn)
    host, port = _resolve_server(server)
    n = len(track_lsns)
    offsets = [lsn + 150 for lsn in track_lsns]
    total_secs = (disc_last_lsn - track_lsns[0] + 1) // 75
    offset_str = " ".join(str(o) for o in offsets)
    version = importlib.metadata.version("cdda2img")

    last_exc: OSError | None = None
    for attempt in range(_CONNECT_ATTEMPTS):
        try:
            return _query_cddb_session(
                host, port, disc_id, n, offset_str, total_secs, version
            )
        except OSError as exc:
            last_exc = exc
            log.debug(
                "CDDB attempt %d/%d failed (%s:%d): %s",
                attempt + 1,
                _CONNECT_ATTEMPTS,
                host,
                port,
                exc,
            )
            if attempt + 1 < _CONNECT_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_S)
    # Every attempt hit a transport error — this is NOT a clean negative, so
    # (unlike the 202 path) we do not cache an empty result.
    log.warning(
        "CDDB query failed after %d attempt(s) (%s:%d): %s",
        _CONNECT_ATTEMPTS,
        host,
        port,
        last_exc,
    )
    return []


def _query_cddb_session(
    host: str,
    port: int,
    disc_id: str,
    n: int,
    offset_str: str,
    total_secs: int,
    version: str,
) -> list[DiscMeta]:
    """Run one CDDB session attempt. Raises OSError on a transport flake (the
    caller retries); returns [] on a protocol-level negative (no retry)."""
    with _CddbSession(host, port) as sess:
        greeting = sess.readline()
        log.debug("CDDB greeting: %s", greeting)
        if greeting[:3] not in ("200", "201"):
            log.warning("CDDB: unexpected greeting: %r", greeting)
            return []

        r = sess.cmd(f"cddb hello anonymous localhost {_CLIENT_NAME} {version}")
        log.debug("CDDB hello: %s", r)
        r = sess.cmd("proto 6")
        log.debug("CDDB proto: %s", r)
        r = sess.cmd(f"cddb query {disc_id} {n} {offset_str} {total_secs}")
        log.debug("CDDB query: %s", r)

        code = r[:3]
        if code == "202":
            return []
        if code not in ("200", "210", "211"):
            log.warning("CDDB: unexpected query response: %r", r)
            return []

        candidates = _collect_candidates(code, r[4:], sess)
        results: list[DiscMeta] = []
        for category, cid in candidates:
            r2 = sess.cmd(f"cddb read {category} {cid}")
            log.debug("CDDB read %s/%s: %s", category, cid, r2)
            if not r2.startswith("210"):
                log.warning("CDDB: unexpected read response: %r", r2)
                continue
            xmcd_lines = sess.read_until_dot()
            results.append(_parse_xmcd(xmcd_lines, n))

        return results
