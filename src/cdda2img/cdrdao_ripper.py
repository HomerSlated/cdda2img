"""
cdrdao_ripper.py — CD-DA ripping via cdrdao read-cd subprocess.

Public interface:
    rip_cdrdao(device, output_pcm) -> RipInfo
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

from cdda2img.disc_reader import RipInfo

log = logging.getLogger(__name__)


def rip_cdrdao(device: str, output_pcm: Path) -> RipInfo:
    """Rip all audio from *device* to *output_pcm* (raw s16le PCM) via cdrdao read-cd.

    cdrdao detects pre-gaps precisely via subchannel reads and writes full CD-Text
    (CATALOG, per-track ISRC, album/track titles). The BIN output is s16be and is
    byte-swapped to s16le before writing to *output_pcm*.

    Returns a RipInfo with the skeleton RBIDisc and raw TOC data for CDDB/MB lookup.
    cdrdao progress output passes through to the terminal directly.
    """
    from cdda2img.cdrdao_reader import convert_cdrdao_bin, parsed_to_rbi_disc
    from cdda2img.toc_parser import parse_toc

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        bin_path = tmp / "rip.bin"
        toc_path = tmp / "rip.toc"

        try:
            result = subprocess.run(  # noqa: S603  # LINT-013
                [  # noqa: S607  # LINT-013
                    "cdrdao",
                    "read-cd",
                    "--device",
                    device,
                    "--datafile",
                    str(bin_path),
                    str(toc_path),
                ],
            )
        except FileNotFoundError:
            msg = "cdrdao not found — install cdrdao"
            raise RuntimeError(msg) from None

        if result.returncode != 0:
            msg = f"cdrdao read-cd exited with code {result.returncode}"
            raise RuntimeError(msg)

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
