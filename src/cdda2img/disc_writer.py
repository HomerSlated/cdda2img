"""
disc_writer.py — Burn an RBI container to a blank CD-DA disc via cdrdao.

Public interface:
    burn_disc(rbi_file, device, write_offset, speed, *, yes, ui) -> None
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import TYPE_CHECKING

from cdda2img.container import read_header
from cdda2img.offset_correct import apply_offset

if TYPE_CHECKING:
    from cdda2img.terminal_ui import TerminalUI

_FILE_NAME_RE = re.compile(r'(FILE\s+)"[^"]*"')
# CATALOG line with anything other than exactly 13 decimal digits
_CATALOG_BAD_RE = re.compile(r'^CATALOG\s+"([^"]*)"\s*\n?', re.MULTILINE)
# Indented CD-Text field with an empty string value
_EMPTY_CDTEXT_RE = re.compile(
    r"[ \t]+(?:TITLE|PERFORMER|SONGWRITER|COMPOSER|ARRANGER|MESSAGE|DISC_ID|GENRE)"
    r'\s+""\s*\n',
    re.MULTILINE,
)


def _patch_toc_filenames(toc_text: str) -> str:
    """Replace all FILE "..." filename fields with "disc.wav"."""
    return _FILE_NAME_RE.sub(r'\1"disc.wav"', toc_text)


def _sanitize_toc_for_burn(toc_text: str) -> str:
    """Strip TOC constructs that cdrdao write rejects but cdrdao read-cd tolerates.

    1. CATALOG with a non-13-digit value — write enforces the Red Book MCN format.
    2. CD-Text fields with empty string values — write cannot encode them; omitting
       them causes cdrdao to inherit from the disc-level block instead.
    """

    def _fix_catalog(m: re.Match[str]) -> str:
        mcn = m.group(1)
        return m.group(0) if (len(mcn) == 13 and mcn.isdigit()) else ""

    text = _CATALOG_BAD_RE.sub(_fix_catalog, toc_text)
    text = _EMPTY_CDTEXT_RE.sub("", text)
    return text


def _ui_status(ui: TerminalUI | None, text: str, prog: float = -1.0) -> None:
    if ui is not None:
        ui.clear_output()
        ui.set_status(text, prog)
    else:
        print(f"  {text}")


def _ui_print(ui: TerminalUI | None, text: str) -> None:
    if ui is not None:
        ui.add_output(text)
    else:
        print(text)


def _run_with_write_progress(
    cmd: list[str],
    cwd: str,
    ui: TerminalUI | None,
    n_tracks: int,
) -> tuple[int, list[str]]:
    """Run cdrdao write, feeding stderr into the progress parser.

    Returns ``(exit_code, stderr_lines)``. *stderr_lines* contains every
    cdrdao stderr line (used to surface error detail on failure) and is
    only populated when *ui* is active — in non-TUI mode cdrdao stderr
    passes directly to the terminal.
    """
    from cdda2img.cdrdao_write_progress import CdrdaoWriteProgress

    if ui is None:
        result = subprocess.run(cmd, cwd=cwd)  # noqa: S603
        return result.returncode, []

    parser = CdrdaoWriteProgress(n_tracks)
    stderr_lines: list[str] = []
    with subprocess.Popen(  # noqa: S603
        cmd,
        cwd=cwd,
        stderr=subprocess.PIPE,
        text=True,
    ) as proc:
        assert proc.stderr is not None  # noqa: S101
        for raw in proc.stderr:
            stderr_lines.append(raw.rstrip("\r\n"))
            update = parser.feed(raw)
            if update is not None:
                ui.set_status(update.status, update.fraction)
        proc.wait()
        final = parser.done()
        if final is not None:
            ui.set_status(final.status, final.fraction)
        return proc.returncode, stderr_lines


def _print_cdrdao_error(ui: TerminalUI | None, lines: list[str]) -> None:
    """Pause the TUI (if active) and print captured cdrdao stderr to the terminal."""
    if ui is None or not lines:
        return
    ui.pause()
    for line in lines:
        if line:
            print(f"  {line}")


def _confirm_insert(yes: bool, ui: TerminalUI | None) -> bool:
    """Pause TUI, prompt for disc insertion, resume TUI. Returns False to abort."""
    if yes:
        return True
    if ui is not None:
        ui.pause()
    try:
        answer = input("\nInsert a blank disc and press Enter, or 'q' to abort: ")
    except (EOFError, KeyboardInterrupt):
        print()
        if ui is not None:
            ui.resume()
        return False
    if answer.strip().lower() == "q":
        print("Aborted.")
        if ui is not None:
            ui.resume()
        return False
    if ui is not None:
        ui.resume()
    return True


def burn_disc(
    rbi_file: Path,
    device: str = "/dev/sr0",
    write_offset: int = 0,
    speed: int = 4,
    *,
    yes: bool = False,
    ui: TerminalUI | None = None,
) -> None:
    """Burn an RBI container to a blank disc via cdrdao.

    If *write_offset* is non-zero, applies correction to the PCM before
    burning: positive offset trims samples from the start (drive burns late);
    negative offset prepends silence (drive burns early).
    """
    header = read_header(rbi_file)

    toc_entry = header.find_block(b"TOC ")
    pcm_entry = header.find_block(b"PCM ")
    if toc_entry is None or pcm_entry is None:
        msg = "Missing required TOC or PCM block in container"
        raise ValueError(msg)

    with open(rbi_file, "rb") as f:
        f.seek(toc_entry.offset)
        toc_bytes = f.read(toc_entry.length)

    toc_text = toc_bytes.decode("utf-8")

    from cdda2img.toc_parser import parse_toc

    parsed = parse_toc(toc_bytes)
    track_count = len(parsed.tracks)
    total_frames = sum(t.pregap_frames + t.duration_frames for t in parsed.tracks)
    total_s = total_frames // 75

    _ui_print(ui, f"\n  {rbi_file.name}")
    if parsed.performer or parsed.title:
        _ui_print(ui, f"  {parsed.performer} — {parsed.title}")
    _ui_print(ui, f"  {track_count} track(s), {total_s // 60}:{total_s % 60:02d}")
    _ui_print(ui, f"  Device: {device}  Speed: {speed}x")
    if write_offset != 0:
        _ui_print(ui, f"  Write offset correction: {write_offset:+d} samples")

    if not _confirm_insert(yes, ui):
        return

    pcm_size = pcm_entry.length

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        pcm_path = tmp / "disc.pcm"
        wav_path = tmp / "disc.wav"
        toc_path = tmp / "disc.toc"

        _ui_status(ui, "Extracting PCM…")
        with open(rbi_file, "rb") as f_in, open(pcm_path, "wb") as f_out:
            f_in.seek(pcm_entry.offset)
            _copy_bytes(f_in, f_out, pcm_size)

        if write_offset != 0:
            _ui_status(ui, "Applying write offset…")
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

        toc_path.write_text(
            _sanitize_toc_for_burn(_patch_toc_filenames(toc_text)), encoding="utf-8"
        )

        _ui_status(ui, f"Burning track 1/{track_count}")
        cmd = [
            "cdrdao",
            "write",
            "--device",
            device,
            "--speed",
            str(speed),
            "--eject",
            toc_path.name,
        ]
        rc, cdrdao_stderr = _run_with_write_progress(cmd, str(tmp), ui, track_count)
        if rc != 0:
            _print_cdrdao_error(ui, cdrdao_stderr)
            msg = f"cdrdao write failed (exit {rc})"
            raise RuntimeError(msg)

    _ui_status(ui, "Done.", 1.0)
    if ui is None:
        print("Done.")


def _copy_bytes(f_in, f_out, length: int) -> None:
    remaining = length
    while remaining:
        chunk = f_in.read(min(remaining, 1 << 20))
        if not chunk:
            break
        f_out.write(chunk)
        remaining -= len(chunk)
