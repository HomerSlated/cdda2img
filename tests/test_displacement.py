"""Displacement check: audio a capture holds intact but out of place.

Every fixture is a synthetic five-track disc of random audio, with the
AccurateRip blocks computed from that audio, so each rule can be broken one at a
time. Random, never silence: silence verifies at every shift.

The capture is in the RAW domain of a drive with a +30 read offset: file sample
``p`` holds disc sample ``p - 30``, exactly as ``verify_rip`` expects.
"""

from __future__ import annotations

import types
from pathlib import Path

import numpy as np
import pytest

import cdda2img.accudisc_reader as adr
from cdda2img import cdda2img as C
from cdda2img import displacement as dp
from cdda2img.accuraterip import (
    ARTrackResult,
    _ar_checksums_arr,
    _ar_crc450_arr,
    _window,
)

_S = 588  # samples per sector
_RO = 30
_LSNS = [0, 500, 1010, 1530, 2040]
_LAST = 2559  # lead-out LBA 2560
_N = len(_LSNS)
_ENDS = [*_LSNS[1:], _LAST + 1]
_TOTAL = (_LAST + 1) * _S


@pytest.fixture(scope="module")
def disc() -> np.ndarray:
    """The disc's own audio, as uint32 stereo frames."""
    rng = np.random.default_rng(20261010)
    return rng.integers(0, 2**32, size=_TOTAL, dtype=np.uint32)


def _block(disc: np.ndarray, *, at: int = 0, conf: int = 200, crc450: bool = True):
    """Two dBAR blocks (a v1-era and a v2-era one) for the pressing whose audio
    sits *at* samples from this disc's."""
    v1_block, v2_block = [], []
    for ti, (s, e) in enumerate(zip(_LSNS, _ENDS)):
        frames = _window(disc, s * _S + at, (e - s) * _S)
        v1, v2 = _ar_checksums_arr(frames, ti + 1, _N)
        c450 = (_ar_crc450_arr(frames) or 0) if crc450 else 0
        v1_block.append({"crc": v1, "conf": conf, "crc450": c450})
        v2_block.append({"crc": v2, "conf": conf, "crc450": c450})
    return [v1_block, v2_block]


def _in_place(disc: np.ndarray) -> np.ndarray:
    """What the drive returns when nothing goes wrong."""
    return np.concatenate([np.zeros(_RO, np.uint32), disc])[:_TOTAL]


def _lose(capture: np.ndarray, at_sector: int, n: int) -> np.ndarray:
    """Drop *n* samples from the stream at *at_sector*; everything after arrives
    *n* samples early."""
    c = at_sector * _S
    return np.concatenate([capture[:c], capture[c + n :], np.zeros(n, np.uint32)])


def _repeat(capture: np.ndarray, at_sector: int, n: int) -> np.ndarray:
    """Deliver *n* samples twice at *at_sector*; everything after arrives late."""
    c = at_sector * _S
    return np.concatenate([capture[:c], capture[c - n :]])[:_TOTAL]


def _verify(capture: np.ndarray, responses: list) -> list[ARTrackResult]:
    """verify_rip's arithmetic without its network fetch."""
    out = []
    for ti, (s, e) in enumerate(zip(_LSNS, _ENDS)):
        v1, v2 = _ar_checksums_arr(
            _window(capture, s * _S + _RO, (e - s) * _S), ti + 1, _N
        )
        hit_v1 = [r[ti]["conf"] for r in responses if r[ti]["crc"] == v1]
        hit_v2 = [r[ti]["conf"] for r in responses if r[ti]["crc"] == v2]
        out.append(
            ARTrackResult(
                track=ti + 1,
                v1_crc=f"{v1:08x}",
                v2_crc=f"{v2:08x}",
                confidence_v1=max(hit_v1) if hit_v1 else None,
                confidence_v2=max(hit_v2) if hit_v2 else None,
                max_confidence=max(r[ti]["conf"] for r in responses),
            )
        )
    return out


def _ok(results: list[ARTrackResult]) -> list[int]:
    return [r.track for r in results if r.confidence_v1 or r.confidence_v2]


def _find(capture: np.ndarray, responses: list, cohort: list | None = None):
    results = _verify(capture, responses)
    if cohort is None:
        cohort = dp.cohort_blocks(responses, results)
    return dp.find_displaced(
        capture, _LSNS, _LAST, cohort, dp.failed_track_numbers(results), _RO
    )


# ── finding ──────────────────────────────────────────────────────────────────


def test_fixture_verifies_in_place(disc):
    # The control every other test leans on: nothing displaced, nothing failed.
    responses = _block(disc)
    assert _ok(_verify(_in_place(disc), responses)) == [1, 2, 3, 4, 5]
    assert _find(_in_place(disc), responses) == {}


def test_lost_samples_are_found_and_the_sign_is_plus(disc):
    # 12 samples lost inside track 3: tracks 4 and 5 arrive 12 early.
    responses = _block(disc)
    capture = _lose(_in_place(disc), 1300, 12)
    assert _ok(_verify(capture, responses)) == [1, 2]

    found = _find(capture, responses)

    assert found == {4: -12, 5: -12}
    assert dp.outcome(found[4]) == "displaced@+12"


def test_the_track_holding_the_slip_is_left_for_the_ladder(disc):
    # Track 3's head is in place and its tail is not: no single shift fits, and
    # the 12 samples are gone. It must stay failed.
    capture = _lose(_in_place(disc), 1300, 12)
    assert 3 not in _find(capture, _block(disc))


def test_real_damage_matches_at_no_shift(disc):
    responses = _block(disc)
    capture = _in_place(disc).copy()
    capture[1700 * _S : 1700 * _S + 40] ^= 0x5A5A5A5A
    assert _ok(_verify(capture, responses)) == [1, 2, 3, 5]

    assert _find(capture, responses) == {}


def test_repeated_samples_give_a_minus_and_never_invent_the_last_tracks_tail(disc):
    # 12 samples delivered twice inside track 2: tracks 3 to 5 arrive 12 late.
    # Track 5 at +12 would need 12 samples the drive never delivered. Zeros there
    # sit in AccurateRip's exclusion zone and would pass, which is why it is
    # refused rather than checked.
    responses = _block(disc)
    capture = _repeat(_in_place(disc), 700, 12)

    found = _find(capture, responses)

    assert found == {3: 12, 4: 12}
    assert dp.outcome(found[3]) == "displaced@-12"
    # Control: the refusal is the rule, not a failed checksum. Zero-padded past
    # the end of the capture, track 5 does verify at +12.
    cohort = dp.cohort_blocks(responses, _verify(capture, responses))
    start, length = _LSNS[4] * _S + _RO, (_ENDS[4] - _LSNS[4]) * _S
    v1, v2 = _ar_checksums_arr(_window(capture, start + 12, length), 5, _N)
    assert any(block[4]["crc"] in (v1, v2) for block in cohort)


def test_another_pressings_offset_cannot_pass_as_a_displacement(disc):
    # A second pressing sits 50 samples from ours, so audio in its right place
    # matches THEIR blocks at +50. Audio displaced by -40 matches OUR blocks at
    # -40 and THEIRS at +10. The nearer shift is the wrong one.
    ours, theirs = _block(disc), _block(disc, at=50, conf=90)
    responses = ours + theirs
    capture = _lose(_in_place(disc), 700, 40)
    results = _verify(capture, responses)

    cohort = dp.cohort_blocks(responses, results)

    assert cohort == ours
    assert _find(capture, responses) == {3: -40, 4: -40, 5: -40}
    # Control: without the cohort rule the decoy wins, so this test can fail.
    unfiltered = _find(capture, responses, cohort=responses)
    assert unfiltered[3] == 10


def test_no_verified_track_means_no_anchor_and_no_answer(disc):
    responses = _block(disc)
    capture = _lose(_in_place(disc), 10, 12)  # inside track 1: every track fails
    results = _verify(capture, responses)
    assert _ok(results) == []

    assert dp.cohort_blocks(responses, results) == []
    assert _find(capture, responses) == {}


def test_without_frame_450_data_one_track_alone_is_not_enough(disc):
    # No independent second sum: a single whole-track match over thousands of
    # shifts is refused, and the same shift on two tracks is accepted.
    responses = _block(disc, crc450=False)

    one = _find(_lose(_in_place(disc), 1800, 12), responses)  # only track 5 moved
    two = _find(_lose(_in_place(disc), 1300, 12), responses)  # tracks 4 and 5

    assert one == {}
    assert two == {4: -12, 5: -12}
    # Control: with frame-450 data the single track IS accepted.
    assert _find(_lose(_in_place(disc), 1800, 12), _block(disc)) == {5: -12}


# ── writing ──────────────────────────────────────────────────────────────────


def _stage(tmp_path: Path, capture: np.ndarray, responses: list):
    pcm = tmp_path / "all_tracks.pcm"
    capture.astype("<u4").tofile(pcm)
    outcomes, cohort = C._displacement_stage(
        pcm, _LSNS, _LAST, _RO, _verify(capture, responses), responses, tmp_path
    )
    return pcm, outcomes, cohort


def test_reslice_restores_adjacent_tracks_and_touches_nothing_else(disc, tmp_path):
    # Tracks 4 and 5 are adjacent and both displaced: track 5's audio starts 12
    # samples inside track 4's place, so writing track 4 first in place would
    # destroy it. Track 5 verifying afterwards is what proves the two phases.
    responses = _block(disc)
    capture = _lose(_in_place(disc), 1300, 12)

    pcm, outcomes, _cohort = _stage(tmp_path, capture, responses)

    assert outcomes == {4: "displaced@+12", 5: "displaced@+12"}
    after = np.fromfile(pcm, dtype="<u4")
    assert _ok(_verify(after, responses)) == [1, 2, 4, 5]
    # Everything before track 4's place is byte-identical: tracks 1 and 2, and
    # track 3 with its slip still in it.
    untouched = _LSNS[3] * _S + _RO
    assert np.array_equal(after[:untouched], capture[:untouched])
    assert not (tmp_path / "resliced.pcm").exists()


def test_nothing_found_writes_nothing(disc, tmp_path):
    responses = _block(disc)
    capture = _in_place(disc).copy()
    capture[1700 * _S : 1700 * _S + 40] ^= 0x5A5A5A5A

    pcm, outcomes, cohort = _stage(tmp_path, capture, responses)

    assert outcomes == {}
    assert cohort == responses
    assert np.array_equal(np.fromfile(pcm, dtype="<u4"), capture)


def test_reslice_checks_the_bytes_again_before_writing(disc, tmp_path):
    # A shift nobody verified is handed straight to reslice: it must refuse.
    responses = _block(disc)
    capture = _in_place(disc)
    pcm = tmp_path / "all_tracks.pcm"
    capture.astype("<u4").tofile(pcm)

    written = dp.reslice(
        pcm,
        {4: -7},
        _LSNS,
        _LAST,
        _RO,
        responses,
        tmp_path / "s.bin",
        C._splice_corrected,
    )

    assert written == {}
    assert np.array_equal(np.fromfile(pcm, dtype="<u4"), capture)


# ── the speed ladder's re-reads ──────────────────────────────────────────────


def _drive(monkeypatch: pytest.MonkeyPatch, what_it_returns: np.ndarray) -> list:
    """A drive whose every span read comes from *what_it_returns*."""
    raw = what_it_returns.astype("<u4").tobytes()
    reads: list[tuple[int, int]] = []

    def fake_read_span(device, start, count, read_speed=None, progress_cb=None):
        reads.append((start, count))
        return raw[start * 2352 : (start + count) * 2352]

    monkeypatch.setattr(adr, "read_span_bytes", fake_read_span)
    return reads


def _ladder(tmp_path: Path, first_pass: np.ndarray, responses: list, cohort):
    pcm = tmp_path / "all_tracks.pcm"
    first_pass.astype("<u4").tofile(pcm)
    outcomes = C._recover_failed_tracks(
        "/dev/sr0",
        [types.SimpleNamespace(track=4)],
        _LSNS,
        _LAST,
        pcm,
        responses,
        _N,
        [40],
        1,
        _RO,
        None,
        None,
        cohort,
    )
    return outcomes, np.fromfile(pcm, dtype="<u4")


def test_a_displaced_re_read_is_used_and_recorded_as_displaced(
    disc, tmp_path, monkeypatch
):
    # The first pass damaged track 4; the re-read returns it intact, 12 early.
    responses = _block(disc)
    first_pass = _in_place(disc).copy()
    first_pass[1700 * _S : 1700 * _S + 40] ^= 0x5A5A5A5A
    reads = _drive(monkeypatch, _lose(_in_place(disc), 1300, 12))

    outcomes, after = _ladder(tmp_path, first_pass, responses, responses)

    assert outcomes == {4: "matched@40X,displaced@+12"}
    assert _ok(_verify(after, responses)) == [1, 2, 3, 4, 5]
    # The margin is what made the displaced copy reachable.
    assert reads == [(_LSNS[3] - 5, (_ENDS[3] - _LSNS[3]) + 5 + 1 + 5)]


def test_without_a_cohort_the_ladder_reads_its_old_window_and_does_not_test(
    disc, tmp_path, monkeypatch
):
    responses = _block(disc)
    first_pass = _in_place(disc).copy()
    first_pass[1700 * _S : 1700 * _S + 40] ^= 0x5A5A5A5A
    reads = _drive(monkeypatch, _lose(_in_place(disc), 1300, 12))

    outcomes, after = _ladder(tmp_path, first_pass, responses, None)

    assert outcomes == {4: "unrecovered"}
    assert np.array_equal(after, first_pass)
    assert reads == [(_LSNS[3], (_ENDS[3] - _LSNS[3]) + 1)]


def test_an_in_place_re_read_is_a_plain_match(disc, tmp_path, monkeypatch):
    responses = _block(disc)
    first_pass = _in_place(disc).copy()
    first_pass[1700 * _S : 1700 * _S + 40] ^= 0x5A5A5A5A
    _drive(monkeypatch, _in_place(disc))

    outcomes, after = _ladder(tmp_path, first_pass, responses, responses)

    assert outcomes == {4: "matched@40X"}
    assert _ok(_verify(after, responses)) == [1, 2, 3, 4, 5]


# ── wiring and wording ───────────────────────────────────────────────────────


def test_the_check_sits_between_ctdb_and_the_re_read_rungs():
    import inspect

    src = inspect.getsource(C.rip_image)
    ctdb = src.index("repair_whole_disc(")
    check = src.index("_displacement_stage(")
    span = src.index("_run_span_rung(")
    ladder = src.index("_recover_failed_tracks(")
    assert ctdb < check < span < ladder


def test_the_report_groups_tracks_by_displacement(capsys):
    C._print_displacement({
        6: "displaced@+12",
        7: "displaced@+12",
        8: "displaced@+12",
        10: "displaced@-6",
    })
    out = capsys.readouterr().out
    assert "tracks 6-8 intact in the capture, 12 samples early" in out
    assert "track 10 intact in the capture, 6 samples late" in out
    assert "no re-read" in out
