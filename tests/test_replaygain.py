"""
test_replaygain.py — Unit tests for EBU R128 loudness analysis.

Covers analyse_raw(): the np.memmap-based raw-PCM analysis used by the rip and
import pipelines. Focus areas: the progress-callback contract, and the invariant
that chunked add_frames() feeding yields results independent of chunk size.

Fixtures are synthetic (a quiet sine written as raw s16le) so no example audio
or physical disc is required.
"""

import numpy as np
import pytest

from cdda2img import replaygain
from cdda2img.rbi_format import RBIDisc, RBITocEntry

_INT16_PER_FRAME = 1176  # int16 samples per CD frame (588 stereo pairs)


def _write_pcm(path, cd_frames: int) -> None:
    """Write a quiet stereo s16le sine spanning *cd_frames* CD frames to *path*."""
    n = cd_frames * _INT16_PER_FRAME
    sine = (np.sin(np.arange(n, dtype=np.float64) / 50.0) * 3000).astype("<i2")
    path.write_bytes(sine.tobytes())


def _make_disc(durations: list[int]) -> RBIDisc:
    """Build a disc with contiguous, pregap-free tracks of the given frame durations."""
    disc = RBIDisc(album="Test", artist="Test")
    start = 0
    for i, dur in enumerate(durations, start=1):
        disc.tracks.append(
            RBITocEntry(
                track_number=i,
                title=f"T{i}",
                performer="Test",
                start_frame=start,
                duration_frames=dur,
            )
        )
        start += dur
    return disc


@pytest.fixture
def disc_and_pcm(tmp_path):
    durations = [1000, 1800, 500]  # track 2 spans multiple default-size chunks
    disc = _make_disc(durations)
    pcm = tmp_path / "all.pcm"
    _write_pcm(pcm, sum(durations))
    return disc, pcm, sum(durations)


def test_progress_callback_contract(disc_and_pcm) -> None:
    disc, pcm, total = disc_and_pcm
    calls: list[tuple[int, int]] = []
    replaygain.analyse_raw(disc, pcm, progress_cb=lambda d, t: calls.append((d, t)))

    assert calls, "progress_cb was never called"
    done = [d for d, _ in calls]
    assert {t for _, t in calls} == {total}  # total constant, == sum of durations
    assert done == sorted(done)  # monotonically non-decreasing
    assert done[-1] == total  # ends at exactly 100%
    assert all(0 < d <= total for d in done)


def test_chunk_size_does_not_affect_result(disc_and_pcm, monkeypatch) -> None:
    disc, pcm, _ = disc_and_pcm

    monkeypatch.setattr(replaygain, "_RG_CHUNK_FRAMES", 250)
    fine = replaygain.analyse_raw(disc, pcm)
    monkeypatch.setattr(replaygain, "_RG_CHUNK_FRAMES", 10_000_000)
    whole = replaygain.analyse_raw(disc, pcm)

    assert fine.album_gain == pytest.approx(whole.album_gain)
    assert fine.album_peak == pytest.approx(whole.album_peak)
    assert fine.album_lra == pytest.approx(whole.album_lra)
    assert len(fine.tracks) == len(whole.tracks) == 3
    for a, b in zip(fine.tracks, whole.tracks):
        assert a.gain == pytest.approx(b.gain)
        assert a.peak == pytest.approx(b.peak)
        assert a.lra == pytest.approx(b.lra)


def test_analyse_raw_rejects_empty_disc(tmp_path) -> None:
    pcm = tmp_path / "empty.pcm"
    _write_pcm(pcm, 1)
    with pytest.raises(ValueError, match="no tracks"):
        replaygain.analyse_raw(RBIDisc(album="x", artist="y"), pcm)
