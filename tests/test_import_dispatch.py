"""Tests for ``cdda2img._import_source`` — the foreign-image format dispatch.

These cover the *wiring*, not the readers: that each accepted source reaches its
own reader, and that the three things the dispatch itself decides come back
right — the ``ripper`` provenance tag, the output stem, and the resolved source
path.

Written when the five branches were lifted out of ``import_image`` (adding
PlexTools tipped it over C901).  Nothing in the suite exercised that dispatch
before, so a refactor touching all five formats had no coverage at all; the only
check was a source-text grep for a literal, which answers "is the string
present" rather than "does dispatch reach the reader" — the narrower-question
bug shape this project keeps meeting.

The output stem is the subtle one: DDP is a *directory* and uses ``.name``,
every file format uses ``.stem``.  Assert it per format rather than trusting the
asymmetry to survive an edit.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from cdda2img import cdda2img as app
from cdda2img.container import TempFiles
from cdda2img.rbi_format import FLAG_MASTER_MODE, RBIDisc


@pytest.fixture
def temp(tmp_path: Path) -> Iterator[TempFiles]:
    """A real ``TempFiles``, not a stand-in.

    The dispatch only reads ``.pcm_file`` and ``.pcm_pre``, so a stub would pass
    — and would keep passing if those names moved.
    """
    files = TempFiles(tmp_path)
    yield files
    files.cleanup()


def _disc(album: str = "Some Album") -> RBIDisc:
    return RBIDisc(album=album, artist="Some Artist")


# ---------------------------------------------------------------------------
# The three formats that are one call to one reader
# ---------------------------------------------------------------------------

# (suffix, reader module, reader attribute, expected ripper tag)
SINGLE_CALL = [
    (".nrg", "cdda2img.nrg_reader", "import_nrg", "nrg"),
    (".ccd", "cdda2img.ccd_reader", "import_ccd", "ccd"),
    (".pxi", "cdda2img.pxi_reader", "import_pxi", "pxi"),
]


@pytest.mark.parametrize(("suffix", "module", "attr", "ripper"), SINGLE_CALL)
def test_each_file_format_reaches_its_own_reader(
    tmp_path: Path,
    temp: TempFiles,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    module: str,
    attr: str,
    ripper: str,
) -> None:
    source = tmp_path / f"album{suffix}"
    source.write_bytes(b"")
    seen: dict[str, object] = {}

    def _fake(path: Path, pcm_out: Path, *args: object) -> tuple[RBIDisc, int]:
        seen["path"] = path
        seen["pcm_out"] = pcm_out
        return _disc(), FLAG_MASTER_MODE

    monkeypatch.setattr(f"{module}.{attr}", _fake)

    disc, stem, prov = app._import_source(source, temp, None)

    assert seen == {"path": source, "pcm_out": temp.pcm_file}
    assert prov["ripper"] == ripper
    assert prov["mode"] == "import"
    assert prov["source"] == str(source.resolve())
    assert stem == "Some Album"
    assert disc.album == "Some Album"


@pytest.mark.parametrize(("suffix", "module", "attr", "ripper"), SINGLE_CALL)
def test_an_untitled_disc_falls_back_to_the_file_stem(
    tmp_path: Path,
    temp: TempFiles,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    module: str,
    attr: str,
    ripper: str,
) -> None:
    """Not ``.name`` — the suffix must not end up in the output filename."""
    source = tmp_path / f"myalbum{suffix}"
    source.write_bytes(b"")
    monkeypatch.setattr(
        f"{module}.{attr}", lambda *a, **k: (_disc(album=""), FLAG_MASTER_MODE)
    )

    _disc_out, stem, _prov = app._import_source(source, temp, None)
    assert stem == "myalbum"


def test_the_suffix_match_is_case_insensitive(
    tmp_path: Path, temp: TempFiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "ALBUM.PXI"
    source.write_bytes(b"")
    monkeypatch.setattr(
        "cdda2img.pxi_reader.import_pxi", lambda *a, **k: (_disc(), FLAG_MASTER_MODE)
    )

    _d, _stem, prov = app._import_source(source, temp, None)
    assert prov["ripper"] == "pxi"


def test_pxi_is_handed_the_provenance_dict_it_records_padding_into(
    tmp_path: Path, temp: TempFiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PXI reader writes ``pxi_tail_padded`` into the caller's dict.

    Passing it a throwaway would drop the record of fabricated samples silently.
    """
    source = tmp_path / "album.pxi"
    source.write_bytes(b"")

    def _fake(path: Path, pcm_out: Path, prov: dict[str, str] | None = None):
        assert prov is not None
        prov["pxi_tail_padded"] = "120"
        return _disc(), FLAG_MASTER_MODE

    monkeypatch.setattr("cdda2img.pxi_reader.import_pxi", _fake)

    _d, _stem, prov = app._import_source(source, temp, None)
    assert prov["pxi_tail_padded"] == "120"


# ---------------------------------------------------------------------------
# DDP — a directory, and the one format keyed on that rather than a suffix
# ---------------------------------------------------------------------------


def test_a_directory_is_dispatched_to_the_ddp_reader(
    tmp_path: Path, temp: TempFiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "DDP_Something"
    source.mkdir()
    seen: dict[str, object] = {}

    def _fake(path: Path, pcm_out: Path) -> tuple[RBIDisc, int]:
        seen["path"] = path
        return _disc(), FLAG_MASTER_MODE

    monkeypatch.setattr("cdda2img.ddp_reader.import_ddp", _fake)

    _d, stem, prov = app._import_source(source, temp, None)
    assert seen["path"] == source
    assert prov["ripper"] == "ddp"
    assert stem == "Some Album"


def test_an_untitled_ddp_directory_uses_its_name_not_its_stem(
    tmp_path: Path, temp: TempFiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory called ``Disc 1.2`` must not lose the ``.2``."""
    source = tmp_path / "Disc 1.2"
    source.mkdir()
    monkeypatch.setattr(
        "cdda2img.ddp_reader.import_ddp",
        lambda *a: (_disc(album=""), FLAG_MASTER_MODE),
    )

    _d, stem, _prov = app._import_source(source, temp, None)
    assert stem == "Disc 1.2"


# ---------------------------------------------------------------------------
# cdrdao TOC+BIN — the one branch with a companion file and a conversion step
# ---------------------------------------------------------------------------


def _stub_toc_branch(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    monkeypatch.setattr(
        "cdda2img.cdrdao_reader._find_bin_filename", lambda _text: "album.bin"
    )
    monkeypatch.setattr("cdda2img.toc_parser.parse_toc", lambda _raw: object())
    monkeypatch.setattr(
        "cdda2img.cdrdao_reader.parsed_to_rbi_disc", lambda _parsed: _disc()
    )
    monkeypatch.setattr(
        "cdda2img.cdrdao_reader.convert_cdrdao_bin_to_wav",
        lambda src, dst: calls.append(f"convert:{src.name}->{dst.name}"),
    )
    monkeypatch.setattr(
        "cdda2img.cdda2img.wav_to_raw_pcm",
        lambda src, dst: calls.append(f"strip:{src.name}->{dst.name}"),
    )


def test_a_toc_is_converted_through_the_byteswap_and_then_stripped(
    tmp_path: Path, temp: TempFiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cdrdao BIN is s16be, so this branch alone runs a conversion step.

    Order matters: byte-swap into a WAV, then strip the header to raw PCM.
    """
    source = tmp_path / "album.toc"
    source.write_text('FILE "album.bin" 0\n')
    (tmp_path / "album.bin").write_bytes(b"")
    calls: list[str] = []
    _stub_toc_branch(monkeypatch, calls)

    _d, stem, prov = app._import_source(source, temp, None)

    assert calls == [
        f"convert:album.bin->{temp.pcm_pre.name}",
        f"strip:{temp.pcm_pre.name}->{temp.pcm_file.name}",
    ]
    assert prov["ripper"] == "toc"
    assert stem == "Some Album"


def test_a_toc_whose_bin_is_missing_fails_before_any_conversion(
    tmp_path: Path, temp: TempFiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "album.toc"
    source.write_text('FILE "album.bin" 0\n')  # no album.bin beside it
    calls: list[str] = []
    _stub_toc_branch(monkeypatch, calls)

    with pytest.raises(FileNotFoundError, match="BIN file not found"):
        app._import_source(source, temp, None)
    assert calls == []


# ---------------------------------------------------------------------------
# Rejection
# ---------------------------------------------------------------------------


def test_an_unsupported_suffix_is_refused_and_names_every_accepted_source(
    tmp_path: Path, temp: TempFiles
) -> None:
    source = tmp_path / "album.iso"
    source.write_bytes(b"")

    with pytest.raises(ValueError) as excinfo:
        app._import_source(source, temp, None)

    message = str(excinfo.value)
    for expected in (".toc", "DDP", ".nrg", ".ccd", ".pxi"):
        assert expected in message


def test_both_entry_points_share_one_list_of_accepted_sources() -> None:
    """``info_image`` and ``import_image`` must never disagree about what is accepted.

    They did: for two months after CloneCD shipped, the argparse help and the
    error message still listed four formats.  One function now owns the wording.
    """
    import inspect

    for func in (app.info_image, app._import_source):
        src = inspect.getsource(func)
        assert "_unsupported_source_msg" in src
