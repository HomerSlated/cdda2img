#!/usr/bin/env python3
"""Are static Q frames enriched near track boundaries? With a coverage baseline.

AccuDisc's §as.5 hypothesis is that the static Q population clusters around track
boundaries. Testing it needs one thing that an earlier version of this analysis
(outbound §71.3) did not have: **the fraction of the disc that is near a boundary**.

"2.7 % of static frames lie within 150 sectors of a boundary" reads as absence and is
meaningless alone. On a disc with 11 tracks and 162 892 sectors, +/-150 around each
interior boundary covers ~1.7 % of the disc, so 2.7 % is a 1.6x *enrichment*. The raw
percentage and the conclusion point in opposite directions. Every window here is
therefore reported against its own coverage, never as a bare share.

The test is binomial. Under the null, each static frame lands in the test region with
probability p = covered_sectors / window_sectors, independently of the others::

    expected = N_static * p
    z        = (observed - expected) / sqrt(N_static * p * (1 - p))

Two window shapes, because they answer different questions:

- **symmetric** (``--window W``): +/-W around every boundary. The natural first look.
- **asymmetric** (``--pre A --post B``): A sectors before, B after. This exists because
  on ABBA *Gold* the signal was **one-sided** — a pre-20/post-10 split at near-identical
  coverage put the whole effect on the pre side. A symmetric window dilutes a one-sided
  effect with null area, which costs power precisely when the effect is real.

**Multiplicity.** ABBA's split was chosen after looking, so it carried a k=3 Sidak
correction. A run that tests a *pre-specified* window is confirmatory and takes k=1 --
that is what pre-registration buys. ``--family K`` states the size of the family
explicitly and applies Sidak; it defaults to 1 and the output always names the value
used, so a corrected and an uncorrected p can never be confused for one another.

Usage:
    uv run python tools/q_boundary.py run8/q_intersection.json --fulltoc run8/geometry.fulltoc
    uv run python tools/q_boundary.py run8/q_intersection.json --boundaries 12032,34295 --pre 20 --post 10
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cdda2img.subchannel import parse_fulltoc, session1_audio_tracks


def load_boundaries(fulltoc: Path) -> tuple[list[int], int]:
    """Interior track starts and the lead-out LBA, from a raw READ TOC format-0x02 dump.

    LBA 0 is excluded: it is the start of the disc, not a boundary between two tracks,
    and it has no "before" side for a one-sided window to sit in. Including it would
    add coverage that the pre-side hypothesis cannot possibly populate.
    """
    tracks, leadout = session1_audio_tracks(parse_fulltoc(fulltoc.read_bytes()))
    starts = [t.start_lba for t in tracks if t.start_lba > 0]
    return starts, leadout


def covered(boundaries: list[int], pre: int, post: int, lo: int, hi: int) -> int:
    """Sectors within [b-pre, b+post) of any boundary, as a *union* clipped to [lo,hi).

    A union, not a sum: on a disc with a short track two windows can overlap, and
    double-counting the shared sectors would inflate the expected count and bias the
    test toward "no enrichment" — a conservative direction, but wrong is wrong, and it
    is silently wrong.
    """
    spans = sorted((max(lo, b - pre), min(hi, b + post)) for b in boundaries)
    total = 0
    cur_lo, cur_hi = None, None
    for s, e in spans:
        if e <= s:
            continue
        if cur_hi is None or s > cur_hi:
            if cur_hi is not None:
                total += cur_hi - cur_lo  # type: ignore[operator]
            cur_lo, cur_hi = s, e
        else:
            cur_hi = max(cur_hi, e)
    if cur_hi is not None:
        total += cur_hi - cur_lo  # type: ignore[operator]
    return total


def in_region(lba: int, boundaries: list[int], pre: int, post: int) -> bool:
    return any(b - pre <= lba < b + post for b in boundaries)


def _phi(z: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def sidak(p: float, k: int) -> float:
    """Family-wise p under Sidak for k independent tests. k=1 returns p unchanged."""
    if k <= 1:
        return p
    return 1.0 - (1.0 - p) ** k


@dataclass
class WindowTest:
    label: str
    pre: int
    post: int
    observed: int
    n_static: int
    covered_sectors: int
    window_sectors: int

    @property
    def p_null(self) -> float:
        return (
            self.covered_sectors / self.window_sectors if self.window_sectors else 0.0
        )

    @property
    def expected(self) -> float:
        return self.n_static * self.p_null

    @property
    def enrichment(self) -> float | None:
        return self.observed / self.expected if self.expected > 0 else None

    @property
    def z(self) -> float | None:
        p = self.p_null
        var = self.n_static * p * (1.0 - p)
        if var <= 0:
            return None
        return (self.observed - self.expected) / math.sqrt(var)

    @property
    def p_one_sided(self) -> float | None:
        z = self.z
        return None if z is None else 1.0 - _phi(z)

    def power_note(self) -> str | None:
        """Say so when the window is too sparse to detect the effect it is testing.

        A null from an expectation below ~5 frames is not evidence of absence, and
        reporting it as one is the mistake ZZ Top's boundary analysis would have made
        if the expectation had not been checked first (outbound §75).
        """
        if self.expected < 5.0:
            return (
                f"expectation {self.expected:.2f} frames — too sparse to test; a null "
                f"here is NO POWER, not absence"
            )
        return None


def run_test(
    label: str,
    static: list[int],
    boundaries: list[int],
    pre: int,
    post: int,
    lo: int,
    hi: int,
) -> WindowTest:
    return WindowTest(
        label=label,
        pre=pre,
        post=post,
        observed=sum(1 for lba in static if in_region(lba, boundaries, pre, post)),
        n_static=len(static),
        covered_sectors=covered(boundaries, pre, post, lo, hi),
        window_sectors=hi - lo,
    )


def deciles(static: list[int], lo: int, hi: int) -> tuple[list[int], float]:
    """Per-decile counts and the chi-square statistic against uniform (9 df)."""
    span = hi - lo
    counts = [0] * 10
    for lba in static:
        counts[min(9, (lba - lo) * 10 // span)] += 1
    exp = len(static) / 10.0
    chi2 = sum((c - exp) ** 2 / exp for c in counts) if exp > 0 else 0.0
    return counts, chi2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("intersection", type=Path, help="q_frame_stability --json output")
    ap.add_argument("--fulltoc", type=Path, help="raw READ TOC format-0x02 dump")
    ap.add_argument(
        "--boundaries",
        help="comma-separated interior track-start LBAs (overrides --fulltoc)",
    )
    ap.add_argument(
        "--window", type=int, default=150, help="symmetric +/-W (default 150)"
    )
    ap.add_argument("--pre", type=int, default=20, help="asymmetric: sectors before")
    ap.add_argument("--post", type=int, default=10, help="asymmetric: sectors after")
    ap.add_argument(
        "--family",
        type=int,
        default=1,
        help="number of tests in the family, for Sidak (default 1 = confirmatory, "
        "pre-specified window)",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    data = json.loads(args.intersection.read_text())
    static: list[int] = data["static"]
    lo, hi = data["common_window"]

    if args.boundaries:
        boundaries = [int(x) for x in args.boundaries.split(",") if x.strip()]
    elif args.fulltoc:
        boundaries, leadout = load_boundaries(args.fulltoc)
        if lo < leadout <= hi:
            boundaries.append(leadout)
    else:
        print("need --fulltoc or --boundaries", file=sys.stderr)
        return 2
    boundaries = sorted({b for b in boundaries if lo < b < hi})
    if not boundaries:
        print("no interior boundaries inside the common window", file=sys.stderr)
        return 2

    tests = [
        run_test(
            f"symmetric +/-{args.window}",
            static,
            boundaries,
            args.window,
            args.window,
            lo,
            hi,
        ),
        run_test(f"pre {args.pre}", static, boundaries, args.pre, 0, lo, hi),
        run_test(f"post {args.post}", static, boundaries, 0, args.post, lo, hi),
    ]
    dec, chi2 = deciles(static, lo, hi)

    if args.json:
        print(
            json.dumps(
                {
                    "n_static": len(static),
                    "window": [lo, hi],
                    "boundaries": boundaries,
                    "family_k": args.family,
                    "tests": [
                        {
                            "label": t.label,
                            "observed": t.observed,
                            "expected": round(t.expected, 3),
                            "coverage": round(t.p_null, 6),
                            "enrichment": t.enrichment,
                            "z": t.z,
                            "p_one_sided": t.p_one_sided,
                            "p_sidak": sidak(t.p_one_sided, args.family)
                            if t.p_one_sided is not None
                            else None,
                            "power_note": t.power_note(),
                        }
                        for t in tests
                    ],
                    "deciles": dec,
                    "chi2_uniform_9df": round(chi2, 3),
                },
                indent=2,
            )
        )
        return 0

    print(
        f"{len(static)} static frames over LBA [{lo}, {hi}), {len(boundaries)} boundaries"
    )
    print(
        f"Sidak family k = {args.family}"
        + ("  (confirmatory)" if args.family == 1 else "")
    )
    for t in tests:
        print(f"\n{t.label}:")
        print(
            f"  coverage {t.p_null * 100:6.3f}% of disc -> expected {t.expected:8.2f}, "
            f"observed {t.observed}"
        )
        if t.enrichment is not None and t.z is not None and t.p_one_sided is not None:
            ps = sidak(t.p_one_sided, args.family)
            tail = f", Sidak k={args.family} p = {ps:.3e}" if args.family > 1 else ""
            print(
                f"  enrichment {t.enrichment:.2f}x   z = {t.z:+.2f}   p = {t.p_one_sided:.3e}{tail}"
            )
        note = t.power_note()
        if note:
            print(f"  ** {note}")

    print(f"\ndeciles: {dec}")
    print(f"  chi2 vs uniform (9 df) = {chi2:.2f}   [critical 16.92 at p=0.05]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
