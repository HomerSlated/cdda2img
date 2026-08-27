"""
test_write_offset.py — the pulse geometry is a CROSS-PROJECT CONTRACT.

This file exists for one reason: `write_offset.py`'s four geometry constants are
half of a wire format shared with AccuDisc, and until 2026-08-27 nothing but a
comment said so. A comment is a claim; this is a guard.

The failure it prevents is silent in the worst way. Trimming the 75-second signal
to save scratch space, or moving pulse B, leaves every other test here passing
and simply stops AccuDisc's locator finding the second pulse — so the only
blank-free validation either project has would break with nothing to report it.
AccuDisc hit the mirror image of this: they had documented the interop property
since 0.20.0, but every buffer in their suite came from their own generator, so a
correlating "optimisation" would have passed everything.
"""

from __future__ import annotations

import pytest

from cdda2img.write_offset import (
    _DURATION,
    _FRAME_BYTES,
    _PULSE_A,
    _PULSE_B,
    _PULSE_LEN,
    _PULSE_SEED,
)

# The contract, spelled out rather than imported, so this test states the
# agreement independently of the module it checks. Importing the values and
# comparing them to themselves would pass unconditionally.
_CONTRACT_SAMPLES = 3_307_500  # 75 s
_CONTRACT_PULSE_A = 44_100  # 1.0 s
_CONTRACT_PULSE_B = 2_646_000  # 60.0 s
_CONTRACT_PULSE_LEN = 588  # one CD frame


def test_geometry_matches_the_cross_project_contract() -> None:
    """The four constants AccuDisc's locator depends on. Changing one is breaking.

    If this fails you have changed a wire format, not a tuning parameter. The
    remedy is a message on the correspondence channel before the commit, not an
    update to the numbers below.
    """
    assert _DURATION == _CONTRACT_SAMPLES
    assert _PULSE_A == _CONTRACT_PULSE_A
    assert _PULSE_B == _CONTRACT_PULSE_B
    assert _PULSE_LEN == _CONTRACT_PULSE_LEN


def test_signal_byte_length_is_exactly_what_the_locator_demands() -> None:
    """13,230,000 bytes exactly — their locator refuses anything else.

    Derived, not restated: it is the product of two constants above, so this
    catches a change to either one via the quantity that actually reaches the
    other project. AccuDisc's `write_offset_locate` raises rather than measures
    on a wrong length, deliberately — a short read-back would put pulse B off the
    end and silently reduce the measurement to a single pulse, losing the
    two-pulse cross-check entirely.
    """
    assert _DURATION * _FRAME_BYTES == 13_230_000


def test_the_seed_is_not_part_of_the_contract() -> None:
    """Documents the OTHER half of the split, which is easy to get backwards.

    Both locators are threshold detectors, not matched filters — measured
    2026-08-27, when AccuDisc's located our full-scale seed-42 noise despite
    their own generator emitting a half-scale burst. So the waveform is free and
    only its POSITIONS are agreed. This asserts the seed is merely an int: it is
    here to carry the sentence, since "you may change this" has no falsifiable
    form. If you are tempted to add the seed to the contract test above, this is
    the note saying don't.
    """
    assert isinstance(_PULSE_SEED, int)


def test_geometry_agrees_with_accudisc_itself() -> None:
    """The real guard: compare against AccuDisc's own published constants.

    The literals above catch drift on OUR side. This catches drift on EITHER,
    which is the failure that matters, since the contract is only meaningful as
    an agreement between two trees.

    Skipped rather than failed without the binding — CI has no AccuDisc — and a
    skip is loud, which an uncollected test is not.

    Resolved through the seam's own `_import_binding` rather than a bare
    `pytest.importorskip("accudisc")`. That matters: the package is not on
    `sys.path` until `_binding_search_path()` puts it there, so a bare import
    skips even on a machine where the binding is present and working — leaving
    the one guard that can catch drift on AccuDisc's side inert exactly where it
    could have run. Measured: bare importorskip skipped here; this does not.
    """
    from cdda2img.accudisc_reader import _import_binding

    accudisc, why_not = _import_binding()
    if accudisc is None:
        pytest.skip(f"AccuDisc binding not available: {why_not}")
    assert _DURATION == accudisc.WOFF_SAMPLES
    assert _PULSE_A == accudisc.WOFF_PULSE_A
    assert _PULSE_B == accudisc.WOFF_PULSE_B
    assert _PULSE_LEN == accudisc.WOFF_PULSE_LEN
