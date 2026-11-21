"""The bench's recovery strategies in the rip path (kgr, 2026-11-21, option 2).

Until then every profile but ``span-flagged`` ran as the speed ladder while PROV
named the profile, so ``max-variation`` randomised nothing. Now the five bench
arms run their own read plans (``cdda2img.strategy_reads``, shared with the bench),
stand alone, ask for a cache defeat on every read, and record every read.

The disc here is fake and tiny: three tracks of 100 sectors whose GOOD bytes name
their own LBA. The PCM file starts with chosen sectors corrupted, a fake read
returns good bytes for whatever windows it is asked to keep, and a fake
AccurateRip matches a track exactly when its bytes are the good ones.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import random
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cdda2img import accudisc_reader as ar
from cdda2img import cdda2img as app
from cdda2img import recovery_profile as R
from cdda2img import strategy_reads as S
from cdda2img.validation import PROFILE_SCHEMA, validate

_B = 2352
_LSNS = [0, 100, 200]
_LEADOUT = 300
_LADDER = [40, 32, 24, 8, 4]


def _good(lba: int) -> bytes:
    return lba.to_bytes(4, "little") * (_B // 4)


def _bad(lba: int) -> bytes:
    return (lba + 0x10000).to_bytes(4, "little") * (_B // 4)


def _good_track(track: int) -> bytes:
    lo, hi = S.track_bounds(track, _LSNS, _LEADOUT)
    return b"".join(_good(lba) for lba in range(lo, hi))


def _disc(tmp_path: Path, bad: set[int]) -> Path:
    pcm = tmp_path / "disc.pcm"
    pcm.write_bytes(
        b"".join(_bad(lba) if lba in bad else _good(lba) for lba in range(_LEADOUT))
    )
    return pcm


class _Reads:
    """A fake ``read_span_windows``: good bytes for every kept window, unless the
    read is listed in *fail* (then it raises) or *stale* (bytes stay bad)."""

    def __init__(self, *, stale: bool = False, fail: tuple[int, ...] = ()) -> None:
        self.calls: list[dict[str, Any]] = []
        self.stale = stale
        self.fail = fail

    def __call__(self, device, start, count, windows, read_speed=None, **kw):
        self.calls.append({
            "start": start,
            "count": count,
            "windows": list(windows),
            "speed": read_speed,
            **kw,
        })
        if len(self.calls) in self.fail:
            msg = "drive said no"
            raise RuntimeError(msg)
        pieces = []
        for lo, hi in windows:
            a, b = max(lo, start), min(hi, start + count)
            src = _bad if self.stale else _good
            pieces.append(b"".join(src(lba) for lba in range(a, b)) if a < b else b"")
        return ar.WindowsRead(
            pieces=pieces, speed_honoured_x=read_speed, cache_defeat="done"
        )


def _match(corrected, track, n_tracks, responses):
    ok = corrected == _good_track(track)
    return (None, None, 200 if ok else None, None)


def _profile(**fields: Any) -> R.Profile:
    data = {"name": "p", "ladder": "single", **fields}
    assert validate(data, PROFILE_SCHEMA) == [], fields
    return R.Profile.from_dict(data)


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    profile: R.Profile,
    *,
    bad: set[int],
    failed: list[int],
    damage: set[int] | None = None,
    reads: _Reads | None = None,
    seed: int = 7,
):
    reads = reads or _Reads()
    monkeypatch.setattr(ar, "read_span_windows", reads)
    monkeypatch.setattr("cdda2img.accuraterip.match_track_pcm", _match)
    pcm = _disc(tmp_path, bad)
    lane = bytes(1 if lba in (damage or set()) else 0 for lba in range(_LEADOUT))
    kind = S.strategy_kind(profile)
    assert kind is not None
    outcomes, prov = app._recover_by_strategy(
        kind,
        profile,
        device="/dev/sr0",
        failed_tracks=[SimpleNamespace(track=t) for t in failed],
        track_lsns=_LSNS,
        disc_last_lsn=_LEADOUT - 1,
        pcm_file=pcm,
        responses=[["block"]],
        ladder=_LADDER if profile.variation == "full" else [_LADDER[0]],
        read_offset=0,
        disc_damage=lane if damage is not None else None,
        ui=None,
        seed=seed,
    )
    return outcomes, prov, pcm.read_bytes(), reads


# ---- which strategy a profile describes --------------------------------------


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("sector-hammer", "sector-hammer"),
        ("sector-runup", "sector-runup"),
        ("span-fixed", "span-fixed"),
        ("max-variation", "max-variation"),
        ("whole-disc", "whole-disc"),
        ("track-ladder", None),
        ("track-constant", None),
        ("span-flagged", None),
    ],
)
def test_each_shipped_profile_names_the_strategy_it_runs(name, kind) -> None:
    assert S.strategy_kind(R.load_profile(name)) == kind


def test_the_kind_comes_from_fields_not_the_file_name() -> None:
    """A user profile may shadow a shipped one, so PROV must describe what ran."""
    p = _profile(name="whole-disc", granularity="sector", run_up=16)
    assert S.strategy_kind(p) == "sector-runup"


@pytest.mark.parametrize(
    ("data", "where"),
    [
        (
            {"granularity": "sector", "ladder": "single", "run_up": 8, "span": 64},
            "span",
        ),
        ({"granularity": "track", "variation": "full"}, "variation"),
        (
            {"granularity": "whole-disc", "ladder": "single", "variation": "full"},
            "variation",
        ),
        ({"granularity": "sector", "variation": "full", "run_up": 8}, "variation"),
        (
            {"granularity": "sector", "ladder": "full", "variation": "speed"},
            "variation",
        ),
        (
            {"granularity": "sector", "ladder": "single", "variation": "full"},
            "ladder",
        ),
        (
            {"granularity": "sector", "ladder": "single", "verify_passes": 2},
            "verify_passes",
        ),
        (
            {"granularity": "whole-disc", "ladder": "single", "overlap_sectors": 4},
            "overlap_sectors",
        ),
        (
            {
                "granularity": "sector",
                "ladder": "single",
                "c2_retries": 3,
                "verify_passes": 2,
            },
            "c2_retries",
        ),
    ],
)
def test_a_combination_no_strategy_can_carry_out_is_refused(data, where) -> None:
    """Each would otherwise run as some other strategy while PROV named this one."""
    errors = validate({"name": "p", **data}, PROFILE_SCHEMA)
    assert where in {e.where for e in errors}, errors


def test_the_valid_strategy_shapes_pass() -> None:
    """Control for the table above: the rules refuse combinations, not strategies."""
    for data in (
        {"granularity": "sector", "ladder": "single"},
        {"granularity": "sector", "ladder": "single", "run_up": 32},
        {"granularity": "sector", "ladder": "single", "span": 512},
        {"granularity": "sector", "ladder": "full", "variation": "full"},
        {"granularity": "whole-disc", "ladder": "single"},
        {"granularity": "track", "ladder": "full"},
    ):
        assert validate({"name": "p", **data}, PROFILE_SCHEMA) == [], data


# ---- one definition for the bench and the rip ---------------------------------


def test_the_bench_runs_the_rips_read_plans() -> None:
    path = Path(__file__).resolve().parents[1] / "tools" / "strategy_bench.py"
    spec = importlib.util.spec_from_file_location("_strategy_bench", path)
    assert spec is not None and spec.loader is not None
    bench = importlib.util.module_from_spec(spec)
    sys.modules["_strategy_bench"] = bench
    try:
        spec.loader.exec_module(bench)
        assert getattr(bench, "strategy_reads") is S.strategy_reads  # noqa: B009
        assert getattr(bench, "STRATEGIES") is S.STRATEGIES  # noqa: B009
    finally:
        del sys.modules["_strategy_bench"]


# ---- the runner ----------------------------------------------------------------


def test_sector_hammer_repairs_the_flagged_sector_and_nothing_else(
    monkeypatch, tmp_path
) -> None:
    outcomes, prov, pcm, reads = _run(
        monkeypatch,
        tmp_path,
        _profile(granularity="sector", passes=8),
        bad={150},
        failed=[2],
        damage={150},
    )
    assert outcomes == {2: "strategy_matched@a1"}
    assert pcm == b"".join(_good(lba) for lba in range(_LEADOUT))
    assert prov["recovery_strategy"] == "sector-hammer"
    assert prov["strategy_targets_track_2"] == "1"
    assert prov["strategy_reads_track_2"] == "150+1@40>40:done"
    assert "recovery_seed" not in prov  # nothing random ran
    assert reads.calls[0]["windows"] == [(100, 200)]


def test_every_sector_read_asks_for_a_cache_defeat(monkeypatch, tmp_path) -> None:
    """Short repeated windows are what the defeat exists for (AccuDisc 11-21j)."""
    _o, _p, _pcm, reads = _run(
        monkeypatch,
        tmp_path,
        _profile(granularity="sector", passes=4),
        bad={150},
        failed=[2],
        damage={150},
        reads=_Reads(stale=True),
    )
    assert len(reads.calls) == 4
    assert all(c["cache_defeat"] is True for c in reads.calls)


def test_an_unrecovered_track_keeps_its_audio_and_lists_every_read(
    monkeypatch, tmp_path
) -> None:
    before = b"".join(
        _bad(lba) if lba == 150 else _good(lba) for lba in range(_LEADOUT)
    )
    outcomes, prov, pcm, _reads = _run(
        monkeypatch,
        tmp_path,
        _profile(granularity="sector", passes=3),
        bad={150},
        failed=[2],
        damage={150},
        reads=_Reads(stale=True),
    )
    assert outcomes == {2: "unrecovered"}
    assert pcm == before  # no unverified splice
    assert prov["strategy_reads_track_2"].count("@") == 3


def test_a_read_that_raises_is_recorded_and_the_walk_goes_on(
    monkeypatch, tmp_path
) -> None:
    outcomes, prov, _pcm, _reads = _run(
        monkeypatch,
        tmp_path,
        _profile(granularity="sector", passes=8),
        bad={150},
        failed=[2],
        damage={150},
        reads=_Reads(fail=(1,)),
    )
    assert outcomes == {2: "strategy_matched@a2"}
    assert prov["strategy_reads_track_2"] == "150+1@40:error,150+1@40>40:done"


def test_a_run_up_reaching_into_the_previous_track_splices_only_this_one(
    monkeypatch, tmp_path
) -> None:
    """A max-variation run-up may start inside the previous track (the bench
    clamps it at sector 0, not at the track). Only this track's window is kept,
    and its bytes land at the right place: a misplaced copy would never match."""
    outcomes, _prov, pcm, reads = _run(
        monkeypatch,
        tmp_path,
        _profile(granularity="sector", ladder="full", variation="full", passes=40),
        bad={101, 102},
        failed=[2],
        damage={101, 102},
        seed=4,
    )
    # Seed 4's first read is 69+35 at 8X: it starts inside track 1 and covers both
    # bad sectors, so it alone must repair the track. With a later read allowed to
    # do it instead, a misplaced first copy would go unseen (mutation-tested). Not
    # a read starting at sector 0: with a 100-sector window, Python's negative
    # slice index puts an unclamped copy at the right place by coincidence.
    assert (reads.calls[0]["start"], reads.calls[0]["count"]) == (69, 35)
    assert outcomes[2] == "strategy_matched@a1"
    assert pcm == b"".join(_good(lba) for lba in range(_LEADOUT))
    assert reads.calls[0]["windows"] == [(100, 200)]


def test_max_variation_records_a_seed_that_regenerates_its_reads(
    monkeypatch, tmp_path
) -> None:
    """The seed is the record: the same seed must give the same reads."""
    profile = _profile(granularity="sector", ladder="full", variation="full", passes=6)
    _o, prov, _pcm, reads = _run(
        monkeypatch,
        tmp_path,
        profile,
        bad={150, 151},
        failed=[2],
        damage={150, 151},
        reads=_Reads(stale=True),
        seed=12345,
    )
    assert prov["recovery_strategy"] == "max-variation"
    assert prov["recovery_seed"] == "12345"
    expected = list(
        S.strategy_reads(
            "max-variation",
            2,
            [150, 151],
            _LSNS,
            _LEADOUT,
            _LADDER,
            _LADDER[0],
            0,
            0,
            random.Random(12345),  # noqa: S311 - a seed is the point
            6,
        )
    )
    # The seed regenerates the sectors; the speed steps through the ladder, one
    # rung per read (kgr, 2026-11-21), whatever the plan drew.
    assert [(c["start"], c["count"]) for c in reads.calls] == [
        (r.start, r.count) for r in expected
    ]
    assert [c["speed"] for c in reads.calls] == [
        _LADDER[i % len(_LADDER)] for i in range(len(expected))
    ]


def test_a_track_with_nothing_flagged_is_aimed_at_its_first_sector(
    monkeypatch, tmp_path
) -> None:
    """The bench's rule, kept: no damage map means every read starts the track."""
    _o, prov, _pcm, reads = _run(
        monkeypatch,
        tmp_path,
        _profile(granularity="sector", passes=2),
        bad={150},
        failed=[2],
        damage=None,
        reads=_Reads(stale=True),
    )
    assert prov["strategy_targets_track_2"] == "0"
    assert [c["start"] for c in reads.calls] == [100, 100]


def test_whole_disc_serves_every_failed_track_from_one_read(
    monkeypatch, tmp_path
) -> None:
    outcomes, prov, pcm, reads = _run(
        monkeypatch,
        tmp_path,
        _profile(granularity="whole-disc", passes=3),
        bad={50, 250},
        failed=[1, 3],
    )
    assert len(reads.calls) == 1
    assert (reads.calls[0]["start"], reads.calls[0]["count"]) == (0, _LEADOUT)
    assert reads.calls[0]["windows"] == [(0, 100), (200, 300)]
    assert outcomes == {1: "strategy_matched@a1", 3: "strategy_matched@a1"}
    assert prov["strategy_reads_track_1"] == prov["strategy_reads_track_3"]
    assert "strategy_targets_track_1" not in prov
    assert pcm == b"".join(_good(lba) for lba in range(_LEADOUT))


def test_whole_disc_stops_reading_once_nothing_is_left(monkeypatch, tmp_path) -> None:
    _o, _p, _pcm, reads = _run(
        monkeypatch,
        tmp_path,
        _profile(granularity="whole-disc", passes=5),
        bad={50},
        failed=[1],
    )
    assert len(reads.calls) == 1


# ---- wiring: a strategy stands alone ------------------------------------------


def test_a_strategy_runs_instead_of_the_ladder_never_before_it() -> None:
    """kgr, 2026-11-21: each strategy stands alone. The ladder call must sit in the
    ELSE arm of the strategy branch, so a track a strategy leaves unrecovered is
    never handed on."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(app.rip_image)))
    branches = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.If)
        and "strategy_kind is not None" in ast.unparse(n.test)
        and any("_recover_by_strategy" in ast.unparse(s) for s in n.body)
    ]
    assert len(branches) == 1
    orelse = "".join(ast.unparse(s) for s in branches[0].orelse)
    assert "_recover_failed_tracks" in orelse
    body = "".join(ast.unparse(s) for s in branches[0].body)
    assert "_recover_failed_tracks" not in body


def test_the_whole_disc_read_asks_for_no_cache_defeat(monkeypatch, tmp_path) -> None:
    """AccuDisc 11-21j: its far target lies past the lead-out, so it would fail on
    every pass, and the disc's start cannot be cached after a pass that ended at
    the lead-out. kgr adopted their table."""
    _o, _p, _pcm, reads = _run(
        monkeypatch,
        tmp_path,
        _profile(granularity="whole-disc", passes=2),
        bad={50},
        failed=[1],
        reads=_Reads(stale=True),
    )
    assert len(reads.calls) == 2
    assert all(c["cache_defeat"] is False for c in reads.calls)


def test_the_runner_steps_and_span_fixed_holds(monkeypatch, tmp_path) -> None:
    from cdda2img import accudisc_reader as ar2

    reads = _Reads(stale=True)
    monkeypatch.setattr(ar2, "read_span_windows", reads)
    monkeypatch.setattr("cdda2img.accuraterip.match_track_pcm", _match)
    lane = bytes(1 if lba == 150 else 0 for lba in range(_LEADOUT))
    for kind, prof, fixed, expected in (
        (
            "sector-hammer",
            _profile(granularity="sector", passes=6),
            None,
            _LADDER + _LADDER[:1],
        ),
        (
            "span-fixed",
            _profile(granularity="sector", span=64, passes=3),
            24,
            [24, 24, 24],
        ),
        # Decided by the field, not the name: any single-ladder shape holds.
        (
            "sector-runup",
            _profile(granularity="sector", run_up=16, passes=2),
            8,
            [8, 8],
        ),
    ):
        reads.calls.clear()
        pcm = _disc(tmp_path, {150})
        app._recover_by_strategy(
            kind,
            prof,
            device="/dev/sr0",
            failed_tracks=[SimpleNamespace(track=2)],
            track_lsns=_LSNS,
            disc_last_lsn=_LEADOUT - 1,
            pcm_file=pcm,
            responses=[["block"]],
            ladder=_LADDER,
            read_offset=0,
            disc_damage=lane,
            ui=None,
            seed=1,
            fixed_speed=fixed,
        )
        assert [c["speed"] for c in reads.calls] == expected, kind
