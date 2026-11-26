"""
discogs_lookup.py — Discogs metadata lookups.

Requires DISCOGS_TOKEN environment variable (free personal access token from
discogs.com/settings/developers). Returns empty lists silently when the token
is absent so callers need not branch on availability.
"""

from __future__ import annotations

import functools
import importlib.metadata
import logging
import os
from dataclasses import dataclass

from cdda2img import query_log
from cdda2img.barcode import normalize_barcode
from cdda2img.countries import discogs_country_code
from cdda2img.lookup_result import DiscMeta, TrackMeta
from cdda2img.net import NETWORK_TIMEOUT

log = logging.getLogger(__name__)

_USER_AGENT = f"cdda2img/{importlib.metadata.version('cdda2img')} +https://github.com/HomerSlated/cdda2img"


def is_available() -> bool:
    """Return True if DISCOGS_TOKEN is set in the environment."""
    return bool(os.environ.get("DISCOGS_TOKEN"))


def lookup_master_year(release_id: int) -> int | None:
    """R11: return the year of the Discogs master's main_release for *release_id*.

    Walks ``release.master.main_release.year`` via the discogs_client API.
    Returns None when:
      * Discogs is unavailable (no token).
      * The release has no associated master (e.g. one-off release).
      * The master has no main_release (rare).
      * Any API call raises.

    One extra Discogs API call beyond the original release fetch.
    """
    client = _get_client()
    if client is None:
        return None
    try:
        release = client.release(release_id)
        master = release.master
        if master is None:
            return None
        main_release = master.main_release
        if main_release is None:
            return None
        # main_release is a Release object; fetch its year via the same
        # discogs_client property accessor used elsewhere.
        year_raw = main_release.fetch("year")
        if not year_raw:
            return None
        return int(year_raw)
    except Exception as exc:
        log.debug("Discogs master year lookup failed for %s: %s", release_id, exc)
        return None


def _get_client():
    token = os.environ.get("DISCOGS_TOKEN", "")
    if not token:
        return None
    try:
        import discogs_client  # type: ignore[import-untyped]

        client = discogs_client.Client(_USER_AGENT, user_token=token)
    except ImportError:
        return None
    # discogs_client exposes no timeout, and its stock fetcher calls
    # requests.request() without one; requests ignores the process-wide socket
    # default, so a stalled Discogs would block indefinitely (net.py). The fetcher
    # is the library's own swap point — fetchers.py describes the abstraction as
    # "designed to make testing easier", and ships a delegating fetcher of its own.
    client._fetcher = _timeout_fetcher_class()(token)
    return client


@functools.cache
def _timeout_fetcher_class():
    """``UserTokenRequestsFetcher`` with a request timeout; see :mod:`cdda2img.net`.

    Built lazily because ``discogs_client`` is imported lazily — a run with no
    ``DISCOGS_TOKEN`` never imports it. The body mirrors the library's own ``fetch``
    exactly, plus ``timeout=``; ``test_network_timeouts`` pins both the timeout and
    that the stock fetcher still lacks one, so an upstream fix is noticed rather
    than silently duplicated.
    """
    import requests
    from discogs_client.fetchers import (  # type: ignore[import-untyped]
        UserTokenRequestsFetcher,
    )

    class _TimeoutUserTokenFetcher(UserTokenRequestsFetcher):
        def fetch(self, client, method, url, data=None, headers=None, json=True):
            # Recorded for the failure report (kgr, 2026-11-21); the token rides
            # in params, never in the recorded URL.
            query = f"{method} {url} (token=<redacted>)"
            try:
                resp = requests.request(
                    method,
                    url,
                    params={"token": self.user_token},
                    data=data,
                    headers=headers,
                    timeout=NETWORK_TIMEOUT,
                )
            except requests.RequestException as exc:
                query_log.record("Discogs", query, f"network: {exc}")
                raise
            query_log.record(
                "Discogs",
                query,
                f"HTTP {resp.status_code}",
                resp.content.decode("utf-8", "replace"),
            )
            return resp.content, resp.status_code

    return _TimeoutUserTokenFetcher


def _discogs_primary_type(formats) -> str | None:
    """Map a Discogs search-stub ``format`` list to an MB-style primary type.

    Discogs encodes format descriptors as a list of strings on the search stub,
    e.g. ``["CD", "Album"]`` or ``["CD", "Single"]``. We fold these to the same
    vocabulary MusicBrainz uses for its release-group primary type
    (Album / Single / EP) so the menu's Type column reads consistently across
    sources. Discogs "Compilation" (a *secondary* type in MB, where the primary
    stays "Album") folds to Album for that consistency. Returns None when no
    descriptor is recognised or the field is missing/malformed (→ "?" in the
    menu, an honest unknown).
    """
    if not isinstance(formats, list):
        return None
    tokens = {str(f).strip().lower() for f in formats if f}
    if "single" in tokens or "maxi-single" in tokens:
        return "Single"
    if "ep" in tokens:
        return "EP"
    if tokens & {"album", "lp", "mini-album", "compilation", "mixtape"}:
        return "Album"
    return None


def _parse_result(r) -> DiscMeta:
    """Parse a Discogs search result object into a DiscMeta.

    Reads ``r.data`` ONLY, never an attribute of *r*. A search row is a
    ``discogs_client`` Release object holding the search's flattened dict, and
    reading one of its attributes that the dict lacks (``r.year`` on a row with no
    year) makes the client fetch the whole release: one request per such row.
    Until 2026-11-26 this function fell back to ``getattr(r, "year")`` and the
    like "defensively", so a barcode search cost up to one request per hit; *Dire
    Straits, Brothers in Arms* (99 hits) passed Discogs' 60-a-minute limit at the
    61st, the search raised ``HTTP 429``, returned nothing, and the rip aborted
    with ``lookup_status_discogs=empty``. A full release is parsed by
    :func:`_parse_full_release`, never here.
    """
    data: dict = getattr(r, "data", {}) or {}

    raw_title = data.get("title") or ""
    if " - " in raw_title:
        parts = raw_title.split(" - ", 1)
        artist, album = parts[0].strip(), parts[1].strip()
    else:
        artist, album = "", raw_title

    year_raw = data.get("year")
    year = str(int(year_raw)) if year_raw else None

    country = data.get("country")

    # labels: search result returns list[str]; full release returns list[Label]
    label_raw = data.get("label") or []
    label = label_raw[0] if isinstance(label_raw, list) and label_raw else ""
    if isinstance(label, dict):
        label = label.get("name", "")
    catalog_number = data.get("catno") or ""

    barcodes: list = data.get("barcode") or []
    barcode = next((n for n in (normalize_barcode(b) for b in barcodes) if n), None)

    release_id = data.get("id")
    return DiscMeta(
        album=album or None,
        artist=artist or None,
        barcode=barcode,
        discogs_release_id=int(release_id) if release_id else None,
        discogs_master_id=_master_id(data),
        release_date=year,
        country=discogs_country_code(country),
        label=str(label) if label else None,
        catalog_number=catalog_number or None,
        primary_type=_discogs_primary_type(data.get("format")),
        source="discogs",
    )


def _discogs_join_artists(artists: list) -> str:
    """Build a display name from a Discogs artists list (full release shape)."""
    parts: list[str] = []
    for a in artists:
        if not isinstance(a, dict):
            continue
        parts.append(a.get("anv") or a.get("name") or "")
        join = a.get("join") or ""
        if join == ",":
            parts.append(", ")
        elif join:
            parts.append(f" {join.strip()} ")
    return "".join(parts).strip().rstrip(",").strip()


def _discogs_parse_tracklist(tracklist: list) -> list[TrackMeta]:
    """Convert a Discogs full-release tracklist to TrackMeta objects."""
    tracks: list[TrackMeta] = []
    seq = 0
    for t in tracklist:
        if not isinstance(t, dict) or t.get("type_") == "heading":
            continue
        seq += 1
        pos = t.get("position") or ""
        try:
            number = int(pos)
        except (ValueError, TypeError):
            number = seq  # vinyl-style positions (A1, B2) → sequential fallback
        tracks.append(TrackMeta(number=number, title=t.get("title") or None))
    return tracks


def _parse_full_release(r) -> DiscMeta:
    """Parse a full Discogs Release object (fetched by ID, not from search).

    Full releases have a separate artists list and a flat tracklist, unlike search
    stubs which encode "Artist - Album" in a single title field with no tracklist.
    """
    data: dict = getattr(r, "data", {}) or {}

    album = data.get("title") or ""
    artist = _discogs_join_artists(data.get("artists") or [])

    year_raw = data.get("year")
    year = str(int(year_raw)) if year_raw else None

    country = data.get("country") or None

    labels: list = data.get("labels") or []
    label = ""
    catalog_number = ""
    if labels and isinstance(labels[0], dict):
        label = labels[0].get("name") or ""
        catalog_number = labels[0].get("catno") or ""

    barcode_idents = [
        ident
        for ident in (data.get("identifiers") or [])
        if isinstance(ident, dict) and (ident.get("type") or "").lower() == "barcode"
    ]
    scanned = [
        ident
        for ident in barcode_idents
        if "scanned" in (ident.get("description") or "").lower()
    ]
    # Prefer Scanned; within each group take the first value that normalises.
    barcode = next(
        (n for n in (normalize_barcode(i.get("value")) for i in scanned) if n), None
    ) or next(
        (n for n in (normalize_barcode(i.get("value")) for i in barcode_idents) if n),
        None,
    )

    return DiscMeta(
        album=album or None,
        artist=artist or None,
        barcode=barcode,
        discogs_release_id=int(data["id"]) if data.get("id") else None,
        discogs_master_id=_master_id(data),
        release_date=year,
        country=discogs_country_code(country),
        label=str(label) if label else None,
        catalog_number=catalog_number or None,
        source="discogs",
        tracks=_discogs_parse_tracklist(data.get("tracklist") or []),
    )


def fetch_release(release_id: int) -> DiscMeta | None:
    """Fetch a full Discogs release by integer ID and return its complete metadata.

    Returns None when the token is absent, the library is unavailable, or on error.
    Use this after the user selects a search stub to populate the track listing.
    """
    client = _get_client()
    if not client:
        return None
    try:
        r = client.release(release_id)
        r.refresh()  # client.release() returns a stub; refresh() fetches the full data
        return _parse_full_release(r)
    except Exception as exc:
        log.debug("Discogs release fetch failed for %d: %s", release_id, exc)
        return None


def search_releases(
    query: str = "",
    *,
    artist: str = "",
    release_title: str = "",
    limit: int = 25,
) -> list[DiscMeta]:
    """Search for releases on Discogs.

    When *artist* or *release_title* are provided they are passed as structured
    field parameters (``artist=``, ``release_title=``), which produce more precise
    results than the free-text ``q=`` form.  Falls back to ``q=query`` when
    neither structured field is set.
    """
    client = _get_client()
    if not client:
        return []
    try:
        if artist or release_title:
            results = client.search(
                type="release", artist=artist, release_title=release_title
            )
        else:
            results = client.search(query, type="release")
        page1 = results.page(1)
        return [_parse_result(r) for r in page1[:limit]]
    except Exception as exc:
        log.debug("Discogs search failed: %s", exc)
        return []


# Discogs paginates at 50 per page. The cap bounds a pathological barcode (a
# compilation series sharing one EAN across dozens of pressings) rather than any
# realistic disc; 4 pages is 200 candidates against the 68 that the worst disc
# measured here produces.
_BARCODE_SEARCH_MAX_PAGES = 4


def _master_id(data: dict) -> int | None:
    """A release's master id; Discogs writes 0 or omits it for a release with no
    master."""
    try:
        value = int(data.get("master_id") or 0)
    except (TypeError, ValueError):
        return None
    return value or None


# ---------------------------------------------------------------------------
# Master versions: trust_model_design.md §12
# ---------------------------------------------------------------------------

#: Pages of 100 read per country from a master's versions: a runaway guard, not a
#: cap any one country of a real album reaches. Logged when it binds.
_MASTER_VERSIONS_MAX_PAGES = 5


@dataclass(frozen=True)
class DiscogsVersion:
    """One release of a Discogs master, as the versions list gives it (no
    request per release)."""

    release_id: int
    title: str
    country: str  # Discogs' own name ("UK", "Europe", "UK & Europe")
    year: str | None  # None when Discogs records none ("0")
    label: str
    catno: str
    formats: str  # e.g. "Album, Reissue"


@dataclass(frozen=True)
class MasterVersions:
    """The versions of a master in one format, for the chosen countries."""

    versions: tuple[DiscogsVersion, ...]
    total: int  # how many Discogs reported, across the chosen countries
    years: tuple[tuple[str, int], ...]  # the released facet: (year, count)
    truncated: bool  # the page guard bound; ``versions`` is short of ``total``

    @property
    def undated(self) -> int:
        """Versions with no year: the facet lists dated ones only."""
        return max(0, self.total - sum(n for _y, n in self.years))


def _versions_url(master_id: int, fmt: str, country: str | None, page: int, per: int):
    from urllib.parse import urlencode

    query = {"format": fmt, "per_page": per, "page": page}
    if country is not None:
        query["country"] = country
    return (
        f"https://api.discogs.com/masters/{int(master_id)}/versions?{urlencode(query)}"
    )


def master_countries(
    master_id: int, *, fmt: str = "CD"
) -> tuple[tuple[tuple[str, int], ...], int] | None:
    """The countries a master's *fmt* versions come from, with counts, and the
    total: one request (the facet rides on the first page). ``None`` on error.

    Measured on *No Jacket Required* (master 98041): 108 CDs, ``US 36, Europe 18,
    UK 11, Japan 9, …``. The names are Discogs' own; "Germany" is not among them
    although MusicBrainz says a German pressing exists, because Discogs files it
    under "Europe".
    """
    client = _get_client()
    if not client:
        return None
    try:
        data = client._get(_versions_url(master_id, fmt, None, 1, 1))
    except Exception as exc:
        log.debug("Discogs master %d countries failed: %s", master_id, exc)
        return None
    total = int((data.get("pagination") or {}).get("items") or 0)
    return _facet(data, "country"), total


def _facet(data: dict, name: str) -> tuple[tuple[str, int], ...]:
    for facet in data.get("filter_facets") or []:
        if facet.get("id") == name:
            # ``title``, not ``value``: Discogs sends ``value`` already
            # URL-encoded ("UK+%26+Europe"), and the filter is encoded once more
            # when it is sent back.
            return tuple(
                (str(v.get("title") or ""), int(v.get("count") or 0))
                for v in facet.get("values") or []
                if v.get("title")
            )
    return ()


def _version(v: dict) -> DiscogsVersion | None:
    if not v.get("id"):
        return None
    released = str(v.get("released") or "")[:4]
    return DiscogsVersion(
        release_id=int(v["id"]),
        title=v.get("title") or "",
        country=v.get("country") or "",
        year=released if released.strip("0") else None,
        label=v.get("label") or "",
        catno=v.get("catno") or "",
        formats=v.get("format") or "",
    )


def master_versions(
    master_id: int, *, countries: tuple[str, ...] = (), fmt: str = "CD"
) -> MasterVersions | None:
    """Every *fmt* version of *master_id* from *countries* (Discogs names; empty =
    every country), filtered on Discogs' side, with the year facet for that set.
    ``None`` on error.

    Years are NOT filtered here: Discogs' ``released`` filter drops undated
    versions, and on *No Jacket Required* 60 of 108 CDs have no year, including
    near-duplicates of the disc kgr held. The year is narrowed by the caller,
    keeping undated versions unless asked not to (§12 D4).
    """
    client = _get_client()
    if not client:
        return None
    seen: set[int] = set()
    versions: list[DiscogsVersion] = []
    years: dict[str, int] = {}
    total = 0
    truncated = False
    try:
        for country in countries or (None,):
            for page in range(1, _MASTER_VERSIONS_MAX_PAGES + 1):
                data = client._get(_versions_url(master_id, fmt, country, page, 100))
                if page == 1:
                    total += int((data.get("pagination") or {}).get("items") or 0)
                    for year, n in _facet(data, "released"):
                        years[year] = years.get(year, 0) + n
                for v in data.get("versions") or []:
                    parsed = _version(v)
                    if parsed and parsed.release_id not in seen:
                        seen.add(parsed.release_id)
                        versions.append(parsed)
                if page >= int((data.get("pagination") or {}).get("pages") or 1):
                    break
            else:
                truncated = True
                log.warning(
                    "Discogs master %d: more than %d pages of %s versions from %s;"
                    " the rest are not offered",
                    master_id,
                    _MASTER_VERSIONS_MAX_PAGES,
                    fmt,
                    country or "every country",
                )
    except Exception as exc:
        log.debug("Discogs master %d versions failed: %s", master_id, exc)
        return None
    return MasterVersions(
        tuple(versions), total, tuple(sorted(years.items())), truncated
    )


@dataclass(frozen=True)
class ReleaseDetails:
    """What tells one pressing of a release from another (§12 D5)."""

    release_id: int
    master_id: int | None
    title: str
    country: str
    released: str
    label: str
    catno: str
    formats: tuple[str, ...]
    barcodes: tuple[str, ...]  # as listed; empty = Discogs lists none
    identifiers: tuple[tuple[str, str, str], ...]  # (type, value, description)
    notes: str
    labels: tuple[tuple[str, str], ...] = ()  # (name, catno) per label, in order

    def catalogue_fields(self) -> dict[str, str]:
        """The pressing-level fields this release states, normalised to the
        container's forms, keyed by ``RBIDisc`` field; a field Discogs leaves
        blank (or cannot be normalised) is absent, never empty (kgr, 2026-11-26:
        Discogs overwrites MusicBrainz "only where they exist").

        ``label``/``catalog_number`` from the first label, ``country`` as an
        ISO code (``countries.discogs_country_code``: a region it cannot map is
        absent), ``barcode`` the first listed value that is a valid GTIN, and
        ``release_date`` with Discogs' ``-00`` placeholders dropped.
        """
        out: dict[str, str] = {}
        name, catno = self.labels[0] if self.labels else ("", "")
        if name.strip():
            out["label"] = name.strip()
        if catno.strip() and catno.strip().lower() != "none":
            out["catalog_number"] = catno.strip()
        code = discogs_country_code(self.country or None)
        if code:
            out["country"] = code
        barcode = next(
            (b for b in (normalize_barcode(v) for v in self.barcodes) if b), None
        )
        if barcode:
            out["barcode"] = barcode
        date = _discogs_date(self.released)
        if date:
            out["release_date"] = date
        return out


def _discogs_date(released: str) -> str | None:
    """Discogs ``released`` as ``YYYY``, ``YYYY-MM`` or ``YYYY-MM-DD``; its
    ``00`` placeholders for an unknown month or day are dropped, and ``0`` or
    ``0000`` (no date) is ``None``."""
    parts = (released or "").strip().split("-")
    kept: list[str] = []
    for part in parts:
        if not part.isdigit() or int(part) == 0:
            break
        kept.append(part)
    if not kept or len(kept[0]) != 4:
        return None
    return "-".join(kept[:3])


def release_details(release_id: int) -> ReleaseDetails | None:
    """One release in full, for the details screen; ``None`` on error.

    *No Jacket Required* GB, CDV 2345: eleven CD releases share label and
    catalogue number; "No barcode", "black print on label", SPARS codes and the
    runout are what separate them, and all of them live here.
    """
    client = _get_client()
    if not client:
        return None
    try:
        r = client.release(release_id)
        r.refresh()
        data: dict = getattr(r, "data", {}) or {}
    except Exception as exc:
        log.debug("Discogs release fetch failed for %d: %s", release_id, exc)
        return None
    labels = [lab for lab in data.get("labels") or [] if isinstance(lab, dict)]
    idents = [i for i in data.get("identifiers") or [] if isinstance(i, dict)]
    return ReleaseDetails(
        release_id=int(data.get("id") or release_id),
        master_id=_master_id(data),
        title=data.get("title") or "",
        country=data.get("country") or "",
        released=str(data.get("released") or ""),
        label=", ".join(lab.get("name") or "" for lab in labels),
        catno=", ".join(lab.get("catno") or "" for lab in labels),
        formats=tuple(
            ", ".join([f.get("name") or "", *(f.get("descriptions") or [])]).strip(", ")
            for f in data.get("formats") or []
            if isinstance(f, dict)
        ),
        barcodes=tuple(
            str(i.get("value") or "")
            for i in idents
            if (i.get("type") or "").lower() == "barcode"
        ),
        identifiers=tuple(
            (
                str(i.get("type") or ""),
                str(i.get("value") or ""),
                str(i.get("description") or ""),
            )
            for i in idents
            if (i.get("type") or "").lower() != "barcode"
        ),
        notes=(data.get("notes") or "").strip(),
        labels=tuple((lab.get("name") or "", lab.get("catno") or "") for lab in labels),
    )


@dataclass(frozen=True)
class MasterHit:
    """A Discogs master from a search (§12.2: no master linked)."""

    master_id: int
    title: str
    year: str | None


def search_masters(artist: str, album: str) -> list[MasterHit]:
    """Discogs masters matching *artist* and *album*, for a release group
    MusicBrainz links to none. ``[]`` on error."""
    client = _get_client()
    if not client:
        return []
    try:
        results = client.search(album, artist=artist, type="master")
        hits: list[MasterHit] = []
        for r in results.page(1):
            data: dict = getattr(r, "data", {}) or {}
            if data.get("id"):
                year = str(data.get("year") or "") or None
                hits.append(MasterHit(int(data["id"]), data.get("title") or "", year))
    except Exception as exc:
        log.debug("Discogs master search failed: %s", exc)
        return []
    return hits


def search_by_barcode(barcode: str) -> list[DiscMeta]:
    """Lookup by barcode (EAN-13 / UPC). Returns [] if token not set or on error.

    Reads up to ``_BARCODE_SEARCH_MAX_PAGES`` pages, not just the first. It used
    to return ``page(1)[:25]``, which was **not** "the 25 best matches" — Discogs
    does not rank a barcode search, so it was an arbitrary 25 of however many
    exist, and the rest were invisible. Measured on Tracy Chapman: the barcode
    yields 68 releases, and the one MusicBrainz's url-relation points at is not
    among the first 25.

    Note the caller's ``len(results) != 1`` uniqueness gate now sees the true
    count, so a barcode that looked unique only because its siblings sat past the
    cut-off will correctly stop merging. That is fewer merges and more honest
    ones: the previous behaviour could merge a pressing chosen by nothing better
    than result order.
    """
    client = _get_client()
    if not client:
        return []
    try:
        results = client.search(barcode, type="release")
        parsed: list[DiscMeta] = []
        for page_no in range(1, min(results.pages, _BARCODE_SEARCH_MAX_PAGES) + 1):
            parsed.extend(_parse_result(r) for r in results.page(page_no))
    except Exception as exc:
        log.debug("Discogs barcode search failed: %s", exc)
        return []
    return parsed
