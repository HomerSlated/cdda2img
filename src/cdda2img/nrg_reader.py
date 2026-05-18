"""
nrg_reader.py — Nero NRG disc image importer.

Supports NER5 (new format, 64-bit offsets) and NERO (old format, 32-bit offsets).
Audio must be DAO CD-DA (mode 0x07, 2352 bytes/sector).
NRG stores audio as s16le (Windows-native), matching DDP/GEAR Pro — no byteswap needed.
Multi-session images and TAO (ETN2/ETNF) images are rejected.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import IO

from cdda2img.ddp_reader import parse_cdtext_packs
from cdda2img.rbi_format import FLAG_MASTER_MODE, RBIDisc, RBITocEntry

_CHUNK_BYTES = 1 << 20  # 1 MiB read buffer

# Footer signatures
_SIG_NER5 = b"NER5"  # new format: EOF-12 sig + 8-byte uint64 BE trailer offset
_SIG_NERO = b"NERO"  # old format: EOF-8  sig + 4-byte uint32 BE trailer offset

# Trailer block IDs
_BLK_DAOX = b"DAOX"  # DAO tracks, 64-bit offsets (NER5)
_BLK_DAOI = b"DAOI"  # DAO tracks, 32-bit offsets (NERO)
_BLK_CUEX = b"CUEX"  # CUE sheet, 64-bit (NER5)
_BLK_CUES = b"CUES"  # CUE sheet, 32-bit (NERO)
_BLK_CDTX = b"CDTX"  # raw CD-Text packs (18 bytes each)
_BLK_MTYP = b"MTYP"  # media type flags
_BLK_END = b"END!"  # trailer terminator

# Media type bits (MTYP)
_MTYP_CD = 0x01
_MTYP_CDROM = 0x02

# CD-DA audio constants
_CDDA_SECTOR_BYTES = 2352
_CDDA_MODE_CODE = 0x07

# DAOX per-track: ISRC(12)+sector_size(2)+mode_code(1)+pad(3)+3x uint64 = 42 bytes
_DAOX_TRACK_FMT = ">12sHBxxx3Q"
_DAOX_TRACK_SIZE = struct.calcsize(_DAOX_TRACK_FMT)  # 42

# DAOI per-track: ISRC(12)+sector_size(2)+mode_code(1)+pad(3)+3x uint32 = 30 bytes
_DAOI_TRACK_FMT = ">12sHBxxx3I"
_DAOI_TRACK_SIZE = struct.calcsize(_DAOI_TRACK_FMT)  # 30

# DAOX/DAOI block header: dummy(4)+MCN(13)+dummy(1)+session_type(1)+unk(1)+first(1)+last(1)
_DAO_HDR_SIZE = 22


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _detect_format(f: IO[bytes]) -> tuple[bool, int]:
    """Return (new_format, trailer_offset).

    new_format=True means NER5 (64-bit offsets); False means NERO (32-bit).
    """
    f.seek(-12, 2)
    if f.read(4) == _SIG_NER5:
        return True, struct.unpack(">Q", f.read(8))[0]

    f.seek(-8, 2)
    if f.read(4) == _SIG_NERO:
        return False, struct.unpack(">I", f.read(4))[0]

    msg = "Not a Nero NRG image (NER5/NERO signature not found)"
    raise ValueError(msg)


def _parse_trailer(f: IO[bytes], trailer_offset: int) -> dict[bytes, list[bytes]]:
    """Walk the trailer and return {block_id: [block_data, …]}.

    Duplicate block IDs (e.g. two DAOX blocks in a multi-session image) produce
    a list with multiple entries — used for multi-session detection.
    """
    f.seek(trailer_offset)
    blocks: dict[bytes, list[bytes]] = {}
    while True:
        hdr = f.read(8)
        if len(hdr) < 8:
            break
        block_id = hdr[:4]
        length = struct.unpack(">I", hdr[4:])[0]
        data = f.read(length)
        if block_id == _BLK_END:
            break
        blocks.setdefault(block_id, []).append(data)
    return blocks


# ---------------------------------------------------------------------------
# DAO block parsing
# ---------------------------------------------------------------------------


def _parse_dao_tracks(data: bytes, new_format: bool) -> tuple[str | None, list[dict]]:
    """Parse a DAOX (new_format=True) or DAOI block.

    Returns (mcn, dao_tracks) where each dao_track dict has:
    isrc, sector_size, mode_code, pregap_offset, start_offset, end_offset.
    """
    if len(data) < _DAO_HDR_SIZE:
        msg = f"DAO block too short: {len(data)} bytes (expected ≥ {_DAO_HDR_SIZE})"
        raise ValueError(msg)

    mcn_raw = data[4:17].rstrip(b"\x00")
    mcn = mcn_raw.decode("ascii", errors="replace") or None
    first_track = data[20]
    last_track = data[21]
    n_tracks = max(last_track - first_track + 1, 0)

    track_fmt = _DAOX_TRACK_FMT if new_format else _DAOI_TRACK_FMT
    track_size = _DAOX_TRACK_SIZE if new_format else _DAOI_TRACK_SIZE

    tracks: list[dict] = []
    pos = _DAO_HDR_SIZE
    for _ in range(n_tracks):
        if pos + track_size > len(data):
            break
        isrc_raw, sector_size, mode_code, pregap_off, start_off, end_off = (
            struct.unpack_from(track_fmt, data, pos)
        )
        isrc = isrc_raw.rstrip(b"\x00").decode("ascii", errors="replace") or None
        tracks.append({
            "isrc": isrc,
            "sector_size": sector_size,
            "mode_code": mode_code,
            "pregap_offset": pregap_off,
            "start_offset": start_off,
            "end_offset": end_off,
        })
        pos += track_size

    return mcn, tracks


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_tracks(tracks: list[dict], filename: str) -> None:
    """Raise ValueError for any track that is not CD-DA mode 0x07 / 2352 bytes."""
    for i, t in enumerate(tracks, start=1):
        if t["mode_code"] != _CDDA_MODE_CODE:
            msg = f"{filename}: track {i} mode 0x{t['mode_code']:02x} is not supported (only CD-DA 0x07)"
            raise ValueError(msg)
        if t["sector_size"] != _CDDA_SECTOR_BYTES:
            msg = f"{filename}: track {i} sector size {t['sector_size']} is not supported (expected 2352)"
            raise ValueError(msg)
        audio_len = t["end_offset"] - t["start_offset"]
        if audio_len % _CDDA_SECTOR_BYTES:
            msg = f"{filename}: track {i} audio length {audio_len} is not a multiple of 2352"
            raise ValueError(msg)
        pregap_len = t["start_offset"] - t["pregap_offset"]
        if pregap_len % _CDDA_SECTOR_BYTES:
            msg = f"{filename}: track {i} pregap length {pregap_len} is not a multiple of 2352"
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# RBIDisc construction
# ---------------------------------------------------------------------------


def _build_disc(
    dao_tracks: list[dict],
    catalog: str | None,
    disc_title: str,
    disc_performer: str,
    disc_id: str | None,
    track_map: dict[int, tuple[str, str]],
) -> RBIDisc:
    """Build an RBIDisc from parsed NRG DAO track metadata."""
    disc = RBIDisc(
        album=disc_title,
        artist=disc_performer,
        catalog=catalog,
        disc_id=disc_id,
    )

    pcm_frame = 0  # running frame position in the output PCM stream
    for n, t in enumerate(dao_tracks, start=1):
        pregap_frames = (t["start_offset"] - t["pregap_offset"]) // _CDDA_SECTOR_BYTES
        duration_frames = (t["end_offset"] - t["start_offset"]) // _CDDA_SECTOR_BYTES

        if n == 1:
            # Track 1 lead-in pre-gap is not stored in the RBI PCM block
            start_frame = 0
            pregap_frames = 0
        else:
            start_frame = pcm_frame

        title, performer = track_map.get(n, (disc_title, disc_performer))
        disc.tracks.append(
            RBITocEntry(
                track_number=n,
                title=title,
                performer=performer,
                start_frame=start_frame,
                duration_frames=duration_frames,
                pregap_frames=pregap_frames,
                isrc=t["isrc"],
            )
        )

        pcm_frame += pregap_frames + duration_frames

    return disc


# ---------------------------------------------------------------------------
# PCM extraction
# ---------------------------------------------------------------------------


def _write_pcm(f: IO[bytes], dao_tracks: list[dict], pcm_out: Path) -> None:
    """Copy DAO audio to pcm_out as-is (NRG stores s16le — no byteswap needed).

    Track 1's pre-gap is skipped (lead-in silence; not stored in the RBI PCM block).
    Pre-gap audio for tracks 2+ is included so the RBI TOC can reference it.
    """
    with open(pcm_out, "wb") as out:
        for n, t in enumerate(dao_tracks, start=1):
            audio_start = t["start_offset"] if n == 1 else t["pregap_offset"]
            audio_end = t["end_offset"]
            f.seek(audio_start)
            remaining = audio_end - audio_start
            while remaining > 0:
                chunk = f.read(min(_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _parse_nrg(nrg_path: Path) -> tuple[RBIDisc, bool, list[dict]]:
    """Parse a Nero NRG image without writing PCM.

    Returns ``(disc, has_cdtext, dao_tracks)``.  *dao_tracks* carry byte
    offsets into the original file so ``_write_pcm`` can re-open it later.

    Raises ValueError for: multi-session images, TAO format (ETN2/ETNF),
    non-CD-DA tracks, unsupported sector sizes, and sector-alignment failures.
    """
    with open(nrg_path, "rb") as f:
        new_format, trailer_offset = _detect_format(f)
        blocks = _parse_trailer(f, trailer_offset)

    dao_key = _BLK_DAOX if new_format else _BLK_DAOI
    dao_blocks = blocks.get(dao_key, [])

    if not dao_blocks:
        if blocks.get(b"ETN2") or blocks.get(b"ETNF"):
            msg = f"{nrg_path.name}: TAO format (ETN2/ETNF) is not supported; only DAO CD-DA images"
        else:
            msg = f"{nrg_path.name}: no DAO track data found (DAOX/DAOI block missing)"
        raise ValueError(msg)

    if len(dao_blocks) > 1:
        msg = (
            f"{nrg_path.name}: multi-session NRG images are not supported"
            f" ({len(dao_blocks)} sessions found)"
        )
        raise ValueError(msg)

    mtyp_list = blocks.get(_BLK_MTYP, [])
    if mtyp_list:
        mtyp = struct.unpack(">I", mtyp_list[0][:4])[0]
        if not (mtyp & (_MTYP_CD | _MTYP_CDROM)):
            msg = f"{nrg_path.name}: unsupported media type 0x{mtyp:02x}"
            raise ValueError(msg)

    catalog, dao_tracks = _parse_dao_tracks(dao_blocks[0], new_format)
    _validate_tracks(dao_tracks, nrg_path.name)

    disc_title = disc_performer = ""
    disc_id: str | None = None
    track_map: dict[int, tuple[str, str]] = {}
    cdtx_list = blocks.get(_BLK_CDTX, [])
    has_cdtext = bool(cdtx_list)
    if has_cdtext:
        disc_title, disc_performer, disc_id, track_map = parse_cdtext_packs(
            cdtx_list[0]
        )

    disc = _build_disc(
        dao_tracks, catalog, disc_title, disc_performer, disc_id, track_map
    )
    return disc, has_cdtext, dao_tracks


def info_nrg(nrg_path: Path) -> tuple[RBIDisc, bool, int]:
    """Return ``(disc, has_cdtext, file_bytes)`` for an NRG image without importing it."""
    disc, has_cdtext, _ = _parse_nrg(nrg_path)
    return disc, has_cdtext, nrg_path.stat().st_size


def import_nrg(nrg_path: Path, pcm_out: Path) -> tuple[RBIDisc, int]:
    """Import a Nero NRG disc image as master-mode RBI.

    Returns ``(disc, FLAG_MASTER_MODE)``.

    Raises ValueError for: multi-session images, TAO format (ETN2/ETNF),
    non-CD-DA tracks, unsupported sector sizes, and sector-alignment failures.
    """
    disc, has_cdtext, dao_tracks = _parse_nrg(nrg_path)
    print(f"  CD-Text: {'YES' if has_cdtext else 'NO'}")
    with open(nrg_path, "rb") as f:
        _write_pcm(f, dao_tracks, pcm_out)
    return disc, FLAG_MASTER_MODE
