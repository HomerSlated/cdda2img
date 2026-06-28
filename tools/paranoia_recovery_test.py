#!/usr/bin/env python3
"""Multi-pass cd-paranoia recovery experiment for a single track.

Repeatedly rips one track (default 8 — our known-defective Tracy Chapman track) and
checks it against the AccurateRip v1/v2 database after each pass, stopping the moment a
pass produces an AR-matching rip. The point is to find out whether a *bad* rip can be
recovered by retrying at different speeds / after physically re-seating the disc, rather
than giving up after the first failed re-rip.

It is deliberately standalone: it does NOT run the full disc pipeline. It queries the disc
TOC live (cd-paranoia -Q), fetches the AccurateRip response once, then loops the fixed
strategy below, ripping only the target track each pass and computing that track's interior
AR checksum directly.

Hard-coded 10-pass strategy (capped by --max-attempts, default 10):

    Pass  1: max speed
    Pass  2: min speed
    Pass  3: eject, reload, max speed
    Pass  4: eject, reload, min speed
    Pass  5: eject, ioctl reset (CDROMRESET), reload, max speed
    Pass  6: eject, ioctl reset (CDROMRESET), reload, min speed
    Pass  7: min speed + 1 ladder step
    Pass  8: pass-7 speed + 1 ladder step
    Pass  9: pass-8 speed + 1 ladder step
    Pass 10: pass-9 speed + 1 ladder step

min/max and the speed ladder are the drive's *actual* discrete speeds, probed at startup
by setting each candidate via CDROM_SELECT_SPEED and reading back the achieved speed
(cdrdao drive-info). Stepping the ladder one rung per pass (rather than two) traverses more
interim speeds — a typical CD ladder has only ~6 rungs (4/8/16/24/32/40X), so a 2-rung step
overshoots to the ceiling almost immediately. The sequence breaks on an AR match, or gives
up at the cap.

Output: the raw cd-paranoia output of every pass, then a summary table.

Other modes (mutually exclusive):
  --characterize  speed-vs-success-RATE: repeated, randomized, no-break trials per speed
                  (re-seating held constant) with a Wilson 95% CI, to tell a real speed
                  effect from chance — a single sequential run cannot, as it confounds
                  speed with attempt-order, re-seating, and a sample size of one.
  --retries       attempts-to-first-match at each fixed speed (how many passes does
                  recovery take at 8X vs 32X?). Stops at the first match per speed.
  --transitions   does a TARGET pass match after a PRIME pass (e.g. 40X->8X)? Tests whether
                  a high-speed prime pass helps the following pass; reports target-match vs
                  prime-match so you can see which pass is doing the work.

All modes split a "miss" into rip-failure vs read-but-no-AR-match in their output.

Usage:
  uv run python tools/paranoia_recovery_test.py [--device /dev/sr0] [--track 8]
      [--max-attempts 10] [--offset N]
  uv run python tools/paranoia_recovery_test.py --characterize [--repeat 15]
      [--speeds 4,8,16,24,32,40] [--seed 1]
  uv run python tools/paranoia_recovery_test.py --retries --speeds 8,32 --repeat 10
  uv run python tools/paranoia_recovery_test.py --transitions --pairs 40:8,40:32,40:16
      --repeat 3

CDROMRESET (passes 5-6) needs CAP_SYS_ADMIN. Build + grant the bundled helper once:
    make -C tools cdreset && doas setcap cap_sys_admin+ep tools/cdreset
(the rest of the tool then runs unprivileged; without the helper the reset is skipped).
"""

from __future__ import annotations

import argparse
import array
import fcntl
import math
import os
import random
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from cdda2img import drive_speed
from cdda2img.accuraterip import _ar_checksums, _ar_disc_ids, _fetch_ar, _parse_dbar
from cdda2img.cddb import compute_cddb_disc_id
from cdda2img.config import load_config
from cdda2img.container import wav_to_raw_pcm
from cdda2img.disc_reader import query_disc
from cdda2img.drive_info import probe_drive_name

# Linux CDROM ioctls (linux/cdrom.h)
_CDROMEJECT = 0x5309
_CDROMRESET = 0x5312  # hard-reset the drive
_CDROM_DRIVE_STATUS = 0x5326
_CDS_DISC_OK = 4  # drive status: disc present and readable

_KBPS_PER_X = 176
# /var/tmp, not /tmp: /tmp is RAM-backed tmpfs and CD audio floods it.
_WORK = Path("/var/tmp")  # noqa: S108
# Candidate Nx values to probe; the drive snaps each to a supported speed.
_SPEED_PROBE = (1, 2, 3, 4, 6, 8, 10, 12, 16, 20, 24, 32, 40, 48)


@dataclass
class PassResult:
    n: int
    label: str
    speed_x: int
    v1: str = "--------"
    v2: str = "--------"
    conf_v1: int | None = None
    conf_v2: int | None = None
    matched: bool = False
    note: str = ""


@dataclass
class PassPlan:
    label: str
    speed: str  # "max" | "min" | "ladder"
    eject: bool = False
    reset: bool = False
    ladder_idx: int = 0  # for speed == "ladder"


# ── drive control ────────────────────────────────────────────────────────────


def _ioctl(device: str, op: int, arg: int = 0) -> bool:
    """Best-effort CDROM ioctl; returns True on success."""
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        print(f"  ! open {device} failed: {exc}", file=sys.stderr)
        return False
    try:
        fcntl.ioctl(fd, op, arg)
    except OSError as exc:
        print(f"  ! ioctl 0x{op:x} failed: {exc}", file=sys.stderr)
        return False
    else:
        return True
    finally:
        os.close(fd)


def _eject(device: str) -> None:
    subprocess.run(["eject", device], check=False)  # noqa: S603, S607


def _load(device: str) -> None:
    subprocess.run(["eject", "-t", device], check=False)  # noqa: S603, S607


_CDRESET = Path(__file__).resolve().parent / "cdreset"


def _reset(device: str) -> None:
    print("  · CDROMRESET (hard-reset drive)")
    # CDROMRESET needs CAP_SYS_ADMIN. Prefer the setcap helper so the rest of the tool
    # stays unprivileged; fall back to a direct ioctl (works only as root/doas).
    if _CDRESET.exists():
        if subprocess.run([str(_CDRESET), device], check=False).returncode == 0:  # noqa: S603
            return
    elif _ioctl(device, _CDROMRESET):
        return
    print(
        "  ! CDROMRESET unavailable — build + grant the helper once:\n"
        f"      make -C tools cdreset && doas setcap cap_sys_admin+ep {_CDRESET}",
        file=sys.stderr,
    )


def _wait_ready(device: str, timeout_s: int = 40) -> bool:
    """Poll CDROM_DRIVE_STATUS until the disc is readable, or time out."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            time.sleep(1.0)
            continue
        try:
            status = fcntl.ioctl(fd, _CDROM_DRIVE_STATUS, 0)
        except OSError:
            status = -1
        finally:
            os.close(fd)
        if status == _CDS_DISC_OK:
            return True
        time.sleep(1.0)
    return False


def _reseat(device: str, *, reset: bool) -> None:
    """Eject, optionally hard-reset, reload, and wait for the disc to spin up."""
    print(f"  · eject {device}")
    _eject(device)
    time.sleep(2.0)
    if reset:
        _reset(device)
    print(f"  · reload {device}")
    _load(device)
    if not _wait_ready(device):
        print(
            "  ! drive did not report a ready disc; proceeding anyway", file=sys.stderr
        )


# ── speed ladder ─────────────────────────────────────────────────────────────


def probe_speed_ladder(device: str) -> list[int]:
    """Return the drive's actual discrete read speeds (X), ascending and de-duplicated.

    Sets each candidate Nx via CDROM_SELECT_SPEED and reads back the achieved speed
    from cdrdao drive-info — so the ladder is the drive's real snapping behaviour.
    """
    achieved: set[int] = set()
    for n in _SPEED_PROBE:
        if not drive_speed._select_speed(device, n):
            continue
        current_kbps, _ = drive_speed.read_drive_speed(device)
        if current_kbps:
            achieved.add(max(1, round(current_kbps / _KBPS_PER_X)))
    ladder = sorted(achieved)
    drive_speed.restore_drive_speed(device)  # leave it at max after probing
    return ladder


def build_plan(ladder: list[int]) -> list[PassPlan]:
    """The fixed 10-pass strategy, parameterised by the probed ladder."""
    return [
        PassPlan("max speed", "max"),
        PassPlan("min speed", "min"),
        PassPlan("eject/reload, max speed", "max", eject=True),
        PassPlan("eject/reload, min speed", "min", eject=True),
        PassPlan("eject/reset/reload, max speed", "max", eject=True, reset=True),
        PassPlan("eject/reset/reload, min speed", "min", eject=True, reset=True),
        PassPlan("min +1 ladder step", "ladder", ladder_idx=1),
        PassPlan("min +2 ladder steps", "ladder", ladder_idx=2),
        PassPlan("min +3 ladder steps", "ladder", ladder_idx=3),
        PassPlan("min +4 ladder steps", "ladder", ladder_idx=4),
    ]


def plan_speed(plan: PassPlan, ladder: list[int]) -> int:
    if plan.speed == "max":
        return ladder[-1]
    if plan.speed == "min":
        return ladder[0]
    return ladder[min(plan.ladder_idx, len(ladder) - 1)]  # clamp to top rung


# ── AR setup + per-pass verify ───────────────────────────────────────────────


@dataclass
class ARContext:
    track_idx: int  # 0-based index of the target track
    n_tracks: int
    responses: list[list[dict]] = field(default_factory=list)
    transport: str | None = None


def setup_ar(device: str, track_num: int) -> ARContext | None:
    """Query the disc TOC live and fetch the AccurateRip response once."""
    _disc_first, disc_last, tracks = query_disc(device)
    track_lsns = [first_lsn for _, first_lsn, _ in tracks]
    n = len(tracks)
    if not 1 <= track_num <= n:
        print(f"track {track_num} out of range (disc has {n} tracks)", file=sys.stderr)
        return None
    cddb_id = int(compute_cddb_disc_id(track_lsns, disc_last), 16)
    id1, id2 = _ar_disc_ids(track_lsns, disc_last)
    data, transport = _fetch_ar(n, id1, id2, cddb_id)
    if not data:
        print("disc not found in AccurateRip — cannot verify", file=sys.stderr)
        return None
    responses = _parse_dbar(
        data,
        n,
        expected_id1=int(id1, 16),
        expected_id2=int(id2, 16),
        expected_cddb_id=cddb_id,
    )
    if not responses:
        print("no usable AccurateRip blocks for this disc", file=sys.stderr)
        return None
    print(
        f"AccurateRip: {len(responses)} block(s) via {transport}; "
        f"verifying track {track_num} of {n}"
    )
    return ARContext(
        track_idx=track_num - 1, n_tracks=n, responses=responses, transport=transport
    )


def verify_track(pcm: Path, ar: ARContext) -> tuple[int, int, int | None, int | None]:
    """Compute the target track's v1/v2 and the best confidence each matched at."""
    raw = pcm.read_bytes()
    frames: array.array = array.array("I")
    frames.frombytes(raw[: len(raw) - len(raw) % 4])
    v1, v2 = _ar_checksums(frames, ar.track_idx + 1, ar.n_tracks)
    conf_v1: int | None = None
    conf_v2: int | None = None
    for resp in ar.responses:
        entry = resp[ar.track_idx]
        if entry["crc"] == v1:
            conf_v1 = max(conf_v1 or 0, entry["conf"])
        if entry["crc"] == v2:
            conf_v2 = max(conf_v2 or 0, entry["conf"])
    return v1, v2, conf_v1, conf_v2


# ── rip one pass ─────────────────────────────────────────────────────────────


def rip_track(device: str, track: int, speed_x: int, offset: int, wav: Path) -> bool:
    """Run cd-paranoia for one track at a fixed speed, raw output to the terminal."""
    cmd = [
        "cd-paranoia",
        "-d",
        device,
        "-O",
        str(offset),
        "-S",
        str(speed_x),
        "--",
        str(track),
        str(wav),
    ]
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=False).returncode == 0  # noqa: S603


def rip_and_verify(
    device: str,
    track: int,
    speed_x: int,
    offset: int,
    wav: Path,
    pcm: Path,
    ar: ARContext,
) -> tuple[str, str, int | None, int | None] | None:
    """Rip the track at *speed_x* and AR-verify it. Returns (v1, v2, conf_v1, conf_v2)
    as hex/ints, or None if the rip itself failed."""
    if not rip_track(device, track, speed_x, offset, wav):
        return None
    wav_to_raw_pcm(wav, pcm)
    v1, v2, cv1, cv2 = verify_track(pcm, ar)
    return f"{v1:08x}", f"{v2:08x}", cv1, cv2


# ── characterization mode (speed vs success rate) ────────────────────────────


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for k successes in n trials (better than the normal
    approximation at small n / extreme proportions)."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def run_characterization(
    device: str,
    track: int,
    ar: ARContext,
    speeds: list[int],
    repeat: int,
    offset: int,
    wav: Path,
    pcm: Path,
) -> int:
    """Repeated, randomized, no-break trials to measure success RATE per speed.

    Re-seating is held constant (no eject between trials) so speed is the only variable;
    the (speed, trial) order is shuffled so attempt-number / warm-up / progressive disc
    degradation don't align with any speed.
    """
    trials = [s for s in speeds for _ in range(repeat)]
    random.shuffle(trials)
    total = len(trials)
    outcomes: dict[int, list[bool]] = {s: [] for s in speeds}
    print(
        f"\ncharacterization: {repeat} trials x {len(speeds)} speeds = {total} reads, "
        f"randomized order, re-seating held constant"
    )

    for i, speed_x in enumerate(trials, start=1):
        print("\n" + "#" * 72)
        print(f"# TRIAL {i}/{total}: {speed_x}X")
        print("#" * 72)
        rv = rip_and_verify(device, track, speed_x, offset, wav, pcm, ar)
        if rv is None:
            print("  → rip failed (counted as a miss)")
            outcomes[speed_x].append(False)
            continue
        v1, v2, cv1, cv2 = rv
        ok = bool(cv1 or cv2)
        outcomes[speed_x].append(ok)
        via = "v1" if cv1 else ("v2" if cv2 else None)
        print(f"\n  → v1={v1} v2={v2}  {'MATCH via ' + via if via else 'no AR match'}")

    print_char_summary(outcomes, track)
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
    # Verdict: is the best speed's rate CI disjoint from the worst's? (suggestive, not proof)
    ranked = sorted(rows, key=lambda r: r[3], reverse=True)
    best, worst = ranked[0], ranked[-1]
    if best[0] == worst[0]:
        print("only one speed tested — no comparison.")
    elif best[4] > worst[5]:  # best CI-low > worst CI-high → non-overlapping
        print(
            f"speed effect SUPPORTED: {best[0]}X ({best[3]:.0%}) CI does not overlap "
            f"{worst[0]}X ({worst[3]:.0%})."
        )
    else:
        print(
            f"INCONCLUSIVE: {best[0]}X looks best ({best[3]:.0%}) but CIs overlap "
            f"{worst[0]}X ({worst[3]:.0%}) — more trials needed to separate them."
        )


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


def print_summary(results: list[PassResult], track: int) -> None:
    print("\n" + "=" * 72)
    print(f"SUMMARY — track {track}")
    print("=" * 72)
    print(f"{'#':>2}  {'strategy':<32}{'spd':>4}  {'v1':>8} {'v2':>8}  result")
    print("-" * 72)
    for r in results:
        if r.matched:
            via = "v1" if r.conf_v1 else "v2"
            conf = r.conf_v1 or r.conf_v2
            result = f"MATCH ({via} conf {conf})"
        elif r.note:
            result = r.note
        else:
            result = "no match"
        print(f"{r.n:>2}  {r.label:<32}{r.speed_x:>3}X  {r.v1:>8} {r.v2:>8}  {result}")
    print("-" * 72)
    win = next((r for r in results if r.matched), None)
    if win:
        print(f"RECOVERED at pass {win.n} ({win.label}, {win.speed_x}X).")
    else:
        print(f"FAILED — no AR match after {len(results)} pass(es).")


def run_sequential(
    device: str,
    track: int,
    ar: ARContext,
    ladder: list[int],
    max_attempts: int,
    offset: int,
    wav: Path,
    pcm: Path,
) -> int:
    """The fixed 10-pass recovery strategy: stop at the first AR match."""
    plan = build_plan(ladder)[:max_attempts]
    results: list[PassResult] = []
    for i, p in enumerate(plan, start=1):
        speed_x = plan_speed(p, ladder)
        print("\n" + "#" * 72)
        print(f"# PASS {i}/{len(plan)}: {p.label}  ({speed_x}X)")
        print("#" * 72)
        res = PassResult(n=i, label=p.label, speed_x=speed_x)
        if p.eject:
            _reseat(device, reset=p.reset)
        rv = rip_and_verify(device, track, speed_x, offset, wav, pcm, ar)
        if rv is None:
            res.note = "rip failed"
            results.append(res)
            continue
        res.v1, res.v2, res.conf_v1, res.conf_v2 = rv
        res.matched = bool(res.conf_v1 or res.conf_v2)
        results.append(res)
        via = "v1" if res.conf_v1 else ("v2" if res.conf_v2 else None)
        verdict = (
            f"MATCH via {via} (conf {res.conf_v1 or res.conf_v2})"
            if via
            else "no AR match"
        )
        print(f"\n  → v1={res.v1} v2={res.v2}  {verdict}")
        if res.matched:
            break
    print_summary(results, track)
    return 0 if any(r.matched for r in results) else 2


# ── retries mode (attempts-to-match at a fixed speed) ────────────────────────


def run_retries(
    device: str,
    track: int,
    ar: ARContext,
    speeds: list[int],
    max_passes: int,
    offset: int,
    wav: Path,
    pcm: Path,
) -> int:
    """At each speed, rip up to *max_passes* times, stopping at the first AR match —
    measuring how many attempts recovery takes at that speed."""
    summary: list[
        tuple[int, int | None, int, int]
    ] = []  # speed, match@, rip_fails, used
    for speed in speeds:
        match_at: int | None = None
        fails = used = 0
        for attempt in range(1, max_passes + 1):
            used = attempt
            print("\n" + "#" * 72)
            print(f"# {speed}X  attempt {attempt}/{max_passes}")
            print("#" * 72)
            rv = rip_and_verify(device, track, speed, offset, wav, pcm, ar)
            if rv is None:
                fails += 1
                print("  → rip failed")
                continue
            v1, v2, cv1, cv2 = rv
            if cv1 or cv2:
                via = "v1" if cv1 else "v2"
                print(f"\n  → v1={v1} v2={v2}  MATCH via {via} (conf {cv1 or cv2})")
                match_at = attempt
                break
            print(f"\n  → v1={v1} v2={v2}  no AR match")
        summary.append((speed, match_at, fails, used))
    print("\n" + "=" * 72)
    print(f"RETRIES — attempts to first AR match per speed (track {track})")
    print("=" * 72)
    print(f"{'speed':>6}  {'result':<30}{'rip-fails':>10}")
    print("-" * 72)
    for speed, match_at, fails, used in summary:
        result = (
            f"matched on attempt {match_at}"
            if match_at
            else f"no match in {used} attempts"
        )
        print(f"{speed:>5}X  {result:<30}{fails:>10}")
    print("-" * 72)
    return 0 if any(m for _, m, _, _ in summary) else 2


# ── transitions mode (does a target pass match after a prime pass?) ──────────


def run_transitions(
    device: str,
    track: int,
    ar: ARContext,
    pairs: list[tuple[int, int]],
    reps: int,
    offset: int,
    wav: Path,
    pcm: Path,
) -> int:
    """For each (prime, target) speed pair, repeat *reps* times: rip the prime speed, then
    the target speed, recording whether the TARGET pass matches — testing the hypothesis
    that a high-speed prime pass helps the following pass succeed. The prime-match column
    shows whether the prime pass was already succeeding on its own."""
    summary: list[
        tuple[int, int, int, int, int]
    ] = []  # prime, target, t_match, p_match, t_fail
    for prime, target in pairs:
        t_match = p_match = t_fail = 0
        for rep in range(1, reps + 1):
            print("\n" + "#" * 72)
            print(f"# {prime}X -> {target}X   rep {rep}/{reps}")
            print("#" * 72)
            print(f"  · prime pass @ {prime}X")
            rv_p = rip_and_verify(device, track, prime, offset, wav, pcm, ar)
            if rv_p and (rv_p[2] or rv_p[3]):
                p_match += 1
                print(f"    prime matched ({rv_p[0]})")
            print(f"  · target pass @ {target}X")
            rv_t = rip_and_verify(device, track, target, offset, wav, pcm, ar)
            if rv_t is None:
                t_fail += 1
                print("    target rip failed")
            elif rv_t[2] or rv_t[3]:
                t_match += 1
                print(f"    target MATCH ({rv_t[0]})")
            else:
                print(f"    target no match ({rv_t[0]})")
        summary.append((prime, target, t_match, p_match, t_fail))
    print("\n" + "=" * 72)
    print(f"TRANSITIONS — target-pass AR match after a prime pass (track {track})")
    print("=" * 72)
    print(
        f"{'pair':>12}  {'target match':>13}  {'prime match':>13}  {'tgt rip-fail':>13}"
    )
    print("-" * 72)
    for prime, target, t_match, p_match, t_fail in summary:
        print(
            f"{f'{prime}->{target}X':>12}  {f'{t_match}/{reps}':>13}  "
            f"{f'{p_match}/{reps}':>13}  {f'{t_fail}/{reps}':>13}"
        )
    print("-" * 72)
    print("target-match-after-prime is the hypothesis; compare it against prime-match")
    print("(the prime pass's own success rate) to see if the prime is what helps.")
    return 0 if any(tm for _, _, tm, _, _ in summary) else 2


def _parse_pairs(spec: str) -> list[tuple[int, int]]:
    pairs = []
    for tok in spec.split(","):
        a, b = tok.split(":")
        pairs.append((int(a), int(b)))
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--device", default="/dev/sr0")
    ap.add_argument("--track", type=int, default=8)
    ap.add_argument("--max-attempts", type=int, default=10)
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
    grp.add_argument(
        "--transitions",
        action="store_true",
        help="does a target pass match after a prime pass (use --pairs, default 40:8,40:32,40:16)",
    )
    ap.add_argument(
        "--repeat",
        type=int,
        default=None,
        help="trials per speed / max attempts / reps per pair (mode-dependent default)",
    )
    ap.add_argument(
        "--speeds",
        default=None,
        help="comma-separated X speeds (--characterize default: ladder; --retries default: 8,32)",
    )
    ap.add_argument(
        "--pairs",
        default="40:8,40:32,40:16",
        help="comma-separated prime:target X pairs for --transitions",
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=None,
        help="RNG seed for trial order (reproducibility)",
    )
    args = ap.parse_args()

    offset = resolve_offset(args.device, args.offset)
    mode = next(
        (m for m in ("characterize", "retries", "transitions") if getattr(args, m)),
        "sequential",
    )
    print(f"device={args.device} track={args.track} offset={offset:+d} mode={mode}")

    ar = setup_ar(args.device, args.track)
    if ar is None:
        return 1

    print("probing drive speed ladder…")
    ladder = probe_speed_ladder(args.device)
    if not ladder:
        print("could not probe any drive speeds; aborting", file=sys.stderr)
        return 1
    print(f"drive speed ladder: {', '.join(f'{x}X' for x in ladder)}")

    wav = _WORK / f"recovery_t{args.track}.wav"
    pcm = _WORK / f"recovery_t{args.track}.pcm"
    try:
        if args.characterize:
            if args.seed is not None:
                random.seed(args.seed)
            speeds = [int(s) for s in args.speeds.split(",")] if args.speeds else ladder
            return run_characterization(
                args.device, args.track, ar, speeds, args.repeat or 10, offset, wav, pcm
            )
        if args.retries:
            speeds = (
                [int(s) for s in args.speeds.split(",")] if args.speeds else [8, 32]
            )
            return run_retries(
                args.device, args.track, ar, speeds, args.repeat or 10, offset, wav, pcm
            )
        if args.transitions:
            pairs = _parse_pairs(args.pairs)
            return run_transitions(
                args.device, args.track, ar, pairs, args.repeat or 3, offset, wav, pcm
            )
        return run_sequential(
            args.device, args.track, ar, ladder, args.max_attempts, offset, wav, pcm
        )
    finally:
        wav.unlink(missing_ok=True)
        pcm.unlink(missing_ok=True)
        drive_speed.restore_drive_speed(args.device)  # leave the drive at full speed


if __name__ == "__main__":
    raise SystemExit(main())
