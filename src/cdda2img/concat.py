"""
concat.py — Concatenate multiple WAV files into a single WAV output.
"""

import wave
from pathlib import Path


def concat_wav(input_files: list[Path], output_path: Path) -> None:
    """Concatenate WAV files in order, writing a single WAV to output_path.

    All inputs must share the same sample rate, channel count, and sample width
    (guaranteed when they come from transcode.py + silence.py).
    """
    if not input_files:
        msg = "No input files to concatenate"
        raise ValueError(msg)

    with wave.open(str(output_path), "wb") as out:
        params_set = False
        for path in input_files:
            with wave.open(str(path), "rb") as inp:
                if not params_set:
                    out.setparams(inp.getparams())
                    params_set = True
                out.writeframes(inp.readframes(inp.getnframes()))
