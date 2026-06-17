"""Tests for cdda2img.metadata.derive_album_info."""

from __future__ import annotations

from pathlib import Path

import pytest

from cdda2img.metadata import derive_album_info


def test_album_fallback_uses_parent_dir_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # BUG-2 regression: when no readable album tag is found, the fallback must be
    # the audio files' parent directory name, not the process CWD. File() is
    # stubbed to None (no tags) so the test does not depend on a real audio file.
    monkeypatch.setattr("cdda2img.metadata.File", lambda *a, **k: None)
    track = tmp_path / "Greatest Hits" / "01.flac"

    info = derive_album_info([track], autoaccept=True)

    assert info["album"] == "Greatest Hits"
