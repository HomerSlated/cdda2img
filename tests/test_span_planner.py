"""Locate-and-cluster tests (span-recovery-plan.md §4.1-4.2).

Every test here is a pure function of a damage map and two integers — no device,
no engine, no fixture files — which is the point of the module being separate.

The bias under test is asymmetric and the tests are written to match it: the
planner is a cost optimiser, so over-triggering costs time while under-triggering
costs correctness. Where a case could reasonably go either way, the assertion is
that the planner errs wide.
"""

from __future__ import annotations

import pytest

from cdda2img.span_planner import Span, cluster_spans, locate_targets


def _map(size: int, damaged: list[int]) -> bytes:
    out = bytearray(size)
    for i in damaged:
        out[i] = 1
    return bytes(out)


# ---- locate ------------------------------------------------------------------


def test_targets_are_absolute_sectors_not_window_offsets() -> None:
    """The map is indexed by absolute sector and so are the results. A planner that
    returned window-relative indices would need every caller to add `start` back,
    and a caller that forgot would re-read the wrong part of the disc while looking
    entirely correct."""
    damage = _map(200, [100, 150])
    assert locate_targets(damage, 50, 120) == (100, 150)


def test_targets_outside_the_window_are_not_returned() -> None:
    damage = _map(200, [10, 100, 190])
    assert locate_targets(damage, 50, 100) == (100,)


def test_an_excluded_sector_is_dropped() -> None:
    """The disc's final sector is flagged on every rip of the reference disc and is
    a lead-out boundary effect, not damage. Excluding it is a parameter rather than
    a rule, so a bench run can still measure the artefact itself."""
    damage = _map(200, [100, 199])
    assert locate_targets(damage, 0, 200, exclude=[199]) == (100,)


def test_a_map_shorter_than_the_window_yields_only_what_it_covers() -> None:
    """Absent map bytes are not evidence of damage. Treating them as damaged would
    turn a truncated map into a whole-track re-read; treating them as clean is the
    honest reading, and the AR gate still judges the track either way."""
    damage = _map(120, [100])
    assert locate_targets(damage, 50, 200) == (100,)


def test_any_non_zero_byte_counts_as_damage() -> None:
    """The lane is `0` / non-zero, not a boolean, and the high nibble of the
    engine's map byte carries severity. Testing `== 1` would silently drop every
    severity above the lowest."""
    damage = bytearray(10)
    damage[3] = 0x40
    damage[7] = 0xFF
    assert locate_targets(bytes(damage), 0, 10) == (3, 7)


def test_a_clean_map_yields_no_targets() -> None:
    assert locate_targets(_map(100, []), 0, 100) == ()


# ---- cluster -----------------------------------------------------------------


def test_no_targets_yields_no_spans() -> None:
    """Which the caller must read as "nothing to do", never as "nothing is wrong":
    the capture pass cannot see a displacement, so a track can fail AccurateRip
    with an empty target set. That is §4.5's falsifier, not a success."""
    assert cluster_spans([], gap=4, pad=2, lo=0, hi=100) == ()


def test_gap_counts_the_clean_sectors_between_targets() -> None:
    """gap=0 merges only adjacent sectors; gap=3 tolerates a three-sector hole.
    Stated as a count of CLEAN sectors rather than a difference of addresses,
    because the off-by-one between the two is invisible in the output."""
    targets = [10, 11, 15]  # three clean sectors between 11 and 15
    assert cluster_spans(targets, gap=0, pad=0, lo=0, hi=100) == (
        Span(10, 2),
        Span(15, 1),
    )
    assert cluster_spans(targets, gap=2, pad=0, lo=0, hi=100) == (
        Span(10, 2),
        Span(15, 1),
    )
    assert cluster_spans(targets, gap=3, pad=0, lo=0, hi=100) == (Span(10, 6),)


def test_padding_grows_a_span_at_both_ends() -> None:
    """The engine settles a positional disagreement by anchoring against
    neighbours, so a span with no clean margin gives it nothing to anchor to."""
    assert cluster_spans([50], gap=0, pad=4, lo=0, hi=100) == (Span(46, 9),)


def test_spans_that_overlap_only_after_padding_are_merged() -> None:
    """The subtle one. These two clusters are 5 apart and do not merge at gap=1,
    but padding by 3 makes them overlap. Emitting both would re-read the shared
    sectors twice in a single pass — wasted time, and worse, an accepted-copy count
    in which one sector contributed two observations."""
    unpadded = cluster_spans([20, 26], gap=1, pad=0, lo=0, hi=100)
    assert unpadded == (Span(20, 1), Span(26, 1))  # five clean sectors apart
    # [17,24) and [23,30) overlap by one, so the merged span is [17,30).
    assert cluster_spans([20, 26], gap=1, pad=3, lo=0, hi=100) == (Span(17, 13),)


def test_padding_clamps_at_the_window_edges() -> None:
    """A span cannot grow outside the window it was given. This is a real
    limitation rather than arithmetic tidiness: a fault whose leading edge lies
    before `lo` cannot be reached by padding, which is why the caller passes the
    track window plus an offset margin rather than the track window alone."""
    assert cluster_spans([1], gap=0, pad=5, lo=0, hi=10) == (Span(0, 7),)
    assert cluster_spans([98], gap=0, pad=5, lo=90, hi=100) == (Span(93, 7),)


def test_an_empty_window_yields_no_spans() -> None:
    assert cluster_spans([50], gap=0, pad=0, lo=100, hi=100) == ()


def test_a_correct_island_inside_a_run_is_not_carved_out() -> None:
    """Measured at raw 113068: a four-sector byte-correct island sits in the middle
    of a wrong run with nothing marking either edge, and 31 of 44 wrong sectors
    were reported OK. So the map locates the neighbourhood of a fault, not its
    extent — and a planner clever enough to exclude the apparently-good middle
    would reintroduce exactly the corruption the rung exists to remove.

    A gap wide enough to span the island is what keeps it inside."""
    targets = [100, 101, 106, 107]  # 4-sector hole at 102-105
    assert cluster_spans(targets, gap=4, pad=0, lo=0, hi=200) == (Span(100, 8),)


def test_duplicate_targets_do_not_produce_duplicate_spans() -> None:
    assert cluster_spans([10, 10, 10], gap=0, pad=0, lo=0, hi=100) == (Span(10, 1),)


def test_unsorted_targets_are_handled() -> None:
    """Callers derive targets from an ordered scan today, so this is defensive —
    but a silently wrong clustering from an unordered input would look exactly like
    a tuning problem."""
    assert cluster_spans([15, 10, 11], gap=3, pad=0, lo=0, hi=100) == (Span(10, 6),)


@pytest.mark.parametrize(("gap", "pad"), [(-1, 0), (0, -1)])
def test_negative_gap_or_pad_is_refused(gap: int, pad: int) -> None:
    """Rejected rather than clamped to 0. A negative pad silently behaving as no
    padding would make a mistyped profile measure something other than what it
    says, and the whole bench rests on profiles meaning what they claim."""
    with pytest.raises(ValueError, match="must not be negative"):
        cluster_spans([10], gap=gap, pad=pad, lo=0, hi=100)


def test_spans_are_ascending_and_never_overlap() -> None:
    """The property the caller depends on: it walks spans in order and assumes a
    sector belongs to at most one, so an accepted copy has exactly one origin."""
    targets = [5, 6, 40, 41, 42, 90]
    spans = cluster_spans(targets, gap=2, pad=6, lo=0, hi=200)
    assert list(spans) == sorted(spans)
    for a, b in zip(spans, spans[1:]):
        assert a.stop <= b.start
    assert all(s.count > 0 for s in spans)


def test_every_target_lands_inside_some_span() -> None:
    """The invariant that makes under-triggering impossible *within* the located
    set: clustering may add sectors, never drop one. Dropping a target would mean a
    flagged sector is never re-read and so never witnessed."""
    targets = [3, 4, 30, 31, 32, 77, 120]
    spans = cluster_spans(targets, gap=2, pad=3, lo=0, hi=200)
    covered = {lba for s in spans for lba in range(s.start, s.stop)}
    assert set(targets) <= covered


def test_clustering_is_wider_than_the_targets_it_was_given() -> None:
    """Err-wide, asserted directly. If a future change made the planner tighter
    than its input, every test above would still pass while the rung quietly
    witnessed fewer sectors than it was asked to."""
    targets = [50, 51]
    spans = cluster_spans(targets, gap=0, pad=2, lo=0, hi=100)
    covered = sum(s.count for s in spans)
    assert covered > len(targets)
