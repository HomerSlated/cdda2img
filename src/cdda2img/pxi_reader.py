"""
pxi_reader.py — PlexTools ``.pxi`` disc image importer.

PlexTools was Plextor's own bundled ripping/burning application; the format is
undocumented and was reverse-engineered here against a known-good disc (ABBA
*Gold*, whose TOC and audio we also hold as a verified AccuDisc rip).

Layout — every multi-byte field is little-endian **except** the CD-Text length,
which is a verbatim MMC big-endian READ TOC header::

    0x00000000  magic ``PXI\\0``
    0x0000000B  CD-Text: a raw READ TOC format 0x05 response — BE u16 length,
                2 reserved bytes, then 18-byte packs.  ``cdtext.py`` consumes
                this shape directly, CRCs and SIZE_INFO included.
    0x00008000  TOC block: lead-out at +0x0B (u32), track count at +0x47 (u8),
                MCN at +0x4C (13 ASCII bytes, all-``0`` when absent)
    0x00008067  index table: 36-byte records, session +0, track +4,
                position +16, length +20.  Terminated by an all-zero record.
    0x0006007B  audio, raw s16le, contiguous to EOF

**Positions are absolute frame addresses (LBA + 150), not LBAs** — the single
fact the whole format turns on.  Each record is one INDEX point: the first
record of a track is INDEX 00, the rest are INDEX 01, 02, …  Track 1's INDEX 00
sits at absolute frame 0, i.e. LBA -150, describing lead-in that no image can
contain; its start is clamped to LBA 0.

Audio origin (``_AUDIO_OFFSET``) is the one constant that cannot be derived
from the file.  It is pinned by measurement, not by structure: the byte there is
LBA 0 of the *offset-corrected* stream, established by a unique byte-match
against our own AccurateRip-verified rip of the same disc and by AccurateRip
verifying 17/19 tracks of the result at confidence 61-67.

That leaves ``_AUDIO_OFFSET - 120`` bytes of preceding silence unaccounted for,
and 120 bytes is exactly the +30-sample read offset of the drive that wrote this
image.  Two readings fit — PlexTools stored raw audio and those 120 bytes are
real LBA-0 samples, or it stored corrected audio and simply ran 30 samples short
at the tail.  **The two predict identical bytes at every offset in the file**,
and the only region that could separate them is silence under both, so the file
cannot settle it.  Recorded as unresolved rather than guessed.
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path
from typing import IO

from cdda2img.cdtext import CDTextBlock, parse_cdtext
from cdda2img.rbi_format import FLAG_MASTER_MODE, RBIDisc, RBITocEntry

log = logging.getLogger(__name__)

_CHUNK_BYTES = 1 << 20  # 1 MiB copy buffer

_MAGIC = b"PXI\x00"

# CD-Text — a verbatim MMC READ TOC format 0x05 response.
_CDTEXT_OFFSET = 0x0B
_CDTEXT_LEN_BYTES = 2  # the BE u16 length counts everything after itself

# TOC block, and field offsets within it.
_TOC_OFFSET = 0x8000
_TOC_LEADOUT = 0x0B  # u32, absolute frame
_TOC_TRACK_COUNT = 0x47  # u8
_TOC_MCN = 0x4C  # 13 ASCII bytes
_TOC_MCN_LEN = 13

# Index table: 36-byte records, first at this absolute file offset.
_INDEX_TABLE_OFFSET = 0x8067
_INDEX_RECORD_BYTES = 36
_IDX_SESSION = 0  # u32
_IDX_TRACK = 4  # u32
_IDX_POSITION = 16  # u32, absolute frame (LBA + 150)
_IDX_LENGTH = 20  # u32, frames
_MAX_INDEX_RECORDS = 99 * 100  # 99 tracks x 100 index points, a runaway guard

# Audio. See the module docstring — measured, not derived.
_AUDIO_OFFSET = 0x6007B

_CDDA_SECTOR_BYTES = 2352
_LEAD_IN_FRAMES = 150  # absolute frame 150 == LBA 0
_MAX_TRACKS = 99


class PXIError(ValueError):
    """A ``.pxi`` file that cannot be parsed as a single-session CD-DA image."""


# ---------------------------------------------------------------------------
# Index table
# ---------------------------------------------------------------------------


def _read_index_records(buf: bytes, filename: str) -> list[tuple[int, int, int, int]]:
    """Return ``(session, track, position, length)`` per index record.

    Reads until an all-zero record or the end of the buffer.  Positions are
    absolute frames as stored; no LBA conversion happens here.
    """
    records: list[tuple[int, int, int, int]] = []
    off = _INDEX_TABLE_OFFSET - _TOC_OFFSET
    while len(records) < _MAX_INDEX_RECORDS:
        if off + _INDEX_RECORD_BYTES > len(buf):
            break
        session = struct.unpack_from("<I", buf, off + _IDX_SESSION)[0]
        track = struct.unpack_from("<I", buf, off + _IDX_TRACK)[0]
        position = struct.unpack_from("<I", buf, off + _IDX_POSITION)[0]
        length = struct.unpack_from("<I", buf, off + _IDX_LENGTH)[0]
        if not (session or track or position or length):
            break
        records.append((session, track, position, length))
        off += _INDEX_RECORD_BYTES

    if not records:
        msg = f"{filename}: index table is empty — not a PlexTools CD-DA image"
        raise PXIError(msg)
    return records


def _group_by_track(
    records: list[tuple[int, int, int, int]], filename: str
) -> list[list[tuple[int, int]]]:
    """Group index records into one ``[(position, length), …]`` list per track.

    Tracks must appear once, contiguously, in ascending order — the format has
    no facility for anything else and a file that violates it is not one we
    understand well enough to import.
    """
    sessions = {r[0] for r in records}
    if sessions != {1}:
        msg = (
            f"{filename}: multi-session images are not supported"
            f" (sessions found: {sorted(sessions)})"
        )
        raise PXIError(msg)

    groups: list[list[tuple[int, int]]] = []
    seen: list[int] = []
    for _session, track, position, length in records:
        if not seen or track != seen[-1]:
            if track in seen:
                msg = f"{filename}: track {track} appears in two separate runs of the index table"
                raise PXIError(msg)
            if seen and track < seen[-1]:
                msg = f"{filename}: track {track} follows track {seen[-1]} — index table is not ascending"
                raise PXIError(msg)
            seen.append(track)
            groups.append([])
        groups[-1].append((position, length))

    if seen != list(range(seen[0], seen[0] + len(seen))):
        msg = f"{filename}: track numbers are not consecutive: {seen}"
        raise PXIError(msg)
    if len(groups) > _MAX_TRACKS:
        msg = f"{filename}: {len(groups)} tracks exceeds the Red Book maximum of {_MAX_TRACKS}"
        raise PXIError(msg)
    return groups


def _validate_layout(
    records: list[tuple[int, int, int, int]],
    groups: list[list[tuple[int, int]]],
    leadout: int,
    declared_tracks: int,
    filename: str,
) -> None:
    """Check the index table describes a gapless disc ending at the lead-out.

    The two independent track counts — the ``0x47`` byte and the table itself —
    are cross-checked rather than one being trusted, because a disagreement
    means the layout is not the one this parser was written against.
    """
    if declared_tracks != len(groups):
        msg = (
            f"{filename}: TOC declares {declared_tracks} tracks but the index table"
            f" describes {len(groups)}"
        )
        raise PXIError(msg)

    for (_s, track, position, length), (_s2, _t2, next_position, _l2) in zip(
        records, records[1:]
    ):
        if position + length != next_position:
            msg = (
                f"{filename}: gap in the index table at track {track}:"
                f" frame {position}+{length} does not meet {next_position}"
            )
            raise PXIError(msg)

    _s, _t, last_position, last_length = records[-1]
    if last_position + last_length != leadout:
        msg = (
            f"{filename}: index table ends at frame {last_position + last_length}"
            f" but the lead-out is at {leadout}"
        )
        raise PXIError(msg)

    for group in groups:
        if len(group) < 2:
            msg = (
                f"{filename}: a track has {len(group)} index point(s);"
                " INDEX 00 and INDEX 01 are both required"
            )
            raise PXIError(msg)


# ---------------------------------------------------------------------------
# CD-Text
# ---------------------------------------------------------------------------


def _read_cdtext(raw_head: bytes, filename: str) -> CDTextBlock | None:
    """Decode block 0 of the embedded CD-Text, or None when absent/undecodable."""
    if len(raw_head) < _CDTEXT_OFFSET + _CDTEXT_LEN_BYTES:
        return None
    declared = struct.unpack_from(">H", raw_head, _CDTEXT_OFFSET)[0]
    if declared <= _CDTEXT_LEN_BYTES:
        return None  # header present, no packs

    end = _CDTEXT_OFFSET + _CDTEXT_LEN_BYTES + declared
    if end > len(raw_head):
        log.warning(
            "%s: CD-Text declares %d bytes but the header region holds %d — ignoring",
            filename,
            declared,
            len(raw_head) - _CDTEXT_OFFSET - _CDTEXT_LEN_BYTES,
        )
        return None

    try:
        blocks = parse_cdtext(raw_head[_CDTEXT_OFFSET:end])
    except ValueError as exc:
        log.warning("%s: CD-Text did not decode (%s) — ignoring", filename, exc)
        return None
    return next((b for b in blocks if b.block == 0), None)


def _cdtext_matches_disc(block: CDTextBlock, first: int, last: int) -> bool:
    """True when the CD-Text SIZE_INFO track range agrees with the disc's.

    Same guard as the rip path's (``subq_toc._cdtext_matches_disc``): prefer no
    CD-Text over CD-Text belonging to a different disc.  Absent SIZE_INFO is not
    a mismatch — it is no evidence either way.
    """
    if block.first_track is None or block.last_track is None:
        return True
    return block.first_track == first and block.last_track == last


# ---------------------------------------------------------------------------
# RBIDisc construction
# ---------------------------------------------------------------------------


def _build_disc(
    groups: list[list[tuple[int, int]]],
    first_track: int,
    catalog: str | None,
    cdtext: CDTextBlock | None,
) -> RBIDisc:
    """Build an RBIDisc from the grouped index points.

    ``start_frame`` is the PCM offset of the track's pre-gap and
    ``start_frame + pregap_frames`` its audio start — the LSN that AccurateRip,
    CDDB and the MB disc ID are all computed from.  Track 1's INDEX 00 addresses
    lead-in that the image cannot contain, so its start clamps to LBA 0 and the
    frames between LBA 0 and INDEX 01 become its pre-gap (the ABBA *Gold*
    program-area pre-gap case; see ``subq_toc.build_rip_info``).
    """
    album = (cdtext.album_title if cdtext else None) or ""
    artist = (cdtext.album_performer if cdtext else None) or ""

    disc = RBIDisc(
        album=album,
        artist=artist,
        catalog=catalog,
        cdtext_catalog_ref=cdtext.disc_id if cdtext else None,
    )

    for offset, group in enumerate(groups):
        number = first_track + offset
        index00_lba = group[0][0] - _LEAD_IN_FRAMES
        index01_lba = group[1][0] - _LEAD_IN_FRAMES

        start_frame = max(index00_lba, 0)
        pregap_frames = index01_lba - start_frame
        duration_frames = sum(length for _position, length in group[1:])
        index_points = [
            position - _LEAD_IN_FRAMES - index01_lba for position, _length in group[2:]
        ]

        title = (cdtext.track_title(number) if cdtext else None) or ""
        performer = (cdtext.track_performer(number) if cdtext else None) or artist

        disc.tracks.append(
            RBITocEntry(
                track_number=number,
                title=title,
                performer=performer,
                start_frame=start_frame,
                duration_frames=duration_frames,
                pregap_frames=pregap_frames,
                index_points=index_points,
            )
        )

    return disc


# ---------------------------------------------------------------------------
# PCM extraction
# ---------------------------------------------------------------------------


def _write_pcm(f: IO[bytes], total_frames: int, pcm_out: Path) -> int:
    """Copy the audio region to *pcm_out*; return the number of pad bytes added.

    PXI stores raw s16le, so the copy is verbatim.  The stored audio can fall
    short of the lead-out by a partial sector (see the module docstring); the
    tail is zero-filled to the declared length so the PCM block and the TOC
    agree, because every downstream consumer slices the PCM using the TOC.
    """
    want = total_frames * _CDDA_SECTOR_BYTES
    written = 0
    f.seek(_AUDIO_OFFSET)
    with open(pcm_out, "wb") as out:
        while written < want:
            chunk = f.read(min(_CHUNK_BYTES, want - written))
            if not chunk:
                break
            out.write(chunk)
            written += len(chunk)

        pad = want - written
        if pad:
            out.write(b"\x00" * pad)
    return pad


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _parse_pxi(pxi_path: Path) -> tuple[RBIDisc, bool, int]:
    """Parse a PlexTools image without writing PCM.

    Returns ``(disc, has_cdtext, total_frames)`` where *total_frames* is the
    lead-out LBA, i.e. the full sector count of the audio region.
    """
    name = pxi_path.name
    with open(pxi_path, "rb") as f:
        head = f.read(_TOC_OFFSET)
        toc = f.read(
            _INDEX_TABLE_OFFSET - _TOC_OFFSET + _MAX_INDEX_RECORDS * _INDEX_RECORD_BYTES
        )

    if head[: len(_MAGIC)] != _MAGIC:
        msg = f"{name}: not a PlexTools image (missing {_MAGIC!r} magic)"
        raise PXIError(msg)
    if len(toc) < _INDEX_TABLE_OFFSET - _TOC_OFFSET:
        msg = f"{name}: file ends before the TOC block at 0x{_TOC_OFFSET:x}"
        raise PXIError(msg)

    leadout = struct.unpack_from("<I", toc, _TOC_LEADOUT)[0]
    if leadout <= _LEAD_IN_FRAMES:
        msg = f"{name}: lead-out at absolute frame {leadout} is not a readable disc"
        raise PXIError(msg)
    total_frames = leadout - _LEAD_IN_FRAMES

    declared_tracks = toc[_TOC_TRACK_COUNT]
    mcn_raw = toc[_TOC_MCN : _TOC_MCN + _TOC_MCN_LEN]
    mcn = mcn_raw.split(b"\x00")[0].decode("ascii", errors="replace").strip()
    catalog = mcn if mcn and set(mcn) != {"0"} else None

    records = _read_index_records(toc, name)
    groups = _group_by_track(records, name)
    _validate_layout(records, groups, leadout, declared_tracks, name)

    first_track = records[0][1]
    last_track = first_track + len(groups) - 1

    cdtext = _read_cdtext(head, name)
    if cdtext is not None and not _cdtext_matches_disc(cdtext, first_track, last_track):
        log.warning(
            "%s: CD-Text covers tracks %s-%s but the disc has %d-%d — discarding it",
            name,
            cdtext.first_track,
            cdtext.last_track,
            first_track,
            last_track,
        )
        cdtext = None

    disc = _build_disc(groups, first_track, catalog, cdtext)
    return disc, cdtext is not None, total_frames


def info_pxi(pxi_path: Path) -> tuple[RBIDisc, bool, int]:
    """Return ``(disc, has_cdtext, file_bytes)`` without importing."""
    disc, has_cdtext, _ = _parse_pxi(pxi_path)
    return disc, has_cdtext, pxi_path.stat().st_size


def import_pxi(
    pxi_path: Path, pcm_out: Path, prov: dict[str, str] | None = None
) -> tuple[RBIDisc, int]:
    """Import a PlexTools ``.pxi`` disc image as master-mode RBI.

    Returns ``(disc, FLAG_MASTER_MODE)``.  When *prov* is given, any tail
    padding applied to reach the declared lead-out is recorded there as
    ``pxi_tail_padded`` so the fabricated samples stay identifiable.

    Raises :class:`PXIError` for images this parser does not understand:
    multi-session, non-consecutive or non-ascending tracks, a gap in the index
    table, a lead-out that the index table does not reach, or a track count the
    TOC and the index table disagree on.
    """
    disc, has_cdtext, total_frames = _parse_pxi(pxi_path)
    print(f"  CD-Text: {'YES' if has_cdtext else 'NO'}")

    with open(pxi_path, "rb") as f:
        pad = _write_pcm(f, total_frames, pcm_out)

    if pad:
        log.warning(
            "%s: audio ran %d bytes short of the lead-out; tail zero-filled",
            pxi_path.name,
            pad,
        )
        if prov is not None:
            prov["pxi_tail_padded"] = str(pad)

    return disc, FLAG_MASTER_MODE
