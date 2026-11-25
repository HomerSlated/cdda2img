"""The per-rip drive-work budget (kgr, 2026-11-25).

kgr: "check that the total number of read-passes does not escalate to the point
where we create a packet storm on the drive, potentially damaging it ... I want
to give recovery attempts the breathing space they need to do their job, but ...
I don't want to destroy my increasingly irreplaceable hardware in the process."

Three pieces: every recovery read is charged before it is sent (drive_work), the
chain stops cleanly when the budget is spent, and heavy damage no longer runs the
four per-target strategies whose reads grow with the flagged sectors.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from cdda2img import cdda2img as app
from cdda2img import drive_work
from cdda2img.drive_work import DriveBudgetSpent, DriveWork
from cdda2img.rip_abort import RipAborted
from cdda2img.validation import CONFIG_SCHEMA, validate
from tests.test_recovery_chain import _ctx, _fake_stages, _lane
from tests.test_recovery_passes import _Drive, _profile, _strategy

_PER_TARGET = {"max-variation", "sector-runup", "span-fixed", "sector-hammer"}


# ---- the counter ---------------------------------------------------------------------


def test_reads_seeks_and_speed_changes_are_counted() -> None:
    w = DriveWork()
    for speed in (40, 40, 32, 32, 8):
        w.charge(speed, far_read=True)
    w.charge(8, far_read=False)
    assert (w.reads, w.seeks, w.speed_changes) == (6, 5, 3)
    assert w.record() == "reads:6,seeks:5,speed_changes:3"


def test_the_read_limit_refuses_the_read_that_would_pass_it() -> None:
    w = DriveWork(max_reads=2)
    w.charge(40, far_read=True)
    w.charge(40, far_read=True)
    with pytest.raises(DriveBudgetSpent) as exc:
        w.charge(40, far_read=True)
    assert isinstance(exc.value, DriveBudgetSpent)
    assert (exc.value.what, exc.value.limit) == ("reads", 2)
    assert w.reads == 2  # the refused read was never counted, or sent


def test_the_speed_change_limit_refuses_only_a_change() -> None:
    w = DriveWork(max_speed_changes=1)
    w.charge(40, far_read=False)
    w.charge(40, far_read=False)  # control: no change, still allowed
    with pytest.raises(DriveBudgetSpent, match="speed_changes:1"):
        w.charge(32, far_read=False)


def test_zero_means_no_limit() -> None:
    w = DriveWork(max_reads=0, max_speed_changes=0)
    for i in range(10_000):
        w.charge(40 if i % 2 else 8, far_read=True)
    assert w.reads == 10_000


def test_a_spent_budget_is_not_an_error_the_recovery_loops_skip() -> None:
    """The loops catch RuntimeError/OSError to skip one failed read and go on."""
    assert not issubclass(DriveBudgetSpent, (RuntimeError, OSError))


def test_outside_a_rip_nothing_is_charged() -> None:
    drive_work.charge(40, far_read=True)  # no budget set: must not raise


def test_the_budget_is_scoped_to_its_block() -> None:
    with drive_work.budget(1, 0) as w:
        drive_work.charge(40, far_read=True)
        assert w.reads == 1
    drive_work.charge(40, far_read=True)  # outside again: no budget, no raise
    assert w.reads == 1


# ---- a real stage under the budget ---------------------------------------------------


def test_a_strategy_stops_at_the_budget_and_the_drive_never_sees_more(
    monkeypatch, tmp_path
) -> None:
    """sector-hammer on one target plans 5 reads (one per rung); a budget of 3
    must stop it after exactly 3, before the fourth reaches the drive."""
    drive = _Drive()
    with drive_work.budget(3, 0), pytest.raises(DriveBudgetSpent):
        _strategy(
            monkeypatch,
            tmp_path,
            _profile(granularity="sector", ladder="full"),
            flagged={150},
            drive=drive,
        )
    assert len(drive.calls) == 3


# ---- the chain -----------------------------------------------------------------------


def test_heavy_damage_leaves_out_the_per_target_strategies() -> None:
    assert not _PER_TARGET & set(app._CHAIN_ORDER["high"])
    assert app._CHAIN_ORDER["high"] == (
        "track-ladder",
        "track-constant",
        "span-flagged",
    )
    assert (
        set(app._CHAIN_ORDER["low"]) >= _PER_TARGET
    )  # control: light damage keeps them


def test_a_heavily_damaged_track_runs_no_per_target_strategy(monkeypatch) -> None:
    calls = _fake_stages(monkeypatch, {})
    app._recovery_chain(
        _ctx(disc_damage=_lane(set(range(110, 150)))), [SimpleNamespace(track=2)], 32, 0
    )
    assert [c[0] for c in calls] == [*app._CHAIN_ORDER["high"], "whole-disc"]


def test_a_spent_budget_stops_the_whole_chain_and_says_so(monkeypatch) -> None:
    calls: list[str] = []

    def stage(name, results, _ctx):
        calls.append(name)
        if name == "max-variation":
            raise DriveBudgetSpent("reads", 5000)
        return {r.track: "unrecovered" for r in results}, {}

    monkeypatch.setattr(app, "_run_chain_stage", stage)
    tracks = [SimpleNamespace(track=2), SimpleNamespace(track=3)]
    outcomes, prov = app._recovery_chain(
        _ctx(disc_damage=_lane({150, 250})), tracks, 32, 0
    )
    assert calls == ["span-flagged", "max-variation"]  # nothing after, no whole-disc
    assert prov["recovery_budget_hit"] == "reads:5000"
    assert prov["recovery_chain_track_2"] == "span-flagged:miss,max-variation:budget"
    assert prov["recovery_chain_track_3"] == "budget"
    assert outcomes == {2: "unrecovered", 3: "unrecovered"}


def test_the_abort_says_the_budget_stopped_it_and_how_to_raise_it() -> None:
    track = SimpleNamespace(track=2, confidence_v1=None, confidence_v2=None)
    prov = {
        "recovery_budget_hit": "speed_changes:1000",
        "recovery_drive_work": "reads:812,seeks:812,speed_changes:1000",
    }
    with pytest.raises(RipAborted) as exc:
        app._require_every_track_verified([track], {2: "unrecovered"}, prov)
    assert isinstance(exc.value, RipAborted)
    text = " ".join(exc.value.details)
    assert "recovery_max_speed_changes" in text
    assert "reads:812" in text


# ---- configuration and wiring --------------------------------------------------------


def test_the_limits_default_on_and_zero_turns_them_off() -> None:
    from cdda2img.config import Config

    c = Config()
    assert (c.recovery_max_reads, c.recovery_max_speed_changes) == (5000, 1000)
    assert validate({"recovery_max_reads": 0}, CONFIG_SCHEMA) == []
    assert validate({"recovery_max_reads": -1}, CONFIG_SCHEMA)
    assert validate({"recovery_max_speed_changes": -1}, CONFIG_SCHEMA)


def test_every_recovery_read_is_charged_before_it_is_sent() -> None:
    for fn, read in (
        (app._ladder_attempt, "read_span_attempt("),
        (app._strategy_attempt, "read_span_windows("),
        (app._span_pass, "read_span_detail("),
    ):
        src = inspect.getsource(fn)
        assert "drive_work.charge(" in src, fn.__name__
        assert src.index("drive_work.charge(") < src.index(read), fn.__name__


def test_the_rip_runs_recovery_inside_the_budget_and_records_the_work() -> None:
    src = inspect.getsource(app.rip_image)
    assert "drive_work.budget(" in src
    assert "cfg.recovery_max_reads, cfg.recovery_max_speed_changes" in src
    assert 'ladder_prov["recovery_drive_work"] = work.record()' in src


def test_a_budget_spent_in_the_whole_disc_stage_is_recorded_on_every_track(
    monkeypatch,
) -> None:
    def stage(name, results, _ctx):
        if name == "whole-disc":
            raise DriveBudgetSpent("reads", 5000)
        return {r.track: "unrecovered" for r in results}, {}

    monkeypatch.setattr(app, "_run_chain_stage", stage)
    tracks = [SimpleNamespace(track=1), SimpleNamespace(track=2)]
    _o, prov = app._recovery_chain(_ctx(), tracks, 32, 0)
    assert prov["recovery_budget_hit"] == "reads:5000"
    for t in (1, 2):
        assert prov[f"recovery_chain_track_{t}"].endswith("whole-disc:budget")
