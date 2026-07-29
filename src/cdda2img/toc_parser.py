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
    pre_emphasis: bool = False  # per-track Q CONTROL 0x1 (rbi_spec §6.1.10)
    copy_permitted: bool = False  # per-track Q CONTROL 0x2 (rbi_spec §6.1.10)
    index_points: list[int] = field(default_factory=list)  # INDEX >= 02 offsets,
    # frames relative to the audio start, ascending (rbi_spec §6.1.10)

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
    # R14: True if any track block contains PRE_EMPHASIS (CONTROL bit 0).
    # NO PRE_EMPHASIS or absence → False. None is reserved for
    # non-cdrdao parsers that don't propagate this signal.
    pre_emphasis: bool | None = None


_CATALOG_RE = re.compile(r'CATALOG\s+"([^"]+)"')
_TITLE_RE = re.compile(r'TITLE\s+"([^"]*)"')
_PERFORMER_RE = re.compile(r'PERFORMER\s+"([^"]*)"')
_DISC_ID_RE = re.compile(r'DISC_ID\s+"([^"]*)"')
_ISRC_RE = re.compile(r'ISRC\s+"([^"]+)"')
_FILE_TS_RE = re.compile(
    r'FILE\s+"[^"]+"\s+(0|\d{2}:\d{2}:\d{2})\s+(\d{2}:\d{2}:\d{2})'
)
_START_RE = re.compile(r"^\s*START\s+(\d{2}:\d{2}:\d{2})", re.MULTILINE)
# SILENCE/ZERO directives that precede the FILE line are synthetic — not stored
# in the audio file. read-cd embeds them as real PCM in the BIN (so FILE offsets
# are disc positions); read-toc emits them as directives, leaving FILE offsets as
# audio-only positions. We track these to adjust start_frame and slot_frames.
_SILENCE_ZERO_RE = re.compile(
    r"^\s*(?:SILENCE|ZERO)\s+(\d{2}:\d{2}:\d{2})", re.MULTILINE
)
_TRACK_MARKER_RE = re.compile(r"^//\s*Track\s+(\d+)", re.MULTILINE)
_TITLE_UNICODE_RE = re.compile(r"^//\s*TRACK_TITLE_UNICODE:\s*(.+)$", re.MULTILINE)
# R14: matches a bare "PRE_EMPHASIS" line. The "NO PRE_EMPHASIS" form is
# treated as the negation — handled separately so we don't false-match.
_PRE_EMPH_RE = re.compile(r"^\s*PRE_EMPHASIS\s*$", re.MULTILINE)
_NO_PRE_EMPH_RE = re.compile(r"^\s*NO\s+PRE_EMPHASIS\s*$", re.MULTILINE)
# Same bare/negated pair for the digital-copy-permitted flag (rbi_spec §6.1.10).
_COPY_RE = re.compile(r"^\s*COPY\s*$", re.MULTILINE)
_NO_COPY_RE = re.compile(r"^\s*NO\s+COPY\s*$", re.MULTILINE)
# INDEX >= 02 points: offsets relative to the audio start (rbi_spec §6.1.10).
_INDEX_RE = re.compile(r"^\s*INDEX\s+(\d{2}:\d{2}:\d{2})", re.MULTILINE)

_ALL_ZEROS_MCN = "0000000000000"


def _first(pattern: re.Pattern[str], masked: str, raw: str, default: str = "") -> str:
    """First capture of *pattern*, located in *masked* and read from *raw*.

    Two texts, one offset space. The keyword has to be found in the masked view
    or a ``TITLE "..."`` sitting inside somebody else's quoted string counts as a
    field; the value has to be read from the raw text because masking is exactly
    what blanked it. :func:`mask_quoted` preserves length, so one match position
    indexes both.
    """
    m = pattern.search(masked)
    return raw[m.start(1) : m.end(1)] if m else default


def _first_or_none(pattern: re.Pattern[str], masked: str, raw: str) -> str | None:
    m = pattern.search(masked)
    return raw[m.start(1) : m.end(1)] if m else None


class TocParseError(ValueError):
    """A TOC that cannot be parsed safely. Refused rather than half-understood."""


def mask_quoted(text: str) -> str:
    """Blank the *interiors* of ``"..."`` strings, preserving length and offsets.

    The structural patterns here are line-anchored with ``re.MULTILINE`` and have
    no idea whether a line sits inside a quoted string. So a foreign TOC whose
    ``TITLE`` value contains a newline followed by ``START 00:02:00`` gets that
    line read as a real directive — the parser cannot tell an attacker's payload
    from the file's own structure. Only the ``import`` subcommand is exposed
    (nothing we write ever contains one, and ``escape_toc_string`` guards the
    write side), but "we only feed it our own files" is a property of today's
    callers, not of the parser.

    Masking rather than stripping is deliberate: **every offset must survive.**
    ``parse_toc`` slices per-track blocks by the byte positions of the ``// Track``
    markers, so a mask that changed length would silently mis-slice every track —
    trading an injection bug for a corruption bug.

    A newline inside an open quote is a **hard error**, not something to mask
    around. cdrdao strings do not span lines, so an unterminated one means the
    file is either malformed or hostile, and in both cases the honest answer is
    to refuse. Guessing where the author "meant" to close it is how a parser ends
    up with two readings of the same bytes.

    ``\\`` escapes are honoured (cdrdao's lexer treats ``\\"`` as a literal quote),
    or a value ending in a backslash would appear to swallow its own delimiter.
    """
    out = list(text)
    base = 0
    for lineno, line in enumerate(text.split("\n"), start=1):
        _mask_line(line, base, out, lineno)
        base += len(line) + 1  # + the newline that split() consumed
    return "".join(out)


def _mask_line(line: str, base: int, out: list[str], lineno: int) -> None:
    """Blank quoted interiors of one line, in place, at offset *base* in *out*.

    Per line rather than per file because a cdrdao string cannot span one — so
    "unterminated at end of line" and "unterminated at end of file" are the same
    condition, and the scanner needs no state that outlives a line.
    """
    in_string = escaped = False
    for j, ch in enumerate(line):
        if escaped:
            escaped = False
            out[base + j] = " "
        elif in_string and ch == "\\":
            escaped = True
            out[base + j] = " "
        elif not in_string and line[j : j + 2] == "//":
            # A `//` comment runs to end-of-line and cannot contain a TOC string,
            # so its quotes are not delimiters. Not a convenience: our own
            # `// TRACK_TITLE_UNICODE:` lines carry JSON, which is full of them,
            # and a hand-edited comment with a lone quote would otherwise flip
            # `in_string` and refuse a well-formed file. parse_toc is on the
            # extract/list/test path, not just import, so a false refusal there
            # is a regression on our own archives.
            return
        elif ch == '"':
            in_string = not in_string
        elif in_string:
            out[base + j] = " "
    if in_string:
        msg = (
            f"TOC line {lineno}: unterminated string — a quoted value may not span "
            f"lines. Refusing to parse rather than guess where it ends."
        )
        raise TocParseError(msg)


def parse_toc(toc_bytes: bytes) -> ParsedDisc:
    """Parse cdrdao-format TOC bytes and return disc/track metadata.

    Raises :class:`TocParseError` on a TOC whose quoting is unparseable — see
    :func:`mask_quoted`. Structure is read from a masked view; string *values*
    come from the original text, since the mask blanks exactly what they want.
    """
    text = toc_bytes.decode("utf-8")
    # Structure comes from here; values from `text`. Same length, same offsets,
    # so a match position in one indexes the other.
    masked = mask_quoted(text)
    markers = list(_TRACK_MARKER_RE.finditer(masked))

    cut = markers[0].start() if markers else len(text)
    disc_raw, disc_masked = text[:cut], masked[:cut]
    disc_title = _first(_TITLE_RE, disc_masked, disc_raw)
    disc_performer = _first(_PERFORMER_RE, disc_masked, disc_raw)

    catalog_raw = _first_or_none(_CATALOG_RE, disc_masked, disc_raw)
    catalog = catalog_raw if catalog_raw and catalog_raw != _ALL_ZEROS_MCN else None
    disc_id = _first_or_none(_DISC_ID_RE, disc_masked, disc_raw)

    tracks = []
    any_pre_emph = False
    # Running total of SILENCE/ZERO frames NOT stored in the audio file.
    # read-toc emits these as directives; read-cd embeds them as real PCM
    # in the BIN so FILE offsets already include them. This accumulator
    # corrects start_frame and slot_frames for the read-toc case.
    cumulative_out_of_file_silence = 0
    for i, marker in enumerate(markers):
        block_end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        raw_block = text[marker.start() : block_end]
        # Structure is read from `block` (masked), values from `raw_block`. Same
        # slice bounds, so a match in one indexes the other.
        block = masked[marker.start() : block_end]
        # R14: track-level PRE_EMPHASIS aggregated to disc level. The
        # presence of "NO PRE_EMPHASIS" must not false-match because of
        # the trailing "PRE_EMPHASIS" — match against the cleaned block.
        cleaned = _NO_PRE_EMPH_RE.sub("", block)
        track_pre_emph = bool(_PRE_EMPH_RE.search(cleaned))
        if track_pre_emph:
            any_pre_emph = True
        track_copy = bool(_COPY_RE.search(_NO_COPY_RE.sub("", block)))

        file_m = _FILE_TS_RE.search(block)
        if not file_m:
            continue

        # The unicode title is a `//` COMMENT carrying JSON, not a quoted TOC
        # string, so it survives masking untouched and is read from the raw block.
        unicode_m = _TITLE_UNICODE_RE.search(raw_block)
        if unicode_m:
            try:
                track_title = json.loads(unicode_m.group(1))
            except (json.JSONDecodeError, ValueError):
                track_title = _first(_TITLE_RE, block, raw_block)
        else:
            track_title = _first(_TITLE_RE, block, raw_block)

        # SILENCE/ZERO frames before the FILE line are synthetic (not in the
        # audio file). Add them to this track's slot and to the accumulator
        # so all subsequent tracks' start_frame values are shifted correctly.
        file_pos_in_block = file_m.start()
        silence_in_block = sum(
            frames_from_timestamp(sm.group(1))
            for sm in _SILENCE_ZERO_RE.finditer(block)
            if sm.start() < file_pos_in_block
        )

        file_start = (
            0 if file_m.group(1) == "0" else frames_from_timestamp(file_m.group(1))
        )
        start_frame = file_start + cumulative_out_of_file_silence
        # slot_frames includes the synthetic silence so total_frames reflects
        # actual disc space (needed for the MB disc-ID lead-out offset).
        slot_frames = frames_from_timestamp(file_m.group(2)) + silence_in_block
        start_m = _START_RE.search(block)
        pregap_frames = frames_from_timestamp(start_m.group(1)) if start_m else 0
        duration_frames = slot_frames - pregap_frames

        tracks.append(
            ParsedTrack(
                track_number=int(marker.group(1)),
                title=track_title,
                performer=_first(_PERFORMER_RE, block, raw_block, disc_performer),
                start_frame=start_frame,
                duration_frames=duration_frames,
                pregap_frames=pregap_frames,
                isrc=_first_or_none(_ISRC_RE, block, raw_block),
                pre_emphasis=track_pre_emph,
                copy_permitted=track_copy,
                index_points=[
                    frames_from_timestamp(im.group(1))
                    for im in _INDEX_RE.finditer(block)
                ],
            )
        )
        cumulative_out_of_file_silence += silence_in_block

    return ParsedDisc(
        title=disc_title,
        performer=disc_performer,
        catalog=catalog,
        disc_id=disc_id,
        tracks=tracks,
        pre_emphasis=any_pre_emph if tracks else None,
    )
