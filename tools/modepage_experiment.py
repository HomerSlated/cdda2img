#!/usr/bin/env python3
"""Mode-page-01 read-error-recovery experiment (RECOVERY.md §2.10 / §4.6).

Question: does changing the DRIVE's own error-recovery policy (mode page 0x01:
error-handling byte + internal retry count; PX-716A default err=0x00 retries=10)
help or hurt the recovery toolkit? Three measurables, per arm, on the damaged
reference track:

  1. read latency over the defect (does fast-fail shorten sweep attempts?)
  2. C2 flag volume (does giving up earlier surface more/fewer flags?)
  3. C2 HONESTY — precision/recall of flags against an AR-verified oracle
     (are the flags pointing at the actual wrong samples?)

Method: first acquire the oracle by sweep-recovering the track (AR the gate —
same mechanism as the shipped recovery). Then run interleaved reps of each arm
(interleaving decorrelates drive/disc drift from the arm): one c2read window
read of the whole track WITH C2 at a fixed speed, `--recovery E,R` applied for
non-default arms (c2read restores the saved page on every exit). Per read:
elapsed, hard sectors, C2-flagged pairs, wrong pairs vs oracle, TP/FP/FN,
AR match. The C2 bitmap is aligned to the audio with the drive's measured
k = -2 sample-pair offset before comparison.

Usage:
  uv run python tools/modepage_experiment.py [--device /dev/sr0] [--track 8]
      [--arms default,0x20:1,0x00:1] [--reps 5] [--speed 40] [--k -2]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from c2read_recovery_test import (  # type: ignore[unresolved-import]
    Disc,
    query_toc,
    resolve_offset,
    setup_ar,
    stop_spindle,
)

from cdda2img import drive_speed
from cdda2img.accuraterip import match_track_pcm

_C2READ = "c2read"
_SPS = 588
_FRAME = 2352
_WORK = Path("/var/tmp")  # noqa: S108 — /tmp is RAM-backed tmpfs


# ── one instrumented read ────────────────────────────────────────────────────


@dataclass
class ReadResult:
    arm: str
    rep: int
    elapsed: float
    hard: int
    flagged_pairs: int
    wrong_pairs: int
    tp: int
    fp: int
    fn: int
    ar_match: bool


def read_track_c2(
    device: str,
    disc: Disc,
    track: int,
    speed: int,
    offset: int,
    recovery: str | None,
    pcm: Path,
    c2: Path,
) -> tuple[np.ndarray, np.ndarray, int, float]:
    """One window read of *track* with C2. Returns (window pairs u32,
    per-raw-pair C2 flags bool, hard sectors, elapsed). Window = track sectors
    plus ceil(offset/588) tail margin (positive offsets only — this experiment
    runs on a +offset drive; extend if ever needed)."""
    s, e = disc.bounds[track - 1], disc.bounds[track]
    tail = math.ceil(offset / _SPS) if offset > 0 else 0
    hi = min(disc.leadout, e + tail)
    cmd = [
        _C2READ,
        "--device",
        device,
        "--start",
        str(s),
        "--count",
        str(hi - s),
        "--speed",
        str(speed),
        "-q",
        "--pcm",
        str(pcm),
        "--c2",
        str(c2),
    ]
    if recovery is not None:
        cmd += ["--recovery", recovery]
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
    elapsed = time.monotonic() - t0
    if proc.returncode not in (0, 3):
        msg = f"c2read failed (exit {proc.returncode}): {proc.stderr.strip()}"
        raise RuntimeError(msg)
    hard = sum(1 for ln in proc.stderr.splitlines() if ln.startswith("hard "))
    pairs = np.fromfile(pcm, dtype="<u4")
    raw_c2 = np.fromfile(c2, dtype=np.uint8)
    nsec = raw_c2.size // 294
    bits = np.unpackbits(raw_c2[: nsec * 294].reshape(nsec, 294), axis=1)
    flags = bits.reshape(nsec, _SPS, 4).any(axis=2).reshape(-1)  # per raw pair
    return pairs, flags, hard, elapsed


def corrected_slice(window: np.ndarray, tp_pairs: int, offset: int) -> np.ndarray:
    seg = window[offset : offset + tp_pairs]
    if seg.size < tp_pairs:
        msg = f"short read: {seg.size} of {tp_pairs} pairs"
        raise RuntimeError(msg)
    return seg


# ── oracle acquisition (sweep until AR matches) ──────────────────────────────


def acquire_oracle(
    device: str,
    disc: Disc,
    ar,
    ladder: list[int],
    offset: int,
    tp_pairs: int,
    pcm: Path,
    c2: Path,
    sweeps: int = 4,
) -> np.ndarray:
    for sweep in range(1, sweeps + 1):
        for speed in ladder:
            window, _f, _h, secs = read_track_c2(
                device, disc, ar.track, speed, offset, None, pcm, c2
            )
            seg = corrected_slice(window, tp_pairs, offset)
            _v1, _v2, cv1, cv2 = match_track_pcm(
                seg.tobytes(), ar.track, ar.n_tracks, ar.responses
            )
            print(
                f"  oracle sweep {sweep} @ {speed}X: {secs:5.1f}s "
                f"{'MATCH' if cv1 or cv2 else 'no match'}"
            )
            if cv1 or cv2:
                return seg.copy()
    msg = "could not acquire an AR-verified oracle within budget"
    raise RuntimeError(msg)


# ── experiment ───────────────────────────────────────────────────────────────


def parse_arms(spec: str) -> list[tuple[str, str | None]]:
    """'default,0x20:1,0x00:1' → [('default', None), ('0x20,1', '0x20,1'), …]"""
    arms: list[tuple[str, str | None]] = []
    for tok in spec.split(","):
        if tok == "default":
            arms.append(("default", None))
        else:
            err, _, retr = tok.partition(":")
            arms.append((f"{err},{retr}", f"{err},{retr}"))
    return arms


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--device", default="/dev/sr0")
    ap.add_argument("--track", type=int, default=8)
    ap.add_argument("--arms", default="default,0x20:1,0x00:1")
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--speed", type=int, default=40, help="read speed for all arms")
    # k convention here: the flag for raw audio pair j sits at bitmap pair index
    # j + k. Empirically pinned on the PX-716A by TP-argmax sweep (2026-07-05):
    # k = +2 → precision 0.993 / recall 0.990 vs an AR-verified oracle. (c2bench's
    # historical "k = -2" was the same physical lag in the opposite convention.)
    ap.add_argument("--k", type=int, default=2, help="C2/audio pair alignment")
    ap.add_argument("--offset", type=int, default=None)
    ap.add_argument("--json", type=Path, default=_WORK / "modepage_experiment.json")
    args = ap.parse_args()

    offset = resolve_offset(args.device, args.offset)
    if offset <= 0:
        print("experiment assumes a positive read offset drive", file=sys.stderr)
        return 1
    arms = parse_arms(args.arms)
    disc = query_toc(args.device)
    ar = setup_ar(disc, args.track)
    if ar is None:
        return 1
    s, e = disc.bounds[args.track - 1], disc.bounds[args.track]
    tp_pairs = (e - s) * _SPS
    ladder = sorted(set(drive_speed.probe_speed_ladder(args.device)), reverse=True)
    print(f"track {args.track}: {e - s} sectors; offset +{offset}; ladder {ladder}")

    pcm = _WORK / "modepage.pcm"
    c2 = _WORK / "modepage.c2"
    results: list[ReadResult] = []
    try:
        print("acquiring AR-verified oracle…")
        oracle = acquire_oracle(
            args.device, disc, ar, ladder, offset, tp_pairs, pcm, c2
        )

        for rep in range(1, args.reps + 1):
            for arm, recovery in arms:
                window, flags, hard, elapsed = read_track_c2(
                    args.device,
                    disc,
                    args.track,
                    args.speed,
                    offset,
                    recovery,
                    pcm,
                    c2,
                )
                seg = corrected_slice(window, tp_pairs, offset)
                # C2 flags live in the RAW pair index space: corrected pair i maps
                # to raw pair i + offset, plus the drive's k alignment.
                base = offset + args.k
                fl = flags[base : base + tp_pairs]
                if fl.size < tp_pairs:
                    fl = np.pad(fl, (0, tp_pairs - fl.size))
                wrong = seg != oracle
                tp_ = int((wrong & fl).sum())
                fp = int((~wrong & fl).sum())
                fn = int((wrong & ~fl).sum())
                _v1, _v2, cv1, cv2 = match_track_pcm(
                    seg.tobytes(), ar.track, ar.n_tracks, ar.responses
                )
                r = ReadResult(
                    arm=arm,
                    rep=rep,
                    elapsed=round(elapsed, 2),
                    hard=hard,
                    flagged_pairs=int(fl.sum()),
                    wrong_pairs=int(wrong.sum()),
                    tp=tp_,
                    fp=fp,
                    fn=fn,
                    ar_match=bool(cv1 or cv2),
                )
                results.append(r)
                print(
                    f"rep {rep} {arm:>8}: {r.elapsed:5.1f}s hard={r.hard} "
                    f"flags={r.flagged_pairs:>6} wrong={r.wrong_pairs:>6} "
                    f"TP={r.tp:>5} FP={r.fp:>4} FN={r.fn:>5} "
                    f"{'AR-MATCH' if r.ar_match else ''}"
                )
    finally:
        pcm.unlink(missing_ok=True)
        c2.unlink(missing_ok=True)
        drive_speed.restore_drive_speed(args.device)
        stop_spindle(args.device)

    print("\n" + "=" * 78)
    print(f"MODE PAGE 01 — track {args.track} @ {args.speed}X, {args.reps} reps/arm")
    print("=" * 78)
    print(
        f"{'arm':>10}  {'secs':>10} {'hard':>5} {'flags':>7} {'wrong':>7} "
        f"{'precision':>9} {'recall':>7} {'AR':>4}"
    )
    print("-" * 78)
    for arm, _rec in arms:
        rs = [r for r in results if r.arm == arm]
        secs = [r.elapsed for r in rs]
        tp_ = sum(r.tp for r in rs)
        fp = sum(r.fp for r in rs)
        fn = sum(r.fn for r in rs)
        prec = tp_ / (tp_ + fp) if tp_ + fp else float("nan")
        rec = tp_ / (tp_ + fn) if tp_ + fn else float("nan")
        n_ar = sum(1 for r in rs if r.ar_match)
        print(
            f"{arm:>10}  {min(secs):4.1f}-{max(secs):4.1f}s "
            f"{sum(r.hard for r in rs):>5} "
            f"{sum(r.flagged_pairs for r in rs) // len(rs):>7} "
            f"{sum(r.wrong_pairs for r in rs) // len(rs):>7} "
            f"{prec:>9.3f} {rec:>7.3f} {n_ar:>2}/{len(rs)}"
        )
    print("-" * 78)
    print("(flags/wrong are per-read means; precision/recall pooled over reps)")

    args.json.write_text(json.dumps([asdict(r) for r in results], indent=2))
    print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(main())
    raise SystemExit(130)
