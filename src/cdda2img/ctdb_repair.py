"""CTDB (CUETools Database) Reed-Solomon parity repair of whole-disc CD-DA PCM.

This is the canonical CTDB-repair logic for the rip pipeline
(:func:`repair_whole_disc`). It owns the network (CTDB lookup + parity fetch) and
policy (entry selection, double-gate verification), and delegates the Reed-Solomon
math to AccuDisc through the seam (``accudisc_reader.ctdb_repair``).

The drive's C2 error pointers, when available, are fed in as *erasures*, which
roughly doubles the damage that can be reconstructed (correction holds when
``e + 2t <= npar`` vs ``2t <= npar`` error-only). C2 is only a modifier: with it
absent/disabled, error-only decoding still repairs, so recovery is never disabled —
only the erasure boost.

Repair is safe by construction: the decode writes to a fresh buffer and is only
committed if BOTH gates pass — CTDB per-track CRC (at the entry's consensus offset)
AND AccurateRip (at the drive read-offset). A miscorrection fails a gate and is
discarded, leaving the original PCM untouched.

**Two things changed when this moved off the ``ctanalyse`` binary (2026-08-02),
and neither is visible in the call signature.**

*The old-byte splice check is gone.* The binary returned a correction list, and
:func:`apply_corrections` refused the whole splice if any correction's ``old``
value disagreed with the stored word — a cheap check that caught a mis-aligned or
stale correction list before it touched audio. The API returns repaired audio
instead of corrections, deliberately (a caller able to read corrections without
the verdict can commit them past a refusal), so that check now lives inside
AccuDisc as its per-column re-verification. The outcome is no less safe — both
gates below still close over the result — but a failure that once said
``old-byte mismatch (splice aborted)`` now arrives as the less specific
``CTDB CRC gate failed``.

*``erasure_columns`` may answer a different question, and we could not tell.*
``ctanalyse`` counted columns where erasures were used **and changed the
outcome** — it retries error-only on failure without incrementing. AccuDisc
documents its field as dirty columns **carrying at least one erasure**, which is
the larger quantity. Measured on the Tracy fixture the two agree exactly on both
arms available to us: 533 and 533 with the aligned bitmap, 30 and 30 with the
deliberately misaligned one. That is not evidence they mean the same thing —
separating them needs a column that carries an erasure and decodes error-only
anyway, which neither arm contains. Treat the number as unresolved rather than
migrated; it reaches no gate and no provenance key, so nothing depends on the
answer today. (The PROV key ``ctdb_erasures`` is the marker ``"c2"``, not this
count — do not confuse them.)
"""

from __future__ import annotations

import logging
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from cdda2img import accudisc_reader
from cdda2img.accudisc_reader import CtdbRepairReport

log = logging.getLogger(__name__)

_FRAME = 2352  # bytes per CD sector
_SPP = 588  # stereo sample-pairs per sector
_SWEEP_WINDOW = 700  # entry offset sweep range, ± stereo samples
_CTDB_NS = {"c": "http://db.cuetools.net/ns/mmd-1.0#"}


@dataclass
class Entry:
    id: str
    confidence: int
    npar: int
    stride: int  # wire value (internal stride = 2x)
    hasparity: str
    trackcrcs: list[int]


@dataclass
class Selection:
    entry: Entry
    offset: int  # stereo samples, our-domain -> entry-domain window shift
    damaged: list[int] = field(default_factory=list)
    unverifiable: list[int] = field(default_factory=list)


@dataclass
class CtdbRepairResult:
    """Outcome of a whole-disc CTDB repair attempt. On success the repaired PCM has
    been written back to the input path; on failure the input is untouched."""

    repaired: bool
    reason: str
    entry_id: str | None = None
    ctdb_offset: int | None = None
    corrections: int = 0
    erasure_columns: int = 0
    damaged_tracks: list[int] = field(default_factory=list)
    used_c2: bool = False
    unverified_columns: int = 0
    """Columns AccuDisc *determined* rather than verified — see
    :class:`cdda2img.accudisc_reader.CtdbRepairReport`. Non-zero means the
    committed audio came from the weaker of AccuDisc's two success claims, which
    both gates below still had to pass. Recorded rather than folded away so a
    rip that took that path stays identifiable afterwards; a repair that hides
    which claim it rests on is exactly what AccuDisc's two-buffer return exists
    to prevent."""


def _lookup_url(bounds: list[int]) -> str:
    toc = ":".join(str(x) for x in bounds)
    return (
        "http://db.cuetools.net/lookup2.php"
        "?version=3&ctdb=1&fuzzy=0&metadata=default&toc=" + toc
    )


def load_entries(
    bounds: list[int], n_tracks: int, xml_cache: Path | None = None
) -> list[Entry]:
    """Fetch (or read a cached) CTDB lookup and parse parity-bearing entries."""
    if xml_cache and xml_cache.exists():
        raw = xml_cache.read_bytes()
    else:
        req = urllib.request.Request(  # noqa: S310 — CTDB is plain-http by design
            _lookup_url(bounds), headers={"User-Agent": "cdda2img/ctdb-repair"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            raw = resp.read()
        if xml_cache:
            xml_cache.parent.mkdir(parents=True, exist_ok=True)
            xml_cache.write_bytes(raw)
    entries: list[Entry] = []
    for e in ET.fromstring(raw).findall("c:entry", _CTDB_NS):  # noqa: S314
        crcs = [int(x, 16) for x in (e.get("trackcrcs") or "").split()]
        if len(crcs) != n_tracks or not e.get("hasparity"):
            continue
        entries.append(
            Entry(
                id=e.get("id", "?"),
                confidence=int(e.get("confidence", "0")),
                npar=int(e.get("npar", "0")),
                stride=int(e.get("stride", "0")),
                hasparity=e.get("hasparity", ""),
                trackcrcs=crcs,
            )
        )
    return entries


def track_crc_at(
    pcm: bytes,
    track: int,
    offset: int,
    stride_wire: int,
    bounds: list[int],
    n_tracks: int,
) -> int | None:
    """CTDB per-track CRC32 of *track*'s window shifted by *offset* stereo samples.

    Edge-aware (AccurateRip.cs:CTDBCRC): track 1 excludes the first stride/2 stereo
    samples, the last track the final laststride/2. None if out of range.

    ``laststride`` is a property of CTDB's *image* — ``[bounds[0], bounds[-1])``,
    i.e. first-track INDEX 01 to lead-out — and must NOT be derived from ``len(pcm)``,
    which spans ``[0, lead-out)``. The two coincide only when track 1's INDEX 01 is at
    LBA 0. On a disc with a program-area pre-gap (ABBA *Gold*: 33 frames) they differ
    by more than the ±700 sweep, so the last track's CRC can never match and the CTDB
    gate becomes unpassable — see
    private/research/incoming/ctdb-failure-abba-gold-20260725.md."""
    stride = stride_wire * 2
    image_words = (bounds[-1] - bounds[0]) * _SPP * 2
    laststride = stride + image_words % stride
    s = bounds[track - 1] * _SPP + offset
    e = bounds[track] * _SPP + offset
    if track == 1:
        s += stride // 2
    if track == n_tracks:
        e -= laststride // 2
    if s < 0 or e * 4 > len(pcm):
        return None
    return zlib.crc32(pcm[s * 4 : e * 4]) & 0xFFFFFFFF


def _confirm_candidate(
    pcm: bytes, en: Entry, off: int, sweep_track: int, bounds: list[int], n_tracks: int
) -> Selection | None:
    damaged, unverifiable, matched = [], [], 0
    for t in range(1, n_tracks + 1):
        if t == sweep_track:
            matched += 1
            continue
        crc = track_crc_at(pcm, t, off, en.stride, bounds, n_tracks)
        if crc is None:
            unverifiable.append(t)
        elif crc == en.trackcrcs[t - 1]:
            matched += 1
        else:
            damaged.append(t)
    verifiable = n_tracks - len(unverifiable)
    if matched <= verifiable // 2:
        return None
    return Selection(en, off, damaged, unverifiable)


def select_entry(
    pcm: bytes, entries: list[Entry], bounds: list[int], n_tracks: int
) -> Selection | None:
    """Sweep one interior track for candidate (entry, offset) pairs, confirm on the
    rest; qualify on a majority of verifiable tracks. Prefer highest npar, then conf."""
    by_size = sorted(range(2, n_tracks), key=lambda t: bounds[t] - bounds[t - 1])
    for sweep_track in by_size:
        candidates: list[tuple[Entry, int]] = []
        for n in range(-_SWEEP_WINDOW, _SWEEP_WINDOW + 1):
            crc = track_crc_at(pcm, sweep_track, n, entries[0].stride, bounds, n_tracks)
            if crc is None:
                continue
            candidates.extend(
                (en, n) for en in entries if en.trackcrcs[sweep_track - 1] == crc
            )
        if not candidates:
            continue
        candidates.sort(key=lambda c: (-c[0].npar, -c[0].confidence))
        for en, off in candidates:
            sel = _confirm_candidate(pcm, en, off, sweep_track, bounds, n_tracks)
            if sel is not None:
                return sel
    return None


def fetch_parity(entry: Entry, cache: Path) -> Path:
    """Range-GET the syndrome ('parity') prefix, or reuse a correctly-sized cache."""
    want = entry.npar * (entry.stride * 2) * 2
    if cache.exists() and cache.stat().st_size == want:
        return cache
    req = urllib.request.Request(  # noqa: S310 — CTDB is plain-http by design
        entry.hasparity,
        headers={"User-Agent": "cdda2img/ctdb-repair", "Range": f"bytes=0-{want - 1}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        raw = resp.read()
    if len(raw) < want:
        msg = f"CTDB parity fetch short: {len(raw)} < {want}"
        raise RuntimeError(msg)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(raw[:want])
    return cache


def build_erasure_bitmap(c2_path: Path, nwords: int, align_pairs: int = -2) -> bytes:
    """C2 capture (294 B/sector, MSB-first per byte) -> per-word LSB-first erasure
    bitmap in the decoder's PCM word domain.

    Collapse per-byte -> per-sample-pair (any of 4 bytes flagged), shift by the drive's
    C2/audio offset (align_pairs, -2 on the PX-716A -- **re-measured 2026-08-01**
    through AccuDisc's ``Device.probe_c2_lag``, 5/5 conclusive runs on Tracy
    Chapman track 8 at 40x, ``lag_pairs=2`` every time; the underlying evidence
    swung 3.4x between runs, 1263-4302 flags, and the estimate did not move.
    It had been a literal carried on one earlier measurement and never re-checked),
    expand each pair to its 2 words,
    packbits. packbits (not fancy-index ``|=``) is mandatory: C2 flags cluster, so many
    words share a byte and fancy-index OR silently drops duplicates.

    Sign convention: align_pairs=-2 makes audio pair i read bitmap index i+2 — the C2
    bitmap LAGS the audio by 2 pairs. tools/modepage_experiment.py measures the same
    physical lag as k=+2 in its slice convention (TP-argmax vs an AR-verified oracle,
    precision 0.993 / recall 0.990, 2026-07-05). Do not "fix" either sign.

    **Domain: this bitmap spans our PCM, ``[0, lead-out)`` — deliberately, not by
    oversight.** The decoder works in CTDB's image domain and does the conversion on
    its side, skipping ``word_base / 8`` bytes (``word_base = bounds[0] * 1176``)
    before bucketing bits into grid cells — true of ``ctanalyse``
    (tools/ctanalyse/main.c, ``bits + skip``) and stated as a contract by AccuDisc's
    ``ctdb_repair``, whose ``erasures`` parameter is documented PCM-absolute. Narrowing
    this function to the image domain would apply the shift twice and silently displace
    every erasure by the length of the track-1 pre-gap. Given that domain confusion is
    exactly what caused the 2026-07-25 ABBA *Gold* failure, treat the asymmetry as
    load-bearing. Verified on real media with ``bounds[0]=33`` by
    tools/ctdb_erasure_origin_test.py: 6 errors in one column (over the error-only
    capacity of npar/2, under the erasure capacity of npar) repair bit-exactly with the
    correct bitmap, and fail both with no bitmap and with one shifted by ``word_base``."""
    raw = np.fromfile(c2_path, dtype=np.uint8)
    nsec = raw.size // 294
    bits = np.unpackbits(raw[: nsec * 294].reshape(nsec, 294), axis=1)
    pair = bits.reshape(nsec, _SPP, 4).any(axis=2).reshape(-1)
    er = np.zeros_like(pair)
    k = align_pairs
    if k <= 0:
        m = -k
        er[: er.size - m] = pair[m:] if m else pair
    else:
        er[k:] = pair[: pair.size - k]
    word_flag = np.repeat(er, 2)
    if word_flag.size < nwords:
        word_flag = np.concatenate([
            word_flag,
            np.zeros(nwords - word_flag.size, dtype=bool),
        ])
    else:
        word_flag = word_flag[:nwords]
    return np.packbits(word_flag, bitorder="little").tobytes()


def run_repair(
    pcm: bytes,
    parity: Path,
    entry: Entry,
    bounds: list[int],
    offset: int,
    erasures: bytes | None,
) -> CtdbRepairReport:
    """Decode *pcm* against the entry's parity blob at *offset*. Raises on failure.

    The domain conversion the caller must not do: *pcm* and *erasures* both span
    ``[0, lead-out)``, while CTDB's parity covers ``[bounds[0], bounds[-1])``.
    AccuDisc is told the window (``image_first_frame`` / ``image_frames``) and does
    the shift on both buffers itself. Narrowing either one here would apply it
    twice and silently displace every word by the length of track 1's pre-gap —
    the 2026-07-25 ABBA *Gold* failure.

    This is also where the binary's staleness guard used to live. ``ctanalyse``
    took the window as a ``--toc`` string that an older build parsed and ignored,
    analysing ``[0, lead-out)`` and returning confident nonsense, so the reported
    ``image_first_frame`` had to be checked against what we asked for. The window
    is now a pair of integer arguments to a linked library: there is no build that
    accepts them and analyses something else, and an extension skewed against
    ``libaccudisc`` raises ``AbiMismatch`` through the seam instead.
    """
    return accudisc_reader.ctdb_repair(
        pcm=pcm,
        parity=parity.read_bytes(),
        npar=entry.npar,
        wire_stride=entry.stride,
        image_first_frame=bounds[0],
        image_frames=bounds[-1] - bounds[0],
        offset_pairs=offset,
        erasures=erasures,
    )


@dataclass
class CtdbVerdict:
    """Per-track outcome of the CTDB CRC gate, split by the role each track played
    in the selection. ``ok`` iff nothing is unfixed and nothing regressed."""

    unfixed: list[int] = field(default_factory=list)  # called damaged, still wrong
    regressed: list[int] = field(default_factory=list)  # was clean, now wrong
    abstained: list[int] = field(default_factory=list)  # window outside the PCM

    @property
    def ok(self) -> bool:
        return not self.unfixed and not self.regressed

    def describe(self) -> str:
        parts = []
        if self.unfixed:
            parts.append("unfixed " + ",".join(str(t) for t in self.unfixed))
        if self.regressed:
            parts.append("regressed " + ",".join(str(t) for t in self.regressed))
        if self.abstained:
            parts.append("abstained " + ",".join(str(t) for t in self.abstained))
        return "; ".join(parts) or "all tracks match"


def verify_ctdb(
    pcm: bytes, sel: Selection, bounds: list[int], n_tracks: int
) -> CtdbVerdict:
    """Role-split CTDB CRC gate.

    Every track the selection called *damaged* must now match, and every track it
    called clean must *still* match; a track whose window falls outside the PCM
    abstains. Splitting by role (rather than one all-or-nothing boolean) is what
    makes the failure diagnosable — a rejected repair can say which tracks it
    failed to fix versus which ones it broke."""
    damaged = set(sel.damaged)
    verdict = CtdbVerdict()
    for t in range(1, n_tracks + 1):
        crc = track_crc_at(pcm, t, sel.offset, sel.entry.stride, bounds, n_tracks)
        if crc is None:
            verdict.abstained.append(t)
        elif crc != sel.entry.trackcrcs[t - 1]:
            (verdict.unfixed if t in damaged else verdict.regressed).append(t)
    return verdict


def verify_ar(
    pcm: bytes,
    tracks: list[int],
    track_lsns: list[int],
    disc_last_lsn: int,
    cddb_id: int,
    read_offset: int,
) -> bool:
    """AR gate at the drive read-offset: shift each track window by read_offset stereo
    samples (0 when the PCM is already offset-corrected)."""
    from cdda2img.accuraterip import fetch_ar_responses, match_track_pcm

    responses, _transport, _b3 = fetch_ar_responses(track_lsns, disc_last_lsn, cddb_id)
    if not responses:
        return False
    bounds = [*track_lsns, disc_last_lsn + 1]
    n_tracks = len(track_lsns)
    ok = True
    for t in tracks:
        s = bounds[t - 1] * _SPP + read_offset
        e = bounds[t] * _SPP + read_offset
        _v1, _v2, cv1, cv2 = match_track_pcm(pcm[s * 4 : e * 4], t, n_tracks, responses)
        ok &= bool(cv1 or cv2)
    return ok


def _repair_and_verify(
    pcm: bytes,
    pcm_path: Path,
    sel: Selection,
    parity: Path,
    bounds: list[int],
    n_tracks: int,
    track_lsns: list[int],
    disc_last_lsn: int,
    cddb_id: int,
    read_offset: int,
    erasures: bytes | None,
    used_c2: bool,
    verify_ar_gate: bool,
) -> CtdbRepairResult:
    """Decode, run both gates on the *repaired* buffer, and on success write it back
    to *pcm_path*. Any failure leaves the file — and *pcm* — untouched."""
    try:
        report = run_repair(pcm, parity, sel.entry, bounds, sel.offset, erasures)
    except (OSError, RuntimeError, ValueError) as exc:
        # A missing binding raises RuntimeError out of the seam, where it is
        # normally fatal. Here it must not be: a CTDB repair that cannot run is
        # one exit of the recovery ladder declining, and the AR re-read ladder
        # below it is still worth trying. "The engine is absent" is an outcome
        # about this disc's recovery, not a reason to abandon the rip.
        log.warning("CTDB parity repair failed: %s", exc)
        return CtdbRepairResult(
            False,
            "parity repair failed",
            entry_id=sel.entry.id,
            ctdb_offset=sel.offset,
            damaged_tracks=sel.damaged,
            used_c2=used_c2,
        )

    # Three outcomes, and the weaker success is kept distinct all the way to the
    # commit. `audio or audio_unverified` would be shorter, run, and lose the one
    # bit that says which claim the audio rests on.
    repaired = report.audio if report.audio is not None else report.audio_unverified
    if repaired is None:
        return CtdbRepairResult(
            False,
            "damage exceeds RS capacity",
            entry_id=sel.entry.id,
            ctdb_offset=sel.offset,
            damaged_tracks=sel.damaged,
            used_c2=used_c2,
        )
    if report.audio is None:
        # Accepting the weaker claim is sound *only* because of the gate below.
        # Every word an at-capacity repair can touch lies inside
        # [bounds[0], bounds[-1]) — precisely the window CTDB's per-track CRCs
        # cover — so verify_ctdb closes over it absolutely. Committing this
        # without that gate is the one thing AccuDisc's split exists to stop.
        log.info(
            "CTDB repair: %d column(s) determined but not verified — "
            "committing only if the per-track CRC gate passes",
            report.unverified_columns,
        )
    out = bytes(repaired)

    verdict = verify_ctdb(out, sel, bounds, n_tracks)
    if not verdict.ok:
        return CtdbRepairResult(
            False,
            f"CTDB CRC gate failed ({verdict.describe()})",
            entry_id=sel.entry.id,
            ctdb_offset=sel.offset,
            damaged_tracks=sel.damaged,
            used_c2=used_c2,
        )
    if verify_ar_gate and not verify_ar(
        out, sel.damaged, track_lsns, disc_last_lsn, cddb_id, read_offset
    ):
        return CtdbRepairResult(
            False,
            "AccurateRip gate failed",
            entry_id=sel.entry.id,
            ctdb_offset=sel.offset,
            damaged_tracks=sel.damaged,
            used_c2=used_c2,
        )

    pcm_path.write_bytes(out)
    return CtdbRepairResult(
        True,
        "repaired",
        entry_id=sel.entry.id,
        ctdb_offset=sel.offset,
        corrections=report.corrections,
        erasure_columns=report.erasure_columns,
        damaged_tracks=sel.damaged,
        used_c2=used_c2,
        unverified_columns=report.unverified_columns,
    )


def repair_whole_disc(
    pcm_path: Path,
    track_lsns: list[int],
    disc_last_lsn: int,
    cddb_id: int,
    read_offset: int,
    *,
    c2_path: Path | None = None,
    c2_align: int = -2,
    cache_dir: Path | None = None,
    verify_ar_gate: bool = True,
) -> CtdbRepairResult:
    """Attempt a CTDB parity repair of the whole-disc PCM at *pcm_path*.

    On success the repaired PCM is written back to *pcm_path* and repaired=True. On any
    failure (not in CTDB, no reconciling entry, over RS capacity, a gate rejects the
    result) the file is left untouched. *read_offset* is the drive offset for the AR
    gate — 0 when the PCM is already offset-corrected (the rip pipeline). Pass *c2_path*
    to feed C2 erasures.
    """
    bounds = [*track_lsns, disc_last_lsn + 1]
    n_tracks = len(track_lsns)
    cache = cache_dir or pcm_path.parent

    entries = load_entries(
        bounds, n_tracks, xml_cache=cache / "ctdb.xml" if cache_dir else None
    )
    if not entries:
        return CtdbRepairResult(False, "disc not in CTDB")

    pcm = pcm_path.read_bytes()
    sel = select_entry(pcm, entries, bounds, n_tracks)
    if sel is None:
        return CtdbRepairResult(False, "no CTDB entry reconciles with this rip")
    if not sel.damaged:
        return CtdbRepairResult(
            True,
            "ctdb: all tracks already match",
            entry_id=sel.entry.id,
            ctdb_offset=sel.offset,
        )

    try:
        parity = fetch_parity(sel.entry, cache / f"ctdb_parity_{sel.entry.id}.bin")
    except (OSError, RuntimeError) as exc:
        log.warning("CTDB parity fetch failed: %s", exc)
        return CtdbRepairResult(
            False, "CTDB parity fetch failed", entry_id=sel.entry.id
        )

    # Erasure-assisted first (roughly double the reconstruction capacity), then
    # error-only as a fallback. They are genuine alternatives, not one path: an
    # over-flagging C2 bitmap can spend erasure budget on clean words and turn a
    # decodable stride undecodable, so a failed erasure run says nothing about
    # whether error-only would have worked.
    #
    # *pcm* is passed straight in on every attempt, with no defensive copy. That
    # used to be mandatory — corrections were spliced in place, so a failed
    # attempt left a half-modified buffer for the next one to build on. AccuDisc
    # never mutates its input and returns the repaired audio as a fresh buffer,
    # so the hazard the copy defended against no longer exists.
    attempts: list[tuple[bytes | None, bool]] = [(None, False)]
    if c2_path and c2_path.exists():
        attempts.insert(
            0, (build_erasure_bitmap(c2_path, len(pcm) // 2, c2_align), True)
        )

    result = CtdbRepairResult(False, "no repair attempted")
    for eras, used_c2 in attempts:
        result = _repair_and_verify(
            pcm,
            pcm_path,
            sel,
            parity,
            bounds,
            n_tracks,
            track_lsns,
            disc_last_lsn,
            cddb_id,
            read_offset,
            erasures=eras,
            used_c2=used_c2,
            verify_ar_gate=verify_ar_gate,
        )
        if result.repaired:
            return result
        if used_c2:
            log.info(
                "CTDB erasure-assisted repair failed (%s); retrying error-only",
                result.reason,
            )
    return result
