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
        ref_params: wave._wave_params | None = None  # type: ignore[name-defined]
        for path in input_files:
            with wave.open(str(path), "rb") as inp:
                params = inp.getparams()
                if ref_params is None:
                    ref_params = params
                    out.setparams(params)
                elif (params.nchannels, params.sampwidth, params.framerate) != (
                    ref_params.nchannels,
                    ref_params.sampwidth,
                    ref_params.framerate,
                ):
                    msg = (
                        f"{path.name}: WAV parameters do not match first file "
                        f"(channels={params.nchannels} width={params.sampwidth} "
                        f"rate={params.framerate} vs "
                        f"{ref_params.nchannels}/{ref_params.sampwidth}/{ref_params.framerate})"
                    )
                    raise ValueError(msg)
                out.writeframes(inp.readframes(inp.getnframes()))
