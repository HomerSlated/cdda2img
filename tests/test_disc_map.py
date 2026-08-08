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
