"""accudisc_reader: AccuDisc subprocess wrappers — args, combos, progress streaming.

Asserts the AccuDisc machine interface: subcommand form, ``--c2f`` (not ``--c2``),
whole-disc read via no ``--count``, inline ``--cdtext`` / ``--fulltoc`` lead-in
capture, ``--progress-fd 1`` machine tokens on stdout, and the exit contract
(0/3 = completed, 1/2 = fatal).
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

import cdda2img.accudisc_reader as ar

_ACC = ar._ACCUDISC

_FEATURES_OUT = """\
cd_read_feature present current=1 dap=0 c2_flags=1 cd_text=1
combo c2 ok
combo sub_raw ok
combo sub_q ok
combo c2+sub_raw ok
combo c2+sub_q failed
verdict C2_SUPPORTED
accurate_stream yes
"""


class _Result:
    def __init__(
        self, stdout: str | bytes = "", stderr: bytes = b"", returncode: int = 0
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _patch_run(monkeypatch, result_for=None):
    """Record every subprocess.run argv; return a per-command _Result."""
    calls: list[list[str]] = []
    make = result_for or (lambda cmd: _Result(returncode=0))

    def _run(cmd, **k):
        calls.append(cmd)
        return make(cmd)

    monkeypatch.setattr(ar.subprocess, "run", _run)
    return calls


# ── drive_supports_c2 / probe_combos ─────────────────────────────────────────


def test_drive_supports_c2_gates_on_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ar.subprocess, "run", lambda *a, **k: _Result(stdout=_FEATURES_OUT)
    )
    assert ar.drive_supports_c2("/dev/sr0") is True
    monkeypatch.setattr(
        ar.subprocess,
        "run",
        lambda *a, **k: _Result(stdout=_FEATURES_OUT, returncode=1),
    )
    assert ar.drive_supports_c2("/dev/sr0") is False


def test_drive_supports_c2_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*a: object, **k: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(ar.subprocess, "run", _raise)
    assert ar.drive_supports_c2("/dev/sr0") is False


def test_probe_combos_parses_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ar.subprocess, "run", lambda *a, **k: _Result(stdout=_FEATURES_OUT)
    )
    combos = ar.probe_combos("/dev/sr0")
    assert combos == {
        "c2": True,
        "sub_raw": True,
        "sub_q": True,
        "c2+sub_raw": True,
        "c2+sub_q": False,
    }


# ── read_disc_c2 command construction ────────────────────────────────────────


def test_read_disc_c2_default_args(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_run(monkeypatch)
    ar.read_disc_c2("/dev/sr0", Path("a.pcm"), Path("a.c2"))
    (cmd,) = calls  # no cdtext/fulltoc requested → only the read
    assert cmd[:4] == [_ACC, "--device", "/dev/sr0", "read"]
    assert cmd[cmd.index("--c2f") + 1] == "a.c2"
    assert cmd[cmd.index("--pcm") + 1] == "a.pcm"
    assert "--full" not in cmd  # whole-disc read is no --count, not --full
    assert "--count" not in cmd
    assert "--c2" not in cmd  # bitmap flag is --c2f
    assert cmd[-1] == "-q"  # no progress_cb → quiet


def test_read_disc_c2_sub_and_speed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_run(monkeypatch)
    ar.read_disc_c2(
        "/dev/sr0", Path("a.pcm"), Path("a.c2"), output_sub=Path("a.sub"), read_speed=8
    )
    (cmd,) = calls
    i = cmd.index("--sub")
    assert cmd[i : i + 4] == ["--sub", "raw", "--subf", "a.sub"]
    assert cmd[cmd.index("--speed") + 1] == "8"


def test_read_disc_c2_inline_leadin(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_run(monkeypatch)
    ar.read_disc_c2(
        "/dev/sr0",
        Path("a.pcm"),
        Path("a.c2"),
        output_cdtext=Path("a.cdtext"),
        output_fulltoc=Path("a.fulltoc"),
    )
    # Single read pass; lead-in dumps captured inline (one spin-up).
    (cmd,) = calls
    assert cmd[3] == "read"
    assert cmd[cmd.index("--fulltoc") + 1] == "a.fulltoc"
    assert cmd[cmd.index("--cdtext") + 1] == "a.cdtext"


def test_read_disc_c2_exit_3_is_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Exit 3 = completed with caveats (hard/suspect/residual C2) — not a failure.
    monkeypatch.setattr(ar.subprocess, "run", lambda *a, **k: _Result(returncode=3))
    ar.read_disc_c2("/dev/sr0", Path("a.pcm"), Path("a.c2"))  # no raise


def test_read_disc_c2_exit_1_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ar.subprocess,
        "run",
        lambda *a, **k: _Result(stderr=b"boom", returncode=1),
    )
    with pytest.raises(RuntimeError, match=r"exit 1.*boom"):
        ar.read_disc_c2("/dev/sr0", Path("a.pcm"), Path("a.c2"))


# ── read_span (targeted recovery re-read) ────────────────────────────────────


def test_read_span_command_and_speed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_run(monkeypatch)
    ar.read_span("/dev/sr0", 111142, 9481, Path("w.pcm"), read_speed=8)
    (cmd,) = calls
    assert cmd[:4] == [_ACC, "--device", "/dev/sr0", "read"]
    assert cmd[cmd.index("--start") + 1] == "111142"
    assert cmd[cmd.index("--count") + 1] == "9481"
    assert cmd[cmd.index("--speed") + 1] == "8"
    assert cmd[cmd.index("--pcm") + 1] == "w.pcm"
    assert "--c2f" not in cmd  # span read captures PCM only


def test_read_span_no_speed_flag_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_run(monkeypatch)
    ar.read_span("/dev/sr0", 0, 10, Path("w.pcm"))
    (cmd,) = calls
    assert "--speed" not in cmd


def test_read_span_exit_1_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ar.subprocess,
        "run",
        lambda *a, **k: _Result(stderr=b"boom", returncode=1),
    )
    with pytest.raises(RuntimeError, match=r"exit 1.*boom"):
        ar.read_span("/dev/sr0", 0, 10, Path("w.pcm"))


def test_read_span_exit_3_is_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ar.subprocess, "run", lambda *a, **k: _Result(returncode=3))
    ar.read_span("/dev/sr0", 0, 10, Path("w.pcm"))  # no raise


# ── progress streaming (--progress-fd 1 machine tokens on stdout) ─────────────


class _FakeProc:
    """Popen stand-in: stdout yields the --progress-fd machine lines; returncode set.

    stderr is written to a real TemporaryFile by _run_with_progress (which the
    mock ignores), so only stdout + wait()/returncode need faking here.
    """

    def __init__(self, stdout_lines: str, returncode: int = 0) -> None:
        self.stdout = io.StringIO(stdout_lines)
        self.returncode = returncode

    def wait(self) -> int:
        return self.returncode


def _patch_popen(monkeypatch, stdout_lines: str, returncode: int = 0) -> dict:
    captured: dict = {}

    def _popen(cmd, **k):
        captured["cmd"] = cmd
        return _FakeProc(stdout_lines, returncode)

    monkeypatch.setattr(ar.subprocess, "Popen", _popen)
    return captured


def test_read_disc_c2_streams_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    # --progress-fd 1 machine tokens on stdout, then the summary line.
    stdout = (
        "progress 23 100\n"
        "progress 100 100\n"
        "summary hard=0 c2=0 recovered=0 suspect=0 rereads=0 slips=0\n"
    )
    captured = _patch_popen(monkeypatch, stdout, returncode=0)
    seen: list[tuple[int, int]] = []
    ar.read_disc_c2(
        "/dev/sr0",
        Path("a.pcm"),
        Path("a.c2"),
        progress_cb=lambda d, t: seen.append((d, t)),
    )
    assert seen == [(23, 100), (100, 100)]
    # The machine channel is requested on fd 1, with the human line muted.
    cmd = captured["cmd"]
    assert cmd[cmd.index("--progress-fd") + 1] == "1"
    assert "-q" in cmd


def test_read_disc_c2_progress_exit_3_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    # Exit 3 via the progress path is completed-with-caveats, not a failure.
    _patch_popen(monkeypatch, "progress 100 100\n", returncode=3)
    ar.read_disc_c2(
        "/dev/sr0", Path("a.pcm"), Path("a.c2"), progress_cb=lambda d, t: None
    )  # no raise


def test_read_disc_c2_progress_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_popen(monkeypatch, "", returncode=1)
    with pytest.raises(RuntimeError, match="exit 1"):
        ar.read_disc_c2(
            "/dev/sr0", Path("a.pcm"), Path("a.c2"), progress_cb=lambda d, t: None
        )


# ── binary resolution ────────────────────────────────────────────────────────


def test_resolve_accudisc_prefers_snapshot() -> None:
    # The snapshot exists in this checkout, so resolution must point at it.
    resolved = ar._resolve_accudisc()
    assert resolved.endswith("tools/accudisc/accudisc")
    assert Path(resolved).is_file()
