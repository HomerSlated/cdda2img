"""drive_speed: read current/max via cdrdao drive-info, restore to max via ioctl."""

from __future__ import annotations

import pytest

import cdda2img.disc_reader as dr
import cdda2img.drive_speed as ds

# Real `cdrdao drive-info --device /dev/sr0` output (PLEXTOR PX-716A): current 706 kB/s
# (4X), max 7056 kB/s (40X) — the post -S 1 throttled state.
_DRIVE_INFO = """\
/dev/sr0: PLEXTOR DVDR   PX-716A\tRev: 1.11
CD-TEXT writing is supported.
Using driver: Generic SCSI-3/MMC - Version 2.0 (options 0x0010)

Maximum reading speed: 7056 kB/s
Current reading speed: 706 kB/s
Maximum writing speed: 8467 kB/s
Current writing speed: 8467 kB/s
BurnProof supported: yes
"""


class _Result:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


# ── read_drive_speed ─────────────────────────────────────────────────────────


def test_read_drive_speed_parses_current_and_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ds.subprocess, "run", lambda *a, **k: _Result(stdout=_DRIVE_INFO)
    )
    assert ds.read_drive_speed("/dev/sr0") == (706, 7056)


def test_read_drive_speed_scans_stderr_too(monkeypatch: pytest.MonkeyPatch) -> None:
    # Some cdrdao builds print drive-info to stderr; the reader must scan both streams.
    monkeypatch.setattr(
        ds.subprocess, "run", lambda *a, **k: _Result(stderr=_DRIVE_INFO)
    )
    assert ds.read_drive_speed("/dev/sr0") == (706, 7056)


def test_read_drive_speed_missing_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ds.subprocess, "run", lambda *a, **k: _Result(stdout="garbage"))
    assert ds.read_drive_speed("/dev/sr0") == (None, None)


def test_read_drive_speed_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ds.subprocess, "run", lambda *a, **k: _Result(stdout=_DRIVE_INFO, returncode=1)
    )
    assert ds.read_drive_speed("/dev/sr0") == (None, None)


def test_read_drive_speed_cdrdao_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(ds.subprocess, "run", boom)
    assert ds.read_drive_speed("/dev/sr0") == (None, None)


# ── restore_drive_speed ──────────────────────────────────────────────────────


def _capture_select(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Stub os.open/close + fcntl.ioctl; return the list of Nx args passed to ioctl."""
    nx_calls: list[int] = []
    monkeypatch.setattr(ds.os, "open", lambda *a, **k: 7)
    monkeypatch.setattr(ds.os, "close", lambda fd: None)
    monkeypatch.setattr(ds.fcntl, "ioctl", lambda fd, op, arg: nx_calls.append(arg))
    return nx_calls


def test_restore_sets_exact_max_nx_when_throttled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ds, "read_drive_speed", lambda d: (706, 7056))
    nx_calls = _capture_select(monkeypatch)
    ds.restore_drive_speed("/dev/sr0")
    assert nx_calls == [40]  # 7056 // 176 = 40X, the exact CD-DA max


def test_restore_noop_when_already_at_max(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ds, "read_drive_speed", lambda d: (7056, 7056))
    nx_calls = _capture_select(monkeypatch)
    ds.restore_drive_speed("/dev/sr0")
    assert nx_calls == []  # current == max → no ioctl


def test_restore_falls_back_to_zero_when_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ds, "read_drive_speed", lambda d: (None, None))
    nx_calls = _capture_select(monkeypatch)
    ds.restore_drive_speed("/dev/sr0")
    assert nx_calls == [0]  # max unknown → 0 = "fastest" sentinel


def test_restore_swallows_ioctl_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ds, "read_drive_speed", lambda d: (706, 7056))
    monkeypatch.setattr(ds.os, "open", lambda *a, **k: 7)
    monkeypatch.setattr(ds.os, "close", lambda fd: None)

    def boom(fd, op, arg):
        raise OSError("EINVAL")

    monkeypatch.setattr(ds.fcntl, "ioctl", boom)
    ds.restore_drive_speed("/dev/sr0")  # must not raise


def test_restore_swallows_open_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ds, "read_drive_speed", lambda d: (706, 7056))

    def boom(*a, **k):
        raise OSError("ENOENT")

    monkeypatch.setattr(ds.os, "open", boom)
    ds.restore_drive_speed("/dev/sr0")  # must not raise


# ── disc_reader finally hook ─────────────────────────────────────────────────


def _stub_single_track(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub the rip_single_track innards; return the list of restored devices."""
    restored: list[str] = []
    import cdda2img.container as container

    monkeypatch.setattr(dr, "query_disc", lambda d: (0, 999, [(1, 0, 1000)]))
    monkeypatch.setattr(
        dr.subprocess, "run", lambda *a, **k: type("P", (), {"returncode": 0})()
    )
    monkeypatch.setattr(container, "wav_to_raw_pcm", lambda *a, **k: None)
    monkeypatch.setattr(ds, "restore_drive_speed", lambda dev: restored.append(dev))
    return restored


def test_rip_single_track_restores_speed_when_slowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    restored = _stub_single_track(monkeypatch)
    dr.rip_single_track("/dev/sr0", 1, tmp_path / "o.pcm", read_speed=1)
    assert restored == ["/dev/sr0"]


def test_rip_single_track_no_restore_at_default_speed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    restored = _stub_single_track(monkeypatch)
    dr.rip_single_track("/dev/sr0", 1, tmp_path / "o.pcm")  # read_speed=None
    assert restored == []


# ── probe_speed_ladder ───────────────────────────────────────────────────────


def test_probe_speed_ladder_builds_sorted_unique(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Model a drive with discrete rungs [4,8,16,24,32,40]X: each set request snaps to the
    # nearest rung, read back via drive-info.
    rungs_kbps = [x * 176 for x in (4, 8, 16, 24, 32, 40)]
    holder: dict[str, int] = {}
    restored: list[str] = []

    def fake_select(dev: str, n: int) -> bool:
        holder["n"] = n
        return True

    def fake_read(dev: str):
        target = holder["n"] * 176
        snapped = min(rungs_kbps, key=lambda k: abs(k - target))
        return snapped, 40 * 176

    monkeypatch.setattr(ds, "_select_speed", fake_select)
    monkeypatch.setattr(ds, "read_drive_speed", fake_read)
    monkeypatch.setattr(ds, "restore_drive_speed", lambda dev: restored.append(dev))

    ladder = ds.probe_speed_ladder("/dev/sr0")
    assert ladder == [4, 8, 16, 24, 32, 40]
    assert restored == ["/dev/sr0"]  # restored to max after probing


def test_probe_speed_ladder_skips_unsettable_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ds, "_select_speed", lambda dev, n: n in (8, 40))
    monkeypatch.setattr(ds, "read_drive_speed", lambda dev: (40 * 176, 40 * 176))
    monkeypatch.setattr(ds, "restore_drive_speed", lambda dev: None)
    # only n in {8,40} set successfully; both read back 40X here → ladder = [40]
    assert ds.probe_speed_ladder("/dev/sr0") == [40]
