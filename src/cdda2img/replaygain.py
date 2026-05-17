"""
replaygain.py — EBU R128 / ReplayGain 2.0 loudness analysis.

Measures audio files via libebur128 (through the pyebur128 binding) and PyAV
for decoding. Returns per-track and album gain, true peak, and loudness range.

Public API
----------
    result = analyse(paths)               # measure N audio files → RGResult
    result = analyse_raw(disc, pcm_path)  # measure raw s16le PCM via mmap → RGResult
    embed_rg_tags(result, flac_paths)     # write RG tags into existing FLACs
    data   = pack_rg_block(result)        # serialise to RBI container RG block bytes
    result = unpack_rg_block(data, N)     # deserialise from RBI container RG block bytes
"""

from __future__ import annotations

import mmap
import struct
from dataclasses import dataclass, field
from pathlib import Path

import av
import numpy as np
import pyebur128

from cdda2img.rbi_format import (
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
    RG_BLOCK_FIXED_SIZE,
    RG_BLOCK_FIXED_STRUCT,
    RBIDisc,
    RBIReplayGain,
)

# ---------------------------------------------------------------------------
# libebur128 mode flags
# pyebur128 0.1.1 does not expose these as Python-visible constants, but
# R128State accepts raw int. Each compound value OR-in its mode dependencies:
#   MODE_S requires MODE_M; MODE_I requires MODE_M; MODE_LRA requires MODE_S;
#   MODE_TRUE_PEAK requires MODE_SAMPLE_PEAK and MODE_M.
# ---------------------------------------------------------------------------
_MODE_M = 1 << 0  # momentary (400 ms)
_MODE_S = (1 << 1) | _MODE_M  # short-term (3 s) + M
_MODE_I = (1 << 2) | _MODE_M  # integrated + M
_MODE_LRA = (1 << 3) | _MODE_S  # loudness range + S + M
_MODE_SAMPLE_PK = (1 << 4) | _MODE_M  # sample peak + M
_MODE_TRUE_PK = (1 << 5) | _MODE_SAMPLE_PK  # true peak (oversampled) + sample + M
_EBUR128_MODE = _MODE_I | _MODE_LRA | _MODE_TRUE_PK  # = 63

RG_REFERENCE: float = -18.0  # LUFS — ReplayGain 2.0 / ITU-R BS.1770-3
RG_VERSION: int = 1  # RG block format version stored in the container

_LRA_WARN_LU: float = 5.0  # album LRA below this indicates heavily compressed source
_INT16_PER_FRAME = 1176  # 2352 bytes per CD frame / 2 bytes per int16


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class TrackRG:
    """Loudness analysis for a single track."""

    gain: float  # dB adjustment to reach RG_REFERENCE
    peak: float  # true peak, linear scale
    lra: float  # loudness range, LU


@dataclass
class RGResult:
    """Per-track and album EBU R128 / ReplayGain 2.0 analysis result."""

    reference: float  # LUFS reference level (= RG_REFERENCE)
    album_gain: float  # dB
    album_peak: float  # linear true peak
    album_lra: float  # LU
    tracks: list[TrackRG] = field(default_factory=list)

    @property
    def warnings(self) -> list[str]:
        """Human-readable warnings for heavily compressed source material."""
        if self.album_lra < _LRA_WARN_LU:
            return [
                f"Album LRA {self.album_lra:.1f} LU is below {_LRA_WARN_LU} LU — "
                "source may be heavily compressed (loudness war mastering)"
            ]
        return []


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


def _state_results(
    state: pyebur128.R128State, channels: int
) -> tuple[float, float, float]:
    """Read (integrated_lufs, peak_linear, lra_lu) from a finalised R128State."""
    lufs = pyebur128.get_loudness_global(state)
    lra = pyebur128.get_loudness_range(state)
    peak = max(pyebur128.get_true_peak(state, ch) for ch in range(channels))
    return lufs, peak, lra


# ---------------------------------------------------------------------------
# Public API — analysis
# ---------------------------------------------------------------------------


def analyse(paths: list[Path]) -> RGResult:
    """Measure per-track and album EBU R128 loudness for a list of audio files.

    Single decode pass: each file is decoded once, feeding both a per-track
    R128State and a shared album R128State simultaneously. This halves the
    I/O and decode cost compared to the naïve two-pass approach.
    """
    if not paths:
        msg = "analyse: no input files provided"
        raise ValueError(msg)

    tracks: list[TrackRG] = []
    album_state: pyebur128.R128State | None = None

    for path in paths:
        with av.open(str(path)) as c:
            stream = c.streams.audio[0]
            rate, channels = stream.sample_rate, stream.channels
            if album_state is None:
                album_state = pyebur128.R128State(channels, rate, _EBUR128_MODE)
            track_state = pyebur128.R128State(channels, rate, _EBUR128_MODE)
            resampler = av.AudioResampler(
                format="fltp", layout=stream.layout.name, rate=rate
            )
            for packet in c.demux(stream):
                for frame in packet.decode():
                    for rf in resampler.resample(frame):  # type: ignore[arg-type]  # LINT-002: audio stream yields AudioFrame; stubs over-broad
                        chunk = rf.to_ndarray().T.flatten()
                        n = len(chunk) // channels
                        track_state.add_frames(chunk, n)
                        album_state.add_frames(chunk, n)
            for rf in resampler.resample(None):
                chunk = rf.to_ndarray().T.flatten()
                n = len(chunk) // channels
                track_state.add_frames(chunk, n)
                album_state.add_frames(chunk, n)

        integrated, peak, lra = _state_results(track_state, channels)
        tracks.append(TrackRG(gain=RG_REFERENCE - integrated, peak=peak, lra=lra))

    if album_state is None:
        msg = "analyse: no tracks were processed"
        raise RuntimeError(msg)
    al_int, al_peak, al_lra = _state_results(album_state, channels)  # type: ignore[possibly-undefined]
    return RGResult(
        reference=RG_REFERENCE,
        album_gain=RG_REFERENCE - al_int,
        album_peak=al_peak,
        album_lra=al_lra,
        tracks=tracks,
    )


def analyse_raw(disc: RBIDisc, pcm_path: Path) -> RGResult:
    """Measure EBU R128 from a raw s16le PCM file, slicing per-track via mmap.

    Bypasses WAV encoding and PyAV decode entirely: mmap + np.frombuffer gives
    a zero-copy view into the PCM file; the only allocation per track is the
    float32 conversion (int16 → float32 / 32768). Both per-track and album
    R128States are fed in a single linear scan of the file.
    """
    if not disc.tracks:
        msg = "analyse_raw: disc has no tracks"
        raise ValueError(msg)

    album_state = pyebur128.R128State(PCM_CHANNELS, PCM_SAMPLE_RATE, _EBUR128_MODE)
    track_results: list[TrackRG] = []
    with (
        open(pcm_path, "rb") as f,
        mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm,
    ):
        s16_view = np.frombuffer(mm, dtype="<i2")
        for track in disc.tracks:
            idx_start = (track.start_frame + track.pregap_frames) * _INT16_PER_FRAME
            idx_count = track.duration_frames * _INT16_PER_FRAME
            samples = (
                s16_view[idx_start : idx_start + idx_count].astype(np.float32) / 32768.0
            )
            n_frames = len(samples) // PCM_CHANNELS
            track_state = pyebur128.R128State(
                PCM_CHANNELS, PCM_SAMPLE_RATE, _EBUR128_MODE
            )
            track_state.add_frames(samples, n_frames)
            integrated, peak, lra = _state_results(track_state, PCM_CHANNELS)
            track_results.append(
                TrackRG(gain=RG_REFERENCE - integrated, peak=peak, lra=lra)
            )
            album_state.add_frames(samples, n_frames)
    al_int, al_peak, al_lra = _state_results(album_state, PCM_CHANNELS)
    return RGResult(
        reference=RG_REFERENCE,
        album_gain=RG_REFERENCE - al_int,
        album_peak=al_peak,
        album_lra=al_lra,
        tracks=track_results,
    )


# ---------------------------------------------------------------------------
# Public API — RG block serialisation (RBI container format §7)
# ---------------------------------------------------------------------------


def pack_rg_block(result: RGResult) -> bytes:
    """Serialise an RGResult to the binary RG block format (spec §7.2).

    Layout: fixed header (17 bytes) + track_gain[N] + track_peak[N] + track_range[N].
    """
    fixed = struct.pack(
        RG_BLOCK_FIXED_STRUCT,
        RG_VERSION,
        result.reference,
        result.album_gain,
        result.album_peak,
        result.album_lra,
    )
    n = len(result.tracks)
    gains = struct.pack(f"<{n}f", *(t.gain for t in result.tracks))
    peaks = struct.pack(f"<{n}f", *(t.peak for t in result.tracks))
    ranges = struct.pack(f"<{n}f", *(t.lra for t in result.tracks))
    return fixed + gains + peaks + ranges


def embed_rg_tags(result: RGResult, flac_paths: list[Path]) -> None:
    """Write ReplayGain 2.0 Vorbis comment tags into existing FLAC files.

    Uses mutagen to patch the Vorbis comment block in-place without re-encoding.
    """
    from mutagen.flac import FLAC

    for idx, path in enumerate(flac_paths):
        t = result.tracks[idx]
        rg_tags = {
            "REPLAYGAIN_TRACK_GAIN": [f"{t.gain:+.2f} dB"],
            "REPLAYGAIN_TRACK_PEAK": [f"{t.peak:.6f}"],
            "REPLAYGAIN_TRACK_RANGE": [f"{t.lra:.2f} LU"],
            "REPLAYGAIN_ALBUM_GAIN": [f"{result.album_gain:+.2f} dB"],
            "REPLAYGAIN_ALBUM_PEAK": [f"{result.album_peak:.6f}"],
            "REPLAYGAIN_ALBUM_RANGE": [f"{result.album_lra:.2f} LU"],
            "REPLAYGAIN_REFERENCE_LOUDNESS": [f"{result.reference:.2f} LUFS"],
        }
        flac = FLAC(str(path))
        for key, value in rg_tags.items():
            flac[key] = value
        flac.save()


def unpack_rg_block(data: bytes, track_count: int) -> RBIReplayGain:
    """Deserialise raw RG block bytes into an RBIReplayGain dataclass."""
    expected = RG_BLOCK_FIXED_SIZE + 12 * track_count
    if len(data) < expected:
        msg = f"RG block too short: {len(data)} bytes, expected {expected} for {track_count} tracks"
        raise ValueError(msg)

    rg_version, rg_reference, album_gain, album_peak, album_range = struct.unpack(
        RG_BLOCK_FIXED_STRUCT, data[:RG_BLOCK_FIXED_SIZE]
    )

    offset = RG_BLOCK_FIXED_SIZE
    track_gain = list(struct.unpack_from(f"<{track_count}f", data, offset))
    offset += 4 * track_count
    track_peak = list(struct.unpack_from(f"<{track_count}f", data, offset))
    offset += 4 * track_count
    track_range = list(struct.unpack_from(f"<{track_count}f", data, offset))

    return RBIReplayGain(
        rg_version=rg_version,
        rg_reference=rg_reference,
        album_gain=album_gain,
        album_peak=album_peak,
        album_range=album_range,
        track_gain=track_gain,
        track_peak=track_peak,
        track_range=track_range,
    )
