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
    BLOCK_FLAG_SKIP,
    BLOCK_TYPE_ARIP,
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
    RBIDirEntry,
    RBIDisc,
    RBIHeader,
    RBIReplayGain,
)

_TOOL_VERSION = importlib.metadata.version("cdda2img")


# ---------------------------------------------------------------------------
# Checksum helpers
# ---------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def sha256_file(path: Path) -> bytes:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.digest()


# ---------------------------------------------------------------------------
# Temporary file management
# ---------------------------------------------------------------------------


def resolve_temp_dir(min_required_bytes: int = 100_000_000) -> Path:
    candidates = [
        os.getenv("TMP"),
        os.getenv("TEMP"),
        os.getenv("TMPDIR"),
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
    def __init__(self, base_dir: Path):
        self.base = base_dir
        self.pcm_file = base_dir / "all_tracks.pcm"  # final raw PCM (stored in RBI)
        self.pcm_pre = (
            base_dir / "all_tracks_pre.wav"
        )  # concatenated WAV, pre-normalisation
        self.pcm_norm = (
            base_dir / "all_tracks_norm.wav"
        )  # normalised WAV (if normalisation enabled)
        self._temp_tracks: list[Path] = []

    def temp_track(self, i: int, suffix: str) -> Path:
        path = self.base / f"temp_track_{i}{suffix}"
        self._temp_tracks.append(path)
        return path

    def cleanup(self) -> None:
        for path in [self.pcm_file, self.pcm_pre, self.pcm_norm, *self._temp_tracks]:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# PROV block builder
# ---------------------------------------------------------------------------


def build_prov_block(data: dict[str, str]) -> bytes:
    """Serialise a provenance dict as UTF-8 key=value text (one pair per line).

    Always prepends ``creator`` and ``created``; caller-supplied values override
    if those keys are present in *data*.
    """
    merged: dict[str, str] = {
        "creator": f"cdda2img v{_TOOL_VERSION}",
        "created": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    merged.update(data)
    return "\n".join(f"{k}={v}" for k, v in merged.items()).encode("utf-8")


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
    extra_flags: int = 0,
) -> None:
    """Assemble and write an RBI v4.0 container from raw PCM and TOC data.

    Blocks are written in order: TOC → PROV → RGDB → ARIP → RLOG → PCM.  The block
    directory is appended last, and ``dir_offset`` is patched into the fixed
    header via a seek after all data is written.

    *extra_flags* is OR-ed into the flags word. Use FLAG_MASTER_MODE for
    master-mode containers.
    """
    prov_block = build_prov_block(prov_data) if prov_data is not None else None

    dir_count = 2  # TOC + PCM always present
    if prov_block is not None:
        dir_count += 1
    if rg_block is not None:
        dir_count += 1
    if arip_block is not None:
        dir_count += 1
    if rlog_block is not None:
        dir_count += 1

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
            sha256_bytes(toc_data),
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
                sha256_bytes(prov_block),
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
                sha256_bytes(rg_block),
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
                sha256_bytes(arip_block),
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
                sha256_bytes(rlog_block),
            ))

        # PCM block (streaming to avoid loading the whole file into memory)
        pcm_checksum = sha256_file(pcm_path)
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

    print(f"Container created: {output_file}")


# ---------------------------------------------------------------------------
# Container reader
# ---------------------------------------------------------------------------


def read_header(file: Path) -> RBIHeader:
    """Read and validate the fixed header and block directory of an RBI v4.0 file."""
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
    h = hashlib.sha256()
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
    normalize: bool = False
    warn_missing: bool = True


def _write_bin_format_hint(raw_dir: Path) -> None:
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
    (raw_dir / "bin_format.txt").write_text(hint, encoding="utf-8")


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
        extract_tracks,
        write_cue,
    )

    if base_dir is None:
        base_dir = Path.cwd() / "extracted"

    header = read_header(container_file)
    stem = container_file.stem

    toc_entry = header.find_block(BLOCK_TYPE_TOC)
    pcm_entry = header.find_block(BLOCK_TYPE_PCM)
    if toc_entry is None or pcm_entry is None:
        msg = "Missing required TOC or PCM block in container"
        raise ValueError(msg)

    with open(container_file, "rb") as f:
        f.seek(toc_entry.offset)
        toc_data = f.read(toc_entry.length)
        f.seek(pcm_entry.offset)
        pcm_checksum = _stream_sha256(f, pcm_entry.length)

    _warn_checksum("TOC", sha256_bytes(toc_data), toc_entry.checksum)
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
        if sha256_bytes(rg_raw) == rg_entry.checksum:
            rg_data = unpack_rg_block(rg_raw, header.track_count)
        else:
            print(
                "Warning: RG block checksum mismatch — ReplayGain data may be corrupt"
            )

    disc = parse_toc(toc_data)

    if opts.raw:
        raw_dir = base_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)

        toc_text = toc_data.decode("utf-8").replace(f'"{stem}.s16le"', f'"{stem}.bin"')
        (raw_dir / f"{stem}.toc").write_text(toc_text, encoding="utf-8")
        print(f"TOC saved: {raw_dir / f'{stem}.toc'}")

        bin_path = raw_dir / f"{stem}.bin"
        with open(container_file, "rb") as f_in, open(bin_path, "wb") as f_out:
            f_in.seek(pcm_entry.offset)
            _copy_bytes_swapped(f_in, f_out, pcm_entry.length)
        print(f"BIN saved: {bin_path}  (s16le → s16be, disc-native byte order)")
        _write_bin_format_hint(raw_dir)
        if comment:
            print(f"Created:   {comment}")
        _print_provenance(prov)

    if opts.tracks:
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
        )
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
                for warning in rg_result.warnings:
                    print(f"  Warning: {warning}")
                print(
                    f"  Album gain: {rg_result.album_gain:+.2f} dB  "
                    f"peak: {rg_result.album_peak:.4f}  "
                    f"LRA: {rg_result.album_lra:.1f} LU"
                )
                embed_rg_tags(rg_result, flac_paths)
                print("ReplayGain tags embedded (computed post-extraction).")

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

    Splits on the first ``=`` only (values may contain ``=``). Skips blank
    lines and lines starting with ``#``. Per spec §6.3, value whitespace is
    significant and is not stripped.
    """
    try:
        text = prov_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    result: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key] = value
    return result


_REMASTER_LABELS = {
    "YES": "Yes (confirmed)",
    "POSSIBLE": "Possible",
    "NO": "No",
    "UNKNOWN": "Unknown",
}

_BLOCK_NAMES = {
    BLOCK_TYPE_TOC: "TOC",
    BLOCK_TYPE_PCM: "PCM audio",
    BLOCK_TYPE_PROV: "Provenance",
    BLOCK_TYPE_RGDB: "ReplayGain",
    BLOCK_TYPE_ARIP: "AccurateRip",
    BLOCK_TYPE_RLOG: "Rip log",
    BLOCK_TYPE_CTDB: "CTDB",
}


def _print_provenance(provenance: dict[str, str]) -> None:
    if not provenance:
        return
    mode = provenance.get("mode", "?")
    mode_label = {"r": "r (rip)", "i": "i (import)", "c": "c (create)"}.get(mode, mode)
    print(f"Mode:      {mode_label}")
    if source := provenance.get("source"):
        print(f"Source:    {source}")
    if ripper := provenance.get("ripper"):
        print(f"Ripper:    {ripper}")
    if drive_name := provenance.get("drive_name"):
        offset_str = provenance.get("drive_read_offset", "?")
        print(f"Drive:     {drive_name}  (offset {offset_str})")
    if rms := provenance.get("remastered"):
        label = _REMASTER_LABELS.get(rms, rms)
        extra = ""
        if rd := provenance.get("release_date"):
            extra += f"  (this release: {rd}"
            if od := provenance.get("original_release_date"):
                extra += f", original: {od}"
            extra += ")"
        print(f"Remaster:  {label}{extra}")


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

    mode_flags: list[str] = ["master" if header.is_master else "remaster"]
    if header.find_block(BLOCK_TYPE_RGDB) is not None:
        mode_flags.append("ReplayGain")
    flags_str = ", ".join(mode_flags)

    creator = prov.get("creator", "")
    created = prov.get("created", "")
    created_str = (
        f"{creator} on {created}"
        if creator and created
        else creator or created or "(unknown)"
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
        mode_label = {"r": "r (rip)", "i": "i (import)", "c": "c (create)"}.get(
            mode, mode
        )
        lines.append(f"Mode:      {mode_label}")
    if source := prov.get("source"):
        lines.append(f"Source:    {source}")
    if ripper := prov.get("ripper"):
        lines.append(f"Ripper:    {ripper}")
    if drive_name := prov.get("drive_name"):
        offset_str = prov.get("drive_read_offset", "?")
        lines.append(f"Drive:     {drive_name}  (offset {offset_str})")
    if rms := prov.get("remastered"):
        label = _REMASTER_LABELS.get(rms, rms)
        extra = ""
        if rd := prov.get("release_date"):
            extra += f"  (this release: {rd}"
            if od := prov.get("original_release_date"):
                extra += f", original: {od}"
            extra += ")"
        lines.append(f"Remaster:  {label}{extra}")

    lines.append("")

    col_w = len("Provenance block") + 2
    hdr_line = f"{'Block':<{col_w}}  {'Offset':>14}  {'Size':>14}"
    lines.append(hdr_line)
    lines.append("-" * len(hdr_line))

    for entry in header.directory:
        name = _BLOCK_NAMES.get(
            entry.type_id,
            entry.type_id.decode("ascii", errors="replace"),
        )
        extra_str = ""
        if entry.type_id == BLOCK_TYPE_PCM:
            pcm_seconds = entry.length / (
                PCM_SAMPLE_RATE * PCM_CHANNELS * (PCM_BIT_DEPTH // 8)
            )
            extra_str = f"  ({_fmt_duration(pcm_seconds)})"
        lines.append(
            f"{name:<{col_w}}  {entry.offset:>14,}  {_fmt_size(entry.length):>14}{extra_str}"
        )

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
            if all(s == ARIP_STATUS_NOT_IN_DB for s in statuses):
                lines.append("AccurateRip:         not in database")
            elif all(s == ARIP_STATUS_OK for s in statuses):
                min_conf = min(
                    max(t.v1_confidence, t.v2_confidence) for t in arip.tracks
                )
                lines.append(
                    f"AccurateRip:         {n}/{n} tracks OK  (min conf {min_conf})"
                )
            elif all(s == ARIP_STATUS_MISMATCH for s in statuses):
                max_total = max(t.db_total for t in arip.tracks)
                lines.append(
                    f"AccurateRip:         in DB (max total {max_total}) but no CRC match"
                )
            else:
                n_ok = sum(1 for s in statuses if s == ARIP_STATUS_OK)
                lines.append(f"AccurateRip:         {n_ok}/{n} tracks verified")
        except ValueError:
            pass

    lines.append("")

    with open(rbi_file, "rb") as f:
        f.seek(toc_entry.offset)
        toc_bytes = f.read(toc_entry.length)
    disc = parse_toc(toc_bytes)
    lines.append(f"Tracks:  {disc.performer} — {disc.title}")
    lines.append("")
    for track in disc.tracks:
        dur = _fmt_duration(track.duration_frames / CD_FRAMES_PER_SECOND)
        lines.append(f"  {track.track_number:2d}  {track.title:<52}  {dur:>5}")

    return "\n".join(lines)


def list_container(  # noqa: C901
    rbi_file: Path,
    *,
    info: bool = True,
    rg: bool = False,
    ar: bool = False,
    log: bool = False,
) -> None:
    """Print a human-readable listing of an RBI file.

    Flags are additive. If none of rg/ar/log are set, info defaults to True.
    All output goes to stdout; pipe to a pager yourself if needed.
    """
    parts: list[str] = []

    if info:
        parts.append(_list_info(rbi_file))

    if rg:
        header = read_header(rbi_file)
        rg_entry = header.find_block(BLOCK_TYPE_RGDB)
        if rg_entry is not None:
            from cdda2img.replaygain import unpack_rg_block

            with open(rbi_file, "rb") as f:
                f.seek(rg_entry.offset)
                rg_raw = f.read(rg_entry.length)
            if sha256_bytes(rg_raw) == rg_entry.checksum:
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
    """Validate an RBI file against the v4.0 format specification (27 rules).

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
        "2. Format version major == 4",
        version_major == VERSION_MAJOR,
        f"major version {version_major} unsupported (need {VERSION_MAJOR})",
    )
    if not (magic_ok and version_ok):
        print(f"\n  {len(failed)} check(s) FAILED — cannot continue.")
        return False
    if version_minor > VERSION_MINOR:
        print(
            f"  [WARN] 3. Minor version {version_minor} > {VERSION_MINOR} — proceeding (minor increments are backwards-compatible)"
        )
    else:
        check("3. Format version minor == 0", version_minor == VERSION_MINOR)

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

    # Rule 20: checksums for all blocks
    print("  Verifying block checksums (may take a moment for PCM)...")
    for entry in directory:
        type_name = _BLOCK_NAMES.get(
            entry.type_id, entry.type_id.decode("ascii", errors="replace")
        )
        if entry.offset >= file_size or entry.offset + entry.length > file_size:
            check(
                f"20. {type_name} block checksum (SHA-256)",
                False,
                "block out of file bounds",
            )
            continue
        with open(rbi_file, "rb") as f:
            f.seek(entry.offset)
            computed = _stream_sha256(f, entry.length)
        check(f"20. {type_name} block checksum (SHA-256)", computed == entry.checksum)

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

        # Rule 27: RLOG SHA-256 self-seal
        lines = rlog_bytes.split(b"\n")
        if lines and _re.match(rb"SHA-256: [0-9a-f]{64}", lines[-1]):
            body = b"\n".join(lines[:-1]) + b"\n"
            expected_seal = lines[-1][len(b"SHA-256: ") :].decode()
            actual_seal = hashlib.sha256(body).hexdigest()
            check("27. RLOG SHA-256 self-seal", actual_seal == expected_seal)
        elif lines and not lines[-1]:
            # trailing newline: try second-to-last
            if len(lines) >= 2 and _re.match(rb"SHA-256: [0-9a-f]{64}", lines[-2]):
                body = b"\n".join(lines[:-2]) + b"\n"
                expected_seal = lines[-2][len(b"SHA-256: ") :].decode()
                actual_seal = hashlib.sha256(body).hexdigest()
                check("27. RLOG SHA-256 self-seal", actual_seal == expected_seal)
            else:
                print("  [SKIP] 27. RLOG SHA-256 self-seal (no seal line found)")
        else:
            print("  [SKIP] 27. RLOG SHA-256 self-seal (no seal line found)")

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

    total = len(passed) + len(failed)
    print()
    if failed:
        print(f"  {len(failed)}/{total} check(s) FAILED.")
    else:
        print(f"  All {total} checks passed.")
    return len(failed) == 0
