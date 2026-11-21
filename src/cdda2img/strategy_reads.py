"""The recovery bench's read plans, shared by the bench and the rip path.

Each strategy is a generator of :class:`ReadReq`: *which* sectors to re-read, at
what speed, in what order. Both callers splice every read into a working copy of
the track and check AccurateRip after each one, so a strategy describes nothing
else.

**One definition, two callers.** ``tools/strategy_bench.py`` measured these plans
on hardware in July 2026 (``docs/reference/accudisc-migration-plan.md`` §9.1), and
the shipped profiles carry those scores. Until 2026-11-21 the rip path did not run
them at all: every profile but ``span-flagged`` ran as the speed ladder while PROV
named the profile. The bench now imports this module rather than keeping its own
copy, so the plan the rip runs and the plan that was scored cannot drift apart
(the same reasoning as ``tools/ctdb_fixture.py`` importing the repair it tests).

**The rip dispatches on fields, never on names** (:func:`strategy_kind`). A user
profile may shadow a shipped one, so ``max-variation`` is whatever profile has
``granularity = "sector"`` and ``variation = "full"``, and PROV records the kind
that ran (``recovery_strategy``), not the file it came from.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

#: The bench's arms, in its reporting order. ``track-constant`` and
#: ``track-ladder`` are the speed ladder in the rip path (``rungs_for`` decides
#: single or full); the other five are what :func:`strategy_kind` can return.
STRATEGIES = [
    "sector-hammer",
    "sector-runup",
    "track-constant",
    "track-ladder",
    "whole-disc",
    "span-fixed",
    "max-variation",
]

#: Choices ``max-variation`` draws from on every read (bench, 2026-07-23).
MAX_VARIATION_RUN_UPS = (0, 8, 32, 128, 512)
MAX_VARIATION_TAILS = (2, 16, 64)


@dataclass(frozen=True)
class ReadReq:
    """One read a strategy asks for: sectors [start, start+count) at a given speed."""

    start: int
    count: int
    speed: int


def track_bounds(track: int, track_lsns: list[int], leadout: int) -> tuple[int, int]:
    """``[start, end)`` sectors of 1-based *track*; the last runs to the lead-out."""
    start = track_lsns[track - 1]
    end = track_lsns[track] if track < len(track_lsns) else leadout
    return start, end


def strategy_reads(
    name: str,
    track: int,
    flagged: list[int],
    track_lsns: list[int],
    leadout: int,
    ladder: list[int],
    base_speed: int,
    runup: int,
    span: int,
    rng: random.Random,
    max_attempts: int,
) -> Iterator[ReadReq]:
    """The reads strategy *name* makes for *track*, at most *max_attempts* of them.

    *flagged* are the capture pass's flagged LBAs; only those inside the track are
    used, and a track with none is aimed at its first sector. Moved verbatim from
    the bench (``_strategy_reads``, 2026-07-23): a change here changes what both the
    bench and the rip mean by each name, so it needs the bench's evidence first.
    """
    start, end = track_bounds(track, track_lsns, leadout)
    in_track = [s for s in flagged if start <= s < end] or [start]

    for i in range(max_attempts):
        if name == "sector-hammer":
            lba = in_track[i % len(in_track)]
            yield ReadReq(lba, 1, base_speed)
        elif name == "sector-runup":
            lba = in_track[i % len(in_track)]
            s = max(start, lba - runup)
            yield ReadReq(s, lba - s + 1 + 2, base_speed)
        elif name == "track-constant":
            yield ReadReq(start, end - start, base_speed)
        elif name == "track-ladder":
            yield ReadReq(start, end - start, ladder[i % len(ladder)])
        elif name == "whole-disc":
            yield ReadReq(0, leadout, base_speed)
        elif name == "span-fixed":
            # The control that separates SPAN SIZE from VARIATION. max-variation wins
            # while reading 145-664 sectors; sector-runup fails reading 35. Those two
            # differ in span AND in speed/run-up randomness, so the first run could not
            # say which mattered. This arm is a fixed large span at a constant speed:
            # if it wins as often as max-variation, span size is the whole story and
            # the randomness is incidental.
            lba = in_track[i % len(in_track)]
            s = max(start, lba - span)
            yield ReadReq(s, lba - s + 1 + 2, base_speed)
        elif name == "max-variation":
            lba = rng.choice(in_track)
            k = rng.choice(MAX_VARIATION_RUN_UPS)
            s = max(0, lba - k)
            yield ReadReq(
                s,
                lba - s + 1 + rng.choice(MAX_VARIATION_TAILS),
                rng.choice(ladder),
            )
        else:
            msg = f"unknown strategy {name}"
            raise ValueError(msg)


def strategy_kind(profile: Any) -> str | None:
    """The bench strategy a profile's fields describe, or ``None`` for the ladder.

    ``None`` for ``granularity = "track"`` (the speed ladder: ``track-ladder`` or
    ``track-constant``, as ``rungs_for`` binds it) and ``"span"`` (the flagged-span
    rung). Validation (``validation.PROFILE_SCHEMA``) refuses the field
    combinations this cannot place, so every valid profile maps to exactly one
    answer.
    """
    if profile.granularity == "whole-disc":
        return "whole-disc"
    if profile.granularity != "sector":
        return None
    if profile.variation == "full":
        return "max-variation"
    if profile.span > 0:
        return "span-fixed"
    if profile.run_up > 0:
        return "sector-runup"
    return "sector-hammer"
