#!/usr/bin/env python3
"""Strategy bench: rank whole RECOVERY STRATEGIES by success rate and time-to-success.

The other bench tools measure parameters — run-up length, speed, sector. This one
measures the thing the user actually chooses: a **strategy**, run end to end until it
either recovers a track or gives up. That is the deliverable. A user can waste a long
period on a strategy that reaches the same result as a much faster one, or worse,
pursue one that fails where a faster one would have succeeded, so the ranking is
`P(success)` against `time-to-success` — not a mechanism.

No causal claim is made or needed. Candidate explanations for why a strategy wins
(disc slip, tracking drift, a surface-damage gradient, drive entropy) are speculation
and do not change the ranking.

The strategies deliberately span a **variation axis**, because the 2026-07-23 probes
found that every read pattern which improved the disc varied something (speed,
position, or both) while the one that varied nothing degraded it:

    sector-hammer   one sector, one speed, repeated      <- minimum variation
    sector-runup    one sector + run-up, one speed
    track-constant  whole track, one speed
    track-ladder    whole track, speed ladder            <- production today
    whole-disc      whole disc, one speed                 (positional variation)
    max-variation   random speed x random run-up x random order  <- maximum

`sector-hammer` is the bench's current `_recover_rung` shape, which scored 0/25 on this
disc while plain whole-disc passes came back clean ~27% of the time. If the variation
reading is right it should rank last here.

Method. Take one baseline whole-disc capture; find the tracks AccurateRip rejects.
For each trial, run a strategy against a *working copy* of that baseline, splicing each
read into place, and stop the clock the moment the target track AR-verifies. Splicing
mirrors production `_recover_failed_tracks`, and AR is the authoritative gate — a track
matches an AccurateRip block or it does not.

Trials are INTERLEAVED across strategies, never run in blocks: reading changes the
disc's state, so a strategy measured entirely in one contiguous slot would be scored
against whatever state that slot happened to hold.

Every result is LOCALLY meaningful only — one disc, one drive. What transposes is the
shape of the ranking, as a profile.

Usage (from project root):
    TMPDIR=/var/tmp uv run python tools/strategy_bench.py \
        --device /dev/sr0 --read-offset 30 --trials 3 \
        --out private/bench/runs/run3/tracy_chapman/strategy.toml
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from cdda2img.strategy_reads import STRATEGIES, strategy_reads, track_bounds

_SECTOR_BYTES = 2352
_C2_BYTES = 294


@dataclass
class Trial:
    strategy: str
    track: int
    success: bool
    elapsed_s: float
    attempts: int
    sectors_read: int


@dataclass
class Score:
    strategy: str
    trials: list[Trial] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.trials)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trials if t.success)

    @property
    def rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def mean_win_s(self) -> float | None:
        won = [t.elapsed_s for t in self.trials if t.success]
        return sum(won) / len(won) if won else None

    @property
    def expected_s(self) -> float | None:
        """Expected seconds to a recovered track: mean winning time / success rate.

        The ranking quantity. A strategy that wins 50% of the time in 60 s beats one
        that wins 100% of the time in 200 s. None when it never won."""
        m = self.mean_win_s
        return m / self.rate if m is not None and self.rate else None


# --- AR plumbing ---------------------------------------------------------------


def _verify_track(
    pcm: bytes,
    track: int,
    track_lsns: list[int],
    leadout: int,
    read_offset: int,
    responses: list[list[dict]],
) -> bool:
    """Mirrors accuraterip.verify_rip's window arithmetic — clamp AND zero-pad, or a
    positive read offset silently shortens track 1 / the last track and shifts
    _ar_checksums' exclusion boundary into a spurious miss."""
    from cdda2img.accuraterip import match_track_pcm

    idx = track - 1
    byte_start = track_lsns[idx] * _SECTOR_BYTES + read_offset * 4
    end_lsn = track_lsns[idx + 1] if idx + 1 < len(track_lsns) else leadout
    byte_end = end_lsn * _SECTOR_BYTES + read_offset * 4

    raw = pcm[max(0, byte_start) : min(len(pcm), byte_end)]
    if byte_start < 0:
        raw = bytes(-byte_start) + raw
    if byte_end > len(pcm):
        raw = raw + bytes(byte_end - len(pcm))

    _v1, _v2, c1, c2 = match_track_pcm(raw, track, len(track_lsns), responses)
    return c1 is not None or c2 is not None


# --- strategies ----------------------------------------------------------------
#
# The read plans live in cdda2img.strategy_reads, shared with the rip path since
# 2026-11-21, so what the bench scores and what `rip --profile` runs are one
# definition. Each is a generator of ReadReq; the runner splices every read into a
# working copy of the baseline and checks AR after each one.


def run_trial(
    device: str,
    name: str,
    track: int,
    baseline: bytes,
    flagged: list[int],
    track_lsns: list[int],
    leadout: int,
    read_offset: int,
    responses: list[list[dict]],
    ladder: list[int],
    base_speed: int,
    runup: int,
    span: int,
    rng: random.Random,
    max_attempts: int,
    budget_s: float,
    tmp: Path,
) -> Trial:
    """Run one strategy until the target track AR-verifies, or the budget runs out."""
    from cdda2img.accudisc_reader import read_span

    working = bytearray(baseline)
    t0 = time.monotonic()
    attempts = 0
    sectors = 0

    for req in strategy_reads(
        name,
        track,
        flagged,
        track_lsns,
        leadout,
        ladder,
        base_speed,
        runup,
        span,
        rng,
        max_attempts,
    ):
        if time.monotonic() - t0 > budget_s:
            break
        read_span(device, req.start, req.count, tmp, read_speed=req.speed)
        data = tmp.read_bytes()
        # Splice at the sector's own offset — neighbouring audio is never perturbed,
        # exactly as production _recover_failed_tracks does.
        working[req.start * _SECTOR_BYTES : req.start * _SECTOR_BYTES + len(data)] = (
            data
        )
        attempts += 1
        sectors += req.count

        if _verify_track(
            bytes(working), track, track_lsns, leadout, read_offset, responses
        ):
            return Trial(name, track, True, time.monotonic() - t0, attempts, sectors)

    return Trial(name, track, False, time.monotonic() - t0, attempts, sectors)


# --- reporting -----------------------------------------------------------------


def summarise(scores: dict[str, Score]) -> str:
    lines = [
        "",
        "=== recovery strategies, ranked by expected time to a recovered track ===",
        f"{'strategy':<16}{'wins':>8}{'rate':>8}{'mean_win_s':>13}{'expected_s':>13}",
    ]
    ranked = sorted(
        scores.values(),
        key=lambda s: (s.expected_s is None, s.expected_s or 0.0),
    )
    for s in ranked:
        mw = f"{s.mean_win_s:.0f}" if s.mean_win_s is not None else "--"
        ex = f"{s.expected_s:.0f}" if s.expected_s is not None else "--"
        lines.append(
            f"{s.strategy:<16}{s.wins:>4}/{s.n:<3}{s.rate:>8.2f}{mw:>13}{ex:>13}"
        )
    lines.append("(expected_s = mean winning time / success rate; '--' = never won)")
    return "\n".join(lines)


def write_toml(path: Path, scores: dict[str, Score], meta: dict) -> None:
    lines = ["# recovery strategy bench", ""]
    lines += [f"{k} = {v!r}" for k, v in meta.items()]
    lines.append("")
    for s in scores.values():
        lines += [
            "[[strategy]]",
            f"name = {s.strategy!r}",
            f"trials = {s.n}",
            f"wins = {s.wins}",
            f"rate = {s.rate:.4f}",
            f"mean_win_s = {s.mean_win_s if s.mean_win_s is not None else 0.0:.2f}",
            f"expected_s = {s.expected_s if s.expected_s is not None else 0.0:.2f}",
            "",
        ]
    for s in scores.values():
        for t in s.trials:
            lines += [
                "[[trial]]",
                f"strategy = {t.strategy!r}",
                f"track = {t.track}",
                f"success = {str(t.success).lower()}",
                f"elapsed_s = {t.elapsed_s:.2f}",
                f"attempts = {t.attempts}",
                f"sectors_read = {t.sectors_read}",
                "",
            ]
    path.write_text("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--device", default="/dev/sr0")
    ap.add_argument("--read-offset", type=int, default=30)
    ap.add_argument("--trials", type=int, default=3, help="trials per strategy")
    ap.add_argument("--strategies", default=",".join(STRATEGIES))
    ap.add_argument("--ladder", default="40,32,24,16,8,4")
    ap.add_argument("--base-speed", type=int, default=32)
    ap.add_argument("--runup", type=int, default=32)
    ap.add_argument(
        "--span", type=int, default=512, help="span-fixed arm: sectors before target"
    )
    ap.add_argument(
        "--track",
        type=int,
        default=0,
        help="target track (default: the first AR-failing one). Prefer the track that "
        "fails PERSISTENTLY -- an easy target makes every strategy win on attempt 1 "
        "and the ranking collapses to per-attempt cost.",
    )
    ap.add_argument("--max-attempts", type=int, default=8)
    ap.add_argument("--budget", type=float, default=240.0, help="seconds per trial")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default="private/bench/runs/run3/strategy.toml")
    args = ap.parse_args(argv)

    from cdda2img.accudisc_reader import park_spindle, read_disc_c2, read_toc
    from cdda2img.accuraterip import fetch_ar_responses
    from cdda2img.cddb import compute_cddb_disc_id

    names = [n for n in args.strategies.split(",") if n]
    unknown = [n for n in names if n not in STRATEGIES]
    if unknown:
        print(f"unknown strategies: {unknown}", file=sys.stderr)
        return 2
    ladder = [int(s) for s in args.ladder.split(",") if s]

    geom = read_toc(args.device)
    lsns = geom.track_lsns
    # compute_cddb_disc_id and _ar_disc_ids BOTH take disc_last_lsn and add the +1
    # themselves; passing the lead-out doubles it and 404s on a nonexistent disc.
    leadout = geom.disc_last_lsn + 1
    cddb_hex = compute_cddb_disc_id(lsns, geom.disc_last_lsn)
    responses, _t, _b = fetch_ar_responses(lsns, geom.disc_last_lsn, int(cddb_hex, 16))
    if not responses:
        print("no AccurateRip blocks — cannot gate recovery, aborting", file=sys.stderr)
        return 3
    print(f"# disc {cddb_hex}, {len(lsns)} tracks, {len(responses)} AR block(s)")

    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    pcm_p, c2_p = out.parent / ".sb_base.pcm", out.parent / ".sb_base.c2"
    span_tmp = out.parent / ".sb_span.pcm"

    scores = {n: Score(n) for n in names}
    try:
        print("# baseline whole-disc C2 capture ...", flush=True)
        read_disc_c2(args.device, pcm_p, c2_p, read_speed=args.base_speed)
        baseline = pcm_p.read_bytes()
        c2 = c2_p.read_bytes()
        flagged = [
            s
            for s in range(len(c2) // _C2_BYTES)
            if any(c2[s * _C2_BYTES : (s + 1) * _C2_BYTES])
        ]
        failing = [
            t
            for t in range(1, len(lsns) + 1)
            if not _verify_track(
                baseline, t, lsns, leadout, args.read_offset, responses
            )
        ]
        print(f"# baseline: {len(flagged)} C2-flagged, AR-failing tracks {failing}")
        # Per-failing-track flagged LBAs. Without these a run is not diagnosable: a
        # track can fail AR with ZERO C2 flags (silent mis-correction — the drive
        # believes the read was clean), and then every sector-targeted strategy has no
        # valid target and falls back to the track start, so it fails for a reason
        # that has nothing to do with the strategy. That case must be distinguishable
        # from a genuine sector-level failure, and the count alone cannot do it.
        for t in failing:
            t_start, t_end = track_bounds(t, lsns, leadout)
            in_t = [f for f in flagged if t_start <= f < t_end]
            note = (
                "  <-- NO C2 TARGET: sector strategies cannot aim" if not in_t else ""
            )
            print(f"#   track {t:2d}: n={len(in_t)} {in_t[:12]}{note}")
        if not failing:
            print(
                "# baseline is fully AR-clean — nothing to recover, so no strategy "
                "can be scored. Reload the disc and re-run until a load lands in a "
                "defective state.",
                file=sys.stderr,
            )
            return 4

        if args.track and args.track not in failing:
            print(
                f"# requested track {args.track} is not AR-failing "
                f"(failing: {failing}) — nothing to recover there",
                file=sys.stderr,
            )
            return 5
        target = args.track or failing[0]
        print(f"# recovery target: track {target}", flush=True)
        rng = random.Random(args.seed)  # noqa: S311 — experiment ordering
        # INTERLEAVED: one trial of each strategy per round, order reshuffled. Reading
        # changes the disc state, so block-running a strategy would score it against
        # whatever state its slot happened to hold.
        order = list(names)
        for rnd in range(1, args.trials + 1):
            rng.shuffle(order)
            for name in order:
                tr = run_trial(
                    args.device,
                    name,
                    target,
                    baseline,
                    flagged,
                    lsns,
                    leadout,
                    args.read_offset,
                    responses,
                    ladder,
                    args.base_speed,
                    args.runup,
                    args.span,
                    rng,
                    args.max_attempts,
                    args.budget,
                    span_tmp,
                )
                scores[name].trials.append(tr)
                print(
                    f"[r{rnd} {name:<15}] "
                    f"{'WIN ' if tr.success else 'fail'} "
                    f"{tr.elapsed_s:6.0f}s  attempts={tr.attempts}  "
                    f"sectors={tr.sectors_read}",
                    flush=True,
                )
    finally:
        for f in (pcm_p, c2_p, span_tmp):
            f.unlink(missing_ok=True)
        park_spindle(args.device)

    write_toml(
        out,
        scores,
        {
            "disc": cddb_hex,
            "trials": args.trials,
            "ladder": ladder,
            "base_speed": args.base_speed,
            "runup": args.runup,
            "span": args.span,
            "target_track": target,
            "budget_s": args.budget,
            "seed": args.seed,
            "trial_order": "interleaved-shuffled",
        },
    )
    print(summarise(scores))
    print(f"\n# wrote → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
