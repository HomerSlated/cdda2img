#!/usr/bin/env python3
"""c2timing.py — wall-clock benchmark: C2-guided recovery vs blind whole-track recovery.

Completes the C2 dataset (see [[project_c2_experiment]]). Both arms are driven to an
*AccurateRip-verified* target track — correctness is the gate, C2 is never trusted on
its own — and differ only in re-read granularity:

  baseline : re-read the WHOLE track across the drive's speed ladder (fast→slow),
             AR-check each read; first match wins. (Mirrors the shipped recovery.)
  c2guided : read once WITH C2; re-read only the C2-flagged span across the ladder,
             splice, AR-check; fall back to whole-track on no-flags / span-exhaustion
             (the positioning-error path that C2 cannot see).

Same-speed consensus can't fix this disc (persistent 40x miscorrections), so speed
diversity is the recovery mechanism in BOTH arms — the only variable is how much data
each re-read pulls. All reads go through c2read (raw READ CD, paranoia-free); the +30
Plextor offset is applied in-process for AR verification.

Reads are timed live on the drive; a run is minutes. Usage:
    uv run python tools/c2timing.py --track 8 --trials 3
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from ctdb_probe import (  # type: ignore[unresolved-import]
    _CDDB_ID,
    _DEVICE,
    _FRAME,
    _LEADOUT,
    _LSNS,
    _READ_OFFSET,
)

_SPS = _FRAME // 4
_N_TRACKS = len(_LSNS)
_BOUNDS = [*_LSNS, _LEADOUT]
_C2READ = "c2read"  # resolved on $PATH (symlinked into ~/.local/bin)
_SCRATCH = Path("private/testdata/c2/timing")
_MAX_INIT_TRIES = 5  # re-read the initial pass until it genuinely fails AR


# ---- reads + verification ---------------------------------------------------


def read_window(
    start_sec: int, count_sec: int, speed: int, *, with_c2: bool
) -> tuple[np.ndarray, np.ndarray | None, float]:
    """Read [start_sec, start_sec+count_sec) via c2read at *speed*. Returns
    (pairs u32, per-sector-any-C2 bool array or None, elapsed seconds)."""
    _SCRATCH.mkdir(parents=True, exist_ok=True)
    pcm = _SCRATCH / "win.pcm"
    c2 = _SCRATCH / "win.c2"
    cmd = [
        _C2READ,
        "--device",
        _DEVICE,
        "--start",
        str(start_sec),
        "--count",
        str(count_sec),
        "--speed",
        str(speed),
        "-q",
        "--pcm",
        str(pcm),
    ]
    if with_c2:
        cmd += ["--c2", str(c2)]
    t0 = time.monotonic()
    # c2read exit code is the C2 *verdict*: 0 = flags found, 3 = none found / hard-unreadable
    # regions. Both mean the read itself completed; only 1 (I/O error) or 2 (usage) are fatal.
    proc = subprocess.run(cmd, capture_output=True)  # noqa: S603 — fixed local tool
    if proc.returncode not in (0, 3):
        msg = f"c2read failed (exit {proc.returncode}): {proc.stderr.decode(errors='replace')}"
        raise RuntimeError(msg)
    elapsed = time.monotonic() - t0
    pairs = np.fromfile(pcm, dtype="<u4")
    flags = None
    if with_c2:
        raw = np.fromfile(c2, dtype=np.uint8)
        nsec = raw.size // 294  # tolerate a short (hard-unreadable) read
        raw = raw[: nsec * 294].reshape(nsec, 294)
        bits = np.unpackbits(raw, axis=1)
        flags = bits.reshape(nsec, _SPS, 4).any(axis=(1, 2))  # per sector
    return pairs, flags, elapsed


def ar_ok(window: np.ndarray, track: int, responses: list) -> bool:
    """AR-verify *track* from a window whose first sector is the track's start.

    The window is raw (drive +30); AR wants offset-corrected, so the corrected track
    is window[+30 : track_pairs+30] — which is why the window carries one extra trailing
    sector. Interior tracks are self-contained for match_track_pcm."""
    from cdda2img.accuraterip import match_track_pcm

    tp = (_BOUNDS[track] - _BOUNDS[track - 1]) * _SPS
    seg = window[_READ_OFFSET : tp + _READ_OFFSET]
    if seg.size < tp:  # short read / missing offset tail
        return False
    _v1, _v2, cv1, cv2 = match_track_pcm(seg.tobytes(), track, _N_TRACKS, responses)
    return bool(cv1 or cv2)


def flagged_span(flags: np.ndarray, track: int) -> tuple[int, int] | None:
    """Contiguous [lo, hi) sector span (absolute LBA) covering all C2-flagged sectors
    in the track window, or None if the drive flagged nothing (→ forces fallback)."""
    hits = np.nonzero(flags)[0]
    if hits.size == 0:
        return None
    base = _BOUNDS[track - 1]
    return base + int(hits.min()), base + int(hits.max()) + 1


# ---- recovery arms ----------------------------------------------------------


@dataclass
class ArmResult:
    arm: str
    init_secs: float
    init_tries: int
    recovery_secs: float
    won_at_speed: int | None
    fell_back: bool
    recovered: bool


def _initial_failing_read(
    track: int, count: int, ladder: list[int], responses: list, *, with_c2: bool
) -> tuple[np.ndarray, np.ndarray | None, float, int]:
    """Read the track window at top speed until it genuinely fails AR (so recovery is
    actually exercised). Returns (window, flags, last_read_secs, n_tries)."""
    top = ladder[0]
    start = _BOUNDS[track - 1]
    for i in range(1, _MAX_INIT_TRIES + 1):
        window, flags, secs = read_window(start, count, top, with_c2=with_c2)
        if not ar_ok(window, track, responses):
            return window, flags, secs, i
    return window, flags, secs, _MAX_INIT_TRIES  # gave up forcing a failure


def recover_baseline(
    track: int, ladder: list[int], responses: list, sweeps: int
) -> ArmResult:
    count = _BOUNDS[track] - _BOUNDS[track - 1] + 1  # +1 sector for the +30 offset tail
    start = _BOUNDS[track - 1]
    window, _f, init_secs, tries = _initial_failing_read(
        track, count, ladder, responses, with_c2=False
    )
    if ar_ok(window, track, responses):  # forcing failed → nothing to recover
        return ArmResult("baseline", init_secs, tries, 0.0, ladder[0], False, True)

    t0 = time.monotonic()
    for _ in range(
        sweeps
    ):  # each sweep is one fast→slow pass; defect is non-deterministic
        for speed in ladder:
            window, _f, _s = read_window(start, count, speed, with_c2=False)
            if ar_ok(window, track, responses):
                return ArmResult(
                    "baseline",
                    init_secs,
                    tries,
                    time.monotonic() - t0,
                    speed,
                    False,
                    True,
                )
    return ArmResult(
        "baseline", init_secs, tries, time.monotonic() - t0, None, False, False
    )


def recover_c2guided(
    track: int, ladder: list[int], responses: list, sweeps: int
) -> ArmResult:
    count = _BOUNDS[track] - _BOUNDS[track - 1] + 1
    start = _BOUNDS[track - 1]
    window, flags, init_secs, tries = _initial_failing_read(
        track, count, ladder, responses, with_c2=True
    )
    if ar_ok(window, track, responses):
        return ArmResult("c2guided", init_secs, tries, 0.0, ladder[0], False, True)

    t0 = time.monotonic()
    span = flagged_span(flags, track) if flags is not None else None

    # Targeted arm: re-read only the flagged span, splice into the window, AR-check.
    if span is not None:
        lo, hi = span
        off = (lo - start) * _SPS
        for _ in range(sweeps):
            for speed in ladder:
                patch, _f, _s = read_window(lo, hi - lo, speed, with_c2=False)
                window[off : off + patch.size] = patch
                if ar_ok(window, track, responses):
                    return ArmResult(
                        "c2guided",
                        init_secs,
                        tries,
                        time.monotonic() - t0,
                        speed,
                        False,
                        True,
                    )

    # Fallback: whole-track re-read (no flags, or span re-reads never verified).
    for _ in range(sweeps):
        for speed in ladder:
            window, _f, _s = read_window(start, count, speed, with_c2=False)
            if ar_ok(window, track, responses):
                return ArmResult(
                    "c2guided",
                    init_secs,
                    tries,
                    time.monotonic() - t0,
                    speed,
                    True,
                    True,
                )
    return ArmResult(
        "c2guided", init_secs, tries, time.monotonic() - t0, None, True, False
    )


# ---- driver -----------------------------------------------------------------


def stop_spindle() -> None:
    """Park the spindle (c2read --stop → SCSI START STOP UNIT). Deploy whenever we're
    done with the drive and no further reads are pending, so a finished run never
    leaves the platter spinning at the speed the last read set."""
    with contextlib.suppress(OSError):
        subprocess.run(  # noqa: S603 — fixed local tool
            [_C2READ, "--device", _DEVICE, "--stop", "-q"], capture_output=True
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--track", type=int, default=8, help="target track (default 8, the defect)"
    )
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument(
        "--sweeps", type=int, default=3, help="ladder passes per arm (attempt budget)"
    )
    ap.add_argument("--json", type=Path, default=_SCRATCH / "timing.json")
    args = ap.parse_args()

    from cdda2img.accuraterip import fetch_ar_responses
    from cdda2img.drive_speed import probe_speed_ladder, restore_drive_speed

    responses, transport, _b3 = fetch_ar_responses(_LSNS, _LEADOUT - 1, _CDDB_ID)
    if not responses:
        print("disc not in AccurateRip — no correctness gate; aborting")
        return 1
    ladder = sorted(set(probe_speed_ladder(_DEVICE)), reverse=True)  # fast → slow
    print(f"AR transport {transport}; ladder (fast→slow) {ladder}; track {args.track}")

    rows: list[dict] = []
    try:
        for trial in range(1, args.trials + 1):
            print(f"\n--- trial {trial}/{args.trials} ---")
            for fn in (recover_baseline, recover_c2guided):
                r = fn(args.track, ladder, responses, args.sweeps)
                rows.append({"trial": trial, **asdict(r)})
                tag = "FELL BACK" if r.fell_back else "targeted"
                status = f"won@{r.won_at_speed}X" if r.recovered else "UNRECOVERED"
                print(
                    f"  {r.arm:9s} init {r.init_secs:5.1f}s (x{r.init_tries}) "
                    f"recovery {r.recovery_secs:6.1f}s  {status:12s} [{tag}]"
                )
    finally:
        restore_drive_speed(_DEVICE)  # leave the speed setting at max for the next op…
        stop_spindle()  # …but park the spindle now — we're done reading the drive

    # Aggregate: mean recovery per arm (recovered trials only).
    print("\n=== summary (mean recovery seconds, recovered trials) ===")
    for arm in ("baseline", "c2guided"):
        rs = [x["recovery_secs"] for x in rows if x["arm"] == arm and x["recovered"]]
        fb = sum(1 for x in rows if x["arm"] == arm and x["fell_back"])
        if rs:
            print(
                f"  {arm:9s} mean {sum(rs) / len(rs):6.1f}s  n={len(rs)}  fell_back={fb}"
            )
        else:
            print(f"  {arm:9s} no recovered trials")

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(rows, indent=2))
    print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
