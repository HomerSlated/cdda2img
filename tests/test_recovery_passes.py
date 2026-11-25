"""Sweeps and passes (kgr, 2026-11-24; the repeat count merged into
``recovery_passes`` on 2026-11-25).

"For the standard profiles, set the number of passes to the minimum needed to
cover all speeds ... Each full round of passes per profile = 1x attempt. Have a
config option --recovery-attempts set to 1 by default."

A pass is now one SWEEP: every target at every speed the profile reads at. How
many reads that is depends on the disc's admitted ladder and the track's flagged
sectors, so it is computed, not written in the TOML: every shipped profile has
``passes = 1``. ``recovery_attempts`` repeated the sweeps, inside one call, and
the first AccurateRip match still ends a track's reads.

2026-11-25, kgr: "merge recovery_attempts into recovery_passes (keep the name as
recovery_passes)". One setting, ``recovery_passes`` (config, or
``--recovery-passes``), default 1, 0 = no recovery.
"""

from __future__ import annotations

import inspect
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cdda2img import accudisc_reader as ar
from cdda2img import cdda2img as app
from cdda2img import recovery_profile as R
from cdda2img.validation import CONFIG_SCHEMA, PROFILE_SCHEMA, validate

_B = 2352
_LSNS = [0, 100, 200]
_LEADOUT = 300
_LADDER = [40, 32, 24, 8, 4]


def _good(lba: int) -> bytes:
    return lba.to_bytes(4, "little") * (_B // 4)


def _bad(lba: int) -> bytes:
    return (lba + 0x10000).to_bytes(4, "little") * (_B // 4)


def _good_track(track: int) -> bytes:
    lo = _LSNS[track - 1]
    hi = _LSNS[track] if track < len(_LSNS) else _LEADOUT
    return b"".join(_good(lba) for lba in range(lo, hi))


class _Drive:
    """A fake ``read_span_windows``: bad bytes until read *good_from* (1-based),
    good from then on. Each read advances the fake clock by *secs*."""

    def __init__(self, good_from: int | None = None, secs: float = 0.0) -> None:
        self.calls: list[dict[str, Any]] = []
        self.good_from = good_from
        self.secs = secs
        self.now = 0.0

    def __call__(self, device, start, count, windows, read_speed=None, **kw):
        self.calls.append({"start": start, "count": count, "speed": read_speed})
        self.now += self.secs
        good = self.good_from is not None and len(self.calls) >= self.good_from
        src = _good if good else _bad
        pieces = []
        for lo, hi in windows:
            a, b = max(lo, start), min(hi, start + count)
            pieces.append(b"".join(src(x) for x in range(a, b)) if a < b else b"")
        return ar.WindowsRead(
            pieces=pieces, speed_honoured_x=read_speed, cache_defeat="done"
        )


def _match(corrected, track, n_tracks, responses):
    return (None, None, 200 if corrected == _good_track(track) else None, None)


def _profile(**fields: Any) -> R.Profile:
    data = {"name": "p", **fields}
    assert validate(data, PROFILE_SCHEMA) == [], fields
    return R.Profile.from_dict(data)


def _strategy(
    monkeypatch,
    tmp_path: Path,
    profile: R.Profile,
    *,
    flagged: set[int],
    drive: _Drive,
    failed: tuple[int, ...] = (2,),
    sweeps: int | None = None,
    fixed_speed: int | None = None,
):
    monkeypatch.setattr(ar, "read_span_windows", drive)
    monkeypatch.setattr("cdda2img.accuraterip.match_track_pcm", _match)
    monkeypatch.setattr(time, "monotonic", lambda: drive.now)
    pcm = tmp_path / "disc.pcm"
    # The capture is good except at the flagged sectors, so a good re-read of
    # those (or, for whole-disc, of everything) is what makes a track match.
    pcm.write_bytes(
        b"".join(_bad(x) if x in flagged else _good(x) for x in range(_LEADOUT))
    )
    lane = bytes(1 if x in flagged else 0 for x in range(_LEADOUT))
    from cdda2img.strategy_reads import strategy_kind

    kind = strategy_kind(profile)
    assert kind is not None
    return app._recover_by_strategy(
        kind,
        profile,
        device="/dev/sr0",
        failed_tracks=[SimpleNamespace(track=t) for t in failed],
        track_lsns=_LSNS,
        disc_last_lsn=_LEADOUT - 1,
        pcm_file=pcm,
        responses=[["block"]],
        ladder=_LADDER,
        read_offset=0,
        disc_damage=lane,
        ui=None,
        seed=1,
        fixed_speed=fixed_speed,
        sweeps=sweeps,
    )


# ---- the shipped profiles ------------------------------------------------------


_SHIPPED = sorted(p.stem for p in R.shipped_profiles_dir().glob("*.toml"))


@pytest.mark.parametrize("name", _SHIPPED)
def test_every_shipped_profile_runs_one_sweep_per_pass(name: str) -> None:
    """One sweep already tries every speed, so 1 is the minimum; more effort
    comes from recovery_passes, the same for every profile. Loaded from the
    shipped file itself, so a user profile of the same name cannot answer."""
    path = R.shipped_profiles_dir() / f"{name}.toml"
    assert R.load_profile(name, profiles={name: path}).passes == 1


def test_one_pass_is_the_schema_default() -> None:
    assert R.Profile.from_dict({"name": "p"}).passes == 1


# ---- how long a sweep is ------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "flagged", "fixed", "reads"),
    [
        # targets x rungs
        ({"granularity": "sector", "ladder": "full"}, {150}, None, 5),
        ({"granularity": "sector", "ladder": "full"}, {120, 150, 180}, None, 15),
        (
            {"granularity": "sector", "ladder": "full", "run_up": 8},
            {150, 190},
            None,
            10,
        ),
        (
            {"granularity": "sector", "ladder": "full", "variation": "full"},
            {150, 190},
            None,
            10,
        ),
        # one held speed: one read per target
        ({"granularity": "sector", "ladder": "single", "span": 64}, {150, 190}, 24, 2),
        # nothing flagged: the track's first sector is the one target
        ({"granularity": "sector", "ladder": "full"}, set(), None, 5),
    ],
)
def test_a_sweep_is_every_target_at_every_rung(
    monkeypatch, tmp_path, fields, flagged, fixed, reads
) -> None:
    drive = _Drive()
    _strategy(
        monkeypatch,
        tmp_path,
        _profile(**fields),
        flagged=flagged,
        drive=drive,
        fixed_speed=fixed,
    )
    assert len(drive.calls) == reads


def test_a_whole_disc_sweep_is_one_read_per_rung(monkeypatch, tmp_path) -> None:
    drive = _Drive()
    _strategy(
        monkeypatch,
        tmp_path,
        _profile(granularity="whole-disc", ladder="full"),
        flagged=set(),
        drive=drive,
    )
    assert [c["speed"] for c in drive.calls] == _LADDER
    assert all((c["start"], c["count"]) == (0, _LEADOUT) for c in drive.calls)


# ---- passes --------------------------------------------------------------------------


def test_passes_repeat_the_sweep(monkeypatch, tmp_path) -> None:
    drive = _Drive()
    _strategy(
        monkeypatch,
        tmp_path,
        _profile(granularity="sector", ladder="full"),
        flagged={150},
        drive=drive,
        sweeps=3,
    )
    assert [c["speed"] for c in drive.calls] == _LADDER * 3


@pytest.mark.parametrize(
    ("stage", "how"),
    [
        ("sector-hammer", "strategy"),
        ("whole-disc", "strategy"),
        ("track-ladder", "ladder"),
        ("track-constant", "ladder"),
        ("span-flagged", "spans"),
    ],
)
def test_the_chain_hands_every_stage_profile_passes_times_recovery_passes(
    monkeypatch, stage: str, how: str
) -> None:
    seen: dict[str, int] = {}

    def ladder(device, results, lsns, last, pcm, resp, n, ladder, passes, *a, **k):
        seen["ladder"] = passes
        return {}, {}

    def strategy(kind, profile, **kw):
        seen["strategy"] = kw["sweeps"]
        return {}, {}

    def spans(*a, **kw):
        seen["spans"] = kw["sweeps"]
        return {}, {}

    monkeypatch.setattr(app, "_recover_failed_tracks", ladder)
    monkeypatch.setattr(app, "_recover_by_strategy", strategy)
    monkeypatch.setattr(app, "_recover_flagged_spans", spans)
    monkeypatch.setattr(app, "_span_rung_decline", lambda *a: None)
    ctx = app._ChainContext(
        device="/dev/sr0",
        track_lsns=_LSNS,
        disc_last_lsn=_LEADOUT - 1,
        pcm_file=Path("/nonexistent"),
        responses=[["block"]],
        ladder=_LADDER,
        fixed_speed=32,
        read_offset=0,
        disc_damage=bytes(1 if x == 150 else 0 for x in range(_LEADOUT)),
        ui=None,
        cohort=None,
        recovery_passes=3,
    )
    app._run_chain_stage(stage, [SimpleNamespace(track=2)], ctx)
    # passes 1 x recovery_passes 3: the same 3 sweeps kgr's series ran with
    # recovery_attempts = 3 before the merge, so the merge changes no stage.
    assert seen[how] == 3


# ---- success = early exit ------------------------------------------------------------


def test_a_match_ends_the_reads_however_many_passes_remain(
    monkeypatch, tmp_path
) -> None:
    drive = _Drive(good_from=2)
    outcomes, _prov = _strategy(
        monkeypatch,
        tmp_path,
        _profile(granularity="sector", ladder="full"),
        flagged={150},
        drive=drive,
        sweeps=3,
    )
    assert outcomes == {2: "strategy_matched@a2"}
    assert len(drive.calls) == 2  # not the 15 that three passes allow


def test_whole_disc_stops_once_every_track_has_matched(monkeypatch, tmp_path) -> None:
    drive = _Drive(good_from=2)
    outcomes, _prov = _strategy(
        monkeypatch,
        tmp_path,
        _profile(granularity="whole-disc", ladder="full"),
        flagged=set(),
        drive=drive,
        failed=(1, 3),
        sweeps=3,
    )
    assert outcomes == {1: "strategy_matched@a2", 3: "strategy_matched@a2"}
    assert len(drive.calls) == 2


def test_the_speed_ladder_stops_at_its_first_match(monkeypatch, tmp_path) -> None:
    calls: list[int] = []

    def attempt(device, lsns, last, idx, off, speed, *a, **k):
        calls.append(speed)
        hit = (_good_track(2), f"matched@{speed}X") if len(calls) == 2 else None
        return hit, f"{speed}>{speed}:done", None

    monkeypatch.setattr(app, "_ladder_attempt", attempt)
    pcm = tmp_path / "disc.pcm"
    pcm.write_bytes(bytes(_LEADOUT * _B))
    outcomes, _prov = app._recover_failed_tracks(
        "/dev/sr0",
        [SimpleNamespace(track=2)],
        _LSNS,
        _LEADOUT - 1,
        pcm,
        [["block"]],
        3,
        _LADDER,
        3,  # sweeps: passes 1 x recovery_passes 3
        0,
        None,
    )
    assert outcomes == {2: "matched@32X"}
    assert calls == [40, 32]


def test_the_span_rung_stops_at_its_first_match(monkeypatch, tmp_path) -> None:
    passes: list[int | None] = []
    monkeypatch.setattr(
        app, "_span_pass", lambda dev, spans, prof, speed, *a, **k: passes.append(speed)
    )
    monkeypatch.setattr(
        app,
        "_span_gate",
        lambda *a, **k: _good_track(2) if len(passes) == 2 else None,
    )
    pcm = tmp_path / "disc.pcm"
    pcm.write_bytes(bytes(_LEADOUT * _B))
    profile = _profile(granularity="span", ladder="full", verify_passes=2)
    outcomes, _prov = app._recover_flagged_spans(
        "/dev/sr0",
        [SimpleNamespace(track=2)],
        _LSNS,
        _LEADOUT - 1,
        pcm,
        [["block"]],
        3,
        _LADDER,
        profile,
        0,
        bytes(1 if x == 150 else 0 for x in range(_LEADOUT)),
        None,
        sweeps=3,
    )
    assert outcomes == {2: "span_matched@p2"}
    assert passes == [40, 32]


def test_a_span_sweep_is_one_pass_per_rung(monkeypatch, tmp_path) -> None:
    passes: list[int | None] = []
    monkeypatch.setattr(
        app, "_span_pass", lambda dev, spans, prof, speed, *a, **k: passes.append(speed)
    )
    monkeypatch.setattr(app, "_span_gate", lambda *a, **k: None)
    pcm = tmp_path / "disc.pcm"
    pcm.write_bytes(bytes(_LEADOUT * _B))
    app._recover_flagged_spans(
        "/dev/sr0",
        [SimpleNamespace(track=2)],
        _LSNS,
        _LEADOUT - 1,
        pcm,
        [["block"]],
        3,
        _LADDER,
        _profile(granularity="span", ladder="full", verify_passes=2),
        0,
        bytes(1 if x == 150 else 0 for x in range(_LEADOUT)),
        None,
        sweeps=2,
    )
    assert passes == _LADDER * 2


# ---- the time allowance grows with the plan --------------------------------------------


def test_budget_s_is_per_read_so_it_never_cuts_a_planned_sweep(
    monkeypatch, tmp_path
) -> None:
    """Each read takes 1.5 s of fake time and budget_s is 2: as a per-track cap
    the old reading stopped after two reads; per read, the whole sweep runs."""
    drive = _Drive(secs=1.5)
    _strategy(
        monkeypatch,
        tmp_path,
        _profile(granularity="sector", ladder="full", budget_s=2),
        flagged={150},
        drive=drive,
    )
    assert len(drive.calls) == 5


def test_a_stalled_drive_still_hits_the_time_bound(monkeypatch, tmp_path) -> None:
    """Control: reads far slower than budget_s each, so the bound does bite."""
    drive = _Drive(secs=10.0)
    _strategy(
        monkeypatch,
        tmp_path,
        _profile(granularity="sector", ladder="full", budget_s=2),
        flagged={150},
        drive=drive,
    )
    assert len(drive.calls) < 5


# ---- the option ------------------------------------------------------------------------


def test_recovery_passes_defaults_to_one_and_zero_turns_recovery_off() -> None:
    from cdda2img.config import Config

    assert Config().recovery_passes == 1
    assert validate({"recovery_passes": 3}, CONFIG_SCHEMA) == []
    assert validate({"recovery_passes": 0}, CONFIG_SCHEMA) == []  # the kill switch
    for bad in (-1, 101, "3", True):
        assert validate({"recovery_passes": bad}, CONFIG_SCHEMA), bad


def test_recovery_attempts_is_no_longer_a_config_key() -> None:
    """Merged into recovery_passes (kgr, 2026-11-25). A config still naming it is
    refused, loudly, rather than read as something it no longer is."""
    assert validate({"recovery_attempts": 3}, CONFIG_SCHEMA)


@pytest.mark.parametrize(
    ("value", "ok"),
    [("3", 3), ("1", 1), ("0", 0), ("-1", None), ("101", None), ("x", None)],
)
def test_the_cli_option(monkeypatch, value: str, ok: int | None) -> None:
    monkeypatch.setattr("sys.argv", ["cdda2img", "rip", "--recovery-passes", value])
    if ok is None:
        with pytest.raises(SystemExit):
            app.parse_args()
    else:
        assert app.parse_args().recovery_passes == ok


def test_recovery_attempts_is_no_longer_an_option(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["cdda2img", "rip", "--recovery-attempts", "3"])
    with pytest.raises(SystemExit) as exc:
        app.parse_args()
    assert isinstance(exc.value, SystemExit)
    assert exc.value.code == 1


@pytest.mark.parametrize(
    ("cli", "configured", "want"),
    [(None, 3, 3), (0, 3, 0), (2, 0, 2), (None, 0, 0), (5, 1, 5)],
)
def test_the_cli_overrides_the_config_and_zero_is_a_value(
    cli: int | None, configured: int, want: int
) -> None:
    """`--recovery-passes 0` over a config of 3 is no recovery, not 3: the CLI is
    tested against None, never for truth."""
    assert app._effective_recovery_passes(cli, configured) == want


def test_the_rip_uses_the_one_effective_value_everywhere(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["cdda2img", "rip"])
    assert app.parse_args().recovery_passes is None
    src = inspect.getsource(app.rip_image)
    assert "_effective_recovery_passes(recovery_passes, cfg.recovery_passes)" in src
    # Nothing else may read the config value past the CLI override.
    assert src.count("cfg.recovery_passes") == 1
    assert "ar_responses and passes > 0" in src  # the kill switch
    assert src.count("recovery_passes=passes") == 2  # chain context, span rung
    assert 'provenance["recovery_passes"] = str(passes)' in src
    # The old setting is gone from the code (comments keep its history).
    assert "cfg.recovery_attempts" not in src
    assert '"recovery_attempts"' not in src  # the PROV key
    assert "attempts=attempts" not in src
    assert "recovery_passes=args.recovery_passes" in inspect.getsource(app._dispatch)
