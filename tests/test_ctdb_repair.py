"""CTDB parity-repair policy tests.

The regression these exist for: every defect in the 2026-07-25 ABBA *Gold* failure
was invisible to a fixture whose track 1 INDEX 01 sits at LBA 0, because that is the
one case where CTDB's image ``[bounds[0], bounds[-1])`` and our PCM ``[0, lead-out)``
coincide. Every geometry fixture here therefore uses ``bounds[0] != 0``.
"""

from __future__ import annotations

import re
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


def _report(
    audio: bytes | None = None,
    audio_unverified: bytes | None = None,
    **kw: int,
) -> C.CtdbRepairReport:
    """An AccuDisc repair report. Both buffers default to None — a refusal."""
    fields: dict = {
        "offset_pairs": 0,
        "dirty_columns": 1,
        "repaired_columns": 1,
        "refused_columns": 0,
        "erasure_columns": 0,
        "unverified_columns": 0,
        "corrections": 1,
        "crc32_before": 0,
        "crc32_after": 0,
    }
    fields.update(kw)
    return C.CtdbRepairReport(audio=audio, audio_unverified=audio_unverified, **fields)


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
    """The decoder converts to CTDB's image domain itself, skipping word_base/8 bytes.
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
    h["monkeypatch"].setattr(C, "_repair_and_verify", h["record"]([True]))
    _run(h, c2=True)
    assert seen == [h["pcm_path"].stat().st_size // 2]


# ---- B2: the image window is an argument, not a parsed string ---------------


def test_the_ctdb_image_window_is_passed_as_integers_not_a_toc_string(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """This replaces the stale-binary guard, and the reason it can is the point.

    ``ctanalyse`` took the window as a ``--toc`` string; a build predating
    2026-07-25 parsed it, ignored it, analysed ``[0, lead-out)`` and returned
    confident nonsense, so the old test asserted that a reported
    ``image_first_frame`` disagreeing with ``bounds[0]`` was refused. There is no
    equivalent failure now — the window is two integers handed to a linked
    library, and a build that disagrees about the struct raises ``AbiMismatch``
    instead. What is left worth pinning is that the arithmetic reaching the seam
    is right, above all ``image_frames``, which is a *length* and not a bound.
    """
    seen: dict = {}
    monkeypatch.setattr(
        C.accudisc_reader, "ctdb_repair", lambda **kw: seen.update(kw) or _report()
    )
    parity = tmp_path / "par.bin"
    parity.write_bytes(b"\x00" * 8)

    C.run_repair(b"\x00" * 16, parity, _entry([0, 0]), [33, 100, 200], -639, None)

    assert seen["image_first_frame"] == 33
    assert seen["image_frames"] == 200 - 33, "a length, not bounds[-1]"
    assert seen["offset_pairs"] == -639, "our sweep's offset, passed through"
    assert seen["erasures"] is None, "error-only is a mode, not a degrade"


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
    h["monkeypatch"].setattr(C, "_repair_and_verify", h["record"]([False, False]))
    result = _run(h, c2=True)
    assert h["calls"] == [True, False], "erasure-assisted first, then error-only"
    assert not result.repaired


def test_error_only_is_not_reached_when_erasures_succeed(_repair_harness: dict) -> None:
    h = _repair_harness
    h["monkeypatch"].setattr(C, "_repair_and_verify", h["record"]([True]))
    result = _run(h, c2=True)
    assert h["calls"] == [True]
    assert result.repaired


def test_without_a_c2_capture_only_error_only_runs(_repair_harness: dict) -> None:
    h = _repair_harness
    h["monkeypatch"].setattr(C, "_repair_and_verify", h["record"]([False]))
    _run(h, c2=False)
    assert h["calls"] == [False]


# ---- D2: an unavailable engine is an outcome, not a crash -------------------


def test_an_unimportable_binding_declines_the_repair_instead_of_raising(
    _repair_harness: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing engine must decline, and this is the one place in the seam where
    that is true.

    Everywhere else a missing binding is deliberately **fatal** (kgr's 2026-08-01
    ruling: there is no second carrier, so a rip that cannot reach the engine must
    stop rather than quietly do something else). CTDB repair is the exception,
    because it is one exit of the recovery ladder and the AR re-read ladder below
    it is still worth trying — "the decoder is absent" is a fact about this
    disc's recovery, not a reason to abandon the rip.

    Deliberately runs the *real* ``_repair_and_verify`` and the *real* seam,
    faulting only the binding import, so the ``RuntimeError`` that must be caught
    is the one ``_binding()`` actually raises. This used to fault an absent
    ``ctanalyse`` binary; the invariant survived the migration, its spelling did
    not. A developer machine can never reach this state on its own — it has the
    binding installed.
    """
    h = _repair_harness
    before = h["pcm_path"].read_bytes()
    monkeypatch.setattr(
        C.accudisc_reader, "_import_binding", lambda: (None, "no module named accudisc")
    )

    result = C.repair_whole_disc(h["pcm_path"], [33, 100], 199, 0x1234, 30)

    assert not result.repaired
    assert result.reason == "parity repair failed"
    assert h["pcm_path"].read_bytes() == before, (
        "a failed repair must not touch the PCM"
    )


# ---- D3: three outcomes, and the weaker success stays distinct --------------


@pytest.fixture
def _gated_harness(_repair_harness: dict) -> dict:
    """`_repair_harness` with both gates forced open, so only the outcome triage runs."""
    h = _repair_harness
    h["monkeypatch"].setattr(C, "verify_ctdb", lambda *a, **k: C.CtdbVerdict())
    h["monkeypatch"].setattr(C, "verify_ar", lambda *a, **k: True)
    return h


def _repaired_pcm(h: dict) -> bytes:
    return b"\xa5" * h["pcm_path"].stat().st_size


def test_a_refusal_commits_nothing_and_leaves_the_pcm_alone(
    _gated_harness: dict,
) -> None:
    """Both buffers None. A refusal is a normal answer about a damaged disc."""
    h = _gated_harness
    before = h["pcm_path"].read_bytes()
    h["monkeypatch"].setattr(C, "run_repair", lambda *a, **k: _report())

    result = C.repair_whole_disc(h["pcm_path"], [33, 100], 199, 0x1234, 30)

    assert not result.repaired
    assert result.reason == "damage exceeds RS capacity"
    assert h["pcm_path"].read_bytes() == before


def test_a_verified_repair_commits_and_records_no_unverified_columns(
    _gated_harness: dict,
) -> None:
    h = _gated_harness
    out = _repaired_pcm(h)
    h["monkeypatch"].setattr(
        C, "run_repair", lambda *a, **k: _report(audio=out, erasure_columns=7)
    )

    result = C.repair_whole_disc(h["pcm_path"], [33, 100], 199, 0x1234, 30)

    assert result.repaired
    assert result.unverified_columns == 0
    assert result.erasure_columns == 7
    assert h["pcm_path"].read_bytes() == out


def test_an_unverified_repair_commits_but_stays_identifiable(
    _gated_harness: dict,
) -> None:
    """The weaker of AccuDisc's two success claims is accepted — its columns were
    *determined* rather than verified — but the result must still say so.

    Accepting it is sound only because the CTDB per-track CRC gate covers
    ``[bounds[0], bounds[-1])``, which is every word an at-capacity repair can
    touch. Folding the two buffers together (``audio or audio_unverified``) would
    commit the same bytes and lose the record of which claim they rest on, which
    is exactly what AccuDisc's two-buffer return exists to prevent.
    """
    h = _gated_harness
    out = _repaired_pcm(h)
    h["monkeypatch"].setattr(
        C,
        "run_repair",
        lambda *a, **k: _report(audio_unverified=out, unverified_columns=3),
    )

    result = C.repair_whole_disc(h["pcm_path"], [33, 100], 199, 0x1234, 30)

    assert result.repaired
    assert result.unverified_columns == 3, "the weaker claim must not vanish"
    assert h["pcm_path"].read_bytes() == out


def test_an_unverified_repair_is_still_refused_by_the_crc_gate(
    _gated_harness: dict,
) -> None:
    """The gate is what makes accepting the weaker claim safe, so it must bite."""
    h = _gated_harness
    before = h["pcm_path"].read_bytes()
    h["monkeypatch"].setattr(
        C, "verify_ctdb", lambda *a, **k: C.CtdbVerdict(unfixed=[1])
    )
    h["monkeypatch"].setattr(
        C,
        "run_repair",
        lambda *a, **k: _report(
            audio_unverified=_repaired_pcm(h), unverified_columns=3
        ),
    )

    result = C.repair_whole_disc(h["pcm_path"], [33, 100], 199, 0x1234, 30)

    assert not result.repaired
    assert "CTDB CRC gate failed" in result.reason
    assert h["pcm_path"].read_bytes() == before


# ---- D4: the at-capacity path, against the real decoder ---------------------


def test_a_column_at_full_erasure_capacity_returns_the_weaker_buffer() -> None:
    """The one arm our disc fixtures cannot reach, built arithmetically instead.

    Every fixture we hold reports ``unverified_columns == 0``, so the acceptance of
    ``audio_unverified`` in ``_repair_and_verify`` was reasoned about and never
    executed. Real C2 data rarely produces it: C2 over-flags, and one spare flag in
    the column puts it *over* capacity rather than exactly at it.

    Constructing it needs no disc and no fixture, only the identity that an all-zero
    image has all-zero syndromes, so an all-zero parity blob is *valid* parity for
    it. Damage exactly ``npar`` words in one column and flag every one: the erasures
    consume every check equation, the errata are determined, and re-deriving the
    syndromes from them cannot disagree. Recipe from AccuDisc's 2026-08-03b §147.1.

    The assertion that matters is ``r.audio is None``. A gate spelled ``if r.audio:``
    reads as correct and silently declines every at-capacity repair; one spelled
    ``audio or audio_unverified`` commits the weaker claim without recording it. This
    pins the middle path — and that the weaker buffer is a *repair*, not a refusal in
    disguise.

    The skip is evaluated **inside** the test, not as a module-level ``skipif``.
    ``_import_binding`` is a ``functools.cache`` on a module global that
    ``test_accudisc_reader`` fakes and clears per test; a decorator argument runs at
    *collection* time, which primed that cache before those tests could set it up and
    broke ``test_a_namespace_package_is_not_the_binding`` — passing alone, failing in
    the suite. Exactly the order-dependence that file's own fixture exists to prevent.
    """
    if C.accudisc_reader._import_binding()[0] is None:
        pytest.skip("AccuDisc binding not importable")

    npar, stride, frames = 2, 3, 40
    s, w = stride * 2, frames * 1176
    pcm, parity = bytearray(w * 2), bytes(s * npar * 2)
    original = bytes(pcm)

    bitmap = bytearray((w + 7) // 8)
    for row in (4, 6):  # exactly npar rows, all in column 0
        word = s + row * s
        pcm[word * 2 : word * 2 + 2] = (0x1234).to_bytes(2, "little")
        bitmap[word // 8] |= 1 << (word % 8)

    r = C.accudisc_reader.ctdb_repair(
        pcm=bytes(pcm),
        parity=parity,
        npar=npar,
        wire_stride=stride,
        image_first_frame=0,
        image_frames=frames,
        offset_pairs=0,
        erasures=bytes(bitmap),
    )

    assert r.audio is None, "an at-capacity repair must not claim to be verified"
    assert r.audio_unverified is not None, "it is a repair, not a refusal"
    assert not r.refused
    assert r.unverified_columns == 1
    assert r.unverified_columns <= r.repaired_columns, "a subset, not a parallel count"
    assert bytes(r.audio_unverified) == original, "the damage is undone exactly"


# ---------------------------------------------------------------------------
# §E — the repaired-sector map (progress-map-plan.md §5, N2 step 6)
#
# Positions are DERIVED from the pre/post buffers, not reported by the decoder:
# the API returns repaired audio rather than a correction list, so there is no
# per-position quantity in the report to read. That also makes this a
# measurement of the repair's effect rather than its self-report — the same
# reason the CRC and AR gates exist.
# ---------------------------------------------------------------------------

_F = C._FRAME


def test_the_map_marks_exactly_the_sectors_whose_bytes_changed():
    before = bytearray(_F * 5)
    after = bytearray(before)
    after[_F * 1] ^= 0xFF  # first byte of sector 1
    after[_F * 3 + _F - 1] ^= 0x01  # LAST byte of sector 3

    got = C._repaired_sector_map(bytes(before), bytes(after))

    assert list(got) == [0, 1, 0, 1, 0]


def test_a_change_in_the_final_byte_of_a_sector_is_not_missed():
    """The boundary a reshape gets wrong. A per-sector `any` that sliced
    `[lo:hi-1]`, or an off-by-one in the chunk stride, still passes the test
    above (which touches byte 0 of sector 1) and fails here."""
    before = bytearray(_F * 2)
    after = bytearray(before)
    after[-1] = 0xFF

    assert list(C._repaired_sector_map(bytes(before), bytes(after))) == [
        0,
        1,
    ]


def test_the_chunk_boundary_is_not_a_blind_spot():
    """Damage placed exactly either side of the chunk stride.

    `_DIFF_CHUNK_SECTORS` exists to bound memory, and a chunked loop is where an
    off-by-one hides: the sectors at `chunk-1` and `chunk` are the two a wrong
    stride drops or double-counts. Sized to span three chunks so a middle chunk
    is fully interior.
    """
    chunk = C._DIFF_CHUNK_SECTORS
    n = chunk * 2 + 3
    before = bytearray(_F * n)
    after = bytearray(before)
    marked = {0, chunk - 1, chunk, chunk * 2 - 1, chunk * 2, n - 1}
    for s in marked:
        after[s * _F] = 0xAA

    got = C._repaired_sector_map(bytes(before), bytes(after))

    assert len(got) == n
    assert {i for i, v in enumerate(got) if v} == marked


def test_identical_buffers_map_to_all_zero_which_is_why_failure_returns_none():
    """An all-zero map and "no map" are different claims and must stay so.

    A failed repair writes nothing, so `before == after` and a diff would yield
    all zeros — indistinguishable from a successful repair that changed nothing.
    `CtdbRepairResult.repaired_sectors` is therefore `None` on every failure
    path, and this test states the reason rather than leaving it to the
    docstring: "repaired nothing" and "did not repair" must not render alike.
    """
    buf = bytes(_F * 4)
    assert set(C._repaired_sector_map(buf, buf)) == {0}


def test_a_trailing_partial_sector_is_dropped_rather_than_reported():
    """`min(len) // _FRAME` truncates. A partial sector cannot be characterised
    as repaired-or-not, and inventing a cell for it would put a colour on the
    outer edge of the disc — where damage concentrates and a false mark is worst."""
    before = bytes(_F * 3 + 17)
    after = bytearray(before)
    after[-1] = 0xFF
    assert len(C._repaired_sector_map(before, bytes(after))) == 3


def test_the_map_covers_the_whole_pcm_not_just_the_ctdb_window():
    """A write outside [bounds[0], bounds[-1]) is the bug most worth seeing, so
    the diff must not be narrowed to the window the repair is allowed to touch."""
    before = bytearray(_F * 4)
    after = bytearray(before)
    after[0] = 0xFF  # sector 0 — before any plausible bounds[0]
    got = C._repaired_sector_map(bytes(before), bytes(after))
    assert got[0] == 1


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


_MAP_GLYPHS = "█▓▒░"


def _report_lines(capsys) -> list[str]:
    """Captured report, colour stripped, blank lines dropped."""
    return [
        _ANSI.sub("", ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()
    ]


def _bar_span(line: str) -> tuple[int, int]:
    """(start column, width) of the map on *line*.

    Locating the bar by its first "█" is what the damage/repairs version could
    get away with, because both rows drew the same pattern. Rows that differ —
    which is now the point — make that the first *healthy* cell instead, so it
    silently measures the content rather than the geometry.
    """
    hits = [i for i, ch in enumerate(line) if ch in _MAP_GLYPHS]
    assert hits, f"no map on {line!r}"
    return hits[0], len(hits)


def test_the_report_draws_before_and_after_with_after_clean(capsys, monkeypatch):
    """kgr, 2026-08-14: before/after, not damage/repairs.

    The question the user has after a repair is "is the disc fixed?". The first
    version paired C2 damage with the repairs, which left the damage row still
    showing the original damage after a successful repair — answering "no" in
    the only vocabulary the row has.

    "clean" is earned rather than assumed: `repair_whole_disc` writes the PCM
    back only after `verify_ctdb` AND `verify_ar` both pass, so every track CTDB
    called damaged verifies against both references by the time we draw this.
    """
    monkeypatch.setenv("COLUMNS", "120")
    from cdda2img.cdda2img import _print_ctdb_repair_map

    dmg, rep = bytearray(1000), bytearray(1000)
    dmg[100:150] = b"\x01" * 50
    rep[100:150] = b"\x01" * 50

    _print_ctdb_repair_map(bytes(rep), bytes(dmg), resolved=True)
    lines = _report_lines(capsys)

    assert len(lines) == 3, lines
    assert "Before" in lines[1] and "50 flagged" in lines[1]
    assert "After" in lines[2] and "clean" in lines[2]
    # The after row is entirely healthy: no ramp glyph survives anywhere in it.
    assert set(lines[2].split()[1]) == {"█"}, lines[2]
    # Column alignment is the whole point of two rows — a reader compares them
    # vertically, which needs one start column and one width, not just one start.
    assert _bar_span(lines[1]) == _bar_span(lines[2])


def test_unresolved_draws_the_residual_instead_of_claiming_clean(capsys, monkeypatch):
    """AR and CTDB are different reference populations, so a track can still fail
    AR while passing the per-track CRC that admitted the repair. The gate that
    committed the audio cannot see that; the post-repair AR verify can."""
    monkeypatch.setenv("COLUMNS", "120")
    from cdda2img.cdda2img import _print_ctdb_repair_map

    dmg, rep = bytearray(1000), bytearray(1000)
    dmg[100:150] = b"\x01" * 50  # flagged
    rep[100:120] = b"\x01" * 20  # only part of it rewritten

    _print_ctdb_repair_map(bytes(rep), bytes(dmg), resolved=False)
    lines = _report_lines(capsys)

    assert "50 flagged" in lines[1]
    # 50 flagged - 20 rewritten = 30 still flagged. Never "clean".
    assert "30 remain" in lines[2], lines[2]
    assert "clean" not in lines[2]


def test_resolved_never_draws_residual_c2_flags(capsys, monkeypatch):
    """The residual is right ONLY when AR still disagrees. C2 over-flags, so once
    AR verifies, unrewritten flags are refuted evidence — drawing them would
    paint phantom damage directly above a report saying every track is OK."""
    monkeypatch.setenv("COLUMNS", "120")
    from cdda2img.cdda2img import _print_ctdb_repair_map

    dmg, rep = bytearray(1000), bytearray(1000)
    dmg[100:150] = b"\x01" * 50
    rep[100:120] = b"\x01" * 20  # 30 sectors flagged but never rewritten

    _print_ctdb_repair_map(bytes(rep), bytes(dmg), resolved=True)
    lines = _report_lines(capsys)

    assert "clean" in lines[2]
    assert set(lines[2].split()[1]) == {"█"}, lines[2]


def test_no_damage_map_sources_before_from_the_repairs_and_says_so(capsys, monkeypatch):
    """Without a C2 capture there is no "before" in the vocabulary the live map
    used, so the row falls back to what parity rewrote — measured, but a lower
    bound on the damage. The suffix names the quantity rather than quietly
    swapping one for the other under an unchanged label."""
    monkeypatch.setenv("COLUMNS", "120")
    from cdda2img.cdda2img import _print_ctdb_repair_map

    rep = bytearray(1000)
    rep[10:20] = b"\x01" * 10

    _print_ctdb_repair_map(bytes(rep), None, resolved=True)
    lines = _report_lines(capsys)

    assert len(lines) == 3, lines
    assert "10 repaired" in lines[1], lines[1]
    assert "flagged" not in lines[1]
    assert "clean" in lines[2]


def test_unresolved_without_a_damage_map_draws_no_after_row(capsys, monkeypatch):
    """Nothing truthful to draw: the repair committed, AR still disagrees, and
    there is no C2 map to localise what is left. The before row stands alone
    rather than an after row being invented."""
    monkeypatch.setenv("COLUMNS", "120")
    from cdda2img.cdda2img import _print_ctdb_repair_map

    rep = bytearray(1000)
    rep[10:20] = b"\x01" * 10

    _print_ctdb_repair_map(bytes(rep), None, resolved=False)
    lines = _report_lines(capsys)

    # Three lines, but the third is a refusal rather than a map: emitting two
    # where every other run emits three reads as truncated output.
    assert len(lines) == 3, lines
    assert "not shown" in lines[2] and "no C2 map" in lines[2]
    assert "█" not in lines[2]
    assert "clean" not in "".join(lines)


def test_nothing_is_printed_when_no_repair_happened(capsys):
    """`repaired_sectors` is None on every failure path. Drawing an empty report
    there would assert a repair took place and did nothing."""
    from cdda2img.cdda2img import _print_ctdb_repair_map

    _print_ctdb_repair_map(None, b"\x01" * 100)
    _print_ctdb_repair_map(b"", b"\x01" * 100)
    assert capsys.readouterr().out == ""


def test_mismatched_map_lengths_are_clamped_so_the_columns_still_align(
    capsys, monkeypatch
):
    """`cells_from_damage` derives its bucket size from each map's own length, so
    two maps of different lengths put cell i over different sectors and the
    vertical comparison silently misaligns. They should already agree — but
    "should" is not a reason to skip the clamp."""
    monkeypatch.setenv("COLUMNS", "120")
    from cdda2img.cdda2img import _print_ctdb_repair_map

    dmg = bytes(bytearray([1] * 50 + [0] * 950))
    rep = bytes(bytearray([1] * 50 + [0] * 450))  # half as long

    _print_ctdb_repair_map(rep, dmg, resolved=True)
    lines = _report_lines(capsys)

    assert _bar_span(lines[1]) == _bar_span(lines[2])
    assert "50 flagged" in lines[1]


@pytest.mark.parametrize("cols", [40, 60, 80, 100, 124, 130, 153, 160, 200])
def test_every_line_fits_the_terminal(capsys, monkeypatch, cols):
    """THE regression test, and the one the first version had no equivalent of.

    That version budgeted `terminal_width - 4` for the bar while the line also
    carried a 20-column label prefix and a 13-column suffix, so it emitted a
    153-column line under a cap that claimed to fit 120 — wrapping on every
    terminal narrower than 153, including the ones the cap was protecting.

    The live map is immune because it subtracts the whole line's furniture and
    then clips to `avail`; a one-shot report has to get the budget right up
    front. Asserting the rendered width directly is what makes the class of bug
    untestable-by-inspection go away.
    """
    monkeypatch.setenv("COLUMNS", str(cols))
    from cdda2img.cdda2img import _print_ctdb_repair_map

    dmg, rep = bytearray(200000), bytearray(200000)
    dmg[1000:9000] = b"\x01" * 8000  # six-figure counts widen the suffix
    rep[1000:9000] = b"\x01" * 8000

    _print_ctdb_repair_map(bytes(rep), bytes(dmg), resolved=True)
    for ln in _report_lines(capsys):
        assert len(ln) <= cols, f"{len(ln)} > {cols}: {ln!r}"


def test_a_terminal_too_narrow_for_a_bar_prints_counts_only(capsys, monkeypatch):
    """A floor that forces a bar wider than fits is the original bug with a
    different constant, so below the minimum there is simply no bar."""
    monkeypatch.setenv("COLUMNS", "24")
    from cdda2img.cdda2img import _print_ctdb_repair_map

    dmg, rep = bytearray(1000), bytearray(1000)
    dmg[100:150] = b"\x01" * 50
    rep[100:150] = b"\x01" * 50

    _print_ctdb_repair_map(bytes(rep), bytes(dmg), resolved=True)
    lines = _report_lines(capsys)

    assert len(lines) == 3, lines
    assert "█" not in "".join(lines)
    assert "50 flagged" in lines[1] and "clean" in lines[2]
    for ln in lines:
        assert len(ln) <= 24, ln


def test_indentation_matches_the_accuraterip_report(capsys, monkeypatch):
    """This block is printed between two AR reports, so being one column out is
    maximally visible. `print_ar_report` emits f"   {line}" over a body whose own
    lines are indented 2, giving 3 for the header and 5 for the rows."""
    monkeypatch.setenv("COLUMNS", "120")
    from cdda2img.cdda2img import _print_ctdb_repair_map

    rep = bytearray(1000)
    rep[10:20] = b"\x01" * 10

    _print_ctdb_repair_map(bytes(rep), None, resolved=True)
    out = capsys.readouterr().out.splitlines()

    assert out[0].startswith("   C") and not out[0].startswith("    ")
    for ln in out[1:]:
        assert ln.startswith("     ") and not ln.startswith("      "), ln
