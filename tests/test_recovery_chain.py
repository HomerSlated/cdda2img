"""The automatic recovery chain (kgr, 2026-11-21).

"The selection should be automatic according to the damage profile, do a speed
ladder run per profile, switch automatically to another profile upon failure,
and run the full list of profiles to exhaustion, terminating early as soon as
all tracks are recovered, or aborting with an error."

Stages are replaced by fakes here: the stages themselves are the span rung, the
speed ladder and the bench strategies, each tested in its own file. What is
tested is the order, the stopping rule, the shared whole-disc stage, the record,
the ceiling, and which existing code each stage hands its track to.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from cdda2img import cdda2img as app


def _ctx(**over: object) -> app._ChainContext:
    ctx = app._ChainContext(
        device="/dev/sr0",
        track_lsns=[0, 100, 200],
        disc_last_lsn=299,
        pcm_file=Path("/nonexistent"),
        responses=[["block"]],
        ladder=[40, 32, 24, 8, 4],
        fixed_speed=32,
        read_offset=0,
        disc_damage=None,
        ui=None,
        cohort=None,
    )
    return ctx._replace(**over)


def _lane(flagged: set[int]) -> bytes:
    return bytes(1 if lba in flagged else 0 for lba in range(300))


def _fake_stages(monkeypatch, hits: dict[str, set[int]]):
    """Each stage recovers the tracks listed for it; returns the call log."""
    calls: list[tuple[str, tuple[int, ...]]] = []

    def stage(name, results, _ctx):
        tracks = tuple(r.track for r in results)
        calls.append((name, tracks))
        got = {
            t: (f"{name}@ok" if t in hits.get(name, set()) else "unrecovered")
            for t in tracks
        }
        return got, {f"strategy_reads_track_{t}": f"{name}-reads" for t in tracks}

    monkeypatch.setattr(app, "_run_chain_stage", stage)
    return calls


# ---- classes, order, speed ------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "cls"), [(0, "none"), (1, "low"), (5, "low"), (6, "high")]
)
def test_the_damage_class_threshold_is_the_benchs(n: int, cls: str) -> None:
    assert app._damage_class(n) == cls


def test_every_shipped_profile_is_in_the_chain() -> None:
    """Full utilisation of all recovery profiles: none may be left uninvokable."""
    from cdda2img.recovery_profile import list_profiles

    used = set(app._CHAIN_ORDER["low"]) | set(app._CHAIN_ORDER["high"]) | {"whole-disc"}
    assert used == set(list_profiles())


def test_nothing_flagged_gets_only_whole_track_shapes() -> None:
    """The sector shapes would aim at the track's first sector; the span rung
    has nothing to read."""
    assert app._CHAIN_ORDER["none"] == ("track-ladder", "track-constant")


def test_light_damage_starts_targeted_and_heavy_damage_with_the_ladder() -> None:
    assert app._CHAIN_ORDER["low"][:2] == ("span-flagged", "max-variation")
    assert app._CHAIN_ORDER["high"][0] == "track-ladder"


@pytest.mark.parametrize(
    ("speed", "ladder", "snapped"),
    [
        (32, [40, 32, 24, 8, 4], 32),
        (32, [8, 4], 8),
        (28, [32, 24], 24),
        (99, [40, 32], 40),
    ],
)
def test_the_fixed_speed_snaps_to_an_admitted_rung(speed, ladder, snapped) -> None:
    assert app._snap_to_ladder(speed, ladder) == snapped


# ---- the runner ----------------------------------------------------------------------


def test_a_light_track_walks_its_order_and_stops_at_the_first_hit(monkeypatch) -> None:
    calls = _fake_stages(monkeypatch, {"track-ladder": {2}})
    ctx = _ctx(disc_damage=_lane({150, 151}))
    outcomes, prov = app._recovery_chain(ctx, [SimpleNamespace(track=2)], 32, 0)
    assert [c[0] for c in calls] == ["span-flagged", "max-variation", "track-ladder"]
    assert outcomes == {2: "track-ladder@ok"}
    assert prov["recovery_damage_track_2"] == "2:low"
    assert prov["recovery_chain_track_2"] == (
        "span-flagged:miss,max-variation:miss,track-ladder:hit"
    )
    assert prov["recovery_speed"] == "32X>32X"


def test_stage_keys_never_overwrite_each_other(monkeypatch) -> None:
    _fake_stages(monkeypatch, {"track-ladder": {2}})
    _o, prov = app._recovery_chain(
        _ctx(disc_damage=_lane({150})), [SimpleNamespace(track=2)], 32, 0
    )
    assert prov["strategy_reads_track_2__span_flagged"] == "span-flagged-reads"
    assert prov["strategy_reads_track_2__max_variation"] == "max-variation-reads"
    assert "strategy_reads_track_2" not in prov


def test_unrecovered_tracks_share_one_whole_disc_stage(monkeypatch) -> None:
    calls = _fake_stages(monkeypatch, {"whole-disc": {1}})
    tracks = [SimpleNamespace(track=1), SimpleNamespace(track=2)]
    outcomes, prov = app._recovery_chain(_ctx(), tracks, 32, 0)
    assert calls[-1] == ("whole-disc", (1, 2))
    assert [c for c in calls if c[0] == "whole-disc"] == [("whole-disc", (1, 2))]
    assert outcomes == {1: "whole-disc@ok", 2: "unrecovered"}
    assert prov["recovery_chain_track_2"].endswith("whole-disc:miss")
    assert prov["recovery_damage_track_1"] == "0:none"


def test_a_recovered_track_is_not_read_again(monkeypatch) -> None:
    calls = _fake_stages(monkeypatch, {"track-ladder": {1}})
    tracks = [SimpleNamespace(track=1), SimpleNamespace(track=2)]
    app._recovery_chain(_ctx(), tracks, 32, 0)
    assert ("whole-disc", (2,)) in calls
    assert all(1 not in t for name, t in calls if name != "track-ladder")


def test_every_stage_runs_to_exhaustion_without_a_ceiling(monkeypatch) -> None:
    calls = _fake_stages(monkeypatch, {})
    app._recovery_chain(
        _ctx(disc_damage=_lane({150})), [SimpleNamespace(track=2)], 32, 0
    )
    assert [c[0] for c in calls] == [*app._CHAIN_ORDER["low"], "whole-disc"]


def test_the_ceiling_stops_the_chain_and_says_so(monkeypatch) -> None:
    calls = _fake_stages(monkeypatch, {})
    clock = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0])
    monkeypatch.setattr("time.monotonic", lambda: next(clock, 100.0))
    outcomes, prov = app._recovery_chain(_ctx(), [SimpleNamespace(track=2)], 32, 10)
    assert len(calls) == 1  # the deadline passed after the first stage
    assert prov["recovery_ceiling_hit"] == "10"
    assert outcomes == {2: "unrecovered"}


def test_an_exhausted_chain_is_not_reported_as_a_ceiling(monkeypatch) -> None:
    _fake_stages(monkeypatch, {})
    _o, prov = app._recovery_chain(_ctx(), [SimpleNamespace(track=2)], 32, 0)
    assert "recovery_ceiling_hit" not in prov


def test_the_abort_names_the_ceiling_and_the_chain() -> None:
    from cdda2img import rip_abort as ra

    track = SimpleNamespace(track=2, confidence_v1=None, confidence_v2=None)
    prov = {
        "recovery_ceiling_hit": "600",
        "recovery_chain_track_2": "track-ladder:miss",
    }
    with pytest.raises(ra.RipAborted) as exc:
        app._require_every_track_verified([track], {2: "unrecovered"}, prov)
    text = " ".join(exc.value.details)  # type: ignore[attr-defined]
    assert "recovery_ceiling_s" in text
    assert "track-ladder:miss" in text


# ---- each stage hands its track to the existing code ---------------------------------


def test_each_stage_reaches_the_code_that_runs_its_profile(monkeypatch) -> None:
    seen: dict[str, tuple | list | bool] = {}

    def ladder(device, results, lsns, last, pcm, resp, n, ladder, passes, *a, **k):
        seen["ladder"] = list(ladder)
        return {}, {}

    def strategy(kind, profile, **kw):
        seen["strategy"] = (kind, kw["fixed_speed"], kw["ladder"])
        return {}, {}

    def spans(*a, **k):
        seen["spans"] = True
        return {}, {}

    monkeypatch.setattr(app, "_recover_failed_tracks", ladder)
    monkeypatch.setattr(app, "_recover_by_strategy", strategy)
    monkeypatch.setattr(app, "_recover_flagged_spans", spans)
    monkeypatch.setattr(app, "_span_rung_decline", lambda *a: None)
    ctx = _ctx(disc_damage=_lane({150}), fixed_speed=24)
    r = [SimpleNamespace(track=2)]

    app._run_chain_stage("track-ladder", r, ctx)
    assert seen["ladder"] == [40, 32, 24, 8, 4]
    app._run_chain_stage("track-constant", r, ctx)
    assert seen["ladder"] == [24]  # the fixed recovery_speed, alone
    app._run_chain_stage("max-variation", r, ctx)
    assert seen["strategy"] == ("max-variation", 24, [40, 32, 24, 8, 4])
    app._run_chain_stage("span-fixed", r, ctx)
    strategy_seen = seen["strategy"]
    assert isinstance(strategy_seen, tuple)
    assert strategy_seen[0] == "span-fixed"
    app._run_chain_stage("span-flagged", r, ctx)
    assert seen["spans"] is True


def test_a_declined_span_stage_is_a_recorded_miss(monkeypatch) -> None:
    got, prov = app._run_chain_stage(
        "span-flagged", [SimpleNamespace(track=2)], _ctx(disc_damage=None)
    )
    assert got == {}
    assert prov == {"span_declined": "no_damage_map"}


# ---- wiring --------------------------------------------------------------------------


def test_every_profile_runs_through_the_chain() -> None:
    """No profile named: every stage. A named profile: that stage alone. Only the
    --ad-* escape hatch (no profile) takes the old single-ladder path."""
    src = inspect.getsource(app.rip_image)
    cond = src[
        src.rindex("if (", 0, src.index("_recovery_chain(")) : src.index(
            "_recovery_chain("
        )
    ]
    assert "bound.profile is not None" in cond
    assert 'bound.source == "builtin"' not in cond
    branch = src[src.index("and bound.profile is not None") - 200 :]
    assert "_recovery_chain(" in branch[:2500]
    assert 'only=None if bound.source == "builtin" else bound.profile.name' in branch


def test_a_named_profile_restricts_the_chain_to_its_stage(monkeypatch) -> None:
    calls = _fake_stages(monkeypatch, {})
    app._recovery_chain(
        _ctx(disc_damage=_lane({150})),
        [SimpleNamespace(track=2)],
        32,
        0,
        only="max-variation",
    )
    assert [c[0] for c in calls] == ["max-variation"]


def test_naming_whole_disc_runs_only_the_shared_stage(monkeypatch) -> None:
    calls = _fake_stages(monkeypatch, {})
    tracks = [SimpleNamespace(track=1), SimpleNamespace(track=2)]
    app._recovery_chain(_ctx(), tracks, 32, 0, only="whole-disc")
    assert calls == [("whole-disc", (1, 2))]


def test_stage_dispatch_follows_the_fields_not_the_name(monkeypatch, tmp_path) -> None:
    """A user profile that shadows a shipped name runs what its fields say."""
    from cdda2img import recovery_profile as R

    (tmp_path / "track-ladder.toml").write_text(
        'name = "track-ladder"\ngranularity = "sector"\nladder = "full"\n'
    )
    monkeypatch.setattr(R, "user_profiles_dir", lambda: tmp_path)
    seen: list[str] = []

    def strategy(kind, *_a, **_k):
        seen.append(kind)
        return {}, {}

    def ladder(*_a, **_k):
        seen.append("ladder")
        return {}, {}

    monkeypatch.setattr(app, "_recover_by_strategy", strategy)
    monkeypatch.setattr(app, "_recover_failed_tracks", ladder)
    app._run_chain_stage("track-ladder", [SimpleNamespace(track=2)], _ctx())
    assert seen == ["sector-hammer"]


def test_recovery_speed_is_a_cli_option_over_the_config(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["cdda2img", "rip", "--recovery-speed", "24"])
    assert app.parse_args().recovery_speed == 24
    monkeypatch.setattr("sys.argv", ["cdda2img", "rip"])
    assert app.parse_args().recovery_speed is None  # the config's value applies
    src = inspect.getsource(app.rip_image)
    # Every use: the initial fixed speed, the auto flag, and the requested speed
    # PROV records.
    assert src.count("recovery_speed or cfg.recovery_speed") == 3


# ---- recovery_speed = "auto": the rung that came closest (2026-11-21) -----------


@pytest.mark.parametrize(
    ("record", "best"),
    [
        ("40X:12,32X:3,24X:5", (32, 3)),
        ("40X:3,8X:3", (8, 3)),  # a tie goes to the slower rung
        ("24X:0", (24, 0)),
    ],
)
def test_fewest_c2(record: str, best: tuple[int, int]) -> None:
    assert app._fewest_c2(record) == best


def test_auto_starts_at_the_slowest_rung_until_there_is_evidence() -> None:
    assert app._initial_fixed_speed("auto", [40, 32, 8]) == 8
    assert app._initial_fixed_speed(32, [40, 32, 8]) == 32


def test_auto_holds_the_rung_the_ladder_found_closest(monkeypatch) -> None:
    held: list[int] = []

    def stage(name, results, ctx):
        t = results[0].track
        if name == "track-ladder":
            return {t: "unrecovered"}, {f"recovery_c2_track_{t}": "40X:9,24X:2,8X:4"}
        if name == "track-constant":
            held.append(ctx.fixed_speed)
        return {t: "unrecovered"}, {}

    monkeypatch.setattr(app, "_run_chain_stage", stage)
    ctx = _ctx(fixed_speed=4, auto_speed=True)
    _o, prov = app._recovery_chain(ctx, [SimpleNamespace(track=1)], "auto", 0)
    assert held == [24]
    assert prov["recovery_speed_track_1"] == "24X:c2=2"
    assert prov["recovery_speed"] == "auto"


def test_auto_without_ladder_evidence_says_so(monkeypatch) -> None:
    _fake_stages(monkeypatch, {})
    ctx = _ctx(fixed_speed=4, auto_speed=True)
    _o, prov = app._recovery_chain(
        ctx, [SimpleNamespace(track=1)], "auto", 0, only="track-constant"
    )
    assert prov["recovery_speed_track_1"] == "4X:no_c2_data"


def test_only_auto_asks_the_ladder_for_c2(monkeypatch) -> None:
    seen: list[bool] = []

    def ladder(*_a, c2=False, **_k):
        seen.append(c2)
        return {}, {}

    monkeypatch.setattr(app, "_recover_failed_tracks", ladder)
    r = [SimpleNamespace(track=2)]
    app._run_chain_stage("track-ladder", r, _ctx(auto_speed=True))
    app._run_chain_stage("track-ladder", r, _ctx(auto_speed=False))
    app._run_chain_stage("track-constant", r, _ctx(auto_speed=True))
    assert seen == [True, False, False]


@pytest.mark.parametrize(
    ("value", "ok"), [("auto", True), ("32", True), ("0", False), ("fast", False)]
)
def test_the_cli_accepts_a_rate_or_auto(monkeypatch, value: str, ok: bool) -> None:
    monkeypatch.setattr("sys.argv", ["cdda2img", "rip", "--recovery-speed", value])
    if ok:
        app.parse_args()
    else:
        with pytest.raises(SystemExit):
            app.parse_args()


def test_the_config_accepts_a_rate_or_auto() -> None:
    from cdda2img.validation import CONFIG_SCHEMA, validate

    assert validate({"recovery_speed": "auto"}, CONFIG_SCHEMA) == []
    assert validate({"recovery_speed": 24}, CONFIG_SCHEMA) == []
    assert validate({"recovery_speed": "fast"}, CONFIG_SCHEMA)
    assert validate({"recovery_speed": 0}, CONFIG_SCHEMA)
