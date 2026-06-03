"""Tests for the CD subchannel Q-channel decoder (:mod:`cdda2img.subchannel`).

The real-data fixtures are raw 96-byte subcode sectors lifted verbatim from a
PX-716A ``redumper dump`` of *American Idiot*. They are committed as hex so the
decode is validated against genuine disc bytes (non-circular) without depending
on the multi-hundred-MB capture, which is gitignored under ``rips/``.
"""

from __future__ import annotations

from cdda2img.subchannel import (
    ADR_ISRC,
    ADR_MCN,
    ADR_POSITION,
    CD_SUBCODE_SIZE,
    crc16_gsm,
    decode_q,
    extract_q,
    parse_fulltoc_leadout,
    scan_subcode,
)

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


# --- synthetic attribution (deterministic bucketing) ------------------------


def _pack_q(q: bytes) -> bytes:
    """Inverse of extract_q: spread a 12-byte Q frame into bit 6 of 96 bytes."""
    sector = bytearray(CD_SUBCODE_SIZE)
    for i in range(CD_SUBCODE_SIZE):
        if q[i >> 3] & (1 << (7 - (i & 7))):
            sector[i] |= 0x40
    return bytes(sector)


def _make_q(adr: int, payload9: bytes) -> bytes:
    """Build a valid 12-byte Q frame: [ctrl|adr] + 9 payload + CRC-16/GSM."""
    assert len(payload9) == 9
    head = bytes([(0x4 << 4) | adr]) + payload9  # CONTROL=audio(0x4)
    crc = crc16_gsm(head)
    return head + bytes([crc >> 8, crc & 0xFF])


def _position(track: int, lba: int) -> bytes:
    frames = lba + 150
    amin, rem = divmod(frames, 60 * 75)
    asec, aframe = divmod(rem, 75)
    payload = bytes([track, 0x01, 0x00, 0x00, 0x00, 0x00, amin, asec, aframe])
    return _pack_q(_make_q(ADR_POSITION, payload))


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
