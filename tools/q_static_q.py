#!/usr/bin/env python3
"""How *static* is a static Q frame? Subset-matched counts and the q estimator.

``q_frame_stability.py`` answers "which frames failed CRC in every capture". It
cannot answer "and how reliably do they fail", because a single intersection depth
gives one number and the question needs two. This tool supplies the second by
intersecting **subsets** of the same captures — no extra reads, no model.

If a frame in the retained population fails independently with per-capture
probability q, an intersection over n captures keeps it with probability q**n. So
for two depths n > k over the same disc::

    count(n) / count(k) = q**(n - k)      ->      q = (count(n) / count(k)) ** (1/(n-k))

Two things fall out of the same loop:

**The matched comparison.** Cross-disc static counts are only comparable at equal
depth: a deeper intersection is strictly harder to survive, so a disc measured at
n=15 is biased low against one measured at n=12 on sample size alone. Drawing
random k-subsets of the n captures gives the count *as if* the disc had been
measured at depth k. That is a measurement, not a correction. (AccuDisc §aw.1.)

**The q estimate.** Which is the more interesting half, because it is a property of
the defect population rather than of our sampling: q = 1 is a perfectly
deterministic defect, and q well below 1 says "static" is a threshold artefact of
the depth we happened to choose.

Caveats that belong in any report of the output, not in a footnote:

- **q is pooled unless you stratify.** The estimator assumes one homogeneous
  population. ABBA *Gold*'s static frames sat 90 % in deciles 0/1/9 while ZZ Top's
  were uniform, so a single q may be a mixture on one disc and clean on the other.
  ``--strata`` splits by LBA decile group and reports q per stratum: equal q across
  strata means the pooled figure is a mean that means what it says, unequal q means
  "static" is two populations, which is the better result. (AccuDisc §ax.3.)
- **The spread across draws is not a confidence interval.** Subsets of one capture
  set share captures and are therefore correlated; the reported SD describes
  which-k sampling noise only, and says nothing about how this disc would compare
  to a re-rip of it.
- **q is undefined when count(k) is 0**, and unstable when it is very small. The
  tool reports the counts and declines the division rather than emitting a
  plausible-looking number from one or two frames.

Usage:
    uv run python tools/q_static_q.py private/bench/runs/run8/*.sub
    uv run python tools/q_static_q.py --depth 12 --draws 40 --strata run8/*.sub
    uv run python tools/q_static_q.py --depth 9 --json private/bench/runs/run6/*.sub
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import random
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from q_frame_stability import (
    Capture,
    TruncatedCapture,
    check_lengths,
    load_capture,
    overlap_window,
)

# The decile grouping ABBA's static population concentrated into (§75.2). Named
# rather than inlined because the split is an empirical finding about one disc, and
# a reader has to be able to see that it was chosen before this tool ran, not after.
_EDGE_DECILES = frozenset({0, 1, 9})


def static_at(captures: list[Capture], window: tuple[int, int]) -> set[int]:
    """LBAs bad in *every* capture, restricted to the common window.

    Set intersection rather than a tally, because the tally form invites an
    off-by-one in the threshold and this has exactly one meaning.
    """
    lo, hi = window
    it = iter(captures)
    acc = {lba for lba in next(it).bad if lo <= lba < hi}
    for cap in it:
        acc &= cap.bad
        if not acc:
            break
    return acc


def decile(lba: int, window: tuple[int, int]) -> int:
    """0..9 — which tenth of the common window this LBA sits in."""
    lo, hi = window
    span = hi - lo
    if span <= 0:
        return 0
    return min(9, (lba - lo) * 10 // span)


@dataclass
class SubsetResult:
    """Counts at a shallower depth, over random subsets of one capture set."""

    depth: int
    draws: int
    exhaustive: bool
    """True when every C(n,k) subset was enumerated, so `mean` is exact."""
    counts: list[int]

    @property
    def mean(self) -> float:
        return statistics.fmean(self.counts)

    @property
    def sd(self) -> float:
        return statistics.stdev(self.counts) if len(self.counts) > 1 else 0.0


def subset_counts(
    captures: list[Capture],
    window: tuple[int, int],
    depth: int,
    draws: int,
    rng: random.Random,
) -> SubsetResult:
    """Static-frame count over `draws` random subsets of size `depth`.

    Enumerates exhaustively when C(n, k) <= draws — at that size sampling would be
    strictly worse than the exact answer, and the exactness is worth reporting.
    """
    n = len(captures)
    total = math.comb(n, depth)
    if total <= draws:
        subsets: list[tuple[Capture, ...]] = list(
            itertools.combinations(captures, depth)
        )
        exhaustive = True
    else:
        # Sample *without* replacement over subsets: a repeated subset contributes a
        # duplicate of one measurement, which shrinks the reported SD without adding
        # information about the spread it is supposed to describe.
        seen: set[tuple[int, ...]] = set()
        subsets = []
        while len(subsets) < draws:
            idx = tuple(sorted(rng.sample(range(n), depth)))
            if idx in seen:
                continue
            seen.add(idx)
            subsets.append(tuple(captures[i] for i in idx))
        exhaustive = False

    return SubsetResult(
        depth=depth,
        draws=len(subsets),
        exhaustive=exhaustive,
        counts=[len(static_at(list(s), window)) for s in subsets],
    )


def estimate_q(count_deep: float, count_shallow: float, gap: int) -> float | None:
    """q from two intersection depths, or None when the ratio cannot carry it.

    Declines rather than guesses in the two cases where the arithmetic still works
    and the answer would be meaningless: an empty shallow count (division by zero)
    and a zero deep count (q = 0 asserted from an absence). A ratio above 1 is
    returned as-is — it is impossible under the model, so it is evidence against
    the model and must not be silently clamped to 1.
    """
    if gap <= 0 or count_shallow <= 0 or count_deep <= 0:
        return None
    return (count_deep / count_shallow) ** (1.0 / gap)


@dataclass
class StratumReport:
    name: str
    deep_count: int
    shallow_mean: float
    q: float | None


def stratified(
    captures: list[Capture],
    window: tuple[int, int],
    depth: int,
    draws: int,
    rng: random.Random,
) -> list[StratumReport]:
    """q computed separately for the edge deciles (0/1/9) and everything else."""
    n = len(captures)
    deep = static_at(captures, window)
    masks = {
        "edge (deciles 0,1,9)": lambda lba: decile(lba, window) in _EDGE_DECILES,
        "interior (deciles 2-8)": lambda lba: decile(lba, window) not in _EDGE_DECILES,
    }

    # One subset draw, scored under both masks — separate draws per stratum would
    # make the two q values differ partly through sampling noise rather than through
    # the physical difference the comparison exists to detect.
    sub = subset_counts(captures, window, depth, draws, rng)
    per_mask: dict[str, list[int]] = {k: [] for k in masks}
    total = math.comb(n, depth)
    if total <= sub.draws:
        subsets = list(itertools.combinations(captures, depth))
    else:
        rng2 = random.Random(rng.random())  # noqa: S311 — statistical sample, seeded for reproducibility
        seen: set[tuple[int, ...]] = set()
        subsets = []
        while len(subsets) < sub.draws:
            idx = tuple(sorted(rng2.sample(range(n), depth)))
            if idx in seen:
                continue
            seen.add(idx)
            subsets.append(tuple(captures[i] for i in idx))
    for s in subsets:
        st = static_at(list(s), window)
        for name, pred in masks.items():
            per_mask[name].append(sum(1 for lba in st if pred(lba)))

    out = []
    for name, pred in masks.items():
        dc = sum(1 for lba in deep if pred(lba))
        sm = statistics.fmean(per_mask[name]) if per_mask[name] else 0.0
        out.append(
            StratumReport(
                name=name,
                deep_count=dc,
                shallow_mean=sm,
                q=estimate_q(dc, sm, n - depth),
            )
        )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("captures", nargs="+", type=Path)
    ap.add_argument(
        "--depth",
        type=int,
        default=12,
        help="shallower intersection depth k for the matched comparison (default 12, "
        "the depth ABBA Gold was measured at)",
    )
    ap.add_argument(
        "--draws",
        type=int,
        default=40,
        help="random k-subsets to average over (default 40); enumerates exhaustively "
        "when C(n,k) is smaller",
    )
    ap.add_argument(
        "--strata",
        action="store_true",
        help="also report q separately for LBA deciles 0/1/9 vs 2-8 (AccuDisc §ax.3)",
    )
    ap.add_argument("--seed", type=int, default=0, help="RNG seed (default 0)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--allow-short", action="store_true")
    args = ap.parse_args(argv)

    caps = [load_capture(p) for p in args.captures]
    n = len(caps)
    if args.depth >= n:
        print(
            f"--depth {args.depth} must be below the capture count ({n}): the estimator "
            "needs two different intersection depths",
            file=sys.stderr,
        )
        return 2
    if args.depth < 2:
        print("--depth must be at least 2", file=sys.stderr)
        return 2
    if not args.allow_short:
        try:
            check_lengths(caps)
        except TruncatedCapture as exc:
            print(f"refusing: {exc}", file=sys.stderr)
            return 2

    window = overlap_window(caps)
    rng = random.Random(args.seed)  # noqa: S311
    deep = static_at(caps, window)
    sub = subset_counts(caps, window, args.depth, args.draws, rng)
    q = estimate_q(len(deep), sub.mean, n - args.depth)
    strata = (
        stratified(caps, window, args.depth, args.draws, random.Random(args.seed))  # noqa: S311
        if args.strata
        else []
    )

    if args.json:
        print(
            json.dumps(
                {
                    "captures": n,
                    "window": list(window),
                    "deep_depth": n,
                    "deep_count": len(deep),
                    "shallow_depth": sub.depth,
                    "shallow_draws": sub.draws,
                    "shallow_exhaustive": sub.exhaustive,
                    "shallow_mean": round(sub.mean, 3),
                    "shallow_sd": round(sub.sd, 3),
                    "shallow_min": min(sub.counts),
                    "shallow_max": max(sub.counts),
                    "q": q,
                    "strata": [
                        {
                            "name": s.name,
                            "deep_count": s.deep_count,
                            "shallow_mean": round(s.shallow_mean, 3),
                            "q": s.q,
                        }
                        for s in strata
                    ],
                },
                indent=2,
            )
        )
        return 0

    print(f"{n} captures, common window LBA [{window[0]}, {window[1]})")
    print(f"  static at depth {n:>2}: {len(deep)}")
    how = "exhaustive" if sub.exhaustive else f"{sub.draws} random subsets"
    print(
        f"  static at depth {sub.depth:>2}: {sub.mean:.1f} "
        f"(sd {sub.sd:.1f}, range {min(sub.counts)}-{max(sub.counts)}, {how})"
    )
    if q is None:
        print(
            f"\nq: undefined — needs both counts non-zero "
            f"(deep {len(deep)}, shallow mean {sub.mean:.1f})"
        )
    else:
        print(f"\nq = ({len(deep)} / {sub.mean:.1f}) ** (1/{n - sub.depth}) = {q:.4f}")
        if q > 1.0:
            print(
                "  q > 1 is impossible under the model (a deeper intersection cannot "
                "retain more frames than a shallower one) — reported unclamped, as "
                "evidence against the model rather than a value"
            )
        else:
            print(
                f"  a static frame reads bad in {q * 100:.1f}% of captures; "
                f"q=1 would be a perfectly deterministic defect"
            )

    for s in strata:
        qs = f"{s.q:.4f}" if s.q is not None else "undefined"
        print(
            f"\n  {s.name}: deep {s.deep_count}, shallow {s.shallow_mean:.1f}, q = {qs}"
        )
    if strata:
        print(
            "\n  Equal q across strata => the pooled q is a mean and means what it says.\n"
            "  Unequal => 'static' is two populations, not one (AccuDisc §ax.3)."
        )
    print(
        "\nThe subset SD is which-k sampling noise only. Subsets of one capture set "
        "share\ncaptures and are correlated; it is not a confidence interval for the "
        "disc."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
