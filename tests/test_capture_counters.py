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
