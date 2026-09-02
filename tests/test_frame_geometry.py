"""Frame geometry: the PCM block and the TOC must describe the same disc.

rbi_spec §6.2.1. Track boundaries on a CD are frame-quantised (IEC 60908 MM:SS:FF),
so a writer assembling a disc from sample-exact sources has to resolve every
boundary to a frame. The rule is that boundaries are snapped off the *absolute*
cumulative sample position, never off a running sum of already-rounded durations
— the latter accumulates and displaces later tracks without bound.

The negative control below reproduces that original algorithm. Without it these
tests would pass on the buggy code too, since a container is internally
self-consistent either way: the drift is only visible against the true audio.
"""

from __future__ import annotations

import struct
import wave

import pytest

from cdda2img.container import pad_pcm_to_declared_frames
from cdda2img.rbi_format import (
    BYTES_PER_CD_FRAME,
    PCM_SAMPLE_RATE,
    SAMPLES_PER_CD_FRAME,
    frames_for_samples,
)
from cdda2img.toc import track_frame_durations

# Deliberately not multiples of 588, and not a constant remainder either.
_COUNTS = [PCM_SAMPLE_RATE * 3 + 137 * (i + 1) for i in range(15)]


def _write_track(path, n_pairs, value):
    """A WAV of *n_pairs* stereo pairs, every sample the constant *value*.

    A constant per track makes contamination directly observable: a slice that
    holds two values has borrowed audio from its neighbour.
    """
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(PCM_SAMPLE_RATE)
        w.writeframes(struct.pack("<h", value) * (n_pairs * 2))


def _raw_samples(path):
    """Every sample byte of *path*, with no WAV wrapper."""
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


@pytest.fixture
def tracks(tmp_path):
    paths = []
    for i, n in enumerate(_COUNTS, start=1):
        p = tmp_path / f"t{i:02d}.wav"
        _write_track(p, n, i * 1000)
        paths.append(p)
    return paths


# ---------------------------------------------------------------------------
# frames_for_samples
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("samples", "expected"),
    [(0, 0), (1, 1), (587, 1), (588, 1), (589, 2), (1176, 2)],
)
def test_frames_for_samples_rounds_up(samples, expected):
    """A partial frame still occupies a whole frame on the disc."""
    assert frames_for_samples(samples) == expected


# ---------------------------------------------------------------------------
# Boundary placement
# ---------------------------------------------------------------------------


def _true_boundaries():
    """Absolute end of each track, in sample pairs, in the raw concatenation."""
    out, c = [], 0
    for n in _COUNTS:
        c += n
        out.append(c)
    return out


def test_boundary_error_is_bounded_and_does_not_accumulate(tracks):
    """Every declared boundary lands within half a frame of the true one.

    Half a frame is 294 sample pairs (6.7 ms). The *bound* is the assertion; the
    accumulation check below is what distinguishes this from the old algorithm.
    """
    durations, _ = track_frame_durations(tracks)

    declared, cum = [], 0
    for d in durations:
        cum += d
        declared.append(cum * SAMPLES_PER_CD_FRAME)

    errors = [d - t for d, t in zip(declared, _true_boundaries(), strict=True)]
    assert all(abs(e) <= SAMPLES_PER_CD_FRAME // 2 + 1 for e in errors), errors

    # The last boundary is no worse than the first few: no drift along the disc.
    assert abs(errors[-1]) <= max(abs(e) for e in errors[:3]) + SAMPLES_PER_CD_FRAME


def test_negative_control_old_algorithm_does_accumulate():
    """The pre-fix rule (floor each duration, sum the floors) must fail the bound.

    Without this, the test above proves nothing: it would pass on the buggy
    implementation too. Here the final boundary drifts far past half a frame.
    """
    durations = [n // SAMPLES_PER_CD_FRAME for n in _COUNTS]  # the old int() floor

    declared, cum = [], 0
    for d in durations:
        cum += d
        declared.append(cum * SAMPLES_PER_CD_FRAME)

    errors = [d - t for d, t in zip(declared, _true_boundaries(), strict=True)]
    # It violates the very bound the snapped algorithm satisfies, rather than
    # some threshold picked to make the point.
    bound = SAMPLES_PER_CD_FRAME // 2 + 1
    assert abs(errors[-1]) > bound, errors
    # ... and grows monotonically, which is the signature of accumulation
    # rather than of a merely coarser rounding.
    assert all(abs(errors[i]) >= abs(errors[i - 1]) for i in range(1, len(errors)))


def test_every_track_is_at_least_one_frame(tracks):
    durations, _ = track_frame_durations(tracks)
    assert all(d > 0 for d in durations)
    assert len(durations) == len(_COUNTS)


def test_total_frames_covers_all_audio(tracks):
    """The final boundary rounds UP: no source sample is ever dropped."""
    _, total_frames = track_frame_durations(tracks)
    assert total_frames == frames_for_samples(sum(_COUNTS))
    assert total_frames * SAMPLES_PER_CD_FRAME >= sum(_COUNTS)


def test_durations_sum_to_total_frames(tracks):
    durations, total_frames = track_frame_durations(tracks)
    assert sum(durations) == total_frames


def test_rejects_non_red_book_sample_rate(tmp_path):
    p = tmp_path / "48k.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"\x00\x00\x00\x00" * 1000)
    with pytest.raises(ValueError, match="not Red Book audio"):
        track_frame_durations([p])


# ---------------------------------------------------------------------------
# Gapless preservation and the lead-out pad
# ---------------------------------------------------------------------------


def test_stored_audio_is_bit_identical_to_the_concatenation(tmp_path, tracks):
    """Snapping moves index marks only — it must never alter a sample.

    This is what makes a gapless album survive the create pipeline.
    """
    from cdda2img.concat import concat_wav
    from cdda2img.container import wav_to_raw_pcm

    durations, total_frames = track_frame_durations(tracks)
    concat = tmp_path / "all.wav"
    pcm = tmp_path / "all.pcm"
    concat_wav(tracks, concat)
    wav_to_raw_pcm(concat, pcm)
    pad_pcm_to_declared_frames(pcm, total_frames)

    raw = b"".join(_raw_samples(p) for p in tracks)
    stored = pcm.read_bytes()
    assert stored[: len(raw)] == raw, "stored audio diverges from the source"
    assert set(stored[len(raw) :]) <= {0}, "the lead-out pad must be silence"
    assert len(stored) % BYTES_PER_CD_FRAME == 0
    assert len(stored) == sum(durations) * BYTES_PER_CD_FRAME


def test_pad_refuses_a_shortfall_of_a_whole_frame(tmp_path):
    """The bounds check is the protection: only a sub-frame tail may be filled.

    A larger disagreement is a real defect and must not be zero-filled into
    apparent agreement.
    """
    pcm = tmp_path / "short.pcm"
    pcm.write_bytes(bytes(BYTES_PER_CD_FRAME))  # one frame present
    with pytest.raises(RuntimeError, match="shortfall of less than one frame"):
        pad_pcm_to_declared_frames(pcm, 3)  # three declared


def test_pad_refuses_pcm_longer_than_declared(tmp_path):
    pcm = tmp_path / "long.pcm"
    pcm.write_bytes(bytes(BYTES_PER_CD_FRAME * 3))
    with pytest.raises(RuntimeError, match="shortfall of less than one frame"):
        pad_pcm_to_declared_frames(pcm, 1)


def test_pad_is_a_noop_when_already_aligned(tmp_path):
    pcm = tmp_path / "aligned.pcm"
    pcm.write_bytes(b"\x01" * (BYTES_PER_CD_FRAME * 2))
    pad_pcm_to_declared_frames(pcm, 2)
    assert pcm.read_bytes() == b"\x01" * (BYTES_PER_CD_FRAME * 2)


# ---------------------------------------------------------------------------
# End-to-end: a container assembled the way `create` assembles one must satisfy
# validation rule 31.
# ---------------------------------------------------------------------------


def test_created_container_passes_the_geometry_rule(tmp_path, tracks, capsys):
    """Mirrors the create wiring: concat -> raw PCM -> snap -> pad -> build.

    Rule 31 is what makes this end-to-end rather than a restatement of the unit
    tests: it re-derives the declared geometry from the TOC *text* in the built
    container, so a disagreement introduced anywhere between here and the block
    directory still surfaces.
    """
    from cdda2img.concat import concat_wav
    from cdda2img.container import (
        build_container,
        verify_container,
        wav_to_raw_pcm,
    )
    from cdda2img.rbi_format import RBIDisc
    from cdda2img.toc import build_toc_entries, generate_toc

    durations, total_frames = track_frame_durations(tracks)

    concat = tmp_path / "all.wav"
    pcm = tmp_path / "all.pcm"
    concat_wav(tracks, concat)
    wav_to_raw_pcm(concat, pcm)
    pad_pcm_to_declared_frames(pcm, total_frames)

    disc = RBIDisc(album="Geometry", artist="Test", disc_number=1, disc_total=1)
    disc.tracks = build_toc_entries(tracks, durations, disc)

    rbi = tmp_path / "geometry.rbi"
    build_container(pcm, generate_toc(disc), disc, rbi, quiet=True)

    capsys.readouterr()
    assert verify_container(rbi) is True
    assert "[FAIL]" not in capsys.readouterr().out


def test_geometry_rule_catches_an_unpadded_container(tmp_path, tracks, capsys):
    """Negative control for rule 31: skip the pad and the verifier must fail.

    This is the shape every pre-fix `create` container has — internally
    self-consistent, correct checksums, and 104 ms of audio the TOC never
    describes.
    """
    from cdda2img.concat import concat_wav
    from cdda2img.container import build_container, verify_container, wav_to_raw_pcm
    from cdda2img.rbi_format import RBIDisc
    from cdda2img.toc import build_toc_entries, generate_toc

    durations, _ = track_frame_durations(tracks)

    concat = tmp_path / "all.wav"
    pcm = tmp_path / "all.pcm"
    concat_wav(tracks, concat)
    wav_to_raw_pcm(concat, pcm)  # deliberately NOT padded

    disc = RBIDisc(album="Ragged", artist="Test", disc_number=1, disc_total=1)
    disc.tracks = build_toc_entries(tracks, durations, disc)

    rbi = tmp_path / "ragged.rbi"
    build_container(pcm, generate_toc(disc), disc, rbi, quiet=True)

    capsys.readouterr()
    assert verify_container(rbi) is False
    assert "31. PCM length == TOC geometry" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Every import reader must satisfy the same invariant.
#
# Rule 31 is unconditional and hard-failing, so it applies to containers built
# from imported images too — five reader paths, none of which previously touched
# verify_container. NRG and PXI are safe by construction (NRG rejects non-2352
# track lengths outright; PXI zero-fills its tail), but that is a property of
# today's code and worth pinning rather than assuming.
# ---------------------------------------------------------------------------


def _declared_bytes(disc):
    return (
        sum(t.pregap_frames + t.duration_frames for t in disc.tracks)
        * BYTES_PER_CD_FRAME
    )


def _write_pq(track, index, abs_frame):
    """One 64-byte PQDESCR record. Track 0xAA is the lead-out sentinel."""
    mm, rem = divmod(abs_frame, 75 * 60)
    ss, ff = divmod(rem, 75)
    rec = bytearray(b" " * 64)
    rec[0:4] = b"VVVS"
    rec[4:6] = b"AA" if track == 0xAA else f"{track:02d}".encode()
    rec[6:8] = f"{index:02d}".encode()
    rec[10:16] = f"{mm:02d}{ss:02d}{ff:02d}".encode()
    return bytes(rec)


def _make_ddp(tmp_path):
    """A minimal well-formed DDP 2.0 image: 3 tracks, one with a pre-gap.

    Written out here rather than reused because DDP has no other test coverage
    in the tree at all.
    """
    pregap, lens = 150, [3000, 4000, 2500]
    t1_i1 = pregap
    t2_i0 = t1_i1 + lens[0]
    t2_i1 = t2_i0 + 150
    t3_i0 = t2_i1 + lens[1]
    leadout = t3_i0 + lens[2]

    d = tmp_path / "ddp"
    d.mkdir()
    (d / "DDPID").write_bytes(b"DDP 2.00" + b"5037300003931" + b" " * 40)
    (d / "PQDESCR").write_bytes(
        _write_pq(1, 1, t1_i1)
        + _write_pq(2, 0, t2_i0)
        + _write_pq(2, 1, t2_i1)
        + _write_pq(3, 0, t3_i0)
        + _write_pq(3, 1, t3_i0)
        + _write_pq(0xAA, 1, leadout)
    )
    for n, span in enumerate([t2_i0, t3_i0 - t2_i0, leadout - t3_i0], start=1):
        (d / f"TRACK{n:02d}.DAT").write_bytes(bytes(span * BYTES_PER_CD_FRAME))
    return d


def test_ddp_import_satisfies_the_geometry_rule(tmp_path):
    from cdda2img.ddp_reader import import_ddp

    pcm = tmp_path / "ddp.pcm"
    disc, _ = import_ddp(_make_ddp(tmp_path), pcm, report=lambda _s: None)
    assert pcm.stat().st_size == _declared_bytes(disc)


def test_cdrdao_import_satisfies_the_geometry_rule(tmp_path):
    from cdda2img.cdrdao_reader import import_cdrdao
    from tests.test_cdrdao_reader import _SYNTHETIC_TOC, _make_synthetic_bin

    toc = tmp_path / "test.toc"
    _make_synthetic_bin(tmp_path / "test.bin")
    toc.write_text(_SYNTHETIC_TOC, encoding="utf-8")

    pcm = tmp_path / "cdrdao.pcm"
    disc, _ = import_cdrdao(toc, pcm)
    assert pcm.stat().st_size == _declared_bytes(disc)


def test_ccd_import_satisfies_the_geometry_rule(tmp_path):
    from cdda2img.ccd_reader import import_ccd
    from tests.test_ccd_reader import _make_standard

    ccd, _img = _make_standard(tmp_path)
    pcm = tmp_path / "ccd.pcm"
    disc, _ = import_ccd(ccd, pcm, report=lambda _s: None)
    assert pcm.stat().st_size == _declared_bytes(disc)


def test_nrg_import_satisfies_the_geometry_rule(tmp_path):
    from cdda2img.nrg_reader import import_nrg
    from tests.test_nrg_reader import _make_standard_nrg

    nrg = tmp_path / "test.nrg"
    nrg.write_bytes(_make_standard_nrg())
    pcm = tmp_path / "nrg.pcm"
    disc, _ = import_nrg(nrg, pcm, report=lambda _s: None)
    assert pcm.stat().st_size == _declared_bytes(disc)


def test_pxi_import_satisfies_the_geometry_rule(tmp_path):
    from cdda2img.pxi_reader import import_pxi
    from tests.test_pxi_reader import build_pxi

    # (session, track, position, length) -- positions are absolute frames.
    pxi = build_pxi(
        tmp_path,
        records=[
            (1, 1, 150, 0),
            (1, 1, 150, 2000),
            (1, 2, 2150, 0),
            (1, 2, 2150, 1500),
        ],
        leadout=3650,
    )
    pcm = tmp_path / "pxi.pcm"
    disc, _ = import_pxi(pxi, pcm, report=lambda _s: None)
    assert pcm.stat().st_size == _declared_bytes(disc)
