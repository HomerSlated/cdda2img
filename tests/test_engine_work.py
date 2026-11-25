"""The engine's own recovery work, recorded per rip (kgr, 2026-11-25).

AccuDisc 0.57.0 reports what each read spent on its own recovery
(``ReadStats.budget_spent_cmds`` / ``budget_spent_changes``). The seam books it
into the rip's drive work after each recovery read returns, and the rip records
it as PROV ``recovery_engine_work``. kgr set the per-rip limit at 10,000 ("Go
ahead with 10,000"), and the same day turned every 0.53-0.59 feature on ("I would
rather do my tests against 0.59.0 with all new features enabled"), so the limit
is enforced: each read's engine gets what is left of the rip's budget, and a
spent budget refuses the read, because AccuDisc's 0 means unlimited.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from cdda2img import accudisc_reader as ar
from cdda2img import cdda2img as app
from cdda2img import drive_work
from cdda2img.drive_work import DriveBudgetSpent, DriveWork
from cdda2img.validation import CONFIG_SCHEMA, validate


def _stats(cmds: int, changes: int) -> SimpleNamespace:
    return SimpleNamespace(budget_spent_cmds=cmds, budget_spent_changes=changes)


def _module(*, budget: bool, cmds: int = 7, changes: int = 2) -> SimpleNamespace:
    """A binding whose one Device.read returns the given spend."""

    class _Device:
        def __init__(self, _path: str) -> None:
            pass

        def __enter__(self) -> _Device:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self, *_args: object, **_kw: object) -> SimpleNamespace:
            return SimpleNamespace(stats=_stats(cmds, changes))

    features = frozenset({"read_budget"}) if budget else frozenset()
    return SimpleNamespace(Device=_Device, features=features)


# ---- the counter ---------------------------------------------------------------------


def test_engine_spend_is_summed_with_the_largest_single_read() -> None:
    w = DriveWork()
    for cmds, changes in ((12, 2), (0, 0), (40, 4), (3, 0)):
        w.book_engine(cmds, changes)
    assert w.engine_record() == (
        "cmds:55,speed_changes:6,max_read_cmds:40,reported_reads:4"
    )


def test_no_report_is_absent_not_zero() -> None:
    """An engine before 0.57.0 reports nothing; that must not read as 'spent 0'."""
    assert DriveWork().engine_record() is None


def test_each_read_gets_what_is_left_of_the_rips_engine_budget() -> None:
    with drive_work.budget(5000, 1000, 100) as w:
        drive_work.charge(32, far_read=True)  # our head speed set: 1 change
        assert drive_work.engine_budget() == (100, 999)
        drive_work.book_engine(60, 4)
        assert drive_work.engine_budget() == (40, 995)
    assert w.all_speed_changes == 5


def test_a_spent_engine_budget_refuses_the_read_rather_than_pass_zero() -> None:
    """AccuDisc's 0 means UNLIMITED (outbound 279): a remainder of 0 must stop."""
    with drive_work.budget(5000, 1000, 10):
        drive_work.book_engine(10, 0)
        with pytest.raises(DriveBudgetSpent) as exc:
            drive_work.engine_budget()
        assert isinstance(exc.value, DriveBudgetSpent)
        assert exc.value.what == "engine_cmds"
        with pytest.raises(DriveBudgetSpent):
            drive_work.charge(32, far_read=False)  # and our next read is not sent


def test_engine_speed_changes_share_the_one_spindle_limit() -> None:
    with drive_work.budget(5000, 3, 0):
        drive_work.charge(40, far_read=False)  # ours: 1
        drive_work.book_engine(0, 2)  # the engine's: 2, so 3 of 3
        with pytest.raises(DriveBudgetSpent) as exc:
            drive_work.engine_budget()
        assert isinstance(exc.value, DriveBudgetSpent)
        assert exc.value.what == "speed_changes"
        with pytest.raises(DriveBudgetSpent):
            drive_work.charge(32, far_read=False)


def test_a_wide_remainder_is_clamped_to_the_field_not_wrapped() -> None:
    with drive_work.budget(0, 70000, 2**33):
        assert drive_work.engine_budget() == (0xFFFFFFFF, 0xFFFF)


def test_no_limit_and_no_rip_both_mean_no_engine_budget() -> None:
    assert drive_work.engine_budget() == (0, 0)  # the capture: never budgeted
    with drive_work.budget(0, 0, 0):
        assert drive_work.engine_budget() == (0, 0)


def test_booking_outside_a_rip_is_a_no_op() -> None:
    drive_work.book_engine(99, 9)  # no budget active: must not raise


# ---- the seam ------------------------------------------------------------------------


def test_a_recovery_read_books_its_engine_spend() -> None:
    with drive_work.budget(0, 0) as w:
        ar._defeated_read(
            _module(budget=True), "/dev/null", 0, 1, lambda _c: None, 8, False
        )
    assert w.engine_record() == (
        "cmds:7,speed_changes:2,max_read_cmds:7,reported_reads:1"
    )


def test_an_engine_without_read_budget_books_nothing() -> None:
    """Feature-detected, never ``getattr``-defaulted: absent is not 0."""
    with drive_work.budget(0, 0) as w:
        ar._defeated_read(
            _module(budget=False), "/dev/null", 0, 1, lambda _c: None, 8, False
        )
    assert w.engine_record() is None


def test_every_recovery_read_in_the_seam_books_its_spend() -> None:
    """Both recovery read paths (every windowed read goes through
    ``_defeated_read``; the span detail read) book after their ``Device.read``;
    the capture does not (it is never inside a budget, and its own counts are
    ``capture_*``)."""
    assert "_defeated_read(" in inspect.getsource(ar._read_windows_binding)
    assert "_defeated_read(" in inspect.getsource(ar._read_windows_with_c2)
    for fn in (ar._defeated_read, ar._read_span_detail_binding):
        src = inspect.getsource(fn)
        assert "_book_engine_work(module, result.stats)" in src, fn.__name__
        assert src.index("dev.read(") < src.index("_book_engine_work("), fn.__name__
    assert "_book_engine_work" not in inspect.getsource(ar._read_disc_binding)


# ---- configuration and wiring --------------------------------------------------------


def test_the_engine_limit_defaults_to_10000_and_rejects_negatives() -> None:
    from cdda2img.config import Config

    assert Config().recovery_max_engine_cmds == 10000
    assert validate({"recovery_max_engine_cmds": 0}, CONFIG_SCHEMA) == []
    assert validate({"recovery_max_engine_cmds": -1}, CONFIG_SCHEMA)


def test_the_rip_carries_the_limit_and_records_the_engine_work() -> None:
    src = " ".join(inspect.getsource(app.rip_image).split())
    assert "cfg.recovery_max_engine_cmds," in src
    assert 'ladder_prov["recovery_engine_work"] = engine' in src


def test_the_real_binding_names_the_fields_the_seam_reads() -> None:
    """The fakes above supply the names the seam chose, so they cannot catch a
    wrong one. A wrong name raises ``AttributeError`` on the first recovery read of
    any engine publishing ``read_budget``, which no recovery loop catches: the rip
    would end. So check the real ``ReadStats``. Skipped inside the test (not
    ``skipif``) for the reason ``test_ctdb_repair`` gives."""
    module = ar._import_binding()[0]
    if module is None or "read_budget" not in getattr(module, "features", ()):
        pytest.skip("AccuDisc binding before 0.57.0, or not importable")
    import dataclasses

    names = {f.name for f in dataclasses.fields(module.ReadStats)}
    assert {"budget_spent_cmds", "budget_spent_changes"} <= names
