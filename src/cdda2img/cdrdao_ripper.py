"""
cdrdao_ripper.py — CD-DA ripping via cdrdao read-cd subprocess.

Public interface:
    rip_cdrdao(device, output_pcm, progress_cb=None) -> RipInfo
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from cdda2img.disc_reader import RipInfo

if TYPE_CHECKING:
    from cdda2img.cdrdao_progress import ProgressUpdate

log = logging.getLogger(__name__)

_CMD_BASE = ["cdrdao", "read-cd"]  # LINT-013


def rip_cdrdao(
    device: str,
    output_pcm: Path,
    progress_cb: Callable[[ProgressUpdate], None] | None = None,
) -> RipInfo:
    """Rip all audio from *device* to *output_pcm* (raw s16le PCM) via cdrdao read-cd.

    cdrdao detects pre-gaps precisely via subchannel reads and writes full CD-Text
    (CATALOG, per-track ISRC, album/track titles). The BIN output is s16be and is
    byte-swapped to s16le before writing to *output_pcm*.

    When *progress_cb* is provided, cdrdao stderr is captured line-by-line and fed
    through CdrdaoProgress; each ProgressUpdate is forwarded to the callback. cdrdao
    writes all progress text to stderr (its data goes to the BIN/TOC files), and
    stdout is discarded. When *progress_cb* is None, behaviour is unchanged from
    before: subprocess.run() with no output capture.

    Returns a RipInfo with the skeleton RBIDisc and raw TOC data for CDDB/MB lookup.
    """
    from cdda2img.cdrdao_reader import convert_cdrdao_bin, parsed_to_rbi_disc
    from cdda2img.toc_parser import parse_toc

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        bin_path = tmp / "rip.bin"
        toc_path = tmp / "rip.toc"

        cmd = [
            *_CMD_BASE,
            "--device",
            device,
            "--datafile",
            str(bin_path),
            str(toc_path),
        ]

        try:
            if progress_cb is None:
                result = subprocess.run(cmd)  # noqa: S603
                if result.returncode != 0:
                    msg = f"cdrdao read-cd exited with code {result.returncode}"
                    raise RuntimeError(msg)
            else:
                _run_with_progress(cmd, progress_cb)
        except FileNotFoundError:
            msg = "cdrdao not found — install cdrdao"
            raise RuntimeError(msg) from None

        parsed = parse_toc(toc_path.read_bytes())
        disc = parsed_to_rbi_disc(parsed)

        track_lsns = [pt.start_frame + pt.pregap_frames for pt in parsed.tracks]
        last = parsed.tracks[-1]
        disc_last_lsn = last.start_frame + last.pregap_frames + last.duration_frames - 1

        log.debug(
            "cdrdao: tracks=%d disc_last_lsn=%d",
            len(parsed.tracks),
            disc_last_lsn,
        )

        convert_cdrdao_bin(bin_path, output_pcm)

    return RipInfo(disc=disc, track_lsns=track_lsns, disc_last_lsn=disc_last_lsn)


def _run_with_progress(
    cmd: list[str],
    progress_cb: Callable[[ProgressUpdate], None],
) -> None:
    """Run cdrdao, feeding each stdout line through CdrdaoProgress → progress_cb."""
    from cdda2img.cdrdao_progress import CdrdaoProgress

    proc = subprocess.Popen(  # noqa: S603
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stderr is not None  # noqa: S101  # guaranteed by stderr=PIPE

    parser = CdrdaoProgress()
    for line in proc.stderr:
        update = parser.feed(line)
        if update is not None:
            progress_cb(update)

    proc.wait()

    final = parser.done()
    if final is not None:
        progress_cb(final)

    if proc.returncode != 0:
        msg = f"cdrdao read-cd exited with code {proc.returncode}"
        raise RuntimeError(msg)
