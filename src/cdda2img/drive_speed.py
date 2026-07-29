"""
drive_speed.py — restore the optical drive to full read speed.

cd-paranoia's ``-S`` flag (used to slow the AccurateRip-recovery re-rips) sets the
drive read speed *persistently* — it stays in effect until the next speed-set, not
just for that one command. cdrdao has no read-speed control, so a drive left slow by
a prior ``-S 1`` cripples the next cdrdao operation (e.g. fast-toc), slow enough to
blow the album-art fetch timeout. This module reads the drive's current/max read speed
and, if the drive is throttled, restores it to maximum.

Reading: there is no Linux ioctl for CD speed. The trustworthy source is MODE SENSE
page 2A read at the *correct offsets* (max = page[8:10], current = page[14:16] — the
fields cdrdao drive-info reports; the "page 2A lies" folklore is naive readers using
the wrong fields). ``accudisc speed`` reads exactly those fields (validated
kB/s-identical to cdrdao drive-info at 4X and 40X on the PX-716A) and is the only
reader — there is no cdrdao fallback (M6 of the AccuDisc migration).

This module owns the **ioctl** side of speed control. Every AccuDisc invocation
lives in :mod:`cdda2img.accudisc_reader`, which is the single seam the library
binding will replace; nothing here builds an argv.

Setting: the ``CDROM_SELECT_SPEED`` block-device ioctl (proven by cd-paranoia/cdspeedctl)
needs only device access — no root, unlike a raw SG_IO ``SET CD SPEED``.

Best-effort throughout: every failure path is swallowed so a rip is never affected.

Public interface:
    read_drive_speed(device) -> (current_kbps, max_kbps) | (None, None)
    restore_drive_speed(device) -> None
"""

from __future__ import annotations

import fcntl
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: the runtime import stays inside read_speed_rows, because
    # accudisc_reader imports nothing from here and this module is imported by
    # the rip path long before any drive is touched.
    from cdda2img.accudisc_reader import SpeedRow

log = logging.getLogger(__name__)

# linux/cdrom.h: set the CD-ROM speed. The arg is an Nx multiplier (the kernel scales
# it by ~177 into a SET CD SPEED command); arg 0 is the "fastest possible" sentinel.
_CDROM_SELECT_SPEED = 0x5322
_KBPS_PER_X = 176  # 1x CD = 75 sectors/s * 2352 bytes / 1000 ≈ 176 kB/s

#: AccuDisc's verdict tokens that :func:`admitted_ladder` keys on. The others —
#: ``duplicate:<n>`` and ``quantized:<n>`` — carry the rung they collapse onto in
#: the token itself, so they are matched by exclusion rather than by name.
#: ``unknown`` means "not judged", which is NOT the same as "rejected".
_VERDICT_ADMITTED = "admitted"
_VERDICT_UNKNOWN = "unknown"

# Candidate Nx values to probe for the drive's real speed ladder; the drive snaps each to a
# supported speed and we read back the achieved value (so the ladder is the drive's own,
# not an assumed table).
_SPEED_PROBE = (1, 2, 3, 4, 6, 8, 10, 12, 16, 20, 24, 32, 40, 48)


def read_drive_speed(device: str) -> tuple[int | None, int | None]:
    """Return ``(current_kbps, max_kbps)``, or ``(None, None)``.

    Delegates to :func:`accudisc_reader.read_speed` — this module owns the *ioctl*
    side of speed control, not the AccuDisc transport. Never raises: any failure
    yields ``(None, None)`` and callers treat that as "unknown".
    """
    from cdda2img.accudisc_reader import read_speed

    return read_speed(device)


def _select_speed(device: str, nx: int) -> bool:
    """Issue CDROM_SELECT_SPEED(nx). Returns True on success; swallows OSError."""
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        log.warning("could not open %s to restore read speed: %s", device, exc)
        return False
    try:
        fcntl.ioctl(fd, _CDROM_SELECT_SPEED, nx)
    except OSError as exc:
        log.warning("CDROM_SELECT_SPEED(%d) failed on %s: %s", nx, device, exc)
        return False
    finally:
        os.close(fd)
    return True


def restore_drive_speed(device: str) -> None:
    """If the drive is throttled below its max read speed, restore it to maximum.

    Read-then-conditional: queries current vs max via :func:`read_drive_speed` and only
    acts when they differ. Sets the exact max (``max_kbps // 176`` as an Nx multiplier),
    which avoids relying on the ``0 = max`` convention; if the read failed, falls back to
    Nx ``0`` so the drive is un-throttled regardless. Best-effort — never raises.
    """
    current, maximum = read_drive_speed(device)

    if maximum is None:
        # Couldn't read — un-throttle blind with the "fastest" sentinel.
        if _select_speed(device, 0):
            log.info("drive %s read speed reset to max (speed unknown)", device)
        return

    if current == maximum:
        log.debug("drive %s already at max read speed (%d kB/s)", device, maximum)
        return

    nx = max(1, maximum // _KBPS_PER_X)
    if _select_speed(device, nx):
        cur_x = current // _KBPS_PER_X if current else "?"
        log.info("drive %s read speed: %sX -> %dX (restored)", device, cur_x, nx)


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

    restore_drive_speed(device)  # the probe left the drive at its last rung

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


def probe_speed_ladder(device: str) -> list[int]:
    """Return the drive's actual discrete read speeds (X), ascending and de-duplicated.

    Sets each :data:`_SPEED_PROBE` candidate via ``CDROM_SELECT_SPEED`` and reads back the
    achieved speed from ``cdrdao drive-info`` — so the ladder reflects the drive's real
    snapping behaviour, not an assumed table. Restores the drive to max afterwards.
    Best-effort: a candidate that can't be set or read back is skipped.
    """
    achieved: set[int] = set()
    for n in _SPEED_PROBE:
        if not _select_speed(device, n):
            continue
        current_kbps, _ = read_drive_speed(device)
        if current_kbps:
            achieved.add(max(1, round(current_kbps / _KBPS_PER_X)))
    restore_drive_speed(device)  # leave the drive at max after probing
    return sorted(achieved)
