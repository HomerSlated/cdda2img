"""The disc map — the rip progress bar rendered as a per-sector damage map.

The bar does not *carry* a map, it **is** one. Each cell stands for a fixed span
of sectors and reports what the read found there, so a rip's progress and its
damage are one widget rather than a bar plus a report at the end.

Two lanes are planned: C2 (audio error pointers) and Q (subchannel CRC). Only
C2 exists today — see the module note under :func:`render` for why the Q lane
cannot be computed on this side and what the renderer does until it arrives.

The aesthetic was settled on synthetic data in ``tools/progress_lab.py`` (kgr,
2026-08-07): glyph shapes, the colourblind-safe palette, decade-wide severity
bands. That bench is now **frozen** — it is the record of the experiment, not a
library — and this module is the production implementation of what it settled.
It deliberately does not import from it, and vice versa.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import NamedTuple

# ── states ────────────────────────────────────────────────────────────────────

UNREAD = 0
OK = 1
ERR = 2


class Cell(NamedTuple):
    """One column of the map: a state, and (for ERR) a severity band."""

    state: int
    level: int = -1


# Error densities span four orders of magnitude, so a LINEAR ramp is useless: at
# ~10,000 sectors per cell, isolated damage lands around 1e-4..1e-3 and a solid
# burst around 5e-1, which a linear scale renders as "nothing" and "everything"
# with no shades between. The bands are therefore decade-wide. Each boundary is
# the flagged fraction at which a cell moves UP a band.
#
# **C2 calibration.** The healthy baseline is exactly ZERO — a clean disc flags no
# sectors at all — so any non-zero density is worth colouring and the faintest
# band starts at 0.1%.
RAMP_BANDS = (1e-3, 1e-2, 1e-1)

# **Q calibration, and it cannot be the C2 one.** The subchannel's healthy
# baseline is NOT zero: CRC-bad Q frames are ordinary. Measured across two discs
# and five speeds each (RECOVERY.md §12.2.2, §12.3, and the 42-capture qlag
# sweep):
#
#     ZZ Top    0.081% to 0.204% bad  (Q-ok 0.99919 to 0.99796)
#     Tracy     ~2%                     (Q-ok 0.980 at every speed, flat)
#     ABBA 4x   3.5%                    (Q-ok 0.965, healthy for that disc)
#
# Against the C2 bands that healthy 2% lands in band 2 of 4, so Tracy's normal
# read paints EVERY cell orange — a clean disc drawn as uniformly damaged, and
# with 2% and 20% saturating the same band the map could not tell them apart.
# That shipped, and kgr's screenshot is what it looks like.
#
# The three measured collapses are 38.7%, 47.8% and 52% bad. So there is an
# order of magnitude of empty space between "normal for some disc" and "this
# read lost the subchannel", and the bands are placed in it: healthy stays band
# 0 for any disc on the shelf, and a real collapse still reaches the top.
#
# The 5% floor is a **claim about discs, not about this drive**: a pressing whose
# ordinary Q rate exceeds it would read as damaged. None of the three measured
# does, ABBA's 3.5% being the worst, and no counter-example has been observed —
# but it is a floor set from three discs and should move if a fourth contradicts
# it, rather than being defended.
SUBQ_RAMP_BANDS = (5e-2, 1.5e-1, 3.5e-1)


def band(frac: float, bands: tuple[float, ...] = RAMP_BANDS) -> int:
    """Which severity band a flagged fraction falls in (0 = faintest).

    *bands* selects the calibration: :data:`RAMP_BANDS` for C2 (healthy = 0),
    :data:`SUBQ_RAMP_BANDS` for the Q lane (healthy = up to a few per cent).
    Sharing one table across both lanes is what made a clean disc look damaged.
    """
    level = 0
    for edge in bands:
        if frac >= edge:
            level += 1
    return level


# ── palette ───────────────────────────────────────────────────────────────────
#
# 256-colour SGR rather than truecolour: every terminal that can draw half-blocks
# can do 256, and palette indices survive a theme change better than hex.


@dataclass(frozen=True)
class Palette:
    unread: int
    ok: int
    # Four shades of ONE hue, faintest first. The ramp encodes severity, and a
    # ramp that drifts across hues reads as a change of kind, not of degree.
    err_ramp: tuple[int, int, int, int]


# Blue/orange, not green/red: red/green dichromacy affects ~8% of men, and this
# pair also stays distinct in greyscale because the hues differ in luminance.
CB = Palette(unread=236, ok=33, err_ramp=(94, 136, 172, 214))

RESET = "\033[0m"

# Shape carries the signal when colour does not. A damaged cell is a different
# CHARACTER from a healthy one, so the map still reads over a pipe, in a log
# file, and under NO_COLOR. What mono cannot carry is the ramp — severity lives
# in colour alone — so a mono map answers "where" but never "how much".
_READ_OK = "█"
_READ_ERR = "▒"
_UNREAD = "░"

#: U+2580 UPPER HALF BLOCK. Top half takes the foreground colour, bottom half
#: the background — two independent lanes on one text row at full horizontal
#: resolution, and the reason this map is one line rather than two.
_UPPER_HALF = "▀"

# Two lanes on one row. U+2580/U+2584 draw a half-block each, so the glyph says
# WHICH lane is intact: top half = Q, bottom half = C2, **filled means healthy**.
# A full block is both good and the shaded block is neither — deliberately not
# blank, because blank is unread and "no damage found" must never look like "not
# looked at". Settled on the bench, kgr 2026-08-07.
_GLYPH = {
    (OK, OK): "█",  # both lanes intact
    (OK, ERR): "▀",  # Q intact, C2 damaged — only the top half survives
    (ERR, OK): "▄",  # C2 intact, Q damaged
    (ERR, ERR): "▒",  # nothing here survived
}


def colour_enabled(stream: object = None) -> bool:
    """Whether to emit SGR colour, per the NO_COLOR convention.

    Two things matter and are easy to get wrong. **NO_COLOR is an application-
    side convention** (https://no-color.org): nothing strips the codes for us, so
    an application that does not check the variable simply ignores it. And the
    rule is *presence*, not truthiness — any non-empty value disables colour, so
    ``NO_COLOR=0`` means no colour. Testing the value would invert the one case
    a user is most likely to try.

    The stream tested is **stdout**, which is where the escapes go. The TUI as a
    whole is gated on ``sys.stdin.isatty()`` elsewhere, which is a separate
    pre-existing bug (``cdda2img rip … > log.txt`` from a terminal writes cursor
    motion into the log); this predicate deliberately does not inherit it.
    """
    if os.environ.get("NO_COLOR"):
        return False
    stream = sys.stdout if stream is None else stream
    return bool(getattr(stream, "isatty", lambda: False)())


# ── bucketing ─────────────────────────────────────────────────────────────────


def cells_from_damage(
    damage: bytes | bytearray,
    frontier: int,
    width: int,
    *,
    bands: tuple[float, ...] = RAMP_BANDS,
) -> list[Cell]:
    """Bucket a per-sector damage map into *width* cells.

    *damage* is one byte per sector, non-zero meaning "this sector is not intact
    in this lane". *frontier* is how many sectors have been read: everything at
    or past it is UNREAD, which is **not** the same as clean and must never
    render as it.

    *bands* is the severity calibration and **must match the lane**:
    :data:`RAMP_BANDS` for C2, :data:`SUBQ_RAMP_BANDS` for Q. The two lanes have
    different healthy baselines — zero versus a few per cent — so one table
    cannot serve both without drawing one of them wrong.

    ``per`` is derived from *width*, so a caller that changes width mid-read
    re-buckets every cell and the map appears to rewrite its own history. Pin the
    width for the life of the read; that bug was found on the bench and the fix
    is the caller's, not this function's.
    """
    total = len(damage)
    if width <= 0 or total <= 0:
        return []
    per = max(1, total // width)
    out: list[Cell] = []
    for c in range(width):
        lo = c * per
        # The last cell absorbs the remainder of the integer division. Without
        # this, up to width-1 sectors at the END of the disc belong to no cell
        # and their damage is invisible — and the outer edge is where damage
        # concentrates, so the dropped sectors are the ones most worth drawing.
        hi = total if c == width - 1 else min((c + 1) * per, total)
        if lo >= frontier or hi <= lo:
            out.append(Cell(UNREAD))
            continue
        hi = min(hi, frontier)
        # Count ZEROS and subtract, rather than counting the writer's chosen
        # marker byte: "damaged" is *any* non-zero value, and counting 1s would
        # silently read a severity byte or a bitmask as a clean disc. The clean
        # reading is the dangerous direction for this map, so it must not be the
        # one an unexpected value falls into.
        #
        # bytearray.count(int, start, end) is one C-level pass over a slice that
        # is never materialised — the Python-level `any(...)` alternative is
        # ~10,000 interpreter steps per cell per frame.
        errs = (hi - lo) - damage.count(0, lo, hi)
        if not errs:
            out.append(Cell(OK))
        else:
            out.append(Cell(ERR, band(errs / (hi - lo), bands)))
    return out


# ── rendering ─────────────────────────────────────────────────────────────────


def _colour_of(cell: Cell, pal: Palette) -> int:
    if cell.state == UNREAD:
        return pal.unread
    if cell.state == OK:
        return pal.ok
    return pal.err_ramp[cell.level] if cell.level >= 0 else pal.err_ramp[-1]


def _worse(a: Cell, b: Cell) -> Cell:
    """The cell whose colour the pair should take.

    ``UNREAD`` wins over everything — an unread cell is not a clean cell — and
    among read cells the worse state wins, then the higher severity band. Colour
    carries severity, and severity is a property of the pair rather than of one
    lane, because the glyph has already spent its shape saying *which* lane.
    """
    if UNREAD in (a.state, b.state):
        return Cell(UNREAD)
    return max(a, b, key=lambda c: (c.state, c.level))


def render(
    cells: list[Cell],
    *,
    colour: bool = True,
    pal: Palette = CB,
    q_cells: list[Cell] | None = None,
) -> str:
    """One row of map, ready to drop into a progress line.

    *cells* is the C2 lane. *q_cells*, when given, is the Q-subchannel lane.
    Without it the row is C2 alone — the honest rendering on a binding with no
    ``subq_map``, since **drawing Q as healthy would assert something never
    measured**, and computing that lane here is not an option (a hard-unreadable
    sector arrives zero-filled and a zero Q frame *fails* CRC, so a DIY lane
    fabricates subchannel damage exactly where the audio is already gone).

    **Two lanes are drawn in colour, not in shape, whenever there is colour.**
    ``▀`` paints its top half in the foreground and its bottom half in the
    *background*, so one cell carries Q above C2 with both halves inked and
    neither able to look unrendered.

    That last point is the whole reason this is not the bench's `glyph` style.
    Glyph encodes a failing lane as an *unfilled half* — and on a disc where one
    lane is broadly unhealthy, which is precisely when the map matters, half of
    every cell falls back to the terminal background. Measured on a real 40x rip
    where the PX-716A's Q yield collapses: the map correctly reported a
    disc-wide Q failure, and it read as the top half of the bar having failed to
    draw. A map that is right and looks broken is worse than one that says less.

    Mono keeps the glyph shapes, because there the shape is the only channel
    there is.

    SGR is emitted only on a *change*, so a clean disc costs a couple of escapes
    for the whole row rather than two per cell.
    """
    if not cells:
        return ""
    if colour and q_cells is not None:
        return _render_dual(cells, q_cells, pal)
    parts: list[str] = []
    last = None
    for i, cell in enumerate(cells):
        q = q_cells[i] if q_cells is not None and i < len(q_cells) else None
        if q is None:
            shown, ch = cell, _one_lane_glyph(cell)
        else:
            shown = _worse(q, cell)
            ch = _UNREAD if shown.state == UNREAD else _GLYPH[q.state, cell.state]
        if colour:
            col = _colour_of(shown, pal)
            if col != last:
                parts.append(f"\033[38;5;{col}m")
                last = col
        parts.append(ch)
    if colour:
        parts.append(RESET)
    return "".join(parts)


def _render_dual(cells: list[Cell], q_cells: list[Cell], pal: Palette) -> str:
    """Q above C2, both inked: ``▀`` top half = foreground, bottom half = background.

    Every cell is the same character, so the lanes are distinguished purely by
    colour and nothing can be mistaken for a gap in the drawing.
    """
    parts: list[str] = []
    last_fg = last_bg = None
    for i, c2 in enumerate(cells):
        q = q_cells[i] if i < len(q_cells) else Cell(UNREAD)
        fg_col = _colour_of(q, pal)
        bg_col = _colour_of(c2, pal)
        if fg_col != last_fg:
            parts.append(f"\033[38;5;{fg_col}m")
            last_fg = fg_col
        if bg_col != last_bg:
            parts.append(f"\033[48;5;{bg_col}m")
            last_bg = bg_col
        parts.append(_UPPER_HALF)
    parts.append(RESET)
    return "".join(parts)


def _one_lane_glyph(cell: Cell) -> str:
    if cell.state == UNREAD:
        return _UNREAD
    return _READ_OK if cell.state == OK else _READ_ERR
