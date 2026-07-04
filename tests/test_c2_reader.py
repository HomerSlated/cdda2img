"""c2_reader: c2read subprocess wrappers — args, combos, progress streaming."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

import cdda2img.c2_reader as c2r

_FEATURES_OUT = """\
cd_read_feature present current=1 dap=0 c2_flags=1 cd_text=1
c2_read_smoke ok
combo c2 ok
combo sub_raw ok
combo sub_q ok
combo c2+sub_raw ok
combo c2+sub_q failed
verdict C2_SUPPORTED
"""


class _Result:
    def __init__(
        self, stdout: str | bytes = "", stderr: bytes = b"", returncode: int = 0
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


# ── drive_supports_c2 / probe_combos ─────────────────────────────────────────


def test_drive_supports_c2_gates_on_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        c2r.subprocess, "run", lambda *a, **k: _Result(stdout=_FEATURES_OUT)
    )
    assert c2r.drive_supports_c2("/dev/sr0") is True
    monkeypatch.setattr(
        c2r.subprocess,
        "run",
        lambda *a, **k: _Result(stdout=_FEATURES_OUT, returncode=1),
    )
    assert c2r.drive_supports_c2("/dev/sr0") is False


def test_drive_supports_c2_missing_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*a: object, **k: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(c2r.subprocess, "run", _raise)
    assert c2r.drive_supports_c2("/dev/sr0") is False


def test_probe_combos_parses_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        c2r.subprocess, "run", lambda *a, **k: _Result(stdout=_FEATURES_OUT)
    )
    combos = c2r.probe_combos("/dev/sr0")
    assert combos == {
        "c2": True,
        "sub_raw": True,
        "sub_q": True,
        "c2+sub_raw": True,
        "c2+sub_q": False,
    }


def test_probe_combos_missing_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*a: object, **k: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(c2r.subprocess, "run", _raise)
    assert c2r.probe_combos("/dev/sr0") == {}


# ── read_disc_c2 command construction ────────────────────────────────────────


def _capture_cmd(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def _run(cmd: list[str], **k: object) -> _Result:
        calls.append(cmd)
        return _Result(stdout=b"", returncode=0)

    monkeypatch.setattr(c2r.subprocess, "run", _run)
    return calls


def test_read_disc_c2_default_args(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_cmd(monkeypatch)
    c2r.read_disc_c2("/dev/sr0", Path("a.pcm"), Path("a.c2"))
    (cmd,) = calls
    assert cmd[:4] == ["c2read", "--device", "/dev/sr0", "--full"]
    assert "--sub" not in cmd
    assert "--speed" not in cmd


def test_read_disc_c2_sub_and_speed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_cmd(monkeypatch)
    c2r.read_disc_c2(
        "/dev/sr0", Path("a.pcm"), Path("a.c2"), output_sub=Path("a.sub"), read_speed=8
    )
    (cmd,) = calls
    i = cmd.index("--sub")
    assert cmd[i : i + 4] == ["--sub", "raw", "--subf", "a.sub"]
    assert cmd[cmd.index("--speed") + 1] == "8"


def test_read_disc_c2_exit_3_is_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        c2r.subprocess, "run", lambda *a, **k: _Result(stdout=b"", returncode=3)
    )
    c2r.read_disc_c2("/dev/sr0", Path("a.pcm"), Path("a.c2"))  # no raise


def test_read_disc_c2_exit_1_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        c2r.subprocess,
        "run",
        lambda *a, **k: _Result(stdout=b"", stderr=b"boom", returncode=1),
    )
    with pytest.raises(RuntimeError, match=r"exit 1.*boom"):
        c2r.read_disc_c2("/dev/sr0", Path("a.pcm"), Path("a.c2"))


# ── progress streaming ───────────────────────────────────────────────────────


class _FakeProc:
    def __init__(self, lines: str, returncode: int = 0) -> None:
        self.stdout = io.StringIO(lines)
        self.returncode = returncode

    def wait(self) -> int:
        return self.returncode


def test_read_disc_c2_streams_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = "progress 23 100\nnoise line\nprogress 100 100\n"
    monkeypatch.setattr(
        c2r.subprocess, "Popen", lambda *a, **k: _FakeProc(lines, returncode=3)
    )
    seen: list[tuple[int, int]] = []
    c2r.read_disc_c2(
        "/dev/sr0",
        Path("a.pcm"),
        Path("a.c2"),
        progress_cb=lambda d, t: seen.append((d, t)),
    )
    assert seen == [(23, 100), (100, 100)]


def test_read_disc_c2_progress_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        c2r.subprocess, "Popen", lambda *a, **k: _FakeProc("", returncode=1)
    )
    with pytest.raises(RuntimeError, match="exit 1"):
        c2r.read_disc_c2(
            "/dev/sr0", Path("a.pcm"), Path("a.c2"), progress_cb=lambda d, t: None
        )
