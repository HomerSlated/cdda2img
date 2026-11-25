"""Every recovery read is filled by AccuDisc's read policy (kgr, 2026-11-25: "I
would rather do my tests against 0.59.0 with all new features enabled").

Inside a rip's recovery (``accudisc_reader.recovery_policy``) the policy owns
every field that decides what a read does; the seam adds the sink, maps and the
cancel flag, overrides ``shift_search`` on for ``SPAN_RESCUE``, hands the engine
what is left of the rip's budget, and records what the policy chose.
"""

from __future__ import annotations

import enum
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cdda2img import accudisc_reader as ar
from cdda2img import drive_work


class _C2(enum.IntEnum):
    NONE = 0
    PTRS = 1


class _Purpose(enum.IntEnum):
    CAPTURE = 1
    GATED_REREAD = 2
    SPAN_RESCUE = 3
    REFERENCE_VERIFY = 4


class _Reason(enum.IntFlag):
    NO_C2 = 1
    DEFEAT_NO_TARGET = 2
    SHIFT_SEARCH_PENDING_AB = 4


class _Defeat(enum.IntEnum):
    OFF = 0
    DONE = 1


class _Agree(enum.IntEnum):
    NOT_COMPARED = 0
    AGREE = 1
    AGREE_C2 = 2
    DIFFER = 3


class _Invalid(Exception):
    pass


def _module(*, defeat: bool = True, reasons: int = 0, refuse: bool = False):
    """A binding whose policy echoes its inputs and whose Device records reads."""
    reads: list[dict[str, Any]] = []
    policies: list[dict[str, Any]] = []

    def read_policy(purpose, lba, count, **kw):
        policies.append({"purpose": purpose, "lba": lba, "count": count, **kw})
        if refuse:
            msg = "no admitted rung left"
            raise _Invalid(msg)
        c2 = _C2.PTRS if kw["c2_usable"] else _C2.NONE
        out = {
            "lba": lba,
            "count": count,
            "c2": c2,
            "speed_x": kw["speed_x"] or 24,
            "cache_defeat": defeat,
            "verify_passes": 2 if purpose == _Purpose.SPAN_RESCUE else 0,
            "shift_search": False,
            "budget_cmds": kw.get("budget_cmds", 0),
            "budget_speed_changes": kw.get("budget_speed_changes", 0),
        }
        return SimpleNamespace(
            revision=2, reasons=_Reason(reasons), read_kwargs=lambda: dict(out)
        )

    class _Device:
        def __init__(self, _path: str) -> None:
            pass

        def __enter__(self) -> _Device:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self, *args: Any, **kw: Any) -> SimpleNamespace:
            if args:  # the pre-policy path passes lba and count by position
                kw = {"lba": args[0], "count": args[1], **kw}
            reads.append(kw)
            return SimpleNamespace(
                stats=SimpleNamespace(cache_defeat=_Defeat.DONE, speed_honoured_x=0)
            )

        def compare_read(self, *, reference, **kw: Any) -> SimpleNamespace:
            reads.append(kw)
            audio, absent = reference(kw["lba"], kw["count"])
            states = bytes([_Agree.AGREE] * 3 + [_Agree.DIFFER] + [_Agree.AGREE])
            return SimpleNamespace(
                stats=SimpleNamespace(speed_honoured_x=0),
                agree_map=states,
                compare=SimpleNamespace(
                    agree=4, agree_c2=0, differ=1, shifted=0, hard=0
                ),
                reference=(audio, absent),
            )

    module = SimpleNamespace(
        Device=_Device,
        features=frozenset(),
        C2=_C2,
        Purpose=_Purpose,
        PolicyReason=_Reason,
        CacheDefeat=_Defeat,
        AgreeState=_Agree,
        InvalidArgument=_Invalid,
        read_policy=read_policy,
    )
    return module, reads, policies


def _gated_read(module, **kw):
    return ar._defeated_read(
        module, "/dev/null", 100, 8, lambda _c: None, 32, True, **kw
    )


# ---- GATED_REREAD: the speed-ladder and strategy reads -------------------------------


def test_outside_a_rips_recovery_the_read_is_built_as_before() -> None:
    module, reads, policies = _module()
    _gated_read(module)
    assert policies == []
    assert reads[0]["speed_x"] == 32


def test_inside_recovery_the_policy_fills_the_read() -> None:
    module, reads, policies = _module()
    with ar.recovery_policy((40, 32, 24), c2_usable=False, readable=(0, 9000)) as pol:
        _result, defeat = _gated_read(module)
    assert policies[0]["purpose"] == _Purpose.GATED_REREAD
    assert policies[0]["admitted"] == (40, 32, 24)
    assert policies[0]["speed_x"] == 32
    assert policies[0]["readable"] == (0, 9000)
    assert reads[0]["lba"] == 100 and reads[0]["count"] == 8
    assert defeat == "done"
    assert pol.record() == "gated_reread:r2:none"


def test_a_declined_defeat_is_recorded_as_declined_not_off() -> None:
    """``off`` means an engine before 0.49.0 in PROV; the policy's "no target"
    is a different fact."""
    module, _reads, _p = _module(defeat=False, reasons=_Reason.DEFEAT_NO_TARGET)
    with ar.recovery_policy((32,), c2_usable=False, readable=None) as pol:
        _result, defeat = _gated_read(module)
    assert defeat == "declined"
    assert pol.record() == "gated_reread:r2:defeat_no_target"


def test_the_policys_c2_must_match_the_sinks_record_width() -> None:
    """GATED_REREAD asks for C2 when the drive has it; a sink framed for audio
    alone would mis-frame every record, so the read is refused."""
    module, reads, _p = _module()
    with (
        ar.recovery_policy((32,), c2_usable=True, readable=None),
        pytest.raises(RuntimeError, match="mis-frame"),
    ):
        _gated_read(module)
    assert reads == []


def test_the_windowed_read_follows_the_policys_c2() -> None:
    assert ar._windowed_read(False) is ar._read_windows_binding
    with ar.recovery_policy((32,), c2_usable=True, readable=None):
        assert ar._windowed_read(False) is ar._read_windows_with_c2
    with ar.recovery_policy((32,), c2_usable=False, readable=None):
        assert ar._windowed_read(True) is ar._read_windows_binding


def test_the_engine_gets_what_is_left_of_the_rips_budget() -> None:
    module, _reads, policies = _module()
    with (
        drive_work.budget(5000, 1000, 10000),
        ar.recovery_policy((32,), c2_usable=False, readable=None),
    ):
        drive_work.charge(32, far_read=True)
        drive_work.book_engine(400, 9)
        _gated_read(module)
    assert policies[0]["budget_cmds"] == 9600
    assert policies[0]["budget_speed_changes"] == 990


def test_a_spent_budget_stops_before_the_policy_or_the_drive() -> None:
    module, reads, policies = _module()
    with (
        drive_work.budget(5000, 1000, 10),
        ar.recovery_policy((32,), c2_usable=False, readable=None),
    ):
        drive_work.book_engine(10, 0)
        with pytest.raises(drive_work.DriveBudgetSpent):
            _gated_read(module)
    assert policies == [] and reads == []


# ---- SPAN_RESCUE and the shift_search override ---------------------------------------


def test_span_rescue_turns_shift_search_on_and_records_the_override() -> None:
    module, _reads, _p = _module(reasons=_Reason.SHIFT_SEARCH_PENDING_AB)
    with ar.recovery_policy((32, 24), c2_usable=True, readable=None) as pol:
        kw = ar._policy_kwargs(module, "SPAN_RESCUE", 100, 4, 32)
    assert kw is not None
    assert kw["shift_search"] is True
    assert pol.record() == "span_rescue:r2:shift_search_pending_ab"
    assert pol.overrides_record() == "span_rescue:shift_search=on"


def test_no_override_is_recorded_when_no_span_read_ran() -> None:
    module, _reads, _p = _module()
    with ar.recovery_policy((32,), c2_usable=False, readable=None) as pol:
        _gated_read(module)
    assert pol.overrides_record() is None


def test_the_span_detail_read_goes_through_span_rescue() -> None:
    src = inspect.getsource(ar._read_span_detail_binding)
    assert '_policy_kwargs(module, "SPAN_RESCUE"' in src
    assert 'verify_passes = int(policy["verify_passes"])' in src


# ---- REFERENCE_VERIFY: the digital-media check ---------------------------------------


def test_verify_compares_against_the_capture_and_finds_the_first_disagreement(
    tmp_path: Path, monkeypatch
) -> None:
    module, reads, policies = _module()
    monkeypatch.setattr(ar, "_binding", lambda _w: module)
    capture = tmp_path / "disc.pcm"
    capture.write_bytes(b"".join(bytes([i]) * 2352 for i in range(5)))
    heard: list[int] = []
    got = ar.verify_against_capture(
        "/dev/null",
        capture,
        ref_speed_x=40,
        exclude_speed_x=0,
        admitted=(40, 32),
        c2_usable=True,
        readable=(0, 5),
        on_speed=heard.append,
    )
    assert policies[0]["purpose"] == _Purpose.REFERENCE_VERIFY
    assert (policies[0]["ref_speed_x"], policies[0]["speed_x"]) == (40, 0)
    assert reads[0]["lba"] == 0 and reads[0]["count"] == 5
    assert heard == [24]
    assert got.first_disagreement == 3
    assert (got.agree, got.differ) == (4, 1)


def test_no_rung_left_is_its_own_error(tmp_path: Path, monkeypatch) -> None:
    module, reads, _p = _module(refuse=True)
    monkeypatch.setattr(ar, "_binding", lambda _w: module)
    capture = tmp_path / "disc.pcm"
    capture.write_bytes(bytes(2352))
    with pytest.raises(ar.NoVerifyRung):
        ar.verify_against_capture(
            "/dev/null",
            capture,
            ref_speed_x=40,
            exclude_speed_x=0,
            admitted=(40,),
            c2_usable=True,
            readable=None,
        )
    assert reads == []


# ---- the real binding ----------------------------------------------------------------


def test_the_real_policy_fits_device_read() -> None:
    """The fakes above echo the shape the seam expects, so only the real binding
    can show the policy's keywords, plus ours, bind to ``Device.read``. Skipped
    inside the test (not ``skipif``) for the reason ``test_ctdb_repair`` gives."""
    module = ar._import_binding()[0]
    if module is None or ar.missing_rip_features():
        pytest.skip("AccuDisc before 0.59.0, or not importable")
    sig = inspect.signature(module.Device.read)
    for purpose, speed in (("GATED_REREAD", 32), ("SPAN_RESCUE", 32)):
        chosen = module.read_policy(
            module.Purpose[purpose],
            100,
            8,
            c2_usable=True,
            admitted=(40, 32, 24),
            speed_x=speed,
            readable=(0, 200000),
            budget_cmds=10,
            budget_speed_changes=5,
        )
        kw = chosen.read_kwargs()
        sig.bind(None, **kw, sink=None, copy=False, status_map=True, cancel=None)
        assert kw["budget_cmds"] == 10
    verify = module.read_policy(
        module.Purpose.REFERENCE_VERIFY,
        0,
        8,
        c2_usable=True,
        admitted=(40, 32, 24),
        ref_speed_x=40,
        readable=(0, 200000),
    )
    assert verify.read_kwargs()["speed_x"] == 32
    assert "compare_read" in dir(module.Device)
