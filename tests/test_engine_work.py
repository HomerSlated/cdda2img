"""The engine's own recovery work, recorded per rip (kgr, 2026-11-25).

AccuDisc 0.57.0 reports what each read spent on its own recovery
(``ReadStats.budget_spent_cmds`` / ``budget_spent_changes``). The seam books it
into the rip's drive work after each recovery read returns, and the rip records
it as PROV ``recovery_engine_work``. kgr set the per-rip limit at 10,000 ("Go
ahead with 10,000") on the understanding that it is RECORDED ONLY until the
per-read budget is adopted after his series: these tests pin that it never stops
anything yet, as well as that the record is right.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from cdda2img import accudisc_reader as ar
from cdda2img import cdda2img as app
from cdda2img import drive_work
from cdda2img.drive_work import DriveWork
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


def test_the_engine_limit_is_recorded_only_and_never_stops_a_rip() -> None:
    """kgr approved 10,000 as inert until the series ends: booking past the limit
    must not raise, and must not stop our own reads either."""
    with drive_work.budget(5000, 1000, 10) as w:
        drive_work.book_engine(50, 0)
        drive_work.book_engine(50, 0)
        drive_work.charge(32, far_read=False)  # our next read still goes ahead
    assert w.max_engine_cmds == 10
    assert w.engine_cmds == 100
    assert w.reads == 1


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
    """The three recovery reads (window, defeated, span detail) book after their
    ``Device.read``; the capture does not (it is never inside a budget, and its
    own counts are ``capture_*``)."""
    for fn in (
        ar._read_windows_binding,
        ar._defeated_read,
        ar._read_span_detail_binding,
    ):
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
