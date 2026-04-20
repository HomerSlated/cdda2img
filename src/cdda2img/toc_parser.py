"""
toc_parser.py — Parse cdrdao-format TOC text into structured track data.
"""

import re
from dataclasses import dataclass, field

from cdda2img.rbi_format import frames_from_timestamp


@dataclass
class ParsedTrack:
    track_number: int
    title: str
    performer: str
    start_frame: int  # absolute CD frame offset into PCM blob
    duration_frames: int  # track length in CD frames


@dataclass
class ParsedDisc:
    title: str
    performer: str
    tracks: list[ParsedTrack] = field(default_factory=list)


_TITLE_RE = re.compile(r'TITLE\s+"([^"]*)"')
_PERFORMER_RE = re.compile(r'PERFORMER\s+"([^"]*)"')
_FILE_TS_RE = re.compile(r'FILE\s+"[^"]+"\s+(\d{2}:\d{2}:\d{2})\s+(\d{2}:\d{2}:\d{2})')
_TRACK_MARKER_RE = re.compile(r"^//\s*Track\s+(\d+)", re.MULTILINE)


def _first(pattern: re.Pattern[str], text: str, default: str = "") -> str:
    m = pattern.search(text)
    return m.group(1) if m else default


def parse_toc(toc_bytes: bytes) -> ParsedDisc:
    """Parse cdrdao-format TOC bytes and return disc/track metadata."""
    text = toc_bytes.decode("utf-8")
    markers = list(_TRACK_MARKER_RE.finditer(text))

    disc_section = text[: markers[0].start()] if markers else text
    disc_title = _first(_TITLE_RE, disc_section)
    disc_performer = _first(_PERFORMER_RE, disc_section)

    tracks = []
    for i, marker in enumerate(markers):
        block_end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        block = text[marker.start() : block_end]

        file_m = _FILE_TS_RE.search(block)
        if not file_m:
            continue

        tracks.append(
            ParsedTrack(
                track_number=int(marker.group(1)),
                title=_first(_TITLE_RE, block, disc_title),
                performer=_first(_PERFORMER_RE, block, disc_performer),
                start_frame=frames_from_timestamp(file_m.group(1)),
                duration_frames=frames_from_timestamp(file_m.group(2)),
            )
        )

    return ParsedDisc(title=disc_title, performer=disc_performer, tracks=tracks)
