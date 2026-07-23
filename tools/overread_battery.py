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

# Default targets: the 12 distinct C2-flagged LBAs observed across Tracy Chapman's
# run3 matrix (tracks 2, 7, 8, 9). Override with --sectors.
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

    @property
    def rate(self) -> float:
        return self.successes / self.reps if self.reps else 0.0


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
    """AR-verify one track's offset-corrected slice out of a whole-disc raw capture."""
    from cdda2img.accuraterip import match_track_pcm

    idx = track - 1
    s = track_lsns[idx]
    e = track_lsns[idx + 1] if idx + 1 < len(track_lsns) else leadout
    base = s * _SECTOR_BYTES + read_offset * 4
    corrected = pcm[base : e * _SECTOR_BYTES + read_offset * 4]
    _v1, _v2, c1, c2 = match_track_pcm(corrected, track, len(track_lsns), responses)
    return c1 is not None or c2 is not None


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
    """Establish each target sector's true bytes by **consensus across whole-disc
    passes**. A full sequential pass reads each sector correctly the large majority
    of the time (only a handful of 162k sectors flag per pass), so the modal value
    over a few passes converges to the truth — no live AccurateRip dependency, which
    matters because the AR endpoint 404s intermittently.

    A sector is banked only if its most-common value was seen in ``>= min_agree``
    passes (a strict majority guards against a deterministic mis-read winning). When
    AR blocks *are* available, each banked sector's enclosing track is AR-checked as
    a bonus cross-check and recorded in ``verified_tracks`` — but AR is never
    required."""
    from collections import Counter

    from cdda2img.accudisc_reader import read_span

    want = {lba: _track_of(lba, track_lsns, leadout) for lba in targets}
    observed: dict[int, Counter[bytes]] = {lba: Counter() for lba in targets}
    passes_pcm: list[bytes] = []

    for attempt in range(1, passes + 1):
        print(f"# ground truth pass {attempt}/{passes} (consensus)", flush=True)
        read_span(device, 0, leadout, tmp, read_speed=speed)
        pcm = tmp.read_bytes()
        passes_pcm.append(pcm)
        for lba in targets:
            sec = pcm[lba * _SECTOR_BYTES : (lba + 1) * _SECTOR_BYTES]
            observed[lba][sec] += 1

    gt = GroundTruth()
    for lba in targets:
        value, count = observed[lba].most_common(1)[0]
        distinct = len(observed[lba])
        if count >= min_agree:
            gt.sector_bytes[lba] = value
            print(
                f"#   lba {lba}: consensus {count}/{passes} "
                f"({distinct} distinct value(s) seen)"
            )
        else:
            print(
                f"# WARNING: lba {lba} no {min_agree}/{passes} consensus "
                f"(top {count}/{passes}, {distinct} distinct) — dropped",
                file=sys.stderr,
            )

    if responses:  # optional AR cross-check, best-effort
        for track in sorted({want[lba] for lba in gt.sector_bytes}):
            if any(
                _verify_track(pcm, track, track_lsns, leadout, read_offset, responses)
                for pcm in passes_pcm
            ):
                gt.verified_tracks.add(track)
        if gt.verified_tracks:
            print(f"#   AR cross-check: tracks {sorted(gt.verified_tracks)} verified")

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
) -> list[Cell]:
    targets = sorted(gt.sector_bytes)
    cells: list[Cell] = []
    total = len(speeds) * len(targets) * len(leadins)
    done = 0
    for speed in speeds:
        for lba in targets:
            start_lba = None
            for k in leadins:
                start_lba = max(0, lba - k)
                cell = Cell(speed, lba, k, lba - start_lba, reps=reps)
                for _ in range(reps):
                    _flush_cache(device, lba, leadout, flush_sectors, speed, flush_tmp)
                    got = _read_target_sector(device, lba, k, tail, speed, tmp)
                    if got == gt.sector_bytes[lba]:
                        cell.successes += 1
                cells.append(cell)
                done += 1
                print(
                    f"[{done}/{total}] {speed:>2}x  lba {lba}  K={k:<4} "
                    f"(actual {cell.actual_leadin})  "
                    f"{cell.successes}/{reps}  rate={cell.rate:.2f}",
                    flush=True,
                )
    return cells


# --- reporting -----------------------------------------------------------------


def summarise(cells: list[Cell], speeds: list[int], leadins: list[int]) -> str:
    """Aggregate success rate per (speed, K) across all target sectors — the headline
    speed x run-up interaction."""
    agg: dict[tuple[int, int], list[int]] = {}
    for c in cells:
        s, r = agg.setdefault((c.speed, c.leadin), [0, 0])
        agg[(c.speed, c.leadin)] = [s + c.successes, r + c.reps]

    lines = ["", "=== success rate by speed x run-up (all target sectors pooled) ==="]
    header = "speed\\K " + "".join(f"{k:>8}" for k in leadins)
    lines.append(header)
    for speed in speeds:
        row = f"{speed:>5}x "
        for k in leadins:
            s, r = agg.get((speed, k), [0, 0])
            row += f"{(s / r if r else 0):>8.2f}"
        lines.append(row)
    lines.append("(cell = fraction of attempts byte-exact vs a known-good pass)")
    return "\n".join(lines)


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
    leadout = geom.disc_last_lsn + 1
    cddb_hex = compute_cddb_disc_id(track_lsns, leadout)
    responses, _transport, _b3 = fetch_ar_responses(
        track_lsns, leadout, int(cddb_hex, 16)
    )
    # AR is only a bonus cross-check now; ground truth is consensus-based, so an AR
    # 404 (the endpoint 404s intermittently) does not block the experiment.
    print(
        f"# disc cddb {cddb_hex}, {len(track_lsns)} tracks, lead-out {leadout}, "
        f"{len(responses)} AR block(s){' (AR unavailable — consensus only)' if not responses else ''};"
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
    }
    write_toml(out, cells, sweep_gt, meta)
    print(summarise(cells, speeds, leadins))
    print(f"\n# wrote {len(cells)} cells → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
