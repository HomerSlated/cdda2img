"""CD-Text pack decoder tests (synthetic packs, CRC-exact)."""

from __future__ import annotations

import pytest

from cdda2img.cdtext import (
    PTI_PERFORMER,
    PTI_SIZE_INFO,
    PTI_TITLE,
    PTI_UPC_ISRC,
    parse_cdtext,
)
from cdda2img.subchannel import crc16_gsm

_HDR = b"\x00\x00\x00\x00"  # 4-byte TOC response header (content ignored)


def _pack(
    pti: int,
    track: int,
    seq: int,
    block: int,
    data: bytes,
    *,
    dbc: bool = False,
    with_crc: bool = True,
    corrupt_crc: bool = False,
) -> bytes:
    head = bytes([pti, track, seq, (0x80 if dbc else 0) | (block << 4)])
    p = head + data.ljust(12, b"\x00")[:12]
    if not with_crc:
        return p
    crc = crc16_gsm(p)
    if corrupt_crc:
        crc ^= 0xFFFF
    return p + bytes([crc >> 8, crc & 0xFF])


def _text_packs(
    pti: int, first_track: int, strings: list[str], *, block: int = 0, seq0: int = 0
) -> list[bytes]:
    payload = b"\x00".join(s.encode("latin-1") for s in strings) + b"\x00"
    return [
        _pack(
            pti,
            first_track if i == 0 else 0,
            seq0 + i // 12,
            block,
            payload[i : i + 12],
        )
        for i in range(0, len(payload), 12)
    ]


def test_album_and_track_titles_span_packs():
    packs = _text_packs(PTI_TITLE, 0, ["A Long Album Title", "First Track", "Second"])
    blocks = parse_cdtext(_HDR + b"".join(packs))
    (b,) = blocks
    assert b.album_title == "A Long Album Title"
    assert b.track_title(1) == "First Track"
    assert b.track_title(2) == "Second"


def test_tab_means_same_as_previous_track():
    packs = _text_packs(PTI_PERFORMER, 0, ["Band", "Band Solo", "\t"])
    (b,) = parse_cdtext(_HDR + b"".join(packs))
    assert b.album_performer == "Band"
    assert b.track_performer(1) == "Band Solo"
    assert b.track_performer(2) == "Band Solo"


def test_upc_isrc_pti():
    packs = _text_packs(PTI_UPC_ISRC, 0, ["7559607740206", "USEE10400001"])
    (b,) = parse_cdtext(_HDR + b"".join(packs))
    assert b.mcn == "7559607740206"
    assert b.isrc(1) == "USEE10400001"


def test_16_byte_stride_without_crc():
    packs = [
        p[:16] for p in _text_packs(PTI_TITLE, 0, ["Album", "Track One", "Track Two"])
    ]
    (b,) = parse_cdtext(_HDR + b"".join(packs))
    assert b.album_title == "Album"
    assert b.track_title(2) == "Track Two"


def test_crc_failing_pack_dropped():
    good = _text_packs(PTI_TITLE, 0, ["Album"])
    bad = [_pack(PTI_PERFORMER, 0, 0, 0, b"Ghost", corrupt_crc=True)]
    (b,) = parse_cdtext(_HDR + b"".join(good + bad))
    assert b.album_title == "Album"
    assert b.album_performer is None  # the corrupt pack must not contribute


def test_double_byte_block_skipped():
    packs = [_pack(PTI_TITLE, 0, 0, 0, b"\x82\xa0\x00", dbc=True)]
    assert parse_cdtext(_HDR + b"".join(packs)) == []


def test_size_info_sets_charset_and_language():
    text = _text_packs(PTI_TITLE, 0, ["Album"])
    info = bytearray(36)
    info[0] = 0x00  # ISO 8859-1
    info[1], info[2] = 1, 2  # first/last track
    info[28] = 0x09  # language code (block 0): English
    size = [
        _pack(PTI_SIZE_INFO, i, i, 0, bytes(info[i * 12 : (i + 1) * 12]))
        for i in range(3)
    ]
    (b,) = parse_cdtext(_HDR + b"".join(text + size))
    assert b.charset == 0x00
    assert b.language_code == 0x09
    assert (b.first_track, b.last_track) == (1, 2)


def test_unsupported_charset_block_skipped():
    text = _text_packs(PTI_TITLE, 0, ["Album"])
    info = bytearray(36)
    info[0] = 0x80  # MS-JIS
    size = [
        _pack(PTI_SIZE_INFO, i, i, 0, bytes(info[i * 12 : (i + 1) * 12]))
        for i in range(3)
    ]
    assert parse_cdtext(_HDR + b"".join(text + size)) == []


def test_malformed_length_raises():
    with pytest.raises(ValueError, match="neither"):
        parse_cdtext(_HDR + b"\x00" * 17)
