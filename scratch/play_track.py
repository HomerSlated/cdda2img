#!/usr/bin/env python3
"""Prototype: rip the first CD track to a temp WAV and play it.

cd-paranoia 1 → temp WAV file → pw-play
"""

import subprocess
import tempfile
from pathlib import Path

DEVICE = "/dev/sr0"
TRACK = 1

with tempfile.TemporaryDirectory(prefix="cdplay_") as tmpdir:
    wav_path = Path(tmpdir) / f"track{TRACK:02d}.wav"

    print(f"Ripping track {TRACK} from {DEVICE}...")
    subprocess.run(
        ["cd-paranoia", "-d", DEVICE, "-q", str(TRACK), str(wav_path)],
        check=True,
    )
    size_mb = wav_path.stat().st_size / 1024 / 1024
    print(f"  Ripped {size_mb:.1f} MiB → {wav_path.name}")

    print("Playing via pw-play...")
    subprocess.run(["pw-play", str(wav_path)], check=True)

print("Done.")
