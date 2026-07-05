#!/usr/bin/env python3
"""c2read multi-pass speed-ladder recovery experiment — the c2read arm of
paranoia_recovery_test.py.

Question under test: can plain c2read re-reads (raw MMC READ CD — no paranoia
engine, no overlap verification) recover a damaged track purely by sweeping the
drive's speed ladder across multiple passes, with AccurateRip as the ONLY gate?
No C2 erasures, no CTDB parity — the same conditions as the cd-paranoia
experiments, so the comparison is honest. If the answer is yes, c2read replaces
cd-paranoia in the pipeline's `_recover_failed_tracks` and cd-paranoia is retired.

Design mirrors paranoia_recovery_test.py:
  - disc geometry queried live (`c2read --toc`), AccurateRip response fetched once
  - each attempt reads ONE whole track via c2read at a fixed speed (whole-track,
    like the cd-paranoia baseline — no span targeting, no splicing)
  - the raw read is offset-corrected in-process (c2read reads stay raw; the
    window carries margin sectors, zero-padded at disc edges — the pad falls in
    AR's 2940-sample exclusion zone, same invariant as accuraterip.py)
  - a "miss" is split into read-failure vs read-but-no-AR-match; zero-filled
    hard sectors are counted per attempt

Modes (mutually exclusive):
  default         sweeps x ladder (fast->slow), stop at the first AR match — the
                  production recovery shape. Default --sweeps 4 with the PX-716A's
                  6-rung ladder = 24 attempts, the cd-paranoia baseline budget.
  --characterize  speed-vs-success-RATE: repeated, randomized, no-break trials per
                  speed with a Wilson 95% CI (head-to-head against the cd-paranoia
                  characterization data).
  --retries       attempts-to-first-match at each fixed speed.

Usage:
  uv run python tools/c2read_recovery_test.py [--device /dev/sr0] [--track 8]
      [--sweeps 4] [--offset N]
  uv run python tools/c2read_recovery_test.py --characterize [--repeat 15]
      [--speeds 4,8,16,24,32,40] [--seed 1]
  uv run python tools/c2read_recovery_test.py --retries --speeds 8,32 --repeat 10
"""

from __future__ import annotations

import argparse
import contextlib
import math
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from cdda2img import drive_speed
from cdda2img.accuraterip import fetch_ar_responses, match_track_pcm
from cdda2img.cddb import compute_cddb_disc_id
from cdda2img.config import load_config
from cdda2img.drive_info import probe_drive_name

_C2READ = "c2read"  # resolved on $PATH (symlinked into ~/.local/bin)
_SPS = 588  # stereo sample pairs per CD sector

# /var/tmp, not /tmp: /tmp is RAM-backed tmpfs and CD audio floods it.
_WORK = Path("/var/tmp")  # noqa: S108

_TOC_TRACK_RE = re.compile(r"^track (\d+) lba (\d+)")
_TOC_LEADOUT_RE = re.compile(r"^leadout lba (\d+)")


# ── disc geometry + AR setup ─────────────────────────────────────────────────


@dataclass
class Disc:
    bounds: list[int]  # track start LBAs + lead-out LBA (len = n_tracks + 1)

    @property
    def n_tracks(self) -> int:
        return len(self.bounds) - 1

    @property
    def leadout(self) -> int:
        return self.bounds[-1]


def query_toc(device: str) -> Disc:
    """Track boundaries via `c2read --toc` (READ TOC format 0, LBA)."""
    cmd = [_C2READ, "--device", device, "--toc"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)  # noqa: S603
    starts: list[int] = []
    leadout: int | None = None
    for line in proc.stdout.splitlines():
        if m := _TOC_TRACK_RE.match(line):
            starts.append(int(m.group(2)))
        elif m := _TOC_LEADOUT_RE.match(line):
            leadout = int(m.group(1))
    if not starts or leadout is None:
        msg = f"could not parse c2read --toc output:\n{proc.stdout}"
        raise RuntimeError(msg)
    return Disc(bounds=[*starts, leadout])


@dataclass
class ARContext:
    track: int  # 1-based target track
    n_tracks: int
    responses: list[list[dict]] = field(default_factory=list)
    transport: str | None = None


def setup_ar(disc: Disc, track_num: int) -> ARContext | None:
    """Fetch the AccurateRip response once for the live disc TOC."""
    if not 1 <= track_num <= disc.n_tracks:
        print(
            f"track {track_num} out of range (disc has {disc.n_tracks} tracks)",
            file=sys.stderr,
        )
        return None
    track_lsns = disc.bounds[:-1]
    disc_last = disc.leadout - 1
    cddb_id = int(compute_cddb_disc_id(track_lsns, disc_last), 16)
    responses, transport, _b3 = fetch_ar_responses(track_lsns, disc_last, cddb_id)
    if not responses:
        print("disc not found in AccurateRip — cannot verify", file=sys.stderr)
        return None
    print(
        f"AccurateRip: {len(responses)} block(s) via {transport}; "
        f"verifying track {track_num} of {disc.n_tracks}"
    )
    return ARContext(
        track=track_num,
        n_tracks=disc.n_tracks,
        responses=responses,
        transport=transport,
    )


# ── one attempt: c2read whole-track read + offset-correct + AR match ─────────


class ReadError(RuntimeError):
    pass


def read_window(
    device: str, start: int, count: int, speed: int, pcm: Path
) -> tuple[np.ndarray, int, float]:
    """Read [start, start+count) sectors via c2read at *speed*. Returns
    (pairs u32, hard-sector count, elapsed seconds). Exit 0 and 3 both mean the
    read completed (3 = c2read's no-C2-flags verdict); 1/2 are real failures."""
    cmd = [
        _C2READ,
        "--device",
        device,
        "--start",
        str(start),
        "--count",
        str(count),
        "--speed",
        str(speed),
        "-q",
        "--pcm",
        str(pcm),
    ]
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
    elapsed = time.monotonic() - t0
    if proc.returncode not in (0, 3):
        msg = f"c2read failed (exit {proc.returncode}): {proc.stderr.strip()}"
        raise ReadError(msg)
    hard = sum(1 for ln in proc.stderr.splitlines() if ln.startswith("hard "))
    return np.fromfile(pcm, dtype="<u4"), hard, elapsed


def read_track_corrected(
    device: str, disc: Disc, track: int, speed: int, offset: int, pcm: Path
) -> tuple[bytes, int, float]:
    """Read *track* whole at *speed* and return its offset-corrected PCM bytes.

    c2read reads are raw (no offset applied); corrected sample i of the track
    lives at raw absolute position track_start*588 + offset + i, so the read
    window carries ceil(|offset|/588) margin sectors on the side the offset
    points to, zero-padded where the window would cross a disc edge (the pad
    lands inside AR's first/last 2940-sample exclusion zone)."""
    s, e = disc.bounds[track - 1], disc.bounds[track]
    tp = (e - s) * _SPS
    lead = math.ceil(-offset / _SPS) if offset < 0 else 0
    tail = math.ceil(offset / _SPS) if offset > 0 else 0
    lo, hi = s - lead, e + tail
    pad_front = pad_back = 0
    if lo < 0:
        pad_front, lo = -lo * _SPS, 0
    if hi > disc.leadout:
        pad_back, hi = (hi - disc.leadout) * _SPS, disc.leadout
    pairs, hard, elapsed = read_window(device, lo, hi - lo, speed, pcm)
    if pad_front or pad_back:
        pairs = np.concatenate([
            np.zeros(pad_front, dtype="<u4"),
            pairs,
            np.zeros(pad_back, dtype="<u4"),
        ])
    base = lead * _SPS + offset  # index of the track's first corrected sample
    seg = pairs[base : base + tp]
    if seg.size < tp:
        msg = f"short read: got {seg.size} of {tp} sample pairs"
        raise ReadError(msg)
    return seg.tobytes(), hard, elapsed


@dataclass
class Attempt:
    n: int
    sweep: int
    speed_x: int
    v1: str = "--------"
    v2: str = "--------"
    conf_v1: int | None = None
    conf_v2: int | None = None
    hard: int = 0
    secs: float = 0.0
    matched: bool = False
    note: str = ""


def attempt_read(
    device: str, disc: Disc, ar: ARContext, speed: int, offset: int, pcm: Path
) -> Attempt:
    """One whole-track read + AR verify at *speed*; never raises."""
    a = Attempt(n=0, sweep=0, speed_x=speed)
    try:
        raw, a.hard, a.secs = read_track_corrected(
            device, disc, ar.track, speed, offset, pcm
        )
    except ReadError as exc:
        a.note = f"read failed: {exc}"
        return a
    a.v1, a.v2, a.conf_v1, a.conf_v2 = match_track_pcm(
        raw, ar.track, ar.n_tracks, ar.responses
    )
    a.matched = bool(a.conf_v1 or a.conf_v2)
    return a


def describe(a: Attempt) -> str:
    if a.note:
        return a.note
    if a.matched:
        via = "v1" if a.conf_v1 else "v2"
        return f"MATCH via {via} (conf {a.conf_v1 or a.conf_v2})"
    return f"no AR match ({a.hard} hard)" if a.hard else "no AR match"


# ── sequential mode (production shape: sweeps x ladder, stop at match) ───────


def run_sequential(
    device: str,
    disc: Disc,
    ar: ARContext,
    ladder: list[int],
    sweeps: int,
    offset: int,
    pcm: Path,
) -> int:
    total = sweeps * len(ladder)
    results: list[Attempt] = []
    n = 0
    for sweep in range(1, sweeps + 1):
        for speed in ladder:
            n += 1
            print("\n" + "#" * 72)
            print(f"# ATTEMPT {n}/{total}: sweep {sweep}, {speed}X")
            print("#" * 72)
            a = attempt_read(device, disc, ar, speed, offset, pcm)
            a.n, a.sweep = n, sweep
            results.append(a)
            print(
                f"  → {a.secs:5.1f}s  v1={a.v1} v2={a.v2}  {describe(a)}",
            )
            if a.matched:
                print_summary(results, ar.track, total)
                return 0
    print_summary(results, ar.track, total)
    return 2


def print_summary(results: list[Attempt], track: int, budget: int) -> None:
    print("\n" + "=" * 72)
    print(f"SUMMARY — track {track} (c2read whole-track, AR-only gate)")
    print("=" * 72)
    print(
        f"{'#':>3} {'swp':>4} {'spd':>4}  {'v1':>8} {'v2':>8} {'hard':>5} "
        f"{'secs':>6}  result"
    )
    print("-" * 72)
    for a in results:
        print(
            f"{a.n:>3} {a.sweep:>4} {a.speed_x:>3}X  {a.v1:>8} {a.v2:>8} "
            f"{a.hard:>5} {a.secs:>6.1f}  {describe(a)}"
        )
    print("-" * 72)
    win = next((a for a in results if a.matched), None)
    spent = sum(a.secs for a in results)
    if win:
        print(
            f"RECOVERED at attempt {win.n}/{budget} (sweep {win.sweep}, "
            f"{win.speed_x}X) — {spent:.1f}s of reads."
        )
    else:
        print(
            f"FAILED — no AR match in {len(results)}/{budget} attempts "
            f"({spent:.1f}s of reads)."
        )


# ── characterize mode (speed vs success rate, randomized) ────────────────────


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for k successes in n trials."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def run_characterization(
    device: str,
    disc: Disc,
    ar: ARContext,
    speeds: list[int],
    repeat: int,
    offset: int,
    pcm: Path,
) -> int:
    """Repeated, randomized, no-break trials — success RATE per speed. Order is
    shuffled so warm-up / progressive degradation don't align with any speed."""
    trials = [s for s in speeds for _ in range(repeat)]
    random.shuffle(trials)
    total = len(trials)
    outcomes: dict[int, list[bool]] = {s: [] for s in speeds}
    print(
        f"\ncharacterization: {repeat} trials x {len(speeds)} speeds = {total} "
        f"reads, randomized order"
    )
    for i, speed in enumerate(trials, start=1):
        print(f"\n# TRIAL {i}/{total}: {speed}X")
        a = attempt_read(device, disc, ar, speed, offset, pcm)
        outcomes[speed].append(a.matched)
        print(f"  → {a.secs:5.1f}s  v1={a.v1} v2={a.v2}  {describe(a)}")
    print_char_summary(outcomes, ar.track)
    return 0


def print_char_summary(outcomes: dict[int, list[bool]], track: int) -> None:
    print("\n" + "=" * 72)
    print(f"CHARACTERIZATION — track {track} (success = AR v1/v2 match)")
    print("=" * 72)
    print(f"{'speed':>6}  {'k/n':>7}  {'rate':>6}  {'95% CI (Wilson)':>18}")
    print("-" * 72)
    rows = []
    for speed in sorted(outcomes):
        oks = outcomes[speed]
        k, n = sum(oks), len(oks)
        lo, hi = wilson_ci(k, n)
        rows.append((speed, k, n, k / n if n else 0.0, lo, hi))
        print(
            f"{speed:>5}X  {f'{k}/{n}':>7}  {k / n if n else 0:>5.0%}  "
            f"[{lo:>4.0%}, {hi:>4.0%}]"
        )
    print("-" * 72)
    ranked = sorted(rows, key=lambda r: r[3], reverse=True)
    best, worst = ranked[0], ranked[-1]
    if best[0] == worst[0]:
        print("only one speed tested — no comparison.")
    elif best[4] > worst[5]:
        print(
            f"speed effect SUPPORTED: {best[0]}X ({best[3]:.0%}) CI does not "
            f"overlap {worst[0]}X ({worst[3]:.0%})."
        )
    else:
        print(
            f"INCONCLUSIVE: {best[0]}X looks best ({best[3]:.0%}) but CIs overlap "
            f"{worst[0]}X ({worst[3]:.0%}) — more trials needed."
        )


# ── retries mode (attempts-to-first-match per fixed speed) ───────────────────


def run_retries(
    device: str,
    disc: Disc,
    ar: ARContext,
    speeds: list[int],
    max_passes: int,
    offset: int,
    pcm: Path,
) -> int:
    summary: list[tuple[int, int | None, int, int]] = []
    for speed in speeds:
        match_at: int | None = None
        fails = used = 0
        for attempt in range(1, max_passes + 1):
            used = attempt
            print(f"\n# {speed}X  attempt {attempt}/{max_passes}")
            a = attempt_read(device, disc, ar, speed, offset, pcm)
            print(f"  → {a.secs:5.1f}s  v1={a.v1} v2={a.v2}  {describe(a)}")
            if a.note:
                fails += 1
                continue
            if a.matched:
                match_at = attempt
                break
        summary.append((speed, match_at, fails, used))
    print("\n" + "=" * 72)
    print(f"RETRIES — attempts to first AR match per speed (track {ar.track})")
    print("=" * 72)
    print(f"{'speed':>6}  {'result':<30}{'read-fails':>11}")
    print("-" * 72)
    for speed, match_at, fails, used in summary:
        result = (
            f"matched on attempt {match_at}"
            if match_at
            else f"no match in {used} attempts"
        )
        print(f"{speed:>5}X  {result:<30}{fails:>11}")
    print("-" * 72)
    return 0 if any(m for _, m, _, _ in summary) else 2


# ── main ─────────────────────────────────────────────────────────────────────


def resolve_offset(device: str, override: int | None) -> int:
    if override is not None:
        return override
    cfg = load_config()
    name = probe_drive_name(device)
    for d in getattr(cfg, "drives", []) or []:
        if d.name == name:
            return d.read_offset
    print(f"  ! no configured read offset for {name!r}; using 0", file=sys.stderr)
    return 0


def stop_spindle(device: str) -> None:
    """Park the spindle (c2read --stop → START STOP UNIT) — never leave the
    platter spinning at whatever speed the last read set."""
    with contextlib.suppress(OSError):
        cmd = [_C2READ, "--device", device, "--stop", "-q"]
        subprocess.run(cmd, capture_output=True)  # noqa: S603


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--device", default="/dev/sr0")
    ap.add_argument("--track", type=int, default=8)
    ap.add_argument(
        "--sweeps",
        type=int,
        default=4,
        help="ladder passes in sequential mode (default 4 → 24 attempts on a "
        "6-rung ladder, the cd-paranoia baseline budget)",
    )
    ap.add_argument(
        "--offset", type=int, default=None, help="read offset (default: config)"
    )
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument(
        "--characterize",
        action="store_true",
        help="speed-vs-success-rate mode: repeated, randomized, no-break trials",
    )
    grp.add_argument(
        "--retries",
        action="store_true",
        help="attempts-to-first-match per fixed speed (use --speeds, default 8,32)",
    )
    ap.add_argument(
        "--repeat",
        type=int,
        default=None,
        help="trials per speed / max attempts (mode-dependent default 10)",
    )
    ap.add_argument(
        "--speeds",
        default=None,
        help="comma-separated X speeds (--characterize default: ladder; "
        "--retries default: 8,32)",
    )
    ap.add_argument("--seed", type=int, default=None, help="RNG seed for trial order")
    args = ap.parse_args()

    offset = resolve_offset(args.device, args.offset)
    mode = (
        "characterize"
        if args.characterize
        else ("retries" if args.retries else "sequential")
    )
    print(f"device={args.device} track={args.track} offset={offset:+d} mode={mode}")

    disc = query_toc(args.device)
    print(f"TOC: {disc.n_tracks} tracks, lead-out LBA {disc.leadout}")
    ar = setup_ar(disc, args.track)
    if ar is None:
        return 1

    print("probing drive speed ladder…")
    ladder = sorted(set(drive_speed.probe_speed_ladder(args.device)), reverse=True)
    if not ladder:
        print("could not probe any drive speeds; aborting", file=sys.stderr)
        return 1
    print(f"drive speed ladder (fast→slow): {', '.join(f'{x}X' for x in ladder)}")

    pcm = _WORK / f"c2recovery_t{args.track}.pcm"
    try:
        if args.characterize:
            if args.seed is not None:
                random.seed(args.seed)
            speeds = [int(s) for s in args.speeds.split(",")] if args.speeds else ladder
            return run_characterization(
                args.device, disc, ar, speeds, args.repeat or 10, offset, pcm
            )
        if args.retries:
            speeds = (
                [int(s) for s in args.speeds.split(",")] if args.speeds else [8, 32]
            )
            return run_retries(
                args.device, disc, ar, speeds, args.repeat or 10, offset, pcm
            )
        return run_sequential(args.device, disc, ar, ladder, args.sweeps, offset, pcm)
    finally:
        pcm.unlink(missing_ok=True)
        drive_speed.restore_drive_speed(args.device)  # speed setting back to max…
        stop_spindle(args.device)  # …and park the spindle — we're done reading


if __name__ == "__main__":
    raise SystemExit(main())
