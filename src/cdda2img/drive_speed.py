"""
drive_speed.py — read-speed **policy**. Every command to the drive goes through
:mod:`cdda2img.accudisc_reader`.

This module used to own "the ioctl side" of speed control while AccuDisc owned the
reads — a split that put the two halves of one conversation on two different SCSI
commands. Setting went out as ``CDROM_SELECT_SPEED`` (the kernel's ``SET CD SPEED``)
while ``Device.set_speed`` prefers **SET STREAMING (0xB6)**, an enforced ceiling, and
only falls back to ``SET CD SPEED``. So a ceiling AccuDisc installed was being cleared
by a different command than the one that set it, on a drive-dependent whether-it-works
basis, with nothing on either side able to report which had happened. Retired
2026-08-09 (kgr): everything that touches the disc goes through the one engine.

Reading: there is no Linux ioctl for CD speed. The trustworthy source is MODE SENSE
page 2A read at the *correct offsets* (max = page[8:10], current = page[14:16] — the
fields cdrdao drive-info reports; the "page 2A lies" folklore is naive readers using
the wrong fields). ``Device.get_speed`` reads exactly those fields (validated
kB/s-identical to cdrdao drive-info at 4X and 40X on the PX-716A).

Best-effort throughout: every failure path is swallowed so a rip is never affected.

Public interface:
    read_drive_speed(device) -> (current_kbps, max_kbps) | (None, None)
    current_speed_x(device) -> int | None
    request_speed(device, nx) -> bool
    restore_drive_speed(device, target_x=None) -> None
    admitted_ladder(device) -> list[int]
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: the runtime import stays inside read_speed_rows, because
    # accudisc_reader imports nothing from here and this module is imported by
    # the rip path long before any drive is touched.
    from cdda2img.accudisc_reader import SpeedRow

log = logging.getLogger(__name__)

_KBPS_PER_X = 176  # 1x CD = 75 sectors/s * 2352 bytes / 1000 ≈ 176 kB/s

#: AccuDisc's verdict tokens that :func:`admitted_ladder` keys on. The others —
#: ``duplicate:<n>`` and ``quantized:<n>`` — carry the rung they collapse onto in
#: the token itself, so they are matched by exclusion rather than by name.
#: ``unknown`` means "not judged", which is NOT the same as "rejected".
_VERDICT_ADMITTED = "admitted"
_VERDICT_UNKNOWN = "unknown"


def read_drive_speed(device: str) -> tuple[int | None, int | None]:
    """Return ``(current_kbps, max_kbps)``, or ``(None, None)``.

    Delegates to :func:`accudisc_reader.read_speed`. Never raises: any failure
    yields ``(None, None)`` and callers treat that as "unknown".
    """
    from cdda2img.accudisc_reader import read_speed

    return read_speed(device)


def request_speed(device: str, nx: int) -> bool:
    """Ask the drive for *nx*, via the seam. True on success; never raises.

    Does NOT verify the drive honoured it — a successful command and an honoured
    rate are different claims, and the caller that cares (``_apply_read_speed``)
    reads page 2A back itself. ``nx = 0`` is the "fastest possible" sentinel.
    """
    from cdda2img.accudisc_reader import set_speed

    try:
        return set_speed(device, nx)
    except (RuntimeError, OSError) as exc:
        log.warning("could not set %s to %dX: %s", device, nx, exc)
        return False


def current_speed_x(device: str) -> int | None:
    """The drive's current read rate as a plain multiplier, or ``None``.

    The value :func:`restore_drive_speed` is meant to be handed back later, so it
    is captured *before* anything sets a speed. ``None`` when the drive did not
    report — which callers must keep distinct from a number, since restoring to a
    speed nobody measured is worse than not restoring at all.

    MODE SENSE page 2A: instant, no disc spin-up, no command to the media. Never
    raises: this is called for the rip's status line as well as for the restore,
    and neither is worth failing a rip over.
    """
    try:
        current, _maximum = read_drive_speed(device)
    except (RuntimeError, OSError):
        return None
    return max(1, current // _KBPS_PER_X) if current else None


def restore_drive_speed(device: str, target_x: int | None = None) -> None:
    """Put the drive back where we found it — or at max when we do not know.

    *target_x* is the multiplier captured by :func:`current_speed_x` before the rip
    touched anything. **Restore-as-found is the semantic, and it is a change**: this
    used to restore unconditionally to maximum, which was right when the thing being
    undone was cd-paranoia's persistent ``-S 1``. cd-paranoia is gone from the tree,
    and the only process that now throttles this drive is us — so a drive found at 8x
    was deliberately set to 8x by the user or by another program, and blasting it to
    max on our way out is us editing someone else's setting.

    ``target_x=None`` keeps the old read-then-conditional behaviour for callers with
    nothing captured (the ladder probe, which leaves the drive at its last rung and
    genuinely does want it back at the top).

    Best-effort — never raises.
    """
    current, maximum = read_drive_speed(device)
    current_x = current // _KBPS_PER_X if current else None

    if target_x is not None:
        if current_x == target_x:
            log.debug("drive %s already at %dX", device, target_x)
            return
        if request_speed(device, target_x):
            log.info(
                "drive %s read speed: %sX -> %dX (restored as found)",
                device,
                current_x if current_x else "?",
                target_x,
            )
        return

    if maximum is None:
        # Couldn't read — un-throttle blind with the "fastest" sentinel.
        if request_speed(device, 0):
            log.info("drive %s read speed reset to max (speed unknown)", device)
        return

    if current == maximum:
        log.debug("drive %s already at max read speed (%d kB/s)", device, maximum)
        return

    nx = max(1, maximum // _KBPS_PER_X)
    if request_speed(device, nx):
        log.info(
            "drive %s read speed: %sX -> %dX (restored)",
            device,
            current_x if current_x else "?",
            nx,
        )


def read_speed_rows(device: str) -> list[SpeedRow]:
    """``accudisc speeds`` rows as ``(req, page2a, measured, verdict)``.

    Each row is a timed streaming read at a requested rung: *req* is what we asked
    for, *page2a* what mode page 2A reported the drive settled on (0 = the page did
    not report), *measured* the achieved throughput in X, *verdict* AccuDisc's own
    judgement (``None`` from an engine that does not supply one).

    The probe performs real reads, so it both warms the disc — letting a
    self-throttling governor settle to its true ceiling — and leaves the drive at
    the last rung it tried. :func:`admitted_ladder` restores it afterwards.

    Delegates to :func:`accudisc_reader.speed_ladder_rows`; kept as a name here
    because :func:`admitted_ladder` is the policy that consumes it.
    """
    from cdda2img.accudisc_reader import speed_ladder_rows

    return speed_ladder_rows(device)


def admitted_ladder(device: str) -> list[int]:
    """The rungs this drive honoured *distinctly*, fastest first (migration plan §9.3).

    Three rules in priority order. Each exists because the one below it cannot see
    something, and the top one closes the §9.3 known gap (2026-07-29).

    **1. Verdict (preferred).** Admit rows AccuDisc judged ``admitted``. Their
    verdict is a *rate* comparison taken at three radii, so it distinguishes a real
    rung from one that merely got its request echoed back — which is the thing
    ``req == page2a`` structurally cannot do, since both operands derive from the
    same advertised ceiling and the equality therefore cross-checks the drive's
    quantiser rather than its ceiling. Measured on Tracy, 2026-07-29, uncap
    latched: ``req=48 page2a=48 measured=22.96`` sits **above** ``req=40 page2a=40
    measured=23.68``. Page 2A advertises the 48x *data* ceiling while CD-DA is
    governed to 40x, so those two rows are one speed wearing two labels — and the
    faster-looking one is the slower one. The old rule admitted both and produced
    ``[48, 40, 32, 24, 8, 4]``; the verdict rule yields ``[40, 32, 24, 8, 4]``,
    matching AccuDisc's ``ladder admitted=`` line exactly.

    **2. ``req == page2a``** — the previous rule, kept for an engine that reports no
    verdict (an older build, or a ``points=1`` probe where nothing was judged). It
    still catches outright quantisation: a row where they differ means the drive
    silently read at 8x under a label saying 32x, which mislabels every measurement
    taken there. It cannot catch case 1, which is why it is second.

    **3. ``measured``** — when *every* row reports ``page2a == 0`` the page did not
    report at all (not the same as "quantised to zero"), so rule 2 would admit
    nothing. Collapse rungs achieving the same rate. AccuDisc caught this case: on a
    drive with no usable page 2A, rule 2 alone yields a silently empty ladder.

    **Outcome guard** — if the ladder still resolves empty by any path, degrade to a
    single rung at the drive's reported maximum and warn. The guard is on the
    *outcome*, not on the causes, because the causes are open-ended: a drive
    reporting a real ``page2a`` that never equals ``req`` (it supports 10x/20x while
    we probe {40,32,24,16,8,4}) is non-zero, so rule 3 does not fire, and the ladder
    is empty again for a completely different reason.

    Note the guard must not fire on a legitimately empty *verdict* set. AccuDisc
    warn that an empty ``admitted_ladder()`` at ``points=1`` means "nothing was
    judged", not "no rungs" — we always probe at ``points=3``, and the branch is
    entered only when some row carries a verdict at all, so an all-``unknown``
    result falls through to rule 2 rather than degrading.

    The ladder is a property of **drive x disc**, not of the drive: a self-throttling
    governor caps a degraded disc regardless of what the drive can do. The PX-716A
    admitted [32, 24, 8, 4] on ABBA *Gold* in July and [8, 4] on the same disc once
    it had degraded further. Never cache this per drive.
    """
    # Captured before the probe, restored after it. This used to be a bare
    # `restore_drive_speed(device)` — i.e. a blast to MAX — which was harmless while
    # the rip never requested a speed of its own. It is not harmless now: with
    # `--ad-speed 8` the sequence became read-at-8, probe, *set 40*, then run the
    # recovery ladder, so the re-reads inherited a rate nobody asked for. That is the
    # D1 subq_speed_cliff shape, and the D1 guard cannot see it — that guard only
    # checks for restores sited before the disc read, and this one is after.
    entry_x = current_speed_x(device)

    rows = read_speed_rows(device)
    ladder: list[int] = []

    # "Some row was JUDGED", not "some row has a verdict string": `unknown` is a
    # verdict and it is truthy, so gating on presence would send an all-unknown
    # probe into the verdict branch, out with an empty ladder, and on to the
    # degrade guard — reporting one rung at max for a drive that has several.
    # AccuDisc flagged this shape directly (§ce.3): an empty admitted set means
    # "nothing was judged", never "no rungs".
    if any(r.verdict and r.verdict != _VERDICT_UNKNOWN for r in rows):
        # Preferred: AccuDisc's own verdict, a rate comparison across three radii.
        ladder = sorted(
            {r.requested for r in rows if r.verdict == _VERDICT_ADMITTED}, reverse=True
        )
    elif any(r.page2a for r in rows):
        ladder = sorted(
            {r.page2a for r in rows if r.requested == r.page2a and r.page2a},
            reverse=True,
        )
    elif rows:
        # No page 2A anywhere: rank by achieved throughput, one rung per distinct rate.
        by_rate: dict[int, int] = {}
        for r in rows:
            by_rate.setdefault(round(r.measured), r.requested)
        ladder = [by_rate[k] for k in sorted(by_rate, reverse=True)]

    # The probe leaves the drive at its last rung, so this is not optional — but it
    # restores to where the caller had it, not to max. `entry_x is None` means the
    # drive would not report, and restoring to a rate nobody measured is worse than
    # leaving it: the rip's own `finally` is the backstop either way.
    if entry_x is not None:
        restore_drive_speed(device, entry_x)

    if not ladder:
        _, maximum = read_drive_speed(device)
        top = max(1, (maximum or _KBPS_PER_X) // _KBPS_PER_X)
        log.warning(
            "drive %s admitted no speed rungs (%d probe rows); "
            "degrading to a single rung at %dX",
            device,
            len(rows),
            top,
        )
        return [top]
    return ladder


# ``probe_speed_ladder`` — the legacy set-then-read-back sweep over a candidate
# table — was DELETED 2026-08-09. It had no caller in ``src/``: `admitted_ladder`
# superseded it, deriving rungs from AccuDisc's timed reads at three radii rather
# than from a page-2A read-back of what we had just asked for.
#
# It is worth recording what went with it, because the replacement is not strictly
# better on every axis. That sweep was the **only** code in this project that set a
# speed and then independently checked the drive had taken it. `admitted_ladder`
# compares `req` against `page2a`, but both numbers now arrive from AccuDisc, so it
# is our policy over their measurement rather than a second opinion. The
# verification did not move to a better home; it stopped happening, and nothing
# recorded the trade at the time. `_rip_disc_stage` now carries an explicit
# read-back for the one speed request that matters (§ the whole-disc read), which
# is where that check belongs.
#
# The equivalent probe, if one is ever wanted again, is
# ``Device.probe_speed_ladder(candidates=…)`` through
# :func:`accudisc_reader.speed_ladder_rows` — it takes the same candidate list and
# returns a measured rate per rung instead of an echoed request.
