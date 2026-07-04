"""CTDB (CUETools Database) Reed-Solomon parity repair of whole-disc CD-DA PCM.

This is the canonical CTDB-repair logic used by both the rip pipeline
(:func:`repair_whole_disc`) and the standalone ``tools/ctdb_repair.py`` CLI. It
owns the network (CTDB lookup + parity fetch) and policy (entry selection,
double-gate verification), and delegates the Reed-Solomon math to the ``ctanalyse``
binary (on ``$PATH``).

The drive's C2 error pointers, when available, are fed to ctanalyse as *erasures*,
which roughly doubles the damage it can reconstruct (correction holds when
``e + 2t <= npar`` vs ``2t <= npar`` error-only). C2 is only a modifier: with it
absent/disabled, error-only ctanalyse still repairs, so recovery is never disabled —
only the erasure boost.

Repair is safe by construction: corrections are applied to a copy, old-byte-checked,
and only committed if BOTH gates pass — CTDB per-track CRC (at the entry's consensus
offset) AND AccurateRip (at the drive read-offset). A miscorrection fails a gate and
is discarded, leaving the original PCM untouched.
"""

from __future__ import annotations

import logging
import struct
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

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
    samples, the last track the final laststride/2. None if out of range."""
    stride = stride_wire * 2
    laststride = stride + (len(pcm) // 2) % stride
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
    bitmap in ctanalyse's PCM word domain.

    Collapse per-byte -> per-sample-pair (any of 4 bytes flagged), shift by the drive's
    C2/audio offset (align_pairs, -2 on the PX-716A), expand each pair to its 2 words,
    packbits. packbits (not fancy-index ``|=``) is mandatory: C2 flags cluster, so many
    words share a byte and fancy-index OR silently drops duplicates."""
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


def run_ctanalyse(
    pcm_path: Path,
    parity: Path,
    entry: Entry,
    bounds: list[int],
    erasures: Path | None,
    binary: str = "ctanalyse",
) -> dict:
    """Invoke the ctanalyse binary; return its parsed JSON. Raises on non-zero exit."""
    toc = ":".join(str(x) for x in bounds)
    argv = [
        binary,
        "--pcm",
        str(pcm_path),
        "--parity",
        str(parity),
        "--npar",
        str(entry.npar),
        "--stride",
        str(entry.stride),
        "--toc",
        toc,
    ]
    if erasures is not None:
        argv += ["--erasures", str(erasures)]
    proc = subprocess.run(  # noqa: S603 — fixed binary on $PATH; args are numeric/paths
        argv, capture_output=True, text=True, timeout=600, check=False
    )
    if proc.returncode != 0:
        msg = f"ctanalyse exited {proc.returncode}: {proc.stderr.strip()}"
        raise RuntimeError(msg)
    import json

    return json.loads(proc.stdout)


def apply_corrections(pcm: bytearray, corrections: list[dict]) -> int | None:
    """Splice corrections in place. Returns the index of the first old-byte mismatch
    (caller discards the buffer), or None on full success."""
    for i, c in enumerate(corrections):
        byte, old, new = c["byte"], c["old"], c["new"]
        if byte < 0 or byte + 2 > len(pcm) or byte % 2:
            return i
        (cur,) = struct.unpack_from("<H", pcm, byte)
        if cur != old:
            return i
        struct.pack_into("<H", pcm, byte, new)
    return None


def verify_ctdb(pcm: bytes, sel: Selection, bounds: list[int], n_tracks: int) -> bool:
    ok = True
    for t in range(1, n_tracks + 1):
        crc = track_crc_at(pcm, t, sel.offset, sel.entry.stride, bounds, n_tracks)
        if crc is None:
            continue
        if crc != sel.entry.trackcrcs[t - 1]:
            ok = False
    return ok


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


def _ctanalyse_and_verify(
    pcm: bytearray,
    pcm_path: Path,
    sel: Selection,
    parity: Path,
    bounds: list[int],
    n_tracks: int,
    track_lsns: list[int],
    disc_last_lsn: int,
    cddb_id: int,
    read_offset: int,
    eras_path: Path | None,
    used_c2: bool,
    ctanalyse_bin: str,
    verify_ar_gate: bool,
) -> CtdbRepairResult:
    """Run ctanalyse, apply corrections to *pcm*, run both gates, and on success write
    the repaired PCM back to *pcm_path*. Any failure leaves the file untouched."""
    try:
        result = run_ctanalyse(
            pcm_path, parity, sel.entry, bounds, eras_path, ctanalyse_bin
        )
    except (OSError, RuntimeError, ValueError) as exc:
        log.warning("ctanalyse failed: %s", exc)
        return CtdbRepairResult(
            False, "ctanalyse failed", entry_id=sel.entry.id, used_c2=used_c2
        )

    if not result.get("can_recover"):
        return CtdbRepairResult(
            False,
            "damage exceeds RS capacity",
            entry_id=sel.entry.id,
            damaged_tracks=sel.damaged,
            used_c2=used_c2,
        )

    corrections = result.get("corrections", [])
    if apply_corrections(pcm, corrections) is not None:
        return CtdbRepairResult(
            False,
            "old-byte mismatch (splice aborted)",
            entry_id=sel.entry.id,
            used_c2=used_c2,
        )

    if not verify_ctdb(bytes(pcm), sel, bounds, n_tracks):
        return CtdbRepairResult(
            False, "CTDB CRC gate failed", entry_id=sel.entry.id, used_c2=used_c2
        )
    if verify_ar_gate and not verify_ar(
        bytes(pcm), sel.damaged, track_lsns, disc_last_lsn, cddb_id, read_offset
    ):
        return CtdbRepairResult(
            False, "AccurateRip gate failed", entry_id=sel.entry.id, used_c2=used_c2
        )

    pcm_path.write_bytes(pcm)
    return CtdbRepairResult(
        True,
        "repaired",
        entry_id=sel.entry.id,
        ctdb_offset=sel.offset,
        corrections=len(corrections),
        erasure_columns=int(result.get("erasure_columns", 0)),
        damaged_tracks=sel.damaged,
        used_c2=used_c2,
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
    ctanalyse_bin: str = "ctanalyse",
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

    pcm = bytearray(pcm_path.read_bytes())
    sel = select_entry(bytes(pcm), entries, bounds, n_tracks)
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

    eras_path = None
    used_c2 = False
    if c2_path and c2_path.exists():
        eras_path = cache / "ctdb_erasures.bin"
        eras_path.write_bytes(build_erasure_bitmap(c2_path, len(pcm) // 2, c2_align))
        used_c2 = True

    return _ctanalyse_and_verify(
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
        eras_path,
        used_c2,
        ctanalyse_bin,
        verify_ar_gate,
    )
