"""
test_silence.py — unit tests for silence trimming and inter-track gap padding.

Tests use synthetic stereo s16le WAV data so no example audio is required.
The filter chain under test is:
  silenceremove (leading) → areverse → silenceremove (trailing) → areverse → apad

Key invariants:
  1. Leading and trailing silence is removed: output shorter than silence-padded input.
  2. Pad duration is appended unconditionally: content_only + pad_dur ≈ output length.
  3. The threshold_db parameter controls what counts as silence (higher = stricter).
"""

from __future__ import annotations

import wave

import numpy as np
import pytest

from cdda2img.silence import trim_silence_cd_da

RATE = 44100
CHANNELS = 2


def _write_wav(path, frames: np.ndarray) -> None:
    """Write a stereo s16le WAV to *path*. *frames* shape: (N, 2) int16."""
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes(frames.astype("<i2").tobytes())


def _duration(path) -> float:
    with wave.open(str(path)) as wf:
        return wf.getnframes() / wf.getframerate()


def _silence(secs: float) -> np.ndarray:
    n = int(secs * RATE)
    return np.zeros((n, CHANNELS), dtype=np.int16)


def _sine(secs: float, amplitude: int = 8000, freq: float = 440.0) -> np.ndarray:
    """Stereo sine wave well above any reasonable silence threshold."""
    n = int(secs * RATE)
    t = np.arange(n) / RATE
    mono = (np.sin(2 * np.pi * freq * t) * amplitude).astype(np.int16)
    return np.column_stack([mono, mono])


# ---------------------------------------------------------------------------
# Trim — silence is removed
# ---------------------------------------------------------------------------


def test_leading_silence_removed(tmp_path) -> None:
    input_wav = tmp_path / "in.wav"
    output_wav = tmp_path / "out.wav"
    # 1 s silence + 2 s signal + 0 pad
    _write_wav(input_wav, np.vstack([_silence(1.0), _sine(2.0)]))
    trim_silence_cd_da(str(input_wav), str(output_wav), pad_dur="0")
    assert _duration(output_wav) < _duration(input_wav)


def test_trailing_silence_removed(tmp_path) -> None:
    input_wav = tmp_path / "in.wav"
    output_wav = tmp_path / "out.wav"
    # 2 s signal + 1 s silence + 0 pad
    _write_wav(input_wav, np.vstack([_sine(2.0), _silence(1.0)]))
    trim_silence_cd_da(str(input_wav), str(output_wav), pad_dur="0")
    assert _duration(output_wav) < _duration(input_wav)


def test_both_ends_trimmed(tmp_path) -> None:
    input_wav = tmp_path / "in.wav"
    output_wav = tmp_path / "out.wav"
    # 1 s silence + 2 s signal + 1 s silence = 4 s total
    _write_wav(input_wav, np.vstack([_silence(1.0), _sine(2.0), _silence(1.0)]))
    trim_silence_cd_da(str(input_wav), str(output_wav), pad_dur="0")
    out_dur = _duration(output_wav)
    assert out_dur < _duration(input_wav)
    # Output should be close to just the signal duration (2 s)
    assert out_dur == pytest.approx(2.0, abs=0.2)


def test_no_silence_unchanged(tmp_path) -> None:
    input_wav = tmp_path / "in.wav"
    output_wav = tmp_path / "out.wav"
    # Pure signal — nothing to trim
    _write_wav(input_wav, _sine(2.0))
    trim_silence_cd_da(str(input_wav), str(output_wav), pad_dur="0")
    # Duration should be essentially unchanged (within one codec frame)
    assert _duration(output_wav) == pytest.approx(_duration(input_wav), abs=0.05)


# ---------------------------------------------------------------------------
# Pad — inter-track gap is appended
# ---------------------------------------------------------------------------


def test_pad_duration_appended(tmp_path) -> None:
    input_wav = tmp_path / "in.wav"
    output_wav = tmp_path / "out.wav"
    _write_wav(input_wav, _sine(2.0))
    trim_silence_cd_da(str(input_wav), str(output_wav), pad_dur="2")
    # 2 s signal + 2 s pad ≈ 4 s
    assert _duration(output_wav) == pytest.approx(4.0, abs=0.2)


def test_pad_added_after_trim(tmp_path) -> None:
    input_wav = tmp_path / "in.wav"
    output_wav = tmp_path / "out.wav"
    # 1 s silence + 2 s signal + 1 s silence (4 s total); after trim ~2 s; + 2 s pad = ~4 s
    _write_wav(input_wav, np.vstack([_silence(1.0), _sine(2.0), _silence(1.0)]))
    trim_silence_cd_da(str(input_wav), str(output_wav), pad_dur="2")
    assert _duration(output_wav) == pytest.approx(4.0, abs=0.3)


# ---------------------------------------------------------------------------
# Threshold — controls what counts as silence
# ---------------------------------------------------------------------------


def test_threshold_affects_trim_depth(tmp_path) -> None:
    """Stricter threshold treats more audio as silence and trims more aggressively.

    threshold_db=N means "audio below -N dBFS is silence".
    - threshold_db=40: silence below -40 dBFS → amplitude 327 is the cut-off
    - threshold_db=55: silence below -55 dBFS → amplitude 58 is the cut-off
    Amplitude 200 (-44 dBFS) is: SILENCE at threshold 40 but SIGNAL at threshold 55.
    A trailing high-amplitude burst ensures the output file is never empty.
    """
    input_wav = tmp_path / "in.wav"
    out_threshold40 = tmp_path / "out_40.wav"
    out_threshold55 = tmp_path / "out_55.wav"

    # 1 s zeros + 2 s amplitude-200 "quasi-silence" + 0.1 s full-amplitude anchor
    low_amp = (np.ones((RATE * 2, CHANNELS)) * 200).astype(np.int16)
    anchor = _sine(0.1)
    _write_wav(input_wav, np.vstack([_silence(1.0), low_amp, anchor]))

    # threshold_db=40 (stricter): amplitude 200 < cut-off 327 → treated as silence → ~0.1 s out
    trim_silence_cd_da(
        str(input_wav), str(out_threshold40), pad_dur="0", threshold_db=40
    )
    # threshold_db=55 (looser): amplitude 200 > cut-off 58 → treated as signal → ~2.1 s out
    trim_silence_cd_da(
        str(input_wav), str(out_threshold55), pad_dur="0", threshold_db=55
    )

    # Stricter threshold (40) removes the low-amp region; looser (55) keeps it
    assert _duration(out_threshold40) < _duration(out_threshold55)


# ---------------------------------------------------------------------------
# Output file validity
# ---------------------------------------------------------------------------


def test_output_is_valid_wav(tmp_path) -> None:
    input_wav = tmp_path / "in.wav"
    output_wav = tmp_path / "out.wav"
    _write_wav(input_wav, _sine(1.0))
    trim_silence_cd_da(str(input_wav), str(output_wav), pad_dur="1")
    # wave.open raises an exception if the file is not a valid WAV
    with wave.open(str(output_wav)) as wf:
        assert wf.getnchannels() == CHANNELS
        assert wf.getframerate() == RATE
        assert wf.getsampwidth() == 2
