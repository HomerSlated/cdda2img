"""
disc_writer.py — Burn an RBI container to a blank CD-DA disc via AccuDisc.

AccuDisc ``write`` consumes a cdrdao-format ``.toc`` plus a raw **s16le** audio
BIN (``write --toc FILE --bin FILE``) and writes a Disc-At-Once audio session.
It expects little-endian PCM and handles the drive's own byte order internally
(the PX-716A is LE), so the normal pipeline is swap-free: the RBI stores s16le,
and the BIN is that PCM verbatim — no WAV wrapper, no byte-swap (``--byteswap``
is AccuDisc's manual override for legacy s16be inputs only). AccuDisc is a
separate external project (https://github.com/HomerSlated/accudisc); a
git-ignored local snapshot in ``tools/accudisc/`` is used, resolved via
:data:`cdda2img.accudisc_reader._ACCUDISC`. cdrdao no longer plays any role in
burning.

Public interface:
    burn_disc(rbi_file, device, write_offset, speed, *, simulate, yes, ui) -> None
"""

from __future__ import annotations

import re
import subprocess
import tempfile
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
# LANGUAGE block whose body is empty (all fields were stripped above)
_EMPTY_LANG_BLOCK_RE = re.compile(
    r"  LANGUAGE \d+ \{\n  \}\n",
    re.MULTILINE,
)
# CD_TEXT block left empty after stripping fields/LANGUAGE sections.
# Optional inner group matches a disc-level LANGUAGE_MAP; without it the
# block is a track-level shell.  [^}]* is safe because LANGUAGE_MAP content
# never contains a bare '}' character.
_EMPTY_CDTEXT_SHELL_RE = re.compile(
    r"^CD_TEXT \{\n(?:  LANGUAGE_MAP \{[^}]*\}\n)?\}\n?",
    re.MULTILINE,
)


def _patch_toc_filenames(toc_text: str) -> str:
    """Replace all FILE "..." filename fields with "disc.pcm" (the raw s16le BIN).

    AccuDisc takes the audio path explicitly via ``--bin`` and reads the ``.toc``
    only for track layout (per-track ``FILE`` frame offsets/lengths and ``START``),
    so this filename is cosmetic — but keeping it consistent with the BIN we pass
    avoids confusion. The frame offsets are container-agnostic and map directly
    onto the header-less raw BIN (frame x 2352 from byte 0).
    """
    return _FILE_NAME_RE.sub(r'\1"disc.pcm"', toc_text)


def _sanitize_toc_for_burn(toc_text: str) -> str:
    """Strip TOC constructs that cdrdao write rejects but cdrdao read-cd tolerates.

    1. CATALOG with a non-13-digit value — write enforces the Red Book MCN format.
    2. CD-Text fields with empty string values — cdrdao's encoder receives a NULL
       pointer and fails; omit them so cdrdao inherits from the disc-level block.
    3. Empty LANGUAGE blocks left behind after step 2.
    4. Empty CD_TEXT shells left behind after step 3.
    """

    def _fix_catalog(m: re.Match[str]) -> str:
        mcn = m.group(1)
        return m.group(0) if (len(mcn) == 13 and mcn.isdigit()) else ""

    text = _CATALOG_BAD_RE.sub(_fix_catalog, toc_text)
    text = _EMPTY_CDTEXT_RE.sub("", text)
    text = _EMPTY_LANG_BLOCK_RE.sub("", text)
    text = _EMPTY_CDTEXT_SHELL_RE.sub("", text)
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


def _run_accudisc_write(
    cmd: list[str],
    ui: TerminalUI | None,
    n_tracks: int,
) -> tuple[int, str, str | None]:
    """Run ``accudisc write`` with the ``--progress-fd 1`` machine channel.

    AccuDisc emits newline-delimited ``progress <done> <total>`` tokens and a
    final ``summary … result=<token>`` line on the progress fd; we forward the
    sector counts to the UI and keep the ``result=`` token. stderr goes to a temp
    file (never a pipe) so a chatty burn can't deadlock the single-threaded
    stdout reader; it is read back for error detail.

    Returns ``(returncode, stderr_text, result_token)``. The token is the
    *machine* outcome — stderr wording is explicitly not a stable interface, so
    it must never be the thing a decision keys on.
    """
    status = f"Burning {n_tracks} track(s)…"
    result_token: str | None = None
    with tempfile.TemporaryFile() as err_fp:
        proc = subprocess.Popen(  # noqa: S603
            [*cmd, "--progress-fd", "1"],
            stdout=subprocess.PIPE,
            stderr=err_fp,
            text=True,
        )
        assert proc.stdout is not None  # noqa: S101
        for line in proc.stdout:
            parts = line.split()
            if len(parts) == 3 and parts[0] == "progress" and ui is not None:
                try:
                    done, total = int(parts[1]), int(parts[2])
                except ValueError:
                    continue
                ui.set_status(status, done / total if total > 0 else 0.0)
            elif parts and parts[0] == "summary":
                for tok in parts[1:]:
                    if tok.startswith("result="):
                        result_token = tok.partition("=")[2]
        proc.wait()
        err_fp.seek(0)
        stderr_text = err_fp.read().decode(errors="replace")
    return proc.returncode, stderr_text, result_token


def _print_burn_error(ui: TerminalUI | None, stderr_text: str) -> None:
    """Pause the TUI (if active) and print captured AccuDisc stderr to the terminal."""
    lines = [ln for ln in stderr_text.splitlines() if ln.strip()]
    if not lines:
        return
    if ui is not None:
        ui.pause()
    for line in lines:
        print(f"  {line}")


def _write_disc(
    device: str,
    toc_path: Path,
    pcm_path: Path,
    speed: int,
    *,
    simulate: bool,
    ui: TerminalUI | None,
    track_count: int,
) -> None:
    """Invoke ``accudisc write`` and map its exit code to a clear outcome.

    AccuDisc's tool-wide exit convention (reconciled in AccuDisc ``b547a60``,
    2026-07-24): ``0`` clean burn; ``1`` usage; ``2`` could-not-complete — **disc
    not written** (disc-not-blank, or any transport/device failure); ``3``
    completed **with caveats** — the disc **was** written but some metadata is
    imperfect (e.g. CD-Text SIZE_INFO disagrees with the ``.toc``).

    Exit 3 is therefore a *success with a warning*, not a failure: we surface the
    caveat loudly — the burn invariant is that a written disc must never *silently*
    carry mismatched or dropped metadata — and return. Only ``1``/``2`` raise, with
    a targeted message for the disc-not-blank case (which moved from 3 to 2 in the
    reconciliation, hence keyed on the stderr text, not the bare code).
    """
    from cdda2img.accudisc_reader import _ACCUDISC

    _ui_status(ui, f"Burning {track_count} track(s)…")
    cmd = [
        _ACCUDISC,
        "--device",
        device,
        "write",
        "--toc",
        str(toc_path),
        "--bin",
        str(pcm_path),
        "--speed",
        str(speed),
    ]
    if simulate:
        cmd.append("--simulate")
    rc, stderr_text, result = _run_accudisc_write(cmd, ui, track_count)
    if rc == 0:
        return
    if rc == 3:
        # Completed WITH caveats: the disc WAS written. Surface the detail (never
        # swallow it) and return success — do NOT raise, or the caller would treat
        # a written disc as a failed burn.
        for line in (ln for ln in stderr_text.splitlines() if ln.strip()):
            _ui_print(ui, f"  {line}")
        _ui_print(
            ui,
            "  WARNING: burn completed with a caveat — the disc's CD-Text may not "
            "match its audio (see above). The audio itself was written correctly.",
        )
        return
    _print_burn_error(ui, stderr_text)
    # Keyed on AccuDisc's machine token, not on stderr wording — their contract
    # explicitly reserves the right to reword stderr, and exit 2 also covers
    # transport/device failure, so the exit code alone cannot disambiguate.
    if result == "not_blank":
        msg = "disc is not blank — insert a blank CD-R/RW and retry"
        raise RuntimeError(msg)
    detail = stderr_text.strip().splitlines()[-1] if stderr_text.strip() else ""
    msg = f"accudisc write failed (exit {rc}): {detail}"
    raise RuntimeError(msg)


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
    simulate: bool = False,
    yes: bool = False,
    ui: TerminalUI | None = None,
) -> None:
    """Burn an RBI container to a blank disc via AccuDisc ``write``.

    If *write_offset* is non-zero, applies correction to the PCM before
    burning: positive offset trims samples from the start (drive burns late);
    negative offset prepends silence (drive burns early). With *simulate* the
    full write path runs with the laser off (test write) — useful for validating
    the geometry without consuming a blank.
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
    _ui_print(
        ui, f"  Device: {device}  Speed: {speed}x{'  (SIMULATE)' if simulate else ''}"
    )
    if write_offset != 0:
        _ui_print(ui, f"  Write offset correction: {write_offset:+d} samples")

    if not _confirm_insert(yes, ui):
        return

    pcm_size = pcm_entry.length

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        pcm_path = tmp / "disc.pcm"
        toc_path = tmp / "disc.toc"

        # Raw s16le PCM straight from the RBI — AccuDisc's write path expects
        # little-endian and adapts to the drive itself, so no WAV wrapper and no
        # byte-swap (the whole pipeline stays swap-free).
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

        toc_path.write_text(
            _sanitize_toc_for_burn(_patch_toc_filenames(toc_text)), encoding="utf-8"
        )

        _write_disc(
            device,
            toc_path,
            pcm_path,
            speed,
            simulate=simulate,
            ui=ui,
            track_count=track_count,
        )

    _ui_status(ui, "Done.", 1.0)
    if ui is not None:
        ui.pause()
        print("  Done." + ("  (simulated — no data written)" if simulate else ""))


def _copy_bytes(f_in, f_out, length: int) -> None:
    remaining = length
    while remaining:
        chunk = f_in.read(min(remaining, 1 << 20))
        if not chunk:
            break
        f_out.write(chunk)
        remaining -= len(chunk)
