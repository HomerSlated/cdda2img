"""The legend must not drift from the module it describes.

A legend is a promise about what the user is looking at. If the palette or the
band edges move and the document does not, the promise silently becomes false —
and unlike a stale comment, nobody reading the map can tell.

These tests read the shipped values and assert the document quotes them. They
deliberately do NOT re-derive the numbers, because a test that recomputes what
the code computes agrees with the code by construction and checks nothing.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from cdda2img import disc_map

_LEGEND = pathlib.Path(__file__).resolve().parents[1] / (
    "docs/reference/progress-map-legend.md"
)


@pytest.fixture(scope="module")
def legend() -> str:
    if not _LEGEND.is_file():
        pytest.fail(f"the legend is missing: {_LEGEND}")
    return _LEGEND.read_text(encoding="utf-8")


def _xterm_hex(i: int) -> str:
    """xterm-256 index to #RRGGBB, for the 6x6x6 cube and the grey ramp."""
    if 16 <= i < 232:
        i -= 16
        levels = (0, 95, 135, 175, 215, 255)
        r, g, b = levels[i // 36], levels[(i % 36) // 6], levels[i % 6]
    elif i >= 232:
        r = g = b = 8 + (i - 232) * 10
    else:  # pragma: no cover - the palette uses none of the low 16
        raise ValueError(i)
    return f"#{r:02X}{g:02X}{b:02X}"


def _band_table_rows(legend: str) -> list[str]:
    """The rows of the two band tables: ``| <level> | <range> | `#RRGGBB` |``.

    Scoping matters. These rows are where the document makes its promise about a
    band edge; every other mention of a percentage on the page is narrative, and
    a search over the whole file matches narrative just as happily.
    """
    return [
        ln
        for ln in legend.splitlines()
        if re.match(r"^\|\s*[0-3]\s*\|.*\|\s*`#[0-9A-Fa-f]{6}`\s*\|\s*$", ln)
    ]


def test_every_palette_colour_appears_with_its_index_and_hex(legend: str) -> None:
    """Both are load-bearing: the index is what the code emits, the hex is what
    a reader matches against their screen."""
    palette = {
        "unread": disc_map.CB.unread,
        "ok": disc_map.CB.ok,
        **{f"band {i}": c for i, c in enumerate(disc_map.CB.err_ramp)},
    }
    for role, idx in palette.items():
        assert f"| {idx} |" in legend, f"{role}: index {idx} not documented"
        assert _xterm_hex(idx) in legend, (
            f"{role}: hex {_xterm_hex(idx)} not documented"
        )


def test_the_palette_has_no_undocumented_colours(legend: str) -> None:
    """The complement of the test above, and the one that catches an ADDITION.

    Checking only that known colours appear would pass forever after a fifth
    ramp shade was added and left out of the legend.
    """
    used = {disc_map.CB.unread, disc_map.CB.ok, *disc_map.CB.err_ramp}
    assert len(disc_map.CB.err_ramp) == 4, (
        "the ramp changed size; the legend's band tables assume four shades"
    )
    documented = {i for i in range(256) if f"| {i} |" in legend}
    assert used <= documented, f"undocumented: {sorted(used - documented)}"


@pytest.mark.parametrize(
    ("bands", "name"),
    [(disc_map.RAMP_BANDS, "C2"), (disc_map.SUBQ_RAMP_BANDS, "Q")],
)
def test_every_band_edge_is_quoted(
    legend: str, bands: tuple[float, ...], name: str
) -> None:
    """The edges are the whole content of the calibration.

    Formatted the way a human writes them — 0.1%, 5%, 35% — because that is what
    the document says, and a legend quoting `0.05` would be correct and useless.

    **Searching the whole document does not work, and passed while checking
    nothing.** Two ways it succeeded by coincidence: ``"5%" in legend`` is true
    because of ``15%`` and ``35%``, and even with a digit boundary, an edge moved
    to 20% would match the prose sentence "2% and 20% saturated the same band".
    Every band edge is a short numeric string and this page is full of them.

    So the search is scoped to the **band-table rows** — the lines that actually
    make the promise — and given a boundary so ``5%`` cannot match ``3.5%``.
    """
    rows = "\n".join(_band_table_rows(legend))
    assert rows, "no band-table rows found; this guard would pass vacuously"
    for edge in bands:
        text = f"{edge * 100:g}%"
        # No digit or decimal point immediately before: 5% must not match 15%,
        # 35% or 3.5%.
        pattern = rf"(?<![\d.]){re.escape(text)}"
        assert re.search(pattern, rows), (
            f"{name} band edge {text} is not in any band-table row"
        )


def test_the_two_calibrations_are_documented_as_different(legend: str) -> None:
    """The single most important thing on the page.

    A reader who assumes one table applies to both lanes will read a healthy
    subchannel as damaged — which is exactly the bug that produced this
    document.
    """
    assert disc_map.RAMP_BANDS != disc_map.SUBQ_RAMP_BANDS
    assert "calibrated differently" in legend


def test_every_mono_glyph_is_documented(legend: str) -> None:
    """The fallback rendering is what survives into a log file, so it is the one
    a reader is most likely to meet without the code to hand."""
    for glyph in {*disc_map._GLYPH.values(), disc_map._UNREAD, disc_map._READ_ERR}:
        assert f"| `{glyph}` |" in legend, f"glyph {glyph} not documented"


def test_the_legend_fetches_nothing(legend: str) -> None:
    """Swatches are inline SVG data URIs, not remote images.

    A doc that renders differently depending on network access is a doc that is
    blank in an archive, on a plane, or behind a proxy — and this repo's docs
    reference no external host anywhere else.
    """
    external = [
        ln
        for ln in legend.splitlines()
        if "http" in ln and "http://www.w3.org/2000/svg" not in ln
    ]
    assert not external, f"external references: {external}"
