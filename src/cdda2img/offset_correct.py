"""
offset_correct.py — Post-rip drive read offset correction for raw s16le PCM.

Public interface:
    apply_offset(pcm_path, offset) -> None
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

_BYTES_PER_SAMPLE = 4  # stereo s16le: 2 channels x 2 bytes
_BYTES_PER_FRAME = 2352  # 588 stereo sample pairs x 4 bytes


def apply_offset(pcm_path: Path, offset: int) -> None:
    """Shift raw s16le PCM by *offset* stereo samples in-place.

    Positive offset: drive reads N samples ahead of the correct position —
    drop the first N*4 bytes and append N*4 zero bytes at the end.

    Negative offset: drive reads |N| samples behind — prepend |N|*4 zero bytes
    and drop the last |N|*4 bytes.

    Raises ValueError if the file size is not a multiple of 2352 bytes (one CD
    frame). Shifting audio by a sample offset cannot repair a stream that does
    not hold a whole number of frames, and every consumer downstream addresses
    it as ``frame x 2352`` — so this refuses rather than silently producing a
    differently-misaligned stream. See rbi_spec §6.2.1; validation rule 31 is
    the primary check, and this is the last line of defence for callers that
    reach here with PCM from somewhere else.
    """
    if offset == 0:
        return

    size = pcm_path.stat().st_size
    if size % _BYTES_PER_FRAME != 0:
        msg = (
            f"PCM size {size} is not a multiple of {_BYTES_PER_FRAME} "
            f"({size % _BYTES_PER_FRAME} bytes into a partial frame) — audio "
            "and declared track geometry disagree (rbi_spec §6.2.1); "
            "cannot apply a drive offset to it"
        )
        raise ValueError(msg)

    shift = abs(offset) * _BYTES_PER_SAMPLE

    tmp_fd, tmp_name = tempfile.mkstemp(dir=pcm_path.parent, suffix=".tmp")
    try:
        with open(tmp_fd, "wb") as dst, open(pcm_path, "rb") as src:
            if offset > 0:
                # Drop the first shift bytes (drive was ahead); pad zeros at end.
                src.seek(shift)
                shutil.copyfileobj(src, dst)
                dst.write(bytes(shift))
            else:
                # Prepend shift zero bytes; drop the last shift bytes.
                dst.write(bytes(shift))
                shutil.copyfileobj(src, dst)
                dst.seek(-shift, 2)
                dst.truncate()
        Path(tmp_name).replace(pcm_path)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise
