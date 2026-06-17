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


# --- position-derived track number ---------------------------------------------
# The displayed track is bisected from the absolute MSF position over the TOC start
# offsets, NOT taken from "Track N..." lines. On a damaged disc cdrdao stalls then
# emits a burst of (or spurious far-ahead) "Track N..." lines; tying the number to
# position keeps it monotonic and consistent with the progress bar.

_PREAMBLE = TRANSCRIPT[:10]  # through the "Copying audio tracks" banner; now RIPPING


def _feed_all(parser: CdrdaoProgress, lines: list[str]) -> None:
    for line in lines:
        parser.feed(line)


def _track_after(parser: CdrdaoProgress, line: str) -> int:
    update = parser.feed(line)
    assert update is not None
    return update.track


def test_track_derived_from_position() -> None:
    parser = CdrdaoProgress()
    _feed_all(parser, _PREAMBLE)
    parser.feed("Track 1...")
    # disc starts = [0, 6000, 15000]; MSF lines are absolute disc positions.
    assert _track_after(parser, "00:13:00") == 1  # 975 frames   → track 1
    assert _track_after(parser, "01:30:00") == 2  # 6750 frames  → track 2
    assert _track_after(parser, "03:30:00") == 3  # 15750 frames → track 3


def test_spurious_track_line_does_not_advance_display() -> None:
    # Regression: a far-ahead "Track N..." line (coalesced burst or corrupt-but-
    # CRC-valid Q-frame) while the read position is still in track 1 must NOT jump
    # the displayed track. The number is position-derived.
    parser = CdrdaoProgress()
    _feed_all(parser, _PREAMBLE)
    parser.feed("Track 1...")
    parser.feed("01:00:00")  # 4500 frames, mid track 1
    update = parser.feed("Track 3...")  # spurious / coalesced ahead-of-position
    assert update is not None
    assert update.track == 1  # position says track 1, not 3


def test_track_never_skips_through_burst() -> None:
    # Stall at track 1, then cdrdao floods Track 2 + Track 3 lines (coalesced)
    # before progress advances. Displayed track must be monotonic and never skip.
    parser = CdrdaoProgress()
    _feed_all(parser, _PREAMBLE)
    burst = [
        "Track 1...",
        "01:00:00",  # track 1
        "Track 2...",  # burst of Track lines — must not jump the display
        "Track 3...",
        "01:30:00",  # 6750 → track 2
        "03:30:00",  # 15750 → track 3
    ]
    tracks = [u.track for line in burst if (u := parser.feed(line)) is not None]
    assert tracks == sorted(tracks)  # monotonic
    assert all(b - a <= 1 for a, b in zip(tracks, tracks[1:]))  # no skipped value
    assert tracks[0] == 1 and tracks[-1] == 3


def test_done_uses_position_for_final_track() -> None:
    # done() closes to the last track via position, not a stale "Track N..." line.
    parser = CdrdaoProgress()
    _feed_all(parser, _PREAMBLE)
    parser.feed("Track 1...")
    parser.feed("01:00:00")  # still mid track 1 when cdrdao exits
    final = parser.done()
    assert final is not None
    assert final.track == 3  # last track, derived from total_frames
    assert final.fraction == 1.0
