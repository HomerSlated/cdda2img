"""The flagged-span recovery rung (span-recovery-plan.md §4), offline.

Never run on hardware (kgr, 2026-09-24): these tests are the rung's only
validation, so they are built to fail on the mistakes that matter.

* The audio is a per-sector **ramp**, never silence: silence is also what a
  reader seeking to the wrong place produces.
* ``read_offset = +30``, so the corrected track straddles a sector boundary and
  the window carries a tail margin; a slicing error changes the bytes.
* The fake AccurateRip compares **actual bytes** against the key disc. A canned
  verdict would pass a rung that assembled the wrong audio.
* The merge requirement (§4.4/§6): a candidate that fails AR leaves the PCM file
  **byte-identical**. Moving the splice ahead of the gate must turn that test red.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cdda2img import cdda2img as C
from cdda2img.accudisc_reader import SpanDetail
from cdda2img.recovery_profile import load_profile

_SECTOR = 2352
_TRACK_LSNS = [0, 40, 100]
_LAST = 159  # disc_last_lsn; lead-out at 160
_OFFSET = 30
_DAMAGED = (60, 61, 75)  # all inside track 2's corrected range


def _key() -> bytes:
    """The true disc: every sector distinct, every byte position distinct."""
    return bytes((k * 31 + k // _SECTOR) % 256 for k in range((_LAST + 1) * _SECTOR))


KEY = _key()


def _damaged(key: bytes) -> bytes:
    raw = bytearray(key)
    for lba in _DAMAGED:
        off = lba * _SECTOR
        raw[off : off + _SECTOR] = bytes(b ^ 0xFF for b in raw[off : off + _SECTOR])
    return bytes(raw)


@pytest.fixture
def pcm(tmp_path: Path) -> Path:
    p = tmp_path / "rip.pcm"
    p.write_bytes(_damaged(KEY))
    return p


def _damage_map() -> bytes:
    lane = bytearray(_LAST + 1)
    for lba in _DAMAGED:
        lane[lba] = 1
    return bytes(lane)


def _detail(start: int, count: int, pcm: bytes, **over: object) -> SpanDetail:
    fields: dict = {
        "start_lba": start,
        "pcm": pcm,
        "c2": bytes(count * 294),
        "states": ("OK",) * count,
        "c2_clean": (True,) * count,
        "q_misposition": (False,) * count,
        "position_suspect": (False,) * count,
        "verify_passes": 2,
        "slips": 0,
        "sectors_flagged": 0,
        "sectors_recovered": 0,
        "speed_honoured_x": None,
    }
    fields.update(over)
    return SpanDetail(**fields)


def _true_ar(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """AccurateRip that matches only the track the KEY disc produces."""
    seen: list[bytes] = []

    def match(corrected: bytes, track: int, _n: int, _responses: list) -> tuple:
        seen.append(corrected)
        w = C._raw_track_window(_TRACK_LSNS, _LAST, track - 1, _OFFSET)
        want = C._corrected_from_window(
            KEY[w.lo * _SECTOR : w.hi * _SECTOR], w, _OFFSET
        )
        return (1, 2, 5, None) if corrected == want else (1, 2, None, None)

    monkeypatch.setattr("cdda2img.accuraterip.match_track_pcm", match)
    return seen


def _reads(monkeypatch: pytest.MonkeyPatch, source, **over: object) -> list[tuple]:
    """Fake span reader. *source(pass_no)* returns the disc bytes that pass sees."""
    calls: list[tuple] = []

    def read(_dev: str, start: int, count: int, **kw: object) -> SpanDetail:
        calls.append((start, count, kw.get("read_speed")))
        n_pass = sum(1 for c in calls if c[0] == start)
        disc = source(n_pass)
        return _detail(
            start, count, disc[start * _SECTOR : (start + count) * _SECTOR], **over
        )

    monkeypatch.setattr("cdda2img.accudisc_reader.read_span_detail", read)
    return calls


def _run(pcm: Path, ladder: list[int] | None = None) -> tuple[dict, dict]:
    return C._recover_flagged_spans(
        "/dev/null",
        [SimpleNamespace(track=2)],
        _TRACK_LSNS,
        _LAST,
        pcm,
        [["responses"]],
        len(_TRACK_LSNS),
        ladder or [],
        load_profile("span-flagged"),
        _OFFSET,
        _damage_map(),
        None,
    )


def test_a_clean_re_read_of_the_flagged_spans_recovers_the_track(
    pcm: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _true_ar(monkeypatch)
    _reads(monkeypatch, lambda _p: KEY)
    outcomes, prov = _run(pcm)
    assert outcomes == {2: "span_matched@p1"}
    assert pcm.read_bytes() == KEY  # every damaged sector repaired, nothing else moved
    assert prov == {"span_targets_track_2": "3/2"}


def test_only_the_padded_clusters_are_re_read(
    pcm: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """60-61 and 75 are 13 clean sectors apart, over span_gap=8, so they stay two
    spans; each is padded by span_pad=4. Not the whole 60-sector track."""
    _true_ar(monkeypatch)
    calls = _reads(monkeypatch, lambda _p: KEY)
    _run(pcm)
    assert [(s, n) for s, n, _ in calls] == [(56, 10), (71, 9)]


def test_a_candidate_that_fails_accuraterip_leaves_the_file_byte_identical(
    pcm: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE merge requirement. Every copy passes the acceptance rule and is wrong,
    which is exactly what the LITE-ON delivered (AccuDisc §18w). The gate must be
    the only thing between those copies and the file."""
    before = pcm.read_bytes()
    seen = _true_ar(monkeypatch)
    wrong = bytes(b ^ 0x55 for b in KEY)
    _reads(monkeypatch, lambda _p: wrong)
    outcomes, prov = _run(pcm)
    assert outcomes == {}
    assert len(seen) == 3  # the gate ran on every pass...
    assert pcm.read_bytes() == before  # ...and nothing reached the file
    assert prov["span_accepted_unverified_track_2"] == "3"
    assert "span_unresolved_track_2" not in prov


@pytest.mark.parametrize(
    "over",
    [
        {"states": ("SUSPECT",) * 10},
        {"states": ("C2",) * 10},
        {"states": ("HARD",) * 10},
        {"c2_clean": (False,) * 10},
        {"position_suspect": (True,) * 10},
    ],
    ids=["suspect", "c2", "hard", "c2-dirty", "position-suspect"],
)
def test_a_copy_failing_any_acceptance_condition_is_never_used(
    pcm: Path, monkeypatch: pytest.MonkeyPatch, over: dict
) -> None:
    """The bytes offered are CORRECT, so a rung that ignored the condition would
    recover the track. It must not: each condition is enforced on its own."""
    before = pcm.read_bytes()
    seen = _true_ar(monkeypatch)

    def read(_dev: str, start: int, count: int, **_kw: object) -> SpanDetail:
        fields = {k: v[:count] for k, v in over.items()}
        return _detail(
            start, count, KEY[start * _SECTOR : (start + count) * _SECTOR], **fields
        )

    monkeypatch.setattr("cdda2img.accudisc_reader.read_span_detail", read)
    outcomes, prov = _run(pcm)
    assert outcomes == {}
    assert seen == []  # nothing accepted, so nothing to gate
    assert pcm.read_bytes() == before
    assert prov["span_unresolved_track_2"] == "3"


def test_the_acceptance_rule_requires_a_position_witness() -> None:
    """Checked in the rule itself although the seam refuses less, so it is visible
    where it is applied."""
    good = _detail(0, 1, bytes(_SECTOR))
    assert C._span_copy_accepted(good, 0)
    assert not C._span_copy_accepted(_detail(0, 1, bytes(_SECTOR), verify_passes=1), 0)
    assert C._span_copy_accepted(
        _detail(0, 1, bytes(_SECTOR), states=("RECOVERED",)), 0
    )


def test_a_later_accepted_copy_replaces_an_earlier_wrong_one(
    pcm: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is retired before the gate passes. Keeping the first accepted copy
    would lock pass 1's wrong bytes in and the track could never match."""
    _true_ar(monkeypatch)
    wrong = bytes(b ^ 0x55 for b in KEY)
    calls = _reads(monkeypatch, lambda p: wrong if p == 1 else KEY)
    outcomes, _prov = _run(pcm, ladder=[4, 8, 16])
    assert outcomes == {2: "span_matched@p2"}
    assert pcm.read_bytes() == KEY
    # speed-diverse across passes, fastest first, one speed per pass
    assert [s for _, _, s in calls] == [16, 16, 8, 8]


def test_the_disc_final_sector_is_never_a_target(
    pcm: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flagged on every rip of the reference disc: a lead-out artefact, not damage."""
    _true_ar(monkeypatch)
    calls = _reads(monkeypatch, lambda _p: KEY)
    lane = bytearray(_LAST + 1)
    lane[_LAST] = 1
    outcomes, prov = C._recover_flagged_spans(
        "/dev/null",
        [SimpleNamespace(track=3)],
        _TRACK_LSNS,
        _LAST,
        pcm,
        [["responses"]],
        3,
        [],
        load_profile("span-flagged"),
        _OFFSET,
        bytes(lane),
        None,
    )
    assert calls == []
    assert outcomes == {}
    assert prov == {"span_targets_track_3": "0/0"}


# ---- dispatch ---------------------------------------------------------------


def _dispatch(profile, pcm: Path, **over: object) -> tuple:
    kwargs: dict = {
        "device": "/dev/null",
        "track_lsns": _TRACK_LSNS,
        "disc_last_lsn": _LAST,
        "pcm_file": pcm,
        "responses": [["responses"]],
        "ladder": [],
        "recovery_passes": 3,
        "read_offset": _OFFSET,
        "disc_damage": _damage_map(),
        "ui": None,
    }
    kwargs.update(over)
    failed = [SimpleNamespace(track=1), SimpleNamespace(track=2)]
    return C._run_span_rung(profile, failed, **kwargs)


def test_the_ladder_gets_only_the_tracks_the_span_rung_did_not_recover(
    pcm: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cdda2img.accudisc_reader.span_detail_supported", lambda: True)
    _true_ar(monkeypatch)
    _reads(monkeypatch, lambda _p: KEY)
    outcomes, prov, remaining = _dispatch(load_profile("span-flagged"), pcm)
    assert outcomes == {2: "span_matched@p1"}
    assert [r.track for r in remaining] == [1]  # track 1 had no flags
    assert prov["span_targets_track_1"] == "0/0"


@pytest.mark.parametrize("profile", ["track-ladder", None])
def test_a_non_span_profile_never_touches_the_span_rung(
    pcm: Path, monkeypatch: pytest.MonkeyPatch, profile: str | None
) -> None:
    """Control: the default path is unchanged."""

    monkeypatch.setattr(
        C, "_recover_flagged_spans", lambda *a, **k: pytest.fail("span rung ran")
    )
    outcomes, prov, remaining = _dispatch(
        load_profile(profile) if profile else None, pcm
    )
    assert (outcomes, prov) == ({}, {})
    assert [r.track for r in remaining] == [1, 2]


@pytest.mark.parametrize(
    ("over", "supported", "reason"),
    [
        ({"recovery_passes": 0}, True, "recovery_disabled"),
        ({"disc_damage": None}, True, "no_damage_map"),
        ({"responses": []}, True, "no_ar_responses"),
        ({}, False, "no_engine_support"),
    ],
)
def test_a_declined_rung_says_why_and_hands_every_track_on(
    pcm: Path,
    monkeypatch: pytest.MonkeyPatch,
    over: dict,
    supported: bool,
    reason: str,
) -> None:
    monkeypatch.setattr(
        "cdda2img.accudisc_reader.span_detail_supported", lambda: supported
    )
    monkeypatch.setattr(
        C, "_recover_flagged_spans", lambda *a, **k: pytest.fail("rung ran")
    )
    outcomes, prov, remaining = _dispatch(load_profile("span-flagged"), pcm, **over)
    assert outcomes == {}
    assert prov == {"span_declined": reason}
    assert [r.track for r in remaining] == [1, 2]


def test_rip_image_runs_the_span_rung_before_the_ladder() -> None:
    """Wiring control: the rung is useless if the rip path never calls it, and
    wrong if the ladder runs first or on the full failed list."""
    import inspect

    src = inspect.getsource(C.rip_image)
    span = src.index("_run_span_rung(")
    ladder = src.index("_recover_failed_tracks(")
    assert span < ladder
    assert "if recovery_ladder and failed_tracks:" in src
