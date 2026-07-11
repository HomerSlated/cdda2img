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
the wrong fields). ``accudisc speed-report`` reads exactly those fields (validated
kB/s-identical to cdrdao drive-info at 4X and 40X on the PX-716A) and is the primary
reader; ``cdrdao drive-info`` remains the fallback when the AccuDisc helper is absent.

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
import re
import subprocess

log = logging.getLogger(__name__)

# linux/cdrom.h: set the CD-ROM speed. The arg is an Nx multiplier (the kernel scales
# it by ~177 into a SET CD SPEED command); arg 0 is the "fastest possible" sentinel.
_CDROM_SELECT_SPEED = 0x5322
_KBPS_PER_X = 176  # 1x CD = 75 sectors/s * 2352 bytes / 1000 ≈ 176 kB/s

_MAX_READ_RE = re.compile(r"Maximum reading speed:\s*(\d+)\s*kB/s")
_CUR_READ_RE = re.compile(r"Current reading speed:\s*(\d+)\s*kB/s")
# accudisc speed-report machine line: "speed max_kbps N current_kbps M ..."
# (byte-identical to the c2read prototype's line, so the regex is unchanged.)
_ACCUDISC_SPEED_RE = re.compile(r"speed max_kbps (\d+) current_kbps (\d+)")

# Candidate Nx values to probe for the drive's real speed ladder; the drive snaps each to a
# supported speed and we read back the achieved value (so the ladder is the drive's own,
# not an assumed table).
_SPEED_PROBE = (1, 2, 3, 4, 6, 8, 10, 12, 16, 20, 24, 32, 40, 48)


def read_drive_speed(device: str) -> tuple[int | None, int | None]:
    """Return ``(current_kbps, max_kbps)``, or ``(None, None)``.

    Primary: ``accudisc speed-report`` (page 2A, cdrdao-identical fields, ~instant).
    Fallback: ``cdrdao drive-info`` when the AccuDisc helper is unavailable. Never
    raises — any failure yields ``(None, None)``.
    """
    current, maximum = _read_speed_accudisc(device)
    if maximum is not None:
        return current, maximum
    return _read_speed_cdrdao(device)


def _read_speed_accudisc(device: str) -> tuple[int | None, int | None]:
    from cdda2img.accudisc_reader import _ACCUDISC

    try:
        result = subprocess.run(  # noqa: S603  # LINT-012
            [_ACCUDISC, "--device", device, "speed-report"],  # LINT-012
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError) as exc:
        log.debug("accudisc speed-report failed for %s: %s", device, exc)
        return None, None
    m = _ACCUDISC_SPEED_RE.search(result.stdout)
    if result.returncode != 0 or m is None:
        log.debug("accudisc speed-report unusable for %s", device)
        return None, None
    return int(m.group(2)), int(m.group(1))


def _read_speed_cdrdao(device: str) -> tuple[int | None, int | None]:
    try:
        result = subprocess.run(  # noqa: S603  # LINT-012
            ["cdrdao", "drive-info", "--device", device],  # noqa: S607  # LINT-012
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError) as exc:
        log.debug("cdrdao drive-info failed for %s: %s", device, exc)
        return None, None

    if result.returncode != 0:
        log.debug("cdrdao drive-info exited %d for %s", result.returncode, device)
        return None, None

    # drive-info prints to stdout on some builds, stderr on others — scan both.
    text = result.stdout + "\n" + result.stderr
    cur_m = _CUR_READ_RE.search(text)
    max_m = _MAX_READ_RE.search(text)
    current = int(cur_m.group(1)) if cur_m else None
    maximum = int(max_m.group(1)) if max_m else None
    return current, maximum


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
