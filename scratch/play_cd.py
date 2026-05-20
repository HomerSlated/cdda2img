#!/usr/bin/env python3
"""Prototype: read the first 30s of CD audio from /dev/sr0 and play it.

cd-paranoia -r  →  raw s16le stdout  →  WAV header  →  pw-play
No Python audio dependencies required.
"""

import struct
import subprocess


def _wav_header(
    data_len: int, sample_rate: int = 44100, channels: int = 2, bits: int = 16
) -> bytes:
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    return (
        b"RIFF"
        + struct.pack("<I", 36 + data_len)
        + b"WAVEfmt "
        + struct.pack(
            "<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits
        )
        + b"data"
        + struct.pack("<I", data_len)
    )


DEVICE = "/dev/sr0"
SAMPLE_RATE = 44100
CHANNELS = 2
SECONDS = 30
BYTES_PER_SECTOR = 2352  # 1 CD frame = 588 stereo sample-pairs × 4 bytes
SECTORS_PER_SECOND = 75
BYTES_TO_READ = SECONDS * SECTORS_PER_SECOND * BYTES_PER_SECTOR  # 5,292,000

print(f"Reading {SECONDS}s ({BYTES_TO_READ / 1024 / 1024:.1f} MiB) from {DEVICE}...")

reader = subprocess.Popen(
    ["cd-paranoia", "-d", DEVICE, "-r", "-q", "1:", "-"],
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL,
)
pcm = reader.stdout.read(BYTES_TO_READ)
reader.kill()
reader.wait()

print(f"  Read {len(pcm) / 1024 / 1024:.1f} MiB into RAM")
print("Playing via pw-play (WAV/s16le, 44100 Hz, stereo)...")

wav = _wav_header(len(pcm), SAMPLE_RATE, CHANNELS) + pcm
player = subprocess.Popen(["pw-play", "-"], stdin=subprocess.PIPE)
player.stdin.write(wav)
player.stdin.close()
player.wait()

print("Done.")
