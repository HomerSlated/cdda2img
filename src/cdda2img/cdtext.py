"""CD-Text pack decoder for raw READ TOC/PMA/ATIP format-0x05 responses.

Input is the verbatim response ``c2read --cdtext`` dumps: a 4-byte TOC response
header followed by 18-byte CD-Text packs (some drives strip the trailing CRC,
giving 16-byte packs — both strides are handled). Each pack::

    [0] PTI (pack type, 0x80..0x8F)
    [1] track number (bits 0-6) | extension flag (bit 7)
    [2] sequence number within the block
    [3] double-byte flag (bit 7) | block number (bits 4-6) | char position (0-3)
    [4:16] 12 payload bytes
    [16:18] CRC-16 (poly x^16+x^12+x^5+1, init 0, output inverted — the same
            algorithm as the subchannel Q CRC, so :func:`subchannel.crc16_gsm`)

Text PTIs are NUL-separated strings spanning pack boundaries: concatenate every
payload of one (block, PTI) group in sequence order, split on NUL, and assign
successive strings to successive tracks starting from the first pack's track
number (track 0 = disc level). A string of a single TAB means "same as the
previous track". References: libmirage ``cdtext-coder.c``, cdrdao
``CdrDriver::readCdTextData``, MMC-3 Annex J.

Only language block 0 in a single-byte Latin-ish charset is decoded (ISO 8859-1
or ASCII); MS-JIS / double-byte blocks are counted and skipped — none exist in
the local collection, and wrong-charset text is worse than none.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from cdda2img.subchannel import crc16_gsm

log = logging.getLogger(__name__)

PTI_TITLE = 0x80
PTI_PERFORMER = 0x81
PTI_SONGWRITER = 0x82
PTI_COMPOSER = 0x83
PTI_ARRANGER = 0x84
PTI_MESSAGE = 0x85
PTI_DISC_ID = 0x86
PTI_GENRE = 0x87
PTI_UPC_ISRC = 0x8E
PTI_SIZE_INFO = 0x8F

# PTIs whose payloads are NUL-separated per-track text strings.
_TEXT_PTIS = frozenset((
    PTI_TITLE,
    PTI_PERFORMER,
    PTI_SONGWRITER,
    PTI_COMPOSER,
    PTI_ARRANGER,
    PTI_MESSAGE,
    PTI_DISC_ID,
    PTI_UPC_ISRC,
))

_CHARSET_ISO_8859_1 = 0x00
_CHARSET_ASCII = 0x01

_PACK_LEN = 18
_PACK_LEN_NOCRC = 16
_HEADER_LEN = 4
_PAYLOAD = slice(4, 16)


@dataclass
class CDTextBlock:
    """One decoded CD-Text language block.

    *text* maps PTI -> track number -> string (track 0 = disc level). The
    convenience accessors cover what the rip pipeline consumes; everything
    else stays reachable through *text*.
    """

    block: int
    charset: int = _CHARSET_ISO_8859_1
    language_code: int | None = None
    first_track: int | None = None
    last_track: int | None = None
    text: dict[int, dict[int, str]] = field(default_factory=dict)

    @property
    def album_title(self) -> str | None:
        return self.text.get(PTI_TITLE, {}).get(0)

    @property
    def album_performer(self) -> str | None:
        return self.text.get(PTI_PERFORMER, {}).get(0)

    def track_title(self, track: int) -> str | None:
        return self.text.get(PTI_TITLE, {}).get(track)

    def track_performer(self, track: int) -> str | None:
        return self.text.get(PTI_PERFORMER, {}).get(track)

    @property
    def disc_id(self) -> str | None:
        """PTI 0x86 — the label's catalogue *string* (not the numeric MCN)."""
        return self.text.get(PTI_DISC_ID, {}).get(0)

    @property
    def mcn(self) -> str | None:
        """PTI 0x8E at disc level — a convenience copy only; Q Mode 2 is
        authoritative (CLAUDE.md CD-Text table)."""
        return self.text.get(PTI_UPC_ISRC, {}).get(0)

    def isrc(self, track: int) -> str | None:
        """PTI 0x8E at track level — convenience copy; Q Mode 3 is authoritative."""
        return self.text.get(PTI_UPC_ISRC, {}).get(track)


def _split_packs(raw: bytes) -> tuple[list[bytes], int]:
    """Split the response body into packs, detecting the 18/16-byte stride.

    Returns (packs, dropped) where dropped counts CRC-failing 18-byte packs.
    Stride ambiguity (length divisible by both) is resolved by CRC score.
    """
    body = raw[_HEADER_LEN:]
    fits18 = len(body) % _PACK_LEN == 0
    fits16 = len(body) % _PACK_LEN_NOCRC == 0
    if not fits18 and not fits16:
        msg = f"CD-Text body of {len(body)} bytes fits neither 18- nor 16-byte packs"
        raise ValueError(msg)

    use18 = fits18
    if fits18 and fits16 and len(body):
        # Ambiguous (multiple of 144): count CRC passes on the 18-byte reading.
        packs18 = [body[i : i + _PACK_LEN] for i in range(0, len(body), _PACK_LEN)]
        good = sum(
            1
            for p in packs18
            if crc16_gsm(p[:_PACK_LEN_NOCRC]) == int.from_bytes(p[16:18], "big")
        )
        use18 = good >= len(packs18) // 2

    if not use18:
        return [
            body[i : i + _PACK_LEN_NOCRC] for i in range(0, len(body), _PACK_LEN_NOCRC)
        ], 0

    packs: list[bytes] = []
    dropped = 0
    for i in range(0, len(body), _PACK_LEN):
        p = body[i : i + _PACK_LEN]
        stored = int.from_bytes(p[16:18], "big")
        if stored and crc16_gsm(p[:_PACK_LEN_NOCRC]) != stored:
            dropped += 1
            continue
        packs.append(p)
    return packs, dropped


def _apply_size_info(blocks: dict[int, CDTextBlock], packs: list[bytes]) -> None:
    """Decode the 3-pack SIZE_INFO (0x8F) payload per block: charset, track
    range, and the per-block language codes (bytes 28-35 of the 36)."""
    by_block: dict[int, list[bytes]] = {}
    for p in packs:
        if p[0] == PTI_SIZE_INFO:
            by_block.setdefault((p[3] >> 4) & 0x7, []).append(p)
    for blk, group in by_block.items():
        if len(group) < 3:
            continue
        info = b"".join(p[_PAYLOAD] for p in sorted(group, key=lambda p: p[2]))[:36]
        langs = info[28:36]
        for b, code in enumerate(langs):
            if code and b in blocks:
                blocks[b].language_code = code
        if blk in blocks:
            blocks[blk].charset = info[0]
            blocks[blk].first_track = info[1]
            blocks[blk].last_track = info[2]


def parse_cdtext(raw: bytes) -> list[CDTextBlock]:
    """Decode a raw format-0x05 response into per-language CD-Text blocks.

    Double-byte blocks and non-Latin charsets are skipped with a warning.
    Returns blocks sorted by block number; empty list when nothing decodes.
    """
    packs, dropped = _split_packs(raw)
    if dropped:
        log.warning("CD-Text: dropped %d packs with bad CRC", dropped)

    blocks: dict[int, CDTextBlock] = {}
    groups: dict[tuple[int, int], list[bytes]] = {}
    double_byte: set[int] = set()
    for p in packs:
        blk = (p[3] >> 4) & 0x7
        if p[0] == PTI_SIZE_INFO:
            continue
        blocks.setdefault(blk, CDTextBlock(block=blk))
        if p[3] & 0x80:
            double_byte.add(blk)
            continue
        if p[0] in _TEXT_PTIS:
            groups.setdefault((blk, p[0]), []).append(p)

    _apply_size_info(blocks, packs)

    for blk in sorted(double_byte):
        log.warning("CD-Text: block %d uses double-byte characters — skipped", blk)

    for (blk, pti), group in sorted(groups.items()):
        block = blocks[blk]
        if blk in double_byte:
            continue
        if block.charset not in (_CHARSET_ISO_8859_1, _CHARSET_ASCII):
            log.warning(
                "CD-Text: block %d charset 0x%02x unsupported — skipped",
                blk,
                block.charset,
            )
            continue
        block.text[pti] = _decode_strings(group)

    return [blocks[b] for b in sorted(blocks) if blocks[b].text]


def _decode_strings(group: list[bytes]) -> dict[int, str]:
    """Reassemble one (block, PTI) group into per-track strings."""
    group = sorted(group, key=lambda p: p[2])  # sequence order
    first_track = group[0][1] & 0x7F
    data = b"".join(p[_PAYLOAD] for p in group)

    out: dict[int, str] = {}
    track = first_track
    for chunk in data.split(b"\x00"):
        if track > 99:
            break
        text = chunk.decode("latin-1")
        if text == "\t":
            # TAB shorthand: same as the previous track's string.
            text = out.get(track - 1, "")
        if text:
            out[track] = text
        track += 1
    return out
