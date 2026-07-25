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


def _pack_owners(first_track: int, strings: list[bytes]) -> list[int]:
    """Per-payload-byte track number, matching how real discs fill pack byte [1].

    A pack's byte [1] names the track whose string is in progress at that pack's
    *first payload byte* — so continuation packs restate the current track, they
    do not carry 0. Verified against a real PX-716A capture, whose TITLE headers
    run ``0 0 1 2 2 3 3 4 5 5 …``. A string's terminating NUL belongs to the
    string it terminates.
    """
    owners: list[int] = []
    track = first_track
    for s in strings:
        owners.extend([track] * (len(s) + 1))  # bytes + terminating NUL
        track += 1
    return owners


def _text_packs(
    pti: int, first_track: int, strings: list[str], *, block: int = 0, seq0: int = 0
) -> list[bytes]:
    return _text_packs_bytes(
        pti,
        first_track,
        [s.encode("latin-1") for s in strings],
        block=block,
        seq0=seq0,
    )


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


def test_utf8_payload_decoded_utf8_first():
    """cdrdao/CDEmu-authored discs carry raw UTF-8 despite declaring charset 0x00:
    a U+2019 apostrophe (bytes E2 80 99) must decode as UTF-8, not as the Latin-1
    mojibake 'Talkin\\xe2\\x80\\x99'."""
    packs = _text_packs_bytes(PTI_TITLE, 1, [b"Talkin\xe2\x80\x99 Bout"])
    blocks = parse_cdtext(_HDR + b"".join(packs))
    assert blocks[0].track_title(1) == "Talkin\u2019 Bout"


def test_latin1_payload_still_decodes():
    """Genuine Latin-1 bytes (invalid as UTF-8) take the fallback unchanged."""
    packs = _text_packs_bytes(PTI_TITLE, 1, [b"Caf\xe9 del Mar"])  # lone 0xE9 = é
    blocks = parse_cdtext(_HDR + b"".join(packs))
    assert blocks[0].track_title(1) == "Café del Mar"


def _text_packs_bytes(
    pti: int,
    first_track: int,
    strings: list[bytes],
    *,
    block: int = 0,
    seq0: int = 0,
) -> list[bytes]:
    payload = b"\x00".join(strings) + b"\x00"
    owners = _pack_owners(first_track, strings)
    return [
        _pack(pti, owners[i], seq0 + i // 12, block, payload[i : i + 12])
        for i in range(0, len(payload), 12)
    ]


def test_real_cdemu_capture_utf8_titles():
    """Real capture from a CDEmu-mounted RBI (2026-07-05): UTF-8 CD-Text that
    previously baked mojibake into the ripped RBI's titles."""
    from pathlib import Path

    raw = (Path(__file__).parent / "fixtures" / "cdemu_utf8.cdtext").read_bytes()
    blocks = parse_cdtext(raw)
    assert blocks, "fixture produced no CD-Text blocks"
    b = blocks[0]
    assert b.track_title(1) == "Talkin\u2019 Bout a Revolution"
    assert b.track_title(6) == "Mountains o\u2019 Things"
    assert b.track_title(7) == "She\u2019s Got Her Ticket"
    assert b.track_title(10) == "If Not Now\u2026"
    assert b.track_title(11) == "For You"


def test_malformed_length_raises():
    with pytest.raises(ValueError, match="neither"):
        parse_cdtext(_HDR + b"\x00" * 17)


# ── a track carrying no string at all (2026-07-24) ───────────────────────────
#
# ABBA *Gold* encodes 18 TITLE strings for 19 tracks: track 13 has no title AND
# no empty-string placeholder. The gap is visible only in the per-pack track
# number, and on that disc the correction lands MID-STRING — so a decoder that
# counts NULs, or that resyncs only at string boundaries, mis-titles the disc
# (differently in each case). Shape reproduced synthetically below; the real
# 760-byte capture stays out of the repo (commercial pressing).


def test_track_with_no_string_does_not_shift_the_rest():
    payload = b"One\x00Two\x00Three\x00"
    packs = [
        # "One\x00Two\x00Thr" — opens on track 1's string.
        _pack(PTI_TITLE, 1, 0, 0, payload[0:12]),
        # "ee\x00" — opens MID-"Three", and its header says that string is
        # track 4's. Track 3 carries nothing at all.
        _pack(PTI_TITLE, 4, 1, 0, payload[12:]),
    ]
    (b,) = parse_cdtext(_HDR + b"".join(packs))
    assert b.track_title(1) == "One"
    assert b.track_title(2) == "Two"
    assert b.track_title(3) is None, "the gap must be preserved, not closed up"
    assert b.track_title(4) == "Three", "a mid-string header must be honoured"
    assert b.track_title(5) is None, "the tail must not be duplicated"


def test_continuation_pack_header_overrides_the_running_count():
    """Even with no gap, byte [1] — not the NUL count — decides placement."""
    payload = b"Alpha\x00Beta\x00"
    packs = [
        _pack(PTI_TITLE, 7, 0, 0, payload[0:12]),
        _pack(PTI_TITLE, 8, 1, 0, payload[12:]),
    ]
    (b,) = parse_cdtext(_HDR + b"".join(packs))
    assert b.track_title(7) == "Alpha"
    assert b.track_title(8) == "Beta"
