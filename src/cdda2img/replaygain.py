"""
replaygain.py — EBU R128 / ReplayGain 2.0 loudness analysis.

Measures audio files via libebur128 (through the pyebur128 binding) and PyAV
for decoding. Returns per-track and album gain, true peak, and loudness range.
Inputs are expected to be post-transcode 44.1 kHz stereo WAVs; passing files
with mismatched sample rates or channel layouts to the album measurement will
produce incorrect results.

Public API
----------
    result = analyse(paths)               # measure N tracks → RGResult
    embed_rg_tags(result, flac_paths)     # write RG tags into existing FLACs
    data   = pack_rg_block(result)        # serialise to RBI container RG block bytes
    result = unpack_rg_block(data, N)     # deserialise from RBI container RG block bytes
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

import av
import numpy as np
import pyebur128

from cdda2img.rbi_format import (
    RG_BLOCK_FIXED_SIZE,
    RG_BLOCK_FIXED_STRUCT,
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
# libebur128 measurement via pyebur128 + PyAV
# ---------------------------------------------------------------------------


def _decode_interleaved(path: Path) -> tuple[np.ndarray, int, int]:
    """Decode an audio file to float32 interleaved samples in [-1, 1].

    Returns (samples, sample_rate, channels). AudioResampler with format='fltp'
    ensures consistent float32 planar output regardless of the source codec's
    native sample format (s16, s32, fltp, etc.), while preserving the original
    sample rate and channel layout.
    """
    with av.open(str(path)) as c:
        stream = c.streams.audio[0]
        rate, channels = stream.sample_rate, stream.channels
        resampler = av.AudioResampler(
            format="fltp", layout=stream.layout.name, rate=rate
        )
        chunks: list[np.ndarray] = []
        for packet in c.demux(stream):
            for frame in packet.decode():
                for rf in resampler.resample(frame):  # type: ignore[arg-type]  # LINT-002: audio stream yields AudioFrame; stubs over-broad
                    chunks.append(
                        rf.to_ndarray().T.flatten()
                    )  # (ch, samples) → interleaved
        for rf in resampler.resample(None):  # flush resampler
            chunks.append(rf.to_ndarray().T.flatten())
    return np.concatenate(chunks), rate, channels


def _state_results(
    state: pyebur128.R128State, channels: int
) -> tuple[float, float, float]:
    """Read (integrated_lufs, peak_linear, lra_lu) from a finalised R128State."""
    lufs = pyebur128.get_loudness_global(state)
    lra = pyebur128.get_loudness_range(state)
    peak = max(pyebur128.get_true_peak(state, ch) for ch in range(channels))
    return lufs, peak, lra


def _measure_single(path: Path) -> tuple[float, float, float]:
    """Measure EBU R128 on one file. Returns (integrated_lufs, peak_linear, lra_lu)."""
    samples, rate, channels = _decode_interleaved(path)
    state = pyebur128.R128State(channels, rate, _EBUR128_MODE)
    state.add_frames(samples, len(samples) // channels)
    return _state_results(state, channels)


def _measure_concat(paths: list[Path]) -> tuple[float, float, float]:
    """Measure EBU R128 over the virtual concatenation of all files.

    Returns (integrated_lufs, peak_linear, lra_lu) for the combined programme.
    libebur128 accumulates loudness history across sequential add_frames() calls
    on a single R128State — equivalent to the ffmpeg filter_complex concat approach
    but without subprocess overhead.
    """
    if not paths:
        msg = "_measure_concat() requires at least one path"
        raise ValueError(msg)
    if len(paths) == 1:
        return _measure_single(paths[0])

    samples, rate, channels = _decode_interleaved(paths[0])
    state = pyebur128.R128State(channels, rate, _EBUR128_MODE)
    state.add_frames(samples, len(samples) // channels)
    for path in paths[1:]:
        samples, _, _ = _decode_interleaved(path)
        state.add_frames(samples, len(samples) // channels)
    return _state_results(state, channels)


# ---------------------------------------------------------------------------
# Public API — analysis
# ---------------------------------------------------------------------------


def analyse(paths: list[Path]) -> RGResult:
    """Measure per-track and album EBU R128 loudness for a list of audio files.

    Each track is measured independently for per-track gain/peak/LRA. Album
    values are measured over the virtual concatenation of all tracks.
    """
    if not paths:
        msg = "analyse: no input files provided"
        raise ValueError(msg)

    tracks: list[TrackRG] = []
    for path in paths:
        integrated, peak, lra = _measure_single(path)
        tracks.append(
            TrackRG(
                gain=RG_REFERENCE - integrated,
                peak=peak,
                lra=lra,
            )
        )

    album_integrated, album_peak, album_lra = _measure_concat(paths)

    return RGResult(
        reference=RG_REFERENCE,
        album_gain=RG_REFERENCE - album_integrated,
        album_peak=album_peak,
        album_lra=album_lra,
        tracks=tracks,
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

    Reads each file's existing container metadata, merges the RG tags (uppercase,
    per Vorbis comment convention), and remuxes the audio stream unchanged via
    PyAV stream copy. The original file is replaced atomically.
    """
    for idx, path in enumerate(flac_paths):
        t = result.tracks[idx]
        rg_tags = {
            "REPLAYGAIN_TRACK_GAIN": f"{t.gain:+.2f} dB",
            "REPLAYGAIN_TRACK_PEAK": f"{t.peak:.6f}",
            "REPLAYGAIN_TRACK_RANGE": f"{t.lra:.2f} LU",
            "REPLAYGAIN_ALBUM_GAIN": f"{result.album_gain:+.2f} dB",
            "REPLAYGAIN_ALBUM_PEAK": f"{result.album_peak:.6f}",
            "REPLAYGAIN_ALBUM_RANGE": f"{result.album_lra:.2f} LU",
            "REPLAYGAIN_REFERENCE_LOUDNESS": f"{result.reference:.2f} LUFS",
        }
        tmp = path.with_suffix(".rgtag.flac")
        try:
            with av.open(str(path)) as in_c:
                in_stream = in_c.streams.audio[0]
                merged = {**in_c.metadata, **rg_tags}  # preserve existing tags; add RG
                with av.open(str(tmp), "w") as out_c:
                    out_c.metadata.update(merged)
                    out_stream = out_c.add_stream(template=in_stream)  # type: ignore[call-overload]  # LINT-003: template= is documented PyAV stream-copy API; missing from stubs
                    for packet in in_c.demux(in_stream):
                        if packet.dts is None:
                            continue
                        packet.stream = out_stream
                        out_c.mux(packet)
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)


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
