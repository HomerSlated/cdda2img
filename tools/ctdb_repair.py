#!/usr/bin/env python3
"""CTDB repair driver — Phase 2 of docs/reference/ctanalyse_plan.md (experimental, NOT in src/).

Owns network, policy and writes; delegates pure math to a ctanalyse subprocess:

  1. CTDB lookup (cached XML or live GET)
  2. entry selection — highest npar among entries our clean tracks reconcile to,
     located via a CRC offset-sweep on one clean track, confirmed on the rest
  3. parity fetch (cached or Range GET), only once committed to a repair
  4. invoke ctanalyse (stub or real) → JSON corrections in OUR sample domain
  5. apply corrections with an old-byte check — ANY mismatch aborts the whole splice
  6. double gate: CTDB per-track CRCs at the entry's offset AND our AccurateRip
     on the repaired tracks; both must pass or the repair is discarded

Usage:
    uv run python tools/ctdb_repair.py private/testdata/ctanalyse/bad40x.pcm \
        --ctanalyse "python tools/ctanalyse/stub_ctanalyse.py"

Exit codes: 0 repaired+verified (or already clean); 1 operational error;
3 ctanalyse reports unrecoverable; 4 verification failed (repair discarded);
5 old-byte mismatch (splice aborted).
"""

from __future__ import annotations

import argparse
import json
import shlex
import struct
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ctdb_probe import (  # type: ignore[unresolved-import]
    _CDDB_ID,
    _FRAME,
    _LEADOUT,
    _LOOKUP,
    _LSNS,
)

_CACHE = Path("private/testdata/ctanalyse")
_SWEEP_WINDOW = 700  # offset sweep range in stereo samples, ±
_BOUNDS = [*_LSNS, _LEADOUT]
_N_TRACKS = len(_LSNS)

EXIT_OK = 0
EXIT_ERR = 1
EXIT_UNRECOVERABLE = 3
EXIT_VERIFY_FAILED = 4
EXIT_BAD_OLD = 5


@dataclass
class Entry:
    id: str
    confidence: int
    npar: int
    stride: int  # wire value
    hasparity: str
    trackcrcs: list[int]


@dataclass
class Selection:
    entry: Entry
    offset: int  # stereo samples, our-domain -> entry-domain window shift
    damaged: list[int] = field(default_factory=list)
    unverifiable: list[int] = field(default_factory=list)


def load_entries(xml_path: Path | None) -> list[Entry]:
    if xml_path and xml_path.exists():
        raw = xml_path.read_bytes()
        print(f"lookup: cached {xml_path}")
    else:
        req = urllib.request.Request(  # noqa: S310 — CTDB is plain-http by design
            _LOOKUP, headers={"User-Agent": "cdda2img/ctdb-repair"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            raw = resp.read()
        print("lookup: live GET")
    ns = {"c": "http://db.cuetools.net/ns/mmd-1.0#"}
    entries = []

    for e in ET.fromstring(raw).findall("c:entry", ns):  # noqa: S314
        crcs = [int(x, 16) for x in (e.get("trackcrcs") or "").split()]
        if len(crcs) != _N_TRACKS or not e.get("hasparity"):
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


def track_crc_at(pcm: bytes, track: int, offset: int, stride_wire: int) -> int | None:
    """CTDB per-track CRC32 of *track*'s window shifted by *offset* stereo samples.

    Matches AccurateRip.cs:CTDBCRC (confirmed empirically 2026-07-02): interior tracks
    are full [INDEX01, next INDEX01) windows; track 1 excludes the first stride/2
    stereo samples of the disc and the last track excludes the final laststride/2 —
    the disc-edge regions are offset-cohort-dependent, so the DB leaves them out.
    Returns None if the shifted window falls outside the PCM.
    """
    stride = stride_wire * 2  # internal stride, in 16-bit words
    laststride = stride + (len(pcm) // 2) % stride
    s = _BOUNDS[track - 1] * 588 + offset
    e = _BOUNDS[track] * 588 + offset
    if track == 1:
        s += stride // 2
    if track == _N_TRACKS:
        e -= laststride // 2
    if s < 0 or e * 4 > len(pcm):
        return None
    return zlib.crc32(pcm[s * 4 : e * 4]) & 0xFFFFFFFF


def _confirm_candidate(
    pcm: bytes, en: Entry, off: int, sweep_track: int
) -> Selection | None:
    """Check every other track against *en* at *off*; accept when a majority of
    verifiable tracks match (mismatches become the damaged set)."""
    damaged, unverifiable, matched = [], [], 0
    for t in range(1, _N_TRACKS + 1):
        if t == sweep_track:
            matched += 1
            continue
        crc = track_crc_at(pcm, t, off, en.stride)
        if crc is None:
            unverifiable.append(t)
        elif crc == en.trackcrcs[t - 1]:
            matched += 1
        else:
            damaged.append(t)
    verifiable = _N_TRACKS - len(unverifiable)
    if matched <= verifiable // 2:
        return None
    print(
        f"  selected entry id={en.id} conf={en.confidence} npar={en.npar} "
        f"@ offset {off:+d} (matched {matched}/{verifiable}, "
        f"damaged={damaged or 'none'}, unverifiable={unverifiable or 'none'})"
    )
    return Selection(en, off, damaged, unverifiable)


def select_entry(pcm: bytes, entries: list[Entry]) -> Selection | None:
    """Pick the repair entry: sweep one track for candidate (entry, offset) pairs, then
    confirm on all other tracks; qualify when a majority of verifiable tracks match.
    Preference order: highest npar, then highest confidence."""
    # Sweep the shortest INTERIOR track first — cheapest, and interior windows are
    # stride-independent (edge tracks' CRC windows depend on each entry's stride).
    by_size = sorted(range(2, _N_TRACKS), key=lambda t: _BOUNDS[t] - _BOUNDS[t - 1])

    for sweep_track in by_size:
        candidates: list[tuple[Entry, int]] = []
        for n in range(-_SWEEP_WINDOW, _SWEEP_WINDOW + 1):
            crc = track_crc_at(pcm, sweep_track, n, entries[0].stride)
            if crc is None:
                continue
            candidates.extend(
                (en, n) for en in entries if en.trackcrcs[sweep_track - 1] == crc
            )
        if not candidates:
            print(
                f"  sweep track {sweep_track}: no entry matches (damaged?) — trying next"
            )
            continue

        candidates.sort(key=lambda c: (-c[0].npar, -c[0].confidence))
        for en, off in candidates:
            sel = _confirm_candidate(pcm, en, off, sweep_track)
            if sel is not None:
                return sel
        print(f"  sweep track {sweep_track}: candidates found but confirmation failed")
    return None


def fetch_parity(entry: Entry, cache: Path) -> Path:
    npar, stride_int = entry.npar, entry.stride * 2
    want = npar * stride_int * 2
    if cache.exists() and cache.stat().st_size == want:
        print(f"parity: cached {cache} ({want} bytes)")
        return cache
    req = urllib.request.Request(  # noqa: S310 — CTDB is plain-http by design
        entry.hasparity,
        headers={"User-Agent": "cdda2img/ctdb-repair", "Range": f"bytes=0-{want - 1}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        raw = resp.read()
    if len(raw) < want:
        msg = f"parity fetch short: {len(raw)} < {want}"
        raise SystemExit(msg)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(raw[:want])
    print(f"parity: fetched {want} bytes from {entry.hasparity}")
    return cache


def run_ctanalyse(cmd: str, pcm_path: Path, parity: Path, entry: Entry) -> dict:
    toc = ":".join(str(x) for x in _BOUNDS)
    argv = [
        *shlex.split(cmd),
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
    print(
        f"ctanalyse: {' '.join(argv[:2])} … (npar={entry.npar} stride={entry.stride})"
    )
    proc = subprocess.run(  # noqa: S603
        argv, capture_output=True, text=True, timeout=600, check=False
    )
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        msg = f"ctanalyse failed with exit {proc.returncode}"
        raise SystemExit(msg)
    return json.loads(proc.stdout)


def apply_corrections(pcm: bytearray, corrections: list[dict]) -> int | None:
    """Splice corrections in place. Returns the index of the first old-byte mismatch,
    or None on full success. The caller discards the buffer on mismatch."""
    for i, c in enumerate(corrections):
        byte, old, new = c["byte"], c["old"], c["new"]
        if byte < 0 or byte + 2 > len(pcm) or byte % 2:
            return i
        (cur,) = struct.unpack_from("<H", pcm, byte)
        if cur != old:
            return i
        struct.pack_into("<H", pcm, byte, new)
    return None


def verify_ctdb(pcm: bytes, sel: Selection) -> bool:
    ok = True
    for t in range(1, _N_TRACKS + 1):
        crc = track_crc_at(pcm, t, sel.offset, sel.entry.stride)
        if crc is None:
            print(
                f"  ctdb gate: track {t:2d} unverifiable at offset {sel.offset:+d} (skip)"
            )
            continue
        want = sel.entry.trackcrcs[t - 1]
        status = "OK" if crc == want else f"MISMATCH (want {want:08x})"
        if crc != want:
            ok = False
        print(f"  ctdb gate: track {t:2d} crc {crc:08x} {status}")
    return ok


def verify_ar(pcm: bytes, tracks: list[int]) -> bool:
    from cdda2img.accuraterip import fetch_ar_responses, match_track_pcm

    responses, transport, _b3 = fetch_ar_responses(_LSNS, _LEADOUT - 1, _CDDB_ID)
    if not responses:
        print("  ar gate: disc not in AccurateRip — gate cannot pass")
        return False
    ok = True
    for t in tracks:
        raw = pcm[_BOUNDS[t - 1] * _FRAME : _BOUNDS[t] * _FRAME]
        _v1, _v2, cv1, cv2 = match_track_pcm(raw, t, _N_TRACKS, responses)
        matched = bool(cv1 or cv2)
        ok &= matched
        print(
            f"  ar gate ({transport}): track {t:2d} "
            f"{'MATCH' if matched else 'MISMATCH'} (v1={cv1} v2={cv2})"
        )
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pcm", type=Path, help="damaged whole-disc s16le PCM")
    ap.add_argument("--ctanalyse", default="tools/ctanalyse/ctanalyse")
    ap.add_argument("--out", type=Path, default=None, help="default <pcm>.repaired.pcm")
    ap.add_argument("--xml", type=Path, default=_CACHE / "ctdb.xml")
    ap.add_argument("--parity", type=Path, default=_CACHE / "parity.bin")
    ap.add_argument("--no-ar", action="store_true", help="skip the AccurateRip gate")
    args = ap.parse_args()

    pcm = bytearray(args.pcm.read_bytes())
    if len(pcm) != _LEADOUT * _FRAME:
        print(f"pcm size {len(pcm)} != disc size {_LEADOUT * _FRAME}", file=sys.stderr)
        return EXIT_ERR

    entries = load_entries(args.xml)
    print(f"lookup: {len(entries)} usable entries")
    sel = select_entry(bytes(pcm), entries)
    if sel is None:
        print("no CTDB entry reconciles with this rip — cannot repair")
        return EXIT_UNRECOVERABLE
    if not sel.damaged:
        print("all verifiable tracks already match CTDB — nothing to repair")
        return EXIT_OK

    parity = fetch_parity(sel.entry, args.parity)
    result = run_ctanalyse(args.ctanalyse, args.pcm, parity, sel.entry)

    if not result.get("can_recover"):
        print(
            "ctanalyse: can_recover=false — damage exceeds RS capacity, keeping original"
        )
        return EXIT_UNRECOVERABLE
    corrections = result.get("corrections", [])
    print(
        f"ctanalyse: can_recover=true, {len(corrections)} corrections, "
        f"offset={result.get('offset')}, "
        f"affected sectors={len(result.get('affected_sectors', []))}"
    )

    bad_idx = apply_corrections(pcm, corrections)
    if bad_idx is not None:
        c = corrections[bad_idx]
        print(
            f"SPLICE ABORTED: correction {bad_idx} at byte {c['byte']} — stored word "
            f"does not match claimed old value {c['old']:#06x}; discarding ALL corrections"
        )
        return EXIT_BAD_OLD
    print(
        f"splice: {len(corrections)} corrections applied (old-byte checks all passed)"
    )

    print("verification gate 1/2 — CTDB per-track CRCs")
    ctdb_ok = verify_ctdb(bytes(pcm), sel)
    ar_ok = True
    if args.no_ar:
        print("verification gate 2/2 — AccurateRip: SKIPPED (--no-ar)")
    else:
        print("verification gate 2/2 — AccurateRip (repaired tracks)")
        ar_ok = verify_ar(bytes(pcm), sel.damaged)
    if not (ctdb_ok and ar_ok):
        print("REPAIR DISCARDED: verification failed; original PCM left untouched")
        return EXIT_VERIFY_FAILED

    out = args.out or args.pcm.with_suffix(".repaired.pcm")
    out.write_bytes(pcm)
    print(f"repaired PCM written: {out}")
    print("prov (informational):")
    print(f"  repaired_via=ctdb:{sel.entry.id}@conf{sel.entry.confidence}")
    print(f"  repair_offset={sel.offset}")
    for t in sel.damaged:
        print(f"  recovery_track_{t}=ctdb_repaired")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
