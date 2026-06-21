"""
test_cdrdao_ripper.py — Unit tests for the cdrdao read-cd rip command.

Regression guard for cdrdao bug #75 (ISRC stale-latch on inline subchannel
reads): rip_cdrdao must force the generic-mmc driver with OPT_MMC_READ_ISRC
(0x0004) set, i.e. ``--driver generic-mmc:0x0014``. Dropping that flag silently
reintroduces wrong per-track ISRCs whenever a track's ISRC sits in its first
sectors. See src/cdda2img/cdrdao_ripper.py and
https://sourceforge.net/p/cdrdao/bugs/75/.
"""

from pathlib import Path
from unittest.mock import patch

from cdda2img import cdrdao_ripper


class _StubTrack:
    start_frame = 0
    pregap_frames = 0
    duration_frames = 100


class _StubParsed:
    tracks = (_StubTrack(),)


class _Proc:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode


def _capture_run(captured: list[list[str]]):
    """A fake subprocess.run that records the cmd and writes the TOC arg."""

    def fake_run(cmd, *args, **kwargs):
        captured.append(list(cmd))
        # rip_cdrdao reads the TOC file back; the last arg is its path.
        Path(cmd[-1]).write_bytes(b"")
        return _Proc(0)

    return fake_run


def test_rip_cdrdao_forces_read_isrc_driver_flag(tmp_path):
    """The rip command pins generic-mmc and sets OPT_MMC_READ_ISRC (bug #75)."""
    captured: list[list[str]] = []
    out_pcm = tmp_path / "out.pcm"

    with (
        patch.object(cdrdao_ripper.subprocess, "run", _capture_run(captured)),
        patch("cdda2img.toc_parser.parse_toc", return_value=_StubParsed()),
        patch("cdda2img.cdrdao_reader.parsed_to_rbi_disc", return_value="DISC"),
        patch("cdda2img.cdrdao_reader.convert_cdrdao_bin"),
    ):
        info = cdrdao_ripper.rip_cdrdao("/dev/sr0", out_pcm)

    assert len(captured) == 1
    cmd = captured[0]

    # The driver flag must be present and carry the 0x0004 (READ_ISRC) bit.
    assert "--driver" in cmd
    driver_arg = cmd[cmd.index("--driver") + 1]
    assert driver_arg == "generic-mmc:0x0014"
    driver_name, _, opts_hex = driver_arg.partition(":")
    assert driver_name == "generic-mmc"
    assert int(opts_hex, 16) & 0x0004, "OPT_MMC_READ_ISRC (0x0004) must be set"

    # Sanity: it is still a read-cd invocation for the requested device.
    assert cmd[:2] == ["cdrdao", "read-cd"]
    assert "--device" in cmd and cmd[cmd.index("--device") + 1] == "/dev/sr0"

    assert info.disc == "DISC"
