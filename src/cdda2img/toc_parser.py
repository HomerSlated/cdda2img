"""
toc_parser.py — Parse cdrdao-format TOC text into structured track data.
"""

import json
import re
from dataclasses import dataclass, field

from cdda2img.rbi_format import frames_from_timestamp


@dataclass
class ParsedTrack:
    track_number: int
    title: str
    performer: str
    start_frame: int  # PCM/BIN offset to pregap start (or audio start if no pregap)
    duration_frames: int  # audio-only duration in CD frames; excludes pregap
    pregap_frames: int = 0  # pregap duration in CD frames; 0 if none
    isrc: str | None = None  # ISO 3901 ISRC (12 chars); None if absent

    @property
    def audio_start_frame(self) -> int:
        """Absolute frame offset to the first audio sample (after any pregap)."""
        return self.start_frame + self.pregap_frames


@dataclass
class ParsedDisc:
    title: str
    performer: str
    catalog: str | None = None  # MCN / EAN-13; None if absent or all-zeros
    disc_id: str | None = None  # PTI 0x86 catalogue/label reference; None if absent
    tracks: list[ParsedTrack] = field(default_factory=list)


_CATALOG_RE = re.compile(r'CATALOG\s+"([^"]+)"')
_TITLE_RE = re.compile(r'TITLE\s+"([^"]*)"')
_PERFORMER_RE = re.compile(r'PERFORMER\s+"([^"]*)"')
_DISC_ID_RE = re.compile(r'DISC_ID\s+"([^"]*)"')
_ISRC_RE = re.compile(r'ISRC\s+"([^"]+)"')
_FILE_TS_RE = re.compile(
    r'FILE\s+"[^"]+"\s+(0|\d{2}:\d{2}:\d{2})\s+(\d{2}:\d{2}:\d{2})'
)
_START_RE = re.compile(r"^\s*START\s+(\d{2}:\d{2}:\d{2})", re.MULTILINE)
_TRACK_MARKER_RE = re.compile(r"^//\s*Track\s+(\d+)", re.MULTILINE)
_TITLE_UNICODE_RE = re.compile(r"^//\s*TRACK_TITLE_UNICODE:\s*(.+)$", re.MULTILINE)

_ALL_ZEROS_MCN = "0000000000000"


def _first(pattern: re.Pattern[str], text: str, default: str = "") -> str:
    m = pattern.search(text)
    return m.group(1) if m else default


def _first_or_none(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    return m.group(1) if m else None


def parse_toc(toc_bytes: bytes) -> ParsedDisc:
    """Parse cdrdao-format TOC bytes and return disc/track metadata."""
    text = toc_bytes.decode("utf-8")
    markers = list(_TRACK_MARKER_RE.finditer(text))

    disc_section = text[: markers[0].start()] if markers else text
    disc_title = _first(_TITLE_RE, disc_section)
    disc_performer = _first(_PERFORMER_RE, disc_section)

    catalog_raw = _first_or_none(_CATALOG_RE, disc_section)
    catalog = catalog_raw if catalog_raw and catalog_raw != _ALL_ZEROS_MCN else None
    disc_id = _first_or_none(_DISC_ID_RE, disc_section)

    tracks = []
    for i, marker in enumerate(markers):
        block_end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        block = text[marker.start() : block_end]

        file_m = _FILE_TS_RE.search(block)
        if not file_m:
            continue

        unicode_m = _TITLE_UNICODE_RE.search(block)
        if unicode_m:
            try:
                track_title = json.loads(unicode_m.group(1))
            except (json.JSONDecodeError, ValueError):
                track_title = _first(_TITLE_RE, block, disc_title)
        else:
            track_title = _first(_TITLE_RE, block, disc_title)

        slot_frames = frames_from_timestamp(file_m.group(2))
        start_m = _START_RE.search(block)
        pregap_frames = frames_from_timestamp(start_m.group(1)) if start_m else 0
        duration_frames = slot_frames - pregap_frames

        tracks.append(
            ParsedTrack(
                track_number=int(marker.group(1)),
                title=track_title,
                performer=_first(_PERFORMER_RE, block, disc_performer),
                start_frame=0
                if file_m.group(1) == "0"
                else frames_from_timestamp(file_m.group(1)),
                duration_frames=duration_frames,
                pregap_frames=pregap_frames,
                isrc=_first_or_none(_ISRC_RE, block),
            )
        )

    return ParsedDisc(
        title=disc_title,
        performer=disc_performer,
        catalog=catalog,
        disc_id=disc_id,
        tracks=tracks,
    )
