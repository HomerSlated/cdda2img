"""The capture's own re-reads are recorded in PROV (kgr, 2026-11-25).

AccuDisc's Q-position rescue always runs on a read with the raw subchannel
(their 2026-11-25a, A.1), so the capture pass is not a single pass. Until now
its counts were logged at DEBUG and lost, so nobody could say how often the
rescue fires on a real rip. They are PROV ``capture_*`` now (rbi_spec).
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from cdda2img import cdda2img as app
from cdda2img.accudisc_reader import CaptureCounters
from cdda2img.rip_abort import RipAborted


def test_every_reported_counter_becomes_a_prov_key() -> None:
    assert app._capture_prov(CaptureCounters(22, 1, 17)) == {
        "capture_sectors_recovered": "22",
        "capture_sectors_suspect": "1",
        "capture_subq_misposition": "17",
    }


def test_an_unreported_counter_is_left_out_not_written_as_zero() -> None:
    got = app._capture_prov(CaptureCounters(0, 0, None))
    assert "capture_subq_misposition" not in got
    assert got["capture_sectors_recovered"] == "0"  # control: zero is a value


def _call_kwargs(fn: object, name: str) -> set[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))  # type: ignore[arg-type]
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", getattr(n.func, "attr", None)) == name
    ]
    assert calls, f"no call to {name}; this guard would pass vacuously"
    return {k.arg for c in calls for k in c.keywords if k.arg}


def test_the_rip_asks_the_capture_for_its_counters() -> None:
    assert "stats_cb" in _call_kwargs(app._rip_disc_stage, "read_disc_c2")
    assert "_capture_prov" in inspect.getsource(app._rip_disc_stage)


def test_a_q_gate_abort_reports_the_capture_counters() -> None:
    """The Q rescue's counts belong beside the Q stats when Q is what failed."""
    prov = {
        "subq_q_valid": "0/100",
        "capture_subq_misposition": "4",
        "lookup_status_mb": "OK",
    }
    with pytest.raises(RipAborted) as exc:
        app._require_usable_q(prov)
    assert isinstance(exc.value, RipAborted)
    assert exc.value.context.get("capture_subq_misposition") == "4"
    assert "lookup_status_mb" not in exc.value.context  # still filtered


# ---- which mechanism did the capture's work (kgr, 2026-11-25) -------------------------
# AccuDisc 0.53.0's effort stats, per stage and per origin. Whether the always-on
# rescue catches a damaged sector varies pass to pass (AccuDisc 2026-11-25ad), so
# runs are compared on these, not only on a later AccurateRip result.


def _fake_effort_module():
    import enum
    from types import SimpleNamespace

    class Stage(enum.IntEnum):
        STREAM = 0
        FALLBACK = 1
        QPOS = 2
        FAR = 7
        SPEED = 8

    class Origin(enum.IntEnum):
        NONE = 0
        FIRST = 1
        FALLBACK = 3
        QPOS = 4

    return SimpleNamespace(
        features=frozenset({"effort_stats"}), Stage=Stage, Origin=Origin
    )


def _stats(**over):
    from types import SimpleNamespace

    base = {
        "sectors_recovered": 26,
        "sectors_suspect": 0,
        "subq_misposition": 30,
        "cmds": (1484, 6, 24, 0, 0, 0, 0, 30, 2) + (0,) * 7,
        "cmds_failed": (0, 4, 16) + (0,) * 13,
        "us": (451_200_999, 0, 3_810_500) + (0,) * 13,
        "won_by": (0, 347_180, 0, 2, 26) + (0,) * 11,
        "speed_changes": 1,
    }
    return SimpleNamespace(**{**base, **over})


def test_the_effort_tables_become_prov_keys() -> None:
    from cdda2img.accudisc_reader import _capture_counters

    prov = app._capture_prov(_capture_counters(_stats(), _fake_effort_module()))
    assert prov["capture_cmds"] == "stream:1484,fallback:6,qpos:24,far:30,speed:2"
    assert prov["capture_cmds_failed"] == "fallback:4,qpos:16"
    assert prov["capture_ms"] == "stream:451200,qpos:3810"  # rounded down
    assert prov["capture_won_by"] == "first:347180,fallback:2,qpos:26"
    assert prov["capture_speed_changes"] == "1"


def test_reported_and_all_zero_is_none_not_absent() -> None:
    from cdda2img.accudisc_reader import _capture_counters

    zero = (0,) * 16
    prov = app._capture_prov(
        _capture_counters(_stats(cmds_failed=zero), _fake_effort_module())
    )
    assert prov["capture_cmds_failed"] == "none"


def test_an_engine_before_053_writes_no_effort_keys() -> None:
    from cdda2img.accudisc_reader import _capture_counters

    module = _fake_effort_module()
    module.features = frozenset()  # no "effort_stats"
    prov = app._capture_prov(_capture_counters(_stats(), module))
    assert not {k for k in prov if k in {"capture_cmds", "capture_won_by"}}
    assert prov["capture_sectors_recovered"] == "26"  # control: the totals stay


def test_the_real_binding_names_the_effort_fields_and_stages() -> None:
    """The fakes above use the names the code chose; only the real binding can
    show they are AccuDisc's. Skipped inside the test, as test_ctdb_repair does."""
    import dataclasses

    from cdda2img import accudisc_reader as ar

    module = ar._import_binding()[0]
    if module is None or "effort_stats" not in getattr(module, "features", ()):
        pytest.skip("AccuDisc before 0.53.0, or not importable")
    names = {f.name for f in dataclasses.fields(module.ReadStats)}
    assert {"cmds", "cmds_failed", "us", "won_by", "speed_changes"} <= names
    assert {"stream", "fallback", "qpos", "far", "speed"} <= {
        s.name.lower() for s in module.Stage
    }
    assert {"first", "fallback", "qpos"} <= {o.name.lower() for o in module.Origin}
