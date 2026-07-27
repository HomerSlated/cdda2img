"""drive_speed: read current/max via ``accudisc speed``, restore to max via ioctl."""

from __future__ import annotations

import pytest

import cdda2img.drive_speed as ds

# ── read_drive_speed ─────────────────────────────────────────────────────────
#
# The page-2A parsing moved to accudisc_reader.read_speed (the AccuDisc seam), so
# it is tested there. What remains here is that this module still *asks* — a
# delegation that silently stopped delegating would look identical to a drive
# that reports nothing.


def test_read_drive_speed_delegates_to_the_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cdda2img.accudisc_reader as ar

    seen: list[str] = []

    def _read_speed(device: str) -> tuple[int | None, int | None]:
        seen.append(device)
        return 1411, 7056

    monkeypatch.setattr(ar, "read_speed", _read_speed)
    assert ds.read_drive_speed("/dev/sr0") == (1411, 7056)
    assert seen == ["/dev/sr0"]


def test_read_drive_speed_passes_through_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(None, None) must survive the hop — restore_drive_speed keys on it."""
    import cdda2img.accudisc_reader as ar

    monkeypatch.setattr(ar, "read_speed", lambda d: (None, None))
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
