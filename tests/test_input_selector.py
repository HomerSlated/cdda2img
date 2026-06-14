"""
test_input_selector.py — unit tests for the four batching strategies.

All batch functions take (files, durations) directly, so no real audio files
are needed for most tests. select_batches() is tested with a monkeypatched
get_audio_duration_minutes so the tests stay fast and deterministic.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from cdda2img import input_selector
from cdda2img.input_selector import (
    MAX_TRACKS,
    batch_aatc,
    batch_best,
    batch_fcfs,
    batch_meta,
    select_batches,
)


def _paths(n: int) -> list[Path]:
    return [Path(f"/fake/track_{i:02d}.flac") for i in range(1, n + 1)]


# ---------------------------------------------------------------------------
# batch_fcfs
# ---------------------------------------------------------------------------


def test_fcfs_all_fit() -> None:
    files = _paths(3)
    result = batch_fcfs(files, [10.0, 10.0, 10.0])
    assert result == [files]


def test_fcfs_stops_at_capacity() -> None:
    # 40 + 40 = 80 (fits); adding 5 more would be 85 > 80
    files = _paths(3)
    result = batch_fcfs(files, [40.0, 40.0, 5.0])
    assert result == [files[:2]]


def test_fcfs_exactly_at_capacity() -> None:
    files = _paths(3)
    result = batch_fcfs(files, [30.0, 30.0, 20.0])
    assert result == [files]


def test_fcfs_stops_at_track_limit() -> None:
    files = _paths(MAX_TRACKS + 1)
    durations = [0.5] * (MAX_TRACKS + 1)
    result = batch_fcfs(files, durations)
    assert len(result) == 1
    assert len(result[0]) == MAX_TRACKS


def test_fcfs_oversized_first_track_excluded() -> None:
    # A single track longer than capacity: batch is empty (fcfs breaks immediately)
    files = _paths(2)
    result = batch_fcfs(files, [90.0, 10.0])
    assert result == [[]]


def test_fcfs_custom_capacity() -> None:
    files = _paths(3)
    result = batch_fcfs(files, [20.0, 20.0, 20.0], capacity_minutes=40)
    assert result == [files[:2]]


# ---------------------------------------------------------------------------
# batch_aatc
# ---------------------------------------------------------------------------


def test_aatc_single_disc() -> None:
    files = _paths(3)
    result = batch_aatc(files, [10.0, 10.0, 10.0])
    assert result == [files]


def test_aatc_two_discs() -> None:
    files = _paths(4)
    # 40+40=80 fits; adding 40 makes 120 > 80 → new disc
    result = batch_aatc(files, [40.0, 40.0, 40.0, 40.0])
    assert len(result) == 2
    assert result[0] == files[:2]
    assert result[1] == files[2:]


def test_aatc_track_limit_splits_disc() -> None:
    # 200 tracks of 0.2 min = 40 min total per disc, but each disc holds ≤ 99 tracks
    files = _paths(200)
    durations = [0.2] * 200
    result = batch_aatc(files, durations)
    assert len(result) == 3  # 99 + 99 + 2
    assert len(result[0]) == 99
    assert len(result[1]) == 99
    assert len(result[2]) == 2


def test_aatc_exactly_at_capacity() -> None:
    files = _paths(2)
    result = batch_aatc(files, [40.0, 40.0])
    assert result == [files]


def test_aatc_oversized_track_placed_on_own_disc() -> None:
    # A track longer than capacity: aatc still places it on its own disc
    files = _paths(2)
    result = batch_aatc(files, [90.0, 10.0])
    non_empty = [b for b in result if b]
    assert len(non_empty) == 2
    assert files[0] in non_empty[0]
    assert files[1] in non_empty[1]


def test_aatc_empty_result_for_empty_input() -> None:
    assert batch_aatc([], []) == []


# ---------------------------------------------------------------------------
# batch_best
# ---------------------------------------------------------------------------


def test_best_packs_tighter_than_aatc() -> None:
    # aatc: [41] → [41, 39] → [39] = 3 discs
    # best: [41, 39] + [41, 39] = 2 discs
    files = _paths(4)
    durations = [41.0, 41.0, 39.0, 39.0]
    aatc_result = batch_aatc(files, durations)
    best_result = batch_best(files, durations)
    assert len(best_result) < len(aatc_result)
    # All files present exactly once
    assert sorted(str(f) for f in [f for b in best_result for f in b]) == sorted(
        str(f) for f in files
    )


def test_best_single_disc_bypasses_solver() -> None:
    # When aatc returns 1 disc, best short-circuits without calling the solver
    files = _paths(3)
    result = batch_best(files, [10.0, 10.0, 10.0])
    assert len(result) == 1
    assert set(result[0]) == set(files)


def test_best_covers_all_files() -> None:
    files = _paths(6)
    durations = [20.0, 30.0, 25.0, 35.0, 15.0, 40.0]
    result = batch_best(files, durations)
    all_in_result = sorted(str(f) for f in [f for b in result for f in b])
    assert all_in_result == sorted(str(f) for f in files)


# ---------------------------------------------------------------------------
# batch_meta
# ---------------------------------------------------------------------------


def test_meta_groups_by_disc_number() -> None:
    files = _paths(4)
    disc_map = {files[0]: 1, files[1]: 1, files[2]: 2, files[3]: None}

    with patch.object(input_selector, "_read_disc_number", side_effect=disc_map.get):
        result = batch_meta(files)

    # disc 1 first, disc 2 second, untagged last
    assert result[0] == files[:2]
    assert result[1] == [files[2]]
    assert result[2] == [files[3]]


def test_meta_all_untagged() -> None:
    files = _paths(3)
    with patch.object(input_selector, "_read_disc_number", return_value=None):
        result = batch_meta(files)
    assert result == [files]


def test_meta_all_tagged_same_disc() -> None:
    files = _paths(3)
    with patch.object(input_selector, "_read_disc_number", return_value=1):
        result = batch_meta(files)
    assert result == [files]


def test_meta_sorted_disc_order() -> None:
    files = _paths(3)
    # Disc 2, 1, 3 — output should be ordered 1, 2, 3
    disc_map = {files[0]: 2, files[1]: 1, files[2]: 3}
    with patch.object(input_selector, "_read_disc_number", side_effect=disc_map.get):
        result = batch_meta(files)
    assert result[0] == [files[1]]  # disc 1
    assert result[1] == [files[0]]  # disc 2
    assert result[2] == [files[2]]  # disc 3


# ---------------------------------------------------------------------------
# select_batches
# ---------------------------------------------------------------------------


def test_select_batches_unknown_strategy_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # get_audio_duration_minutes must return non-zero so select_batches
    # doesn't bail early before reaching the strategy dispatch
    monkeypatch.setattr(input_selector, "get_audio_duration_minutes", lambda p: 10.0)
    with pytest.raises(ValueError, match="Unknown strategy"):
        select_batches([Path("/fake/f.flac")], "bogus")


def test_select_batches_filters_zero_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    files = _paths(3)
    dur_map = {files[0]: 10.0, files[1]: 0.0, files[2]: 10.0}
    monkeypatch.setattr(
        input_selector, "get_audio_duration_minutes", lambda p: dur_map[p]
    )
    result = select_batches(files, "fcfs")
    assert files[1] not in result[0]
    assert len(result[0]) == 2


def test_select_batches_all_zero_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    files = _paths(3)
    monkeypatch.setattr(input_selector, "get_audio_duration_minutes", lambda p: 0.0)
    assert select_batches(files, "aatc") == []


def test_select_batches_fcfs(monkeypatch: pytest.MonkeyPatch) -> None:
    files = _paths(3)
    monkeypatch.setattr(input_selector, "get_audio_duration_minutes", lambda p: 10.0)
    result = select_batches(files, "fcfs")
    assert len(result) == 1


def test_select_batches_aatc(monkeypatch: pytest.MonkeyPatch) -> None:
    files = _paths(4)
    monkeypatch.setattr(input_selector, "get_audio_duration_minutes", lambda p: 40.0)
    result = select_batches(files, "aatc")
    assert len(result) == 2


def test_select_batches_best(monkeypatch: pytest.MonkeyPatch) -> None:
    files = _paths(3)
    monkeypatch.setattr(input_selector, "get_audio_duration_minutes", lambda p: 10.0)
    result = select_batches(files, "best")
    assert len(result) == 1
    assert set(result[0]) == set(files)


def test_select_batches_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    files = _paths(4)
    disc_map = {files[0]: 1, files[1]: 1, files[2]: 2, files[3]: None}
    monkeypatch.setattr(input_selector, "get_audio_duration_minutes", lambda p: 10.0)
    with patch.object(input_selector, "_read_disc_number", side_effect=disc_map.get):
        result = select_batches(files, "meta")
    assert len(result) == 3


def test_select_batches_custom_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    files = _paths(3)
    monkeypatch.setattr(input_selector, "get_audio_duration_minutes", lambda p: 20.0)
    # capacity=40: 20+20=40 fits, 3rd overflows → 2 discs
    result = select_batches(files, "aatc", capacity_minutes=40)
    assert len(result) == 2
