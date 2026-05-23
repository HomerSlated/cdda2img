"""
toc.py — TOC generation and track duration utilities.
"""

import json
import re
import wave
from pathlib import Path

from cdda2img.rbi_format import CD_FRAMES_PER_SECOND, RBIDisc, RBITocEntry

_TITLE_REPLACEMENTS: dict[str, str] = {
    "\u2018": "'",  # left single quote
    "\u2019": "'",  # right single quote
    "\u201c": '"',  # left double quote
    "\u201d": '"',  # right double quote
    "\u2013": "-",  # en dash
    "\u2014": "-",  # em dash
    "\u2026": "...",  # ellipsis
}


def sanitize_title(text: str) -> str:
    """Sanitize a track or album title for embedding in the TOC.

    The TOC format uses double-quote as a string delimiter, so any `"` that
    remains after Unicode replacement is converted to `'` to keep the grammar valid.
    """
    for bad, good in _TITLE_REPLACEMENTS.items():
        text = text.replace(bad, good)
    text = re.sub(r"^\d{1,2}[-. ]+", "", text)
    text = re.sub(r"[^\x00-\x7F]+", "", text)
    return text.replace('"', "'")


def get_track_durations(wav_files: list[Path]) -> list[int]:
    """Return CD frame counts (1/75 s) for each WAV file."""
    durations = []
    for path in wav_files:
        with wave.open(str(path), "rb") as w:
            frames_75 = int(w.getnframes() / w.getframerate() * CD_FRAMES_PER_SECOND)
            durations.append(frames_75)
    return durations


def build_toc_entries(
    tracklist: list[Path], durations: list[int], disc: RBIDisc
) -> list[RBITocEntry]:
    """Build RBITocEntry list from file paths, durations, and disc metadata."""
    entries = []
    current_frame = 0
    for i, (path, frames) in enumerate(zip(tracklist, durations), start=1):
        entries.append(
            RBITocEntry(
                track_number=i,
                title=sanitize_title(path.stem),
                performer=disc.artist,
                start_frame=current_frame,
                duration_frames=frames,
            )
        )
        current_frame += frames
    return entries


def generate_toc(
    disc: RBIDisc,
    raw_titles: list[str] | None = None,
) -> bytes:
    """Generate cdrdao-compatible TOC text for the given disc.

    If *raw_titles* is provided (one string per track), tracks whose raw title
    differs from the sanitized TOC title get a '// TRACK_TITLE_UNICODE: <json>'
    comment. This preserves the original Unicode title (e.g. curly quotes) for
    use as the FLAC TITLE tag on extraction, without breaking the TOC grammar.
    """
    album = sanitize_title(disc.album)
    artist = sanitize_title(disc.artist)
    pcm_filename = f"{album}.bin"

    lines: list[str] = ["CD_DA\n"]

    if disc.catalog:
        lines.append(f'CATALOG "{disc.catalog}"\n')

    disc_text_lines = []
    if album:
        disc_text_lines.append(f'    TITLE "{album}"')
    if artist:
        disc_text_lines.append(f'    PERFORMER "{artist}"')
    if disc.disc_id:
        disc_text_lines.append(
            f'    DISC_ID "{disc.disc_id.replace(chr(34), chr(39))}"'
        )

    if disc_text_lines:
        lines += [
            "CD_TEXT {",
            "  LANGUAGE_MAP {",
            "    0: 9",
            "  }",
            "  LANGUAGE 0 {",
            *disc_text_lines,
            "  }",
            "}\n",
        ]

    for idx, track in enumerate(disc.tracks):
        raw_title = raw_titles[idx] if raw_titles and idx < len(raw_titles) else None
        unicode_lines = (
            [f"// TRACK_TITLE_UNICODE: {json.dumps(raw_title)}"]
            if raw_title and raw_title != track.title
            else []
        )

        isrc_lines = [f'ISRC "{track.isrc}"'] if track.isrc else []
        start_lines = (
            [f"START {track.pregap_timestamp}"] if track.pregap_frames > 0 else []
        )

        track_cdtext_lines = []
        if track.title:
            track_cdtext_lines.append(f'    TITLE "{track.title}"')
        track_performer = sanitize_title(track.performer)
        if track_performer:
            track_cdtext_lines.append(f'    PERFORMER "{track_performer}"')
        track_cdtext_block = (
            ["CD_TEXT {", "  LANGUAGE 0 {", *track_cdtext_lines, "  }", "}"]
            if track_cdtext_lines
            else []
        )

        lines += [
            f"// Track {track.track_number}",
            *unicode_lines,
            "TRACK AUDIO",
            "NO COPY",
            "NO PRE_EMPHASIS",
            "TWO_CHANNEL_AUDIO",
            *isrc_lines,
            *track_cdtext_block,
            f'FILE "{pcm_filename}" {track.start_timestamp} {track.slot_timestamp}',
            *start_lines,
            "",
        ]

    return "\n".join(lines).encode("utf-8")
