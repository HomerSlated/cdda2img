"""`--ad-speed` and `--ad-ladder`: the flags that were parsed and then dropped.

Both were fully plumbed as far as PROV and no further. `--ad-speed` never reached
the whole-disc read (`_rip_disc_stage` did not take it, so it could not have), and
*any* `--ad-*` flag silently disabled AccurateRip recovery — including
`--ad-ladder`, whose only purpose is to name the ladder that was being discarded.

The through-line of these tests is that a flag has to be observable at the place it
acts. Asserting `resolve_recovery` returns the right object proves nothing about
whether anything reads it, so the wiring tests below read the source and pin the
hand-over points.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from cdda2img import cdda2img, recovery_profile
from cdda2img.recovery_profile import ProfileError, resolve_recovery

# ── --ad-speed composes; every other --ad-* still excludes ───────────────────


def test_ad_speed_alone_does_not_bypass_the_profile() -> None:
    """The exception kgr carved out, and the reason it is not an inconsistency.

    Every other --ad-* flag describes RECOVERY, which is what a profile describes,
    so the two compete and one must win outright. --ad-speed sets the INITIAL pass
    rate, which no profile has an opinion about — `Profile.speed` is a ladder-rung
    selector consumed only by `rungs_for`. There is nothing to conflict with.
    """
    s = resolve_recovery(ad_flags={"speed": 8}, config_default=None)
    assert s.read_speed == 8
    assert s.source == "builtin", "--ad-speed must not fire the ad-flags rung"
    assert s.profile is not None, "a profile must survive --ad-speed"
    assert s.ad_flags == {}, "speed is lifted out; it is not a recovery knob"


def test_ad_speed_rides_along_with_a_real_bypassing_flag() -> None:
    """When another flag DOES bypass, the speed still has to arrive."""
    s = resolve_recovery(ad_flags={"speed": 8, "retries": 7})
    assert s.source == "ad-flags"
    assert s.read_speed == 8
    assert s.ad_flags == {"retries": 7}


def test_ad_speed_survives_with_ladder() -> None:
    """`with_ladder` rebuilds the dataclass positionally; a field appended without
    being threaded through would be silently reset to its default here."""
    s = resolve_recovery(ad_flags={"speed": 8}).with_ladder([32, 16])
    assert s.read_speed == 8
    assert s.ladder == (32, 16)


def test_other_ad_flags_still_bypass_entirely() -> None:
    s = resolve_recovery(ad_flags={"retries": 7}, config_default=None)
    assert s.source == "ad-flags"
    assert s.profile is None


# ── bounds: reject, never clamp ──────────────────────────────────────────────


@pytest.mark.parametrize("bad", [0, -1, 73, 1000])
def test_out_of_range_speed_is_refused(bad: int) -> None:
    """A clamp here would be `--ad-retries 256` in different clothes: that value
    parses fine, is cut to a uint8_t, and reaches the drive as 2 — the opposite of
    what was asked, with the rip carrying on and mislabelled."""
    with pytest.raises(ProfileError, match="ad-speed"):
        resolve_recovery(ad_flags={"speed": bad})


def test_a_bool_is_not_a_speed() -> None:
    """Python's bool-is-an-int wart: `True` would otherwise arrive as 1X."""
    with pytest.raises(ProfileError, match="integer"):
        resolve_recovery(ad_flags={"speed": True})


def test_unknown_flags_are_still_rejected_when_speed_is_present() -> None:
    """Order matters inside resolve_recovery: `speed` is popped AFTER the
    unknown-key check, or it would become the one flag nobody validates."""
    with pytest.raises(ProfileError, match="turbo"):
        resolve_recovery(ad_flags={"speed": 8, "turbo": 1})


# ── --ad-ladder ──────────────────────────────────────────────────────────────


def test_ad_ladder_parses_in_the_order_given() -> None:
    """NOT sorted. `"4,32"` (slow first, then fast) is a different experiment from
    `"32,4"`, and a caller who wrote one must not silently get the other."""
    assert recovery_profile.parse_ad_ladder("4,32,8") == [4, 32, 8]


def test_ad_ladder_keeps_duplicates() -> None:
    """Repeating a rung is how you spend more attempts at it — a real thing to want
    from a flag whose whole point is direct control."""
    assert recovery_profile.parse_ad_ladder("8,8,4") == [8, 8, 4]


@pytest.mark.parametrize("bad", ["", "8,,4", "fast", "8,0", "8,99"])
def test_bad_ad_ladder_is_refused(bad: str) -> None:
    with pytest.raises(ProfileError):
        recovery_profile.parse_ad_ladder(bad)


def test_ad_ladder_reaches_the_recovery_ladder() -> None:
    """The defect itself: this used to yield `()` because bind_ladder returned
    early on `profile is None`, which the ad-flags rung is by construction."""
    s = resolve_recovery(ad_flags={"ladder": "32,16,8"})
    bound = recovery_profile.bind_ladder(s, "/dev/null")
    assert bound.ladder == (32, 16, 8)


def test_ad_ladder_is_not_filtered_against_the_admitted_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This rung is the escape hatch for driving the engine directly. A user asking
    for a rung our admission policy rejects is exactly what the hatch is for — so
    the drive must not even be probed here."""
    from cdda2img import drive_speed

    probed = "the drive must not be probed on the ad-flags rung"

    def boom(device: str) -> list[int]:
        raise AssertionError(probed)

    monkeypatch.setattr(drive_speed, "admitted_ladder", boom)
    s = resolve_recovery(ad_flags={"ladder": "48,3"})
    assert recovery_profile.bind_ladder(s, "/dev/null").ladder == (48, 3)


def test_ad_flags_without_a_ladder_invent_nothing() -> None:
    """`--ad-retries 7` alone still means no recovery ladder. Fixing the ladder
    gap must not turn every bypassing flag into a silent ladder."""
    s = resolve_recovery(ad_flags={"retries": 7})
    assert recovery_profile.bind_ladder(s, "/dev/null").ladder == ()


# ── the hand-over points, read from the source ───────────────────────────────


def _tree(fn: object) -> ast.AST:
    return ast.parse(textwrap.dedent(inspect.getsource(fn)))  # type: ignore[arg-type]


def _kwargs_of_call(tree: ast.AST, name: str) -> set[str]:
    out: set[str] = set()
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else None
        )
        if called == name:
            found = True
            out |= {kw.arg for kw in node.keywords if kw.arg}
    assert found, f"no call to {name} found; this guard would pass vacuously"
    return out


def test_the_disc_read_is_actually_given_the_speed() -> None:
    """`read_disc_c2` has accepted `read_speed` all along and forwards it as
    `speed_x`. The gap was here: the rip never passed it, so the flag was parsed,
    validated, recorded in PROV, and consulted by nothing that touches the drive."""
    assert "read_speed" in _kwargs_of_call(
        _tree(cdda2img._rip_disc_stage), "read_disc_c2"
    )


def test_the_rip_hands_the_strategys_speed_to_the_read_stage() -> None:
    assert "read_speed" in _kwargs_of_call(_tree(cdda2img.rip_image), "_rip_disc_stage")


def test_the_recovery_gate_does_not_key_on_a_profile() -> None:
    """`strategy.profile is not None` was a fine test for "can I call rungs_for"
    and the wrong one for "should recovery run" — the ad-flags rung has no profile
    by construction, so any --ad-* flag turned recovery off."""
    src = textwrap.dedent(inspect.getsource(cdda2img.rip_image))
    bind = [ln for ln in src.splitlines() if "bind_ladder" in ln and "#" not in ln]
    assert bind, "cannot locate the bind_ladder call"
    window = src[src.index(bind[0]) : src.index(bind[0]) + 400]
    assert "strategy.profile is not None" not in window


def test_the_speed_restore_is_in_a_finally() -> None:
    """It must run on the failure path too, and on the clean-rip path that never
    enters recovery — the old restore sat after the recovery loop, so neither
    was covered."""
    tree = _tree(cdda2img.rip_image)
    in_finally = [
        node
        for try_node in ast.walk(tree)
        if isinstance(try_node, ast.Try)
        for stmt in try_node.finalbody
        for node in ast.walk(stmt)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "restore_drive_speed"
    ]
    assert in_finally, "the read-speed restore is not on an unconditional exit path"


# ── _apply_read_speed: the only read-back in the shipping path ───────────────


class _FakeDrive:
    """Stands in for `drive_speed`, recording requests and reporting a rate."""

    def __init__(self, reports: int | None, accepts: bool = True) -> None:
        self.reports = reports
        self.accepts = accepts
        self.requested: list[int] = []
        #: Queries made AFTER a request — the read-back signal. Counted rather
        #: than flagged so a second one is visible too.
        self.queried = 0

    def request_speed(self, device: str, nx: int) -> bool:
        self.requested.append(nx)
        return self.accepts

    def current_speed_x(self, device: str) -> int | None:
        if self.requested:
            self.queried += 1
        return self.reports


def _patch_drive(monkeypatch: pytest.MonkeyPatch, fake: _FakeDrive) -> None:
    from cdda2img import drive_speed

    monkeypatch.setattr(drive_speed, "request_speed", fake.request_speed)
    monkeypatch.setattr(drive_speed, "current_speed_x", fake.current_speed_x)


def test_no_request_means_query_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default and by far the common case: the drive keeps its own management
    and we must not send it anything at all."""
    fake = _FakeDrive(reports=40)
    _patch_drive(monkeypatch, fake)
    assert cdda2img._apply_read_speed("/dev/sr0", None) == 40
    assert fake.requested == []


def test_a_request_is_issued_before_the_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The request stays ours even though the verification no longer is.

    It has to happen before the read for the spin-up line to say anything true, and
    it lets the drive spin up at the target rate rather than changing mid-stream.
    """
    fake = _FakeDrive(reports=8)
    _patch_drive(monkeypatch, fake)
    assert cdda2img._apply_read_speed("/dev/sr0", 8) == 8
    assert fake.requested == [8]


def test_no_page_2a_read_back_after_a_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retired 2026-08-09 in favour of `ReadStats.speed_honoured_x`.

    Ours read back BEFORE handing off, and `Device.read` sets `speed_x` again inside
    its own handle at the head of the read — so our number described the drive at a
    moment the authoritative request then superseded. Two measurements of one
    quantity, and ours was the earlier and weaker one.

    Pinned by counting the queries: a read-back would make this two (one for the
    return value, one to verify), and the point is that there is now only the one
    the None-branch needs.
    """
    fake = _FakeDrive(reports=40)
    _patch_drive(monkeypatch, fake)
    assert cdda2img._apply_read_speed("/dev/sr0", 8) == 8, (
        "must return the REQUEST; a measured rate here means the read-back is back"
    )
    assert fake.queried == 0, "the drive was interrogated after the request"


def test_a_refused_command_falls_back_to_a_plain_query(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Still ours to report, and distinct from quantization: a refused command means
    the requested rate was never installed at all, where a quantized one means the
    drive took it and snapped it down. The engine can only tell us about the second.
    """
    fake = _FakeDrive(reports=40, accepts=False)
    _patch_drive(monkeypatch, fake)
    with caplog.at_level("WARNING"):
        assert cdda2img._apply_read_speed("/dev/sr0", 8) == 40
    assert "refused" in caplog.text
