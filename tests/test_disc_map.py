"""The disc map renderer — bucketing, severity bands, colour degradation.

The map's failure modes are all *plausible-looking wrong pictures*, so most of
these tests assert something is NOT drawn rather than that something is.
"""

from __future__ import annotations

import io

import pytest

from cdda2img import disc_map
from cdda2img.disc_map import ERR, OK, UNREAD, Cell

# ── severity bands ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("frac", "expected"),
    [
        (0.0, 0),
        (9.9e-4, 0),
        (1e-3, 1),
        (9.9e-3, 1),
        (1e-2, 2),
        (9.9e-2, 2),
        (1e-1, 3),
        (1.0, 3),
    ],
)
def test_bands_are_decade_wide_and_inclusive_at_the_edge(
    frac: float, expected: int
) -> None:
    """A cell moves up a band AT the boundary, not past it.

    The bands exist because error density spans four orders of magnitude: a
    linear ramp renders isolated damage (~1e-4) and a solid burst (~5e-1) as
    "nothing" and "everything" with no shades between.
    """
    assert disc_map.band(frac) == expected


# ── bucketing ─────────────────────────────────────────────────────────────────


def test_nothing_read_is_never_drawn_as_clean() -> None:
    """The whole point of the frontier.

    An unread cell and a clean cell are both "no damage found", and rendering
    them alike would report a pristine disc for a read that has not happened.
    """
    cells = disc_map.cells_from_damage(bytearray(1000), frontier=0, width=10)
    assert [c.state for c in cells] == [UNREAD] * 10


def test_the_frontier_splits_read_from_unread() -> None:
    cells = disc_map.cells_from_damage(bytearray(1000), frontier=500, width=10)
    assert [c.state for c in cells] == [OK] * 5 + [UNREAD] * 5


def test_a_partially_read_cell_is_judged_on_what_was_read() -> None:
    """A cell straddling the frontier must not average in its unread half —
    that would dilute real damage towards clean exactly at the live edge."""
    damage = bytearray(1000)
    damage[500:550] = b"\x01" * 50  # all 50 read sectors of cell 5 are damaged
    cells = disc_map.cells_from_damage(damage, frontier=550, width=10)
    assert cells[5] == Cell(ERR, 3)  # 50/50 = 100%, top band — not 50/100


def test_damage_density_selects_the_band() -> None:
    damage = bytearray(1000)
    damage[0] = 1  # 1/100 of cell 0 = 1e-2 → band 2
    damage[100:105] = b"\x01" * 5  # 5/100 of cell 1 = 5e-2 → band 2
    damage[200:230] = b"\x01" * 30  # 30/100 of cell 2 = 3e-1 → band 3
    cells = disc_map.cells_from_damage(damage, frontier=1000, width=10)
    assert [cells[i].level for i in range(3)] == [2, 2, 3]
    assert cells[3] == Cell(OK)


def test_any_nonzero_byte_counts_as_damage() -> None:
    """The seam writes 1, but the map must not depend on the exact value —
    a future writer using a severity byte would otherwise silently read clean."""
    damage = bytearray(100)
    damage[0] = 0xFF
    cells = disc_map.cells_from_damage(damage, frontier=100, width=10)
    assert cells[0].state == ERR


def test_an_empty_or_zero_width_map_renders_nothing_rather_than_dividing_by_zero() -> (
    None
):
    assert disc_map.cells_from_damage(bytearray(0), frontier=0, width=10) == []
    assert disc_map.cells_from_damage(bytearray(100), frontier=0, width=0) == []


def test_a_settled_cell_never_changes_as_the_frontier_advances() -> None:
    """The bug the bench found, as a regression test.

    A cell entirely behind the frontier is final. If it can still change, the
    map rewrites its own history and a user watching it cannot trust what they
    already saw. Width is fixed here — varying it IS the way to break this, and
    that is the caller's contract (see ``_build_map``).
    """
    damage = bytearray(1000)
    damage[137] = 1
    damage[642] = 1
    settled: dict[int, Cell] = {}
    for frontier in [*range(0, 1000, 7), 1000]:
        cells = disc_map.cells_from_damage(damage, frontier, width=25)
        per = 1000 // 25
        for i, cell in enumerate(cells):
            if (i + 1) * per > frontier:
                continue  # still live, allowed to move
            assert settled.setdefault(i, cell) == cell, f"cell {i} changed"
    assert len(settled) == 25


# ── colour ────────────────────────────────────────────────────────────────────


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_no_color_is_tested_for_presence_not_truthiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``NO_COLOR=0`` means NO colour (https://no-color.org).

    Reading it as a boolean inverts the one value a user is most likely to try,
    and the mistake is invisible to anyone who only ever tests ``NO_COLOR=1``.
    """
    monkeypatch.setenv("NO_COLOR", "0")
    assert disc_map.colour_enabled(_Tty()) is False


def test_no_color_empty_string_does_not_disable_colour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The convention exempts the empty value — it is how a user unsets it in a
    shell that cannot easily remove a variable."""
    monkeypatch.setenv("NO_COLOR", "")
    assert disc_map.colour_enabled(_Tty()) is True


def test_a_non_tty_gets_the_shape_encoded_map(monkeypatch: pytest.MonkeyPatch) -> None:
    """Piping the map into a file should put the map in the file, not a stream
    of escape sequences."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert disc_map.colour_enabled(io.StringIO()) is False


# ── rendering ─────────────────────────────────────────────────────────────────


def test_mono_still_distinguishes_all_three_states() -> None:
    """Colour degradation must not collapse the map to one repeated character.

    This is why damage is a different GLYPH and not merely a different colour:
    under NO_COLOR the map still answers "where", though never "how much".
    """
    cells = [Cell(UNREAD), Cell(OK), Cell(ERR, 0), Cell(ERR, 3)]
    assert disc_map.render(cells, colour=False) == "░█▒▒"


def test_colour_is_emitted_only_when_it_changes() -> None:
    """A clean disc is one run of one colour: two escapes for the whole row,
    not one per cell. At 10 fps over a 12-minute rip the difference is real."""
    out = disc_map.render([Cell(OK)] * 50, colour=True)
    assert out.count("\033[38;5;") == 1
    assert out.endswith(disc_map.RESET)


def test_the_severity_ramp_stays_within_one_hue() -> None:
    """Each band gets its own shade, and they are distinct — a ramp that reused
    a shade would report two decades of damage as the same picture."""
    shades = {disc_map._colour_of(Cell(ERR, lvl), disc_map.CB) for lvl in range(4)}
    assert len(shades) == 4
    assert disc_map.CB.ok not in shades
    assert disc_map.CB.unread not in shades


def test_rendering_no_cells_is_empty_not_a_stray_reset() -> None:
    assert disc_map.render([], colour=True) == ""


def test_the_last_cell_absorbs_the_division_remainder() -> None:
    """Sectors past ``width * (total // width)`` must still be mapped.

    204143 sectors over 47 cells leaves 22 sectors belonging to no cell. They
    are the LAST 22 on the disc — the outer edge, where damage concentrates —
    so dropping them hides exactly the damage most worth seeing.
    """
    total, width = 204143, 47
    damage = bytearray(total)
    damage[-1] = 1
    cells = disc_map.cells_from_damage(damage, frontier=total, width=width)
    assert cells[-1].state == ERR


# ── two lanes ─────────────────────────────────────────────────────────────────


def test_the_glyph_says_which_lane_survived() -> None:
    """Top half = Q, bottom half = C2, FILLED means healthy. A full block is
    both intact; the shaded block is neither — deliberately not blank, because
    blank is unread and "no damage found" must never look like "not looked at"."""
    c2 = [Cell(OK), Cell(ERR, 1), Cell(OK), Cell(ERR, 1)]
    q = [Cell(OK), Cell(OK), Cell(ERR, 1), Cell(ERR, 1)]
    assert disc_map.render(c2, colour=False, q_cells=q) == "█▀▄▒"


def test_an_unread_cell_stays_unread_in_either_lane() -> None:
    """A cell only half-read is not half-clean. UNREAD wins over everything,
    which is why `_worse` short-circuits on it rather than ordering by state."""
    assert disc_map.render([Cell(UNREAD)], colour=False, q_cells=[Cell(OK)]) == "░"
    assert disc_map.render([Cell(OK)], colour=False, q_cells=[Cell(UNREAD)]) == "░"


def test_without_a_q_lane_the_map_stays_single_lane() -> None:
    """Drawing Q as healthy would assert something never measured — the map says
    less rather than saying something false."""
    cells = [Cell(OK), Cell(ERR, 2), Cell(UNREAD)]
    assert disc_map.render(cells, colour=False) == "█▒░"
    assert disc_map.render(cells, colour=False, q_cells=None) == "█▒░"


def test_colour_takes_the_worse_lane_because_shape_already_spent_itself() -> None:
    """The glyph has said WHICH lane failed, so colour is free to say how badly —
    and severity is a property of the pair, not of one lane."""
    hot = disc_map._worse(Cell(OK), Cell(ERR, 3))
    assert hot == Cell(ERR, 3)
    assert disc_map._worse(Cell(ERR, 0), Cell(ERR, 2)) == Cell(ERR, 2)


# ── two lanes in colour: both halves inked ────────────────────────────────────


def test_a_colour_two_lane_row_never_leaves_a_half_uninked() -> None:
    """The bug kgr saw on a real 40x rip.

    The PX-716A's Q yield collapses above ~32x, so the map correctly reported a
    disc-wide Q failure — and drew it as `▄`, whose top half falls back to the
    terminal background. It read as "the upper line of the bar failed to
    render". A map that is right and looks broken is worse than one that says
    less, so in colour BOTH halves carry a palette colour.
    """
    c2 = [Cell(OK)] * 4
    q = [Cell(ERR, 3)] * 4  # a total Q collapse: the case that looked unrendered
    out = disc_map.render(c2, colour=True, q_cells=q)
    assert out.count("\033[48;5;") >= 1, "no background = an uninked half"
    assert "\033[38;5;" in out
    assert out.count(disc_map._UPPER_HALF) == 4


def test_the_two_lanes_get_their_own_colours_the_right_way_up() -> None:
    """Top half is Q (foreground), bottom half is C2 (background). Swapping them
    would be invisible on a disc where both lanes agree, and exactly wrong on the
    Q-collapse disc this exists for."""
    out = disc_map.render([Cell(OK)], colour=True, q_cells=[Cell(ERR, 3)])
    assert f"\033[38;5;{disc_map.CB.err_ramp[3]}m" in out  # Q damaged -> fg
    assert f"\033[48;5;{disc_map.CB.ok}m" in out  # C2 healthy -> bg


def test_mono_keeps_the_glyph_shapes_because_shape_is_all_it_has() -> None:
    c2 = [Cell(OK), Cell(ERR, 1), Cell(OK), Cell(ERR, 1)]
    q = [Cell(OK), Cell(OK), Cell(ERR, 1), Cell(ERR, 1)]
    assert disc_map.render(c2, colour=False, q_cells=q) == "█▀▄▒"


def test_colour_runs_are_still_collapsed_in_the_dual_row() -> None:
    """A clean disc must not cost two escapes per cell at 10 fps."""
    out = disc_map.render([Cell(OK)] * 60, colour=True, q_cells=[Cell(OK)] * 60)
    assert out.count("\033[38;5;") == 1
    assert out.count("\033[48;5;") == 1


# ── the two lanes need two calibrations ───────────────────────────────────────


@pytest.mark.parametrize(
    ("bad_rate", "disc"),
    [
        (0.00081, "ZZ Top best"),
        (0.00204, "ZZ Top worst"),
        (0.020, "Tracy, every speed"),
        (0.035, "ABBA at 4x"),
    ],
)
def test_a_healthy_q_rate_is_not_drawn_as_damage(bad_rate: float, disc: str) -> None:
    """Every measured HEALTHY Q rate must land in the faintest band.

    These are real figures (RECOVERY.md §12.2.2, §12.3): CRC-bad Q frames are
    ordinary, so the lane's healthy baseline is a few per cent rather than zero.
    Against the C2 bands, Tracy's normal 2% reached band 2 of 4 and painted the
    whole disc orange — a clean read drawn as uniformly damaged.
    """
    assert disc_map.band(bad_rate, disc_map.SUBQ_RAMP_BANDS) == 0, disc


@pytest.mark.parametrize("bad_rate", [0.387, 0.478, 0.52])
def test_a_real_q_collapse_still_reaches_the_top_band(bad_rate: float) -> None:
    """The three measured collapses. Quietening the healthy case is only worth
    doing if the case the lane exists for still stands out."""
    assert disc_map.band(bad_rate, disc_map.SUBQ_RAMP_BANDS) == 3


def test_the_q_bands_do_not_quieten_the_c2_lane() -> None:
    """C2's healthy baseline IS zero, so a single flagged sector in a cell is
    worth colouring. Applying the Q calibration to C2 would hide it."""
    one_in_ten_thousand = 1e-4
    assert disc_map.band(one_in_ten_thousand, disc_map.RAMP_BANDS) == 0
    assert disc_map.band(0.002, disc_map.RAMP_BANDS) == 1  # C2: worth showing
    assert disc_map.band(0.002, disc_map.SUBQ_RAMP_BANDS) == 0  # Q: ordinary


def test_bucketing_honours_the_band_table_it_is_given() -> None:
    """The parameter has to reach the cells, or the calibration is decorative."""
    damage = bytearray(1000)
    damage[:20] = b"\x01" * 20  # 2% — Tracy's healthy Q rate
    c2 = disc_map.cells_from_damage(damage, 1000, 1, bands=disc_map.RAMP_BANDS)
    q = disc_map.cells_from_damage(damage, 1000, 1, bands=disc_map.SUBQ_RAMP_BANDS)
    assert c2[0].level == 2, "C2 calibration should call 2% notable"
    assert q[0].level == 0, "Q calibration should call 2% ordinary"


def test_the_default_calibration_is_c2() -> None:
    """C2 is the lane that has always been there and the one whose callers
    predate the parameter; a silent default change would be the bug again."""
    damage = bytearray(1000)
    damage[:20] = b"\x01" * 20
    assert disc_map.cells_from_damage(damage, 1000, 1)[0].level == 2
