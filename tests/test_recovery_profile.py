"""Recovery profiles and the speed-ladder binder (accudisc-migration-plan.md §9.2-9.4).

Everything here is pure: the ladder binder is exercised through mocked `accudisc
speeds` rows, so the two real drive x disc measurements taken on the PX-716A are
preserved as fixtures rather than needing hardware to reproduce.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cdda2img import drive_speed
from cdda2img import recovery_profile as R
from cdda2img.accudisc_reader import SpeedRow
from cdda2img.validation import PROFILE_SCHEMA, validate

# Two real `accudisc speeds` captures from the same drive on the same disc, months
# apart. They are the evidence that the ladder is a property of drive AND disc.
#
# Both predate AccuDisc's `verdict=` token, so they carry no verdict — deliberately
# left that way rather than back-filled. They are what rule 2 (`req == page2a`) has
# to keep working for, and inventing verdicts they never had would delete the only
# regression cover the fallback has.
_PX716A_JULY = [
    SpeedRow(40, 32, 32.0),
    SpeedRow(32, 32, 32.0),
    SpeedRow(24, 24, 24.0),
    SpeedRow(16, 8, 8.0),
    SpeedRow(8, 8, 8.0),
    SpeedRow(4, 4, 4.0),
]
_PX716A_DEGRADED = [
    SpeedRow(40, 8, 8.01),
    SpeedRow(32, 8, 8.01),
    SpeedRow(24, 8, 8.01),
    SpeedRow(16, 8, 8.01),
    SpeedRow(8, 8, 8.01),
    SpeedRow(4, 4, 4.01),
]

# Tracy, 2026-07-29, uncap latched — the capture that closed the §9.3 known gap.
# Note req=48 measured 22.96 while req=40 measured 23.68: page 2A advertises the
# 48x DATA ceiling, CD-DA is governed to 40x, so those are one speed wearing two
# labels and the faster-LOOKING rung is the slower one. `req == page2a` admits
# both because both operands come from that same advertised ceiling.
_PX716A_TRACY_VERDICTS = [
    SpeedRow(48, 48, 22.96, "duplicate"),
    SpeedRow(40, 40, 23.68, "admitted"),
    SpeedRow(32, 32, 19.46, "admitted"),
    SpeedRow(24, 24, 14.93, "admitted"),
    SpeedRow(16, 8, 8.01, "quantized"),
    SpeedRow(8, 8, 8.01, "admitted"),
    SpeedRow(4, 4, 4.01, "admitted"),
]


@pytest.fixture
def _no_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    """admitted_ladder captures the entry speed and restores it; no drive here."""
    monkeypatch.setattr(drive_speed, "current_speed_x", lambda device: 40)
    monkeypatch.setattr(
        drive_speed, "restore_drive_speed", lambda device, target=None: None
    )


def _ladder(monkeypatch: pytest.MonkeyPatch, rows: list[SpeedRow]) -> list[int]:
    monkeypatch.setattr(drive_speed, "read_speed_rows", lambda device: rows)
    return drive_speed.admitted_ladder("/dev/null")


# ---- §9.3 ladder binder -----------------------------------------------------


def test_only_rungs_the_drive_honoured_exactly_are_admitted(
    monkeypatch: pytest.MonkeyPatch, _no_restore: None
) -> None:
    """The plan's worked example. 40 and 16 were quantised, so they are dropped:
    a row labelled 40x that actually read at 32x mislabels every measurement."""
    assert _ladder(monkeypatch, _PX716A_JULY) == [32, 24, 8, 4]


def test_the_same_drive_admits_less_on_a_degraded_disc(
    monkeypatch: pytest.MonkeyPatch, _no_restore: None
) -> None:
    """Measured on the PX-716A with ABBA *Gold* after it degraded: the governor caps
    every request at 8x. Same drive, same probe, different answer — which is why a
    ladder must never be cached per drive."""
    assert _ladder(monkeypatch, _PX716A_DEGRADED) == [8, 4]


def test_the_verdict_rule_drops_a_rung_that_req_equals_page2a_admits(
    monkeypatch: pytest.MonkeyPatch, _no_restore: None
) -> None:
    """The §9.3 known gap, closed. This is the whole reason the rule changed.

    req=48 measured 22.96 and req=40 measured 23.68 on the same disc: page 2A
    advertises the 48x DATA ceiling while CD-DA is governed to 40x, so 48 is not a
    rung — it is 40 with a wrong label, and a SLOWER measured rate. The old rule
    admitted it because both of its operands come from that same advertised
    ceiling, so the equality cross-checks the drive's quantiser and never its
    ceiling. Asserting the old answer too, so this test states what changed rather
    than merely what is.
    """
    old_rule = sorted(
        {
            r.page2a
            for r in _PX716A_TRACY_VERDICTS
            if r.requested == r.page2a and r.page2a
        },
        reverse=True,
    )
    assert old_rule == [48, 40, 32, 24, 8, 4]  # what we shipped until 2026-07-29

    assert _ladder(monkeypatch, _PX716A_TRACY_VERDICTS) == [40, 32, 24, 8, 4]


def test_quantized_and_duplicate_are_both_refused(
    monkeypatch: pytest.MonkeyPatch, _no_restore: None
) -> None:
    """They fail for different reasons and both must be out.

    `quantized` read at 8x under a 16x label — admitting it mislabels every
    measurement taken there. `duplicate:40` read at the right rate under a rung
    that is not distinct (`duplicate`) — admitting it makes the ladder sweep one speed twice and
    call the results independent.
    """
    ladder = _ladder(monkeypatch, _PX716A_TRACY_VERDICTS)
    assert 16 not in ladder  # quantized
    assert 48 not in ladder  # duplicate


def test_an_all_unknown_verdict_set_falls_through_rather_than_degrading(
    monkeypatch: pytest.MonkeyPatch, _no_restore: None
) -> None:
    """`unknown` is a verdict, and it is truthy — the trap AccuDisc named in ce.3.

    At points=1 every rung comes back UNKNOWN because nothing was JUDGED, which is
    not the same as nothing being admissible. Gating the verdict branch on "a
    verdict is present" would send this to an empty ladder and then to the degrade
    guard, reporting one rung at max for a drive that plainly has four.
    """
    rows = [
        SpeedRow(40, 40, 23.7, "unknown"),
        SpeedRow(32, 32, 19.5, "unknown"),
        SpeedRow(8, 8, 8.0, "unknown"),
        SpeedRow(4, 4, 4.0, "unknown"),
    ]
    assert _ladder(monkeypatch, rows) == [40, 32, 8, 4]


def test_rows_without_any_verdict_still_use_the_old_rule(
    monkeypatch: pytest.MonkeyPatch, _no_restore: None
) -> None:
    """An older engine reports no verdict at all. Rule 2 must still be reachable."""
    assert all(r.verdict is None for r in _PX716A_JULY)
    assert _ladder(monkeypatch, _PX716A_JULY) == [32, 24, 8, 4]


def test_a_drive_with_no_page_2a_falls_back_to_measured_throughput(
    monkeypatch: pytest.MonkeyPatch, _no_restore: None
) -> None:
    """page2a == 0 means the page did not report, NOT "quantised to zero". Applying
    the equality test to it admits nothing and leaves the ladder silently empty —
    the hole AccuDisc found in the first version of this rule."""
    rows = [
        SpeedRow(40, 0, 24.0),
        SpeedRow(24, 0, 24.0),
        SpeedRow(8, 0, 8.0),
        SpeedRow(4, 0, 4.0),
    ]
    assert _ladder(monkeypatch, rows) == [40, 8, 4]  # the two 24.0 rows collapse


def test_an_empty_ladder_is_not_a_reachable_state(
    monkeypatch: pytest.MonkeyPatch, _no_restore: None
) -> None:
    """A drive reporting a real page2a that never equals req: non-zero, so the
    fallback does not fire, and the strict rule admits nothing. The guard is on the
    OUTCOME precisely because the causes are open-ended."""
    monkeypatch.setattr(drive_speed, "read_drive_speed", lambda device: (176, 7056))
    rows = [SpeedRow(40, 20, 20.0), SpeedRow(32, 20, 20.0), SpeedRow(8, 10, 10.0)]
    assert _ladder(monkeypatch, rows) == [40]  # 7056 // 176


def test_no_probe_rows_at_all_still_yields_one_rung(
    monkeypatch: pytest.MonkeyPatch, _no_restore: None
) -> None:
    monkeypatch.setattr(drive_speed, "read_drive_speed", lambda device: (None, 1408))
    assert _ladder(monkeypatch, []) == [8]


def test_the_probe_leaves_the_drive_throttled_so_the_binder_restores_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`accudisc speeds` performs real timed reads and ends at its last rung (4x).
    Forgetting the restore would silently run the whole rip at 4x.

    The restore is now TARGETED at the rate captured before the probe, not at max —
    otherwise a rip that asked for `--ad-speed 8` would have its recovery ladder run
    after an unrequested blast to 40x.
    """
    restored: list[tuple[str, int | None]] = []
    monkeypatch.setattr(drive_speed, "current_speed_x", lambda device: 8)
    monkeypatch.setattr(drive_speed, "read_speed_rows", lambda device: _PX716A_DEGRADED)
    monkeypatch.setattr(
        drive_speed,
        "restore_drive_speed",
        lambda device, target=None: restored.append((device, target)),
    )
    drive_speed.admitted_ladder("/dev/sr0")
    assert restored == [("/dev/sr0", 8)]


def test_speed_rows_expose_the_fields_the_ladder_policy_reads() -> None:
    """The row shape the ladder policy depends on.

    This used to assert against the CLI's `speed req=… page2a=… measured=…` text
    via the seam's regex. With the CLI retired there is no text to parse — the
    library returns structs — so what is worth pinning is the *shape the policy
    consumes*, which is what the old test was really standing in for. Positional
    reads are asserted alongside the names because `SpeedRow` is a NamedTuple and
    existing call sites unpack it.
    """
    from cdda2img.accudisc_reader import SpeedRow

    row = SpeedRow(requested=40, page2a=8, measured=8.01, verdict="quantized")
    assert (row.requested, row.page2a, row.measured) == (40, 8, 8.01)
    assert row[:3] == (40, 8, 8.01)
    assert SpeedRow(4, 4, 4.01).verdict is None, "absent verdict is None, not a string"


# ---- shipped profiles -------------------------------------------------------


def test_all_shipped_profiles_ship() -> None:
    """§9.1 retired the pre-measurement names; the first seven are the strategies
    the bench actually ranked, and the set must stay complete or the ranking loses
    its controls (sector-hammer anchors the low end of the variation axis).
    span-flagged is the eighth and is NOT a bench arm: opt-in, never run on
    hardware (span-recovery-plan.md §7; the 2026-09-24 ruling against running it
    was rescinded 2026-11-21)."""
    assert set(R.list_profiles()) == {
        "track-ladder",
        "track-constant",
        "max-variation",
        "whole-disc",
        "sector-runup",
        "sector-hammer",
        "span-fixed",
        "span-flagged",
    }


# From the shipped directory, never list_profiles(): parametrize runs at
# collection, before any fixture isolates XDG_CONFIG_HOME, so list_profiles()
# picked up the machine's own user profiles (kgr's *-x20 copies, 2026-11-22) and
# then failed to load them under the isolated run.
_SHIPPED = sorted(p.stem for p in R.shipped_profiles_dir().glob("*.toml"))


@pytest.mark.parametrize("name", _SHIPPED)
def test_every_shipped_profile_validates(name: str) -> None:
    """A shipped profile that fails its own schema would break every rip using it,
    and `setup` writes user profiles through the same validator."""
    profile = R.load_profile(name)
    assert profile.name == name
    assert validate(profile.__dict__, PROFILE_SCHEMA) == []


def test_the_builtin_default_is_the_bench_winner() -> None:
    assert R.BUILTIN_PROFILE == "track-ladder"
    assert R.load_profile(R.BUILTIN_PROFILE).ladder == "full"


def test_the_experimental_arms_are_flagged_and_the_shipped_ones_are_not() -> None:
    flags = {n: R.load_profile(n).experimental for n in R.list_profiles()}
    assert {n for n, e in flags.items() if e} == {
        "sector-runup",
        "sector-hammer",
        "span-fixed",
        "span-flagged",  # never run on hardware, so it says so in `--list-profiles`
        # kgr, 2026-11-21: experimental until a hardware run of the rip-path
        # strategy code (cdda2img.strategy_reads) reproduces the bench.
        "max-variation",
        "whole-disc",
    }


def test_an_unknown_profile_names_what_is_available(tmp_path: Path) -> None:
    with pytest.raises(R.ProfileError, match="unknown recovery profile 'nope'"):
        R.load_profile("nope")


def test_an_invalid_profile_is_refused_never_defaulted(tmp_path: Path) -> None:
    """Silently substituting a default would mislabel every measurement taken after."""
    bad = tmp_path / "bad.toml"
    bad.write_text('name = "bad"\npasses = 0\n')
    with pytest.raises(R.ProfileError, match="is invalid"):
        R.load_profile("bad", profiles={"bad": bad})


def test_a_profile_whose_name_field_disagrees_with_its_filename_is_refused(
    tmp_path: Path,
) -> None:
    """Resolution is by filename but PROV records the name field; letting them differ
    means the log says one strategy ran and another did."""
    p = tmp_path / "alpha.toml"
    p.write_text('name = "beta"\n')
    with pytest.raises(R.ProfileError, match="must agree"):
        R.load_profile("alpha", profiles={"alpha": p})


def test_a_malformed_toml_file_reports_the_path(tmp_path: Path) -> None:
    p = tmp_path / "x.toml"
    p.write_text("name = \n")
    with pytest.raises(R.ProfileError, match="could not read profile"):
        R.load_profile("x", profiles={"x": p})


# ---- §9.4 resolution --------------------------------------------------------


def test_no_flags_and_no_config_gives_the_builtin() -> None:
    """Not bare AccuDisc flags: those are effectively R0 (--retries defaults to 2),
    a real floor but well below track-ladder's measured 19/20."""
    s = R.resolve_recovery()
    assert s.source == "builtin"
    assert s.profile is not None and s.profile.name == "track-ladder"


def test_an_explicit_profile_outranks_the_config_default() -> None:
    s = R.resolve_recovery(profile_name="whole-disc", config_default="max-variation")
    assert s.profile is not None
    assert (s.source, s.profile.name) == ("profile", "whole-disc")


def test_the_config_default_is_used_when_no_profile_is_requested() -> None:
    s = R.resolve_recovery(config_default="max-variation")
    assert s.profile is not None
    assert (s.source, s.profile.name) == ("config-default", "max-variation")


def test_ad_flags_bypass_profiles_entirely_rather_than_merging() -> None:
    """A blend of an escape hatch and a profile is a configuration neither the user
    nor the profile author asked for, and PROV could not name it honestly."""
    s = R.resolve_recovery(
        ad_flags={"ladder": "32,16"},
        profile_name="whole-disc",
        config_default="max-variation",
    )
    assert s.source == "ad-flags"
    assert s.profile is None
    assert s.ad_flags == {"ladder": "32,16"}


def test_absent_ad_flags_do_not_count_as_supplied() -> None:
    """argparse hands over every --ad-* key with None when unset; treating those as
    present would fire rung 1 on literally every invocation."""
    s = R.resolve_recovery(ad_flags={"ladder": None, "speed": None})
    assert s.source == "builtin"


def test_an_unknown_ad_flag_is_refused() -> None:
    with pytest.raises(R.ProfileError, match="unknown --ad-\\* flag"):
        R.resolve_recovery(ad_flags={"turbo": 1})


@pytest.mark.parametrize(
    "flag", ["--ad-retries", "--ad-c2-retries", "--ad-verify", "--ad-overlap"]
)
def test_the_deleted_ad_flags_are_not_options(monkeypatch, flag: str) -> None:
    """kgr, 2026-11-25: deleted, because no read ever received them. The command
    line now refuses them (exit 1) instead of recording them in PROV."""
    from cdda2img import cdda2img as app

    monkeypatch.setattr("sys.argv", ["cdda2img", "rip", flag, "2"])
    with pytest.raises(SystemExit) as exc:
        app.parse_args()
    assert isinstance(exc.value, SystemExit)
    assert exc.value.code == 1


def test_ad_recovery_is_not_an_option(monkeypatch) -> None:
    """The raw flag string had nothing in AccuDisc to reach (their 2026-11-25a B.5)."""
    from cdda2img import cdda2img as app

    monkeypatch.setattr("sys.argv", ["cdda2img", "rip", "--ad-recovery", "x"])
    with pytest.raises(SystemExit) as exc:
        app.parse_args()
    assert isinstance(exc.value, SystemExit)
    assert exc.value.code == 1


def test_ad_ladder_and_ad_speed_remain(monkeypatch) -> None:
    from cdda2img import cdda2img as app

    monkeypatch.setattr(
        "sys.argv", ["cdda2img", "rip", "--ad-ladder", "32,8", "--ad-speed", "8"]
    )
    args = app.parse_args()
    assert (args.ad_ladder, args.ad_speed) == ("32,8", 8)


# ---- ladder binding (2026-11-21: the chain decides each read's speed) ----------


def test_every_profile_binds_the_whole_admitted_ladder_fastest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recovery chain steps a `ladder = "full"` stage through these rungs and
    snaps a `"single"` one's `recovery_speed` to one of them, so every profile
    needs all of them (kgr, 2026-11-21). Fastest first is the 2026-10-22 ruling."""
    monkeypatch.setattr(
        "cdda2img.drive_speed.admitted_ladder", lambda _d: [8, 40, 4, 24, 32]
    )
    for name in ("track-ladder", "track-constant", "span-fixed", "whole-disc"):
        bound = R.bind_ladder(R.resolve_recovery(profile_name=name), "/dev/sr0")
        assert list(bound.ladder) == [40, 32, 24, 8, 4], name


def test_a_profile_has_no_speed_field_any_more() -> None:
    """Replaced by config/CLI `recovery_speed` (kgr, 2026-11-21): a profile saying
    `speed = "min"` would be ignored by the chain, so it is refused, not read."""
    errors = validate({"name": "p", "speed": "max"}, PROFILE_SCHEMA)
    assert [e.where for e in errors] == ["speed"]


# ---- §9.7 profile creation guards -------------------------------------------


def test_setup_refuses_a_name_that_collides_with_a_shipped_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """Shipped names are reserved. Shadowing `track-ladder` with a local file makes
    every later measurement labelled `track-ladder` incomparable with the bench that
    named it — and there is deliberately no --force."""
    from cdda2img import setup as S

    monkeypatch.setattr(S, "_text", lambda *a, **k: "track-ladder")
    monkeypatch.setattr(R, "user_profiles_dir", lambda: tmp_path)
    assert S._section_create_profile() is False
    assert "already exists" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["Track Ladder", "my profile", "up!", "MyProfile"])
def test_setup_refuses_an_illegal_name_rather_than_mangling_it(
    name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """Silent lowercasing would let two distinct names map to one file, and the
    loser would vanish without a message."""
    from cdda2img import setup as S

    monkeypatch.setattr(S, "_text", lambda *a, **k: name)
    monkeypatch.setattr(R, "user_profiles_dir", lambda: tmp_path)
    assert S._section_create_profile() is False
    assert "Invalid name" in capsys.readouterr().out
    assert not list(tmp_path.glob("*.toml"))


def test_setup_writes_a_profile_that_loads_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Creation goes through the same validator as loading, so a profile cannot be
    born invalid."""
    from cdda2img import setup as S

    monkeypatch.setattr(S, "_text", lambda *a, **k: "my-profile")
    monkeypatch.setattr(S, "_select", lambda *a, **k: "track-constant")
    monkeypatch.setattr(R, "user_profiles_dir", lambda: tmp_path)
    assert S._section_create_profile() is True

    written = tmp_path / "my-profile.toml"
    assert written.is_file()
    loaded = R.load_profile("my-profile", profiles={"my-profile": written})
    assert loaded.name == "my-profile"
    assert loaded.ladder == "single"  # inherited from track-constant
    assert loaded.experimental is False  # a derived profile is not a bench control


def test_the_profile_listing_keeps_its_columns_for_a_long_user_name() -> None:
    """`rip --list-profiles` sized its name column at 16, so a user profile such as
    `track-constant-x20` (18) pushed its own row out of line (kgr, 2026-11-22)."""
    from pathlib import Path

    from cdda2img.cdda2img import _profile_listing

    long_name = "track-constant-x20"
    rows = [
        (n, R.load_profile(n), Path(f"/p/{n}.toml"))
        for n in ("track-ladder", "whole-disc")
    ]
    rows.append((long_name, R.load_profile("track-constant"), Path("/u/x.toml")))
    lines = [ln for ln in _profile_listing(rows) if not ln.startswith("  best for:")]
    heads, paths = lines[0::2], lines[1::2]

    def gran_col(line: str, name: str) -> int:
        rest = line[len(name) :]
        return len(name) + len(rest) - len(rest.lstrip())

    assert len({h.index("ladder=") for h in heads}) == 1
    grans = {gran_col(h, n) for h, (n, _p, _x) in zip(heads, rows, strict=True)}
    assert len(grans) == 1  # granularity column
    assert {len(p) - len(p.lstrip()) for p in paths} == grans  # path under it
    assert all(line == line.rstrip() for line in lines)
    assert "(experimental)" in heads[1] and "(experimental)" not in heads[0]


# ---- best_for: what damage each profile is for (kgr, 2026-11-25) ---------------------


def test_every_shipped_profile_says_what_damage_it_is_for() -> None:
    for name in sorted(_SHIPPED):
        assert R.load_profile(name).best_for, name


def test_the_listing_shows_best_for_under_each_profile() -> None:
    from pathlib import Path

    from cdda2img.cdda2img import _profile_listing

    prof = R.load_profile("sector-runup")
    lines = _profile_listing([("sector-runup", prof, Path("/p/x.toml"))])
    assert lines[2] == f"  best for: {prof.best_for}"
    assert all(len(line) <= 80 for line in lines[2:])


def test_a_profile_without_best_for_lists_as_before() -> None:
    import dataclasses
    from pathlib import Path

    from cdda2img.cdda2img import _profile_listing

    prof = dataclasses.replace(R.load_profile("track-ladder"), best_for="")
    assert len(_profile_listing([("track-ladder", prof, Path("/p/x.toml"))])) == 2


@pytest.mark.parametrize("value", ["two\nlines", "x" * 69])
def test_best_for_must_be_one_short_line(value: str) -> None:
    errors = validate({"name": "p", "best_for": value}, PROFILE_SCHEMA)
    assert [e.where for e in errors] == ["best_for"]
    assert validate({"name": "p", "best_for": "x" * 68}, PROFILE_SCHEMA) == []
