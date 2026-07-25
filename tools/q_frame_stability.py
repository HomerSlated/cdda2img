#!/usr/bin/env python3
"""Frame-level static-vs-transient classification of CRC-bad Q frames.

Aggregate Q tallies (``subq_ok``/``subq_total``, AccuDisc's per-window ``[n ok, m
bad]``) say *how many* frames failed, never *which*. That is not a small gap: a
window whose count moves 5 -> 10 between runs is indistinguishable, from counts
alone, between a stable physical defect and an elevated transient rate. AccuDisc
withdrew a static-population claim on exactly this ground (correspondence §ah), and
the same limit applies to our ``subchannel.scan_subcode`` counters.

This tool closes it the only way counts cannot: intersect the *sets* of failing
frames across repeated captures of one disc.

    frame bad in ALL n captures  ->  static candidate
    frame bad in some           ->  transient
    frame bad in one            ->  noise

The test is sharp because it compounds. If a frame fails transiently with
probability q, the chance it fails in all n *independent* captures is q**n — at the
q ~ 0.017 measured on ABBA *Gold*, 0.017**12 ~ 6e-22. Read that as the transient
null being decisively rejected by any surviving frame, not as the probability that
a survivor is static: independence is exactly what a static defect violates, so it
cannot be assumed when estimating the posterior.

An empty intersection is a real negative — but only over a window every capture
actually covered, which is why ``check_lengths`` refuses rather than clips. "No
static frame" from data that silently excluded the frame in question is the failure
this tool exists to avoid, not a result it is allowed to produce.

Captures are aligned by absolute LBA, never by file index. Two captures of the same
disc can start at different lead-in offsets, and comparing raw indices across them
would silently compare different frames and manufacture exactly the disagreement
the tool exists to measure.

Usage:
    uv run python tools/q_frame_stability.py CAPTURE.sub [CAPTURE.sub ...]
    uv run python tools/q_frame_stability.py --json private/bench/runs/run6/*.sub
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cdda2img.subchannel import (
    CD_SUBCODE_SIZE,
    decode_q,
    scan_subcode,
)


@dataclass
class Capture:
    """One raw subchannel capture, with its bad frames resolved to absolute LBAs."""

    path: Path
    n_sectors: int
    base_lba: int
    anchored: bool
    """True if base_lba came from the Q stream; False if it was assumed 0."""
    bad: set[int]
    """Absolute LBAs whose Q frame failed CRC."""

    @property
    def bad_rate(self) -> float:
        return len(self.bad) / self.n_sectors if self.n_sectors else 0.0


def load_capture(path: Path) -> Capture:
    """Read one ``.sub`` and return every CRC-failing frame as an absolute LBA."""
    data = path.read_bytes()
    n = len(data) // CD_SUBCODE_SIZE
    if n == 0:
        msg = f"{path}: not a subchannel capture (needs whole 96-byte frames)"
        raise ValueError(msg)

    scan = scan_subcode(data)
    anchored = scan.base_lba is not None
    # Falling back to 0 is correct for a whole-disc `accudisc read` (file index IS
    # the LBA), but it is an assumption, so it is reported rather than hidden: an
    # unanchored capture silently misaligned by even one frame would turn a static
    # defect into two transient ones.
    base = scan.base_lba if scan.base_lba is not None else 0

    bad = {
        base + s
        for s in range(n)
        if not decode_q(data[s * CD_SUBCODE_SIZE : (s + 1) * CD_SUBCODE_SIZE]).valid
    }
    return Capture(path=path, n_sectors=n, base_lba=base, anchored=anchored, bad=bad)


def classify(captures: list[Capture]) -> dict[int, int]:
    """Map each ever-bad LBA -> the number of captures it was bad in."""
    tally: dict[int, int] = {}
    for cap in captures:
        for lba in cap.bad:
            tally[lba] = tally.get(lba, 0) + 1
    return tally


def overlap_window(captures: list[Capture]) -> tuple[int, int]:
    """The LBA range covered by *every* capture.

    A frame absent from one capture's range is not evidence of anything, so the
    intersection must be taken over the common window only — otherwise a capture
    that happened to start later would demote a genuinely static frame.
    """
    lo = max(c.base_lba for c in captures)
    hi = min(c.base_lba + c.n_sectors for c in captures)
    return lo, hi


class TruncatedCapture(Exception):
    """A capture is shorter than its peers, so the common window is not the disc."""


def check_lengths(captures: list[Capture]) -> None:
    """Refuse to intersect when one capture is short. Loudly, not quietly.

    ``overlap_window`` conservatively clips to the shortest capture, which is the
    right arithmetic and the wrong silence: a single cell that died at 60% would
    drop the whole tail of the disc out of the window, and every frame there would
    be reported "not static" when it was simply never examined. On this disc frame
    281732 — the one frame anybody is asking about — sits at ~81%, inside exactly
    that discarded tail.

    So the short capture must be *named and excluded* by the operator, never
    absorbed into a narrower window that still reports a confident answer.
    """
    longest = max(c.n_sectors for c in captures)
    short = [c for c in captures if c.n_sectors != longest]
    if short:
        detail = ", ".join(
            f"{c.path.name} ({c.n_sectors}/{longest} sectors, "
            f"{100.0 * c.n_sectors / longest:.1f}%)"
            for c in short
        )
        msg = (
            f"capture length mismatch — the common window would silently exclude "
            f"every frame past the shortest capture: {detail}. Drop the short "
            f"capture(s) explicitly, or pass --allow-short to intersect anyway."
        )
        raise TruncatedCapture(msg)


def _false_static_probability(rates: list[float]) -> float:
    """P(one frame is independently bad in *every* capture) = the product of the
    per-capture rates.

    **Not** ``mean_rate ** n``. That was the first version, and it is only equivalent
    when the captures share a rate. On the run this tool was written for they do not:
    32x ran at 52.2 % bad and 8x at 0.77 %, a 65-fold spread, and the pooled mean
    (14.4 %) describes none of the twelve. The product is 3.8e-18 where mean**12 gives
    8.1e-11 — seven orders of magnitude apart, both "vanishingly small", which is
    exactly why the wrong one survives inspection. Same class as everything else in
    this file's history: well-formed, plausible, about a different question.
    """
    p = 1.0
    for r in rates:
        p *= r
    return p


@dataclass
class Report:
    """The intersection result. A dataclass, not a dict, so the printer reads its
    fields directly instead of narrowing ``object`` back to a type it already knew."""

    captures: list[Capture]
    window: tuple[int, int]
    static: list[int]
    transient_multi: list[int]
    transient_once: list[int]
    mean_bad_rate: float

    @property
    def ever_bad(self) -> int:
        return len(self.static) + len(self.transient_multi) + len(self.transient_once)

    @property
    def false_static_p(self) -> float:
        return _false_static_probability([c.bad_rate for c in self.captures])

    @property
    def expected_static_by_chance(self) -> float:
        """How many static frames the window should contain if none were real."""
        return self.false_static_p * (self.window[1] - self.window[0])

    def to_json(self) -> dict[str, object]:
        return {
            "captures": [
                {
                    "path": str(c.path),
                    "sectors": c.n_sectors,
                    "base_lba": c.base_lba,
                    "anchored": c.anchored,
                    "bad": len(c.bad),
                    "bad_rate": round(c.bad_rate, 6),
                }
                for c in self.captures
            ],
            "common_window": list(self.window),
            "ever_bad": self.ever_bad,
            "static": self.static,
            "transient_multi": self.transient_multi,
            "transient_once": self.transient_once,
            "mean_bad_rate": round(self.mean_bad_rate, 6),
            "false_static_p": self.false_static_p,
            "expected_static_by_chance": self.expected_static_by_chance,
        }


def report(captures: list[Capture]) -> Report:
    lo, hi = overlap_window(captures)
    tally = {lba: k for lba, k in classify(captures).items() if lo <= lba < hi}
    n = len(captures)
    return Report(
        captures=captures,
        window=(lo, hi),
        static=sorted(lba for lba, k in tally.items() if k == n),
        transient_multi=sorted(lba for lba, k in tally.items() if 1 < k < n),
        transient_once=sorted(lba for lba, k in tally.items() if k == 1),
        mean_bad_rate=sum(c.bad_rate for c in captures) / n,
    )


def _print_human(r: Report) -> None:
    n = len(r.captures)
    print(f"{n} capture(s), common window LBA [{r.window[0]}, {r.window[1]})")
    for c in r.captures:
        anchor = "anchored" if c.anchored else "ASSUMED base 0 (unanchored)"
        print(
            f"  {c.path.name:<28} {c.n_sectors:>7} sectors  "
            f"{len(c.bad):>6} bad ({c.bad_rate * 100:.3f}%)  {anchor}"
        )

    print(
        f"\never-bad frames: {r.ever_bad}   "
        f"static (bad in all {n}): {len(r.static)}   "
        f"transient (2..n-1): {len(r.transient_multi)}   "
        f"once-only: {len(r.transient_once)}"
    )
    print(
        f"per-capture rates {min(c.bad_rate for c in r.captures) * 100:.3f}%-"
        f"{max(c.bad_rate for c in r.captures) * 100:.3f}% -> a transient frame survives "
        f"all {n} captures with p = {r.false_static_p:.2e}; expected static-by-chance "
        f"over this window = {r.expected_static_by_chance:.2e}"
    )

    if r.static:
        print(f"\nSTATIC CANDIDATES ({len(r.static)}):")
        for lba in r.static[:50]:
            print(f"  LBA {lba}")
        if len(r.static) > 50:
            print(f"  ... and {len(r.static) - 50} more")
    else:
        print(
            "\nNo static frame. Every CRC failure in the common window read clean at "
            "least once — this disc has no frame-level static Q defect to model "
            "against, however stable its per-track counts look."
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("captures", nargs="+", type=Path)
    ap.add_argument("--json", action="store_true", help="emit the full report as JSON")
    ap.add_argument(
        "--allow-short",
        action="store_true",
        help="intersect even when captures differ in length (the common window will "
        "exclude everything past the shortest capture — see check_lengths)",
    )
    args = ap.parse_args(argv)

    if len(args.captures) < 2:
        print(
            "need at least 2 captures: staticness is a claim about repeated reads, "
            "and one read cannot support it",
            file=sys.stderr,
        )
        return 2

    caps = [load_capture(p) for p in args.captures]
    if not args.allow_short:
        try:
            check_lengths(caps)
        except TruncatedCapture as exc:
            print(f"refusing to intersect: {exc}", file=sys.stderr)
            return 2
    r = report(caps)
    if args.json:
        print(json.dumps(r.to_json(), indent=2))
    else:
        _print_human(r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
