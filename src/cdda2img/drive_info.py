"""
drive_info.py — sysfs drive probe + AccurateRip drive offset catalog.

Manages the ar_drives table in the local SQLite database (see db.py).

Public interface:
    probe_drive_name(device) -> str | None
    ensure_drive_offsets(conn) -> None
    find_drive_offset(conn, drive_name) -> tuple[int, int] | None
"""

from __future__ import annotations

import logging
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import ClassVar

log = logging.getLogger(__name__)

_FETCH_COOLDOWN = timedelta(days=30)
_AR_DRIVE_OFFSETS_URL = "http://www.accuraterip.com/driveoffsets.htm"
_SYSFS_BLOCK = Path("/sys/block")


# ---------------------------------------------------------------------------
# Sysfs probe
# ---------------------------------------------------------------------------


def probe_drive_name(device: str) -> str | None:
    """Return the normalized drive name for *device* from sysfs, or None.

    Reads /sys/block/<dev>/device/vendor and /model, collapses internal
    whitespace, and returns ``"VENDOR MODEL"`` (or just ``"MODEL"`` when the
    vendor string is empty).  Returns None when the sysfs paths are absent.
    """
    dev_path = _SYSFS_BLOCK / Path(device).name / "device"
    try:
        vendor = re.sub(r"\s+", " ", (dev_path / "vendor").read_text().strip())
        model = re.sub(r"\s+", " ", (dev_path / "model").read_text().strip())
    except OSError:
        return None
    return f"{vendor} {model}".strip() if vendor else model or None


# ---------------------------------------------------------------------------
# HTML parser
# ---------------------------------------------------------------------------


def _normalize_ar_name(raw: str) -> str | None:
    """Normalize a raw AccurateRip drive name to a matchable string.

    The page uses ``"VENDOR  - MODEL"`` (extra spaces around the separator).
    Hyphens inside a vendor or model name (e.g. ``HL-DT-ST``, ``DVD-RW``) are
    NOT surrounded by spaces, so requiring ``\\s+`` on BOTH sides of the
    separator hyphen distinguishes it from intra-name hyphens.

    Entries with no vendor start with ``"- MODEL"`` (leading hyphen + space).

    Examples::

        "PLEXTOR  - DVDR   PX-716A"  -> "PLEXTOR DVDR PX-716A"
        "ACER     - DVD-RW AXD001"   -> "ACER DVD-RW AXD001"
        "HL-DT-ST - BD-RE  WH16NS60" -> "HL-DT-ST BD-RE WH16NS60"
        "- 16X12 DVD DUAL"           -> "16X12 DVD DUAL"
    """
    s = raw.strip()
    # No-vendor entries: leading "- MODEL"
    m_lead = re.match(r"^-\s+(.*)", s)
    if m_lead:
        model = re.sub(r"\s+", " ", m_lead.group(1)).strip()
        return model or None
    # Vendor-model separator: whitespace on BOTH sides of the hyphen
    # (distinguishes " - " separator from intra-name hyphens like "HL-DT-ST")
    m_sep = re.match(r"^(.*?)\s+-\s+(.*)", s)
    if m_sep:
        vendor = re.sub(r"\s+", " ", m_sep.group(1)).strip()
        model = re.sub(r"\s+", " ", m_sep.group(2)).strip()
        if not model:
            return None
        return f"{vendor} {model}".strip() if vendor else model
    return re.sub(r"\s+", " ", s) or None


class _OffsetParser(HTMLParser):
    """Extract ``(ar_name, offset, submissions)`` rows from driveoffsets.htm."""

    _DATA_BG: ClassVar[set[str]] = {"#f4f4f4", "#fcfcfc"}

    def __init__(self) -> None:
        super().__init__()
        self._results: list[tuple[str, int, int]] = []
        self._row_cells: list[str] = []
        self._in_data_td = False
        self._in_font = False
        self._cell_buf: list[str] = []
        self._is_data_row = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "tr":
            self._row_cells = []
            self._is_data_row = False
        elif tag == "td":
            bg = (attr.get("bgcolor") or "").lower()
            self._in_data_td = bg in self._DATA_BG
            if self._in_data_td:
                self._is_data_row = True
                self._cell_buf = []
        elif tag == "font" and self._in_data_td:
            self._in_font = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "font":
            self._in_font = False
        elif tag == "td" and self._in_data_td:
            self._row_cells.append("".join(self._cell_buf).strip())
            self._in_data_td = False
            self._cell_buf = []
        elif tag == "tr" and self._is_data_row and len(self._row_cells) >= 3:
            self._emit_row()

    def handle_data(self, data: str) -> None:
        if self._in_font and self._in_data_td:
            self._cell_buf.append(data)

    def _emit_row(self) -> None:
        name_raw = self._row_cells[0]
        offset_raw = self._row_cells[1]
        subs_raw = self._row_cells[2]
        if "purged" in offset_raw.lower():
            return
        try:
            offset = int(offset_raw)
        except ValueError:
            return
        try:
            submissions = int(subs_raw)
        except ValueError:
            return
        ar_name = _normalize_ar_name(name_raw)
        if ar_name:
            self._results.append((ar_name, offset, submissions))

    def results(self) -> list[tuple[str, int, int]]:
        return list(self._results)


def _parse_drive_offsets_html(data: bytes) -> list[tuple[str, int, int]]:
    """Parse driveoffsets.htm bytes into a list of (ar_name, offset, submissions)."""
    parser = _OffsetParser()
    parser.feed(data.decode("latin-1"))
    return parser.results()


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

# Return type: (status, body, last_modified, etag) or None on error.
_FetchResult = tuple[int, bytes, str | None, str | None]


def _last_fetch_time(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute(
        "SELECT fetched_at FROM fetch_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    try:
        return datetime.fromisoformat(row["fetched_at"])
    except (ValueError, TypeError):
        return None


def _http_get(req: urllib.request.Request) -> _FetchResult | None:
    """Issue *req* and return (status, body, lm, etag), or None on error."""
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            return (
                resp.status,
                resp.read(),
                resp.headers.get("Last-Modified"),
                resp.headers.get("ETag"),
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return (304, b"", None, None)
        log.warning("AccurateRip drive offsets fetch failed: HTTP %d", exc.code)
        return None
    except OSError as exc:
        log.warning("AccurateRip drive offsets fetch failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ensure_drive_offsets(conn: sqlite3.Connection) -> None:
    """Fetch and repopulate the AccurateRip drive offset table if the cache is stale.

    Skips the network request when a successful fetch occurred within
    ``_FETCH_COOLDOWN`` (30 days).  Sends ``If-Modified-Since``/``If-None-Match``
    conditional headers when the server provides them (it currently does not,
    but the logic is in place for future use).

    On a successful 200 response, replaces all rows in ``ar_drives`` atomically
    and appends a row to ``fetch_log``.  On network or parse failure, logs a
    warning and leaves the existing data intact.
    """
    last = _last_fetch_time(conn)
    now = datetime.now(timezone.utc)
    if last is not None and (now - last) < _FETCH_COOLDOWN:
        return

    lm_row = conn.execute(
        "SELECT value FROM fetch_state WHERE key='last-modified'"
    ).fetchone()
    etag_row = conn.execute("SELECT value FROM fetch_state WHERE key='etag'").fetchone()

    req = urllib.request.Request(_AR_DRIVE_OFFSETS_URL)  # noqa: S310
    if lm_row:
        req.add_header("If-Modified-Since", lm_row["value"])
    if etag_row:
        req.add_header("If-None-Match", etag_row["value"])

    fetched_at = now.isoformat()
    result = _http_get(req)
    if result is None:
        return

    status, body, new_lm, new_etag = result

    if status == 304:
        with conn:
            conn.execute(
                "INSERT INTO fetch_log (fetched_at, http_status, last_modified, etag, row_count)"
                " VALUES (?, 304, NULL, NULL, NULL)",
                (fetched_at,),
            )
        return

    if status != 200:
        log.warning("AccurateRip drive offsets fetch: unexpected status %d", status)
        return

    rows = _parse_drive_offsets_html(body)
    if not rows:
        log.warning("AccurateRip drive offsets: parsed 0 entries — skipping update")
        return

    with conn:
        conn.execute("DELETE FROM ar_drives")
        conn.executemany(
            "INSERT INTO ar_drives (ar_name, offset, submissions) VALUES (?, ?, ?)",
            rows,
        )
        if new_lm:
            conn.execute(
                "INSERT OR REPLACE INTO fetch_state VALUES ('last-modified', ?)",
                (new_lm,),
            )
        if new_etag:
            conn.execute(
                "INSERT OR REPLACE INTO fetch_state VALUES ('etag', ?)", (new_etag,)
            )
        conn.execute(
            "INSERT INTO fetch_log (fetched_at, http_status, last_modified, etag, row_count)"
            " VALUES (?, 200, ?, ?, ?)",
            (fetched_at, new_lm, new_etag, len(rows)),
        )
    log.debug("AccurateRip drive offsets updated: %d entries", len(rows))


def find_drive_offset(
    conn: sqlite3.Connection, drive_name: str
) -> tuple[int, int] | None:
    """Return ``(offset, submissions)`` for the best match for *drive_name*, or None.

    Selects the row with the highest submissions count when multiple offsets
    exist for the same drive name (e.g. drives submitted by different users
    at different offsets).
    """
    row = conn.execute(
        "SELECT offset, submissions FROM ar_drives WHERE ar_name = ?"
        " ORDER BY submissions DESC LIMIT 1",
        (drive_name,),
    ).fetchone()
    if not row:
        return None
    return (row["offset"], row["submissions"])
