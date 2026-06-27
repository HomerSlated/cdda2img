"""cdrdao read-cd stderr parser → ProgressUpdate events.

Stateful line-by-line parser. Call feed() for each output line; it returns
a ProgressUpdate whenever the rip position changes, None otherwise.
Call done() when the cdrdao process exits to close out the final track.

State machine:
    INIT  → (separator line) → TOC
    TOC   → (Leadout line)   → READY
    READY → (Copying line)   → RIPPING
    RIPPING — emits ProgressUpdate on each Track/MSF line

The displayed track number is *derived from the monotonic disc position* (the
absolute MSF lines bisected against the per-track start offsets parsed from the
TOC table), not taken from cdrdao's "Track N..." lines. Those lines are an
unreliable display signal: on a damaged disc cdrdao stalls re-reading, then emits
a burst of "Track N..." lines faster than the UI repaints, collapsing e.g.
"Track 3 ... Track 13" into a single jump; a corrupt-but-CRC-valid Q-frame can
also emit a spurious far-ahead track. Tying the number to position keeps it
monotonic and always consistent with the progress bar.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from enum import Enum, auto


def _msf_to_frames(m: str, s: str, f: str) -> int:
    return int(m) * 60 * 75 + int(s) * 75 + int(f)


@dataclass(frozen=True)
class ProgressUpdate:
    track: int
    n_tracks: int
    elapsed_frames: int
    total_frames: int
    note: str = ""  # transient activity detail (e.g. cd-paranoia recovery on a stall)

    @property
    def fraction(self) -> float:
        if self.total_frames <= 0:
            return 0.0
        return min(1.0, self.elapsed_frames / self.total_frames)

    @property
    def status(self) -> str:
        w = len(str(self.n_tracks))
        return f"Ripping track {self.track:{w}}/{self.n_tracks}"


class _St(Enum):
    INIT = auto()
    TOC = auto()
    READY = auto()
    RIPPING = auto()


# "----...---" separator before the track table
_SEP = re.compile(r"^-{3,}\s*$")
# " 1  AUDIO  0  00:00:00(   0)  04:04:45(18345)"  → track, start LBA, length
_TOC_ROW = re.compile(
    r"^\s*(\d+)\s+AUDIO\s+\d+\s+[\d:]+\(\s*(\d+)\)\s+[\d:]+\(\s*(\d+)\)"
)
# "Leadout AUDIO  0  45:21:68(204143)"
_LEADOUT = re.compile(r"^Leadout\s+\S+\s+\d+\s+[\d:]+\(\s*(\d+)\)")
# "Copying audio tracks 1-11: ..."
_COPYING = re.compile(r"^Copying audio tracks\s+\d+-\d+:")
# "Track 1..."
_TRACK = re.compile(r"^Track\s+(\d+)\s*\.")
# "00:01:00"  — position within current track
_MSF = re.compile(r"^(\d+):(\d{2}):(\d{2})\s*$")


class CdrdaoProgress:
    """Stateful, line-by-line parser for cdrdao read-cd stderr."""

    def __init__(self) -> None:
        self._st = _St.INIT
        self._n_tracks = 0
        self._total_frames = 0
        self._current_track = 0  # last "Track N..." line; gates MSF + is a fallback
        self._elapsed = 0  # last absolute disc position seen, in frames (monotonic)
        self._track_starts: list[int] = []  # absolute start frame per track, from TOC

    @property
    def n_tracks(self) -> int:
        return self._n_tracks

    def feed(self, raw: str) -> ProgressUpdate | None:
        line = raw.rstrip()

        if self._st == _St.INIT:
            if _SEP.match(line):
                self._st = _St.TOC

        elif self._st == _St.TOC:
            m = _TOC_ROW.match(line)
            if m:
                self._n_tracks += 1
                self._track_starts.append(int(m.group(2)))
                return None
            m = _LEADOUT.match(line)
            if m:
                self._total_frames = int(m.group(1))
                self._st = _St.READY

        elif self._st == _St.READY:
            if _COPYING.match(line):
                self._st = _St.RIPPING

        elif self._st == _St.RIPPING:
            return self._feed_ripping(line)

        return None

    def _feed_ripping(self, line: str) -> ProgressUpdate | None:
        m = _TRACK.match(line)
        if m:
            self._current_track = int(m.group(1))
            return self._make()
        m = _MSF.match(line)
        if m and self._current_track > 0:
            # cdrdao prints the *absolute* disc position (MM:SS:FF measured from
            # frame 0), not a track-relative offset — use it directly as elapsed.
            # max() keeps it monotonic against a stray backward position.
            frames = _msf_to_frames(m.group(1), m.group(2), m.group(3))
            self._elapsed = max(self._elapsed, frames)
            return self._make()
        return None

    def _track_at(self, frames: int) -> int:
        """1-based track containing absolute disc position *frames*.

        Derived by bisecting the TOC start offsets, so it is monotonic and
        immune to cdrdao's unreliable "Track N..." ordering. Falls back to the
        last seen "Track N..." line if the TOC table was never parsed.
        """
        if not self._track_starts:
            return self._current_track
        return bisect.bisect_right(self._track_starts, frames)

    def done(self) -> ProgressUpdate | None:
        """Call when cdrdao exits; closes the last track to 100%."""
        if self._current_track == 0 or self._total_frames == 0:
            return None
        t = self._track_at(self._total_frames)
        self._current_track = 0
        return ProgressUpdate(
            track=t,
            n_tracks=self._n_tracks,
            elapsed_frames=self._total_frames,
            total_frames=self._total_frames,
        )

    def _make(self) -> ProgressUpdate:
        elapsed = min(self._elapsed, self._total_frames)
        return ProgressUpdate(
            track=self._track_at(elapsed),
            n_tracks=self._n_tracks,
            elapsed_frames=elapsed,
            total_frames=self._total_frames,
        )
