"""A per-rip budget on the drive work recovery may ask for (kgr, 2026-11-25).

kgr: "check that the total number of read-passes does not escalate to the point
where we create a packet storm on the drive, potentially damaging it". On a disc
with heavy damage the recovery chain's sector strategies scale with flagged
sectors x speeds x passes, so without a bound one rip could ask for ~189,000
re-reads, each after a far seek, and ~45,000 spindle speed changes.

Every recovery read cdda2img issues is charged here **before** it is sent:

* ``reads``: one per read call (a ladder attempt, a strategy read, a span re-read);
* ``seeks``: the far reads we ask AccuDisc for (``cache_defeat``);
* ``speed_changes``: a read whose requested speed differs from the previous
  recovery read's. The first recovery read counts as one: the capture left the
  drive at a speed this module cannot see.

When a limit would be passed, :class:`DriveBudgetSpent` is raised instead, and the
read is never sent. It is deliberately NOT a ``RuntimeError`` or ``OSError``: the
recovery loops catch those to skip a failed read and carry on, and a spent budget
must stop recovery, not be skipped past.

The engine's OWN re-reads inside one call (its error retries, Q rescue, verify
passes and their far reads) cannot be charged before they happen: they are the
engine's to decide. Since AccuDisc 0.57.0 each read reports what it spent
(``ReadStats.budget_spent_cmds`` / ``budget_spent_changes``), and the seam books
that here **after** the read returns (:func:`book_engine`), for the record
(PROV ``recovery_engine_work``). Booking never raises.

``max_engine_cmds`` (config ``recovery_max_engine_cmds``, kgr 2026-11-25: 10,000)
is **recorded only, not enforced**. Enforcing it means passing the per-rip
remainder into each read as AccuDisc's ``budget_cmds``, which changes what a
read does and waits for the end of kgr's per-profile hardware series. When it
is adopted, mind AccuDisc's contract: 0 there means *unlimited*, so a remainder
of 0 must refuse the read here, never be passed through (outbound 279).

The budget is carried in a :class:`contextvars.ContextVar` set by the rip for its
recovery only (:func:`budget`), so nothing outside a rip is ever charged and the
deep recovery call chain needs no new parameter.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass


class DriveBudgetSpent(Exception):
    """The next recovery read would pass a drive-work limit; it was not sent."""

    def __init__(self, what: str, limit: int) -> None:
        super().__init__(f"{what}:{limit}")
        self.what = what
        self.limit = limit


@dataclass
class DriveWork:
    """Limits (0 = none) and what recovery has asked of the drive so far.

    The ``engine_*`` fields are what the engine reported spending inside those
    reads (:meth:`book_engine`); ``max_engine_cmds`` is carried, not enforced."""

    max_reads: int = 0
    max_speed_changes: int = 0
    max_engine_cmds: int = 0
    reads: int = 0
    seeks: int = 0
    speed_changes: int = 0
    last_speed: int | None = None
    engine_cmds: int = 0
    engine_speed_changes: int = 0
    engine_max_read_cmds: int = 0
    engine_reads: int = 0

    def charge(self, speed: int | None, *, far_read: bool) -> None:
        """Count one read before it is sent, or raise :class:`DriveBudgetSpent`."""
        change = speed is not None and speed != self.last_speed
        if self.max_reads and self.reads + 1 > self.max_reads:
            raise DriveBudgetSpent("reads", self.max_reads)
        if (
            change
            and self.max_speed_changes
            and self.speed_changes + 1 > self.max_speed_changes
        ):
            raise DriveBudgetSpent("speed_changes", self.max_speed_changes)
        self.reads += 1
        self.seeks += int(far_read)
        if change:
            self.speed_changes += 1
            self.last_speed = speed

    def book_engine(self, cmds: int, speed_changes: int) -> None:
        """Add what one read's engine spent on its own recovery. Never raises."""
        self.engine_cmds += cmds
        self.engine_speed_changes += speed_changes
        self.engine_max_read_cmds = max(self.engine_max_read_cmds, cmds)
        self.engine_reads += 1

    def record(self) -> str:
        """PROV ``recovery_drive_work``: ``reads:N,seeks:N,speed_changes:N``."""
        return (
            f"reads:{self.reads},seeks:{self.seeks},speed_changes:{self.speed_changes}"
        )

    def engine_record(self) -> str | None:
        """PROV ``recovery_engine_work``, or ``None`` when no read reported any.

        ``None`` rather than zeros: an engine before 0.57.0 reports nothing, and
        "not reported" must not read as "spent nothing"."""
        if not self.engine_reads:
            return None
        return (
            f"cmds:{self.engine_cmds},speed_changes:{self.engine_speed_changes},"
            f"max_read_cmds:{self.engine_max_read_cmds},"
            f"reported_reads:{self.engine_reads}"
        )


_CURRENT: contextvars.ContextVar[DriveWork | None] = contextvars.ContextVar(
    "cdda2img_drive_work"
)


@contextmanager
def budget(
    max_reads: int, max_speed_changes: int, max_engine_cmds: int = 0
) -> Iterator[DriveWork]:
    """Charge every recovery read inside the block to one :class:`DriveWork`."""
    work = DriveWork(
        max_reads=max_reads,
        max_speed_changes=max_speed_changes,
        max_engine_cmds=max_engine_cmds,
    )
    token = _CURRENT.set(work)
    try:
        yield work
    finally:
        _CURRENT.reset(token)


def charge(speed: int | None, *, far_read: bool) -> None:
    """Charge one recovery read to the rip's budget; a no-op outside a rip."""
    work = _CURRENT.get(None)
    if work is not None:
        work.charge(speed, far_read=far_read)


def book_engine(cmds: int, speed_changes: int) -> None:
    """Book one read's engine recovery spend to the rip; a no-op outside a rip."""
    work = _CURRENT.get(None)
    if work is not None:
        work.book_engine(cmds, speed_changes)
