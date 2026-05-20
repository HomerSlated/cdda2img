"""
test_cdrdao_progress.py — Unit tests for the cdrdao read-cd stderr progress parser.

The transcript fixture mirrors real `cdrdao read-cd` stderr (see
private/images/cdrdao/*.log): a Start/Length TOC table, a "Copying audio tracks"
banner, then interleaved "Track N..." and absolute "MM:SS:FF" position lines.

Regression guard: cdrdao prints the *absolute* disc position, not a track-relative
offset. A parser that adds a per-track base offset overshoots the leadout total.
"""

from cdda2img.cdrdao_progress import CdrdaoProgress

# 3-track synthetic disc; leadout at 20000 frames. TOC columns are Start, Length.
# MSF progress lines are absolute disc positions: 04:20:00 == 260 s == 19500 frames.
TRANSCRIPT = [
    "Reading toc and track data...",
    "",
    "Track   Mode    Flags  Start                Length",
    "------------------------------------------------------------",
    " 1      AUDIO   0      00:00:00(     0)     01:20:00(  6000)",
    " 2      AUDIO   0      01:20:00(  6000)     02:00:00(  9000)",
    " 3      AUDIO   0      03:20:00( 15000)     01:06:50(  5000)",
    "Leadout AUDIO   0      04:26:50( 20000)",
    "",
    'Copying audio tracks 1-3: start 00:00:00, length 04:26:50 to "rip.bin"...',
    "Track 1...",
    "00:13:00",
    "01:00:00",
    "Track 2...",
    "01:30:00",
    "03:00:00",
    "Track 3...",
    "03:30:00",
    "04:20:00",
]


def _run(transcript: list[str]) -> tuple[CdrdaoProgress, list]:
    parser = CdrdaoProgress()
    updates = []
    for line in transcript:
        update = parser.feed(line)
        if update is not None:
            updates.append(update)
    return parser, updates


def test_elapsed_never_exceeds_total() -> None:
    _, updates = _run(TRANSCRIPT)
    assert updates
    for u in updates:
        assert u.elapsed_frames <= u.total_frames
        assert 0.0 <= u.fraction <= 1.0


def test_msf_is_absolute_not_double_counted() -> None:
    _, updates = _run(TRANSCRIPT)
    last = updates[-1]
    # final MSF line "04:20:00" == 19500 frames, used directly — no per-track base.
    assert last.elapsed_frames == 19500
    assert last.total_frames == 20000
    assert last.track == 3


def test_progress_is_monotonic() -> None:
    _, updates = _run(TRANSCRIPT)
    fractions = [u.fraction for u in updates]
    assert fractions == sorted(fractions)


def test_done_forces_full_progress() -> None:
    parser, _ = _run(TRANSCRIPT)
    final = parser.done()
    assert final is not None
    assert final.elapsed_frames == final.total_frames == 20000
    assert final.fraction == 1.0


def test_n_tracks_counted_from_toc() -> None:
    parser, _ = _run(TRANSCRIPT)
    assert parser.n_tracks == 3
