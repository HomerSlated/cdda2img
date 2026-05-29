"""
cddb.py — CDDB disc ID computation and TCP query.

Public interface:
    compute_cddb_disc_id(track_lsns, disc_last_lsn) -> str
    query_cddb(track_lsns, disc_last_lsn, server=None) -> list[DiscMeta]
    prepopulate_from_cddb(disc, track_lsns, disc_last_lsn, server=None) -> RBIDisc
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import logging
import socket
from typing import TYPE_CHECKING

from cdda2img.lookup_result import DiscMeta, TrackMeta

if TYPE_CHECKING:
    from cdda2img.rbi_format import RBIDisc, RBITocEntry

log = logging.getLogger(__name__)

_DEFAULT_SERVER = "cddb.retrobridge.org"
_DEFAULT_PORT = 888
_TIMEOUT = 10  # seconds per socket operation
_CLIENT_NAME = "cdda2img"


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
    total_secs = (disc_last_lsn - track_lsns[0] + 1) // 75
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
        title = fields.get(f"TTITLE{i}", "").strip()
        tracks.append(TrackMeta(number=i + 1, title=title or None))

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
    *server* should be "host:port"; defaults to cddb.retrobridge.org:888.
    Returns [] when offline mode is active (R10).
    """
    from cdda2img.config import is_no_network_active

    if is_no_network_active():
        return []
    host, port = _resolve_server(server)
    disc_id = compute_cddb_disc_id(track_lsns, disc_last_lsn)
    n = len(track_lsns)
    offsets = [lsn + 150 for lsn in track_lsns]
    total_secs = (disc_last_lsn - track_lsns[0] + 1) // 75
    offset_str = " ".join(str(o) for o in offsets)
    version = importlib.metadata.version("cdda2img")

    try:
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

    except OSError as exc:
        log.warning("CDDB query failed (%s:%d): %s", host, port, exc)
        return []


# ---------------------------------------------------------------------------
# RBIDisc pre-population
# ---------------------------------------------------------------------------


def prepopulate_from_cddb(
    disc: RBIDisc,
    track_lsns: list[int],
    disc_last_lsn: int,
    server: str | None = None,
    *,
    verbose: bool = True,
) -> RBIDisc:
    """Query CDDB and fill missing fields in *disc* from the best match.

    Auto-applies the first result (best match in server ordering).
    On no match or network error, returns *disc* unchanged.
    """
    from cdda2img.rbi_format import RBIDisc as _RBIDisc
    from cdda2img.rbi_format import RBITocEntry

    matches = query_cddb(track_lsns, disc_last_lsn, server)
    if not matches:
        return disc

    meta = matches[0]
    if len(matches) > 1:
        log.debug(
            "CDDB: %d matches; using first (%s / %s)",
            len(matches),
            meta.artist,
            meta.album,
        )

    album = disc.album if disc.album else (meta.album or disc.album)
    artist = disc.artist if disc.artist else (meta.artist or disc.artist)
    release_date = disc.release_date or meta.release_date or None

    meta_by_num = {t.number: t for t in meta.tracks if t.number is not None}
    new_tracks: list[RBITocEntry] = []
    for entry in disc.tracks:
        mt = meta_by_num.get(entry.track_number)
        if not entry.title and mt and mt.title:
            title: str = mt.title
        else:
            title = entry.title
        new_tracks.append(
            RBITocEntry(
                track_number=entry.track_number,
                title=title,
                performer=entry.performer,
                start_frame=entry.start_frame,
                duration_frames=entry.duration_frames,
                pregap_frames=entry.pregap_frames,
                isrc=entry.isrc,
            )
        )

    updated = _RBIDisc(
        album=album,
        artist=artist,
        disc_number=disc.disc_number,
        disc_total=disc.disc_total,
        catalog=disc.catalog,
        disc_id=disc.disc_id,
        tracks=new_tracks,
        release_date=release_date,
        original_release_date=disc.original_release_date,
        low_dynamic_range=disc.low_dynamic_range,
        original_release_found=disc.original_release_found,
        original_release_title=disc.original_release_title,
        original_release_year=disc.original_release_year,
        mb_release_id=disc.mb_release_id,
        mb_release_group_id=disc.mb_release_group_id,
    )

    if verbose:
        n_str = f" ({len(matches)} matches, using first)" if len(matches) > 1 else ""
        artist_str = f" by {meta.artist}" if meta.artist else ""
        year_str = f"  ({meta.release_date})" if meta.release_date else ""
        print(f'  CDDB: matched "{meta.album}"{artist_str}{year_str}{n_str}')

    return updated
