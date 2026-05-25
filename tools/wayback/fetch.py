#!/usr/bin/env python3
"""
fetch.py — Retrieve archived content from the Wayback Machine (archive.org).

Requires:  pip install wayback   (or: uv pip install wayback)
           Python 3.9+

Usage (from project root):

  # Discover snapshots of a URL
  uv run python tools/wayback/fetch.py search <URL> [--from DATE] [--to DATE] [--limit N]

  # Fetch the best (most recent successful) snapshot to stdout or a file
  uv run python tools/wayback/fetch.py get <URL> [--timestamp TS] [--output FILE]
  uv run python tools/wayback/fetch.py get <URL> [--from DATE] [--to DATE] [--output FILE]

Date format for --from / --to:  YYYY  or  YYYY-MM-DD  or  YYYYMMDDHHMMSS

Examples:

  # List all known snapshots of an EAC offset page
  uv run python tools/wayback/fetch.py search "http://www.accuraterip.com/driveoffsets.htm"

  # Fetch most recent clean HTML snapshot and save
  uv run python tools/wayback/fetch.py get "http://www.accuraterip.com/driveoffsets.htm" \\
      --output driveoffsets.html

  # Fetch the snapshot closest to a specific date
  uv run python tools/wayback/fetch.py get "http://www.accuraterip.com/driveoffsets.htm" \\
      --timestamp 20200101 --output driveoffsets_2020.html

  # Discover snapshots between two years, then fetch all as files
  uv run python tools/wayback/fetch.py search "http://www.eac-offsetbase.invalid/" \\
      --from 2015 --to 2022 --limit 20

Notes:

  Content is always fetched via Mode.original (equivalent to Wayback's /id_/ modifier),
  which returns the raw archived HTML with no Wayback toolbar injected.  This is
  required for reliable HTML parsing.

  The wayback library handles rate limiting automatically (default: 1 search/sec,
  30 mementos/sec).  If you receive persistent 429 errors, use --from / --to to
  narrow searches so fewer CDX records are fetched per call.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from wayback import (  # type: ignore[import-untyped]
        Mode,
        WaybackClient,
        WaybackSession,
    )
    from wayback.exceptions import (  # type: ignore[import-untyped]
        BlockedByRobotsError,
        MementoPlaybackError,
        NoMementoError,
        RateLimitError,
    )
except ImportError as _import_err:
    print(f"error: cannot import 'wayback': {_import_err}", file=sys.stderr)
    print("Install with: uv pip install wayback", file=sys.stderr)
    sys.exit(1)


_USER_AGENT = "cdda2img-wayback-fetcher/1.0 (+mailto:keith.g.rt@gmail.com)"

# Maximum CDX records to scan when searching for the most-recent snapshot.
# CDX returns oldest-first; we iterate forward and keep the last 200 hit.
# 2000 is enough to cover sites archived hundreds of times per year.
_MAX_CDX_SCAN = 2000

_DATE_FORMATS = [
    "%Y%m%d%H%M%S",
    "%Y-%m-%d",
    "%Y%m%d",
    "%Y",
]


def _parse_date(s: str) -> datetime:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    msg = f"cannot parse date {s!r} — use YYYY, YYYY-MM-DD, YYYYMMDD, or YYYYMMDDHHMMSS"
    raise argparse.ArgumentTypeError(msg)


def _make_client(calls_per_second: float = 0.5) -> WaybackClient:
    session = WaybackSession(
        search_calls_per_second=calls_per_second,
        memento_calls_per_second=10,
    )
    session.headers.update({"User-Agent": _USER_AGENT})
    return WaybackClient(session=session)


# ── subcommand: search ────────────────────────────────────────────────────────


def cmd_search(args: argparse.Namespace) -> int:
    client = _make_client()
    kwargs: dict = {}
    if args.from_date:
        kwargs["from_date"] = args.from_date
    if args.to_date:
        kwargs["to_date"] = args.to_date

    try:
        results = client.search(args.original, **kwargs)
    except Exception as exc:
        print(f"error: CDX search failed: {exc}", file=sys.stderr)
        return 1

    count = 0
    print(f"{'Timestamp':<16}  {'Status':>6}  {'MIME':<24}  {'Length':>8}  URL")
    print("-" * 90)
    try:
        for record in results:
            if args.limit and count >= args.limit:
                break
            ts = record.timestamp.strftime("%Y%m%d%H%M%S") if record.timestamp else "?"
            status = str(record.statuscode) if record.statuscode else "?"
            mime = (record.mime_type or "")[:24]
            length = str(record.length) if record.length else "?"
            print(f"{ts:<16}  {status:>6}  {mime:<24}  {length:>8}  {record.original}")
            count += 1
    except StopIteration:
        pass
    except KeyboardInterrupt:
        pass

    print(f"\n{count} snapshot(s) listed.")
    return 0


# ── subcommand: get ───────────────────────────────────────────────────────────


def _find_best_record(
    client: WaybackClient,
    original: str,
    timestamp: str | None,
    from_date: datetime | None,
    to_date: datetime | None,
):
    """Return the best CdxRecord for the given URL and constraints.

    With --timestamp: first HTTP-200 snapshot at or after that time.
    Without --timestamp: most recent HTTP-200 snapshot within the date range.
    CDX returns results oldest-first, so 'most recent' requires scanning forward.
    """
    kwargs: dict = {}
    if from_date:
        kwargs["from_date"] = from_date
    if to_date:
        kwargs["to_date"] = to_date

    if timestamp:
        # Start CDX scan from the requested timestamp to avoid iterating older records
        ts_dt = _parse_date(timestamp)
        if "from_date" not in kwargs:
            kwargs["from_date"] = ts_dt
        results = client.search(original, **kwargs)
        for record in results:
            if record.statuscode == 200:
                return record  # first 200 at or after ts_dt
        return None
    else:
        # CDX is oldest-first; iterate forward, keep the latest 200 seen.
        # Cap iteration to avoid hanging on heavily-archived URLs.
        results = client.search(original, **kwargs)
        best = None
        for i, record in enumerate(results):
            if record.statuscode == 200:
                best = record
            if i >= _MAX_CDX_SCAN - 1:
                print(
                    f"warning: CDX scan capped at {_MAX_CDX_SCAN} records; "
                    "result may not be the most recent snapshot. "
                    "Use --from / --to to narrow the search window.",
                    file=sys.stderr,
                )
                break
        return best


def cmd_get(args: argparse.Namespace) -> int:
    client = _make_client()

    record = _find_best_record(
        client,
        args.original,
        args.timestamp,
        args.from_date,
        args.to_date,
    )

    if record is None:
        print(
            f"error: no successful (HTTP 200) snapshot found for {args.original!r}",
            file=sys.stderr,
        )
        if args.from_date or args.to_date:
            print(
                "       try broadening the date range or omitting --from / --to",
                file=sys.stderr,
            )
        return 1

    ts_str = (
        record.timestamp.strftime("%Y%m%d%H%M%S") if record.timestamp else "unknown"
    )
    print(f"fetching snapshot {ts_str}  {record.original}", file=sys.stderr)

    try:
        response = client.get_memento(record, mode=Mode.original)
    except NoMementoError:
        print(
            f"error: snapshot {ts_str} is not available for playback", file=sys.stderr
        )
        return 1
    except BlockedByRobotsError:
        print(
            f"error: archive.org blocked access to {record.original!r} (robots.txt)",
            file=sys.stderr,
        )
        return 1
    except RateLimitError:
        print(
            "error: rate limited by archive.org — wait 60 s and retry", file=sys.stderr
        )
        return 1
    except MementoPlaybackError as exc:
        print(f"error: playback error for snapshot {ts_str}: {exc}", file=sys.stderr)
        return 1

    content = response.content

    if args.output:
        dest = Path(args.output)
        dest.write_bytes(content)
        print(f"saved {len(content)} bytes → {dest}", file=sys.stderr)
    else:
        sys.stdout.buffer.write(content)

    return 0


# ── CLI ───────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fetch.py",
        description="Retrieve archived content from the Wayback Machine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p_search = sub.add_parser("search", help="list snapshots for a URL via CDX")
    p_search.add_argument("original", help="URL to search")
    p_search.add_argument(
        "--from",
        dest="from_date",
        metavar="DATE",
        type=_parse_date,
        help="start of date range (YYYY, YYYY-MM-DD, or YYYYMMDDHHMMSS)",
    )
    p_search.add_argument(
        "--to",
        dest="to_date",
        metavar="DATE",
        type=_parse_date,
        help="end of date range",
    )
    p_search.add_argument(
        "--limit", type=int, metavar="N", help="maximum number of snapshots to list"
    )

    # get
    p_get = sub.add_parser("get", help="fetch the content of a snapshot")
    p_get.add_argument("original", help="URL to retrieve")
    p_get.add_argument(
        "--timestamp",
        metavar="TS",
        help="fetch snapshot nearest to this time (YYYYMMDDHHMMSS or YYYY-MM-DD)",
    )
    p_get.add_argument(
        "--from",
        dest="from_date",
        metavar="DATE",
        type=_parse_date,
        help="search start date when no --timestamp given",
    )
    p_get.add_argument(
        "--to", dest="to_date", metavar="DATE", type=_parse_date, help="search end date"
    )
    p_get.add_argument(
        "--output", "-o", metavar="FILE", help="write to FILE instead of stdout"
    )

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "search":
        return cmd_search(args)
    elif args.command == "get":
        return cmd_get(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
