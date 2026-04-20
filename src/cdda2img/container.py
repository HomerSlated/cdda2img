"""
container.py — RBI container writer, reader, and extractor.
"""

import datetime
import hashlib
import os
import shutil
import struct
import tempfile
import wave
from pathlib import Path

from cdda2img.rbi_format import (
    HEADER_FIXED_SIZE,
    HEADER_STRUCT,
    MAGIC,
    MAX_METADATA_LEN,
    PCM_BIT_DEPTH,
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
    VERSION_MAJOR,
    VERSION_MINOR,
    RBIDisc,
    RBIHeader,
)

_TOOL_VERSION = "0.1.4"


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
        if p.is_dir() and os.access(p, os.R_OK | os.W_OK) and shutil.disk_usage(p).free >= min_required_bytes:
            return p
    msg = "No suitable temporary directory with enough free space."
    raise RuntimeError(msg)


class TempFiles:
    def __init__(self, base_dir: Path):
        self.base = base_dir
        self.pcm_file = base_dir / "all_tracks.pcm"  # final raw PCM (stored in RBI)
        self.pcm_pre = base_dir / "all_tracks_pre.wav"  # concatenated WAV, pre-normalisation
        self.pcm_norm = base_dir / "all_tracks_norm.wav"  # normalised WAV (if normalisation enabled)
        self._temp_tracks: list[Path] = []

    def temp_track(self, i: int, suffix: str) -> Path:
        path = self.base / f"temp_track_{i}{suffix}"
        self._temp_tracks.append(path)
        return path

    def cleanup(self) -> None:
        for path in [self.pcm_file, self.pcm_pre, self.pcm_norm, *self._temp_tracks]:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Container writer
# ---------------------------------------------------------------------------


def build_container(pcm_path: Path, toc_data: bytes, disc: RBIDisc, output_file: Path) -> None:
    """Assemble and write an RBI v1.2 container from raw PCM and TOC data."""
    toc_checksum = sha256_bytes(toc_data)
    pcm_checksum = sha256_file(pcm_path)

    created_str = (
        f"Created by cdda2img v{_TOOL_VERSION} "
        f"(format {VERSION_MAJOR}.{VERSION_MINOR}) "
        f"on {datetime.datetime.now().isoformat()}"
    )
    metadata_bytes = created_str.encode("utf-8")
    metadata_len = len(metadata_bytes)
    if metadata_len > MAX_METADATA_LEN:
        msg = f"Metadata string too long: {metadata_len} bytes (max {MAX_METADATA_LEN})"
        raise ValueError(msg)

    toc_start = HEADER_FIXED_SIZE + metadata_len
    toc_end = toc_start + len(toc_data)
    pcm_start = toc_end
    pcm_end = pcm_start + pcm_path.stat().st_size

    header = struct.pack(
        HEADER_STRUCT,
        MAGIC,
        VERSION_MAJOR,
        VERSION_MINOR,
        0,  # flags: all reserved, must be 0
        disc.track_count,
        disc.disc_number,
        disc.disc_total,
        PCM_SAMPLE_RATE,
        PCM_CHANNELS,
        PCM_BIT_DEPTH,
        toc_start,
        toc_end,
        pcm_start,
        pcm_end,
        toc_checksum,
        pcm_checksum,
        metadata_len,
    )
    assert len(header) == HEADER_FIXED_SIZE  # noqa: S101

    with open(output_file, "wb") as out:
        out.write(header)
        out.write(metadata_bytes)
        out.write(toc_data)
        with open(pcm_path, "rb") as pcm:
            shutil.copyfileobj(pcm, out)

    print(f"Container created: {output_file}")


# ---------------------------------------------------------------------------
# Container reader
# ---------------------------------------------------------------------------


def read_header(file: Path) -> RBIHeader:
    """Read and validate the fixed header of an RBI file."""
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
            toc_start,
            toc_end,
            pcm_start,
            pcm_end,
            toc_checksum,
            pcm_checksum,
            metadata_len,
        ) = struct.unpack(HEADER_STRUCT, fixed)

        if magic != MAGIC:
            msg = f"Invalid magic bytes: {magic!r}"
            raise ValueError(msg)
        if version_major != VERSION_MAJOR:
            msg = f"Unsupported format major version: {version_major}"
            raise ValueError(msg)
        if metadata_len > MAX_METADATA_LEN:
            msg = f"Unrealistic metadata length: {metadata_len}"
            raise ValueError(msg)

        metadata_raw = f.read(metadata_len)

    try:
        metadata = metadata_raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = "Invalid UTF-8 in metadata field"
        raise ValueError(msg) from exc

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
        toc_start=toc_start,
        toc_end=toc_end,
        pcm_start=pcm_start,
        pcm_end=pcm_end,
        toc_checksum=toc_checksum,
        pcm_checksum=pcm_checksum,
        metadata=metadata,
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


def extract_data(
    container_file: Path,
    raw_dir: Path | None,
    tracks: bool,
    base_dir: Path,
) -> None:
    """Extract TOC and/or per-track FLACs from an RBI container."""
    from cdda2img.toc_parser import parse_toc
    from cdda2img.track_extract import extract_tracks, write_cue

    header = read_header(container_file)
    stem = container_file.stem

    with open(container_file, "rb") as f:
        f.seek(header.toc_start)
        toc_data = f.read(header.toc_length)
        f.seek(header.pcm_start)
        pcm_checksum = _stream_sha256(f, header.pcm_length)

    if sha256_bytes(toc_data) != header.toc_checksum:
        print("Warning: TOC checksum mismatch — file may be corrupt")
    if pcm_checksum != header.pcm_checksum:
        print("Warning: PCM checksum mismatch — file may be corrupt")

    disc = parse_toc(toc_data)

    if raw_dir is not None:
        raw_dir.mkdir(parents=True, exist_ok=True)

        toc_path = raw_dir / f"{stem}.toc"
        toc_path.write_bytes(toc_data)
        print(f"TOC saved: {toc_path}")

        pcm_path = raw_dir / f"{stem}.s16le"
        with open(container_file, "rb") as f_in, open(pcm_path, "wb") as f_out:
            f_in.seek(header.pcm_start)
            _copy_bytes(f_in, f_out, header.pcm_length)
        print(f"PCM saved: {pcm_path}")
        print(f"Metadata: {header.metadata}")

    if tracks:
        print(f"\nExtracting {header.track_count} tracks...")
        extract_tracks(
            disc=disc,
            container_file=container_file,
            pcm_start=header.pcm_start,
            disc_number=header.disc_number,
            disc_total=header.disc_total,
            sample_rate=header.pcm_sample_rate,
            channels=header.pcm_channels,
            bit_depth=header.pcm_bit_depth,
            comment=header.metadata,
            base=base_dir,
        )
        write_cue(disc, header.disc_number, header.disc_total, base_dir)


def wav_to_raw_pcm(wav_path: Path, pcm_path: Path) -> None:
    """Strip the WAV header, writing only raw PCM frames to pcm_path."""
    with wave.open(str(wav_path), "rb") as w:
        pcm_path.write_bytes(w.readframes(w.getnframes()))


def _write_wav(path: Path, pcm_data: bytes, sample_rate: int, channels: int, bit_depth: int) -> None:
    """Reconstruct a WAV file from raw PCM bytes using the given audio parameters."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(bit_depth // 8)
        w.setframerate(sample_rate)
        w.writeframes(pcm_data)
