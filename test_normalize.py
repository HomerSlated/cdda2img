#!/usr/bin/env python3
"""Standalone test for ffmpeg-normalize Python API."""

import sys

from ffmpeg_normalize import FFmpegNormalize

INPUT = "example/Koiduuni.mp3"
OUTPUT = "/tmp/koiduuni_normalized.wav"  # noqa: S108


def main():
    norm = FFmpegNormalize(
        normalization_type="ebu",
        target_level=-5.0,
        auto_lower_loudness_target=True,
        audio_codec="pcm_s16le",
        sample_rate=44100,
        audio_channels=2,
        progress=True,
    )
    norm.add_media_file(INPUT, OUTPUT)
    norm.run_normalization()
    print(f"Output: {OUTPUT}")


if __name__ == "__main__":
    sys.exit(main())
