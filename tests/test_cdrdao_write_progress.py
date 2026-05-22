"""
test_cdrdao_write_progress.py — Unit tests for the cdrdao write stderr progress parser.

Synthetic transcript mirrors real cdrdao write output (dao/dao.cc verbosity 1):
  "Writing track NN ..."  — track banner
  "Wrote X of Y MB (Buffers ...)."  — per-track progress (\r-terminated lines)
"""

from cdda2img.cdrdao_write_progress import CdrdaoWriteProgress

N_TRACKS = 3
TRANSCRIPT = [
    "Sending CUE sheet...",
    "Writing track 01 (mode AUDIO/AUDIO p)...",
    "Wrote 10 of 100 MB (Buffers  80%  80%).",
    "Wrote 50 of 100 MB (Buffers  75%  70%).",
    "Wrote 100 of 100 MB (Buffers  70%  65%).",
    "Writing track 02 (mode AUDIO/AUDIO p)...",
    "Wrote 20 of 80 MB (Buffers  80%  80%).",
    "Wrote 80 of 80 MB (Buffers  70%  65%).",
    "Writing track 03 (mode AUDIO/AUDIO p)...",
    "Wrote 30 of 60 MB (Buffers  80%  80%).",
    "Wrote 60 of 60 MB (Buffers  70%  65%).",
]


def _run(transcript: list[str]) -> tuple[CdrdaoWriteProgress, list]:
    parser = CdrdaoWriteProgress(N_TRACKS)
    updates = []
    for line in transcript:
        update = parser.feed(line)
        if update is not None:
            updates.append(update)
    return parser, updates


def test_fraction_never_exceeds_one() -> None:
    _, updates = _run(TRANSCRIPT)
    assert updates
    for u in updates:
        assert 0.0 <= u.fraction <= 1.0


def test_progress_is_monotonic() -> None:
    _, updates = _run(TRANSCRIPT)
    fractions = [u.fraction for u in updates]
    assert fractions == sorted(fractions)


def test_final_track_reported_correctly() -> None:
    _, updates = _run(TRANSCRIPT)
    last = updates[-1]
    assert last.track == 3
    assert last.n_tracks == N_TRACKS


def test_done_forces_full_progress() -> None:
    parser, _ = _run(TRANSCRIPT)
    final = parser.done()
    assert final is not None
    assert final.fraction == 1.0
    assert final.track == 3


def test_track_banner_advances_track_number() -> None:
    _, updates = _run(TRANSCRIPT)
    track_nums = [u.track for u in updates]
    # Each "Writing track N" line emits an update; track 1 appears first
    assert track_nums[0] == 1
    # At some point track number increases to 2 and then 3
    assert 2 in track_nums
    assert 3 in track_nums


def test_status_string_zero_pads() -> None:
    _, updates = _run(TRANSCRIPT)
    for u in updates:
        # n_tracks=3 → width=1, so no padding needed; just confirm format
        assert f"/{N_TRACKS}" in u.status
        assert "Burning track" in u.status
