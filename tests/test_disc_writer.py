"""Tests for disc_writer.py: TOC sanitization + the AccuDisc write invocation."""

import io
from pathlib import Path

import pytest

from cdda2img import disc_writer
from cdda2img.container import build_container
from cdda2img.disc_writer import _sanitize_toc_for_burn
from cdda2img.rbi_format import RBIDisc, RBITocEntry
from cdda2img.toc import generate_toc


def _toc(
    catalog: str | None = None,
    tracks: list[tuple[str, str]] | None = None,
    disc_title: str = "Test Album",
    disc_performer: str = "Test Artist",
) -> str:
    lines = ["CD_DA\n"]
    if catalog is not None:
        lines.append(f'CATALOG "{catalog}"\n')
    # Always emit the disc-level block as-is (including empty-string fields) so
    # _sanitize_toc_for_burn can be tested against old-RBI-style empty shells.
    lines += [
        "CD_TEXT {",
        "  LANGUAGE_MAP { 0: 9 }",
        "  LANGUAGE 0 {",
        f'    TITLE "{disc_title}"',
        f'    PERFORMER "{disc_performer}"',
        "  }",
        "}\n",
    ]
    for title, performer in tracks or [("Track 1", "Test Artist")]:
        lines += [
            "TRACK AUDIO",
            "NO COPY",
            "CD_TEXT {",
            "  LANGUAGE 0 {",
            f'    TITLE "{title}"',
            f'    PERFORMER "{performer}"',
            "  }",
            "}",
            'FILE "disc.wav" 0 1:00:00',
            "",
        ]
    return "\n".join(lines)


# ── AccuDisc write invocation (mocked subprocess) ─────────────────────────────


def _tiny_rbi(tmp_path: Path) -> Path:
    disc = RBIDisc(
        album="Album",
        artist="Artist",
        tracks=[
            RBITocEntry(1, "One", "Artist", start_frame=0, duration_frames=150),
            RBITocEntry(2, "Two", "Artist", start_frame=150, duration_frames=150),
        ],
    )
    pcm = tmp_path / "all.pcm"
    pcm.write_bytes(b"\x00" * (2352 * 300))  # 300 frames of silence, s16le
    rbi = tmp_path / "disc.rbi"
    build_container(pcm, generate_toc(disc), disc, rbi, quiet=True)
    return rbi


class _FakeProc:
    def __init__(self, stdout: str, returncode: int) -> None:
        self.stdout = io.StringIO(stdout)
        self.returncode = returncode

    def wait(self) -> int:
        return self.returncode


def _patch_popen(monkeypatch, stdout: str = "", returncode: int = 0) -> dict:
    captured: dict = {}

    def _popen(cmd, **k):
        captured["cmd"] = cmd
        return _FakeProc(stdout, returncode)

    monkeypatch.setattr(disc_writer.subprocess, "Popen", _popen)
    return captured


def test_burn_invokes_accudisc_write_with_raw_s16le_bin(tmp_path, monkeypatch):
    rbi = _tiny_rbi(tmp_path)
    cap = _patch_popen(monkeypatch, "progress 300 300\nsummary result=ok\n", 0)
    disc_writer.burn_disc(rbi, device="/dev/sr0", speed=8, yes=True)
    cmd = cap["cmd"]
    assert cmd[0].endswith("accudisc")
    assert cmd[1:4] == ["--device", "/dev/sr0", "write"]
    # Raw s16le BIN — not a WAV, and no byte-swap (the pipeline is swap-free).
    assert cmd[cmd.index("--bin") + 1].endswith("disc.pcm")
    assert not any(a.endswith(".wav") for a in cmd)
    assert "--byteswap" not in cmd
    assert cmd[cmd.index("--speed") + 1] == "8"
    assert cmd[cmd.index("--progress-fd") + 1] == "1"
    assert "--simulate" not in cmd


def test_burn_simulate_appends_flag(tmp_path, monkeypatch):
    rbi = _tiny_rbi(tmp_path)
    cap = _patch_popen(monkeypatch, "summary result=ok\n", 0)
    disc_writer.burn_disc(rbi, device="/dev/sr0", simulate=True, yes=True)
    assert "--simulate" in cap["cmd"]


def test_burn_exit_3_reports_disc_not_blank(tmp_path, monkeypatch):
    rbi = _tiny_rbi(tmp_path)
    _patch_popen(monkeypatch, "", returncode=3)
    with pytest.raises(RuntimeError, match="not blank"):
        disc_writer.burn_disc(rbi, device="/dev/sr0", yes=True)


def test_burn_transport_error_raises(tmp_path, monkeypatch):
    rbi = _tiny_rbi(tmp_path)
    _patch_popen(monkeypatch, "", returncode=2)
    with pytest.raises(RuntimeError, match="exit 2"):
        disc_writer.burn_disc(rbi, device="/dev/sr0", yes=True)


# ── CATALOG sanitization ──────────────────────────────────────────────────────


def test_valid_13_digit_catalog_is_kept():
    toc = _toc(catalog="1234567890123")
    result = _sanitize_toc_for_burn(toc)
    assert 'CATALOG "1234567890123"' in result


def test_11_digit_catalog_is_stripped():
    toc = _toc(catalog="07599237742")
    result = _sanitize_toc_for_burn(toc)
    assert "CATALOG" not in result


def test_12_digit_catalog_is_stripped():
    toc = _toc(catalog="123456789012")
    result = _sanitize_toc_for_burn(toc)
    assert "CATALOG" not in result


def test_14_digit_catalog_is_stripped():
    toc = _toc(catalog="12345678901234")
    result = _sanitize_toc_for_burn(toc)
    assert "CATALOG" not in result


def test_non_digit_catalog_is_stripped():
    toc = _toc(catalog="ABCDEF1234567")
    result = _sanitize_toc_for_burn(toc)
    assert "CATALOG" not in result


def test_no_catalog_line_unchanged():
    toc = _toc(catalog=None)
    result = _sanitize_toc_for_burn(toc)
    assert "CATALOG" not in result
    assert "CD_DA" in result


# ── Empty CD-Text field sanitization ─────────────────────────────────────────


def test_empty_performer_is_stripped():
    toc = _toc(tracks=[("Sharp Dressed Man", "")])
    result = _sanitize_toc_for_burn(toc)
    assert 'PERFORMER ""' not in result
    assert "Sharp Dressed Man" in result


def test_empty_title_is_stripped():
    toc = _toc(tracks=[("", "ZZ Top")])
    result = _sanitize_toc_for_burn(toc)
    assert 'TITLE ""' not in result
    assert "ZZ Top" in result


def test_non_empty_fields_are_kept():
    toc = _toc(tracks=[("Gimme All Your Lovin", "ZZ Top")])
    result = _sanitize_toc_for_burn(toc)
    assert 'TITLE "Gimme All Your Lovin"' in result
    assert 'PERFORMER "ZZ Top"' in result


def test_disc_level_fields_are_not_affected_by_empty_regex():
    """Disc-level TITLE/PERFORMER at column 4 should not be touched."""
    toc = _toc(catalog=None)
    result = _sanitize_toc_for_burn(toc)
    assert 'TITLE "Test Album"' in result
    assert 'PERFORMER "Test Artist"' in result


def test_both_sanitizations_applied_together():
    toc = _toc(catalog="07599237742", tracks=[("Track 1", ""), ("Track 2", "Artist")])
    result = _sanitize_toc_for_burn(toc)
    assert "CATALOG" not in result
    assert 'PERFORMER ""' not in result
    assert 'PERFORMER "Artist"' in result
    assert "Track 1" in result
    assert "Track 2" in result


# ── Empty CD_TEXT shell stripping ─────────────────────────────────────────────


def test_track_cdtext_block_stripped_when_both_fields_empty():
    """A track-level CD_TEXT block with both TITLE "" and PERFORMER "" stripped."""
    toc = _toc(tracks=[("", "")])
    result = _sanitize_toc_for_burn(toc)
    # Disc-level block is preserved (has default "Test Album" / "Test Artist").
    assert 'TITLE "Test Album"' in result
    # Track-level block is entirely removed — only the disc-level CD_TEXT remains.
    assert result.count("CD_TEXT {") == 1


def test_track_cdtext_block_preserved_when_title_present():
    """Block kept when TITLE has content even if PERFORMER is empty."""
    toc = _toc(tracks=[("Sharp Dressed Man", "")])
    result = _sanitize_toc_for_burn(toc)
    assert 'TITLE "Sharp Dressed Man"' in result
    assert result.count("CD_TEXT {") == 2  # disc-level + track-level both present


def test_disc_level_empty_language_block_stripped():
    """Disc-level CD_TEXT with empty TITLE and PERFORMER stripped entirely."""
    toc = _toc(disc_title="", disc_performer="")
    result = _sanitize_toc_for_burn(toc)
    # The entire disc-level CD_TEXT block (including LANGUAGE_MAP shell) is gone.
    assert "LANGUAGE_MAP" not in result
    assert "CD_DA" in result


def test_disc_level_nonempty_preserved_when_tracks_empty():
    """Disc-level CD_TEXT with content is kept even when track blocks are stripped."""
    toc = _toc(disc_title="The Eliminator", disc_performer="ZZ Top", tracks=[("", "")])
    result = _sanitize_toc_for_burn(toc)
    assert 'TITLE "The Eliminator"' in result
    assert 'PERFORMER "ZZ Top"' in result
    # Disc-level block preserved (has LANGUAGE_MAP); track-level block is gone.
    assert "LANGUAGE_MAP" in result
    assert result.count("CD_TEXT {") == 1
