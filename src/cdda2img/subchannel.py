"""CD subchannel Q-channel decoder for raw redumper ``.subcode`` captures.

The cdrdao ``.toc`` that :mod:`cdda2img.toc_parser` reads collapses subchannel
provenance: a ``CATALOG`` line cannot say whether the MCN came from the lead-in
Q-channel, the program-area Q-channel, or both. The only way to recover that
distinction is to decode the *raw* subchannel, which cdrdao / cd-paranoia /
cdda2wav consume internally and never expose. ``redumper dump`` does expose it,
as a ``.subcode`` file (96 bytes per sector, deinterleaved P..W).

This module decodes the Q-channel out of that file and attributes each MCN
(Q-mode 2) and ISRC (Q-mode 3) observation to its region — lead-in vs a specific
program track — so the "disc is gospel" authority model can rest on *where* on
the disc a datum physically lives, not on a tool's post-decode summary.

It mirrors redumper's ``cd/subcode.ixx`` (Q = bit 6 of each subcode byte) and
``crc/crc16_gsm.ixx`` (the complemented CCITT-variant CRC). Validated against a
real PX-716A capture of *American Idiot*: the program-area MCN decodes to the
known ``0093624877721`` (never fed in — non-circular) and the track-2 position
frame to LBA 13074, matching the redumper TOC.

Q-frame layout (12 bytes, after :func:`extract_q`)::

    q[0]   CONTROL (high nibble) | ADR (low nibble)
    q[1:10] payload (interpretation depends on ADR)
    q[10:12] CRC-16/GSM of q[0:10], big-endian

ADR meanings: 1 = position/TOC, 2 = MCN (Mode 2), 3 = ISRC (Mode 3). In the
lead-in an ADR=1 frame carries a *TOC pointer* (POINT 0xA0/0xA1/0xA2 or a track
start), not a running position, and its track number is 0; in the program area
ADR=1 carries the running absolute MSF and the current track number.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CD_SUBCODE_SIZE = 96
"""Bytes per sector in a redumper ``.subcode`` file (raw P..W subchannel)."""

_Q_FRAME_SIZE = 12
_Q_DATA_LEN = 10  # bytes covered by the CRC (q[0:10])
_MSF_PREGAP = 150  # LBA 0 == absolute MSF 00:02:00 == 150 frames

ADR_POSITION = 1
ADR_MCN = 2
ADR_ISRC = 3

# 6-bit alphanumeric alphabet for the ISRC owner code (ISO-3901 / IEC 60908):
# 0-9 -> digits, 17-42 -> 'A'..'Z', everything else invalid ('_'). Mirrors the
# table in redumper cd/toc.ixx.
_ISRC_TABLE = "0123456789_______ABCDEFGHIJKLMNOPQRSTUVWXYZ_____________________"

# Track number 0 in an ADR=1 frame marks the lead-in / TOC area.
LEADIN_TRACK = 0

# ---------------------------------------------------------------------------
# CRC-16/GSM  (poly 0x1021, init 0x0000, xorout 0xFFFF, no reflection)
# check value for b"123456789" is 0xCE3C.
# ---------------------------------------------------------------------------

_CRC_TABLE: list[int] = []
for _n in range(256):
    _c = _n << 8
    for _ in range(8):
        _c = ((_c << 1) ^ 0x1021) & 0xFFFF if _c & 0x8000 else (_c << 1) & 0xFFFF
    _CRC_TABLE.append(_c)
del _n, _c


def crc16_gsm(data: bytes) -> int:
    """CRC-16/GSM over *data* (the CD subchannel Q-channel CRC)."""
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC_TABLE[((crc >> 8) ^ b) & 0xFF]
    return crc ^ 0xFFFF


def extract_q(sector: bytes) -> bytes:
    """Pack the Q-channel (bit 6 of each subcode byte) into a 12-byte Q frame.

    *sector* is the 96-byte subcode for one CD sector. Bit 6 of byte ``i``
    becomes bit ``7 - (i % 8)`` of Q byte ``i // 8`` (MSB-first), matching
    redumper ``subcode_extract_q``.
    """
    if len(sector) != CD_SUBCODE_SIZE:
        msg = f"subcode sector must be {CD_SUBCODE_SIZE} bytes, got {len(sector)}"
        raise ValueError(msg)
    q = bytearray(_Q_FRAME_SIZE)
    for i in range(CD_SUBCODE_SIZE):
        if sector[i] & 0x40:
            q[i >> 3] |= 1 << (7 - (i & 7))
    return bytes(q)


def _bcd(b: int) -> int:
    """Decode one BCD byte, or -1 if either nibble is not a decimal digit."""
    hi, lo = b >> 4, b & 0x0F
    if hi > 9 or lo > 9:
        return -1
    return hi * 10 + lo


def _six_bit(field: bytes, bit_offset: int) -> int:
    """Read 6 bits (MSB-first) at *bit_offset* from a byte string."""
    val = 0
    for b in range(6):
        bit = bit_offset + b
        val = (val << 1) | ((field[bit >> 3] >> (7 - (bit & 7))) & 1)
    return val


@dataclass(frozen=True)
class ChannelQ:
    """One decoded Q-channel frame."""

    raw: bytes  # 12 bytes

    @property
    def control(self) -> int:
        return self.raw[0] >> 4

    @property
    def adr(self) -> int:
        return self.raw[0] & 0x0F

    @property
    def valid(self) -> bool:
        """True if the stored CRC matches (frame was read cleanly)."""
        stored = (self.raw[10] << 8) | self.raw[11]
        return crc16_gsm(self.raw[:_Q_DATA_LEN]) == stored

    @property
    def track_number(self) -> int:
        """BCD track number (q[1]); 0 = lead-in. -1 if the nibbles are invalid."""
        return _bcd(self.raw[1])

    def position_lba(self) -> int | None:
        """Absolute LBA from a program ADR=1 frame, or None if not applicable.

        Returns None for lead-in frames (track 0, whose ADR=1 payload is a TOC
        pointer, not a position) and for any frame whose absolute MSF nibbles
        are not valid BCD.
        """
        if self.adr != ADR_POSITION or self.track_number <= 0:
            return None
        m, s, f = _bcd(self.raw[7]), _bcd(self.raw[8]), _bcd(self.raw[9])
        if m < 0 or s < 0 or f < 0:
            return None
        return (m * 60 + s) * 75 + f - _MSF_PREGAP

    def mcn(self) -> str | None:
        """13-digit MCN from an ADR=2 frame, or None if not a valid MCN frame."""
        if self.adr != ADR_MCN:
            return None
        digits: list[int] = []
        for byte in self.raw[1:8]:
            digits += [byte >> 4, byte & 0x0F]
        first13 = digits[:13]
        if any(d > 9 for d in first13):
            return None
        return "".join(str(d) for d in first13)

    def isrc(self) -> str | None:
        """12-char ISRC (ISO-3901) from an ADR=3 frame, or None if malformed.

        The 8-byte ISRC field (q[1:9]) packs the 5-character owner code as 6-bit
        alphanumerics (MSB-first, 30 bits), 2 padding bits, then 7 BCD digits
        (year + designation) in bytes 4..7 — the 8th nibble is padding. Mirrors
        redumper cd/toc.ixx.
        """
        if self.adr != ADR_ISRC:
            return None
        field = self.raw[1:9]
        chars: list[str] = []
        for i in range(5):
            ch = _ISRC_TABLE[_six_bit(field, i * 6)]
            if ch == "_":
                return None  # invalid 6-bit code -> not a clean ISRC frame
            chars.append(ch)
        digits: list[int] = []
        for byte in field[4:8]:
            hi, lo = byte >> 4, byte & 0x0F
            if hi > 9 or lo > 9:
                return None
            digits += [hi, lo]
        number = "".join(str(d) for d in digits[:7])  # drop the 8th (padding)
        return "".join(chars) + number


def decode_q(sector_or_q: bytes) -> ChannelQ:
    """Decode a Q frame from a 96-byte subcode sector or a 12-byte Q frame."""
    if len(sector_or_q) == CD_SUBCODE_SIZE:
        return ChannelQ(extract_q(sector_or_q))
    if len(sector_or_q) == _Q_FRAME_SIZE:
        return ChannelQ(bytes(sector_or_q))
    msg = f"expected {CD_SUBCODE_SIZE} or {_Q_FRAME_SIZE} bytes, got {len(sector_or_q)}"
    raise ValueError(msg)


def parse_fulltoc_leadout(fulltoc: bytes) -> int | None:
    """Lead-out LBA from a redumper ``.fulltoc`` (raw READ TOC format-2 response).

    The response is a 4-byte header followed by 11-byte descriptors
    ``[session][ADR/CTRL][TNO][POINT][min][sec][frame][zero][PMIN][PSEC]
    [PFRAME]``. POINT 0xA2 is the lead-out; its PMIN/PSEC/PFRAME are *binary*
    (not BCD). Returns None if no lead-out descriptor is present.
    """
    body = fulltoc[4:]
    for off in range(0, len(body) - 10, 11):
        if body[off + 3] == 0xA2:
            pmin, psec, pframe = body[off + 8], body[off + 9], body[off + 10]
            return (pmin * 60 + psec) * 75 + pframe - _MSF_PREGAP
    return None


# ---------------------------------------------------------------------------
# Aggregate scan
# ---------------------------------------------------------------------------


@dataclass
class RegionDatum:
    """An MCN/ISRC observation aggregated over one region of the disc.

    *region* is ``"lead-in"`` or ``"track NN"``. *lba_min* / *lba_max* span the
    program-area sectors the datum was seen in (None for lead-in frames, whose
    Q carries no running position). *value* is the decoded MCN (13 digits) or
    ISRC (12 chars), taken from the first clean frame in the region.
    """

    type: str
    region: str
    count: int = 0
    value: str | None = None
    lba_min: int | None = None
    lba_max: int | None = None

    def observe(self, lba: int | None) -> None:
        self.count += 1
        if lba is not None:
            self.lba_min = lba if self.lba_min is None else min(self.lba_min, lba)
            self.lba_max = lba if self.lba_max is None else max(self.lba_max, lba)


@dataclass
class SubcodeScan:
    """Result of :func:`scan_subcode`."""

    n_sectors: int
    valid_q: int
    invalid_q: int
    base_lba: int | None  # LBA of file sector 0, or None if anchoring failed
    base_agreement: float  # fraction of program anchors agreeing on base_lba
    program_invalid_q: int | None  # invalid Q over [0, leadout), or None
    data: list[RegionDatum] = field(default_factory=list)


_MIN_BASE_AGREEMENT = 0.5


def _resolve_base_lba(data: bytes, n: int) -> tuple[int | None, float]:
    """Anchor file-sector index 0 to an absolute LBA via program position frames.

    Only ADR=1 frames with a real program track (1..99) and a valid index carry
    a running absolute MSF, so ``base = position_lba - sector_index`` is constant
    across them. Lead-in (track 0) and lead-out (track 0xAA) frames are excluded.
    Returns (base_lba, agreement_fraction); base_lba is None if no anchor agrees
    above :data:`_MIN_BASE_AGREEMENT`.
    """
    votes: dict[int, int] = {}
    for s in range(n):
        q = decode_q(data[s * CD_SUBCODE_SIZE : (s + 1) * CD_SUBCODE_SIZE])
        if not q.valid:
            continue
        lba = q.position_lba()
        if lba is None or _bcd(q.raw[2]) < 1:  # require a valid program index
            continue
        votes[lba - s] = votes.get(lba - s, 0) + 1
    if not votes:
        return None, 0.0
    total = sum(votes.values())
    base, top = max(votes.items(), key=lambda kv: kv[1])
    agreement = top / total
    return (base if agreement >= _MIN_BASE_AGREEMENT else None), agreement


def scan_subcode(data: bytes, *, leadout_lba: int | None = None) -> SubcodeScan:
    """Decode a redumper ``.subcode`` and attribute MCN/ISRC to disc regions.

    *data* is the raw ``.subcode`` (a whole number of 96-byte sectors).
    *leadout_lba* (from the cdrdao TOC or :func:`parse_fulltoc_leadout`) enables
    the program-area invalid-Q count over ``[0, leadout)`` — redumper's own
    ``Q:`` error metric, restricted to the program area. Region attribution does
    not require it.
    """
    n = len(data) // CD_SUBCODE_SIZE
    base_lba, agreement = _resolve_base_lba(data, n)

    valid = invalid = 0
    cur_track: int | None = None
    buckets: dict[tuple[str, str], RegionDatum] = {}

    def bucket(type_: str, region: str) -> RegionDatum:
        key = (type_, region)
        d = buckets.get(key)
        if d is None:
            d = buckets[key] = RegionDatum(type_, region)
        return d

    for s in range(n):
        q = decode_q(data[s * CD_SUBCODE_SIZE : (s + 1) * CD_SUBCODE_SIZE])
        if not q.valid:
            invalid += 1
            continue
        valid += 1
        sector_lba = (s + base_lba) if base_lba is not None else None
        if q.adr == ADR_POSITION:
            cur_track = q.track_number
        elif q.adr == ADR_MCN:
            mcn = q.mcn()
            if mcn is not None:
                in_leadin = cur_track == LEADIN_TRACK
                region = "lead-in" if in_leadin else "program"
                d = bucket("MCN", region)
                d.value = mcn
                d.observe(None if in_leadin else sector_lba)
        elif q.adr == ADR_ISRC:
            in_leadin = cur_track is None or cur_track == LEADIN_TRACK
            region = "lead-in" if in_leadin else f"track {cur_track:02d}"
            d = bucket("ISRC", region)
            if d.value is None:
                d.value = q.isrc()
            d.observe(None if in_leadin else sector_lba)

    program_invalid = _count_program_invalid_q(data, n, base_lba, leadout_lba)

    ordered = sorted(buckets.values(), key=lambda d: (d.type, d.region))
    return SubcodeScan(
        n_sectors=n,
        valid_q=valid,
        invalid_q=invalid,
        base_lba=base_lba,
        base_agreement=agreement,
        program_invalid_q=program_invalid,
        data=ordered,
    )


def _count_program_invalid_q(
    data: bytes, n: int, base_lba: int | None, leadout_lba: int | None
) -> int | None:
    """Count CRC-invalid Q frames over the program area ``[0, leadout)``.

    This is redumper's ``Q:`` metric restricted to the program area — it
    excludes the large unfilled head (file sectors before the captured lead-in)
    and the tail past lead-out, which are not read errors. Returns None when the
    base LBA could not be anchored or the lead-out is unknown.
    """
    if base_lba is None or leadout_lba is None:
        return None
    start = max(0, -base_lba)  # file index of LBA 0
    end = min(n, leadout_lba - base_lba)  # file index of lead-out
    invalid = 0
    for s in range(start, end):
        q = decode_q(data[s * CD_SUBCODE_SIZE : (s + 1) * CD_SUBCODE_SIZE])
        if not q.valid:
            invalid += 1
    return invalid
