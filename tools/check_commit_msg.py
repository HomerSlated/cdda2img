#!/usr/bin/env python3
"""Refuse a commit message carrying a Claude session URL or session trailer.

The rule -- never let a session URL or private session data reach a commit
message, keeping only ``Co-Authored-By`` -- existed here as a convention and was
enforced by **nothing**: not by the (git-ignored, machine-local) ``scripts/``
tooling, and not by ``.pre-commit-config.yaml``, whose hooks are formatting and
syntax only. A rule documented in prose and enforced nowhere is the same shape
as a test suite that reports PASS with its tests deleted.

AccuDisc found the identical defect on their side the same day, one layer up:
their notification gate lived in a git-ignored ``scripts/`` file, so it ran on
one machine and existed in no clone. Their fix and this one share a sentence --
**the script checks, the tracked hook enforces.**

Deliberately narrow. It blocks two unambiguous markers rather than guessing at
"private data", because a hook with false positives is disabled wholesale and a
disabled hook enforces nothing. ``Co-Authored-By: Claude ... @anthropic.com``
does not match either pattern and is explicitly fine.

One known and accepted cost: a commit message that *documents this guard* cannot
quote the literal markers -- the first message to install it tripped on its own
prose. Say "a session URL" rather than the string. There is deliberately **no
bypass env var**: an escape hatch on this particular rule would be reached for
reflexively, and the thing being protected is a one-way publication to a public
repository.
"""

from __future__ import annotations

import argparse
import re
import sys

#: (label, pattern) pairs. Both are markers no legitimate message carries.
FORBIDDEN: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Claude session URL", re.compile(r"claude\.ai", re.IGNORECASE)),
    # Hyphen/underscore only, and it must look like a trailer. A space was
    # allowed here for one commit and matched the ordinary English phrase
    # "Claude session URL" in this tool's own commit message -- a false positive
    # on prose describing the guard. Nothing is lost by narrowing: a real session
    # URL is still caught by the pattern above, and the trailer form is not spelled
    # with a space.
    ("session trailer", re.compile(r"Claude[-_]Session\s*:", re.IGNORECASE)),
)


def offending_lines(text: str) -> list[tuple[int, str, str]]:
    """``(line number, label, line)`` for every line matching a forbidden marker.

    Comment lines are skipped: git strips them before the message is stored, and
    the commit template itself may legitimately mention these terms.
    """
    found: list[tuple[int, str, str]] = []
    for n, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        for label, pattern in FORBIDDEN:
            if pattern.search(line):
                found.append((n, label, line.strip()))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="+", help="commit message file(s)")
    args = parser.parse_args(argv)

    bad = False
    for path in args.path:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            print(f"❌ cannot read commit message {path}: {exc}", file=sys.stderr)
            return 1
        for n, label, line in offending_lines(text):
            bad = True
            print(f"❌ {path}:{n}: {label} in commit message: {line}", file=sys.stderr)

    if bad:
        print(
            "\n   Commit messages are pushed to a public repository.\n"
            "   Remove the URL/trailer; keep Co-Authored-By.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
