"""
disc_writer.py — Burn an RBI container to a blank CD-DA disc via cdrdao.

Public interface:
    burn_disc(rbi_file, device, write_offset, speed, *, yes) -> None
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import wave
from pathlib import Path

from cdda2img.container import read_header
from cdda2img.offset_correct import apply_offset

_FILE_NAME_RE = re.compile(r'(FILE\s+)"[^"]*"')


def _patch_toc_filenames(toc_text: str) -> str:
    """Replace all FILE "..." filename fields with "disc.wav"."""
    return _FILE_NAME_RE.sub(r'\1"disc.wav"', toc_text)


def burn_disc(
    rbi_file: Path,
    device: str = "/dev/sr0",
    write_offset: int = 0,
    speed: int = 4,
    *,
    yes: bool = False,
) -> None:
    """Burn an RBI container to a blank disc via cdrdao.

    If *write_offset* is non-zero, applies correction to the PCM before
    burning: positive offset trims samples from the start (drive burns late);
    negative offset prepends silence (drive burns early).
    """
    header = read_header(rbi_file)

    with open(rbi_file, "rb") as f:
        f.seek(header.toc_start)
        toc_bytes = f.read(header.toc_end - header.toc_start)

    toc_text = toc_bytes.decode("utf-8")

    from cdda2img.toc_parser import parse_toc

    parsed = parse_toc(toc_bytes)
    track_count = len(parsed.tracks)
    total_frames = sum(t.pregap_frames + t.duration_frames for t in parsed.tracks)
    total_s = total_frames // 75
    print(f"\n  {rbi_file.name}")
    if parsed.performer or parsed.title:
        print(f"  {parsed.performer} — {parsed.title}")
    print(f"  {track_count} track(s), {total_s // 60}:{total_s % 60:02d}")
    print(f"  Device: {device}  Speed: {speed}x")
    if write_offset != 0:
        print(f"  Write offset correction: {write_offset:+d} samples")

    if not yes:
        try:
            answer = input("\nInsert a blank disc and press Enter, or 'q' to abort: ")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if answer.strip().lower() == "q":
            print("Aborted.")
            return

    pcm_size = header.pcm_end - header.pcm_start

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        pcm_path = tmp / "disc.pcm"
        wav_path = tmp / "disc.wav"
        toc_path = tmp / "disc.toc"

        with open(rbi_file, "rb") as f_in, open(pcm_path, "wb") as f_out:
            f_in.seek(header.pcm_start)
            _copy_bytes(f_in, f_out, pcm_size)

        if write_offset != 0:
            try:
                apply_offset(pcm_path, write_offset)
            except ValueError as exc:
                msg = f"Cannot apply write offset: {exc}"
                raise RuntimeError(msg) from exc

        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(header.pcm_channels)
            wf.setsampwidth(header.pcm_bit_depth // 8)
            wf.setframerate(header.pcm_sample_rate)
            wf.writeframes(pcm_path.read_bytes())

        toc_path.write_text(_patch_toc_filenames(toc_text), encoding="utf-8")

        print("Burning ...")
        result = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "cdrdao",
                "write",
                "--device",
                device,
                "--speed",
                str(speed),
                "--eject",
                toc_path.name,
            ],
            cwd=str(tmp),
        )
        if result.returncode != 0:
            msg = f"cdrdao write failed (exit {result.returncode})"
            raise RuntimeError(msg)

    print("Done.")


def _copy_bytes(f_in, f_out, length: int) -> None:
    remaining = length
    while remaining:
        chunk = f_in.read(min(remaining, 1 << 20))
        if not chunk:
            break
        f_out.write(chunk)
        remaining -= len(chunk)
