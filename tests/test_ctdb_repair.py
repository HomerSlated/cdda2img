"""CTDB parity-repair policy tests.

The regression these exist for: every defect in the 2026-07-25 ABBA *Gold* failure
was invisible to a fixture whose track 1 INDEX 01 sits at LBA 0, because that is the
one case where CTDB's image ``[bounds[0], bounds[-1])`` and our PCM ``[0, lead-out)``
coincide. Every geometry fixture here therefore uses ``bounds[0] != 0``.
"""

from __future__ import annotations

import zlib
from pathlib import Path

import pytest

from cdda2img import ctdb_repair as C

_FRAME = 2352
_SPP = 588


def _pcm(frames: int) -> bytes:
    """Deterministic pseudo-random PCM, so every track window has a distinct CRC."""
    rng = 0x12345678
    out = bytearray()
    for _ in range(frames * _FRAME // 4):
        rng = (rng * 1103515245 + 12345) & 0xFFFFFFFF
        out += (rng >> 8).to_bytes(4, "little")[:4]
    return bytes(out[: frames * _FRAME])


def _entry(trackcrcs: list[int], stride: int = 10) -> C.Entry:
    return C.Entry(
        id="e1",
        confidence=1,
        npar=8,
        stride=stride,
        hasparity="http://example/1",
        trackcrcs=trackcrcs,
    )


def _all_crcs(pcm: bytes, bounds: list[int], n: int, stride: int) -> list[int]:
    crcs = [C.track_crc_at(pcm, t, 0, stride, bounds, n) for t in range(1, n + 1)]
    assert None not in crcs
    return [c for c in crcs if c is not None]


# ---- A1: laststride domain --------------------------------------------------


def test_last_track_trim_uses_the_ctdb_image_not_the_pcm_buffer() -> None:
    """laststride must come from [bounds[0], bounds[-1]), not from len(pcm)."""
    stride_wire, stride = 10, 20
    bounds = [33, 100, 200]  # track 1 INDEX 01 at LBA 33, as on ABBA *Gold*
    pcm = _pcm(200)  # our domain: [0, lead-out)

    image_words = (bounds[-1] - bounds[0]) * _SPP * 2
    laststride = stride + image_words % stride
    s = bounds[1] * _SPP
    e = bounds[2] * _SPP - laststride // 2
    expected = zlib.crc32(pcm[s * 4 : e * 4]) & 0xFFFFFFFF

    assert C.track_crc_at(pcm, 2, 0, stride_wire, bounds, 2) == expected

    # The pre-fix formula derived laststride from the buffer; on this geometry it
    # gives a different trim, which is precisely why the gate became unpassable.
    assert stride + (len(pcm) // 2) % stride != laststride


def test_trim_is_unchanged_when_track_one_starts_at_lba_zero() -> None:
    """Documents why the defect hid for so long: on a normal disc both agree."""
    stride_wire, stride = 10, 20
    bounds = [0, 100, 200]
    pcm = _pcm(200)

    image_words = (bounds[-1] - bounds[0]) * _SPP * 2
    assert stride + image_words % stride == stride + (len(pcm) // 2) % stride
    assert C.track_crc_at(pcm, 2, 0, stride_wire, bounds, 2) is not None


def test_first_track_trim_is_a_fixed_half_stride() -> None:
    stride_wire, stride = 10, 20
    bounds = [33, 100, 200]
    pcm = _pcm(200)
    s = bounds[0] * _SPP + stride // 2
    e = bounds[1] * _SPP
    assert (
        C.track_crc_at(pcm, 1, 0, stride_wire, bounds, 2)
        == zlib.crc32(pcm[s * 4 : e * 4]) & 0xFFFFFFFF
    )


def test_window_outside_the_pcm_returns_none() -> None:
    pcm = _pcm(100)
    assert C.track_crc_at(pcm, 2, 0, 10, [33, 50, 200], 2) is None


# ---- A2: erasure-bitmap domain ----------------------------------------------


def _c2_flagging(nsec: int, sector: int) -> bytes:
    """A 294 B/sector C2 capture with every word of *sector* flagged."""
    raw = bytearray(nsec * 294)
    raw[sector * 294 : (sector + 1) * 294] = b"\xff" * 294
    return bytes(raw)


def test_the_erasure_bitmap_is_built_over_the_whole_pcm_not_the_ctdb_image(
    tmp_path: Path,
) -> None:
    """ctanalyse converts to CTDB's image domain itself, skipping word_base/8 bytes.
    If this function were "fixed" to emit image-relative bits the shift would be
    applied twice and every erasure would land a pre-gap away from its damage."""
    nsec, sector = 100, 40
    c2 = tmp_path / "d.c2"
    c2.write_bytes(_c2_flagging(nsec, sector))

    bitmap = C.build_erasure_bitmap(c2, nsec * 1176, align_pairs=0)
    bits = [
        (bitmap[w // 8] >> (w % 8)) & 1
        for w in (sector * 1176, sector * 1176 + 1175, (sector - 1) * 1176)
    ]
    # absolute sector position flagged, its neighbour untouched
    assert bits == [1, 1, 0]


def test_repair_whole_disc_sizes_the_bitmap_from_the_pcm_buffer(
    _repair_harness: dict,
) -> None:
    """The caller-side half of the same contract: nwords must span [0, lead-out)."""
    h = _repair_harness
    seen: list[int] = []
    h["monkeypatch"].setattr(
        C,
        "build_erasure_bitmap",
        lambda p, nwords, align: seen.append(nwords) or b"\x00",
    )
    h["monkeypatch"].setattr(C, "_ctanalyse_and_verify", h["record"]([True]))
    _run(h, c2=True)
    assert seen == [h["pcm_path"].stat().st_size // 2]


# ---- B2: stale-binary guard -------------------------------------------------


def _fake_proc(stdout: str):
    class _P:
        returncode = 0
        stderr = ""

    _P.stdout = stdout  # type: ignore[attr-defined]
    return _P()


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ('{"can_recover": true}', "pre-fix binary emits no image_* keys"),
        ('{"can_recover": true, "image_first_frame": 0}', "analysed the wrong window"),
    ],
)
def test_a_ctanalyse_that_ignored_toc_is_refused(
    payload: str, why: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The binary is git-ignored but built from tracked source, so a stale local
    build is easy to end up with — and it fails by returning confident nonsense,
    not by erroring. Refusing is the only safe response."""
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: _fake_proc(payload), raising=True
    )
    with pytest.raises(RuntimeError, match="ignored --toc"):
        C.run_ctanalyse(
            tmp_path / "p.pcm",
            tmp_path / "par.bin",
            _entry([0, 0]),
            [33, 100, 200],
            None,
        )


# ---- C2: role-split verdict -------------------------------------------------


def test_verdict_passes_when_every_track_matches() -> None:
    bounds, n, stride = [33, 100, 200], 2, 10
    pcm = _pcm(200)
    sel = C.Selection(_entry(_all_crcs(pcm, bounds, n, stride), stride), 0, damaged=[1])
    verdict = C.verify_ctdb(pcm, sel, bounds, n)
    assert verdict.ok
    assert verdict.describe() == "all tracks match"


def test_a_damaged_track_that_still_mismatches_is_unfixed() -> None:
    bounds, n, stride = [33, 100, 200], 2, 10
    pcm = _pcm(200)
    crcs = _all_crcs(pcm, bounds, n, stride)
    crcs[0] ^= 0xFFFF  # track 1 will not match
    sel = C.Selection(_entry(crcs, stride), 0, damaged=[1])
    verdict = C.verify_ctdb(pcm, sel, bounds, n)
    assert not verdict.ok
    assert verdict.unfixed == [1]
    assert verdict.regressed == []
    assert "unfixed 1" in verdict.describe()


def test_a_clean_track_that_stops_matching_is_regressed() -> None:
    """A repair that breaks a track the selection called clean must be rejected."""
    bounds, n, stride = [33, 100, 200], 2, 10
    pcm = _pcm(200)
    crcs = _all_crcs(pcm, bounds, n, stride)
    crcs[1] ^= 0xFFFF  # track 2 was never damaged, yet no longer matches
    sel = C.Selection(_entry(crcs, stride), 0, damaged=[1])
    verdict = C.verify_ctdb(pcm, sel, bounds, n)
    assert not verdict.ok
    assert verdict.unfixed == []
    assert verdict.regressed == [2]


def test_an_unverifiable_track_abstains_rather_than_vetoing() -> None:
    """A track whose window falls outside the PCM must not fail an otherwise
    good repair — it carries no evidence either way."""
    bounds, n, stride = [33, 100, 400], 2, 10
    pcm = _pcm(200)  # lead-out claims 400 frames; we only have 200
    crcs = _all_crcs(pcm, [33, 100, 200], n, stride)
    sel = C.Selection(_entry(crcs, stride), 0, damaged=[1])
    verdict = C.verify_ctdb(pcm, sel, bounds, n)
    assert verdict.abstained == [2]
    assert verdict.unfixed == [] and verdict.regressed == []


# ---- D1: erasure-assisted then error-only -----------------------------------


@pytest.fixture
def _repair_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub everything around _ctanalyse_and_verify so only the attempt policy runs."""
    pcm_path = tmp_path / "disc.pcm"
    pcm_path.write_bytes(_pcm(200))
    c2_path = tmp_path / "disc.c2"
    c2_path.write_bytes(b"\x00" * 294)

    entry = _entry([0, 0], 10)
    monkeypatch.setattr(C, "load_entries", lambda *a, **k: [entry])
    monkeypatch.setattr(
        C, "select_entry", lambda *a, **k: C.Selection(entry, 0, damaged=[1])
    )
    monkeypatch.setattr(C, "fetch_parity", lambda e, cache: cache)
    monkeypatch.setattr(C, "build_erasure_bitmap", lambda *a, **k: b"\x00")

    calls: list[bool] = []

    def _record(outcomes: list[bool]):
        # used_c2 is read by keyword on purpose: a positional index would keep
        # passing after a signature reorder while asserting on the wrong parameter.
        def _fake(*args, **kwargs):
            used_c2 = kwargs["used_c2"]
            calls.append(used_c2)
            ok = outcomes[len(calls) - 1]
            return C.CtdbRepairResult(ok, "ok" if ok else "no", used_c2=used_c2)

        return _fake

    return {
        "pcm_path": pcm_path,
        "c2_path": c2_path,
        "calls": calls,
        "record": _record,
        "monkeypatch": monkeypatch,
    }


def _run(h: dict, c2: bool) -> C.CtdbRepairResult:
    return C.repair_whole_disc(
        h["pcm_path"],
        [33, 100],
        199,
        0x1234,
        30,
        c2_path=h["c2_path"] if c2 else None,
    )


def test_error_only_is_retried_when_the_erasure_attempt_fails(
    _repair_harness: dict,
) -> None:
    h = _repair_harness
    h["monkeypatch"].setattr(C, "_ctanalyse_and_verify", h["record"]([False, False]))
    result = _run(h, c2=True)
    assert h["calls"] == [True, False], "erasure-assisted first, then error-only"
    assert not result.repaired


def test_error_only_is_not_reached_when_erasures_succeed(_repair_harness: dict) -> None:
    h = _repair_harness
    h["monkeypatch"].setattr(C, "_ctanalyse_and_verify", h["record"]([True]))
    result = _run(h, c2=True)
    assert h["calls"] == [True]
    assert result.repaired


def test_without_a_c2_capture_only_error_only_runs(_repair_harness: dict) -> None:
    h = _repair_harness
    h["monkeypatch"].setattr(C, "_ctanalyse_and_verify", h["record"]([False]))
    _run(h, c2=False)
    assert h["calls"] == [False]


# ---- D2: the binary is optional ---------------------------------------------


def test_an_absent_ctanalyse_declines_the_repair_instead_of_raising(
    _repair_harness: dict,
) -> None:
    """ctanalyse is not shipped, so its absence must be an outcome, not a crash.

    Deliberately runs the *real* ``_ctanalyse_and_verify`` — the harness stubs it
    for the attempt-policy tests above, and a stub cannot tell you what the
    subprocess layer does with a `FileNotFoundError`. This is the case a
    developer machine can never check, because it has the binary.
    """
    h = _repair_harness
    before = h["pcm_path"].read_bytes()

    result = C.repair_whole_disc(
        h["pcm_path"],
        [33, 100],
        199,
        0x1234,
        30,
        ctanalyse_bin="/nonexistent/ctanalyse",
    )

    assert not result.repaired
    assert result.reason == "ctanalyse failed"
    assert h["pcm_path"].read_bytes() == before, (
        "a failed repair must not touch the PCM"
    )
