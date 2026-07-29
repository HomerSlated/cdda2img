"""Whole-disc AccurateRip miss: diagnosed rather than silent.

The recovery ladder is gated on a PARTIAL mismatch, correctly — re-reading every
track is not a remedy for read errors. But that left the all-tracks case with no
recovery, no diagnosis and no PROV key: a rip that failed to verify with nothing
saying why. The Step-D CD-R is the worked example — a +30 read offset applied to a
disc burned uncorrected on the same drive shifted every track, so every track
missed, so the partial condition never held.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cdda2img import cdda2img as C


def _r(conf: int | None, v1: int | None = None, v2: int | None = None) -> object:
    return SimpleNamespace(max_confidence=conf, confidence_v1=v1, confidence_v2=v2)


def test_total_miss_is_every_in_db_track_failing() -> None:
    assert C._ar_has_total_mismatch([_r(10), _r(10), _r(10)])


def test_a_disc_absent_from_the_database_is_not_a_total_miss() -> None:
    """Different answer, and it must stay different. 'Not in AccurateRip' is a
    fact about the database; 'in it and matching nothing' is a fact about the
    rip. Conflating them would report an offset problem for an obscure pressing.
    """
    assert not C._ar_has_total_mismatch([_r(None), _r(None)])


def test_one_verified_track_is_not_a_total_miss() -> None:
    """That is the PARTIAL case, which the recovery ladder already owns. The two
    predicates must not both fire, or a disc gets re-read and diagnosed."""
    results = [_r(10, v1=5), _r(10), _r(10)]
    assert not C._ar_has_total_mismatch(results)
    assert C._ar_has_partial_mismatch(results)


def test_the_two_predicates_are_mutually_exclusive() -> None:
    for results in (
        [_r(10), _r(10)],  # total
        [_r(10, v1=5), _r(10)],  # partial
        [_r(10, v1=5), _r(10, v2=5)],  # all good
        [_r(None)],  # not in db
    ):
        assert not (
            C._ar_has_total_mismatch(results) and C._ar_has_partial_mismatch(results)
        )


def test_diagnosis_names_the_offset_delta_from_what_was_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actionable output: not 'it failed' but 'it would verify at -667'."""
    monkeypatch.setattr(
        "cdda2img.accuraterip.fetch_ar_responses",
        lambda *a, **k: ([[{"x": 1}]], "https", "b3"),
    )
    monkeypatch.setattr(
        "cdda2img.accuraterip.detect_offset",
        lambda *a, **k: [SimpleNamespace(offset=-667), SimpleNamespace(offset=-1333)],
    )
    prov = C._diagnose_total_ar_miss(Path("/x.pcm"), [0], 100, 0x123, read_offset=30)
    assert prov["ar_total_miss"] == "offset_mismatch"
    assert prov["ar_offset_candidates"] == "-667,-1333"
    assert prov["ar_offset_suggests"] == "-697"  # -667 - 30


def test_verifying_at_no_offset_is_reported_as_such(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In the database and matching nothing anywhere in the swept radius is a
    REAL result — damage or a different pressing — not a misconfiguration, and
    saying 'offset_mismatch' there would send the user after a phantom."""
    monkeypatch.setattr(
        "cdda2img.accuraterip.fetch_ar_responses",
        lambda *a, **k: ([[{"x": 1}]], "https", "b3"),
    )
    monkeypatch.setattr("cdda2img.accuraterip.detect_offset", lambda *a, **k: [])
    prov = C._diagnose_total_ar_miss(Path("/x.pcm"), [0], 100, 0x123, read_offset=30)
    assert prov == {"ar_total_miss": "no_offset_verifies"}


def test_a_failing_probe_never_fails_the_rip(monkeypatch: pytest.MonkeyPatch) -> None:
    """A diagnostic that can abort the thing it is diagnosing is worse than none:
    the rip has already succeeded at this point and the PCM is on disk."""

    def _boom(*_a: object, **_k: object) -> None:
        msg = "network gone"
        raise OSError(msg)

    monkeypatch.setattr("cdda2img.accuraterip.fetch_ar_responses", _boom)
    prov = C._diagnose_total_ar_miss(Path("/x.pcm"), [0], 100, 0x123, read_offset=30)
    assert prov == {"ar_total_miss": "offset_probe_failed"}
