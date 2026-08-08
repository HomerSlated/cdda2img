"""The rip status line: which phase, and how wide it may ever be."""

from __future__ import annotations

import pytest

from cdda2img.accudisc_reader import ReadLanes
from cdda2img.cdda2img import _RipPhase


def _lanes(track_starts, sectors=200000, speed_x=40) -> ReadLanes:
    return ReadLanes(
        sectors=sectors,
        damage=bytearray(sectors),
        subq=None,
        track_starts=tuple(track_starts),
        speed_x=speed_x,
    )


def test_the_disc_phase_holds_until_the_toc_has_been_read() -> None:
    """Before the handover there is no track list, and the drive really is
    spinning up and seeking the lead-in. Naming a track here would be a guess
    dressed as a measurement."""
    phase = _RipPhase()
    assert phase.text == "Ripping disc…"
    assert phase.at(0) == "Ripping disc…"
    assert phase.at(5000) == "Ripping disc…"


def test_the_speed_appears_once_the_drive_has_reported_it() -> None:
    phase = _RipPhase()
    phase.begin(_lanes([0], speed_x=40))
    assert phase.text == "Ripping disc at 40x…"


def test_no_speed_clause_when_the_drive_did_not_report() -> None:
    """ "at 0x" or "at Nonex" looks like a measurement. Omitting is honest."""
    phase = _RipPhase()
    phase.begin(_lanes([0], speed_x=None))
    assert phase.text == "Ripping disc…"


def test_track_numbers_track_the_read_position() -> None:
    phase = _RipPhase()
    phase.begin(_lanes([0, 15000, 30000]))
    assert phase.at(1) == "Ripping track 01…"
    assert phase.at(14999) == "Ripping track 01…"
    assert phase.at(15000) == "Ripping track 01…"
    assert phase.at(15001) == "Ripping track 02…"
    assert phase.at(200000) == "Ripping track 03…"


def test_the_boundary_uses_the_last_sector_DELIVERED() -> None:
    """`done` is a COUNT, so sector `done - 1` is the last one actually read.
    Using `done` names the next track one sector early at every boundary — off
    by one in the direction that makes the display lead the drive."""
    phase = _RipPhase()
    phase.begin(_lanes([0, 15000]))
    assert phase.at(15000) == "Ripping track 01…"  # 15000 sectors read: 0..14999


def test_a_track_1_pregap_leaves_the_head_of_the_disc_unnamed() -> None:
    """ABBA Gold's INDEX 01 is at LBA 33, and `[0, 33)` belongs to no track.
    Reporting "track 01" there would name a track whose audio has not started."""
    phase = _RipPhase()
    phase.begin(_lanes([33]))
    # Still the disc phase — with its speed clause, since the TOC has been read
    # by now and the speed IS known. What must not appear is a track number.
    assert phase.at(1) == "Ripping disc at 40x…"
    assert phase.at(34) == "Ripping track 01…"


def test_the_widest_status_covers_every_phase_not_the_current_one() -> None:
    """The map pins every width for the life of the read, so the widest text has
    to be known BEFORE the first frame. Measuring the current text would pin the
    column count to whichever phase won the race — and the format truncates, so
    every longer phase would be cut for the rest of the rip."""
    phase = _RipPhase()
    phase.begin(_lanes(list(range(0, 150000, 15000)), speed_x=8))
    widest = phase.widest_status()
    assert widest >= len("Ripping disc at 8x…")
    assert widest >= len("Ripping track 10…")
    for done in (0, 1, 15001, 149999):
        assert len(phase.at(done)) <= widest


@pytest.mark.parametrize("speed", [4, 40, 100])
def test_the_speed_clause_is_covered_at_every_digit_count(speed: int) -> None:
    phase = _RipPhase()
    phase.begin(_lanes([0], speed_x=speed))
    assert len(phase.text) <= phase.widest_status()
