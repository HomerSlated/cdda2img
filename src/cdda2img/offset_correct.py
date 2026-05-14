"""
offset_correct.py — Post-rip drive read offset correction for raw s16le PCM.

Public interface:
    apply_drive_offset(pcm_path, drive_offset) -> None
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

_BYTES_PER_SAMPLE = 4  # stereo s16le: 2 channels x 2 bytes
_BYTES_PER_FRAME = 2352  # 588 stereo sample pairs x 4 bytes


def apply_drive_offset(pcm_path: Path, drive_offset: int) -> None:
    """Shift raw s16le PCM by *drive_offset* stereo samples in-place.

    Positive offset: drive reads N samples ahead of the correct position —
    drop the first N*4 bytes and append N*4 zero bytes at the end.

    Negative offset: drive reads |N| samples behind — prepend |N|*4 zero bytes
    and drop the last |N|*4 bytes.

    Raises ValueError if the file size is not a multiple of 2352 bytes (one CD
    frame), which would indicate an incomplete or malformed rip.
    """
    if drive_offset == 0:
        return

    size = pcm_path.stat().st_size
    if size % _BYTES_PER_FRAME != 0:
        msg = (
            f"PCM size {size} is not a multiple of {_BYTES_PER_FRAME} — "
            "cannot apply drive offset to a malformed rip"
        )
        raise ValueError(msg)

    shift = abs(drive_offset) * _BYTES_PER_SAMPLE

    tmp_fd, tmp_name = tempfile.mkstemp(dir=pcm_path.parent, suffix=".tmp")
    try:
        with open(tmp_fd, "wb") as dst, open(pcm_path, "rb") as src:
            if drive_offset > 0:
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
