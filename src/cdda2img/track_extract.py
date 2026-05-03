"""
track_extract.py — Per-track FLAC extraction and CUE sheet generation.
"""

import io
import re
import wave
from pathlib import Path

import av
from av.audio.frame import AudioFrame

from cdda2img.rbi_format import CD_FRAMES_PER_SECOND, RBIReplayGain
from cdda2img.toc_parser import ParsedDisc, ParsedTrack

_UNSAFE_RE = re.compile(r'[/\\:*?"<>|]')


def _fs_safe(name: str) -> str:
    """Replace filesystem-unsafe characters and strip leading/trailing dots and spaces."""
    return _UNSAFE_RE.sub("_", name).strip(". ")


def _disc_dir(disc: ParsedDisc, disc_number: int, disc_total: int, base: Path) -> Path:
    artist = _fs_safe(disc.performer) or "Unknown Artist"
    album = _fs_safe(disc.title) or "Unknown Album"
    d = base / artist / album
    if disc_total > 1:
        d = d / f"disc{disc_number:02d}"
    return d


def _track_filename(track: ParsedTrack) -> str:
    return f"{track.track_number:02d} - {_fs_safe(track.title)}.flac"


def _cue_filename(disc: ParsedDisc) -> str:
    return f"{_fs_safe(disc.title) or 'disc'}.cue"


def _rg_tags(rg: RBIReplayGain, track_index: int) -> dict[str, str]:
    """Format ReplayGain 2.0 Vorbis comment tags for one track (0-based index)."""
    return {
        "REPLAYGAIN_TRACK_GAIN": f"{rg.track_gain[track_index]:+.2f} dB",
        "REPLAYGAIN_TRACK_PEAK": f"{rg.track_peak[track_index]:.6f}",
        "REPLAYGAIN_TRACK_RANGE": f"{rg.track_range[track_index]:.2f} LU",
        "REPLAYGAIN_ALBUM_GAIN": f"{rg.album_gain:+.2f} dB",
        "REPLAYGAIN_ALBUM_PEAK": f"{rg.album_peak:.6f}",
        "REPLAYGAIN_ALBUM_RANGE": f"{rg.album_range:.2f} LU",
        "REPLAYGAIN_REFERENCE_LOUDNESS": f"{rg.rg_reference:.2f} LUFS",
    }


def collect_track_flac_paths(
    disc: ParsedDisc, disc_number: int, disc_total: int, base: Path
) -> list[Path]:
    """Return only the per-track FLAC paths (no CUE sheet)."""
    d = _disc_dir(disc, disc_number, disc_total, base)
    return [d / _track_filename(t) for t in disc.tracks]


def collect_tracks_output_paths(
    disc: ParsedDisc, disc_number: int, disc_total: int, base: Path
) -> list[Path]:
    """Return all paths that would be written by extract_tracks + write_cue."""
    d = _disc_dir(disc, disc_number, disc_total, base)
    paths: list[Path] = [d / _track_filename(t) for t in disc.tracks]
    paths.append(d / _cue_filename(disc))
    return paths


def _read_pcm_slice(
    container_file: Path,
    pcm_file_start: int,
    track_start_frame: int,
    track_duration_frames: int,
    sample_rate: int,
    channels: int,
    bit_depth: int,
) -> bytes:
    bytes_per_frame = (
        (sample_rate // CD_FRAMES_PER_SECOND) * channels * (bit_depth // 8)
    )
    byte_offset = pcm_file_start + track_start_frame * bytes_per_frame
    byte_count = track_duration_frames * bytes_per_frame
    with open(container_file, "rb") as f:
        f.seek(byte_offset)
        return f.read(byte_count)


def _pcm_to_wav_bytes(
    pcm_bytes: bytes, sample_rate: int, channels: int, bit_depth: int
) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(bit_depth // 8)
        w.setframerate(sample_rate)
        w.writeframes(pcm_bytes)
    return buf.getvalue()


def _wav_bytes_to_flac(
    wav_bytes: bytes,
    output_path: Path,
    metadata: dict[str, str],
    sample_rate: int,
) -> None:
    buf = io.BytesIO(wav_bytes)
    with av.open(buf, format="wav", mode="r") as in_c:
        in_stream = in_c.streams.audio[0]
        with av.open(str(output_path), "w", format="flac") as out_c:
            out_c.metadata.update(metadata)
            out_stream = out_c.add_stream("flac", rate=sample_rate)
            for packet in in_c.demux(in_stream):
                for frame in packet.decode():
                    if not isinstance(frame, AudioFrame):
                        continue
                    for out_packet in out_stream.encode(frame):
                        out_c.mux(out_packet)
            for out_packet in out_stream.encode(None):
                out_c.mux(out_packet)


def extract_tracks(
    disc: ParsedDisc,
    container_file: Path,
    pcm_start: int,
    disc_number: int,
    disc_total: int,
    sample_rate: int,
    channels: int,
    bit_depth: int,
    comment: str,
    base: Path,
    rg_data: RBIReplayGain | None = None,
) -> None:
    d = _disc_dir(disc, disc_number, disc_total, base)
    d.mkdir(parents=True, exist_ok=True)
    track_total = len(disc.tracks)

    for idx, track in enumerate(disc.tracks):
        print(f"  Track {track.track_number:2}/{track_total}: {track.title}")
        pcm = _read_pcm_slice(
            container_file,
            pcm_start,
            track.audio_start_frame,  # skip pregap; audio starts after it
            track.duration_frames,
            sample_rate,
            channels,
            bit_depth,
        )
        wav = _pcm_to_wav_bytes(pcm, sample_rate, channels, bit_depth)

        metadata: dict[str, str] = {
            "TITLE": track.title,
            "ARTIST": track.performer,
            "album_artist": disc.performer,
            "ALBUM": disc.title,
            "track": f"{track.track_number}/{track_total}",
            "disc": f"{disc_number}/{disc_total}",
            "comment": comment,
        }
        if rg_data is not None:
            metadata.update(_rg_tags(rg_data, idx))

        out_path = d / _track_filename(track)
        _wav_bytes_to_flac(wav, out_path, metadata, sample_rate)
        print(f"    → {out_path}")


def write_cue(disc: ParsedDisc, disc_number: int, disc_total: int, base: Path) -> Path:
    d = _disc_dir(disc, disc_number, disc_total, base)
    d.mkdir(parents=True, exist_ok=True)

    lines = [
        f'PERFORMER "{disc.performer}"',
        f'TITLE "{disc.title}"',
    ]
    for track in disc.tracks:
        lines += [
            f'FILE "{_track_filename(track)}" WAVE',
            f"  TRACK {track.track_number:02d} AUDIO",
            f'    TITLE "{track.title}"',
            f'    PERFORMER "{track.performer}"',
            "    INDEX 01 00:00:00",
        ]

    cue_path = d / _cue_filename(disc)
    cue_path.write_bytes("\n".join(lines).encode("utf-8"))
    print(f"CUE saved: {cue_path}")
    return cue_path
