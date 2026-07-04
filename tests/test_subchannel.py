"""Tests for the CD subchannel Q-channel decoder (:mod:`cdda2img.subchannel`).

The real-data fixtures are raw 96-byte subcode sectors lifted verbatim from a
PX-716A ``redumper dump`` of *American Idiot*. They are committed as hex so the
decode is validated against genuine disc bytes (non-circular) without depending
on the multi-hundred-MB capture, which is gitignored under ``rips/``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cdda2img.subchannel import (
    ADR_ISRC,
    ADR_MCN,
    ADR_POSITION,
    CD_SUBCODE_SIZE,
    crc16_gsm,
    decode_q,
    derive_track_layout,
    extract_q,
    parse_fulltoc,
    parse_fulltoc_leadout,
    scan_subcode,
    session1_audio_tracks,
)

_FIXTURES = Path(__file__).parent / "fixtures"

# --- real-disc fixtures (raw 96-byte subcode sectors) -----------------------

# Program-area Q-mode-2 frame: decodes to MCN 0093624877721.
MCN_SECTOR = bytes.fromhex(
    "000000000000400000000000000000004000004000004040004040000000400000400000400000000040404000404040004040400000400000000040000000000000000000000000000000000040004000400040400040400000004000404040"
)
# Program-area Q-mode-1 frame for track 2: absolute MSF 02:56:24 -> LBA 13074.
POSITION_SECTOR = bytes.fromhex(
    "80808080808080c0808080808080c08080808080808080c08080808080808080808080808080808080808080808080808080808080808080808080808080c08080c080c080c0c0808080c08080c08080c080c0c0c0c0c0c0c08080c0808080c0"
)

# Program-area Q-mode-3 frame for track 1: ISRC US-RE1-04-00888 (Reprise, 2004).
ISRC_SECTOR = bytes.fromhex(
    "808080808080c0c0c08080c080c0c0808080c0c0c0808080c08080c080c080c08080808080c080808080808080c080808080808080808080c0808080c0808080c0808080808080808080808080c080c0808080c08080c0c0c080c0c080c0c080"
)

EXPECTED_MCN = "0093624877721"
EXPECTED_ISRC = "USRE10400888"


# --- CRC-16/GSM -------------------------------------------------------------


def test_crc16_gsm_check_value():
    # Canonical CRC-16/GSM check value for the ASCII string "123456789".
    assert crc16_gsm(b"123456789") == 0xCE3C


# --- Q extraction + decode on real disc bytes -------------------------------


def test_extract_q_length():
    q = extract_q(MCN_SECTOR)
    assert len(q) == 12


def test_real_mcn_frame_decodes_and_validates():
    q = decode_q(MCN_SECTOR)
    assert q.valid  # CRC over real bytes must check out
    assert q.adr == ADR_MCN
    assert q.mcn() == EXPECTED_MCN


def test_real_position_frame_lba_and_track():
    q = decode_q(POSITION_SECTOR)
    assert q.valid
    assert q.adr == ADR_POSITION
    assert q.track_number == 2
    assert q.position_lba() == 13074  # matches the redumper TOC track-2 start


def test_position_lba_none_for_non_position_frame():
    # An MCN frame is ADR=2, so it has no running position.
    assert decode_q(MCN_SECTOR).position_lba() is None


def test_mcn_none_for_non_mcn_frame():
    assert decode_q(POSITION_SECTOR).mcn() is None


def test_real_isrc_frame_decodes_and_validates():
    q = decode_q(ISRC_SECTOR)
    assert q.valid
    assert q.adr == ADR_ISRC
    # "RE1" is Reprise Records' registrant — never fed in (non-circular).
    assert q.isrc() == EXPECTED_ISRC


def test_isrc_none_for_non_isrc_frame():
    assert decode_q(MCN_SECTOR).isrc() is None
    assert decode_q(POSITION_SECTOR).isrc() is None


def test_decode_q_accepts_12_byte_frame():
    q12 = extract_q(MCN_SECTOR)
    assert decode_q(q12).mcn() == EXPECTED_MCN


# --- full TOC lead-out parse ------------------------------------------------


def test_parse_fulltoc_leadout():
    # Minimal raw READ-TOC format-2 response: header + A0/A1/A2 descriptors.
    # A2 (lead-out) PMIN/PSEC/PFRAME = 57:18:53 (binary) -> LBA 257753.
    header = bytes([0x00, 0xB2, 0x01, 0x01])
    a0 = bytes([0x01, 0x10, 0x00, 0xA0, 0, 0, 0, 0, 0x01, 0x00, 0x00])
    a1 = bytes([0x01, 0x10, 0x00, 0xA1, 0, 0, 0, 0, 0x0D, 0x00, 0x00])
    a2 = bytes([0x01, 0x10, 0x00, 0xA2, 0, 0, 0, 0, 0x39, 0x12, 0x35])
    assert parse_fulltoc_leadout(header + a0 + a1 + a2) == 257753


def test_parse_fulltoc_leadout_absent():
    header = bytes([0x00, 0x0B, 0x01, 0x01])
    a0 = bytes([0x01, 0x10, 0x00, 0xA0, 0, 0, 0, 0, 0x01, 0x00, 0x00])
    assert parse_fulltoc_leadout(header + a0) is None


def _ftd(
    session: int, point: int, pmsf: tuple[int, int, int], *, control: int = 0
) -> bytes:
    """One raw-TOC descriptor (ADR=1)."""
    return bytes([session, 0x10 | control, 0x00, point, 0, 0, 0, 0, *pmsf])


def test_parse_fulltoc_real_capture():
    # c2read --fulltoc capture of the 11-track Tracy Chapman disc (PX-716A).
    raw = (_FIXTURES / "tracy.fulltoc").read_bytes()
    toc = parse_fulltoc(raw)
    assert toc.n_sessions == 1
    assert (toc.first_track[1], toc.last_track[1]) == (1, 11)
    assert toc.leadouts == {1: 162892}
    assert toc.disc_type == 0x00
    assert len(toc.tracks) == 11
    assert toc.tracks[0].start_lba == 0
    assert toc.tracks[1].start_lba == 12032  # matches READ TOC format 0
    assert all(not t.is_data for t in toc.tracks)

    tracks, leadout = session1_audio_tracks(toc)
    assert len(tracks) == 11
    assert leadout == 162892


def test_session1_audio_excludes_enhanced_cd_data():
    hdr = bytes([0x00, 0x00, 0x01, 0x02])
    body = (
        _ftd(1, 0xA0, (1, 0x00, 0))
        + _ftd(1, 0xA1, (2, 0, 0))
        + _ftd(1, 0xA2, (10, 0, 0))
        + _ftd(1, 1, (0, 2, 0))
        + _ftd(1, 2, (5, 2, 0))
        + _ftd(2, 0xA2, (50, 0, 0))
        + _ftd(2, 3, (40, 0, 0), control=0x4)  # session-2 data track
    )
    toc = parse_fulltoc(hdr + body)
    assert toc.n_sessions == 2
    tracks, leadout = session1_audio_tracks(toc)
    assert [t.track for t in tracks] == [1, 2]
    assert leadout == (10 * 60) * 75 - 150


def test_session1_mixed_mode_refused():
    hdr = bytes([0x00, 0x00, 0x01, 0x01])
    body = (
        _ftd(1, 0xA2, (10, 0, 0))
        + _ftd(1, 1, (0, 2, 0))
        + _ftd(1, 2, (5, 0, 0), control=0x4)  # data track INSIDE session 1
    )
    with pytest.raises(ValueError, match="mixed-mode"):
        session1_audio_tracks(parse_fulltoc(hdr + body))


# --- synthetic attribution (deterministic bucketing) ------------------------


def _pack_q(q: bytes) -> bytes:
    """Inverse of extract_q: spread a 12-byte Q frame into bit 6 of 96 bytes."""
    sector = bytearray(CD_SUBCODE_SIZE)
    for i in range(CD_SUBCODE_SIZE):
        if q[i >> 3] & (1 << (7 - (i & 7))):
            sector[i] |= 0x40
    return bytes(sector)


def _make_q(adr: int, payload9: bytes, control: int = 0) -> bytes:
    """Build a valid 12-byte Q frame: [ctrl|adr] + 9 payload + CRC-16/GSM.

    *control* is the Q CONTROL nibble (0 = 2-channel audio, no flags; 0x1 =
    pre-emphasis, 0x2 = copy permitted — redumper cd/subcode.ixx Control).
    """
    assert len(payload9) == 9
    head = bytes([(control << 4) | adr]) + payload9
    crc = crc16_gsm(head)
    return head + bytes([crc >> 8, crc & 0xFF])


def _bcd_enc(v: int) -> int:
    return ((v // 10) << 4) | (v % 10)


def _position(
    track: int, lba: int, index: int = 1, control: int = 0, claim_lba: int | None = None
) -> bytes:
    """ADR=1 program frame. *claim_lba* forges the absolute-MSF position (for
    slip tests) while the frame still sits at *lba* in the stream. All numeric
    fields (track, index, MSF) are BCD, as on disc."""
    frames = (claim_lba if claim_lba is not None else lba) + 150
    amin, rem = divmod(frames, 60 * 75)
    asec, aframe = divmod(rem, 75)
    payload = bytes([
        _bcd_enc(track),
        _bcd_enc(index),
        0x00,
        0x00,
        0x00,
        0x00,
        _bcd_enc(amin),
        _bcd_enc(asec),
        _bcd_enc(aframe),
    ])
    return _pack_q(_make_q(ADR_POSITION, payload, control=control))


def _mcn(digits: str) -> bytes:
    nibbles = [int(c) for c in digits] + [0]  # 14 nibbles
    payload = bytes(
        (nibbles[2 * k] << 4) | nibbles[2 * k + 1] for k in range(7)
    ) + bytes([0x00, 0x00])
    return _pack_q(_make_q(ADR_MCN, payload))


def test_scan_attributes_mcn_to_leadin_and_program():
    # Lead-in run: track-0 position frame, then 2 MCN frames.
    # Program run: track-1 position frames, then 3 MCN frames.
    leadin_pos = _pack_q(
        _make_q(ADR_POSITION, bytes([0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0, 0, 0]))
    )
    sectors = [leadin_pos, _mcn("0093624877721"), _mcn("0093624877721")]
    sectors += [_position(1, 0), _position(1, 1)]
    sectors += [_mcn("0093624877721")] * 3
    scan = scan_subcode(b"".join(sectors))

    by_region = {(d.type, d.region): d for d in scan.data}
    assert by_region[("MCN", "lead-in")].count == 2
    assert by_region[("MCN", "lead-in")].value == "0093624877721"
    assert by_region[("MCN", "lead-in")].lba_min is None  # no running position
    assert by_region[("MCN", "program")].count == 3
    assert scan.base_lba == -3  # program LBA 0 is at file sector index 3
    assert scan.base_agreement == 1.0
    assert scan.invalid_q == 0


# --- ChannelQ.index ----------------------------------------------------------


def test_index_property():
    assert decode_q(_position(1, 100, index=0)).index == 0
    assert decode_q(_position(1, 100, index=1)).index == 1
    assert decode_q(_position(1, 100, index=12)).index == 12


def test_index_invalid_bcd():
    # Forge an index byte with a non-BCD nibble (0x1A).
    payload = bytes([0x01, 0x1A, 0, 0, 0, 0, 0, 2, 0])
    q = decode_q(_pack_q(_make_q(ADR_POSITION, payload)))
    assert q.index == -1


# --- MCN/ISRC majority voting (F4) ------------------------------------------


def _isrc(code: str) -> bytes:
    """ADR=3 frame for a 12-char ISRC (5 six-bit alnum + 7 BCD digits)."""
    table = "0123456789_______ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    bits = 0
    for ch in code[:5]:
        bits = (bits << 6) | table.index(ch)
    field = (bits << 2).to_bytes(4, "big")  # 30 bits + 2 pad
    digits = code[5:12] + "0"  # 7 digits + pad nibble
    field += bytes((int(digits[2 * k]) << 4) | int(digits[2 * k + 1]) for k in range(4))
    return _pack_q(_make_q(ADR_ISRC, field + b"\x00"))


def test_isrc_builder_roundtrip():
    assert decode_q(_isrc("USRE10400888")).isrc() == "USRE10400888"


def test_scan_vote_majority_wins_with_runner_up():
    sectors = [_position(1, 0), _position(1, 1)]
    sectors += [_mcn("0093624877721")] * 3 + [_mcn("1111111111111")] * 2
    scan = scan_subcode(b"".join(sectors))
    (d,) = [d for d in scan.data if d.type == "MCN"]
    assert d.value == "0093624877721"
    assert d.count == 5
    assert d.runner_up == ("1111111111111", 2)


def test_scan_vote_floor_single_observation():
    sectors = [_position(1, 0), _position(1, 1), _mcn("0093624877721")]
    scan = scan_subcode(b"".join(sectors))
    (d,) = [d for d in scan.data if d.type == "MCN"]
    assert d.value is None  # one observation never clears the >=2 floor
    assert d.count == 1


def test_scan_isrc_vote_rejects_invalid_shape():
    # "1SRE1..." decodes cleanly from 6-bit charset but fails ISO-3901 (the
    # country code must be alphabetic); the valid minority value must win.
    sectors = [_position(1, 0), _position(1, 1)]
    sectors += [_isrc("1SRE10400888")] * 3 + [_isrc("USRE10400888")] * 2
    scan = scan_subcode(b"".join(sectors))
    (d,) = [d for d in scan.data if d.type == "ISRC"]
    assert d.value == "USRE10400888"


# --- derive_track_layout (F3) ------------------------------------------------


def _stream(*segments: tuple[int, int, int, int]) -> bytes:
    """Contiguous Q stream from LBA 0: segments of (track, index, control, n)."""
    out: list[bytes] = []
    lba = 0
    for track, index, control, n in segments:
        for _ in range(n):
            out.append(_position(track, lba, index=index, control=control))
            lba += 1
    return b"".join(out)


def test_layout_pregap_detected():
    data = _stream((1, 1, 0, 10), (2, 0, 0, 5), (2, 1, 0, 10))
    layout = derive_track_layout(data, {1: 0, 2: 15}, 25)
    assert layout.pregap_frames == {2: 5}
    assert layout.frames_dropped_slip == 0
    assert layout.frames_used == 25


def test_layout_no_pregap():
    data = _stream((1, 1, 0, 10), (2, 1, 0, 10))
    layout = derive_track_layout(data, {1: 0, 2: 10}, 20)
    assert layout.pregap_frames == {}


def test_layout_pregap_floor_single_frame():
    data = _stream((1, 1, 0, 10), (2, 0, 0, 1), (2, 1, 0, 10))
    layout = derive_track_layout(data, {1: 0, 2: 11}, 21)
    assert layout.pregap_frames == {}  # one index-00 frame must not invent one


def test_layout_implausible_pregap_ignored():
    # An 800-frame apparent pre-gap (> 10 s cap) is distrust-and-drop.
    data = _stream((2, 0, 0, 3), (2, 1, 0, 5))
    layout = derive_track_layout(data, {2: 800}, 900)
    assert layout.pregap_frames == {}


def test_layout_slip_frames_dropped():
    good = _stream((1, 1, 0, 10))
    slipped = _position(1, 10, claim_lba=60)  # claims a position 50 ahead
    tail = b"".join(_position(1, lba) for lba in range(11, 16))
    layout = derive_track_layout(good + slipped + tail, {1: 0}, 16)
    assert layout.frames_dropped_slip == 1
    assert layout.frames_used == 15


def test_layout_index_points():
    data = _stream((1, 1, 0, 10), (1, 2, 0, 4), (1, 3, 0, 1), (2, 1, 0, 5))
    layout = derive_track_layout(data, {1: 0, 2: 15}, 20)
    # INDEX 02 seen 4x -> recorded at its first LBA; INDEX 03 seen once -> floor.
    assert layout.index_points == {1: [(2, 10)]}


def test_layout_control_majority():
    data = _stream((1, 1, 0x1, 9), (1, 1, 0x0, 1), (2, 1, 0x2, 5))
    layout = derive_track_layout(data, {1: 0, 2: 10}, 15)
    assert layout.control[1].pre_emphasis is True  # 9:1 majority
    assert layout.control[1].copy_permitted is False
    assert layout.control[2].copy_permitted is True
    assert layout.control[2].pre_emphasis is False


def test_layout_unanchorable_raises():
    data = _mcn("0093624877721") * 3
    with pytest.raises(ValueError, match="anchor"):
        derive_track_layout(data, {1: 0}, 10)


# --- real capture fixture (PX-716A, c2read --sub raw) ------------------------

# 300 sectors (LBA 11850..12149) of a c2read --sub raw capture of the Tracy
# Chapman disc, spanning the track 1 -> 2 boundary: a real 52-frame pre-gap
# (index 00 from LBA 11980, TOC start 12032) with 3 interleaved MCN frames.
# cdrdao read-toc independently reports pregap 00:00:52 for track 2.


def test_real_fixture_layout_matches_cdrdao():
    data = (_FIXTURES / "subq_track2_boundary.sub").read_bytes()
    layout = derive_track_layout(data, {1: 0, 2: 12032}, 162892)
    assert layout.pregap_frames == {2: 52}
    assert layout.index_points == {}
    assert layout.control[2].pre_emphasis is False
    assert layout.control[2].copy_permitted is False


def test_real_fixture_mcn_vote():
    data = (_FIXTURES / "subq_track2_boundary.sub").read_bytes()
    scan = scan_subcode(data)
    assert scan.base_lba == 11850
    (d,) = [d for d in scan.data if d.type == "MCN"]
    assert d.value == "7559607740206"  # matches READ SUB-CHANNEL ground truth
    assert d.count == 3
