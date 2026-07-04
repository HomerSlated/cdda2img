"""
toc.py - TOC generation and track duration utilities.
"""

import json
import re
import wave
from pathlib import Path

from cdda2img.barcode import normalize_barcode
from cdda2img.rbi_format import (
    CD_FRAMES_PER_SECOND,
    RBIDisc,
    RBITocEntry,
    timestamp_from_frames,
)

_TITLE_REPLACEMENTS: dict[str, str] = {
    "\u2018": "'",  # left single quote
    "\u2019": "'",  # right single quote
    "\u201c": '"',  # left double quote
    "\u201d": '"',  # right double quote
    "\u2013": "-",  # en dash
    "—": "-",  # em dash
    "\u2026": "...",  # ellipsis
}

# Control characters (incl. newline/CR/tab and DEL) - these must never reach a
# quoted TOC string: a newline breaks out of the string and lets the rest of the
# value inject arbitrary cdrdao TOC directives.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def escape_toc_string(text: str) -> str:
    """Make *text* safe to embed inside a cdrdao TOC ``"..."`` string.

    cdrdao's TOC lexer (verified against its ANTLR grammar) treats ``\\`` as an
    escape introducer inside strings - ``\\"`` is a literal quote, ``\\NNN`` an
    octal escape - so a value ending in a lone backslash would escape the closing
    delimiter. This neutralises all three injection vectors:

    1. strip control characters (newline/CR/tab/DEL) so the value cannot break
       onto a new TOC line;
    2. double every backslash so cdrdao reads it as a literal, never as an escape
       (this also neutralises ``\\NNN`` octal and ``\\"`` sequences);
    3. convert the ``"`` delimiter to ``'``.

    Non-ASCII is preserved - callers needing ASCII-only output (see
    :func:`sanitize_title`) strip it separately.
    """
    text = _CONTROL_CHARS.sub("", text)
    text = text.replace("\\", "\\\\")
    return text.replace('"', "'")


def sanitize_title(text: str) -> str:
    """Sanitize a track or album title for embedding in the TOC.

    Replaces common Unicode punctuation with ASCII, strips a leading track
    number and any remaining non-ASCII, then applies :func:`escape_toc_string`
    so the result is both ASCII-only (for CD-Text) and injection-safe.
    """
    for bad, good in _TITLE_REPLACEMENTS.items():
        text = text.replace(bad, good)
    text = re.sub(r"^\d{1,2}[-. ]+", "", text)
    text = re.sub(r"[^\x00-\x7F]+", "", text)
    return escape_toc_string(text)


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

    # Safety net: drop the CATALOG line unless disc.catalog is a *burnable* MCN
    # (13 numeric digits — exactly what cdrdao's Toc::catalog requires). The
    # GS1 check digit is NOT enforced here: by burn time disc.catalog is the
    # MCN the selection step already chose (a clean one when available; an
    # invalid-check-digit gospel value only as last resort), and cdrdao burns
    # any 13-digit numeric catalog. The check-digit ranking lives upstream in
    # _collect_barcode_candidates, not here — so we never drop a gospel MCN.
    catalog_norm = normalize_barcode(disc.catalog, require_check_digit=False)
    if catalog_norm:
        lines.append(f'CATALOG "{catalog_norm}"\n')

    disc_text_lines = []
    if album:
        disc_text_lines.append(f'    TITLE "{album}"')
    if artist:
        disc_text_lines.append(f'    PERFORMER "{artist}"')
    if disc.cdtext_catalog_ref:
        disc_text_lines.append(
            f'    DISC_ID "{escape_toc_string(disc.cdtext_catalog_ref)}"'
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

        # ISRC is validated upstream (validate_isrc -> [A-Z0-9]{12}); escape it
        # anyway so a serialiser-boundary regression can't become an injection.
        isrc_lines = [f'ISRC "{escape_toc_string(track.isrc)}"'] if track.isrc else []
        start_lines = (
            [f"START {track.pregap_timestamp}"] if track.pregap_frames > 0 else []
        )
        # INDEX >= 02 points: offsets relative to the audio start, ascending;
        # index numbers implicit and sequential (rbi_spec §6.1.10).
        index_lines = [
            f"INDEX {timestamp_from_frames(off)}" for off in sorted(track.index_points)
        ]

        track_cdtext_lines = []
        if track.title:
            # GRD-2026-0531-01: track.title is free-text from MB/CDDB and reaches
            # cdrdao raw. Escape (preserving non-ASCII) so it cannot inject TOC
            # directives; the exact Unicode title is still recoverable from the
            # TRACK_TITLE_UNICODE comment above for FLAC extraction.
            track_cdtext_lines.append(f'    TITLE "{escape_toc_string(track.title)}"')
        track_performer = sanitize_title(track.performer) or sanitize_title(disc.artist)
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
            "COPY" if track.copy_permitted else "NO COPY",
            "PRE_EMPHASIS" if track.pre_emphasis else "NO PRE_EMPHASIS",
            "TWO_CHANNEL_AUDIO",
            *isrc_lines,
            *track_cdtext_block,
            f'FILE "{pcm_filename}" {track.start_timestamp} {track.slot_timestamp}',
            *start_lines,
            *index_lines,
            "",
        ]

    return "\n".join(lines).encode("utf-8")
