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
from collections import Counter
from collections.abc import Iterable
from dataclasses import replace
from datetime import date

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
# Candidate reduction — heuristic prune + per-field plurality vote
# ---------------------------------------------------------------------------
#
# gnudb returns many TOC-matched entries for one disc (10 for ABBA Gold), and
# they are crowd-sourced free text: typo'd titles, and DYEAR values that are
# often the *recording* year, not the CD's release year (ABBA Gold carried
# DYEAR=1974 — a decade before the CD existed). Rather than trust the arbitrary
# first entry, collapse the whole set to one consensus record: prune the
# structurally-bogus, then take the per-field plurality of what survives. Fully
# deterministic and network-free; the result still enters the resolver at the
# lowest (fill-blank) trust, so any richer source overrides it.

_CD_ERA_MIN_YEAR = 1982  # first commercial CDs shipped Oct 1982


def _year_of(release_date: str | None) -> int | None:
    if not release_date:
        return None
    try:
        return int(str(release_date)[:4])
    except ValueError:
        return None


def _year_is_plausible(release_date: str | None, max_year: int) -> bool:
    """True when *release_date*'s year could be a real CD release date.

    A year before 1982 (no CD could be sold) or in the future is a submitter
    error — almost always the recording / original-release year rather than
    this CD's release date, which is a different field entirely.
    """
    y = _year_of(release_date)
    return y is not None and _CD_ERA_MIN_YEAR <= y <= max_year


def _vote(values: Iterable[str | None]) -> str | None:
    """Plurality winner among non-blank *values*.

    Comparison is normalised (``strip().casefold()``) so spelling/case variants
    of the same value vote together; the returned form is the most common raw
    spelling of the winning group. Both tiers break ties lexicographically so
    the result is a total, deterministic order (there is no interactive picker).
    """
    groups: dict[str, Counter[str]] = {}
    for v in values:
        s = (v or "").strip()
        if s:
            groups.setdefault(s.casefold(), Counter())[s] += 1
    if not groups:
        return None
    best_key = min(groups, key=lambda k: (-sum(groups[k].values()), k))
    forms = groups[best_key]
    return min(forms, key=lambda f: (-forms[f], f))


def _track_field(metas: list[DiscMeta], i: int, attr: str) -> Iterable[str | None]:
    """Yield the *i*-th track's *attr* across every candidate that has it.

    All candidates come from the same disc-ID query and are parsed with the same
    track count, so index *i* addresses the same track number in each.
    """
    for m in metas:
        if i < len(m.tracks):
            yield getattr(m.tracks[i], attr)


def consensus_from_candidates(
    candidates: list[DiscMeta], *, max_year: int | None = None
) -> tuple[DiscMeta | None, int]:
    """Collapse CDDB candidates to one consensus ``DiscMeta``.

    Stage 1 (heuristic prune): null any implausible ``release_date`` (year
    outside 1982..*max_year*), and drop candidates with neither an album nor any
    title (pure junk). Sparse-but-valid entries are kept — an empty field casts
    no vote, so it can only help. Stage 2 (plurality): per-field majority vote
    across the survivors — album, artist, release_date, and each track's title
    and performer independently.

    Returns ``(consensus, years_pruned)``; ``consensus`` is None when nothing
    usable survives. *max_year* defaults to next year (allows brand-new
    releases) and is injectable for deterministic tests.
    """
    if not candidates:
        return None, 0
    if max_year is None:
        max_year = date.today().year + 1

    years_pruned = 0
    survivors: list[DiscMeta] = []
    for m in candidates:
        if not m.album and not any(t.title for t in m.tracks):
            continue  # degenerate: nothing to contribute
        release_date = m.release_date
        if release_date is not None and not _year_is_plausible(release_date, max_year):
            years_pruned += 1
            release_date = None
        survivors.append(replace(m, release_date=release_date))
    if not survivors:
        return None, years_pruned

    n_tracks = max(len(m.tracks) for m in survivors)
    voted_tracks: list[TrackMeta] = []
    for i in range(n_tracks):
        title = _vote(_track_field(survivors, i, "title"))
        performer = _vote(_track_field(survivors, i, "performer"))
        if title or performer:
            voted_tracks.append(
                TrackMeta(number=i + 1, title=title, performer=performer)
            )

    # Vote every string disc-field a candidate might carry, not just the three
    # _parse_xmcd populates today: a consensus record must never silently drop a
    # field an input set. (Real CDDB fills only album/artist/release_date/titles,
    # so the extra votes return None and this is a no-op there.)
    consensus = DiscMeta(
        album=_vote(m.album for m in survivors),
        artist=_vote(m.artist for m in survivors),
        release_date=_vote(m.release_date for m in survivors),
        label=_vote(m.label for m in survivors),
        country=_vote(m.country for m in survivors),
        catalog_number=_vote(m.catalog_number for m in survivors),
        barcode=_vote(m.barcode for m in survivors),
        source="cddb",
        tracks=voted_tracks,
    )
    return consensus, years_pruned


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


def _query_nsecs(disc_last_lsn: int) -> int:
    """Total disc length in seconds for the ``cddb query`` *nsecs* field.

    The absolute lead-out position in seconds, including the 150-frame (2 s)
    lead-in — the value cd-discid / freedb / whipper all emit. This is distinct
    from the disc-ID's own per-endpoint rounding in ``compute_cddb_disc_id``;
    the earlier ``(disc_last_lsn - track_lsns[0] + 1) // 75`` omitted the lead-in
    and ran ~2-3 s short (e.g. 3605 where reference clients emit 3608).
    """
    return (disc_last_lsn + 1 + 150) // 75


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
    total_secs = _query_nsecs(disc_last_lsn)
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
