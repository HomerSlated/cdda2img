#!/usr/bin/env python3
"""Aesthetic bench for the N2 rip progress bar — the Q + C2 map widget.

Standalone and stdlib-only: it never imports ``cdda2img`` and never touches a
drive. Everything it draws comes from a synthetic disc, so a given error pattern
can be replayed identically as many times as it takes to settle the design. That
is the whole point (TODO N2): a widget driven only by a live rip can be exercised
once per disc, at twelve minutes a run, on whatever damage that disc happens to
carry.

Layout under test::

    ⠹  Ripping track 07…   ▀▀▀▀▀▀▀▀▀▀▀░░░░░░░   42.7% (149520/350000)
    │  │                   │                    │
    │  │                   │                    └─ right:  percent + sectors
    │  │                   └─ middle: the Q + C2 map, which IS the progress bar
    │  └─ left: status text
    └─ left: braille throbber

Run ``--gallery`` first: it renders every style and palette as static frames on
one screen, which is the comparison this tool exists to make.

Usage
-----
    uv run python tools/progress_lab.py --gallery
    uv run python tools/progress_lab.py --style dual --pattern rot
    uv run python tools/progress_lab.py --style glyph --palette mono --at 60
    uv run python tools/progress_lab.py --aggregates --pattern sparse
"""

from __future__ import annotations

import argparse
import random
import shutil
import sys
import time
from dataclasses import dataclass
from enum import Enum
from typing import NamedTuple

# ── the two lanes' per-sector states ──────────────────────────────────────────
#
# kgr's spec names three states per lane: "read", "OK", "error". "read" is the
# frontier — a sector the head has not reached yet is not an absence of errors,
# it is an absence of evidence, and the map must not colour it as good.


class St(Enum):
    UNREAD = 0
    OK = 1
    ERR = 2


# ── palettes ──────────────────────────────────────────────────────────────────
#
# 256-colour SGR rather than truecolour: every terminal that can draw half-blocks
# can do 256, and the palette indices survive a theme change better than hex.


@dataclass(frozen=True)
class Palette:
    name: str
    unread: int
    ok: int
    err: int
    note: str
    # Four shades of the error hue, faintest first, for `--aggregate ramp`. They
    # must stay within ONE hue: the ramp encodes severity, and a ramp that drifts
    # across hues reads as a change of kind rather than a change of degree.
    err_ramp: tuple[int, int, int, int] = (0, 0, 0, 0)


PALETTES: dict[str, Palette] = {
    # The obvious one. Fails for ~8% of men: red/green is the common dichromacy.
    "classic": Palette(
        "classic", 236, 34, 196, "green/red — familiar, not safe", (52, 124, 160, 196)
    ),
    # Blue/orange survives deuteranopia and protanopia, and stays distinct in
    # greyscale because the two differ in luminance as well as hue.
    "cb": Palette(
        "cb", 236, 33, 208, "blue/orange — colourblind-safe", (94, 136, 172, 214)
    ),
    # Higher chroma for a bright terminal; same hue relationship as classic.
    "vivid": Palette("vivid", 238, 46, 203, "bright green/salmon", (89, 125, 168, 203)),
    # No colour at all — see the `glyph` style, which encodes both lanes in the
    # character shape so the map still reads in a log file or over a pipe.
    "mono": Palette("mono", -1, -1, -1, "no colour; shape carries the signal"),
}


def fg(c: int) -> str:
    return f"\033[38;5;{c}m"


def bg(c: int) -> str:
    return f"\033[48;5;{c}m"


RESET = "\033[0m"
SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


# ── the synthetic disc ────────────────────────────────────────────────────────


@dataclass
class Disc:
    """A disc's worth of per-sector lane state, plus track geometry."""

    sectors: int
    track_starts: list[int]
    q: list[St]
    c2: list[St]

    def track_at(self, sector: int) -> int:
        n = 1
        for i, s in enumerate(self.track_starts):
            if sector >= s:
                n = i + 1
        return n


def _tracks(sectors: int, count: int, rng: random.Random) -> list[int]:
    """Track starts, unevenly spaced — a real disc has 2-minute and 7-minute
    tracks and the map's tick spacing has to look right under both."""
    cuts = sorted(rng.uniform(0.04, 0.99) for _ in range(count - 1))
    return [0, *(int(c * sectors) for c in cuts)]


def _burst(lane: list[St], centre: int, width: int, density: float, rng) -> None:
    """Paint an error burst with soft edges — damage is not a rectangle, and a
    map that renders it as one will look wrong against a real disc."""
    lo = max(0, centre - width // 2)
    hi = min(len(lane), centre + width // 2)
    for i in range(lo, hi):
        # Triangular falloff from the centre of the burst.
        edge = 1.0 - abs(i - centre) / max(1.0, width / 2)
        if rng.random() < density * edge:
            lane[i] = St.ERR


def _pat_clean(q, c2, n, rng) -> None:
    """The control. A map that looks busy on a clean disc is lying."""


def _pat_scratch(q, c2, n, rng) -> None:
    """A radial scratch crosses the spiral repeatedly, so it appears as several
    bursts at *increasing* spacing, not as one block."""
    pos = int(n * 0.18)
    step = int(n * 0.07)
    while pos < n * 0.85:
        _burst(c2, pos, int(n * 0.012), 0.85, rng)
        _burst(q, pos, int(n * 0.006), 0.4, rng)
        pos += step
        step = int(step * 1.15)


def _pat_rot(q, c2, n, rng) -> None:
    """Disc rot / edge delamination: density climbs toward the outer edge."""
    for i in range(n):
        r = i / n
        if r > 0.55 and rng.random() < ((r - 0.55) / 0.45) ** 2 * 0.5:
            c2[i] = St.ERR
            if rng.random() < 0.3:
                q[i] = St.ERR


def _pat_sparse(q, c2, n, rng) -> None:
    """Isolated pinpricks — the case `ratio` erases entirely and `worst` inflates
    into a solid red bar. Deliberately down at ~0.1% of the disc: at the 1.7% it
    started out at, the name said "pinpricks" while the data was substantial
    damage, and the ramp's lowest band never got exercised by anything."""
    for _ in range(n // 6000):
        _burst(c2, rng.randrange(n), rng.randrange(2, 12), 0.7, rng)


def _pat_c2only(q, c2, n, rng) -> None:
    """Audio errors with the subchannel intact — the common shape."""
    for _ in range(12):
        _burst(c2, rng.randrange(n), rng.randrange(200, 2000), 0.8, rng)


def _pat_qonly(q, c2, n, rng) -> None:
    """The measured speed cliff: raw-sub Q yield collapses above 24x while audio
    and C2 stay clean. The map has to make this visible — it is exactly the
    failure that silently drops pre-gaps from the TOC while everything else
    looks perfect."""
    for i in range(n):
        if i > n * 0.3 and rng.random() < 0.53:
            q[i] = St.ERR


def _pat_mixed(q, c2, n, rng) -> None:
    """Both lanes damaged independently, plus outer-edge decay."""
    for _ in range(6):
        _burst(c2, rng.randrange(n), rng.randrange(300, 3000), 0.8, rng)
    for _ in range(4):
        _burst(q, rng.randrange(n), rng.randrange(100, 900), 0.6, rng)
    for i in range(int(n * 0.88), n):
        if rng.random() < 0.25:
            c2[i] = St.ERR


# Registry, so `--patterns` and argparse both derive their list from one place.
PATTERNS = {
    "clean": _pat_clean,
    "scratch": _pat_scratch,
    "rot": _pat_rot,
    "sparse": _pat_sparse,
    "c2only": _pat_c2only,
    "qonly": _pat_qonly,
    "mixed": _pat_mixed,
}


def make_disc(pattern: str, sectors: int, tracks: int, seed: int) -> Disc:
    """Build a disc whose damage matches a named real-world failure mode."""
    rng = random.Random(seed)  # noqa: S311 - synthetic test data, not crypto
    q = [St.OK] * sectors
    c2 = [St.OK] * sectors
    PATTERNS[pattern](q, c2, sectors, rng)
    return Disc(sectors, _tracks(sectors, tracks, rng), q, c2)


# ── aggregation ───────────────────────────────────────────────────────────────
#
# A disc is ~350k sectors and the map is ~40 cells wide, so one cell stands for
# thousands of sectors. How that bucket collapses to one state is a real design
# decision, not an implementation detail, and all three arms are wrong in a
# different direction:
#
#   worst  any flagged sector reddens the whole cell. Right for a *fault* map —
#          a single bad sector must be findable — and useless as a health
#          indicator: it paints the entire `sparse` disc red.
#   ratio  majority wins. Right for health, and it erases exactly the isolated
#          damage a fault map exists to show: `sparse` renders solid green.
#   ramp   error *density* picks a shade. Both facts at once, at the cost of a
#          reader having to learn that dark red is not the same as bright red.


class Lane(NamedTuple):
    """One cell's worth of one lane. ``level`` is the ramp band, or -1 when the
    aggregation mode does not ramp (in which case the flat error colour is used
    and the distinction never reaches the palette)."""

    state: St
    level: int = -1


# Error densities span four orders of magnitude, so a LINEAR ramp is useless:
# at ~10,600 sectors per cell, isolated damage lands around 1e-4..1e-3 and a
# solid burst around 5e-1, which a linear scale renders as "nothing" and
# "everything" with no shades in between. The bands are therefore decade-wide.
# Boundaries are the fraction at which a cell moves UP a band.
_RAMP_BANDS = (1e-3, 1e-2, 1e-1)


def _band(frac: float) -> int:
    level = 0
    for edge in _RAMP_BANDS:
        if frac >= edge:
            level += 1
    return level


def bucket(lane: list[St], lo: int, hi: int, frontier: int, mode: str) -> Lane:
    if lo >= frontier:
        return Lane(St.UNREAD)
    hi = min(hi, frontier)
    window = lane[lo:hi]
    if not window:
        return Lane(St.UNREAD)
    errs = sum(1 for s in window if s is St.ERR)
    if mode == "ratio":
        return Lane(St.ERR if errs * 2 > len(window) else St.OK)
    if not errs:
        return Lane(St.OK)
    if mode == "ramp":
        return Lane(St.ERR, _band(errs / len(window)))
    return Lane(St.ERR)  # worst


# ── map renderers ─────────────────────────────────────────────────────────────
#
# Each returns one or more rendered rows. `dual` is the proposal: U+2580 UPPER
# HALF BLOCK paints its top half in the foreground colour and its bottom half in
# the background colour, so one row carries two independent lanes at full
# horizontal resolution.


def _lanes(disc: Disc, width: int, frontier: int, agg: str) -> list[tuple[Lane, Lane]]:
    per = max(1, disc.sectors // width)
    out = []
    for c in range(width):
        lo, hi = c * per, min((c + 1) * per, disc.sectors)
        out.append((
            bucket(disc.q, lo, hi, frontier, agg),
            bucket(disc.c2, lo, hi, frontier, agg),
        ))
    return out


def _col(lane: Lane, p: Palette) -> int:
    if lane.state is St.UNREAD:
        return p.unread
    if lane.state is St.OK:
        return p.ok
    return p.err if lane.level < 0 else p.err_ramp[lane.level]


def _worse(a: Lane, b: Lane) -> Lane:
    """Collapse two lanes to one for the single-lane renderers. UNREAD wins over
    everything — an unread cell is not a clean cell — and among read cells the
    worse state wins, then the higher ramp band."""
    if St.UNREAD in (a.state, b.state):
        return Lane(St.UNREAD)
    return max(a, b, key=lambda x: (x.state.value, x.level))


def render_dual(cells, p: Palette) -> list[str]:
    """One row, two lanes. Q on the upper half, C2 on the lower half."""
    out = []
    for qs, cs in cells:
        out.append(f"{fg(_col(qs, p))}{bg(_col(cs, p))}▀")
    return [f"{''.join(out)}{RESET}"]


def render_stacked(cells, p: Palette) -> list[str]:
    """Two rows of full blocks. Unambiguous, costs a line of vertical space."""
    q = "".join(f"{fg(_col(s, p))}█" for s, _ in cells)
    c = "".join(f"{fg(_col(s, p))}█" for _, s in cells)
    return [f"{q}{RESET}", f"{c}{RESET}"]


def render_single(cells, p: Palette) -> list[str]:
    """One row, one lane: the worse of Q and C2. Simplest, but a reader cannot
    tell a subchannel collapse from an audio error — which are different
    problems with different remedies."""
    out = "".join(f"{fg(_col(_worse(q, c), p))}█" for q, c in cells)
    return [f"{out}{RESET}"]


# Shape-encoded dual lane: the glyph itself says which lane failed, so the map
# survives a pipe, a log file, and a colourblind reader with no palette at all.
_GLYPH = {
    (St.OK, St.OK): "█",  # both good
    (St.OK, St.ERR): "▀",  # Q good, C2 bad — top half only
    (St.ERR, St.OK): "▄",  # C2 good, Q bad — bottom half only
    (St.ERR, St.ERR): "▒",  # both bad
}


def render_glyph(cells, p: Palette) -> list[str]:
    """Shape says which lane failed; colour (when there is any) says how badly.
    Under ``--palette mono`` the ramp is not representable — the glyph is already
    spending its shape on the lane split — so a mono map answers "where" and
    "which lane" but never "how much"."""
    out = []
    for qs, cs in cells:
        ch = "░" if St.UNREAD in (qs.state, cs.state) else _GLYPH[qs.state, cs.state]
        if p.name == "mono":
            out.append(ch)
        else:
            out.append(f"{fg(_col(_worse(qs, cs), p))}{ch}")
    return ["".join(out) + ("" if p.name == "mono" else RESET)]


RENDERERS = {
    "dual": render_dual,
    "stacked": render_stacked,
    "single": render_single,
    "glyph": render_glyph,
}


def render_ticks(disc: Disc, width: int, p: Palette) -> str:
    """A track-boundary ruler under the map — the per-track marker N2 wants back."""
    per = max(1, disc.sectors // width)
    row = [" "] * width
    for s in disc.track_starts:
        c = min(width - 1, s // per)
        row[c] = "╵"
    dim = "" if p.name == "mono" else fg(240)
    tail = "" if p.name == "mono" else RESET
    return f"{dim}{''.join(row)}{tail}"


# ── the whole line ────────────────────────────────────────────────────────────


def status_text(disc: Disc, sector: int, speed: int) -> str:
    """Left-hand status. Three phases, as kgr specified."""
    if sector < disc.sectors * 0.02:
        return "Grabbing track 1…"
    if sector < disc.sectors * 0.06:
        return f"Ripping disc at {speed}x…"
    return f"Ripping track {disc.track_at(sector):02d}…"


def compose(
    disc: Disc,
    sector: int,
    *,
    style: str,
    palette: Palette,
    agg: str,
    frame: int,
    speed: int,
    ticks: bool,
    cols: int,
) -> list[str]:
    spin = SPINNER[frame % len(SPINNER)]
    status = status_text(disc, sector, speed)
    pct = sector / disc.sectors * 100
    right = f"{pct:5.1f}% ({sector}/{disc.sectors})"

    # Fixed overhead: spinner + 2 spaces + status + 2 spaces + 2 spaces + right.
    overhead = 1 + 2 + len(status) + 2 + 2 + len(right)
    width = max(8, cols - overhead)

    cells = _lanes(disc, width, sector, agg)
    rows = RENDERERS[style](cells, palette)

    lines = [f"{spin}  {status}  {rows[0]}  {right}"]
    pad = " " * (1 + 2 + len(status) + 2)
    lines.extend(f"{pad}{r}" for r in rows[1:])
    if ticks:
        lines.append(f"{pad}{render_ticks(disc, width, palette)}")
    return lines


# ── modes ─────────────────────────────────────────────────────────────────────


def run_live(args, disc: Disc, palette: Palette) -> None:
    cols = shutil.get_terminal_size().columns - 1
    height = 0
    steps = int(args.duration * args.fps)
    try:
        for f in range(steps + 1):
            sector = min(disc.sectors, int(disc.sectors * f / steps))
            lines = compose(
                disc,
                sector,
                style=args.style,
                palette=palette,
                agg=args.aggregate,
                frame=f,
                speed=args.speed,
                ticks=args.ticks,
                cols=cols,
            )
            up = (
                f"\033[{height - 1}A\r\033[J"
                if height > 1
                else ("\r\033[J" if height else "")
            )
            sys.stdout.write(up + "\n".join(lines))
            sys.stdout.flush()
            height = len(lines)
            time.sleep(1 / args.fps)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(RESET + "\n")


def run_static(args, disc: Disc, palette: Palette) -> None:
    cols = shutil.get_terminal_size().columns - 1
    sector = int(disc.sectors * args.at / 100)
    lines = compose(
        disc,
        sector,
        style=args.style,
        palette=palette,
        agg=args.aggregate,
        frame=2,
        speed=args.speed,
        ticks=args.ticks,
        cols=cols,
    )
    print("\n".join(lines))


def run_gallery(args) -> None:
    """Every style x palette on one screen at a fixed frame — the comparison
    this tool exists to make. Same disc and same frontier throughout, so any
    difference on screen is the design and not the data."""
    cols = shutil.get_terminal_size().columns - 1
    print("\n\033[1mQ + C2 map — style gallery\033[0m")
    print(
        f"pattern={args.pattern}  aggregate={args.aggregate}  at={args.at}%  "
        f"sectors={args.sectors}\n"
    )

    for pname, palette in PALETTES.items():
        print(f"\033[1m── palette: {pname}\033[0m  \033[2m({palette.note})\033[0m")
        for style in RENDERERS:
            if pname == "mono" and style != "glyph":
                continue  # only `glyph` carries the signal without colour
            disc = make_disc(args.pattern, args.sectors, args.tracks, args.seed)
            sector = int(disc.sectors * args.at / 100)
            lines = compose(
                disc,
                sector,
                style=style,
                palette=palette,
                agg=args.aggregate,
                frame=2,
                speed=args.speed,
                ticks=args.ticks,
                cols=cols,
            )
            print(f"  \033[2m{style:<8}\033[0m")
            for ln in lines:
                print(f"  {ln}")
            print()

    print(
        "\033[2mLegend — dual/stacked/single: colour is state. "
        "glyph: █ both OK  ▀ C2 bad  ▄ Q bad  ▒ both bad  ░ unread.\033[0m"
    )
    if args.aggregate == "ramp":
        p = PALETTES[args.palette if args.palette != "mono" else "classic"]
        swatch = "".join(f"{fg(c)}█" for c in p.err_ramp)
        print(
            f"\033[2mramp bands (faint→bright, decade-wide): \033[0m{swatch}{RESET}"
            f"\033[2m  <0.1%  <1%  <10%  ≥10% of the cell's sectors flagged.\033[0m"
        )
    print()


def run_aggregates(args) -> None:
    """The three aggregation arms on one pattern — the comparison that decides
    which one ships. Run it against `--pattern sparse`, where they disagree most:
    `worst` flags 34 of 60 cells at full intensity, `ratio` flags none at all,
    and `ramp` flags the same 34 in its two faintest bands."""
    cols = shutil.get_terminal_size().columns - 1
    palette = PALETTES[args.palette]
    print(
        f"\n\033[1mQ + C2 map — aggregation sweep\033[0m  "
        f"\033[2m(pattern={args.pattern} style={args.style} "
        f"palette={args.palette})\033[0m\n"
    )
    for mode in ("worst", "ratio", "ramp"):
        disc = make_disc(args.pattern, args.sectors, args.tracks, args.seed)
        sector = int(disc.sectors * args.at / 100)
        lines = compose(
            disc,
            sector,
            style=args.style,
            palette=palette,
            agg=mode,
            frame=2,
            speed=args.speed,
            ticks=args.ticks,
            cols=cols,
        )
        print(f"  \033[2m{mode:<6}\033[0m")
        for ln in lines:
            print(f"  {ln}")
        print()


def run_patterns(args) -> None:
    """Every damage pattern in one style — checks the map reads correctly against
    each real-world failure mode, not just the one that was handy."""
    cols = shutil.get_terminal_size().columns - 1
    palette = PALETTES[args.palette]
    print(
        f"\n\033[1mQ + C2 map — pattern sweep\033[0m  "
        f"\033[2m(style={args.style} palette={args.palette})\033[0m\n"
    )
    for pat in PATTERNS:
        disc = make_disc(pat, args.sectors, args.tracks, args.seed)
        sector = int(disc.sectors * args.at / 100)
        lines = compose(
            disc,
            sector,
            style=args.style,
            palette=palette,
            agg=args.aggregate,
            frame=2,
            speed=args.speed,
            ticks=args.ticks,
            cols=cols,
        )
        print(f"  \033[2m{pat:<8}\033[0m")
        for ln in lines:
            print(f"  {ln}")
        print()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Aesthetic bench for the N2 Q + C2 progress map.",
    )
    ap.add_argument("--style", choices=list(RENDERERS), default="dual")
    ap.add_argument("--palette", choices=list(PALETTES), default="classic")
    ap.add_argument(
        "--pattern",
        choices=list(PATTERNS),
        default="mixed",
    )
    ap.add_argument(
        "--aggregate",
        choices=("worst", "ratio", "ramp"),
        default="worst",
        help=(
            "how thousands of sectors collapse into one cell: worst=any flagged "
            "sector reddens the cell, ratio=majority, ramp=shade by error "
            "density over decade-wide bands (default: worst)"
        ),
    )
    ap.add_argument("--sectors", type=int, default=350000)
    ap.add_argument("--tracks", type=int, default=12)
    ap.add_argument("--speed", type=int, default=24)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--ticks", action="store_true", help="show track-boundary ruler")
    ap.add_argument("--at", type=float, default=100.0, help="static frame, %% done")
    ap.add_argument("--fps", type=float, default=20.0)
    ap.add_argument("--duration", type=float, default=8.0, help="seconds per run")
    ap.add_argument("--gallery", action="store_true", help="every style x palette")
    ap.add_argument("--patterns", action="store_true", help="every damage pattern")
    ap.add_argument(
        "--aggregates",
        action="store_true",
        help="worst vs ratio vs ramp on one pattern (try --pattern sparse)",
    )
    ap.add_argument("--live", action="store_true", help="animate instead of one frame")
    args = ap.parse_args()

    if args.gallery:
        run_gallery(args)
        return
    if args.patterns:
        run_patterns(args)
        return
    if args.aggregates:
        run_aggregates(args)
        return

    disc = make_disc(args.pattern, args.sectors, args.tracks, args.seed)
    palette = PALETTES[args.palette]
    if args.live:
        run_live(args, disc, palette)
    else:
        run_static(args, disc, palette)


if __name__ == "__main__":
    main()
