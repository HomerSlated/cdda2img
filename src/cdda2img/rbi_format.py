"""
rbi_format.py — RBI (Red Book Image) file format definition.

This module is the canonical Python reference for the RBI format (v1.2).
It contains only constants, struct definitions, and dataclasses.
No I/O. No business logic. Translatable directly to C structs, Rust structs, etc.

See rbi_spec.md for the full human-readable specification.
"""

import struct
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

MAGIC: bytes = b"RBIMAGE\x00"  # 8 bytes; null byte prevents text false-matches
VERSION_MAJOR: int = 1
VERSION_MINOR: int = 2

# ---------------------------------------------------------------------------
# Red Book audio constraints (IEC 60908:1999)
# ---------------------------------------------------------------------------

PCM_SAMPLE_RATE: int = 44100  # Hz
PCM_CHANNELS: int = 2  # stereo
PCM_BIT_DEPTH: int = 16  # bits per sample (s16le)
PCM_CODEC: str = "pcm_s16le"  # ffmpeg codec name

MAX_TRACKS: int = 99  # Red Book §3.1.2
MAX_RUNTIME_SECONDS: int = 4800  # 80 minutes
CD_FRAMES_PER_SECOND: int = 75  # Red Book frame rate

# ---------------------------------------------------------------------------
# Header layout — byte offsets (all fields relative to file start)
# ---------------------------------------------------------------------------
#
# Offset  Size  Type          Field
# ------  ----  ----          -----
#      0     8  bytes         magic             b'RBIMAGE\x00'
#      8     1  uint8         version_major     1
#      9     1  uint8         version_minor     2
#     10     4  uint32 LE     flags             feature bitmask (currently 0)
#     14     1  uint8         track_count       1-99
#     15     1  uint8         disc_number       1-based position in set
#     16     1  uint8         disc_total        total discs in set
#     17     4  uint32 LE     pcm_sample_rate   44100
#     21     1  uint8         pcm_channels      2
#     22     1  uint8         pcm_bit_depth     16
#     23     8  uint64 LE     toc_start         byte offset to TOC block
#     31     8  uint64 LE     toc_end           byte offset past TOC block
#     39     8  uint64 LE     pcm_start         byte offset to PCM block
#     47     8  uint64 LE     pcm_end           byte offset past PCM block (== file size)
#     55    32  bytes         toc_checksum      SHA-256 of TOC block
#     87    32  bytes         pcm_checksum      SHA-256 of raw PCM bytes
#    119     2  uint16 LE     metadata_len      length of following UTF-8 string
#    121     ?  UTF-8         metadata          creation string (metadata_len bytes)
#  121+n     ?  UTF-8         TOC block         (toc_end - toc_start bytes)
#    ...     ?  bytes         [future sections] gap between toc_end and pcm_start
#    ...     ?  bytes         PCM block         raw s16le interleaved (pcm_end - pcm_start bytes)

OFFSET_MAGIC: int = 0
OFFSET_VERSION_MAJOR: int = 8
OFFSET_VERSION_MINOR: int = 9
OFFSET_FLAGS: int = 10
OFFSET_TRACK_COUNT: int = 14
OFFSET_DISC_NUMBER: int = 15
OFFSET_DISC_TOTAL: int = 16
OFFSET_PCM_SAMPLE_RATE: int = 17
OFFSET_PCM_CHANNELS: int = 21
OFFSET_PCM_BIT_DEPTH: int = 22
OFFSET_TOC_START: int = 23
OFFSET_TOC_END: int = 31
OFFSET_PCM_START: int = 39
OFFSET_PCM_END: int = 47
OFFSET_TOC_CHECKSUM: int = 55
OFFSET_PCM_CHECKSUM: int = 87
OFFSET_METADATA_LEN: int = 119
OFFSET_METADATA: int = 121

HEADER_FIXED_SIZE: int = 121  # bytes 0-120 inclusive

# ---------------------------------------------------------------------------
# Struct format strings (all little-endian)
# ---------------------------------------------------------------------------

# Full fixed header (excluding variable metadata), written/read in one call.
# Fields: magic(8s), version_major(B), version_minor(B), flags(I),
#         track_count(B), disc_number(B), disc_total(B),
#         pcm_sample_rate(I), pcm_channels(B), pcm_bit_depth(B),
#         toc_start(Q), toc_end(Q), pcm_start(Q), pcm_end(Q),
#         toc_checksum(32s), pcm_checksum(32s), metadata_len(H)
HEADER_STRUCT: str = "<8sBBIBBBIBBQQQQ32s32sH"
HEADER_STRUCT_SIZE: int = struct.calcsize(HEADER_STRUCT)  # must equal HEADER_FIXED_SIZE

assert HEADER_STRUCT_SIZE == HEADER_FIXED_SIZE, (  # noqa: S101
    f"HEADER_STRUCT size {HEADER_STRUCT_SIZE} != HEADER_FIXED_SIZE {HEADER_FIXED_SIZE}"
)

# Placeholder checksums and offsets used when first writing the header
CHECKSUM_SIZE: int = 32  # SHA-256 digest length in bytes
CHECKSUM_PLACEHOLDER: bytes = b"\x00" * CHECKSUM_SIZE
OFFSET_PLACEHOLDER: int = 0

# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------

MAX_METADATA_LEN: int = 1024  # enforced on read and write
FLAGS_RESERVED_MASK: int = 0xFFFFFFFF  # all bits reserved in v1.2; must be 0

# ---------------------------------------------------------------------------
# Flags bitmask (v1.2: all zero; defined here for future use)
# ---------------------------------------------------------------------------
# Even bit positions = "safe to ignore if unknown"
# Odd bit positions  = "must understand to read correctly"
# (No bits are currently defined.)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class RBIHeader:
    """Parsed representation of a validated RBI file header."""

    version_major: int  # uint8
    version_minor: int  # uint8
    flags: int  # uint32
    track_count: int  # uint8; 1-99
    disc_number: int  # uint8; 1-based
    disc_total: int  # uint8
    pcm_sample_rate: int  # uint32; Hz
    pcm_channels: int  # uint8
    pcm_bit_depth: int  # uint8
    toc_start: int  # uint64; byte offset
    toc_end: int  # uint64; byte offset
    pcm_start: int  # uint64; byte offset
    pcm_end: int  # uint64; byte offset
    toc_checksum: bytes  # 32-byte SHA-256 digest
    pcm_checksum: bytes  # 32-byte SHA-256 digest
    metadata: str  # decoded UTF-8 creation string

    @property
    def toc_length(self) -> int:
        return self.toc_end - self.toc_start

    @property
    def pcm_length(self) -> int:
        return self.pcm_end - self.pcm_start

    @property
    def header_size(self) -> int:
        return HEADER_FIXED_SIZE + len(self.metadata.encode("utf-8"))


@dataclass
class RBITocEntry:
    """One track entry as parsed from the embedded TOC text."""

    track_number: int  # 1-based, 1-99
    title: str  # sanitised track title
    performer: str  # track-level performer string
    start_frame: int  # absolute frame offset into PCM blob
    duration_frames: int  # track duration in CD frames (1/75 s)

    @property
    def start_seconds(self) -> float:
        return self.start_frame / CD_FRAMES_PER_SECOND

    @property
    def duration_seconds(self) -> float:
        return self.duration_frames / CD_FRAMES_PER_SECOND

    @property
    def start_timestamp(self) -> str:
        return _frames_to_timestamp(self.start_frame)

    @property
    def duration_timestamp(self) -> str:
        return _frames_to_timestamp(self.duration_frames)


@dataclass
class RBIDisc:
    """Full logical representation of an RBI container."""

    album: str
    artist: str
    disc_number: int = 1
    disc_total: int = 1
    catalog: str | None = None  # MCN / EAN-13; None if not available
    tracks: list[RBITocEntry] = field(default_factory=list)

    @property
    def total_frames(self) -> int:
        return sum(t.duration_frames for t in self.tracks)

    @property
    def total_seconds(self) -> float:
        return self.total_frames / CD_FRAMES_PER_SECOND

    @property
    def track_count(self) -> int:
        return len(self.tracks)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _frames_to_timestamp(frames: int) -> str:
    """Convert an absolute CD frame count to MM:SS:FF timestamp string."""
    mm = frames // (CD_FRAMES_PER_SECOND * 60)
    ss = (frames // CD_FRAMES_PER_SECOND) % 60
    ff = frames % CD_FRAMES_PER_SECOND
    return f"{mm:02}:{ss:02}:{ff:02}"


def frames_from_timestamp(ts: str) -> int:
    """Parse a MM:SS:FF timestamp string to an absolute CD frame count."""
    mm, ss, ff = (int(x) for x in ts.split(":"))
    return mm * CD_FRAMES_PER_SECOND * 60 + ss * CD_FRAMES_PER_SECOND + ff
