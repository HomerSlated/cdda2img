"""Exit codes, the rip abort, and its failure report.

**The policy** (kgr, 2026-11-21): "any outcome other than 100% AR verified tracks
should not result in an RBI being written, as the result is meaningless. The
objective is perfect archival." The user still has the disc, so a rip that cannot
complete aborts with an error and leaves them to diagnose it. That includes an
online service that cannot be reached, and one that answers "nothing found":
both leave the archive without data it needs.

**Exit codes, one per further action** (kgr: "what unique further action does
this exit code prompt?"):

== =====================================================================
0  nothing: success
1  fix the command, the configuration or the input, then re-run (argparse
   usage errors included: they used to exit 2)
2  wait and re-run unchanged: a service did not answer
3  investigate: read the failure report. The disc, the drive, or our
   query is at fault, and a re-run as-is will fail the same way
== =====================================================================

130 (Ctrl-C) stays as the shell's convention. Nothing is written for 2 or 3.

**The failure report is load-bearing.** With no container there is no PROV and
no RLOG, so the reason, the context the rip had gathered, and every online query
with its answer (:mod:`cdda2img.query_log`) go to a file whose path is printed on
stderr. A failed rip is also where the evidence for tuning recovery comes from,
which is one more reason it must outlive the terminal.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

EXIT_OK = 0
EXIT_FIX_INVOCATION = 1
EXIT_RETRY_LATER = 2
EXIT_INVESTIGATE = 3

_ACTION = {
    EXIT_FIX_INVOCATION: "fix the command or configuration, then re-run",
    EXIT_RETRY_LATER: "a service did not answer; re-run when it is reachable",
    EXIT_INVESTIGATE: "read the failure report; re-running unchanged will fail again",
}


class RipAborted(Exception):
    """A rip that will write nothing. Raised anywhere in the rip pipeline and
    turned into an exit code by ``_dispatch``; ``rip_image``'s ``finally`` still
    restores the drive and removes the scratch.

    *details* are the lines that explain *reason*; *context* is what the rip knew
    when it stopped (identifiers, per-track results, recovery records), written to
    the report as ``key=value``.
    """

    def __init__(
        self,
        code: int,
        reason: str,
        details: list[str] | None = None,
        context: dict[str, str] | None = None,
    ) -> None:
        super().__init__(reason)
        if code not in _ACTION:
            msg = f"not an abort exit code: {code}"
            raise ValueError(msg)
        self.code = code
        self.reason = reason
        self.details = list(details or [])
        self.context = dict(context or {})

    def add_context(self, context: dict[str, str]) -> RipAborted:
        """Merge *context* under what the raiser supplied (the raiser wins)."""
        self.context = {**context, **self.context}
        return self


def reports_dir() -> Path:
    """``$XDG_STATE_HOME/cdda2img/reports`` (default ``~/.local/state/...``)."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "cdda2img" / "reports"


def write_report(abort: RipAborted, directory: Path | None = None) -> Path:
    """Write the failure report for *abort*; return its path."""
    from cdda2img import query_log

    directory = directory or reports_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = directory / f"rip-{stamp}.txt"
    n = 1
    while path.exists():
        n += 1
        path = directory / f"rip-{stamp}-{n}.txt"

    lines = [
        f"cdda2img rip aborted at {stamp}",
        f"Exit status {abort.code}: {_ACTION[abort.code]}.",
        "",
        "Reason:",
        f"  {abort.reason}",
    ]
    if abort.details:
        lines += ["", "Details:", *(f"  {d}" for d in abort.details)]
    if abort.context:
        lines += ["", "Context:"]
        lines += [f"  {k}={v}" for k, v in sorted(abort.context.items())]
    queries = query_log.records()
    lines += ["", f"Online queries ({len(queries)}), in the order they were made:"]
    for i, q in enumerate(queries, 1):
        lines += ["", f"[{i}] {q.service}", f"  query:   {q.query}"]
        lines.append(f"  outcome: {q.outcome}")
        if q.detail:
            lines.append("  response:")
            lines += [f"    {line}" for line in q.detail.splitlines()]
    path.write_text(query_log.redact("\n".join(lines)) + "\n")
    return path


def report_to_stderr(abort: RipAborted, report: Path | None) -> int:
    """Print the abort on stderr and return its exit code."""
    import sys

    print(f"Rip aborted, nothing written: {abort.reason}", file=sys.stderr)
    for d in abort.details:
        print(f"  {d}", file=sys.stderr)
    if report is not None:
        print(f"Failure report: {report}", file=sys.stderr)
    print(f"Exit status {abort.code}: {_ACTION[abort.code]}.", file=sys.stderr)
    return abort.code
