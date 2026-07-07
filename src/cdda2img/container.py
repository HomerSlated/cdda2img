"""
container.py — RBI container writer, reader, and extractor.
"""

import array
import datetime
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

from cdda2img.rbi_format import (
    ART_HEADER_SIZE,
    ART_HEADER_STRUCT,
    ART_IMAGE_FORMAT_JPEG,
    BLOCK_FLAG_SKIP,
    BLOCK_TYPE_ARIP,
    BLOCK_TYPE_ART,
    BLOCK_TYPE_CTDB,
    BLOCK_TYPE_PCM,
    BLOCK_TYPE_PROV,
    BLOCK_TYPE_RGDB,
    BLOCK_TYPE_RLOG,
    BLOCK_TYPE_TOC,
    CD_FRAMES_PER_SECOND,
    DIR_ENTRY_SIZE,
    DIR_ENTRY_STRUCT,
    FLAG_MASTER_MODE,
    HEADER_FIXED_SIZE,
    HEADER_STRUCT,
    MAGIC,
    MAX_TRACKS,
    OFFSET_DIR_OFFSET,
    PCM_BIT_DEPTH,
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
    VERSION_MAJOR,
    VERSION_MINOR,
    RBIAlbumArt,
    RBIDirEntry,
    RBIDisc,
    RBIHeader,
    RBIReplayGain,
    format_disc_metadata,
    format_original_fields,
    year_of,
)

_TOOL_VERSION = importlib.metadata.version("cdda2img")


# ---------------------------------------------------------------------------
# Checksum helpers
# ---------------------------------------------------------------------------


def _checksum_bytes(data: bytes) -> bytes:
    import blake3 as _blake3

    return _blake3.blake3(data).digest()


def _checksum_file(path: Path) -> bytes:
    import blake3 as _blake3

    h = _blake3.blake3()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.digest()


def _sha256_bytes(data: bytes) -> bytes:
    """SHA-256 digest — used only when reading v4.x containers."""
    return hashlib.sha256(data).digest()


# ---------------------------------------------------------------------------
# Temporary file management
# ---------------------------------------------------------------------------


def resolve_temp_dir(min_required_bytes: int = 100_000_000) -> Path:
    candidates = [
        os.getenv("TMP"),
        os.getenv("TEMP"),
        os.getenv("TMPDIR"),
        # Prefer disk-backed /var/tmp over a RAM-backed /tmp (tmpfs): a whole-disc rip's
        # PCM plus apply_offset's transient copy is >1.5 GB, which floods a small tmpfs.
        # Skipped automatically if absent / too small (the free-space check below).
        "/var/tmp",  # noqa: S108 — a validated candidate, not an unchecked temp path
        tempfile.gettempdir(),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        p = Path(candidate)
        if (
            p.is_dir()
            and os.access(p, os.R_OK | os.W_OK)
            and shutil.disk_usage(p).free >= min_required_bytes
        ):
            return p
    msg = "No suitable temporary directory with enough free space."
    raise RuntimeError(msg)


class TempFiles:
    """Per-invocation scratch workspace, isolated in its own unique subdirectory.

    Every run gets a fresh ``mkdtemp`` directory under *base_dir*, so a fragment
    left by a previous run (or written by a concurrent one) can never share a
    path with this run's output. This matters because the c2read rip path emits
    sidecar captures (``.cdtext``/``.sub``/``.fulltoc``/``.c2``) beside
    :attr:`pcm_file`, and c2read only writes ``.cdtext`` when the disc actually
    carries CD-Text. With the old fixed ``all_tracks.*`` names in a shared
    ``/var/tmp``, a disc with no CD-Text silently inherited the previous rip's
    stale ``all_tracks.cdtext`` -- baking a wrong album into the image. The
    unique directory removes that whole class of fragment reuse; :meth:`cleanup`
    then discards *all* sidecars in one shot regardless of suffix.
    """

    def __init__(self, base_dir: Path):
        # The random mkdtemp suffix is the unique identifier that binds every
        # fragment below to exactly this invocation. Same filesystem as
        # *base_dir*, so resolve_temp_dir's free-space guarantee still holds.
        self.base = Path(tempfile.mkdtemp(prefix="cdda2img_", dir=base_dir))
        self.pcm_file = self.base / "all_tracks.pcm"  # final raw PCM (stored in RBI)
        self.pcm_pre = (
            self.base / "all_tracks_pre.wav"
        )  # concatenated WAV, pre-normalisation
        self.pcm_norm = (
            self.base / "all_tracks_norm.wav"
        )  # normalised WAV (if normalisation enabled)
        self._temp_tracks: list[Path] = []

    def temp_track(self, i: int, suffix: str) -> Path:
        path = self.base / f"temp_track_{i}{suffix}"
        self._temp_tracks.append(path)
        return path

    def cleanup(self) -> None:
        # Remove the whole isolated directory: every fragment (pcm, wavs,
        # per-track temps, and any c2read .cdtext/.sub/.fulltoc/.c2 sidecars)
        # lives inside it, so one rmtree leaves nothing behind.
        shutil.rmtree(self.base, ignore_errors=True)


# ---------------------------------------------------------------------------
# PROV block builder
# ---------------------------------------------------------------------------


def _escape_prov(s: str) -> str:
    """Escape a PROV key/value so it cannot forge line structure (spec §6.3.4).

    Backslash first so a literal backslash can't collide with an introduced
    escape; then the only line terminator (U+000A) and CR. PROV is an integrity
    surface — a raw newline in a free-text value would otherwise inject a fake
    ``key=value`` provenance record (GRD-2026-0531-02).
    """
    return s.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")


def _unescape_prov(s: str) -> str:
    """Inverse of :func:`_escape_prov` (spec §6.3.4).

    Scans left to right so ``\\\\`` is consumed as one literal backslash rather
    than re-interpreted; an undefined ``\\x`` sequence (or a trailing backslash)
    is preserved verbatim.
    """
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        if s[i] == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "r":
                out.append("\r")
                i += 2
                continue
        out.append(s[i])
        i += 1
    return "".join(out)


def build_prov_block(data: dict[str, str]) -> bytes:
    """Serialise a provenance dict as UTF-8 key=value text (one pair per line).

    Always prepends ``creator`` and ``created``; caller-supplied values override
    if those keys are present in *data*. Keys and values are escaped per spec
    §6.3.4 so a free-text value cannot forge additional provenance records.
    """
    merged: dict[str, str] = {
        "creator": f"cdda2img v{_TOOL_VERSION}",
        "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    merged.update(data)
    return "\n".join(
        f"{_escape_prov(str(k))}={_escape_prov(str(v))}" for k, v in merged.items()
    ).encode("utf-8")


def pack_art_block(art: RBIAlbumArt) -> bytes:
    """Serialise an RBIAlbumArt to the on-disk ART block bytes (header + payload)."""
    header = struct.pack(
        ART_HEADER_STRUCT,
        art.art_version,
        art.image_format,
        art.width,
        art.height,
        len(art.image_data),
    )
    return header + art.image_data


def unpack_art_block(data: bytes) -> RBIAlbumArt | None:
    """Deserialise an ART block. Returns None if the header is malformed."""
    if len(data) < ART_HEADER_SIZE:
        return None
    art_version, image_format, width, height, image_length = struct.unpack(
        ART_HEADER_STRUCT, data[:ART_HEADER_SIZE]
    )
    if image_length != len(data) - ART_HEADER_SIZE:
        return None
    return RBIAlbumArt(
        art_version=art_version,
        image_format=image_format,
        width=width,
        height=height,
        image_data=data[ART_HEADER_SIZE:],
    )


# ---------------------------------------------------------------------------
# Container writer
# ---------------------------------------------------------------------------


def build_container(
    pcm_path: Path,
    toc_data: bytes,
    disc: RBIDisc,
    output_file: Path,
    rg_block: bytes | None = None,
    arip_block: bytes | None = None,
    rlog_block: bytes | None = None,
    prov_data: dict[str, str] | None = None,
    album_art: RBIAlbumArt | None = None,
    extra_flags: int = 0,
    quiet: bool = False,
) -> None:
    """Assemble and write an RBI v5.0 container from raw PCM and TOC data.

    Blocks are written in order: TOC → PROV → RGDB → ARIP → RLOG → ART → PCM.
    The block directory is appended last, and ``dir_offset`` is patched into the
    fixed header via a seek after all data is written.

    *extra_flags* is OR-ed into the flags word. Use FLAG_MASTER_MODE for
    master-mode containers.
    """
    prov_block = build_prov_block(prov_data) if prov_data is not None else None
    art_block = pack_art_block(album_art) if album_art is not None else None

    # TOC + PCM always present; each optional block adds one directory entry.
    dir_count = 2 + sum(
        b is not None for b in (prov_block, rg_block, arip_block, rlog_block, art_block)
    )

    header = struct.pack(
        HEADER_STRUCT,
        MAGIC,
        VERSION_MAJOR,
        VERSION_MINOR,
        extra_flags,
        disc.track_count,
        disc.disc_number,
        disc.disc_total,
        PCM_SAMPLE_RATE,
        PCM_CHANNELS,
        PCM_BIT_DEPTH,
        0,  # dir_offset placeholder; patched after blocks are written
        dir_count,
        b"\x00" * 7,  # reserved
    )
    assert len(header) == HEADER_FIXED_SIZE  # noqa: S101  # LINT-006

    # Collect (type_id, block_flags, offset, length, checksum) as we write
    dir_entries: list[tuple[bytes, int, int, int, bytes]] = []

    with open(output_file, "wb") as out:
        out.write(header)

        # TOC block
        toc_offset = out.tell()
        out.write(toc_data)
        dir_entries.append((
            BLOCK_TYPE_TOC,
            0,
            toc_offset,
            len(toc_data),
            _checksum_bytes(toc_data),
        ))

        # PROV block
        if prov_block is not None:
            prov_offset = out.tell()
            out.write(prov_block)
            dir_entries.append((
                BLOCK_TYPE_PROV,
                BLOCK_FLAG_SKIP,
                prov_offset,
                len(prov_block),
                _checksum_bytes(prov_block),
            ))

        # RGDB block
        if rg_block is not None:
            rg_offset = out.tell()
            out.write(rg_block)
            dir_entries.append((
                BLOCK_TYPE_RGDB,
                BLOCK_FLAG_SKIP,
                rg_offset,
                len(rg_block),
                _checksum_bytes(rg_block),
            ))

        # ARIP block
        if arip_block is not None:
            arip_offset = out.tell()
            out.write(arip_block)
            dir_entries.append((
                BLOCK_TYPE_ARIP,
                BLOCK_FLAG_SKIP,
                arip_offset,
                len(arip_block),
                _checksum_bytes(arip_block),
            ))

        # RLOG block
        if rlog_block is not None:
            rlog_offset = out.tell()
            out.write(rlog_block)
            dir_entries.append((
                BLOCK_TYPE_RLOG,
                BLOCK_FLAG_SKIP,
                rlog_offset,
                len(rlog_block),
                _checksum_bytes(rlog_block),
            ))

        # ART block
        if art_block is not None:
            art_offset = out.tell()
            out.write(art_block)
            dir_entries.append((
                BLOCK_TYPE_ART,
                BLOCK_FLAG_SKIP,
                art_offset,
                len(art_block),
                _checksum_bytes(art_block),
            ))

        # PCM block (streaming to avoid loading the whole file into memory)
        pcm_checksum = _checksum_file(pcm_path)
        pcm_size = pcm_path.stat().st_size
        pcm_offset = out.tell()
        with open(pcm_path, "rb") as pcm:
            shutil.copyfileobj(pcm, out)
        dir_entries.append((BLOCK_TYPE_PCM, 0, pcm_offset, pcm_size, pcm_checksum))

        # Write block directory
        dir_offset = out.tell()
        for type_id, block_flags, offset, length, checksum in dir_entries:
            out.write(
                struct.pack(
                    DIR_ENTRY_STRUCT, type_id, block_flags, offset, length, checksum
                )
            )

        # Patch dir_offset in header
        out.seek(OFFSET_DIR_OFFSET)
        out.write(struct.pack("<Q", dir_offset))

    if not quiet:
        print(f"Container created: {output_file}")


# ---------------------------------------------------------------------------
# Container reader
# ---------------------------------------------------------------------------


def read_header(file: Path) -> RBIHeader:
    """Read and validate the fixed header and block directory of an RBI file (v4.x and v5.0+)."""
    with open(file, "rb") as f:
        fixed = f.read(HEADER_FIXED_SIZE)
        if len(fixed) < HEADER_FIXED_SIZE:
            msg = "File too short to be a valid RBI container"
            raise ValueError(msg)

        (
            magic,
            version_major,
            version_minor,
            flags,
            track_count,
            disc_number,
            disc_total,
            pcm_sample_rate,
            pcm_channels,
            pcm_bit_depth,
            dir_offset,
            dir_count,
            _reserved,
        ) = struct.unpack(HEADER_STRUCT, fixed)

        if magic != MAGIC:
            msg = f"Invalid magic bytes: {magic!r}"
            raise ValueError(msg)
        if version_major != VERSION_MAJOR:
            msg = (
                f"Unsupported format version: {version_major}.{version_minor} "
                f"(this reader requires major version {VERSION_MAJOR})"
            )
            raise ValueError(msg)

        f.seek(dir_offset)
        directory: list[RBIDirEntry] = []
        for _ in range(dir_count):
            entry_raw = f.read(DIR_ENTRY_SIZE)
            if len(entry_raw) < DIR_ENTRY_SIZE:
                msg = "Truncated block directory"
                raise ValueError(msg)
            e_type_id, e_flags, e_offset, e_length, e_checksum = struct.unpack(
                DIR_ENTRY_STRUCT, entry_raw
            )
            directory.append(
                RBIDirEntry(
                    type_id=e_type_id,
                    block_flags=e_flags,
                    offset=e_offset,
                    length=e_length,
                    checksum=e_checksum,
                )
            )

    return RBIHeader(
        version_major=version_major,
        version_minor=version_minor,
        flags=flags,
        track_count=track_count,
        disc_number=disc_number,
        disc_total=disc_total,
        pcm_sample_rate=pcm_sample_rate,
        pcm_channels=pcm_channels,
        pcm_bit_depth=pcm_bit_depth,
        dir_offset=dir_offset,
        dir_count=dir_count,
        directory=directory,
    )


# ---------------------------------------------------------------------------
# Container extractor
# ---------------------------------------------------------------------------


def _stream_sha256(f, length: int) -> bytes:
    """SHA-256 streaming digest — used only when reading v4.x containers."""
    h = hashlib.sha256()
    remaining = length
    while remaining > 0:
        chunk = f.read(min(65536, remaining))
        if not chunk:
            break
        h.update(chunk)
        remaining -= len(chunk)
    return h.digest()


def _stream_checksum(f, length: int) -> bytes:
    """BLAKE3 streaming digest — used for v5.0+ containers."""
    import blake3 as _blake3

    h = _blake3.blake3()
    remaining = length
    while remaining > 0:
        chunk = f.read(min(65536, remaining))
        if not chunk:
            break
        h.update(chunk)
        remaining -= len(chunk)
    return h.digest()


def _copy_bytes(f_in, f_out, length: int) -> None:
    remaining = length
    while remaining > 0:
        chunk = f_in.read(min(65536, remaining))
        if not chunk:
            break
        f_out.write(chunk)
        remaining -= len(chunk)


def _copy_bytes_swapped(f_in, f_out, length: int) -> None:
    """Copy *length* bytes from f_in to f_out, swapping every 16-bit word (s16le↔s16be)."""
    remaining = length
    while remaining > 0:
        chunk = f_in.read(min(65536, remaining))
        if not chunk:
            break
        a = array.array("h", chunk)
        a.byteswap()
        f_out.write(a.tobytes())
        remaining -= len(chunk)


def _warn_checksum(label: str, computed: bytes, expected: bytes) -> None:
    if computed != expected:
        print(f"Warning: {label} checksum mismatch — file may be corrupt")


@dataclass
class ExtractOptions:
    raw: bool = False
    tracks: bool = False
    rg: bool = False
    ar: bool = False
    log: bool = False
    albumart: bool = False
    embedart: bool = False
    normalize: bool = False
    warn_missing: bool = True


def _write_bin_format_hint(out_dir: Path, stem: str) -> None:
    hint = (
        "BIN file format\n"
        "===============\n"
        "The .bin file contains raw CD-DA audio in disc-native byte order (s16be).\n"
        "Sample rate: 44100 Hz, stereo (2 channels), 16-bit signed integer.\n"
        "Byte order: big-endian (s16be) — byte-swapped from the s16le stored in the RBI.\n"
        "\n"
        "To burn with cdrdao:\n"
        "  cdrdao write --device /dev/sr0 <stem>.toc\n"
        "\n"
        "The .toc file references the .bin by name; both must be in the same directory.\n"
    )
    (out_dir / f"{stem}.bin_format.txt").write_text(hint, encoding="utf-8")


def _rg_json_str(rg_data: RBIReplayGain) -> str:
    rg_dict = {
        "reference_loudness_lufs": rg_data.rg_reference,
        "algorithm": "ITU-R BS.1770-3",
        "album_gain_db": round(rg_data.album_gain, 6),
        "album_peak": round(rg_data.album_peak, 6),
        "album_range_lu": round(rg_data.album_range, 6),
        "tracks": [
            {
                "number": i + 1,
                "gain_db": round(rg_data.track_gain[i], 6),
                "peak": round(rg_data.track_peak[i], 6),
                "range_lu": round(rg_data.track_range[i], 6),
            }
            for i in range(len(rg_data.track_gain))
        ],
    }
    return json.dumps(rg_dict, indent=2)


def _write_rg_json(path: Path, rg_data: RBIReplayGain) -> None:
    path.write_text(_rg_json_str(rg_data))
    print(f"RG data saved: {path}")


def _gain_from_rg(rg_data: RBIReplayGain) -> float:
    """Compute linear gain factor from stored RG data, clamped to avoid true-peak clipping."""
    gf = 10.0 ** (rg_data.album_gain / 20.0)
    if rg_data.album_peak > 0:
        gf = min(gf, 1.0 / rg_data.album_peak)
    return gf


def _extract_art_sidecar(
    container_file: Path, art_entry: RBIDirEntry, dest: Path
) -> None:
    """Read the ART block from *container_file* and write the JPEG payload to *dest*."""
    with open(container_file, "rb") as f:
        f.seek(art_entry.offset)
        art_raw = f.read(art_entry.length)
    art = unpack_art_block(art_raw)
    if art is None:
        print(
            f"Warning: ART block is malformed — skipping {dest.name}", file=sys.stderr
        )
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(art.image_data)
    print(f"Album art saved: {dest}")


def _read_art_jpeg(container_file: Path, art_entry: RBIDirEntry) -> bytes | None:
    """Return the raw JPEG payload from the ART block, or None if malformed."""
    with open(container_file, "rb") as f:
        f.seek(art_entry.offset)
        art_raw = f.read(art_entry.length)
    art = unpack_art_block(art_raw)
    return art.image_data if art is not None else None


def _measure_gain_from_container(container_file: Path, pcm_offset: int, disc) -> float:
    """Measure album EBU R128 from raw PCM inside the container; return clamped gain factor."""
    import numpy as np
    import pyebur128

    _RG_REF = -18.0
    _MODE = 63  # _MODE_I | _MODE_LRA | _MODE_TRUE_PK; matches replaygain._EBUR128_MODE
    bytes_per_frame = (
        (PCM_SAMPLE_RATE // CD_FRAMES_PER_SECOND) * PCM_CHANNELS * (PCM_BIT_DEPTH // 8)
    )
    album_state = pyebur128.R128State(PCM_CHANNELS, PCM_SAMPLE_RATE, _MODE)
    with open(container_file, "rb") as f:
        for track in disc.tracks:
            f.seek(pcm_offset + track.audio_start_frame * bytes_per_frame)
            raw = f.read(track.duration_frames * bytes_per_frame)
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            album_state.add_frames(samples, len(samples) // PCM_CHANNELS)
    lufs = pyebur128.get_loudness_global(album_state)
    peak = max(pyebur128.get_true_peak(album_state, ch) for ch in range(PCM_CHANNELS))
    gf = 10.0 ** ((_RG_REF - lufs) / 20.0)
    return min(gf, 1.0 / peak) if peak > 0 else gf


def extract_data(  # noqa: C901
    container_file: Path,
    opts: ExtractOptions,
    base_dir: Path | None = None,
) -> None:
    """Extract blocks from an RBI container according to opts.

    base_dir is the root of the extraction output tree (default: cwd/extracted).
    Layout: raw/ for TOC+BIN, <artist>/<album>/ for FLACs, stem.* for single files.
    """
    from cdda2img.replaygain import analyse, embed_rg_tags, unpack_rg_block
    from cdda2img.toc_parser import parse_toc
    from cdda2img.track_extract import (
        collect_track_flac_paths,
        collect_tracks_output_paths,
        extract_tracks,
        write_cue,
    )

    if base_dir is None:
        base_dir = Path.cwd() / "extracted"
    base_dir.mkdir(parents=True, exist_ok=True)

    header = read_header(container_file)
    stem = container_file.stem

    _csum_bytes = _checksum_bytes if header.version_major >= 5 else _sha256_bytes
    _csum_stream = _stream_checksum if header.version_major >= 5 else _stream_sha256

    toc_entry = header.find_block(BLOCK_TYPE_TOC)
    pcm_entry = header.find_block(BLOCK_TYPE_PCM)
    if toc_entry is None or pcm_entry is None:
        msg = "Missing required TOC or PCM block in container"
        raise ValueError(msg)

    with open(container_file, "rb") as f:
        f.seek(toc_entry.offset)
        toc_data = f.read(toc_entry.length)
        f.seek(pcm_entry.offset)
        pcm_checksum = _csum_stream(f, pcm_entry.length)

    _warn_checksum("TOC", _csum_bytes(toc_data), toc_entry.checksum)
    _warn_checksum("PCM", pcm_checksum, pcm_entry.checksum)

    prov: dict[str, str] = {}
    prov_entry = header.find_block(BLOCK_TYPE_PROV)
    if prov_entry is not None:
        with open(container_file, "rb") as f:
            f.seek(prov_entry.offset)
            prov_raw = f.read(prov_entry.length)
        prov = _parse_provenance(prov_raw)

    creator = prov.get("creator", "")
    created = prov.get("created", "")
    comment = f"{creator} on {created}" if creator and created else creator or created

    rg_data = None
    rg_entry = header.find_block(BLOCK_TYPE_RGDB)
    if rg_entry is not None:
        with open(container_file, "rb") as f:
            f.seek(rg_entry.offset)
            rg_raw = f.read(rg_entry.length)
        if _csum_bytes(rg_raw) == rg_entry.checksum:
            rg_data = unpack_rg_block(rg_raw, header.track_count)
        else:
            print(
                "Warning: RG block checksum mismatch — ReplayGain data may be corrupt"
            )

    disc = parse_toc(toc_data)
    art_entry = header.find_block(BLOCK_TYPE_ART)

    _would_write: list[Path] = []
    if opts.raw:
        _would_write += [
            base_dir / f"{stem}.toc",
            base_dir / f"{stem}.bin",
            base_dir / f"{stem}.bin_format.txt",
        ]
        if opts.albumart and art_entry is not None:
            _would_write.append(base_dir / f"{stem}.jpg")
    track_out_paths: list[Path] = []
    if opts.tracks:
        track_out_paths = collect_tracks_output_paths(
            disc, header.disc_number, header.disc_total, base_dir
        )
        _would_write += track_out_paths
        if art_entry is not None and track_out_paths:
            _would_write.append(track_out_paths[0].parent / "folder.jpg")
    if opts.rg and rg_data is not None:
        _would_write.append(base_dir / f"{stem}.rg.json")
    if opts.ar and header.find_block(BLOCK_TYPE_ARIP) is not None:
        _would_write.append(base_dir / f"{stem}.accurip")
    if opts.log and header.find_block(BLOCK_TYPE_RLOG) is not None:
        _would_write.append(base_dir / f"{stem}.log")
    existing = [p for p in _would_write if p.exists()]
    if existing:
        if not sys.stdin.isatty():
            msg = f"{existing[0]} already exists; aborting in non-interactive mode"
            raise FileExistsError(msg)
        print(
            f"\n{len(existing)} output file(s) already exist and would be overwritten:"
        )
        for p in existing[:5]:
            print(f"  {p}")
        if len(existing) > 5:
            print(f"  ... and {len(existing) - 5} more")
        if input("Overwrite? [y/N] ").strip().lower() not in ("y", "yes"):
            return

    if opts.raw:
        raw_dir = base_dir

        toc_text = re.sub(
            r'FILE "[^"]*"', f'FILE "{stem}.bin"', toc_data.decode("utf-8")
        )
        (raw_dir / f"{stem}.toc").write_text(toc_text, encoding="utf-8")
        print(f"TOC saved: {raw_dir / f'{stem}.toc'}")

        bin_path = raw_dir / f"{stem}.bin"
        with open(container_file, "rb") as f_in, open(bin_path, "wb") as f_out:
            f_in.seek(pcm_entry.offset)
            _copy_bytes_swapped(f_in, f_out, pcm_entry.length)
        print(f"BIN saved: {bin_path}  (s16le → s16be, disc-native byte order)")
        _write_bin_format_hint(raw_dir, stem)
        if comment:
            print(f"Created:   {comment}")
        _print_provenance(prov)

        if opts.albumart and art_entry is not None:
            _extract_art_sidecar(container_file, art_entry, base_dir / f"{stem}.jpg")

    if opts.tracks:
        gain_factor: float | None = None
        if opts.normalize:
            if rg_data is not None:
                gain_factor = _gain_from_rg(rg_data)
            else:
                print("  Measuring loudness (EBU R128)...")
                gain_factor = _measure_gain_from_container(
                    container_file, pcm_entry.offset, disc
                )
        # Read art once; reuse for both FLAC embedding and folder.jpg.
        cover_jpeg: bytes | None = None
        if art_entry is not None and opts.embedart:
            from cdda2img.album_art import downscale_jpeg

            _raw = _read_art_jpeg(container_file, art_entry)
            if _raw is not None:
                cover_jpeg = downscale_jpeg(_raw, max_edge=600)

        print(f"\nExtracting {header.track_count} tracks...")
        extract_tracks(
            disc=disc,
            container_file=container_file,
            pcm_start=pcm_entry.offset,
            disc_number=header.disc_number,
            disc_total=header.disc_total,
            sample_rate=header.pcm_sample_rate,
            channels=header.pcm_channels,
            bit_depth=header.pcm_bit_depth,
            comment=comment,
            base=base_dir,
            rg_data=rg_data if not opts.normalize else None,
            gain_factor=gain_factor,
            cover_jpeg=cover_jpeg,
        )
        if cover_jpeg is not None:
            print("PICTURE tags embedded.")
        write_cue(disc, header.disc_number, header.disc_total, base_dir)
        if not opts.normalize:
            if rg_data is not None:
                print("ReplayGain tags embedded.")
            else:
                print(
                    "\nNo RG block in container — measuring loudness from extracted tracks..."
                )
                flac_paths = collect_track_flac_paths(
                    disc, header.disc_number, header.disc_total, base_dir
                )
                rg_result = analyse(flac_paths)
                print(
                    f"  Album gain: {rg_result.album_gain:+.2f} dB  "
                    f"peak: {rg_result.album_peak:.4f}  "
                    f"LRA: {rg_result.album_lra:.1f} LU"
                )
                embed_rg_tags(rg_result, flac_paths)
                print("ReplayGain tags embedded (computed post-extraction).")

        if art_entry is not None and track_out_paths:
            folder_jpg = track_out_paths[0].parent / "folder.jpg"
            _extract_art_sidecar(container_file, art_entry, folder_jpg)

    if opts.rg:
        if rg_data is not None:
            _write_rg_json(base_dir / f"{stem}.rg.json", rg_data)
        elif opts.warn_missing:
            print(
                f"Warning: no ReplayGain block in {container_file.name}",
                file=sys.stderr,
            )

    if opts.ar:
        arip_entry = header.find_block(BLOCK_TYPE_ARIP)
        if arip_entry is not None:
            from cdda2img.accuraterip import format_arip_text, unpack_arip_block

            with open(container_file, "rb") as f:
                f.seek(arip_entry.offset)
                arip_raw = f.read(arip_entry.length)
            try:
                arip = unpack_arip_block(arip_raw, header.track_count)
                ar_path = base_dir / f"{stem}.accurip"
                ar_path.write_text(format_arip_text(arip), encoding="utf-8")
                print(f"AccurateRip report saved: {ar_path}")
            except ValueError as exc:
                print(f"Warning: could not parse ARIP block: {exc}", file=sys.stderr)
        elif opts.warn_missing:
            print(
                f"Warning: no AccurateRip block in {container_file.name}",
                file=sys.stderr,
            )

    if opts.log:
        rlog_entry = header.find_block(BLOCK_TYPE_RLOG)
        if rlog_entry is not None:
            with open(container_file, "rb") as f:
                f.seek(rlog_entry.offset)
                rlog_raw = f.read(rlog_entry.length)
            log_path = base_dir / f"{stem}.log"
            log_path.write_bytes(rlog_raw)
            print(f"Rip log saved: {log_path}")
        elif opts.warn_missing:
            print(
                f"Warning: no rip log block in {container_file.name}",
                file=sys.stderr,
            )


def wav_to_raw_pcm(wav_path: Path, pcm_path: Path) -> None:
    """Strip the WAV header, writing only raw PCM frames to pcm_path."""
    with wave.open(str(wav_path), "rb") as w:
        pcm_path.write_bytes(w.readframes(w.getnframes()))


def _write_wav(
    path: Path, pcm_data: bytes, sample_rate: int, channels: int, bit_depth: int
) -> None:
    """Reconstruct a WAV file from raw PCM bytes using the given audio parameters."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(bit_depth // 8)
        w.setframerate(sample_rate)
        w.writeframes(pcm_data)


# ---------------------------------------------------------------------------
# Container inspector (l command)
# ---------------------------------------------------------------------------


def _fmt_datetime(s: str) -> str:
    """Reformat an ISO 8601 datetime string as RFC 5322 for human display."""
    try:
        dt = datetime.datetime.fromisoformat(s)
        return dt.strftime("%a, %d %b %Y %H:%M:%S %z").strip()
    except (ValueError, TypeError):
        return s


def _fmt_size(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.2f} GiB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MiB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.1f} KiB"
    return f"{n} B"


def _fmt_duration(seconds: float) -> str:
    total_s = int(seconds)
    h, rem = divmod(total_s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02}:{s:02}"
    return f"{m}:{s:02}"


def _parse_provenance(prov_bytes: bytes) -> dict[str, str]:
    """Parse a PROV block into a key→value dict.

    Splits lines on U+000A only (per spec §6.3.4 — *not* ``str.splitlines()``,
    which also breaks on U+000B/0C/85/2028/2029 and would let an escaped value
    forge a record), partitions on the first ``=`` (values may contain ``=``),
    then unescapes key and value. Skips blank lines and lines starting with
    ``#``. Value whitespace is significant and is not stripped.
    """
    try:
        text = prov_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    result: dict[str, str] = {}
    for line in text.split("\n"):
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[_unescape_prov(key)] = _unescape_prov(value)
    return result


_BLOCK_NAMES = {
    BLOCK_TYPE_TOC: "TOC",
    BLOCK_TYPE_PCM: "PCM audio",
    BLOCK_TYPE_PROV: "Provenance",
    BLOCK_TYPE_RGDB: "ReplayGain",
    BLOCK_TYPE_ARIP: "AccurateRip",
    BLOCK_TYPE_RLOG: "Rip log",
    BLOCK_TYPE_CTDB: "CTDB",
    BLOCK_TYPE_ART: "Album art",
}


def _release_intelligence_line(prov: dict[str, str]) -> str | None:
    """Return the display line for the release-intelligence section, or None.

    When the MB lookup found an original release, render the canonical
    ``Original: …`` line via the shared :func:`format_original_fields` core
    (year granularity — byte-identical to the menu and catalogue). When no
    original-release info is present but a ``release_date`` is, fall back to a
    bare ``Released:  <date>``: this is the only place a ``list`` dump surfaces
    the disc's own release date (there is no Album line in this view).
    """
    if prov.get("original_release_found") == "YES":
        oyear = prov.get("original_release_year", "")
        return format_original_fields(
            year_of(prov.get("release_date")),
            True,
            prov.get("original_release_title") or None,
            int(oyear) if oyear.isdigit() else None,
        )
    if rd := prov.get("release_date"):
        return f"Released:  {rd}"
    return None


def _print_provenance(provenance: dict[str, str]) -> None:
    if not provenance:
        return
    mode = provenance.get("mode", "?")
    mode_label = {
        "r": "rip",
        "rip": "rip",
        "i": "import",
        "import": "import",
        "c": "create",
        "create": "create",
    }.get(mode, mode)
    print(f"Mode:      {mode_label}")
    if source := provenance.get("source"):
        print(f"Source:    {source}")
    if ripper := provenance.get("ripper"):
        print(f"Ripper:    {ripper}")
    if drive_name := provenance.get("drive_name"):
        offset_str = provenance.get("drive_read_offset", "?")
        print(f"Drive:     {drive_name}  (offset {offset_str})")
    if set_title := provenance.get("set_title"):
        print(f"Set:       {set_title}")
    if ldr := provenance.get("low_dynamic_range"):
        print(f"Low DR:    {ldr}")
    if line := _release_intelligence_line(provenance):
        print(line)


def _list_info(rbi_file: Path) -> str:  # noqa: C901
    """Build the --info section for list_container as a string."""
    from cdda2img.toc_parser import parse_toc

    header = read_header(rbi_file)
    file_size = rbi_file.stat().st_size

    prov: dict[str, str] = {}
    prov_entry = header.find_block(BLOCK_TYPE_PROV)
    if prov_entry is not None:
        with open(rbi_file, "rb") as f:
            f.seek(prov_entry.offset)
            prov = _parse_provenance(f.read(prov_entry.length))

    toc_entry = header.find_block(BLOCK_TYPE_TOC)
    if toc_entry is None:
        msg = "No TOC block in container"
        raise ValueError(msg)

    mode_flags: list[str] = ["notrim" if header.is_master else "trim"]
    if header.find_block(BLOCK_TYPE_RGDB) is not None:
        mode_flags.append("ReplayGain")
    flags_str = ", ".join(mode_flags)

    creator = prov.get("creator", "")
    created = prov.get("created", "")
    created_str = (
        f"{creator} on {_fmt_datetime(created)}"
        if creator and created
        else creator or _fmt_datetime(created) or "(unknown)"
    )

    lines: list[str] = [
        f"RBI Image: {rbi_file.name}  ({_fmt_size(file_size)})",
        (
            f"Format:    v{header.version_major}.{header.version_minor}  |  "
            f"disc {header.disc_number}/{header.disc_total}  |  "
            f"{header.track_count} tracks  |  {flags_str}"
        ),
        f"Created:   {created_str}",
    ]

    # Provenance detail lines
    mode = prov.get("mode", "")
    if mode:
        mode_label = {
            "r": "rip",
            "rip": "rip",
            "i": "import",
            "import": "import",
            "c": "create",
            "create": "create",
        }.get(mode, mode)
        lines.append(f"Mode:      {mode_label}")
    if source := prov.get("source"):
        lines.append(f"Source:    {source}")
    if ripper := prov.get("ripper"):
        lines.append(f"Ripper:    {ripper}")
    if drive_name := prov.get("drive_name"):
        offset_str = prov.get("drive_read_offset", "?")
        lines.append(f"Drive:     {drive_name}  (offset {offset_str})")
    if ldr := prov.get("low_dynamic_range"):
        lines.append(f"Low DR:    {ldr}")

    lines.append("")

    col_w = 22
    hdr_line = f"{'Block':<{col_w}}  {'Offset':>14}  {'Size':>14}"
    lines.append(hdr_line)
    lines.append("-" * len(hdr_line))

    for entry in header.directory:
        name = _BLOCK_NAMES.get(
            entry.type_id,
            entry.type_id.decode("ascii", errors="replace"),
        )
        if entry.type_id == BLOCK_TYPE_PCM:
            pcm_seconds = entry.length / (
                PCM_SAMPLE_RATE * PCM_CHANNELS * (PCM_BIT_DEPTH // 8)
            )
            name = f"{name} ({_fmt_duration(pcm_seconds)})"
        lines.append(
            f"{name:<{col_w}}  {entry.offset:>14,}  {_fmt_size(entry.length):>14}"
        )

    lines.append("")

    arip_entry = header.find_block(BLOCK_TYPE_ARIP)
    if arip_entry is not None:
        from cdda2img.accuraterip import unpack_arip_block
        from cdda2img.rbi_format import (
            ARIP_STATUS_MISMATCH,
            ARIP_STATUS_NOT_IN_DB,
            ARIP_STATUS_OK,
        )

        with open(rbi_file, "rb") as f:
            f.seek(arip_entry.offset)
            arip_raw = f.read(arip_entry.length)
        try:
            arip = unpack_arip_block(arip_raw, header.track_count)
            statuses = [t.status for t in arip.tracks]
            n = len(statuses)
            lbl = f"{'AccurateRip:':<{col_w}}"
            if all(s == ARIP_STATUS_NOT_IN_DB for s in statuses):
                lines.append(f"{lbl}not in database")
            elif all(s == ARIP_STATUS_OK for s in statuses):
                min_conf = min(
                    max(t.v1_confidence, t.v2_confidence) for t in arip.tracks
                )
                lines.append(f"{lbl}{n}/{n} tracks OK  (min conf {min_conf})")
            elif all(s == ARIP_STATUS_MISMATCH for s in statuses):
                max_total = max(t.db_total for t in arip.tracks)
                lines.append(f"{lbl}in DB (max total {max_total}) but no CRC match")
            else:
                n_ok = sum(1 for s in statuses if s == ARIP_STATUS_OK)
                lines.append(f"{lbl}{n_ok}/{n} tracks verified")
        except ValueError:
            pass

    art_entry = header.find_block(BLOCK_TYPE_ART)
    if art_entry is not None:
        with open(rbi_file, "rb") as f:
            f.seek(art_entry.offset)
            art_raw = f.read(art_entry.length)
        art = unpack_art_block(art_raw)
        lbl = f"{'Album art:':<{col_w}}"
        if art is not None and art.width and art.height:
            lines.append(f"{lbl}  {'JPEG':>14}  {f'{art.width}x{art.height} px':>14}")
        elif art is not None:
            lines.append(f"{lbl}  {'JPEG':>14}  {_fmt_size(len(art.image_data)):>14}")

    lines.append("")

    with open(rbi_file, "rb") as f:
        f.seek(toc_entry.offset)
        toc_bytes = f.read(toc_entry.length)
    disc = parse_toc(toc_bytes)
    oyear = prov.get("original_release_year", "")
    # Canonical disc-metadata header — byte-identical to the menu and catalogue
    # (rbi_format.format_disc_metadata; rbi_spec.md §6.3.2). `list` prints it
    # flush-left (its own chrome convention).
    lines.extend(
        format_disc_metadata(
            album=disc.title,
            artist=disc.performer,
            release_date=prov.get("release_date"),
            label=prov.get("label"),
            country=prov.get("country"),
            catalog_number=prov.get("catalog_number"),
            mcn=disc.catalog,
            original_release_found=prov.get("original_release_found") == "YES",
            original_release_title=prov.get("original_release_title") or None,
            original_release_year=int(oyear) if oyear.isdigit() else None,
            track_count=len(disc.tracks),
        )
    )
    lines.append("")
    for track in disc.tracks:
        dur = _fmt_duration(track.duration_frames / CD_FRAMES_PER_SECOND)
        title = track.title if len(track.title) <= 52 else track.title[:49] + "..."
        lines.append(f"  {track.track_number:2d}  {title:<52}  {dur:>5}")

    return "\n".join(lines)


def _list_prov(rbi_file: Path) -> str:
    """Build the --prov section: every PROV key=value pair, decoded, in file order.

    The write path (``build_prov_block``) is general — any key round-trips — but
    ``_list_info`` only renders a curated subset, so non-curated keys (e.g.
    ``acoustid_gate``, ``release_selected_via``) are otherwise invisible. This
    dump restores read/write symmetry and makes every key greppable:
    ``cdda2img list disc.rbi --prov | grep acoustid_gate``.
    """
    header = read_header(rbi_file)
    prov_entry = header.find_block(BLOCK_TYPE_PROV)
    if prov_entry is None:
        return "(No provenance block in this container)"
    with open(rbi_file, "rb") as f:
        f.seek(prov_entry.offset)
        prov = _parse_provenance(f.read(prov_entry.length))
    if not prov:
        return "Provenance (PROV):\n  (empty)"
    lines = ["Provenance (PROV):"]
    lines.extend(f"  {k}={v}" for k, v in prov.items())
    return "\n".join(lines)


def list_container(  # noqa: C901
    rbi_file: Path,
    *,
    info: bool = True,
    rg: bool = False,
    ar: bool = False,
    log: bool = False,
    prov: bool = False,
) -> None:
    """Print a human-readable listing of an RBI file.

    Flags are additive. If none of rg/ar/log/prov are set, info defaults to True.
    All output goes to stdout; pipe to a pager yourself if needed.
    """
    parts: list[str] = []

    if info:
        parts.append(_list_info(rbi_file))

    if prov:
        parts.append(_list_prov(rbi_file))

    if rg:
        header = read_header(rbi_file)
        rg_entry = header.find_block(BLOCK_TYPE_RGDB)
        if rg_entry is not None:
            from cdda2img.replaygain import unpack_rg_block

            with open(rbi_file, "rb") as f:
                f.seek(rg_entry.offset)
                rg_raw = f.read(rg_entry.length)
            _csum = _checksum_bytes if header.version_major >= 5 else _sha256_bytes
            if _csum(rg_raw) == rg_entry.checksum:
                rg_data = unpack_rg_block(rg_raw, header.track_count)
                parts.append(_rg_json_str(rg_data))
            else:
                parts.append(
                    "(ReplayGain block checksum mismatch — data may be corrupt)"
                )
        else:
            parts.append("(No ReplayGain block in this container)")

    if ar:
        header = read_header(rbi_file)
        arip_entry = header.find_block(BLOCK_TYPE_ARIP)
        if arip_entry is not None:
            from cdda2img.accuraterip import format_arip_text, unpack_arip_block

            with open(rbi_file, "rb") as f:
                f.seek(arip_entry.offset)
                arip_raw = f.read(arip_entry.length)
            try:
                arip_obj = unpack_arip_block(arip_raw, header.track_count)
                parts.append(format_arip_text(arip_obj))
            except ValueError as exc:
                parts.append(f"(Could not parse ARIP block: {exc})")
        else:
            parts.append("(No AccurateRip block in this container)")

    if log:
        header = read_header(rbi_file)
        rlog_entry = header.find_block(BLOCK_TYPE_RLOG)
        if rlog_entry is not None:
            with open(rbi_file, "rb") as f:
                f.seek(rlog_entry.offset)
                rlog_raw = f.read(rlog_entry.length)
            try:
                parts.append(rlog_raw.decode("utf-8"))
            except UnicodeDecodeError:
                parts.append("(RLOG block is not valid UTF-8)")
        else:
            parts.append("(No rip log block in this container)")

    print("\n\n".join(parts))


# ---------------------------------------------------------------------------
# Container verifier (t command)
# ---------------------------------------------------------------------------


def verify_container(rbi_file: Path) -> bool:  # noqa: C901
    """Validate an RBI file against the RBI format specification (27 rules).

    Supports v4.x (SHA-256 checksums) and v5.0+ (BLAKE3 checksums).
    Prints a pass/fail line for each check. Returns True if all pass.
    """
    import re as _re

    passed: list[str] = []
    failed: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> bool:
        if ok:
            print(f"  [OK]   {label}")
            passed.append(label)
        else:
            suffix = f": {detail}" if detail else ""
            print(f"  [FAIL] {label}{suffix}")
            failed.append(label)
        return ok

    file_size = rbi_file.stat().st_size

    with open(rbi_file, "rb") as f:
        raw = f.read(HEADER_FIXED_SIZE)

    if not check(
        "File large enough for fixed header",
        len(raw) >= HEADER_FIXED_SIZE,
        f"got {len(raw)} B, need {HEADER_FIXED_SIZE} B",
    ):
        print("\n  1 check FAILED — cannot continue.")
        return False

    (
        magic,
        version_major,
        version_minor,
        flags,
        track_count,
        disc_number,
        disc_total,
        pcm_sample_rate,
        pcm_channels,
        pcm_bit_depth,
        dir_offset,
        dir_count,
        reserved,
    ) = struct.unpack(HEADER_STRUCT, raw)

    # Rules 1-8
    magic_ok = check("1. Magic bytes", magic == MAGIC, f"got {magic!r}")
    version_ok = check(
        "2. Format version major in supported range (4-5)",
        4 <= version_major <= VERSION_MAJOR,
        f"major version {version_major} unsupported (supported: 4-{VERSION_MAJOR})",
    )
    if not (magic_ok and version_ok):
        print(f"\n  {len(failed)} check(s) FAILED — cannot continue.")
        return False
    if version_minor > VERSION_MINOR:
        print(
            f"  [WARN] 3. Minor version {version_minor} > {VERSION_MINOR} — proceeding (minor increments are backwards-compatible)"
        )
    else:
        check(
            f"3. Format version minor known (0..{VERSION_MINOR})",
            version_minor <= VERSION_MINOR,
        )

    unknown_odd_bits = flags & ~FLAG_MASTER_MODE & 0xAAAAAAAA  # odd bit positions
    check(
        "4. No unknown odd-position flag bits",
        unknown_odd_bits == 0,
        f"flags=0x{flags:08X}, unknown odd bits 0x{unknown_odd_bits:08X}",
    )
    check(
        "5. Reserved bytes are zero",
        reserved == b"\x00" * 7,
        f"got {reserved!r}",
    )
    check(
        f"6. Track count {track_count} in range 1-{MAX_TRACKS}",
        1 <= track_count <= MAX_TRACKS,
    )
    check(
        f"7. Disc {disc_number}/{disc_total} consistent", 1 <= disc_number <= disc_total
    )
    check(
        "8. PCM parameters (44100 Hz, 2ch, 16-bit)",
        pcm_sample_rate == PCM_SAMPLE_RATE
        and pcm_channels == PCM_CHANNELS
        and pcm_bit_depth == PCM_BIT_DEPTH,
        f"got {pcm_sample_rate} Hz, {pcm_channels}ch, {pcm_bit_depth}-bit",
    )

    # Rules 9-12 (directory structural checks)
    check("9. dir_count >= 2", dir_count >= 2, f"dir_count={dir_count}")
    check("10. dir_count <= 256", dir_count <= 256, f"dir_count={dir_count}")
    check(
        "11. dir_offset >= 40",
        dir_offset >= HEADER_FIXED_SIZE,
        f"dir_offset={dir_offset}",
    )
    check(
        "12. dir_offset + dir_countx54 == file_size",
        dir_offset + dir_count * DIR_ENTRY_SIZE == file_size,
        f"dir_offset={dir_offset}, dir_count={dir_count}, expected file_size={dir_offset + dir_count * DIR_ENTRY_SIZE}, actual={file_size}",
    )

    # Read directory entries
    try:
        with open(rbi_file, "rb") as f:
            f.seek(dir_offset)
            dir_raw = f.read(dir_count * DIR_ENTRY_SIZE)
    except OSError as exc:
        check("Directory readable", False, str(exc))
        print(f"\n  {len(failed)} check(s) FAILED — cannot continue.")
        return False

    if len(dir_raw) < dir_count * DIR_ENTRY_SIZE:
        check("Directory readable", False, "truncated")
        print(f"\n  {len(failed)} check(s) FAILED — cannot continue.")
        return False

    directory: list[RBIDirEntry] = []
    for i in range(dir_count):
        chunk = dir_raw[i * DIR_ENTRY_SIZE : (i + 1) * DIR_ENTRY_SIZE]
        e_type_id, e_flags, e_offset, e_length, e_checksum = struct.unpack(
            DIR_ENTRY_STRUCT, chunk
        )
        directory.append(
            RBIDirEntry(
                type_id=e_type_id,
                block_flags=e_flags,
                offset=e_offset,
                length=e_length,
                checksum=e_checksum,
            )
        )

    toc_entries = [e for e in directory if e.type_id == BLOCK_TYPE_TOC]
    pcm_entries = [e for e in directory if e.type_id == BLOCK_TYPE_PCM]

    # Rules 13-15
    check(
        "13. Exactly one TOC entry", len(toc_entries) == 1, f"found {len(toc_entries)}"
    )
    check(
        "14. Exactly one PCM entry", len(pcm_entries) == 1, f"found {len(pcm_entries)}"
    )

    required_ids = {BLOCK_TYPE_TOC, BLOCK_TYPE_PCM}
    required_dups = [
        e.type_id
        for e in directory
        if e.type_id in required_ids
        if sum(1 for x in directory if x.type_id == e.type_id) > 1
    ]
    check(
        "15. No duplicate required block type_ids",
        len(required_dups) == 0,
        f"duplicated: {[t.decode() for t in set(required_dups)]}",
    )

    # Rules 16-17 (per-entry range checks)
    r16_ok = all(e.offset + e.length <= dir_offset for e in directory)
    check(
        "16. All blocks end before directory",
        r16_ok,
        "one or more blocks overlap the directory",
    )
    r17_ok = all(e.offset >= HEADER_FIXED_SIZE for e in directory)
    check(
        "17. All blocks start after fixed header",
        r17_ok,
        "one or more blocks overlap the fixed header",
    )

    # Rule 18: no overlapping block byte ranges
    sorted_entries = sorted(directory, key=lambda e: e.offset)
    overlaps = any(
        sorted_entries[i].offset + sorted_entries[i].length
        > sorted_entries[i + 1].offset
        for i in range(len(sorted_entries) - 1)
    )
    check("18. No overlapping block byte ranges", not overlaps)

    # Rules 19-27 require reading block data
    if toc_entries:
        toc_entry = toc_entries[0]
        with open(rbi_file, "rb") as f:
            f.seek(toc_entry.offset)
            toc_bytes = f.read(toc_entry.length)

        # Rule 19: TRACK AUDIO count matches track_count
        n_tracks_in_toc = len(re.findall(rb"^TRACK AUDIO", toc_bytes, re.MULTILINE))
        check(
            f"19. TOC TRACK AUDIO count matches header track_count ({track_count})",
            n_tracks_in_toc == track_count,
            f"TOC has {n_tracks_in_toc} TRACK AUDIO entries",
        )

        # Rule 21: TOC is valid UTF-8
        try:
            toc_bytes.decode("utf-8")
            check("21. TOC block is valid UTF-8", True)
        except UnicodeDecodeError as exc:
            check("21. TOC block is valid UTF-8", False, str(exc))
    else:
        print("  [SKIP] 19. TOC TRACK AUDIO count (no TOC entry)")
        print("  [SKIP] 21. TOC block is valid UTF-8 (no TOC entry)")

    # Rule 20: checksums for all blocks (SHA-256 for v4.x; BLAKE3 for v5.0+)
    _stream_csum = _stream_checksum if version_major >= 5 else _stream_sha256
    algo_label = "BLAKE3" if version_major >= 5 else "SHA-256"
    print("  Verifying block checksums (may take a moment for PCM)...")
    for entry in directory:
        type_name = _BLOCK_NAMES.get(
            entry.type_id, entry.type_id.decode("ascii", errors="replace")
        )
        if entry.offset >= file_size or entry.offset + entry.length > file_size:
            check(
                f"20. {type_name} block checksum ({algo_label})",
                False,
                "block out of file bounds",
            )
            continue
        with open(rbi_file, "rb") as f:
            f.seek(entry.offset)
            computed = _stream_csum(f, entry.length)
        check(
            f"20. {type_name} block checksum ({algo_label})", computed == entry.checksum
        )

    # Rule 22: PROV block UTF-8
    prov_entry = next((e for e in directory if e.type_id == BLOCK_TYPE_PROV), None)
    if prov_entry is not None:
        with open(rbi_file, "rb") as f:
            f.seek(prov_entry.offset)
            prov_bytes = f.read(prov_entry.length)
        try:
            prov_bytes.decode("utf-8")
            check("22. PROV block is valid UTF-8", True)
        except UnicodeDecodeError as exc:
            check("22. PROV block is valid UTF-8", False, str(exc))

    # Rule 23: RLOG block UTF-8
    rlog_entry = next((e for e in directory if e.type_id == BLOCK_TYPE_RLOG), None)
    if rlog_entry is not None:
        with open(rbi_file, "rb") as f:
            f.seek(rlog_entry.offset)
            rlog_bytes = f.read(rlog_entry.length)
        try:
            rlog_bytes.decode("utf-8")
            check("23. RLOG block is valid UTF-8", True)
        except UnicodeDecodeError as exc:
            check("23. RLOG block is valid UTF-8", False, str(exc))

        # Rule 27: RLOG self-seal (SHA-256 in v4.x; BLAKE3 in v5.0+)
        if version_major >= 5:
            import blake3 as _blake3

            _seal_pattern = rb"BLAKE3: [0-9a-f]{64}"
            _seal_prefix = b"BLAKE3: "
            _seal_algo = lambda b: _blake3.blake3(b).hexdigest()
            _seal_label = "27. RLOG BLAKE3 self-seal"
        else:
            _seal_pattern = rb"SHA-256: [0-9a-f]{64}"
            _seal_prefix = b"SHA-256: "
            _seal_algo = lambda b: hashlib.sha256(b).hexdigest()
            _seal_label = "27. RLOG SHA-256 self-seal"
        lines = rlog_bytes.split(b"\n")
        if lines and _re.match(_seal_pattern, lines[-1]):
            body = b"\n".join(lines[:-1]) + b"\n"
            expected_seal = lines[-1][len(_seal_prefix) :].decode()
            actual_seal = _seal_algo(body)
            check(_seal_label, actual_seal == expected_seal)
        elif lines and not lines[-1]:
            # trailing newline: try second-to-last
            if len(lines) >= 2 and _re.match(_seal_pattern, lines[-2]):
                body = b"\n".join(lines[:-2]) + b"\n"
                expected_seal = lines[-2][len(_seal_prefix) :].decode()
                actual_seal = _seal_algo(body)
                check(_seal_label, actual_seal == expected_seal)
            else:
                print(f"  [SKIP] {_seal_label} (no seal line found)")
        else:
            print(f"  [SKIP] {_seal_label} (no seal line found)")

    # Rule 24: RGDB block length
    rgdb_entry = next((e for e in directory if e.type_id == BLOCK_TYPE_RGDB), None)
    if rgdb_entry is not None:
        expected_rg_len = 17 + 12 * track_count
        check(
            f"24. RGDB block length == 17 + 12x{track_count} = {expected_rg_len}",
            rgdb_entry.length == expected_rg_len,
            f"got {rgdb_entry.length}",
        )

    # Rules 25-26: ARIP block
    arip_entry = next((e for e in directory if e.type_id == BLOCK_TYPE_ARIP), None)
    if arip_entry is not None:
        expected_arip_len = 13 + 15 * track_count
        check(
            f"25. ARIP block length == 13 + 15x{track_count} = {expected_arip_len}",
            arip_entry.length == expected_arip_len,
            f"got {arip_entry.length}",
        )
        if arip_entry.length == expected_arip_len:
            with open(rbi_file, "rb") as f:
                f.seek(arip_entry.offset + 13)  # skip 13-byte header
                arip_tracks_raw = f.read(15 * track_count)
            statuses = [arip_tracks_raw[i * 15 + 14] for i in range(track_count)]
            check(
                "26. ARIP status values in range 0-2",
                all(0 <= s <= 2 for s in statuses),
                f"invalid: {[s for s in statuses if not 0 <= s <= 2]}",
            )

    # Rules 28-30: ART block
    art_entry_v = next((e for e in directory if e.type_id == BLOCK_TYPE_ART), None)
    if art_entry_v is not None:
        check(
            "28. ART block length >= 10 (room for fixed header)",
            art_entry_v.length >= ART_HEADER_SIZE,
            f"got {art_entry_v.length}",
        )
        if art_entry_v.length >= ART_HEADER_SIZE:
            with open(rbi_file, "rb") as f:
                f.seek(art_entry_v.offset)
                art_hdr_raw = f.read(ART_HEADER_SIZE)
            _, art_img_fmt, _, _, art_img_len = struct.unpack(
                ART_HEADER_STRUCT, art_hdr_raw
            )
            check(
                "29. ART image_length == block length - 10",
                art_img_len == art_entry_v.length - ART_HEADER_SIZE,
                f"header says {art_img_len}, block minus header = {art_entry_v.length - ART_HEADER_SIZE}",
            )
            if art_img_fmt != ART_IMAGE_FORMAT_JPEG:
                print(
                    f"  [WARN] 30. ART image_format {art_img_fmt} unrecognised"
                    " — block should be skipped by strict readers"
                )
            else:
                check("30. ART image_format is recognised (1 = JPEG)", True)

    total = len(passed) + len(failed)
    print()
    if failed:
        print(f"  {len(failed)}/{total} check(s) FAILED.")
    else:
        print(f"  All {total} checks passed.")
    return len(failed) == 0
