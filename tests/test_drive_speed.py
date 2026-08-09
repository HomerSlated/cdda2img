"""drive_speed: read-speed policy. Every drive command goes through the seam."""

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


# ── request_speed: the seam is the only way to the drive ─────────────────────


def test_no_raw_ioctl_survives_in_this_module() -> None:
    """The point of the 2026-08-09 change, pinned so it cannot quietly come back.

    Asserting on the module's *namespace* rather than its source text: a stub that
    re-imported ``fcntl`` inside a function would evade a text search, and a
    comment mentioning ``CDROM_SELECT_SPEED`` (this module has several, explaining
    the history) would trip one. What must not exist is a live handle to either.
    """
    assert not hasattr(ds, "fcntl"), "drive_speed must not import fcntl"
    assert not hasattr(ds, "os"), "drive_speed must not import os"
    assert not hasattr(ds, "_CDROM_SELECT_SPEED")
    assert not hasattr(ds, "_select_speed")


def test_request_speed_delegates_to_the_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    import cdda2img.accudisc_reader as ar

    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(ar, "set_speed", lambda d, n: (calls.append((d, n)), True)[1])
    assert ds.request_speed("/dev/sr0", 8) is True
    assert calls == [("/dev/sr0", 8)]


def test_request_speed_reports_refusal_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """False is the load-bearing return: a caller must be able to SAY it did not
    take. Swallowing to None would make refused indistinguishable from honoured."""
    import cdda2img.accudisc_reader as ar

    monkeypatch.setattr(ar, "set_speed", lambda d, n: False)
    assert ds.request_speed("/dev/sr0", 8) is False

    def boom(d: str, n: int) -> bool:
        raise RuntimeError

    monkeypatch.setattr(ar, "set_speed", boom)
    assert ds.request_speed("/dev/sr0", 8) is False


# ── current_speed_x ──────────────────────────────────────────────────────────


def test_current_speed_x_converts_kbps_to_multiplier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ds, "read_drive_speed", lambda d: (7056, 7056))
    assert ds.current_speed_x("/dev/sr0") == 40


def test_current_speed_x_is_none_when_the_drive_will_not_say(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """None, never 0 — restoring to a rate nobody measured is worse than not
    restoring, and the rip's finally keys on exactly this distinction."""
    monkeypatch.setattr(ds, "read_drive_speed", lambda d: (None, 7056))
    assert ds.current_speed_x("/dev/sr0") is None


def test_current_speed_x_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(d: str) -> tuple[int | None, int | None]:
        raise RuntimeError

    monkeypatch.setattr(ds, "read_drive_speed", boom)
    assert ds.current_speed_x("/dev/sr0") is None


# ── restore_drive_speed ──────────────────────────────────────────────────────


def _capture_requests(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record the Nx values handed to request_speed."""
    nx_calls: list[int] = []
    monkeypatch.setattr(
        ds, "request_speed", lambda dev, nx: (nx_calls.append(nx), True)[1]
    )
    return nx_calls


def test_restore_to_captured_target_puts_the_drive_back_as_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rip captures entry speed and hands it back; 40X-on-exit is NOT the rule
    any more, because cd-paranoia's persistent -S (the thing max-restore undid) is
    gone and the only process throttling this drive is now us."""
    monkeypatch.setattr(ds, "read_drive_speed", lambda d: (8 * 176, 7056))
    nx_calls = _capture_requests(monkeypatch)
    ds.restore_drive_speed("/dev/sr0", 24)
    assert nx_calls == [24]


def test_restore_to_target_is_a_noop_when_already_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ds, "read_drive_speed", lambda d: (24 * 176, 7056))
    nx_calls = _capture_requests(monkeypatch)
    ds.restore_drive_speed("/dev/sr0", 24)
    assert nx_calls == []


def test_restore_sets_exact_max_nx_when_throttled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No target captured (the ladder probe's case) → restore to max, as before."""
    monkeypatch.setattr(ds, "read_drive_speed", lambda d: (706, 7056))
    nx_calls = _capture_requests(monkeypatch)
    ds.restore_drive_speed("/dev/sr0")
    assert nx_calls == [40]  # 7056 // 176 = 40X, the exact CD-DA max


def test_restore_noop_when_already_at_max(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ds, "read_drive_speed", lambda d: (7056, 7056))
    nx_calls = _capture_requests(monkeypatch)
    ds.restore_drive_speed("/dev/sr0")
    assert nx_calls == []  # current == max → nothing sent


def test_restore_falls_back_to_zero_when_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ds, "read_drive_speed", lambda d: (None, None))
    nx_calls = _capture_requests(monkeypatch)
    ds.restore_drive_speed("/dev/sr0")
    assert nx_calls == [0]  # max unknown → 0 = "fastest" sentinel


def test_restore_swallows_a_refusing_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ds, "read_drive_speed", lambda d: (706, 7056))
    monkeypatch.setattr(ds, "request_speed", lambda dev, nx: False)
    ds.restore_drive_speed("/dev/sr0")  # must not raise
    ds.restore_drive_speed("/dev/sr0", 24)  # nor on the targeted path


# ── probe_speed_ladder is gone ───────────────────────────────────────────────


def test_the_legacy_probe_is_retired() -> None:
    """Deleted 2026-08-09: no caller in src/, and admitted_ladder supersedes it.

    Pinned because the replacement is not strictly better on every axis — that
    sweep was the only set-then-read-back check in the tree, and deleting it
    without noticing is how the verification stopped happening the first time.
    ``_rip_disc_stage`` now carries the read-back explicitly.
    """
    assert not hasattr(ds, "probe_speed_ladder")
    assert not hasattr(ds, "_SPEED_PROBE")
