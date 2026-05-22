"""cdrdao write stderr parser → WriteProgressUpdate events.

Stateful line-by-line parser. Call feed() for each stderr line; it returns
a WriteProgressUpdate whenever the burn position changes, None otherwise.
Call done() when cdrdao exits to close out to 100%.

cdrdao write emits (at verbosity 1):
    "Writing track NN (mode ...)..."     — track start
    "Wrote X of Y MB (Buffers ...).\r"  — per-track progress
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class WriteProgressUpdate:
    track: int
    n_tracks: int
    fraction: float

    @property
    def status(self) -> str:
        w = len(str(self.n_tracks))
        return f"Burning track {self.track:{w}d}/{self.n_tracks}"


# "Writing track 03 (mode AUDIO/AUDIO p)..."
_WRITING_TRACK = re.compile(r"^Writing track\s+(\d+)")
# "Wrote 12 of 345 MB (Buffers  75%  70%)."
_WROTE_MB = re.compile(r"^Wrote\s+(\d+)\s+of\s+(\d+)\s+MB")


class CdrdaoWriteProgress:
    """Stateful, line-by-line parser for cdrdao write stderr."""

    def __init__(self, n_tracks: int) -> None:
        self._n_tracks = n_tracks
        self._current_track = 0
        self._completed_tracks = 0  # fully reported tracks before the current one

    @property
    def n_tracks(self) -> int:
        return self._n_tracks

    def feed(self, raw: str) -> WriteProgressUpdate | None:
        line = raw.rstrip("\r\n").strip()

        m = _WRITING_TRACK.match(line)
        if m:
            new_track = int(m.group(1))
            if self._current_track > 0:
                # previous track is now complete
                self._completed_tracks += 1
            self._current_track = new_track
            return self._make(0, 1)

        m = _WROTE_MB.match(line)
        if m and self._current_track > 0:
            wrote = int(m.group(1))
            total = int(m.group(2))
            return self._make(wrote, max(total, 1))

        return None

    def done(self) -> WriteProgressUpdate | None:
        """Call when cdrdao exits; forces fraction to 1.0."""
        if self._current_track == 0:
            return None
        t = self._current_track
        self._current_track = 0
        return WriteProgressUpdate(track=t, n_tracks=self._n_tracks, fraction=1.0)

    def _make(self, wrote: int, total: int) -> WriteProgressUpdate:
        track_frac = wrote / total
        fraction = (self._completed_tracks + track_frac) / max(self._n_tracks, 1)
        return WriteProgressUpdate(
            track=self._current_track,
            n_tracks=self._n_tracks,
            fraction=min(1.0, fraction),
        )
