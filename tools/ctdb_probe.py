#!/usr/bin/env python3
"""Standalone CTDB single-track probe (NEXT.md item 7 — experimental, NOT wired into src/).

Proves, end to end on a *single track*, that:
  * Step 0 — CTDB lookup (db.cuetools.net/lookup2.php) is a TOC-only HTTP GET, and
  * Step 1 — CTDB per-track verification is a plain CRC32 over that one track's PCM,

with no whole-disc / Galois-field computation. Parity *repair* (step 2) is deliberately
out of scope here — that is the only stage that needs the whole disc.

Usage:
    uv run python tools/ctdb_probe.py --validate            # positive control only (no drive)
    env TMPDIR=/var/tmp uv run python tools/ctdb_probe.py   # + bad 40x rip of track 8

The positive control slices track 8 out of the known-good "Tracy Chapman.rbi" and checks its
CRC32 against the CTDB response, confirming both the CRC recipe and that our archived rip is
CTDB-verified. The negative test does a fast 40x cd-paranoia rip of track 8 (expected to fail
AccurateRip) and shows CTDB flags it too.
"""

from __future__ import annotations

import argparse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass
from pathlib import Path

from cdda2img.container import read_header
from cdda2img.rbi_format import BLOCK_TYPE_PCM

_FRAME = 2352  # bytes per CD sector (588 stereo s16 samples)
_DEVICE = "/dev/sr0"
_READ_OFFSET = 30  # Plextor PX-716A, confirmed
_RBI = Path("Tracy Chapman.rbi")

# Track INDEX-01 LBAs (pregap-then-duration convention; reproduces AR id 000f3a54-00838029)
# plus the leadout. Pinned in-session against the stored AccurateRip disc ID.
_LSNS = [0, 12032, 34295, 49642, 57855, 72415, 93372, 111142, 120622, 135055, 148650]
_LEADOUT = 162892
_CDDB_ID = 0x99087B0B
_TOC = ":".join(str(x) for x in [*_LSNS, _LEADOUT])
_LOOKUP = (
    "http://db.cuetools.net/lookup2.php"
    "?version=3&ctdb=1&fuzzy=0&metadata=default&toc=" + _TOC
)

_TRACK = 8  # the historically-damaged track


@dataclass
class Entry:
    id: str
    confidence: int
    npar: int
    stride: int
    trackcrcs: list[int]


def fetch_ctdb() -> list[Entry]:
    """Step 0: TOC-only HTTP GET. Returns the parsed entries (TOC + PCM-free)."""
    req = urllib.request.Request(  # noqa: S310 — CTDB is plain-http by design
        _LOOKUP, headers={"User-Agent": "cdda2img/ctdb-probe"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        raw = resp.read()
    root = ET.fromstring(raw)  # noqa: S314 — structured attrs, CRC-gated downstream
    ns = {"c": "http://db.cuetools.net/ns/mmd-1.0#"}
    entries: list[Entry] = []
    for e in root.findall("c:entry", ns):
        crcs = [int(x, 16) for x in (e.get("trackcrcs") or "").split()]
        entries.append(
            Entry(
                id=e.get("id", "?"),
                confidence=int(e.get("confidence", "0")),
                npar=int(e.get("npar", "0")),
                stride=int(e.get("stride", "0")),
                trackcrcs=crcs,
            )
        )
    return entries


def track_window(pcm: bytes, track: int) -> bytes:
    """Bytes for *track* (1-based) = [track.INDEX01, nextTrack.INDEX01) — the CTDB window."""
    start = _LSNS[track - 1] * _FRAME
    end = (_LSNS[track] if track < len(_LSNS) else _LEADOUT) * _FRAME
    return pcm[start:end]


def ctdb_crc(track_pcm: bytes) -> int:
    """Step 1: CTDB per-track CRC = plain reflected CRC32 over the track's s16le bytes."""
    return zlib.crc32(track_pcm) & 0xFFFFFFFF


def read_rbi_pcm(path: Path) -> bytes:
    hdr = read_header(path)
    blk = hdr.find_block(BLOCK_TYPE_PCM)
    if blk is None:
        msg = f"no PCM block in {path}"
        raise SystemExit(msg)
    data = path.read_bytes()
    return data[blk.offset : blk.offset + blk.length]


def report_ctdb_match(label: str, crc: int, entries: list[Entry], track: int) -> bool:
    """Compare a track's CRC against every entry's trackcrc[track-1]; print the verdict."""
    idx = track - 1
    matches = [e for e in entries if idx < len(e.trackcrcs) and e.trackcrcs[idx] == crc]
    total_conf = sum(e.confidence for e in matches)
    print(f"  {label}: our CRC32 = {crc:08x}")
    if matches:
        best = max(matches, key=lambda e: e.confidence)
        print(
            f"  {label}: CTDB MATCH — {len(matches)} entr(y/ies), "
            f"total confidence {total_conf} "
            f"(top id={best.id} conf={best.confidence} npar={best.npar})"
        )
    else:
        top = max(entries, key=lambda e: e.confidence)
        print(
            f"  {label}: CTDB NO-MATCH — track {track} differs from all "
            f"{len(entries)} pressings (dominant conf={top.confidence} "
            f"expects {top.trackcrcs[idx]:08x})"
        )
    return bool(matches)


def best_offset_match(
    pcm: bytes, track: int, entries: list[Entry], window: int = 700
) -> None:
    """CRC offset-sweep: reconcile a track to CTDB across ±window samples (no Galois field).

    Plain per-track CRC is offset-sensitive, so an offset-0 compare only matches submitters
    at our exact offset. Sweeping the offset (shifting the window, borrowing from neighbours
    in the full-disc PCM) recovers the other offset cohorts — including the dominant one —
    using nothing but CRC32. This is the verification-side answer to the offset problem;
    only *repair* needs the whole-disc syndrome.
    """
    idx = track - 1
    start = _LSNS[track - 1] * _FRAME
    end = (_LSNS[track] if track < len(_LSNS) else _LEADOUT) * _FRAME
    by_offset: dict[int, int] = {}  # offset -> total confidence
    for n in range(-window, window + 1):
        s, e = start + n * 4, end + n * 4
        if s < 0 or e > len(pcm):
            continue
        crc = zlib.crc32(pcm[s:e]) & 0xFFFFFFFF
        conf = sum(
            en.confidence
            for en in entries
            if idx < len(en.trackcrcs) and en.trackcrcs[idx] == crc
        )
        if conf:
            by_offset[n] = conf
    if not by_offset:
        print("  offset-sweep: no CTDB match at any offset")
        return
    best = max(by_offset, key=lambda k: by_offset[k])
    print(
        f"  offset-sweep: reconciled confidence {by_offset[best]} at offset {best:+d} samples "
        f"(vs {by_offset.get(0, 0)} at offset 0) across {len(by_offset)} offset cohort(s)"
    )


def positive_control(entries: list[Entry]) -> None:
    print(f"\n== Positive control: good track {_TRACK} from {_RBI.name} ==")
    if not _RBI.exists():
        print(f"  SKIP — {_RBI} not found")
        return
    pcm = read_rbi_pcm(_RBI)
    good = track_window(pcm, _TRACK)
    print(
        f"  window [{_LSNS[_TRACK - 1]}, {_LSNS[_TRACK]}) = {len(good) // _FRAME} frames"
    )
    report_ctdb_match("good", ctdb_crc(good), entries, _TRACK)
    best_offset_match(pcm, _TRACK, entries)


def bad_rip(entries: list[Entry], speed: int) -> None:
    print(f"\n== Negative test: fresh {speed}x cd-paranoia rip of track {_TRACK} ==")
    import tempfile

    from cdda2img.accuraterip import fetch_ar_responses, match_track_pcm
    from cdda2img.disc_reader import rip_single_track

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "t8.pcm"
        print(
            f"  ripping track {_TRACK} at {speed}x (paranoia off, -O {_READ_OFFSET})…"
        )
        rip_single_track(
            _DEVICE,
            _TRACK,
            out,
            paranoia="off",
            read_offset=_READ_OFFSET,
            read_speed=speed,
        )
        raw = out.read_bytes()
    print(f"  ripped {len(raw)} bytes = {len(raw) // _FRAME} frames")

    # AccurateRip cross-check
    print("  AccurateRip verify…")
    responses, transport, _b3 = fetch_ar_responses(_LSNS, _LEADOUT - 1, _CDDB_ID)
    _v1, _v2, cv1, cv2 = match_track_pcm(raw, _TRACK, len(_LSNS), responses)
    ar_ok = bool(cv1 or cv2)
    print(
        f"  AccurateRip ({transport}): {'MATCH' if ar_ok else 'MISMATCH'} (v1={cv1} v2={cv2})"
    )

    # CTDB CRC verify
    report_ctdb_match("bad ", ctdb_crc(raw), entries, _TRACK)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--validate", action="store_true", help="positive control only, no drive"
    )
    ap.add_argument(
        "--speed", type=int, default=40, help="bad-rip read speed (default 40x)"
    )
    args = ap.parse_args()

    print(f"Step 0: CTDB lookup (TOC-only GET)\n  toc={_TOC}")
    entries = fetch_ctdb()
    top = sorted(entries, key=lambda e: -e.confidence)[:3]
    print(f"  {len(entries)} entries; top:")
    for e in top:
        print(f"    id={e.id} conf={e.confidence} npar={e.npar} stride={e.stride}")

    positive_control(entries)
    if not args.validate:
        bad_rip(entries, args.speed)


if __name__ == "__main__":
    main()
