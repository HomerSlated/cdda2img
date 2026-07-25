"""
rbi_format.py — RBI (Red Book Image) file format definition.

This module is the canonical Python reference for the RBI format (v5.0).
It contains only constants, struct definitions, and dataclasses.
No I/O. No business logic. Translatable directly to C structs, Rust structs, etc.

See rbi_spec.md for the full human-readable specification.
"""

import struct
from dataclasses import dataclass, field
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

MAGIC: bytes = b"RBIMAGE\x00"  # 8 bytes; null byte prevents text false-matches
VERSION_MAJOR: int = 6
VERSION_MINOR: int = 0  # v6.0: disc_id->cdtext_catalog_ref rename + catalogue fields; clean break, no read shim

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
#      8     1  uint8         version_major     4
#      9     1  uint8         version_minor     1
#     10     4  uint32 LE     flags             feature bitmask
#     14     1  uint8         track_count       1-99
#     15     1  uint8         disc_number       1-based position in set
#     16     1  uint8         disc_total        total discs in set
#     17     4  uint32 LE     pcm_sample_rate   44100
#     21     1  uint8         pcm_channels      2
#     22     1  uint8         pcm_bit_depth     16
#     23     8  uint64 LE     dir_offset        byte offset to block directory
#     31     2  uint16 LE     dir_count         number of directory entries
#     33     7  bytes         reserved          all zeros

OFFSET_DIR_OFFSET: int = 23
OFFSET_DIR_COUNT: int = 31

HEADER_FIXED_SIZE: int = 40

# ---------------------------------------------------------------------------
# Struct format strings (all little-endian)
# ---------------------------------------------------------------------------

# Full fixed header, written/read in one call.
# Fields: magic(8s), version_major(B), version_minor(B), flags(I),
#         track_count(B), disc_number(B), disc_total(B),
#         pcm_sample_rate(I), pcm_channels(B), pcm_bit_depth(B),
#         dir_offset(Q), dir_count(H), reserved(7s)
HEADER_STRUCT: str = "<8sBBIBBBIBBQH7s"
HEADER_STRUCT_SIZE: int = struct.calcsize(HEADER_STRUCT)  # must equal HEADER_FIXED_SIZE

assert HEADER_STRUCT_SIZE == HEADER_FIXED_SIZE, (  # noqa: S101  # LINT-005
    f"HEADER_STRUCT size {HEADER_STRUCT_SIZE} != HEADER_FIXED_SIZE {HEADER_FIXED_SIZE}"
)

# Directory entry struct.
# Fields: type_id(4s), block_flags(H), offset(Q), length(Q), checksum(32s)
DIR_ENTRY_STRUCT: str = "<4sHQQ32s"
DIR_ENTRY_SIZE: int = struct.calcsize(DIR_ENTRY_STRUCT)  # must equal 54

assert DIR_ENTRY_SIZE == 54, (  # noqa: S101  # LINT-005
    f"DIR_ENTRY_STRUCT size {DIR_ENTRY_SIZE} != 54"
)

# RG block fixed fields (N-dependent arrays follow; see rbi_spec.md §6.4)
# Fields: rg_version(B), rg_reference(f), album_gain(f), album_peak(f), album_range(f)
RG_BLOCK_FIXED_STRUCT: str = "<Bffff"
RG_BLOCK_FIXED_SIZE: int = struct.calcsize(RG_BLOCK_FIXED_STRUCT)  # 17 bytes

assert RG_BLOCK_FIXED_SIZE == 17, (  # noqa: S101  # LINT-005
    f"RG_BLOCK_FIXED_STRUCT size {RG_BLOCK_FIXED_SIZE} != 17"
)

# Per-track array element (three float32 values: gain, peak, range)
RG_TRACK_STRUCT: str = "<fff"
RG_TRACK_SIZE: int = struct.calcsize(RG_TRACK_STRUCT)  # 12 bytes

# ARIP block structs (see rbi_spec.md §6.5)
# Header: arip_version(B), disc_id1(L), disc_id2(L), cddb_id(L)
ARIP_HEADER_STRUCT: str = "<BLLL"
ARIP_HEADER_SIZE: int = struct.calcsize(ARIP_HEADER_STRUCT)  # 13 bytes

# Per-track: v1_crc(L), v2_crc(L), v1_confidence(H), v2_confidence(H), db_total(H), status(B)
ARIP_TRACK_STRUCT: str = "<LLHHHB"
ARIP_TRACK_SIZE: int = struct.calcsize(ARIP_TRACK_STRUCT)  # 15 bytes

assert ARIP_HEADER_SIZE == 13, (  # noqa: S101  # LINT-005
    f"ARIP_HEADER_STRUCT size {ARIP_HEADER_SIZE} != 13"
)
assert ARIP_TRACK_SIZE == 15, (  # noqa: S101  # LINT-005
    f"ARIP_TRACK_STRUCT size {ARIP_TRACK_SIZE} != 15"
)

ARIP_BLOCK_VERSION: int = 1
ARIP_STATUS_NOT_IN_DB: int = 0
ARIP_STATUS_MISMATCH: int = 1
ARIP_STATUS_OK: int = 2

# ART block struct (see rbi_spec.md §6.8); JPEG payload follows the fixed header.
# Header: art_version(B), image_format(B), width(H), height(H), image_length(I)
ART_HEADER_STRUCT: str = "<BBHHI"
ART_HEADER_SIZE: int = struct.calcsize(ART_HEADER_STRUCT)  # 10 bytes

assert ART_HEADER_SIZE == 10, (  # noqa: S101  # LINT-005
    f"ART_HEADER_STRUCT size {ART_HEADER_SIZE} != 10"
)

ART_BLOCK_VERSION: int = 1
ART_IMAGE_FORMAT_JPEG: int = 1  # the only image_format defined in v4.1

# Placeholder checksum used when pre-writing directory entries
CHECKSUM_SIZE: int = 32  # BLAKE3 digest length in bytes (same as SHA-256)
CHECKSUM_PLACEHOLDER: bytes = b"\x00" * CHECKSUM_SIZE

# ---------------------------------------------------------------------------
# Block type identifiers (4 bytes each; trailing space is part of the identifier)
# ---------------------------------------------------------------------------

BLOCK_TYPE_TOC: bytes = b"TOC "
BLOCK_TYPE_PCM: bytes = b"PCM "
BLOCK_TYPE_PROV: bytes = b"PROV"
BLOCK_TYPE_RGDB: bytes = b"RGDB"
BLOCK_TYPE_ARIP: bytes = b"ARIP"
BLOCK_TYPE_RLOG: bytes = b"RLOG"
BLOCK_TYPE_ART: bytes = b"ART "  # trailing space: 4-byte identifier
BLOCK_TYPE_CTDB: bytes = b"CTDB"

# ---------------------------------------------------------------------------
# Flags bitmask
# ---------------------------------------------------------------------------
# Even bit positions = "safe to ignore if unknown"
# Odd bit positions  = "must understand to read correctly"

BLOCK_FLAG_SKIP: int = (
    0x0001  # bit 0 (even): reader MAY skip this block if unrecognised
)

FLAG_MASTER_MODE: int = (
    0x00000004  # bit 2 (even): created in master mode (no silence trim)
)
FLAGS_RESERVED_MASK: int = 0xFFFFFFFB  # all bits except FLAG_MASTER_MODE are reserved

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class RBIDirEntry:
    """One entry in the block directory appended at the end of the RBI file."""

    type_id: bytes  # 4 bytes, e.g. b"TOC "
    block_flags: int  # uint16; BLOCK_FLAG_SKIP etc.
    offset: int  # uint64; byte offset to start of block
    length: int  # uint64; byte length of block
    checksum: bytes  # 32-byte BLAKE3 digest of block content (SHA-256 in v4.x)

    @property
    def is_skippable(self) -> bool:
        return bool(self.block_flags & BLOCK_FLAG_SKIP)


@dataclass
class RBIHeader:
    """Parsed representation of a validated RBI v4.0 file header."""

    version_major: int  # uint8
    version_minor: int  # uint8
    flags: int  # uint32
    track_count: int  # uint8; 1-99
    disc_number: int  # uint8; 1-based
    disc_total: int  # uint8
    pcm_sample_rate: int  # uint32; Hz
    pcm_channels: int  # uint8
    pcm_bit_depth: int  # uint8
    dir_offset: int  # uint64; byte offset to block directory
    dir_count: int  # uint16; number of directory entries
    directory: list[RBIDirEntry] = field(default_factory=list)

    @property
    def is_master(self) -> bool:
        return bool(self.flags & FLAG_MASTER_MODE)

    def find_block(self, type_id: bytes) -> "RBIDirEntry | None":
        """Return the first directory entry matching type_id, or None."""
        for entry in self.directory:
            if entry.type_id == type_id:
                return entry
        return None


@dataclass
class RBIReplayGain:
    """Parsed RG block from an RBI container."""

    rg_version: int  # uint8; current value: 1
    rg_reference: float  # LUFS; nominally -18.0
    album_gain: float  # dB
    album_peak: float  # linear
    album_range: float  # LU
    track_gain: list[float] = field(default_factory=list)  # dB; one per track
    track_peak: list[float] = field(default_factory=list)  # linear; one per track
    track_range: list[float] = field(default_factory=list)  # LU; one per track


@dataclass
class RBIAripTrack:
    """Per-track entry from an ARIP block (rbi_spec.md §6.5)."""

    v1_crc: int  # uint32; computed AR v1 CRC (0 if not in DB)
    v2_crc: int  # uint32; computed AR v2 CRC (0 if not in DB)
    v1_confidence: int  # uint16; submissions matching v1; 0 = no match
    v2_confidence: int  # uint16; submissions matching v2; 0 = no match
    db_total: int  # uint16; total AR submissions for this track; 0 = not in DB
    status: int  # uint8; ARIP_STATUS_* constant


@dataclass
class RBIArip:
    """Parsed ARIP block from an RBI container."""

    arip_version: int  # uint8; current value: 1
    disc_id1: int  # uint32 LE
    disc_id2: int  # uint32 LE
    cddb_id: int  # uint32 LE
    tracks: list[RBIAripTrack] = field(default_factory=list)


@dataclass
class RBIAlbumArt:
    """Parsed ART block from an RBI container (rbi_spec.md §6.8).

    The stored image is the full-resolution JPEG master; downscaling for the
    terminal preview or a per-track FLAC PICTURE block happens at use time.
    ``width``/``height`` are best-effort (0 = unknown); ``image_length`` is not
    stored separately — it is ``len(image_data)``.
    """

    art_version: int  # uint8; current value: ART_BLOCK_VERSION
    image_format: int  # uint8; ART_IMAGE_FORMAT_JPEG
    width: int  # uint16; pixels, 0 = unknown
    height: int  # uint16; pixels, 0 = unknown
    image_data: bytes  # encoded image bytes (JPEG when image_format == 1)


@dataclass
class RBITocEntry:
    """One track entry as parsed from the embedded TOC text."""

    track_number: int  # 1-based, 1-99
    title: str  # sanitised track title
    performer: str  # track-level performer string
    start_frame: int  # PCM block offset to start of pregap (or audio if no pregap)
    duration_frames: int  # audio-only duration in CD frames (1/75 s); excludes pregap
    pregap_frames: int = 0  # pregap duration in CD frames; 0 if no pregap
    isrc: str | None = None  # ISO 3901 ISRC code (12 chars); None if not available
    pre_emphasis: bool = False  # Q CONTROL 0x1 (spec §6.1.10); False when uncaptured
    copy_permitted: bool = False  # Q CONTROL 0x2 (spec §6.1.10); False when uncaptured
    index_points: list[int] = field(default_factory=list)  # INDEX >= 02 offsets in
    # frames relative to the audio start (after pregap), ascending (spec §6.1.10)

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

    @property
    def pregap_timestamp(self) -> str:
        return _frames_to_timestamp(self.pregap_frames)

    @property
    def slot_timestamp(self) -> str:
        """Total slot duration (pregap + audio) as MM:SS:FF, for the FILE entry."""
        return _frames_to_timestamp(self.pregap_frames + self.duration_frames)


@dataclass
class RBIDisc:
    """Full logical representation of an RBI container."""

    album: str
    artist: str
    disc_number: int = 1
    disc_total: int = 1
    catalog: str | None = None  # on-disc MCN (Q-ch Mode 2); archival only — never
    # a lookup/disambiguation key. May be synthesised from `barcode` at finalisation
    # when the disc carries no MCN (PROV `mcn_source=barcode_derived`); burned to the
    # TOC CATALOG line. See docs/reference/identifier_trust_model.md §1a.
    barcode: str | None = None  # service UPC/EAN barcode (MB/Discogs); the
    # disambiguation key. Persisted to PROV only (never the TOC/physical layer).
    cdtext_catalog_ref: str | None = (
        None  # CD-Text PTI 0x86 catalogue/label reference string; None if absent.
        # Renamed from `disc_id` at v6.0 to remove the collision with the
        # MusicBrainz Disc ID and `mb_release_id`. Distinct from `catalog`
        # (MCN) and `catalog_number` (the label's own alphanumeric number).
    )
    tracks: list[RBITocEntry] = field(default_factory=list)
    release_date: str | None = None  # YYYY, YYYY-MM, or YYYY-MM-DD
    catalog_number: str | None = None  # label's own catalogue number, e.g. "CID U2 6"
    label: str | None = None  # record label / imprint name
    country: str | None = None  # ISO-3166 alpha-2, or MB pseudo-code XE / XW
    original_release_date: str | None = None  # release-group first-release-date
    low_dynamic_range: bool | None = (
        None  # set after EBU R128 analysis; None if RG skipped
    )
    original_release_found: bool = False
    original_release_title: str | None = None
    original_release_year: int | None = None
    mb_release_id: str | None = None  # MusicBrainz release UUID for provenance
    mb_release_group_id: str | None = (
        None  # MB release-group UUID for original-release lookup
    )
    discogs_release_id: int | None = None  # Discogs release ID (for R11 master lookup)
    set_title: str | None = (
        None  # box set / release title when medium has its own album title
    )
    # R14: aggregate disc-level pre-emphasis flag. True = at least one track
    # has the CONTROL bit set. None = not captured (pre-R14 containers,
    # parsers that don't propagate it). Used as a year upper-bound (≤ 1986)
    # in the original-release lookup.
    pre_emphasis: bool | None = None

    @property
    def total_frames(self) -> int:
        return sum(t.pregap_frames + t.duration_frames for t in self.tracks)

    @property
    def total_seconds(self) -> float:
        return self.total_frames / CD_FRAMES_PER_SECOND

    @property
    def track_count(self) -> int:
        return len(self.tracks)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def timestamp_from_frames(frames: int) -> str:
    """Convert an absolute CD frame count to MM:SS:FF timestamp string."""
    mm = frames // (CD_FRAMES_PER_SECOND * 60)
    ss = (frames // CD_FRAMES_PER_SECOND) % 60
    ff = frames % CD_FRAMES_PER_SECOND
    return f"{mm:02}:{ss:02}:{ff:02}"


# In-module shorthand kept for the RBITocEntry timestamp properties above.
_frames_to_timestamp = timestamp_from_frames


def frames_from_timestamp(ts: str) -> int:
    """Parse a MM:SS:FF timestamp string to an absolute CD frame count."""
    mm, ss, ff = (int(x) for x in ts.split(":"))
    return mm * CD_FRAMES_PER_SECOND * 60 + ss * CD_FRAMES_PER_SECOND + ff


def year_of(date_str: str | None) -> int | None:
    """Return the 4-digit year from a YYYY / YYYY-MM / YYYY-MM-DD string, or None."""
    if not date_str:
        return None
    try:
        return int(str(date_str)[:4])
    except (ValueError, IndexError):
        return None


def format_original_fields(
    disc_year: int | None,
    found: bool,
    title: str | None,
    orig_year: int | None,
) -> str:
    """Render the canonical one-line original-release string from raw fields.

    Format::

        Original: <Yes|No|Unknown>, <this release|original title|unknown release> \
(<year>|unknown year)

    Field 1 answers "is THIS disc the original release?" and is **gated by the
    disc's own year** (``disc_year``): without it we cannot claim anything
    predates this disc, so the answer is Unknown and the earlier-release fields
    are unknown too (we never emit a paradoxical "Unknown, Title (year)").
    Comparison is at **year** granularity — a 1983 pressing is the original even
    when the release-group's first-release date is 1983-03-23. The earlier-release
    title/year are shown only in the "No" case; "Yes" shows "this release" + the
    disc's own year.

    This is the shared core behind every surface. Callers holding an ``RBIDisc``
    use :func:`format_original`; callers holding a provenance dict (RBI ``list``)
    or a catalogue DB row pass their already-extracted values here directly. The
    same string is emitted everywhere so the representation is identical.

    The ``"Original:  "`` label here is padded to the value column at index 11,
    matching the list-provenance / extract dumps. The :func:`format_disc_metadata`
    canonical block re-uses the *value* (via :func:`_original_value`) at its own
    wider label column, so the two contexts share the logic without coupling width.
    """
    return f"Original:  {_original_value(disc_year, found, title, orig_year)}"


def _original_value(
    disc_year: int | None,
    found: bool,
    title: str | None,
    orig_year: int | None,
) -> str:
    """Return just the original-release value (no label), shared by the width-11
    :func:`format_original_fields` and the width-15 :func:`format_disc_metadata`."""
    if disc_year is None or not found:
        return "Unknown, unknown release (unknown year)"
    if disc_year == orig_year:
        return f"Yes, this release ({disc_year})"
    disp_title = title or "unknown release"
    year_disp = orig_year if orig_year is not None else "unknown year"
    return f"No, {disp_title} ({year_disp})"


def format_disc_metadata(
    *,
    album: str | None,
    artist: str | None,
    release_date: str | None,
    label: str | None = None,
    country: str | None = None,
    catalog_number: str | None = None,
    mcn: str | None = None,
    original_release_found: bool = False,
    original_release_title: str | None = None,
    original_release_year: int | None = None,
    track_count: int = 0,
) -> list[str]:
    """Render the canonical disc-level metadata header as a list of lines.

    This is the single shared formatter behind every disc-metadata display: the
    interactive metadata menu, the ``list`` / ``--info`` dump, and the catalogue
    browser (docs/reference/rbi_spec.md §6.3.2). Three invariants hold:

    1. **stored ⟺ displayed** — the field set equals the persisted catalogue set,
       both ways (no shown-but-unstored field, no stored-but-hidden field).
    2. **same set in all three** surfaces (all call this function).
    3. **identical format / spacing / order** — defined once here.

    Lines are returned **un-indented** with the value column at index 15 — the
    same column the metadata-menu candidate preview and the catalogue provenance
    lines already use, so the disc-metadata block aligns with them. Callers MAY
    prepend their own leading indent (site chrome) but **MUST NOT** alter the
    labels, order, column width, or value formatting — that identity is the entire
    point. Optional catalogue fields are omitted when absent (no data gap); Album /
    Artist / Original / Tracks always render. The per-track table is a separate,
    site-local concern (out of scope).
    """
    w = 15  # label width; value column aligns with menu preview + catalogue prov

    def _row(label_text: str, value: str) -> str:
        return f"{label_text:<{w}}{value}"

    disc_year = year_of(release_date)
    year_disp = disc_year if disc_year is not None else "unknown"
    lines = [
        _row("Album:", f"{album or '(none)'} ({year_disp})"),
        _row("Artist:", artist or "(none)"),
    ]
    if label:
        lines.append(_row("Label:", label))
    if country:
        lines.append(_row("Country:", country))
    if catalog_number:
        lines.append(_row("Cat. no.:", catalog_number))
    if mcn:
        lines.append(_row("MCN:", mcn))
    lines.append(
        _row(
            "Original:",
            _original_value(
                disc_year,
                original_release_found,
                original_release_title,
                original_release_year,
            ),
        )
    )
    lines.append(_row("Tracks:", str(track_count)))
    return lines


def format_original(disc: RBIDisc) -> str:
    """Render the canonical original-release line for an ``RBIDisc``.

    Thin adapter over :func:`format_original_fields` — extracts the disc's own
    release year from ``release_date`` (the one thing the field-level core cannot
    derive) and forwards the original-release trio.
    """
    return format_original_fields(
        year_of(disc.release_date),
        disc.original_release_found,
        disc.original_release_title,
        disc.original_release_year,
    )


class RipInfo(NamedTuple):
    """Result of reading a physical disc: the disc skeleton plus raw geometry.

    Lived in ``disc_reader`` while cd-paranoia was a read engine; moved here when
    that module was deleted (AccuDisc migration Phase E). It is a pipeline type,
    not a container type, but every producer and consumer of it already imports
    this module, so it costs no new edge in the import graph.
    """

    disc: RBIDisc
    track_lsns: list[int]  # absolute first_lsn per track, needed for CDDB lookup
    disc_last_lsn: int
    prov: dict[str, str] | None = None  # read-stage provenance keys (e.g. the
    # subq_toc toc_source / ISRC vote counts); merged into PROV by rip_image
