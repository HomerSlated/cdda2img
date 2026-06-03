"""Tests for tools/disc_scan.py — the redumper auto-rip seam.

disc_scan.py is a standalone tool under tools/ (not part of the installed
package), so it is imported by path. These tests cover the parts that must be
correct *without* a physical disc: the exact redumper argv, binary resolution,
arg-parsing precedence, and the dump -> dump::extra orchestration (with
subprocess mocked). The only untestable surface left is the real shell-out,
which a live rip exercises.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_TOOLS = str(Path(__file__).resolve().parent.parent / "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import disc_scan  # ty: ignore[unresolved-import]

# --- pure argv builders -----------------------------------------------------


def test_dump_argv_with_device():
    argv = disc_scan.redumper_dump_argv("/bin/redumper", "/img/x", "/dev/sr0")
    assert argv == [
        "/bin/redumper",
        "dump",
        "--image-path=/img/x",
        "--image-name=scan",
        "--retries=5",
        "--drive=/dev/sr0",
    ]


def test_dump_argv_without_device_omits_drive():
    argv = disc_scan.redumper_dump_argv("/bin/redumper", "/img/x", None)
    assert "--drive" not in " ".join(argv)
    assert argv[1] == "dump"


def test_extra_argv_matches_image_name_and_path():
    # dump::extra must address the SAME files the dump wrote (same name+path),
    # otherwise the lead-in augmentation lands on nothing.
    dump = disc_scan.redumper_dump_argv("/bin/redumper", "/img/x", "/dev/sr0")
    extra = disc_scan.redumper_extra_argv("/bin/redumper", "/img/x", "/dev/sr0")
    assert extra[1] == "dump::extra"
    assert "--image-name=scan" in dump and "--image-name=scan" in extra
    assert "--image-path=/img/x" in dump and "--image-path=/img/x" in extra


# --- binary resolution ------------------------------------------------------


def test_resolve_redumper_prefers_explicit():
    with patch.dict("os.environ", {"REDUMPER": "/env/redumper"}):
        assert disc_scan.resolve_redumper("/explicit/redumper") == "/explicit/redumper"


def test_resolve_redumper_env_over_path():
    with (
        patch.dict("os.environ", {"REDUMPER": "/env/redumper"}),
        patch("shutil.which", return_value="/path/redumper"),
    ):
        assert disc_scan.resolve_redumper(None) == "/env/redumper"


def test_resolve_redumper_falls_back_to_path():
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("shutil.which", return_value="/path/redumper"),
    ):
        assert disc_scan.resolve_redumper(None) == "/path/redumper"


def test_resolve_redumper_none_when_unfound():
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("shutil.which", return_value=None),
    ):
        assert disc_scan.resolve_redumper(None) is None


# --- arg-parsing precedence -------------------------------------------------


def test_parser_bare_deep_is_auto_sentinel():
    args = disc_scan._build_parser().parse_args(["--device", "/dev/sr0", "--deep"])
    assert args.deep == disc_scan._DEEP_AUTO


def test_parser_deep_with_path_keeps_path():
    args = disc_scan._build_parser().parse_args(["--deep", "cap.subcode"])
    assert args.deep == "cap.subcode"  # a path wins -> pre-captured mode, no rip


def test_parser_deep_absent_is_none():
    args = disc_scan._build_parser().parse_args(["--toc", "a.toc"])
    assert args.deep is None


def test_main_auto_deep_without_device_errors():
    # Bare --deep needs a drive to rip; argparse error -> SystemExit.
    with pytest.raises(SystemExit):
        disc_scan.main(["--deep"])


# --- run_redumper orchestration (subprocess mocked) -------------------------


class _Proc:
    def __init__(self, returncode: int):
        self.returncode = returncode


def test_run_redumper_runs_dump_then_extra_and_returns_subcode(tmp_path):
    calls: list[list[str]] = []

    def fake_run(argv, *a, **k):
        calls.append(argv)
        # Simulate redumper writing the subcode on the dump pass.
        if argv[1] == "dump":
            (tmp_path / "scan.subcode").write_bytes(b"\x00" * 96)
        return _Proc(0)

    with patch("subprocess.run", side_effect=fake_run):
        out = disc_scan.run_redumper("/bin/redumper", str(tmp_path), "/dev/sr0")

    assert out == tmp_path / "scan.subcode"
    assert [c[1] for c in calls] == ["dump", "dump::extra"]  # order is load-bearing


def test_run_redumper_raises_when_dump_fails(tmp_path):
    with (
        patch("subprocess.run", return_value=_Proc(1)),
        pytest.raises(SystemExit),
    ):
        disc_scan.run_redumper("/bin/redumper", str(tmp_path), "/dev/sr0")


def test_run_redumper_tolerates_extra_failure(tmp_path):
    # dump succeeds (writes subcode), dump::extra fails -> still returns subcode.
    def fake_run(argv, *a, **k):
        if argv[1] == "dump":
            (tmp_path / "scan.subcode").write_bytes(b"\x00" * 96)
            return _Proc(0)
        return _Proc(1)  # dump::extra unavailable (non-Plextor)

    with patch("subprocess.run", side_effect=fake_run):
        out = disc_scan.run_redumper("/bin/redumper", str(tmp_path), "/dev/sr0")
    assert out == tmp_path / "scan.subcode"


def test_run_redumper_raises_when_no_subcode(tmp_path):
    # Both passes "succeed" but no subcode was produced -> clear error, not a
    # downstream FileNotFoundError inside deep_scan.
    with (
        patch("subprocess.run", return_value=_Proc(0)),
        pytest.raises(SystemExit),
    ):
        disc_scan.run_redumper("/bin/redumper", str(tmp_path), "/dev/sr0")
