"""
test_offset_correct.py — Unit tests for offset_correct.apply_offset.

Sections:
  1. No-op (offset=0)
  2. Positive offset — drop start, pad zeros at end
  3. Negative offset — pad zeros at start, drop end
  4. Idempotency: +N then -N restores original
  5. Non-aligned file size raises ValueError
"""

from __future__ import annotations

import pytest

from cdda2img.offset_correct import (
    _BYTES_PER_FRAME,
    _BYTES_PER_SAMPLE,
    apply_offset,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FRAME = _BYTES_PER_FRAME  # 2352
_SAMPLE = _BYTES_PER_SAMPLE  # 4


def _make_pcm(tmp_path, n_frames: int, fill: bytes | None = None) -> tuple:
    """Write a deterministic PCM file of exactly n_frames CD frames.

    Returns (path, data) where data is the bytes written.
    """
    if fill is None:
        # Each byte is its position mod 251 (prime) so shifts are visible.
        data = bytes(i % 251 for i in range(n_frames * _FRAME))
    else:
        data = fill * (n_frames * _FRAME // len(fill))
    p = tmp_path / "rip.pcm"
    p.write_bytes(data)
    return p, data


# ---------------------------------------------------------------------------
# 1. No-op
# ---------------------------------------------------------------------------


def test_noop_offset_zero(tmp_path) -> None:
    p, original = _make_pcm(tmp_path, 4)
    apply_offset(p, 0)
    assert p.read_bytes() == original


# ---------------------------------------------------------------------------
# 2. Positive offset — drop start bytes, pad zeros at end
# ---------------------------------------------------------------------------


def test_positive_offset_drops_start_pads_end(tmp_path) -> None:
    offset = 30  # typical Plextor PX-716A
    shift = offset * _SAMPLE
    p, original = _make_pcm(tmp_path, 4)

    apply_offset(p, offset)

    result = p.read_bytes()
    assert len(result) == len(original)
    # First shift bytes of original are gone; rest is shifted into position 0
    assert result[: len(original) - shift] == original[shift:]
    # Last shift bytes are zeros
    assert result[-shift:] == bytes(shift)


def test_positive_offset_preserves_size(tmp_path) -> None:
    p, original = _make_pcm(tmp_path, 3)
    apply_offset(p, 100)
    assert len(p.read_bytes()) == len(original)


# ---------------------------------------------------------------------------
# 3. Negative offset — pad zeros at start, drop end
# ---------------------------------------------------------------------------


def test_negative_offset_pads_start_drops_end(tmp_path) -> None:
    offset = -30
    shift = abs(offset) * _SAMPLE
    p, original = _make_pcm(tmp_path, 4)

    apply_offset(p, offset)

    result = p.read_bytes()
    assert len(result) == len(original)
    # First shift bytes are zeros
    assert result[:shift] == bytes(shift)
    # Remainder is original without its last shift bytes
    assert result[shift:] == original[: len(original) - shift]


def test_negative_offset_preserves_size(tmp_path) -> None:
    p, original = _make_pcm(tmp_path, 3)
    apply_offset(p, -100)
    assert len(p.read_bytes()) == len(original)


# ---------------------------------------------------------------------------
# 4. Idempotency: +N then -N round-trips
# ---------------------------------------------------------------------------


def test_roundtrip_positive_then_negative(tmp_path) -> None:
    p, original = _make_pcm(tmp_path, 5)
    apply_offset(p, 30)
    apply_offset(p, -30)
    # The first 30*4 bytes will be zeros (they were padded on the +30 pass and
    # not recovered), but everything from byte 120 onward matches original.
    shift = 30 * _SAMPLE
    assert p.read_bytes()[shift:] == original[shift:]


def test_roundtrip_negative_then_positive(tmp_path) -> None:
    p, original = _make_pcm(tmp_path, 5)
    apply_offset(p, -30)
    apply_offset(p, 30)
    shift = 30 * _SAMPLE
    # Last shift bytes will be zeros; everything before matches.
    assert p.read_bytes()[:-shift] == original[:-shift]


# ---------------------------------------------------------------------------
# 5. Non-aligned file size raises ValueError
# ---------------------------------------------------------------------------


def test_non_aligned_size_raises(tmp_path) -> None:
    p = tmp_path / "bad.pcm"
    p.write_bytes(bytes(2352 * 3 + 1))  # one byte too many
    with pytest.raises(ValueError, match="not a multiple of"):
        apply_offset(p, 30)


def test_aligned_size_does_not_raise(tmp_path) -> None:
    p = tmp_path / "ok.pcm"
    p.write_bytes(bytes(2352 * 3))
    apply_offset(p, 30)  # should not raise
