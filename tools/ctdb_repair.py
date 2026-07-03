#!/usr/bin/env python3
"""CTDB repair driver — generalized, C2-erasure-aware (item 8, production path 2+3).

Owns network, policy and writes; delegates pure math to a ctanalyse subprocess:

  1. derive the disc (TOC → track LSNs, lead-out, CDDB id) live from --device
     (c2read --toc) or from an explicit --toc; no disc constants are hard-coded
  2. CTDB lookup (cached XML or live GET, keyed on the derived TOC)
  3. entry selection — highest npar among entries our clean tracks reconcile to,
     located via a CRC offset-sweep on one clean track, confirmed on the rest
  4. parity fetch (cached or Range GET), only once committed to a repair
  5. build C2 erasures (if a --c2 capture is present) and invoke ctanalyse →
     JSON corrections in OUR sample domain
  6. apply corrections with an old-byte check — ANY mismatch aborts the whole splice
  7. double gate: CTDB per-track CRCs at the entry's consensus offset AND our
     AccurateRip (at the drive read-offset) on the repaired tracks; both must pass

Two offsets, kept distinct: the CTDB *consensus* offset (our PCM ↔ CTDB parity,
detected by select_entry / ctanalyse) and the drive *read* offset (our PCM ↔
AccurateRip-absolute, --read-offset, for the AR gate).

Usage:
    # From files (raw whole-disc PCM + its C2 capture, e.g. a c2read --full pass):
    uv run python tools/ctdb_repair.py --pcm pass.pcm --c2 pass.c2 --toc L0:L1:…:LEADOUT
    # From the live disc (reads with C2, parks the spindle after):
    env TMPDIR=/var/tmp uv run python tools/ctdb_repair.py --device /dev/sr0 --read-offset 30

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

import numpy as np

_FRAME = 2352  # bytes per CD sector
_SPP = 588  # stereo sample-pairs per sector
_SWEEP_WINDOW = 700  # offset sweep range in stereo samples, ±
_C2READ = "c2read"  # resolved on $PATH (symlinked into ~/.local/bin)

EXIT_OK = 0
EXIT_ERR = 1
EXIT_UNRECOVERABLE = 3
EXIT_VERIFY_FAILED = 4
EXIT_BAD_OLD = 5


# ---- disc model (derived live, nothing pinned) ------------------------------


@dataclass
class Disc:
    lsns: list[int]
    leadout: int

    @property
    def bounds(self) -> list[int]:
        return [*self.lsns, self.leadout]

    @property
    def n_tracks(self) -> int:
        return len(self.lsns)

    @property
    def toc(self) -> str:
        return ":".join(str(x) for x in self.bounds)

    @property
    def cddb_id(self) -> int:
        from cdda2img.cddb import compute_cddb_disc_id

        return int(compute_cddb_disc_id(self.lsns, self.leadout - 1), 16)

    @property
    def lookup_url(self) -> str:
        return (
            "http://db.cuetools.net/lookup2.php"
            "?version=3&ctdb=1&fuzzy=0&metadata=default&toc=" + self.toc
        )


def disc_from_toc(toc: str) -> Disc:
    nums = [int(x) for x in toc.split(":")]
    if len(nums) < 3:
        msg = "--toc needs at least 2 tracks and a lead-out"
        raise SystemExit(msg)
    return Disc(nums[:-1], nums[-1])


def disc_from_device(device: str) -> Disc:
    """READ TOC via c2read --toc (no drive throttle)."""
    out = subprocess.run(  # noqa: S603 — fixed local tool
        [_C2READ, "--device", device, "--toc"],
        capture_output=True,
        text=True,
        check=True,
    )
    lsns: list[int] = []
    leadout: int | None = None
    for line in out.stdout.splitlines():
        p = line.split()
        if p[:1] == ["track"]:
            lsns.append(int(p[3]))
        elif p[:1] == ["leadout"]:
            leadout = int(p[2])
    if not lsns or leadout is None:
        msg = "could not derive TOC from device"
        raise SystemExit(msg)
    return Disc(lsns, leadout)


def c2_features_ok(device: str) -> bool:
    """True iff c2read --features reports the drive both advertises AND functionally
    supports C2 (exit 0). The `auto` gate for whether to use C2 erasures at all."""
    r = subprocess.run(  # noqa: S603 — fixed local tool
        [_C2READ, "--device", device, "--features"], capture_output=True, check=False
    )
    return r.returncode == 0


def read_disc(device: str, pcm_path: Path, c2_path: Path, speed: int) -> None:
    """Full-disc read WITH C2 via c2read; park the spindle afterwards."""
    print(f"reading disc {device} (full, +C2) @ {speed}x…")
    subprocess.run(  # noqa: S603 — fixed local tool
        [
            _C2READ,
            "--device",
            device,
            "--full",
            "--speed",
            str(speed),
            "-q",
            "--pcm",
            str(pcm_path),
            "--c2",
            str(c2_path),
        ],
        check=True,
    )
    subprocess.run(  # noqa: S603 — park spindle, done reading
        [_C2READ, "--device", device, "--stop", "-q"], capture_output=True, check=False
    )


# ---- C2 -> per-word erasure bitmap ------------------------------------------


def build_erasure_bitmap(c2_path: Path, nwords: int, align_pairs: int) -> bytes:
    """Turn a c2read C2 capture (294 B/sector, MSB-first per byte) into a per-word
    LSB-first erasure bitmap in ctanalyse's PCM word domain.

    Collapse per-byte → per-sample-pair (any of 4 bytes flagged), shift by the drive's
    C2/audio offset (align_pairs, -2 on the PX-716A per c2bench: the flag sits
    align_pairs ahead of the error it marks), expand each pair to its 2 words, and
    packbits. packbits (not |=) is mandatory: C2 flags cluster, so many words share a
    byte and fancy-index OR silently drops duplicates."""
    raw = np.fromfile(c2_path, dtype=np.uint8)
    nsec = raw.size // 294
    bits = np.unpackbits(raw[: nsec * 294].reshape(nsec, 294), axis=1)  # (nsec,2352)
    pair = bits.reshape(nsec, _SPP, 4).any(axis=2).reshape(-1)  # per sample-pair
    # er[i] = pair[i - align]: align=-2 → er[i] = pair[i+2]
    er = np.zeros_like(pair)
    k = align_pairs
    if k <= 0:
        m = -k
        er[: er.size - m] = pair[m:] if m else pair
    else:
        er[k:] = pair[: pair.size - k]
    word_flag = np.repeat(er, 2)  # each sample-pair → 2 words
    if word_flag.size < nwords:
        word_flag = np.concatenate([
            word_flag,
            np.zeros(nwords - word_flag.size, dtype=bool),
        ])
    else:
        word_flag = word_flag[:nwords]
    return np.packbits(word_flag, bitorder="little").tobytes()


# ---- CTDB entries + selection -----------------------------------------------


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
    offset: int  # stereo samples, our-domain → entry-domain window shift
    damaged: list[int] = field(default_factory=list)
    unverifiable: list[int] = field(default_factory=list)


def load_entries(xml_path: Path | None, disc: Disc) -> list[Entry]:
    if xml_path and xml_path.exists():
        raw = xml_path.read_bytes()
        print(f"lookup: cached {xml_path}")
    else:
        req = urllib.request.Request(  # noqa: S310 — CTDB is plain-http by design
            disc.lookup_url, headers={"User-Agent": "cdda2img/ctdb-repair"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            raw = resp.read()
        print("lookup: live GET")
    ns = {"c": "http://db.cuetools.net/ns/mmd-1.0#"}
    entries = []
    for e in ET.fromstring(raw).findall("c:entry", ns):  # noqa: S314
        crcs = [int(x, 16) for x in (e.get("trackcrcs") or "").split()]
        if len(crcs) != disc.n_tracks or not e.get("hasparity"):
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
    pcm: bytes, track: int, offset: int, stride_wire: int, disc: Disc
) -> int | None:
    """CTDB per-track CRC32 of *track*'s window shifted by *offset* stereo samples.

    Edge-aware (AccurateRip.cs:CTDBCRC): track 1 excludes the first stride/2 stereo
    samples, the last track the final laststride/2. Returns None if out of range."""
    stride = stride_wire * 2
    laststride = stride + (len(pcm) // 2) % stride
    s = disc.bounds[track - 1] * _SPP + offset
    e = disc.bounds[track] * _SPP + offset
    if track == 1:
        s += stride // 2
    if track == disc.n_tracks:
        e -= laststride // 2
    if s < 0 or e * 4 > len(pcm):
        return None
    return zlib.crc32(pcm[s * 4 : e * 4]) & 0xFFFFFFFF


def _confirm_candidate(
    pcm: bytes, en: Entry, off: int, sweep_track: int, disc: Disc
) -> Selection | None:
    damaged, unverifiable, matched = [], [], 0
    for t in range(1, disc.n_tracks + 1):
        if t == sweep_track:
            matched += 1
            continue
        crc = track_crc_at(pcm, t, off, en.stride, disc)
        if crc is None:
            unverifiable.append(t)
        elif crc == en.trackcrcs[t - 1]:
            matched += 1
        else:
            damaged.append(t)
    verifiable = disc.n_tracks - len(unverifiable)
    if matched <= verifiable // 2:
        return None
    print(
        f"  selected entry id={en.id} conf={en.confidence} npar={en.npar} "
        f"@ offset {off:+d} (matched {matched}/{verifiable}, "
        f"damaged={damaged or 'none'}, unverifiable={unverifiable or 'none'})"
    )
    return Selection(en, off, damaged, unverifiable)


def select_entry(pcm: bytes, entries: list[Entry], disc: Disc) -> Selection | None:
    """Sweep one interior track for candidate (entry, offset) pairs, confirm on the
    rest; qualify on a majority of verifiable tracks. Prefer highest npar, then conf."""
    by_size = sorted(
        range(2, disc.n_tracks), key=lambda t: disc.bounds[t] - disc.bounds[t - 1]
    )
    for sweep_track in by_size:
        candidates: list[tuple[Entry, int]] = []
        for n in range(-_SWEEP_WINDOW, _SWEEP_WINDOW + 1):
            crc = track_crc_at(pcm, sweep_track, n, entries[0].stride, disc)
            if crc is None:
                continue
            candidates.extend(
                (en, n) for en in entries if en.trackcrcs[sweep_track - 1] == crc
            )
        if not candidates:
            print(f"  sweep track {sweep_track}: no entry matches (damaged?) — next")
            continue
        candidates.sort(key=lambda c: (-c[0].npar, -c[0].confidence))
        for en, off in candidates:
            sel = _confirm_candidate(pcm, en, off, sweep_track, disc)
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


def run_ctanalyse(
    cmd: str,
    pcm_path: Path,
    parity: Path,
    entry: Entry,
    disc: Disc,
    erasures: Path | None,
) -> dict:
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
        disc.toc,
    ]
    if erasures is not None:
        argv += ["--erasures", str(erasures)]
    print(
        f"ctanalyse: {' '.join(argv[:2])} … (npar={entry.npar} stride={entry.stride}"
        f"{', +C2 erasures' if erasures else ''})"
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


def verify_ctdb(pcm: bytes, sel: Selection, disc: Disc) -> bool:
    ok = True
    for t in range(1, disc.n_tracks + 1):
        crc = track_crc_at(pcm, t, sel.offset, sel.entry.stride, disc)
        if crc is None:
            print(
                f"  ctdb gate: track {t:2d} unverifiable at offset {sel.offset:+d} (skip)"
            )
            continue
        want = sel.entry.trackcrcs[t - 1]
        if crc != want:
            ok = False
        print(
            f"  ctdb gate: track {t:2d} crc {crc:08x} "
            f"{'OK' if crc == want else f'MISMATCH (want {want:08x})'}"
        )
    return ok


def verify_ar(pcm: bytes, tracks: list[int], disc: Disc, read_offset: int) -> bool:
    """AR gate at the drive read-offset: AR wants absolute-domain audio, so shift each
    track window by read_offset stereo samples (0 for already-offset-corrected PCM)."""
    from cdda2img.accuraterip import fetch_ar_responses, match_track_pcm

    responses, transport, _b3 = fetch_ar_responses(
        disc.lsns, disc.leadout - 1, disc.cddb_id
    )
    if not responses:
        print("  ar gate: disc not in AccurateRip — gate cannot pass")
        return False
    ok = True
    for t in tracks:
        s = disc.bounds[t - 1] * _SPP + read_offset
        e = disc.bounds[t] * _SPP + read_offset
        raw = pcm[s * 4 : e * 4]
        _v1, _v2, cv1, cv2 = match_track_pcm(raw, t, disc.n_tracks, responses)
        matched = bool(cv1 or cv2)
        ok &= matched
        print(
            f"  ar gate ({transport}): track {t:2d} "
            f"{'MATCH' if matched else 'MISMATCH'} (v1={cv1} v2={cv2})"
        )
    return ok


def _resolve_inputs(args: argparse.Namespace) -> tuple[Disc, Path, Path | None]:
    """Return (disc, pcm_path, c2_path) from either --device (live read) or files."""
    if args.device:
        disc = disc_from_device(args.device)
        pcm_path = args.pcm or Path("private/testdata/c2/live.pcm")
        c2_path = args.c2 or Path("private/testdata/c2/live.c2")
        pcm_path.parent.mkdir(parents=True, exist_ok=True)
        read_disc(args.device, pcm_path, c2_path, args.speed)
        return disc, pcm_path, c2_path
    if not args.pcm or not args.toc:
        msg = "need --device, or --pcm with --toc"
        raise SystemExit(msg)
    return disc_from_toc(args.toc), args.pcm, args.c2


def _maybe_build_erasures(
    args: argparse.Namespace, c2_path: Path | None, nwords: int, pcm_path: Path
) -> Path | None:
    """Decide, per --c2-mode (+ the features gate on --device), whether to use C2
    erasures, and build the bitmap if so. Returns the bitmap path or None."""
    if args.c2_mode == "off" or not (c2_path and c2_path.exists()):
        return None
    if args.c2_mode == "auto" and args.device and not c2_features_ok(args.device):
        print(
            "c2: drive does not advertise/support C2 (features probe) — erasure boost off"
        )
        return None
    eras_path = pcm_path.with_suffix(".erasures.bin")
    eras_path.write_bytes(build_erasure_bitmap(c2_path, nwords, args.c2_align))
    print(f"erasures: built {eras_path} from {c2_path.name} (align {args.c2_align})")
    return eras_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--device", help="read the disc live (with C2) instead of files")
    ap.add_argument("--pcm", type=Path, help="whole-disc s16le PCM (raw or corrected)")
    ap.add_argument(
        "--c2", type=Path, help="matching c2read C2 capture (enables erasures)"
    )
    ap.add_argument("--toc", help="colon TOC L0:L1:…:LEADOUT (required with --pcm)")
    ap.add_argument(
        "--read-offset", type=int, default=0, help="drive read offset for the AR gate"
    )
    ap.add_argument(
        "--c2-align", type=int, default=-2, help="C2/audio offset in sample-pairs"
    )
    ap.add_argument(
        "--c2-mode",
        choices=("auto", "on", "off"),
        default="auto",
        help="C2 erasures: off=never, on=if --c2 present, auto=+features-gated on --device",
    )
    ap.add_argument("--speed", type=int, default=40, help="--device read speed")
    ap.add_argument(
        "--ctanalyse", default="ctanalyse", help="ctanalyse binary (on $PATH)"
    )
    ap.add_argument("--out", type=Path, default=None, help="default <pcm>.repaired.pcm")
    ap.add_argument("--xml", type=Path, default=None, help="cached CTDB lookup XML")
    ap.add_argument("--parity", type=Path, default=None, help="cached parity")
    ap.add_argument("--no-ar", action="store_true", help="skip the AccurateRip gate")
    args = ap.parse_args()

    disc, pcm_path, c2_path = _resolve_inputs(args)
    pcm = bytearray(pcm_path.read_bytes())
    disc_bytes = disc.leadout * _FRAME
    if len(pcm) != disc_bytes:
        print(f"pcm size {len(pcm)} != disc size {disc_bytes}", file=sys.stderr)
        return EXIT_ERR
    print(
        f"disc: {disc.n_tracks} tracks, lead-out {disc.leadout}, cddb {disc.cddb_id:08x}"
    )

    entries = load_entries(args.xml, disc)
    print(f"lookup: {len(entries)} usable entries")
    sel = select_entry(bytes(pcm), entries, disc)
    if sel is None:
        print("no CTDB entry reconciles with this rip — cannot repair")
        return EXIT_UNRECOVERABLE
    if not sel.damaged:
        print("all verifiable tracks already match CTDB — nothing to repair")
        return EXIT_OK

    parity = fetch_parity(sel.entry, args.parity or pcm_path.with_suffix(".parity.bin"))

    eras_path = _maybe_build_erasures(args, c2_path, len(pcm) // 2, pcm_path)

    result = run_ctanalyse(args.ctanalyse, pcm_path, parity, sel.entry, disc, eras_path)
    if not result.get("can_recover"):
        print(
            "ctanalyse: can_recover=false — damage exceeds RS capacity, keeping original"
        )
        return EXIT_UNRECOVERABLE
    corrections = result.get("corrections", [])
    print(
        f"ctanalyse: can_recover=true, {len(corrections)} corrections, "
        f"offset={result.get('offset')}, erasure_columns={result.get('erasure_columns', 0)}, "
        f"affected sectors={len(result.get('affected_sectors', []))}"
    )

    bad_idx = apply_corrections(pcm, corrections)
    if bad_idx is not None:
        c = corrections[bad_idx]
        print(
            f"SPLICE ABORTED: correction {bad_idx} at byte {c['byte']} — stored word "
            f"does not match claimed old {c['old']:#06x}; discarding ALL corrections"
        )
        return EXIT_BAD_OLD
    print(
        f"splice: {len(corrections)} corrections applied (old-byte checks all passed)"
    )

    print("verification gate 1/2 — CTDB per-track CRCs")
    ctdb_ok = verify_ctdb(bytes(pcm), sel, disc)
    ar_ok = True
    if args.no_ar:
        print("verification gate 2/2 — AccurateRip: SKIPPED (--no-ar)")
    else:
        print("verification gate 2/2 — AccurateRip (repaired tracks)")
        ar_ok = verify_ar(bytes(pcm), sel.damaged, disc, args.read_offset)
    if not (ctdb_ok and ar_ok):
        print("REPAIR DISCARDED: verification failed; original PCM left untouched")
        return EXIT_VERIFY_FAILED

    out = args.out or pcm_path.with_suffix(".repaired.pcm")
    out.write_bytes(pcm)
    print(f"repaired PCM written: {out}")
    print("prov (informational):")
    print(f"  repaired_via=ctdb:{sel.entry.id}@conf{sel.entry.confidence}")
    print(f"  repair_offset={sel.offset} erasures={'yes' if eras_path else 'no'}")
    for t in sel.damaged:
        print(f"  recovery_track_{t}=ctdb_repaired")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
