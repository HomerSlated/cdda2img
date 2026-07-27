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

log = logging.getLogger(__name__)

# linux/cdrom.h: set the CD-ROM speed. The arg is an Nx multiplier (the kernel scales
# it by ~177 into a SET CD SPEED command); arg 0 is the "fastest possible" sentinel.
_CDROM_SELECT_SPEED = 0x5322
_KBPS_PER_X = 176  # 1x CD = 75 sectors/s * 2352 bytes / 1000 ≈ 176 kB/s

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


def read_speed_rows(device: str) -> list[tuple[int, int, float]]:
    """``accudisc speeds`` rows as ``(req, page2a, measured)``.

    Each row is a timed streaming read at a requested rung: *req* is what we asked
    for, *page2a* what mode page 2A reported the drive settled on (0 = the page did
    not report), *measured* the achieved throughput in X.

    The probe performs real reads, so it both warms the disc — letting a
    self-throttling governor settle to its true ceiling — and leaves the drive at
    the last rung it tried. :func:`admitted_ladder` restores it afterwards.

    Delegates to :func:`accudisc_reader.speed_ladder_rows`; kept as a name here
    because :func:`admitted_ladder` is the policy that consumes it.
    """
    from cdda2img.accudisc_reader import speed_ladder_rows

    return speed_ladder_rows(device)


def admitted_ladder(device: str) -> list[int]:
    """The rungs this drive honoured *exactly*, fastest first (migration plan §9.3).

    **Strict rule** — whenever any row reports a non-zero ``page2a``, admit only rows
    where ``req == page2a``. A row where they differ means the drive quantised the
    request: reading at 8x while the row is labelled 32x is worse than not having the
    rung, because it silently mislabels every measurement taken there.

    **Fallback** — when *every* row reports ``page2a == 0``, the page did not report
    at all (which is not the same as "quantised to zero"), so the equality test would
    admit nothing. Admit on ``measured`` instead, collapsing rungs that achieved the
    same rate. AccuDisc caught this case: on a drive with no usable page 2A the
    strict rule alone yields a silently empty ladder.

    **Outcome guard** — if the ladder still resolves empty by any path, degrade to a
    single rung at the drive's reported maximum and warn. The guard is on the
    *outcome*, not on the causes, because the causes are open-ended: a drive
    reporting a real ``page2a`` that never equals ``req`` (it supports 10x/20x while
    we probe {40,32,24,16,8,4}) is non-zero, so the fallback does not fire, and the
    ladder is empty again for a completely different reason.

    **Known gap (2026-07-25)** — the strict rule does not guarantee *distinct* rungs,
    which is what the ladder actually needs. Both of its operands come from the same
    advertised ceiling, so it cross-checks the drive's quantiser and not the ceiling
    itself. With the Plextor SpeedRead uncap set, page 2A advertises the 48x **data**
    ceiling while CD-DA tops out at 40x by specification, and `req=48 page2a=48
    measured=20.99` sits alongside `req=40 page2a=40 measured=22.83` (AccuDisc's
    measurement): both admitted, one speed, and the top rung labelled a rate the drive
    never reaches on audio. Only `measured` can see this, and this branch never reads
    it -- while the `page2a == 0` branch below dedupes on `round(measured)`, so the two
    disagree about what ground truth is. Not guarded here: the uncap needs
    CAP_SYS_RAWIO, we never set it, and one n=1 table is not enough to design a
    monotonicity rule against. Migration plan 9.3 carries the full correction.

    The ladder is a property of **drive x disc**, not of the drive: a self-throttling
    governor caps a degraded disc regardless of what the drive can do. The PX-716A
    admitted [32, 24, 8, 4] on ABBA *Gold* in July and [8, 4] on the same disc once
    it had degraded further. Never cache this per drive.
    """
    rows = read_speed_rows(device)
    ladder: list[int] = []

    if any(page2a for _, page2a, _ in rows):
        ladder = sorted({p for req, p, _ in rows if req == p and p}, reverse=True)
    elif rows:
        # No page 2A anywhere: rank by achieved throughput, one rung per distinct rate.
        by_rate: dict[int, int] = {}
        for req, _, measured in rows:
            key = round(measured)
            by_rate.setdefault(key, req)
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
