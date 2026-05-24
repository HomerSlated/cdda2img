"""
test_resolve_output_path.py — unit tests for _resolve_output_path() and the
parent-aware variant of _unique_path() in cdda2img.py.

Both helpers exist to keep `build_container` from being handed a directory
(which would blow up with IsADirectoryError deep in the write path).
"""

from __future__ import annotations

from pathlib import Path

from cdda2img.cdda2img import _resolve_output_path, _unique_path


def test_unique_path_with_no_parent_uses_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = _unique_path("album", "rbi")
    assert p == Path("album.rbi")


def test_unique_path_with_parent_writes_into_parent(tmp_path):
    p = _unique_path("album", "rbi", parent=tmp_path)
    assert p == tmp_path / "album.rbi"


def test_unique_path_appends_suffix_on_collision(tmp_path):
    (tmp_path / "album.rbi").touch()
    p = _unique_path("album", "rbi", parent=tmp_path)
    assert p == tmp_path / "album_1.rbi"


def test_resolve_output_none_derives_from_stem(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = _resolve_output_path(None, "Eliminator")
    assert p == Path("Eliminator.rbi")


def test_resolve_output_directory_uses_parent_and_derives_name(tmp_path):
    p = _resolve_output_path(tmp_path, "Eliminator")
    assert p == tmp_path / "Eliminator.rbi"


def test_resolve_output_explicit_file_honoured_verbatim(tmp_path):
    explicit = tmp_path / "mydisc.rbi"
    assert _resolve_output_path(explicit, "Eliminator") == explicit


def test_resolve_output_disc_suffix_applied_for_derived_names(tmp_path):
    p = _resolve_output_path(tmp_path, "Eliminator", disc_suffix="_disc2")
    assert p == tmp_path / "Eliminator_disc2.rbi"


def test_resolve_output_disc_suffix_inserted_into_explicit_filename(tmp_path):
    explicit = tmp_path / "mydisc.rbi"
    p = _resolve_output_path(explicit, "Eliminator", disc_suffix="_disc2")
    assert p == tmp_path / "mydisc_disc2.rbi"


def test_resolve_output_directory_avoids_collision(tmp_path):
    (tmp_path / "Eliminator.rbi").touch()
    p = _resolve_output_path(tmp_path, "Eliminator")
    assert p == tmp_path / "Eliminator_1.rbi"
