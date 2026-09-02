"""
toc.py - TOC generation and track duration utilities.
"""

import json
import logging
import re
import wave
from pathlib import Path

from unidecode import unidecode

from cdda2img.barcode import normalize_barcode
from cdda2img.rbi_format import (
    PCM_SAMPLE_RATE,
    SAMPLES_PER_CD_FRAME,
    RBIDisc,
    RBITocEntry,
    frames_for_samples,
    timestamp_from_frames,
)

logger = logging.getLogger(__name__)

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

    Non-ASCII is preserved - callers needing charset-safe output (see
    :func:`fold_cdtext`) fold it separately.
    """
    text = _CONTROL_CHARS.sub("", text)
    text = text.replace("\\", "\\\\")
    return text.replace('"', "'")


def fold_cdtext(text: str, field: str = "field") -> str:
    """Transliterate *text* to CD-Text-safe ASCII, reporting every change.

    The CD-Text character set cannot hold arbitrary Unicode, and cdrdao drops
    the **entire** CD-Text lead-in - silently, exit 0 - if any single character
    fails to encode (its ``CdTextItem::updateEncoding`` returns void, so no
    caller can even learn it failed). A real burn lost all CD-Text to one
    ``U+2010 HYPHEN`` that MusicBrainz put in a title. We therefore fold to
    ASCII here, at TOC-generation time (never at burn time, where the cost is
    physical media), using a published transliteration table (``unidecode``)
    rather than a hand-kept list that will always miss the next character.

    Every substitution is reported so the change is never silent - the previous
    ``re.sub(r"[^\\x00-\\x7F]+", "", text)`` deleted unmapped characters
    indistinguishably from success. A character with no transliteration at all
    is dropped from *this one field only*, loudly - it can never take down the
    whole lead-in. ASCII (incl. control characters) passes through untouched;
    control characters are stripped downstream by :func:`escape_toc_string`.

    Normalisation is deliberately not used: ``U+2010`` has an empty Unicode
    decomposition and all four NF forms leave it unchanged.
    """
    out: list[str] = []
    mapped: list[str] = []
    dropped: list[str] = []
    for ch in text:
        if ord(ch) < 0x80:
            out.append(ch)
            continue
        repl = unidecode(ch)
        out.append(repl)
        if repl:
            mapped.append(f"{ch!r}->{repl!r}")
        else:
            dropped.append(f"U+{ord(ch):04X}")
    if mapped:
        logger.info("CD-Text %s: transliterated %s", field, ", ".join(mapped))
    if dropped:
        logger.warning(
            "CD-Text %s: dropped %d untransliterable character(s): %s",
            field,
            len(dropped),
            " ".join(dropped),
        )
    return "".join(out)


def sanitize_title(text: str, field: str = "title") -> str:
    """Sanitize a track or album title for embedding in the TOC.

    Strips a leading track number, transliterates to CD-Text-safe ASCII
    (reporting every substitution, see :func:`fold_cdtext`), then applies
    :func:`escape_toc_string` so the result is both charset-safe and
    injection-safe.
    """
    text = re.sub(r"^\d{1,2}[-. ]+", "", text)
    return escape_toc_string(fold_cdtext(text, field))


def track_frame_durations(wav_files: list[Path]) -> tuple[list[int], int]:
    """Per-track CD frame counts for a GAPLESS concatenation of *wav_files*.

    Returns ``(durations, total_frames)``.

    Each track boundary is snapped to the nearest CD frame of the *continuous*
    stream, so the audio stays bit-identical to the concatenation and a gapless
    album survives: only the index marks move, by at most half a frame (6.7 ms).
    That error does **not** accumulate, because every boundary is derived from
    the absolute cumulative sample count rather than from a running sum of
    already-rounded durations — summing rounded values is what displaced later
    tracks without bound (rbi_spec §6.2.1).

    The final boundary rounds UP so no audio is ever dropped; the resulting
    sub-frame remainder is padded once, at the lead-out, by the caller.
    """
    bounds = [0]
    cumulative = 0
    for path in wav_files:
        with wave.open(str(path), "rb") as w:
            if w.getframerate() != PCM_SAMPLE_RATE:
                msg = (
                    f"{path.name}: {w.getframerate()} Hz is not Red Book audio "
                    f"({PCM_SAMPLE_RATE} Hz required)"
                )
                raise ValueError(msg)
            cumulative += w.getnframes()
        # Round half-up in integers: duration arithmetic never goes through
        # float here (CLAUDE.md), and this also avoids banker's rounding
        # deciding the exact-half case (cumulative % 588 == 294).
        bounds.append(
            (cumulative * 2 + SAMPLES_PER_CD_FRAME) // (2 * SAMPLES_PER_CD_FRAME)
        )
    bounds[-1] = frames_for_samples(cumulative)  # ceil: never drop the tail

    durations = [bounds[i + 1] - bounds[i] for i in range(len(wav_files))]
    if any(d <= 0 for d in durations):
        short = [i for i, d in enumerate(durations, 1) if d <= 0]
        msg = f"track(s) {short} are shorter than one CD frame (1/75 s)"
        raise ValueError(msg)
    return durations, bounds[-1]


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


def _track_cdtext_title(
    track: RBITocEntry, raw_title: str | None
) -> tuple[str, str | None]:
    """Return (charset-safe CD-Text TITLE, Unicode-original to archive or None).

    The TITLE is folded to ASCII (:func:`fold_cdtext`); the pristine Unicode is
    preserved for FLAC extraction whenever the TOC would otherwise lose it — the
    caller's raw original if supplied, else the stored title when folding
    altered it (folding is the lossy step, so the archival comment must fire
    even when raw_titles was not supplied).
    """
    folded_title = (
        fold_cdtext(track.title, f"track {track.track_number} title")
        if track.title
        else ""
    )
    if raw_title and raw_title != track.title:
        return folded_title, raw_title
    if track.title and folded_title != track.title:
        return folded_title, track.title
    return folded_title, None


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
    album = sanitize_title(disc.album, "album")
    artist = sanitize_title(disc.artist, "artist")
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
        disc_id = escape_toc_string(fold_cdtext(disc.cdtext_catalog_ref, "disc_id"))
        disc_text_lines.append(f'    DISC_ID "{disc_id}"')

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
        # CD-Text TITLE must be charset-safe ASCII (one un-encodable character
        # makes cdrdao silently drop the whole lead-in); the pristine Unicode is
        # archived in the TRACK_TITLE_UNICODE comment for FLAC fidelity.
        raw_title = raw_titles[idx] if raw_titles and idx < len(raw_titles) else None
        folded_title, unicode_original = _track_cdtext_title(track, raw_title)
        unicode_lines = (
            [f"// TRACK_TITLE_UNICODE: {json.dumps(unicode_original)}"]
            if unicode_original
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
        if folded_title:
            track_cdtext_lines.append(f'    TITLE "{escape_toc_string(folded_title)}"')
        track_performer = sanitize_title(
            track.performer, "performer"
        ) or sanitize_title(disc.artist, "artist")
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
