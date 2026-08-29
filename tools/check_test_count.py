#!/usr/bin/env python3
"""Fail when the collected test count falls below a floor.

**This guard deliberately lives outside ``tests/``.** A test that counted tests
would be collected from the same tree it is checking, so the one failure it
exists to catch -- a test file emptied or truncated by a bad edit -- would
delete the check along with the evidence.

That failure is not hypothetical. AccuDisc truncated their ``test_binding.py``
from 1942 lines to 260 with a bad string edit on 2026-08-29 and **the suite
still reported PASS**, because the runner at the bottom of the file went with
it; the only surviving tell was a missing per-test count line. A "falsification"
of a new guard, run in that state, proved nothing.

The invariant is therefore that the count must be observed to **rise**. "Passed"
is not the check -- the number is. The floor is a floor and not an exact count
because an exact count fails on every legitimate addition, and a check that
fires on ordinary work gets removed rather than obeyed.

Bump ``FLOOR`` deliberately, in the commit that earns it.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys

import pytest

#: Minimum tests the suite must collect. 1733 collected on 2026-08-29.
FLOOR = 1700


class _Counter:
    """Records how many items collection produced."""

    def __init__(self) -> None:
        self.count: int | None = None

    def pytest_collection_modifyitems(self, items: list[object]) -> None:
        self.count = len(items)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--floor",
        type=int,
        default=FLOOR,
        help=f"minimum acceptable test count (default {FLOOR})",
    )
    args = parser.parse_args(argv)

    counter = _Counter()
    # Collection prints every node id; that is 1733 lines of noise inside
    # ``make check``. Capture it and replay only when something went wrong,
    # where the listing is exactly what a reader needs.
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        rc = int(
            pytest.main(
                ["--collect-only", "-q", "--no-header", "-p", "no:cacheprovider"],
                plugins=[counter],
            )
        )
    if rc != 0:
        print(captured.getvalue(), file=sys.stderr)
        print(f"❌ collection itself failed (pytest exit {rc})", file=sys.stderr)
        return 1
    # None means the hook never ran, which is not the same as "collected zero"
    # and must not be allowed to compare as a number.
    if counter.count is None:
        print("❌ collection reported no result at all", file=sys.stderr)
        return 1
    if counter.count < args.floor:
        print(
            f"❌ collected {counter.count} tests, floor is {args.floor}.\n"
            "   A test file may have been truncated, emptied, or left uncollected.\n"
            "   If the drop is intentional, lower FLOOR in this file in the same commit.",
            file=sys.stderr,
        )
        return 1
    print(f"✅ {counter.count} tests collected (floor {args.floor})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
