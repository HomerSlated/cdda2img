"""
container.py — RBI container writer, reader, and extractor.
"""

import datetime
import hashlib
import json
import os
import shutil
import struct
import tempfile
import wave
from pathlib import Path

from cdda2img.rbi_format import (
    CD_FRAMES_PER_SECOND,
    CHECKSUM_PLACEHOLDER,
    FLAG_RG_PRESENT,
    FLAGS_RESERVED_MASK,
    HEADER_FIXED_SIZE,
    HEADER_STRUCT,
    MAGIC,
    MAX_METADATA_LEN,
    MAX_TRACKS,
    OFFSET_PLACEHOLDER,
    PCM_BIT_DEPTH,
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
    VERSION_MAJOR,
    VERSION_MINOR,
    RBIDisc,
    RBIHeader,
    RBIReplayGain,
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


def build_container(
    pcm_path: Path,
    toc_data: bytes,
    disc: RBIDisc,
    output_file: Path,
    rg_block: bytes | None = None,
    extra_flags: int = 0,
) -> None:
    """Assemble and write an RBI v2.0 container from raw PCM and TOC data.

    *extra_flags* is OR-ed into the computed flags word alongside FLAG_RG_PRESENT.
    Use it to record caller-level provenance bits such as FLAG_MASTER_MODE.
    """
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

    if rg_block is not None:
        flags = extra_flags | FLAG_RG_PRESENT
        rg_start = toc_end
        rg_end = toc_end + len(rg_block)
        rg_checksum_val = sha256_bytes(rg_block)
        pcm_start = rg_end
    else:
        flags = extra_flags
        rg_start = OFFSET_PLACEHOLDER
        rg_end = OFFSET_PLACEHOLDER
        rg_checksum_val = CHECKSUM_PLACEHOLDER
        pcm_start = toc_end

    pcm_end = pcm_start + pcm_path.stat().st_size

    header = struct.pack(
        HEADER_STRUCT,
        MAGIC,
        VERSION_MAJOR,
        VERSION_MINOR,
        flags,
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
        rg_start,
        rg_end,
        rg_checksum_val,
    )
    assert len(header) == HEADER_FIXED_SIZE  # noqa: S101  # LINT-006

    with open(output_file, "wb") as out:
        out.write(header)
        out.write(metadata_bytes)
        out.write(toc_data)
        if rg_block is not None:
            out.write(rg_block)
        with open(pcm_path, "rb") as pcm:
            shutil.copyfileobj(pcm, out)

    print(f"Container created: {output_file}")


# ---------------------------------------------------------------------------
# Container reader
# ---------------------------------------------------------------------------


def read_header(file: Path) -> RBIHeader:
    """Read and validate the fixed header of an RBI v2.0 file."""
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
            rg_start,
            rg_end,
            rg_checksum,
        ) = struct.unpack(HEADER_STRUCT, fixed)

        if magic != MAGIC:
            msg = f"Invalid magic bytes: {magic!r}"
            raise ValueError(msg)
        if version_major != VERSION_MAJOR:
            msg = f"Unsupported format version: {version_major}.{version_minor} (this reader requires major version {VERSION_MAJOR})"
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
        rg_start=rg_start,
        rg_end=rg_end,
        rg_checksum=rg_checksum,
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


def _warn_checksum(label: str, computed: bytes, expected: bytes) -> None:
    if computed != expected:
        print(f"Warning: {label} checksum mismatch — file may be corrupt")


def _write_rg_json(path: Path, rg_data: RBIReplayGain) -> None:
    rg_json = {
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
    path.write_text(json.dumps(rg_json, indent=2))
    print(f"RG data saved: {path}")


def extract_data(
    container_file: Path,
    raw_dir: Path | None,
    tracks: bool,
    base_dir: Path,
    embed_rg: bool = True,
) -> None:
    """Extract TOC and/or per-track FLACs from an RBI container."""
    from cdda2img.replaygain import analyse, embed_rg_tags, unpack_rg_block
    from cdda2img.toc_parser import parse_toc
    from cdda2img.track_extract import collect_track_flac_paths, extract_tracks, write_cue

    header = read_header(container_file)
    stem = container_file.stem

    with open(container_file, "rb") as f:
        f.seek(header.toc_start)
        toc_data = f.read(header.toc_length)
        f.seek(header.pcm_start)
        pcm_checksum = _stream_sha256(f, header.pcm_length)

    _warn_checksum("TOC", sha256_bytes(toc_data), header.toc_checksum)
    _warn_checksum("PCM", pcm_checksum, header.pcm_checksum)

    rg_data = None
    if header.has_rg:
        with open(container_file, "rb") as f:
            f.seek(header.rg_start)
            rg_raw = f.read(header.rg_length)
        if sha256_bytes(rg_raw) == header.rg_checksum:
            rg_data = unpack_rg_block(rg_raw, header.track_count)
        else:
            print("Warning: RG block checksum mismatch — ReplayGain data may be corrupt")

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

        if rg_data is not None:
            _write_rg_json(raw_dir / f"{stem}.rg.json", rg_data)

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
            rg_data=rg_data if embed_rg else None,
        )
        write_cue(disc, header.disc_number, header.disc_total, base_dir)
        if embed_rg:
            if rg_data is not None:
                print("ReplayGain tags embedded.")
            else:
                print("\nNo RG block in container — measuring loudness from extracted tracks...")
                flac_paths = collect_track_flac_paths(disc, header.disc_number, header.disc_total, base_dir)
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


def list_container(rbi_file: Path) -> None:
    """Print a human-readable listing of an RBI file's sections and tracks."""
    from cdda2img.toc_parser import parse_toc

    header = read_header(rbi_file)
    file_size = rbi_file.stat().st_size

    mode_flags: list[str] = ["master" if header.is_master else "remaster"]
    if header.has_rg:
        mode_flags.append("ReplayGain")
    flags_str = ", ".join(mode_flags)

    print(f"RBI Image: {rbi_file.name}  ({_fmt_size(file_size)})")
    print(
        f"Format:    v{header.version_major}.{header.version_minor}  |  disc {header.disc_number}/{header.disc_total}  |  {header.track_count} tracks  |  {flags_str}"
    )
    print(f"Created:   {header.metadata}")
    print()

    col_w = len("ReplayGain block") + 2
    hdr_line = f"{'Section':<{col_w}}  {'Offset':>14}  {'Size':>14}"
    print(hdr_line)
    print("-" * len(hdr_line))

    meta_size = header.toc_start - HEADER_FIXED_SIZE
    sections: list[tuple[str, int, int, str | None]] = [
        ("Fixed header", 0, HEADER_FIXED_SIZE, None),
        ("Metadata", HEADER_FIXED_SIZE, meta_size, None),
        ("TOC", header.toc_start, header.toc_length, None),
    ]
    if header.has_rg:
        sections.append(("ReplayGain block", header.rg_start, header.rg_length, None))

    pcm_seconds = header.pcm_length / (header.pcm_sample_rate * header.pcm_channels * (header.pcm_bit_depth // 8))
    sections.append(("PCM audio", header.pcm_start, header.pcm_length, _fmt_duration(pcm_seconds)))

    for name, offset, size, extra in sections:
        extra_str = f"  ({extra})" if extra else ""
        print(f"{name:<{col_w}}  {offset:>14,}  {_fmt_size(size):>14}{extra_str}")

    print()

    with open(rbi_file, "rb") as f:
        f.seek(header.toc_start)
        toc_bytes = f.read(header.toc_length)

    disc = parse_toc(toc_bytes)
    print(f"Tracks:  {disc.performer} — {disc.title}")
    print()
    for track in disc.tracks:
        dur = _fmt_duration(track.duration_frames / CD_FRAMES_PER_SECOND)
        print(f"  {track.track_number:2d}  {track.title:<52}  {dur:>5}")


# ---------------------------------------------------------------------------
# Container verifier (t command)
# ---------------------------------------------------------------------------


def verify_container(rbi_file: Path) -> bool:
    """Validate an RBI file against the format specification.

    Prints a pass/fail line for each check. Returns True if all pass.
    """
    import struct as _struct

    from cdda2img.toc_parser import parse_toc

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
        toc_start,
        toc_end,
        pcm_start,
        pcm_end,
        toc_checksum_stored,
        pcm_checksum_stored,
        metadata_len,
        rg_start,
        rg_end,
        rg_checksum_stored,
    ) = _struct.unpack(HEADER_STRUCT, raw)

    check("Magic bytes", magic == MAGIC, f"got {magic!r}")
    check(
        f"Format version v{version_major}.{version_minor}",
        version_major == VERSION_MAJOR,
        f"major version {version_major} unsupported (need {VERSION_MAJOR})",
    )
    check(
        "Reserved flag bits are zero",
        (flags & FLAGS_RESERVED_MASK) == 0,
        f"flags=0x{flags:08X}, reserved bits 0x{flags & FLAGS_RESERVED_MASK:08X} set",
    )
    check(f"Track count {track_count} in range 1-{MAX_TRACKS}", 1 <= track_count <= MAX_TRACKS)
    check(f"Disc {disc_number}/{disc_total} consistent", 1 <= disc_number <= disc_total)
    check(f"PCM sample rate {pcm_sample_rate} Hz", pcm_sample_rate == PCM_SAMPLE_RATE, f"expected {PCM_SAMPLE_RATE}")
    check(f"PCM channels {pcm_channels}", pcm_channels == PCM_CHANNELS, f"expected {PCM_CHANNELS}")
    check(f"PCM bit depth {pcm_bit_depth}-bit", pcm_bit_depth == PCM_BIT_DEPTH, f"expected {PCM_BIT_DEPTH}")
    check(f"Metadata length {metadata_len} B in range", metadata_len <= MAX_METADATA_LEN, f"max {MAX_METADATA_LEN}")

    expected_toc_start = HEADER_FIXED_SIZE + metadata_len
    check(
        "TOC starts immediately after header+metadata",
        toc_start == expected_toc_start,
        f"toc_start={toc_start}, expected {expected_toc_start}",
    )
    check("TOC end > TOC start", toc_end > toc_start, f"toc_start={toc_start}, toc_end={toc_end}")

    has_rg = bool(flags & FLAG_RG_PRESENT)
    if has_rg:
        check("RG block starts at TOC end", rg_start == toc_end, f"rg_start={rg_start}, toc_end={toc_end}")
        check("RG block end > RG start", rg_end > rg_start, f"rg_start={rg_start}, rg_end={rg_end}")
        check("PCM starts at RG block end", pcm_start == rg_end, f"pcm_start={pcm_start}, rg_end={rg_end}")
    else:
        check("PCM starts at TOC end", pcm_start == toc_end, f"pcm_start={pcm_start}, toc_end={toc_end}")
    check("PCM end > PCM start", pcm_end > pcm_start, f"pcm_start={pcm_start}, pcm_end={pcm_end}")
    check("File size matches pcm_end", pcm_end == file_size, f"pcm_end={pcm_end}, actual={file_size}")

    with open(rbi_file, "rb") as f:
        f.seek(HEADER_FIXED_SIZE)
        metadata_raw = f.read(metadata_len)
    try:
        metadata_raw.decode("utf-8")
        check("Metadata is valid UTF-8", True)
    except UnicodeDecodeError as exc:
        check("Metadata is valid UTF-8", False, str(exc))

    with open(rbi_file, "rb") as f:
        f.seek(toc_start)
        toc_bytes = f.read(toc_end - toc_start)
    check("TOC checksum (SHA-256)", sha256_bytes(toc_bytes) == toc_checksum_stored)

    if has_rg:
        with open(rbi_file, "rb") as f:
            f.seek(rg_start)
            rg_bytes = f.read(rg_end - rg_start)
        check("ReplayGain block checksum (SHA-256)", sha256_bytes(rg_bytes) == rg_checksum_stored)

    print("  Verifying PCM checksum (may take a moment)...")
    with open(rbi_file, "rb") as f:
        f.seek(pcm_start)
        computed_pcm = _stream_sha256(f, pcm_end - pcm_start)
    check("PCM checksum (SHA-256)", computed_pcm == pcm_checksum_stored)

    try:
        disc = parse_toc(toc_bytes)
        check("TOC parses without error", True)
        check(
            f"Parsed track count matches header ({track_count})",
            len(disc.tracks) == track_count,
            f"parsed {len(disc.tracks)} tracks",
        )
    except Exception as exc:
        check("TOC parses without error", False, str(exc))

    total = len(passed) + len(failed)
    print()
    if failed:
        print(f"  {len(failed)}/{total} check(s) FAILED.")
    else:
        print(f"  All {total} checks passed.")
    return len(failed) == 0
