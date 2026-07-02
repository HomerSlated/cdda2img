#!/usr/bin/env python3
"""Generate the ctanalyse test corpus (Phase 1 of docs/reference/ctanalyse_plan.md).

Produces, under private/testdata/ctanalyse/ (all regenerable, gitignored):

  good.pcm     whole-disc s16le PCM extracted from the AR-verified "Tracy Chapman.rbi"
  bad40x.pcm   full-disc cd-paranoia rip at 40x, paranoia off, -O 30 (needs the disc)
  splice8.pcm  good.pcm with the 40x rip's track 8 spliced over [LBA 111142, 120622)
  ctdb.xml     cached CTDB lookup response (raw XML)
  parity.bin   first npar*stride*2 bytes of the top entry's parity file (Range GET,
               stride = wire stride * 2 per DBEntry.cs)

Usage:
    uv run python tools/make_ctanalyse_testdata.py             # everything (needs disc)
    uv run python tools/make_ctanalyse_testdata.py --skip-rip  # derived artifacts only
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ctdb_probe import (  # type: ignore[unresolved-import]
    _CDDB_ID,
    _DEVICE,
    _FRAME,
    _LEADOUT,
    _LOOKUP,
    _LSNS,
    _RBI,
    _READ_OFFSET,
    ctdb_crc,
    read_rbi_pcm,
    track_window,
)

_OUT = Path("private/testdata/ctanalyse")
_DISC_BYTES = _LEADOUT * _FRAME
_RIP_SPEED = 40
_TRACK8_CRC = 0xC9719806  # CTDB consensus CRC for track 8 (entry 67116, our domain)


def fetch_lookup(dest: Path) -> ET.Element:
    """Fetch (or reuse) the raw CTDB lookup XML; return the parsed root."""
    if dest.exists():
        print(f"  {dest.name}: cached")
        return ET.fromstring(dest.read_bytes())  # noqa: S314 — local cached file
    req = urllib.request.Request(  # noqa: S310 — CTDB is plain-http by design
        _LOOKUP, headers={"User-Agent": "cdda2img/ctdb-probe"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        raw = resp.read()
    dest.write_bytes(raw)
    print(f"  {dest.name}: fetched ({len(raw)} bytes)")
    return ET.fromstring(raw)  # noqa: S314 — structured attrs, CRC-gated downstream


def top_entry(root: ET.Element) -> dict[str, str]:
    ns = {"c": "http://db.cuetools.net/ns/mmd-1.0#"}
    entries = root.findall("c:entry", ns)
    if not entries:
        msg = "CTDB lookup returned no entries"
        raise SystemExit(msg)
    best = max(entries, key=lambda e: int(e.get("confidence", "0")))
    return dict(best.attrib)


def fetch_parity(entry: dict[str, str], dest: Path) -> None:
    """Range-GET the syndrome-format parity prefix (npar * internal_stride * 2 bytes)."""
    npar = int(entry["npar"])
    stride = int(entry["stride"]) * 2  # DBEntry.cs: internal stride = wire stride * 2
    want = npar * stride * 2
    if dest.exists() and dest.stat().st_size == want:
        print(f"  {dest.name}: cached ({want} bytes)")
        return
    url = entry["hasparity"]
    req = urllib.request.Request(  # noqa: S310 — CTDB is plain-http by design
        url,
        headers={
            "User-Agent": "cdda2img/ctdb-probe",
            "Range": f"bytes=0-{want - 1}",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        raw = resp.read()
    if len(raw) < want:
        msg = f"parity fetch short: got {len(raw)}, want {want} ({url})"
        raise SystemExit(msg)
    dest.write_bytes(raw[:want])
    print(
        f"  {dest.name}: fetched {want} bytes (npar={npar} stride={stride}) from {url}"
    )


def extract_good(dest: Path) -> bytes:
    if dest.exists() and dest.stat().st_size == _DISC_BYTES:
        print(f"  {dest.name}: cached")
        return dest.read_bytes()
    pcm = read_rbi_pcm(_RBI)
    if len(pcm) != _DISC_BYTES:
        msg = f"RBI PCM is {len(pcm)} bytes, expected {_DISC_BYTES}"
        raise SystemExit(msg)
    dest.write_bytes(pcm)
    print(f"  {dest.name}: extracted {len(pcm)} bytes from {_RBI.name}")
    return pcm


def rip_bad(dest: Path) -> bytes:
    if dest.exists() and dest.stat().st_size == _DISC_BYTES:
        print(f"  {dest.name}: cached")
        return dest.read_bytes()
    from cdda2img.disc_reader import rip_single_track
    from cdda2img.drive_speed import restore_drive_speed

    n_tracks = len(_LSNS)
    bounds = [*_LSNS, _LEADOUT]
    try:
        with dest.open("wb") as out:
            for t in range(1, n_tracks + 1):
                expected = bounds[t] - bounds[t - 1]
                tmp = dest.with_suffix(f".t{t:02d}")
                print(
                    f"  ripping track {t:2d}/{n_tracks} at {_RIP_SPEED}x "
                    f"({expected} sectors)…"
                )
                rip_single_track(
                    _DEVICE,
                    t,
                    tmp,
                    paranoia="off",
                    read_offset=_READ_OFFSET,
                    read_speed=_RIP_SPEED,
                    restore_speed=False,
                )
                raw = tmp.read_bytes()
                tmp.unlink()
                if len(raw) != expected * _FRAME:
                    msg = (
                        f"track {t}: ripped {len(raw)} bytes, "
                        f"expected {expected * _FRAME} — TOC disagreement, aborting"
                    )
                    raise SystemExit(msg)
                out.write(raw)
    finally:
        restore_drive_speed(_DEVICE)
    print(f"  {dest.name}: ripped {dest.stat().st_size} bytes")
    return dest.read_bytes()


def make_splice(good: bytes, bad: bytes, dest: Path, track: int = 8) -> None:
    start = _LSNS[track - 1] * _FRAME
    end = _LSNS[track] * _FRAME
    spliced = good[:start] + bad[start:end] + good[end:]
    if len(spliced) != _DISC_BYTES:
        msg = f"splice size {len(spliced)} != {_DISC_BYTES}"
        raise SystemExit(msg)
    dest.write_bytes(spliced)
    print(f"  {dest.name}: track {track} window [{start}, {end}) from bad40x")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--skip-rip", action="store_true", help="skip the 40x rip (no drive)"
    )
    args = ap.parse_args()

    _OUT.mkdir(parents=True, exist_ok=True)

    print("CTDB lookup + parity cache")
    root = fetch_lookup(_OUT / "ctdb.xml")
    entry = top_entry(root)
    print(
        f"  top entry: id={entry.get('id')} conf={entry.get('confidence')} "
        f"npar={entry.get('npar')} stride={entry.get('stride')}"
    )
    fetch_parity(entry, _OUT / "parity.bin")

    print("Good PCM (from RBI)")
    good = extract_good(_OUT / "good.pcm")

    if args.skip_rip:
        print("Bad PCM: skipped (--skip-rip)")
        return

    print(f"Bad PCM ({_RIP_SPEED}x cd-paranoia, paranoia off, -O {_READ_OFFSET})")
    bad = rip_bad(_OUT / "bad40x.pcm")

    print("Splice corpus")
    make_splice(good, bad, _OUT / "splice8.pcm")

    print("Verdicts (CTDB per-track CRC, track 8, our domain)")
    g8, b8 = ctdb_crc(track_window(good, 8)), ctdb_crc(track_window(bad, 8))
    print(f"  good  track 8: {g8:08x}  (repair target {_TRACK8_CRC:08x})")
    print(
        f"  bad   track 8: {b8:08x}  ({'DIFFERS — usable corpus' if b8 != g8 else 'IDENTICAL — 40x rip came out clean, re-rip needed'})"
    )
    print(f"  disc id refs: cddb={_CDDB_ID:08x} leadout={_LEADOUT}")


if __name__ == "__main__":
    main()
