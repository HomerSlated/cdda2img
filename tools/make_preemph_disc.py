#!/usr/bin/env python3
"""make_preemph_disc.py — generate a PRE_EMPHASIS CD-DA test image (cdrdao TOC+BIN).

Builds a small synthetic audio disc whose every track carries the ``PRE_EMPHASIS``
flag, for validating the pipeline's pre-emphasis handling end-to-end. The image is
a cdrdao ``.toc`` + raw ``.bin`` (s16be, cdrdao's byte order), which cdemu loads
directly into a virtual drive:

    uv run python tools/make_preemph_disc.py --outdir /var/tmp/preemph_disc
    cdemu load 0 /var/tmp/preemph_disc/preemph.toc      # → /dev/sr1
    uv run python -m cdda2img rip --device /dev/sr1 --auto --no-preview --no-tui
    # → the RBI's PROV shows `pre_emphasis=YES`

Why this validates the *read* path (not just TOC-text parsing): libmirage's
image-toc parser turns each ``PRE_EMPHASIS`` line into a track CONTROL flag, and
the cdemu virtual drive emits it in the synthesized Q-subchannel (CONTROL nibble
bit 0). A rip reads that back through AccuDisc ``read --sub`` and ``subq_toc``,
which majority-votes CONTROL per track — the same code path a physical
pre-emphasis disc exercises. (Confirmed 2026-07-11: all Q position-frames carried
CONTROL 0x1.)

The tone is synthesized in pure Python (no sox/ffmpeg dependency): the audio
content is irrelevant to the pre-emphasis flag, so a plain sine per track suffices.
"""

from __future__ import annotations

import argparse
import math
import struct
from pathlib import Path

_SAMPLE_RATE = 44_100
_CHANNELS = 2
_FRAMES_PER_SEC = 75  # CD sectors per second
_SAMPLES_PER_SECTOR = _SAMPLE_RATE // _FRAMES_PER_SEC  # 588 stereo pairs


def _sine_track_s16be(seconds: int, hz: float, amplitude: float = 0.3) -> bytes:
    """Return raw s16be stereo PCM for a *seconds*-long sine at *hz*.

    s16be matches cdrdao's BIN byte order (the ``cdrdao BIN is s16be`` invariant).
    Length is rounded up to a whole CD sector so the TOC's MSF offsets stay exact.
    """
    total_pairs = seconds * _SAMPLE_RATE
    # Round up to a whole sector (588 stereo pairs).
    total_pairs = (
        (total_pairs + _SAMPLES_PER_SECTOR - 1) // _SAMPLES_PER_SECTOR
    ) * _SAMPLES_PER_SECTOR
    peak = int(amplitude * 32767)
    out = bytearray()
    for i in range(total_pairs):
        s = int(peak * math.sin(2 * math.pi * hz * i / _SAMPLE_RATE))
        sample = struct.pack(">h", s)  # big-endian s16
        out += sample * _CHANNELS  # same on L and R
    return bytes(out)


def _msf(seconds: int) -> str:
    """Render a whole-second duration as cdrdao MM:SS:FF (FF frames = 0)."""
    return f"{seconds // 60:02d}:{seconds % 60:02d}:00"


def build(outdir: Path, tracks: int, seconds: int) -> Path:
    """Write ``preemph.bin`` + ``preemph.toc`` under *outdir*; return the TOC path."""
    outdir.mkdir(parents=True, exist_ok=True)
    bin_path = outdir / "preemph.bin"
    toc_path = outdir / "preemph.toc"

    # One BIN holding all tracks back-to-back; the TOC slices it by MSF offset.
    freqs = [440.0 * (2 ** (t / 12)) for t in range(tracks)]  # rising semitones
    with bin_path.open("wb") as fp:
        for hz in freqs:
            fp.write(_sine_track_s16be(seconds, hz))

    lines = ["CD_DA", ""]
    for t in range(tracks):
        lines += [
            f"// Track {t + 1} — PRE_EMPHASIS",
            "TRACK AUDIO",
            "PRE_EMPHASIS",
            "TWO_CHANNEL_AUDIO",
            f'FILE "{bin_path.name}" {_msf(t * seconds)} {_msf(seconds)}',
            "",
        ]
    toc_path.write_text("\n".join(lines))
    return toc_path


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate a PRE_EMPHASIS CD-DA test image (cdrdao TOC+BIN)."
    )
    ap.add_argument(
        "--outdir",
        type=Path,
        # Disk-backed temp by default (not tmpfs /tmp) — the BIN can be large;
        # a fixed dev-tool output dir, not a security-sensitive temp file.
        default=Path("/var/tmp/preemph_disc"),  # noqa: S108
        help="output directory for preemph.toc + preemph.bin (default /var/tmp/preemph_disc)",
    )
    ap.add_argument(
        "--tracks", type=int, default=2, help="number of tracks (default 2)"
    )
    ap.add_argument(
        "--seconds",
        type=int,
        default=5,
        help="seconds per track (default 5; min 4 for CD-DA)",
    )
    args = ap.parse_args()
    if args.seconds < 4:
        ap.error("CD-DA tracks must be at least 4 seconds")

    toc = build(args.outdir, args.tracks, args.seconds)
    print(f"wrote {toc}")
    print(f"      {toc.with_suffix('.bin')}")
    print("load it:  cdemu load 0", toc)


if __name__ == "__main__":
    main()
