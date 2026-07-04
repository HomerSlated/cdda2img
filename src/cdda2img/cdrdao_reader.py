"""
cdrdao_reader.py — Import a cdrdao TOC+BIN image as an RBI-ready disc object.

Always operates in master mode: pre-gaps are preserved as part of the following
track's audio slot in the RBI PCM block.  The BIN data is byte-swapped from
s16be (raw sector byte order) to RBI-native s16le before writing.
"""

import array
import re
import wave
from pathlib import Path

from cdda2img.rbi_format import FLAG_MASTER_MODE, RBIDisc, RBITocEntry
from cdda2img.toc_parser import ParsedDisc, parse_toc

_FILE_NAME_RE = re.compile(r'FILE\s+"([^"]+)"')

_BYTES_PER_FRAME = 588 * 4  # 588 stereo samples x 2 ch x 2 bytes = 2352 bytes/frame


def _byteswap_s16(data: bytes) -> bytes:
    """Swap every 16-bit word in *data* (s16be ↔ s16le)."""
    a = array.array("h", data)
    a.byteswap()
    return a.tobytes()


def _find_bin_filename(toc_text: str) -> str:
    """Extract the BIN filename from the first FILE entry in a cdrdao TOC."""
    m = _FILE_NAME_RE.search(toc_text)
    if not m:
        msg = "No FILE entry found in cdrdao TOC"
        raise ValueError(msg)
    return m.group(1)


def convert_cdrdao_bin(
    bin_path: Path, pcm_out: Path, chunk_frames: int = 75 * 60
) -> None:
    """Byte-swap a cdrdao s16be BIN file to s16le PCM and write to *pcm_out*.

    Processes the file in chunks of *chunk_frames* CD frames (default: 4500 = 60 s)
    to keep peak memory usage bounded regardless of disc size.
    """
    chunk_bytes = chunk_frames * _BYTES_PER_FRAME
    with open(bin_path, "rb") as f_in, open(pcm_out, "wb") as f_out:
        while True:
            chunk = f_in.read(chunk_bytes)
            if not chunk:
                break
            if len(chunk) % 2:
                msg = f"Odd byte count in BIN chunk ({len(chunk)} bytes); file may be corrupt"
                raise ValueError(msg)
            f_out.write(_byteswap_s16(chunk))


def convert_cdrdao_bin_to_wav(
    bin_path: Path,
    wav_out: Path,
    sample_rate: int = 44100,
    channels: int = 2,
    bit_depth: int = 16,
    chunk_frames: int = 75 * 60,
) -> None:
    """Byte-swap a cdrdao s16be BIN and write a WAV-wrapped s16le file to *wav_out*.

    Produces a file that av.open / wave.open can read, making it suitable for
    passing to replaygain.analyse() before stripping the header with wav_to_raw_pcm().
    """
    chunk_bytes = chunk_frames * _BYTES_PER_FRAME
    with wave.open(str(wav_out), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(bit_depth // 8)
        w.setframerate(sample_rate)
        with open(bin_path, "rb") as f_in:
            while True:
                chunk = f_in.read(chunk_bytes)
                if not chunk:
                    break
                if len(chunk) % 2:
                    msg = f"Odd byte count in BIN chunk ({len(chunk)} bytes); file may be corrupt"
                    raise ValueError(msg)
                w.writeframes(_byteswap_s16(chunk))


def parsed_to_rbi_disc(parsed: ParsedDisc) -> RBIDisc:
    """Convert a ParsedDisc (from toc_parser) to an RBIDisc with full metadata."""
    disc = RBIDisc(
        album=parsed.title,
        artist=parsed.performer,
        catalog=parsed.catalog,
        cdtext_catalog_ref=parsed.disc_id,
        pre_emphasis=parsed.pre_emphasis,
    )
    for pt in parsed.tracks:
        disc.tracks.append(
            RBITocEntry(
                track_number=pt.track_number,
                title=pt.title,
                performer=pt.performer,
                start_frame=pt.start_frame,
                duration_frames=pt.duration_frames,
                pregap_frames=pt.pregap_frames,
                isrc=pt.isrc,
                pre_emphasis=pt.pre_emphasis,
                copy_permitted=pt.copy_permitted,
                index_points=list(pt.index_points),
            )
        )
    return disc


def import_cdrdao(toc_path: Path, pcm_out: Path) -> tuple[RBIDisc, int]:
    """Parse a cdrdao TOC+BIN image and write byte-swapped s16le PCM to *pcm_out*.

    Returns ``(disc, FLAG_MASTER_MODE)`` — callers pass the flag to
    ``build_container`` via *extra_flags*.  The BIN file is resolved relative
    to *toc_path*'s parent directory.
    """
    toc_text = toc_path.read_text(encoding="utf-8")
    toc_bytes = toc_text.encode("utf-8")

    bin_name = _find_bin_filename(toc_text)
    bin_path = toc_path.parent / bin_name
    if not bin_path.exists():
        msg = f"BIN file not found: {bin_path}"
        raise FileNotFoundError(msg)

    parsed = parse_toc(toc_bytes)
    disc = parsed_to_rbi_disc(parsed)

    convert_cdrdao_bin(bin_path, pcm_out)
    return disc, FLAG_MASTER_MODE
