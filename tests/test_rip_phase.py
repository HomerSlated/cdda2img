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
    assert phase.text.rstrip() == "Ripping disc…"
    assert phase.at(0).rstrip() == "Ripping disc…"
    assert phase.at(5000).rstrip() == "Ripping disc…"


def test_the_speed_appears_once_the_drive_has_reported_it() -> None:
    phase = _RipPhase()
    phase.begin(_lanes([0], speed_x=40))
    assert phase.text.rstrip() == "Ripping disc at 40x…"


def test_no_speed_clause_when_the_drive_did_not_report() -> None:
    """ "at 0x" or "at Nonex" looks like a measurement. Omitting is honest."""
    phase = _RipPhase()
    phase.begin(_lanes([0], speed_x=None))
    assert phase.text.rstrip() == "Ripping disc…"


def test_track_numbers_track_the_read_position() -> None:
    phase = _RipPhase()
    phase.begin(_lanes([0, 15000, 30000]))
    assert phase.at(1).rstrip() == "Ripping track 01…"
    assert phase.at(14999).rstrip() == "Ripping track 01…"
    assert phase.at(15000).rstrip() == "Ripping track 01…"
    assert phase.at(15001).rstrip() == "Ripping track 02…"
    assert phase.at(200000).rstrip() == "Ripping track 03…"


def test_the_boundary_uses_the_last_sector_DELIVERED() -> None:
    """`done` is a COUNT, so sector `done - 1` is the last one actually read.
    Using `done` names the next track one sector early at every boundary — off
    by one in the direction that makes the display lead the drive."""
    phase = _RipPhase()
    phase.begin(_lanes([0, 15000]))
    assert (
        phase.at(15000).rstrip() == "Ripping track 01…"
    )  # 15000 sectors read: 0..14999


def test_a_track_1_pregap_leaves_the_head_of_the_disc_unnamed() -> None:
    """ABBA Gold's INDEX 01 is at LBA 33, and `[0, 33)` belongs to no track.
    Reporting "track 01" there would name a track whose audio has not started."""
    phase = _RipPhase()
    phase.begin(_lanes([33]))
    # Still the disc phase — with its speed clause, since the TOC has been read
    # by now and the speed IS known. What must not appear is a track number.
    assert phase.at(1).rstrip() == "Ripping disc at 40x…"
    assert phase.at(34).rstrip() == "Ripping track 01…"


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


def test_every_phase_returns_the_same_width() -> None:
    """The property the map depends on, and the plain bar depends on too.

    The spin-up frame is drawn by the PLAIN bar — the map does not exist yet —
    and that path sizes itself from `len(status)` directly. If the phases differ
    in width, the whole widget jumps sideways the moment the map appears. It
    did, and that is what kgr saw as the bar lining up under a different letter.
    """
    phase = _RipPhase(speed_x=40)
    widths = {len(phase.text)}
    phase.begin(_lanes([0, 15000, 30000]))
    widths |= {len(phase.at(d)) for d in (0, 1, 15001, 200000)}
    assert len(widths) == 1, f"phase widths differ: {widths}"
    assert widths.pop() == phase.widest_status()


def test_the_width_is_bounded_by_red_book_not_by_this_disc() -> None:
    """It must be known before the TOC is read, so it cannot depend on the track
    count. A width that changes when the TOC lands is not a pinned width — the
    map would re-bucket every cell exactly once, mid-rip."""
    early = _RipPhase(speed_x=40)
    before = early.widest_status()
    early.begin(_lanes([0, 15000]))
    assert early.widest_status() == before

    ninety_nine = _RipPhase(speed_x=40)
    ninety_nine.begin(_lanes(list(range(0, 99 * 1000, 1000))))
    assert len(ninety_nine.at(98_500)) == ninety_nine.widest_status()


def test_the_speed_clause_survives_the_handover() -> None:
    """The speed is resolved before the read; lanes reporting None must not
    delete it, or the clause vanishes the instant the TOC lands."""
    phase = _RipPhase(speed_x=40)
    phase.begin(_lanes([0], speed_x=None))
    assert "40x" in phase.text
