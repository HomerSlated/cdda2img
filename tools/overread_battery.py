#!/usr/bin/env python3
"""Overread battery: does a read *run-up* recover a C2-flicker sector, and how long?

Disc 2 of run3 (Tracy Chapman) established the puzzle this tool exists to answer.
A full sequential pass reads the disc's intermittent defect sectors correctly ~27%
of the time; a bare single-sector re-read (``count=1``) reached an AccurateRip match
0/25 times. The single variable that differs is the **run-up** — how many sectors the
head streams over before it reaches the target. Servo PLL lock, C1 error-correction
history and the drive's read-ahead pipeline all warm up over a run-up, and a cold seek
to one sector has none of it.

This sweeps run-up length ``K`` (sectors read *before* the target, then discarded) x
read speed x target sector, and measures, per cell, the fraction of attempts whose
target sector comes back **byte-exact** against a known-good pass. Byte-exact — not
C2-clear (which can be a silent mis-correction) and not whole-track AR (which
conflates many sectors) — is the sharp instrument: it answers exactly "did THIS
sector read correctly".

Two correctness controls:
  * **Ground truth** is the raw bytes of each target sector taken from a whole-disc
    capture whose *enclosing track AR-verified*. AR-pass ⟹ every sample is
    reference-correct, so the raw sector is correct.
  * **Cache defeat** between every attempt: a large block far from the target is read
    to evict the drive's read-ahead/segment cache, so a sector read correctly on one
    attempt cannot be served from cache on the next and fake a success.

Not wired into the pipeline. Diagnostic tool; results inform whether the production
recovery read (``_recover_failed_tracks``, currently a whole-track re-read) should
instead overread the flagged sectors with a measured run-up.

Usage (from project root):
    TMPDIR=/var/tmp uv run python tools/overread_battery.py \
        --device /dev/sr0 --read-offset 30 \
        --speeds 40,32,24,8,4 --leadins 0,4,16,64,256 \
        --sectors 15268,113044,112613 --reps 5 \
        --out private/bench/runs/run3/tracy_chapman/overread.toml
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

_SECTOR_BYTES = 2352  # one CD-DA sector = 588 frames x 4 bytes

# Seed targets only: the 12 distinct C2-flagged LBAs observed across Tracy Chapman's
# run3 matrix (tracks 2, 7, 8, 9).
#
# DO NOT sweep these blind. This disc's C2 flicker moves between passes: the zone
# (~114000-114500) is stable but the individual flagged LBA is not. On 2026-07-23 a
# fresh capture flagged exactly ONE sector, 114436 -- and this list has 114437, i.e.
# ZERO overlap. Sweeping the stale set produced an all-1.00 null result because none
# of its sectors was mis-reading at the time.
#
# Always take a fresh whole-disc C2 capture first and pass the live flagged set via
# --sectors. These stay as a ground-truth seed (banking extra sectors is free -- a
# whole-disc GT pass costs the same however many are tracked) and as the zone map.
_TRACY_FLAGGED = [
    15268,
    110657,
    112349,
    112613,
    112766,
    112877,
    113044,
    113058,
    114018,
    114186,
    114437,
    129496,
]


@dataclass
class Cell:
    speed: int
    lba: int
    leadin: int  # requested K
    actual_leadin: int  # K after clamping to LBA >= 0
    successes: int = 0
    reps: int = 0
    read_seconds: float = 0.0  # target reads only; excludes the cache-flush read

    @property
    def rate(self) -> float:
        return self.successes / self.reps if self.reps else 0.0

    @property
    def mean_read_s(self) -> float:
        return self.read_seconds / self.reps if self.reps else 0.0

    @property
    def expected_s_to_success(self) -> float | None:
        """Expected wall seconds to the FIRST correct read of this sector, retrying
        this (speed, K) until it lands: mean attempt cost / success rate.

        This is the quantity to minimise -- not the success rate. A large run-up can
        win on rate and still lose on time, and a cheap 0.2-rate cell can beat an
        expensive 0.6-rate one. None when the cell never succeeded (unbounded)."""
        return self.mean_read_s / self.rate if self.successes else None


@dataclass
class GroundTruth:
    """Byte-exact target-sector references, and which tracks earned them."""

    sector_bytes: dict[int, bytes] = field(default_factory=dict)
    verified_tracks: set[int] = field(default_factory=set)


# --- geometry helpers ----------------------------------------------------------


def _track_of(lba: int, track_lsns: list[int], leadout: int) -> int:
    """1-based track number owning ``lba`` (audio program area)."""
    for i in range(len(track_lsns)):
        start = track_lsns[i]
        end = track_lsns[i + 1] if i + 1 < len(track_lsns) else leadout
        if start <= lba < end:
            return i + 1
    msg = f"lba {lba} is outside the program area [0, {leadout})"
    raise ValueError(msg)


def _flush_target(lba: int, leadout: int, flush_sectors: int) -> int:
    """A start LBA far from ``lba`` for a cache-evicting throwaway read — always a
    long seek to the opposite half of the disc, clamped inside the program area."""
    far = 500 if lba > leadout // 2 else leadout - flush_sectors - 500
    return max(0, min(far, leadout - flush_sectors - 1))


# --- ground truth --------------------------------------------------------------


def _verify_track(
    pcm: bytes,
    track: int,
    track_lsns: list[int],
    leadout: int,
    read_offset: int,
    responses: list[list[dict]],
) -> bool:
    """AR-verify one track's offset-corrected slice out of a whole-disc raw capture.

    Mirrors ``accuraterip.verify_rip``'s window arithmetic, zero-pad included. The
    pad is not cosmetic: at a positive read offset the last track's window runs past
    the end of the capture, and at track 1 the window starts before its head. A bare
    slice silently returns a SHORT buffer, which moves ``_ar_checksums``' exclusion
    boundary (``sum_to = n - 2939``) and shifts every sample's multiplier -- so the
    checksum misses and the track reads as unverified even on a perfect capture. The
    padded zeros themselves land inside the +/-2940-frame exclusion zone and so never
    reach the sum.
    """
    from cdda2img.accuraterip import match_track_pcm

    idx = track - 1
    byte_start = track_lsns[idx] * _SECTOR_BYTES + read_offset * 4
    end_lsn = track_lsns[idx + 1] if idx + 1 < len(track_lsns) else leadout
    byte_end = end_lsn * _SECTOR_BYTES + read_offset * 4

    corrected = pcm[max(0, byte_start) : min(len(pcm), byte_end)]
    if byte_start < 0:
        corrected = bytes(-byte_start) + corrected
    if byte_end > len(pcm):
        corrected = corrected + bytes(byte_end - len(pcm))

    _v1, _v2, c1, c2 = match_track_pcm(corrected, track, len(track_lsns), responses)
    return c1 is not None or c2 is not None


def discover_flagged(device: str, speed: int, tmp_dir: Path) -> list[int]:
    """One whole-disc C2 capture → the LBAs currently flagged by the drive.

    The reason this exists: pre-selecting target LBAs does not work on a disc whose
    C2 flicker migrates. On 2026-07-23 the sectors flagged an hour apart had ZERO
    overlap, and a battery swept over the stale list scored 1.00 everywhere simply
    because none of those sectors was mis-reading any more. Discovery has to happen
    in the same session as the sweep -- ideally immediately before it.
    """
    from cdda2img.accudisc_reader import read_disc_c2

    pcm_p, c2_p = tmp_dir / ".discover.pcm", tmp_dir / ".discover.c2"
    print(f"# discovery pass: whole-disc C2 capture @ {speed}x", flush=True)
    try:
        read_disc_c2(device, pcm_p, c2_p, read_speed=speed)
        c2 = c2_p.read_bytes()
    finally:
        pcm_p.unlink(missing_ok=True)

    stride = 294  # C2 bits per sector: 2352 samples / 8
    flagged = [
        i for i in range(len(c2) // stride) if any(c2[i * stride : (i + 1) * stride])
    ]
    c2_p.unlink(missing_ok=True)
    print(
        f"# discovery: {len(flagged)} C2-flagged sector(s) {flagged[:40]}", flush=True
    )
    return flagged


def build_ground_truth(
    device: str,
    track_lsns: list[int],
    leadout: int,
    read_offset: int,
    responses: list[list[dict]],
    targets: list[int],
    speed: int,
    passes: int,
    tmp: Path,
    min_agree: int = 3,
) -> GroundTruth:
    """Establish each target sector's true bytes over N whole-disc passes, by the
    strongest evidence available, in this order:

    1. **AR-certified** — a pass whose *enclosing track* matches an AccurateRip
       block is byte-correct across that whole track (tens of thousands of
       independent rips agree), so the target sector's value in that pass is
       ground truth, not a vote. One such pass settles the sector outright.
    2. **Consensus** — no AR (disc not in the database, or every pass failed the
       track): fall back to the modal value, banked only at ``>= min_agree``
       passes so a lone deterministic mis-read cannot win.

    Rung 1 is what makes the experiment sound: a run-up that "recovers" a sector
    is only meaningful against a value something external vouches for. The
    fallback keeps the rig usable on discs AccurateRip has never seen.

    Each pass is scored and released immediately — retaining every pass would
    hold ``passes`` x ~383 MB of PCM resident for a check that is per-pass."""
    from collections import Counter

    from cdda2img.accudisc_reader import read_span

    want = {lba: _track_of(lba, track_lsns, leadout) for lba in targets}
    tracks = sorted(set(want.values()))
    observed: dict[int, Counter[bytes]] = {lba: Counter() for lba in targets}
    certified: dict[int, bytes] = {}
    gt = GroundTruth()

    for attempt in range(1, passes + 1):
        print(f"# ground truth pass {attempt}/{passes}", flush=True)
        read_span(device, 0, leadout, tmp, read_speed=speed)
        pcm = tmp.read_bytes()

        ar_ok = {
            t
            for t in tracks
            if responses
            and _verify_track(pcm, t, track_lsns, leadout, read_offset, responses)
        }
        if ar_ok:
            gt.verified_tracks |= ar_ok
            print(f"#   AR-verified this pass: tracks {sorted(ar_ok)}")

        for lba in targets:
            sec = pcm[lba * _SECTOR_BYTES : (lba + 1) * _SECTOR_BYTES]
            observed[lba][sec] += 1
            if want[lba] in ar_ok and lba not in certified:
                certified[lba] = sec
        del pcm  # ~383 MB per pass — do not accumulate

    for lba in targets:
        value, count = observed[lba].most_common(1)[0]
        distinct = len(observed[lba])
        if lba in certified:
            gt.sector_bytes[lba] = certified[lba]
            agrees = observed[lba][certified[lba]]
            note = "" if certified[lba] == value else " (DISAGREES with the mode)"
            print(
                f"#   lba {lba}: AR-certified (track {want[lba]}), "
                f"{agrees}/{passes} passes agree, {distinct} distinct{note}"
            )
        elif count >= min_agree:
            gt.sector_bytes[lba] = value
            print(
                f"#   lba {lba}: consensus {count}/{passes} "
                f"({distinct} distinct value(s) seen) — NOT AR-certified"
            )
        else:
            print(
                f"# WARNING: lba {lba} no {min_agree}/{passes} consensus "
                f"(top {count}/{passes}, {distinct} distinct) — dropped",
                file=sys.stderr,
            )

    return gt


# --- the battery ---------------------------------------------------------------


def _read_target_sector(
    device: str, lba: int, leadin: int, tail: int, speed: int, tmp: Path
) -> bytes:
    """Overread: read ``[lba-K, lba+tail]`` and return the target sector's raw bytes.
    K is clamped so the start stays >= 0; the returned actual K is ``lba - start``."""
    from cdda2img.accudisc_reader import read_span

    start = max(0, lba - leadin)
    actual_k = lba - start
    count = actual_k + 1 + tail
    read_span(device, start, count, tmp, read_speed=speed)
    buf = tmp.read_bytes()
    off = actual_k * _SECTOR_BYTES
    return buf[off : off + _SECTOR_BYTES]


def _flush_cache(
    device: str, lba: int, leadout: int, flush_sectors: int, speed: int, tmp: Path
) -> None:
    from cdda2img.accudisc_reader import read_span

    target = _flush_target(lba, leadout, flush_sectors)
    read_span(device, target, flush_sectors, tmp, read_speed=speed)


def run_battery(
    device: str,
    gt: GroundTruth,
    leadout: int,
    speeds: list[int],
    leadins: list[int],
    tail: int,
    reps: int,
    flush_sectors: int,
    tmp: Path,
    flush_tmp: Path,
    seed: int = 0,
) -> list[Cell]:
    """Sweep every (speed, sector, run-up) cell, ``reps`` times each.

    **Cells are sampled in ROUNDS with a shuffled order, not swept sequentially.**
    This disc's C2 flicker drifts on a minutes timescale, so a nested
    ``for speed: for K:`` loop would run each speed rung in one contiguous slot and
    confound the speed effect with elapsed time -- the first rung and the last rung
    would be measuring the disc in different states. Interleaving spreads every
    cell's reps across the whole run window, so drift becomes shared noise across
    cells instead of a systematic per-speed bias. ``seed`` keeps it reproducible.
    """
    import random
    import time

    targets = sorted(gt.sector_bytes)
    cells: dict[tuple[int, int, int], Cell] = {
        (speed, lba, k): Cell(speed, lba, k, lba - max(0, lba - k))
        for speed in speeds
        for lba in targets
        for k in leadins
    }
    order = list(cells)
    rng = random.Random(seed)  # noqa: S311 — experiment ordering, not security

    for rnd in range(1, reps + 1):
        rng.shuffle(order)
        t_round = time.monotonic()
        for speed, lba, k in order:
            cell = cells[(speed, lba, k)]
            _flush_cache(device, lba, leadout, flush_sectors, speed, flush_tmp)
            t0 = time.monotonic()
            got = _read_target_sector(device, lba, k, tail, speed, tmp)
            cell.read_seconds += time.monotonic() - t0
            cell.reps += 1
            if got == gt.sector_bytes[lba]:
                cell.successes += 1
        hits = sum(c.successes for c in cells.values())
        att = sum(c.reps for c in cells.values())
        print(
            f"[round {rnd}/{reps}] {len(order)} cells in "
            f"{time.monotonic() - t_round:.0f}s — cumulative {hits}/{att} "
            f"({hits / att:.2f})",
            flush=True,
        )

    return list(cells.values())


# --- reporting -----------------------------------------------------------------


def _pool(cells: list[Cell]) -> dict[tuple[int, int], list[float]]:
    """Pool cells by (speed, K) across sectors → [successes, reps, read_seconds]."""
    agg: dict[tuple[int, int], list[float]] = {}
    for c in cells:
        acc = agg.setdefault((c.speed, c.leadin), [0.0, 0.0, 0.0])
        acc[0] += c.successes
        acc[1] += c.reps
        acc[2] += c.read_seconds
    return agg


def _rate_table(
    agg: dict[tuple[int, int], list[float]], speeds: list[int], leadins: list[int]
) -> list[str]:
    lines = ["", "=== success rate by speed x run-up (all target sectors pooled) ==="]
    lines.append("speed\\K " + "".join(f"{k:>8}" for k in leadins))
    for speed in speeds:
        row = f"{speed:>5}x "
        for k in leadins:
            hits, reps, _ = agg.get((speed, k), [0.0, 0.0, 0.0])
            row += f"{(hits / reps if reps else 0):>8.2f}"
        lines.append(row)
    lines.append("(cell = fraction of attempts byte-exact vs a known-good pass)")
    return lines


def _cost_table(
    agg: dict[tuple[int, int], list[float]], speeds: list[int], leadins: list[int]
) -> list[str]:
    """Expected wall seconds to the first correct read, per (speed, K).

    The rate table alone does not identify the best strategy: a bigger run-up costs
    more per attempt, so it can win on rate and still lose on wall time. This is the
    quantity to minimise. Cells that never succeeded are unbounded and print "--".
    """
    if not any(acc[2] for acc in agg.values()):
        return []
    lines = [
        "",
        "=== expected seconds to first correct read (lower is better) ===",
        "speed\\K " + "".join(f"{k:>8}" for k in leadins),
    ]
    best: tuple[float, int, int] | None = None
    for speed in speeds:
        row = f"{speed:>5}x "
        for k in leadins:
            hits, reps, secs = agg.get((speed, k), [0.0, 0.0, 0.0])
            if not hits or not reps:
                row += f"{'--':>8}"
                continue
            exp = secs / hits  # (secs/reps) / (hits/reps)
            row += f"{exp:>8.2f}"
            if best is None or exp < best[0]:
                best = (exp, speed, k)
        lines.append(row)
    if best:
        lines.append(
            f"fastest cell: {best[1]}x with K={best[2]} "
            f"({best[0]:.2f}s expected per recovered sector)"
        )
    lines.append("(mean attempt seconds / success rate; excludes cache-flush read)")
    return lines


def summarise(cells: list[Cell], speeds: list[int], leadins: list[int]) -> str:
    """Headline speed x run-up interaction: success rate, then expected time cost."""
    agg = _pool(cells)
    return "\n".join(
        _rate_table(agg, speeds, leadins) + _cost_table(agg, speeds, leadins)
    )


def write_toml(path: Path, cells: list[Cell], gt: GroundTruth, meta: dict) -> None:
    lines = ["# overread battery results", ""]
    for k, v in meta.items():
        lines.append(f"{k} = {v!r}")
    lines.append(f"verified_tracks = {sorted(gt.verified_tracks)}")
    lines.append(f"target_sectors = {sorted(gt.sector_bytes)}")
    lines.append("")
    for c in cells:
        lines.append("[[cell]]")
        lines.append(f"speed = {c.speed}")
        lines.append(f"lba = {c.lba}")
        lines.append(f"leadin = {c.leadin}")
        lines.append(f"actual_leadin = {c.actual_leadin}")
        lines.append(f"successes = {c.successes}")
        lines.append(f"reps = {c.reps}")
        lines.append(f"rate = {c.rate:.4f}")
        lines.append(f"read_seconds = {c.read_seconds:.4f}")
        lines.append(f"mean_read_s = {c.mean_read_s:.4f}")
        exp = c.expected_s_to_success
        lines.append(f"expected_s_to_success = {exp:.4f}" if exp else "# never matched")
        lines.append("")
    path.write_text("\n".join(lines))


def save_gt_cache(path: Path, gt: GroundTruth) -> None:
    import json

    path.write_text(
        json.dumps({
            "sectors": {str(k): v.hex() for k, v in gt.sector_bytes.items()},
            "verified_tracks": sorted(gt.verified_tracks),
        })
    )


def load_gt_cache(path: Path, targets: list[int]) -> GroundTruth | None:
    """Return a cached GroundTruth iff it exists and covers every requested target."""
    import json

    if not path.exists():
        return None
    data = json.loads(path.read_text())
    sectors = {int(k): bytes.fromhex(v) for k, v in data["sectors"].items()}
    if not all(lba in sectors for lba in targets):
        return None
    return GroundTruth(
        sector_bytes={lba: sectors[lba] for lba in targets},
        verified_tracks=set(data.get("verified_tracks", [])),
    )


# --- CLI -----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--device", default="/dev/sr0")
    ap.add_argument("--read-offset", type=int, default=30)
    ap.add_argument("--speeds", default="40,32,24,8,4")
    ap.add_argument("--leadins", default="0,4,16,64,256", help="run-up K values")
    ap.add_argument("--tail", type=int, default=2, help="sectors read after the target")
    ap.add_argument(
        "--sectors",
        default="",
        help="LBAs the battery sweeps (default: all ground-truth sectors)",
    )
    ap.add_argument(
        "--gt-sectors",
        default="",
        help="LBAs to establish ground truth for (default: the 12 Tracy flagged "
        "sectors). Always a superset of --sectors; caches independently of the sweep.",
    )
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument(
        "--discover",
        action="store_true",
        help="take one whole-disc C2 capture first and sweep the LIVE flagged "
        "sectors (overrides --sectors). Strongly preferred: this disc's flagged "
        "set migrates between passes, so a pre-selected list goes stale in minutes.",
    )
    ap.add_argument("--discover-speed", type=int, default=32)
    ap.add_argument(
        "--seed",
        type=int,
        default=0,
        help="RNG seed for the interleaved cell order (reproducible runs)",
    )
    ap.add_argument(
        "--flush-sectors",
        type=int,
        default=1100,
        help="throwaway block size for cache eviction (> drive cache in sectors)",
    )
    ap.add_argument(
        "--gt-speed", type=int, default=8, help="ground-truth capture speed"
    )
    ap.add_argument("--gt-passes", type=int, default=6)
    ap.add_argument(
        "--gt-cache",
        default="",
        help="path to persist/reuse the consensus ground truth across runs",
    )
    ap.add_argument("--out", default="private/bench/runs/run3/overread.toml")
    args = ap.parse_args(argv)

    from recovery_bench import set_speed_to_max

    from cdda2img.accudisc_reader import park_spindle, read_toc
    from cdda2img.accuraterip import fetch_ar_responses
    from cdda2img.cddb import compute_cddb_disc_id

    speeds = [int(s) for s in args.speeds.split(",") if s]
    leadins = sorted({int(k) for k in args.leadins.split(",") if k})
    # Ground truth always covers the full flagged set (a whole-disc pass costs the
    # same however many sectors we track), so its cache is reusable across battery
    # runs; --sectors chooses the (sub)set the battery actually sweeps.
    gt_targets = (
        [int(x) for x in args.gt_sectors.split(",") if x]
        if args.gt_sectors
        else list(_TRACY_FLAGGED)
    )
    battery_targets = (
        [int(x) for x in args.sectors.split(",") if x] if args.sectors else gt_targets
    )
    gt_targets = sorted(set(gt_targets) | set(battery_targets))

    geom = read_toc(args.device)
    track_lsns = geom.track_lsns
    # Two quantities one sector apart, and they are NOT interchangeable:
    #   disc_last_lsn -- LSN of the last audio sector (the disc-ID input)
    #   leadout       -- first sector past the audio, i.e. the program-area
    #                    sector count (the read/geometry bound used below)
    # Both compute_cddb_disc_id and _ar_disc_ids take disc_last_lsn and add the
    # 1 themselves. Passing leadout here adds it twice: id1 is off by 1 and id2
    # by n+1, which yields a well-formed URL for a disc that does not exist, and
    # AccurateRip answers 404 -- indistinguishable from "disc not in database".
    # That bug is what made the 2026-07-23 pilot look like an AR outage.
    leadout = geom.disc_last_lsn + 1
    cddb_hex = compute_cddb_disc_id(track_lsns, geom.disc_last_lsn)
    responses, _transport, _b3 = fetch_ar_responses(
        track_lsns, geom.disc_last_lsn, int(cddb_hex, 16)
    )
    print(
        f"# disc cddb {cddb_hex}, {len(track_lsns)} tracks, lead-out {leadout}, "
        f"{len(responses)} AR block(s){' (AR unreachable — consensus only)' if not responses else ''};"
        f" gt {gt_targets}; sweep {battery_targets}",
        flush=True,
    )

    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.parent / ".overread_target.pcm"
    flush_tmp = out.parent / ".overread_flush.pcm"
    gt_tmp = out.parent / ".overread_gt.pcm"

    gt_cache = Path(args.gt_cache).resolve() if args.gt_cache else None
    try:
        set_speed_to_max(args.device)
        if args.discover:
            live = discover_flagged(args.device, args.discover_speed, out.parent)
            if not live:
                print(
                    "# discovery found no C2-flagged sector — the disc is reading "
                    "clean right now, so there is no defect to recover and every "
                    "cell would score 1.00. Aborting rather than banking a null.",
                    file=sys.stderr,
                )
                return 4
            battery_targets = live
            gt_targets = sorted(set(gt_targets) | set(live))
            gt_cache = None  # live targets are new; never reuse a stale bank
            print(f"# sweeping live flagged set: {battery_targets}", flush=True)
        gt = load_gt_cache(gt_cache, gt_targets) if gt_cache else None
        if gt is not None:
            print(
                f"# ground truth loaded from cache ({len(gt.sector_bytes)} sectors)",
                flush=True,
            )
        else:
            gt = build_ground_truth(
                args.device,
                track_lsns,
                leadout,
                args.read_offset,
                responses,
                gt_targets,
                args.gt_speed,
                args.gt_passes,
                gt_tmp,
                min_agree=args.gt_passes // 2 + 1,  # strict majority of passes
            )
            if gt_cache and gt.sector_bytes:
                save_gt_cache(gt_cache, gt)
        # Scope the battery to the requested sweep set (those with ground truth).
        sweep_gt = GroundTruth(
            sector_bytes={
                lba: gt.sector_bytes[lba]
                for lba in battery_targets
                if lba in gt.sector_bytes
            },
            verified_tracks=gt.verified_tracks,
        )
        if not sweep_gt.sector_bytes:
            print("# no ground truth for the swept sectors — aborting", file=sys.stderr)
            return 3
        cells = run_battery(
            args.device,
            sweep_gt,
            leadout,
            speeds,
            leadins,
            args.tail,
            args.reps,
            args.flush_sectors,
            tmp,
            flush_tmp,
            args.seed,
        )
    finally:
        for f in (tmp, flush_tmp, gt_tmp):
            f.unlink(missing_ok=True)
        set_speed_to_max(args.device)
        park_spindle(args.device)

    meta = {
        "disc": cddb_hex,
        "read_offset": args.read_offset,
        "speeds": speeds,
        "leadins": leadins,
        "tail": args.tail,
        "reps": args.reps,
        "flush_sectors": args.flush_sectors,
        "seed": args.seed,
        "cell_order": "interleaved-rounds",
    }
    write_toml(out, cells, sweep_gt, meta)
    print(summarise(cells, speeds, leadins))
    print(f"\n# wrote {len(cells)} cells → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
