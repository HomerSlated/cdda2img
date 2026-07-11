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

# DEPENDENCY: cdda2img requires a cdrdao with the bug #75 fix for correct ISRCs.
#
# cdrdao read-cd reads each track's ISRC inline from the streaming-audio
# subchannel. Affected versions stale-latch the *previous* track's ISRC when a
# track's ISRC sits in its first sectors, recording the wrong code (cdrdao bug
# #75, open upstream 2002-2026; fix submitted at
# https://github.com/cdrdao/cdrdao/issues/79 - Boyer-Moore majority vote in
# CdrDriver::audioRead). With a patched cdrdao the driver is left to per-drive
# auto-detection (bare read-cd), which is the most portable choice.
#
# On an UNPATCHED cdrdao, force the in-tool workaround by setting the driver to
# "generic-mmc:0x0014": the 0x0004 bit is OPT_MMC_READ_ISRC, which makes read-cd
# fetch each ISRC via a dedicated per-track READ SUB-CHANNEL SCSI query (the
# reliable path read-toc/cdda2wav use). Caveat: --driver name:opts replaces
# cdrdao's auto-detected options wholesale, so it also pins generic-mmc.
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


def read_toc_metadata(device: str) -> RipInfo:
    """Capture disc metadata (pre-gaps, per-track ISRC, MCN, CD-Text) via ``cdrdao
    read-toc`` WITHOUT reading the audio — the C2-recovery path pairs this with a
    separate AccuDisc audio pass. Returns a RipInfo whose disc/LSNs come from the
    parsed .toc; the audio is supplied by the caller (there is no BIN). ~one full-disc
    subchannel pass (fast-toc skips pre-gaps/ISRC/MCN, so it can't be used here).

    Returns a RipInfo with a None-valued audio (the caller writes the PCM separately).
    """
    from cdda2img.cdrdao_reader import parsed_to_rbi_disc
    from cdda2img.toc_parser import parse_toc

    with tempfile.TemporaryDirectory() as tmpdir:
        toc_path = Path(tmpdir) / "meta.toc"
        cmd = ["cdrdao", "read-toc", "--device", device, str(toc_path)]  # LINT-013
        try:
            # Capture cdrdao's verbose analysis output so it never corrupts the TUI.
            result = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
        except FileNotFoundError:
            msg = "cdrdao not found — install cdrdao"
            raise RuntimeError(msg) from None
        if result.returncode != 0:
            msg = (
                f"cdrdao read-toc exited with code {result.returncode}: "
                f"{(result.stderr or '').strip()}"
            )
            raise RuntimeError(msg)
        log.debug("cdrdao read-toc: %s", (result.stderr or "").strip())
        parsed = parse_toc(toc_path.read_bytes())

    disc = parsed_to_rbi_disc(parsed)
    track_lsns = [pt.start_frame + pt.pregap_frames for pt in parsed.tracks]
    last = parsed.tracks[-1]
    disc_last_lsn = last.start_frame + last.pregap_frames + last.duration_frames - 1
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
