#!/usr/bin/env python3
"""Settling curve: how a freshly-clamped disc's read errors decay under sustained spin.

Why this exists (2026-07-23, Tracy Chapman / PX-716A). Over a session the disc went
from 1 C2-flagged sector and 10/11 tracks AccurateRip-verified to 0 flagged and 11/11
— it "healed" while nothing about the read parameters changed. Ejecting and reloading
it, with the drive still warm, brought the defect straight back. That rules out
thermal warm-up and points at **mechanical settling**: a freshly-clamped disc sits
slightly eccentric and progressively centres itself under rotation (this drive has a
known-worn spindle o-ring, and thin discs slip on it).

The consequence for recovery is not small. If errors decay with spin time then:
  * ejecting or resetting mid-recovery is actively HARMFUL — it discards the settling
    already accumulated and returns the disc to its worst state;
  * a pre-rip spin-up is a cheaper fix than any run-up or speed tuning;
  * repeated-read recovery partly works because the disc is settling underneath it,
    not because the retries are independent samples.

This measures the decay directly: N back-to-back whole-disc C2 captures, recording per
pass the flagged-sector count, which LBAs, and how many tracks AR-verify. Run it on a
FRESHLY LOADED disc — the curve is meaningless if the disc has already settled.

Usage (from project root):
    TMPDIR=/var/tmp uv run python tools/settling_curve.py \
        --device /dev/sr0 --read-offset 30 --passes 8 --speed 32 \
        --out private/bench/runs/run3/tracy_chapman/settling.toml
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field
from pathlib import Path

_SECTOR_BYTES = 2352
_C2_BYTES = 294  # 2352 samples / 8 bits


@dataclass
class Pass:
    index: int
    elapsed_s: float  # since the first pass STARTED — the settling clock
    read_s: float
    flagged: list[int] = field(default_factory=list)
    ar_ok: list[int] = field(default_factory=list)
    ar_fail: list[int] = field(default_factory=list)


def _verify_track(
    pcm: bytes,
    track: int,
    track_lsns: list[int],
    leadout: int,
    read_offset: int,
    responses: list[list[dict]],
) -> bool:
    """One track's AR check. Mirrors accuraterip.verify_rip's window arithmetic —
    clamp AND zero-pad. Without the pad a positive read offset silently shortens
    track 1 and the last track, which shifts _ar_checksums' exclusion boundary and
    guarantees a spurious miss."""
    from cdda2img.accuraterip import match_track_pcm

    idx = track - 1
    byte_start = track_lsns[idx] * _SECTOR_BYTES + read_offset * 4
    end_lsn = track_lsns[idx + 1] if idx + 1 < len(track_lsns) else leadout
    byte_end = end_lsn * _SECTOR_BYTES + read_offset * 4

    raw = pcm[max(0, byte_start) : min(len(pcm), byte_end)]
    if byte_start < 0:
        raw = bytes(-byte_start) + raw
    if byte_end > len(pcm):
        raw = raw + bytes(byte_end - len(pcm))

    _v1, _v2, c1, c2 = match_track_pcm(raw, track, len(track_lsns), responses)
    return c1 is not None or c2 is not None


def run_curve(
    device: str,
    track_lsns: list[int],
    leadout: int,
    read_offset: int,
    responses: list[list[dict]],
    passes: int,
    speed: int,
    tmp_dir: Path,
) -> list[Pass]:
    from cdda2img.accudisc_reader import read_disc_c2

    pcm_p, c2_p = tmp_dir / ".settle.pcm", tmp_dir / ".settle.c2"
    results: list[Pass] = []
    t_start = time.monotonic()

    for i in range(1, passes + 1):
        t0 = time.monotonic()
        read_disc_c2(device, pcm_p, c2_p, read_speed=speed)
        read_s = time.monotonic() - t0
        pcm, c2 = pcm_p.read_bytes(), c2_p.read_bytes()

        flagged = [
            s
            for s in range(len(c2) // _C2_BYTES)
            if any(c2[s * _C2_BYTES : (s + 1) * _C2_BYTES])
        ]
        ok, fail = [], []
        for t in range(1, len(track_lsns) + 1):
            good = not responses or _verify_track(
                pcm, t, track_lsns, leadout, read_offset, responses
            )
            (ok if good else fail).append(t)

        p = Pass(i, t0 - t_start, read_s, flagged, ok, fail)
        results.append(p)
        print(
            f"[pass {i}/{passes}] t+{p.elapsed_s:6.0f}s  read {read_s:5.0f}s  "
            f"c2_flagged={len(flagged):<4} ar_fail={fail or '-'}  "
            f"{flagged[:8]}{' ...' if len(flagged) > 8 else ''}",
            flush=True,
        )
        del pcm

    pcm_p.unlink(missing_ok=True)
    c2_p.unlink(missing_ok=True)
    return results


def summarise(results: list[Pass], n_tracks: int) -> str:
    lines = ["", "=== settling curve ===", "pass   t+s   read_s  c2_flagged  ar_pass"]
    for p in results:
        lines.append(
            f"{p.index:>4} {p.elapsed_s:>6.0f} {p.read_s:>8.0f} "
            f"{len(p.flagged):>11} {len(p.ar_ok):>6}/{n_tracks}"
        )
    first, last = results[0], results[-1]
    lines += [
        "",
        f"first pass: {len(first.flagged)} flagged, {len(first.ar_ok)}/{n_tracks} AR",
        f"last  pass: {len(last.flagged)} flagged, {len(last.ar_ok)}/{n_tracks} AR",
    ]
    clean = next(
        (p for p in results if not p.flagged and len(p.ar_ok) == n_tracks), None
    )
    if clean:
        lines.append(
            f"SETTLED at pass {clean.index} (t+{clean.elapsed_s:.0f}s from first read)"
        )
    else:
        lines.append("NOT settled within this run — extend --passes")
    # Union of every LBA ever flagged: the eccentricity zone, not a fixed defect.
    zone = sorted({lba for p in results for lba in p.flagged})
    if zone:
        lines.append(f"flagged zone: {zone[0]}..{zone[-1]} ({len(zone)} distinct LBAs)")
    return "\n".join(lines)


def write_toml(path: Path, results: list[Pass], meta: dict) -> None:
    lines = ["# settling curve", ""]
    lines += [f"{k} = {v!r}" for k, v in meta.items()]
    lines.append("")
    for p in results:
        lines += [
            "[[pass]]",
            f"index = {p.index}",
            f"elapsed_s = {p.elapsed_s:.2f}",
            f"read_s = {p.read_s:.2f}",
            f"c2_flagged_count = {len(p.flagged)}",
            f"c2_flagged = {p.flagged}",
            f"ar_pass = {p.ar_ok}",
            f"ar_fail = {p.ar_fail}",
            "",
        ]
    path.write_text("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--device", default="/dev/sr0")
    ap.add_argument("--read-offset", type=int, default=30)
    ap.add_argument("--passes", type=int, default=8)
    ap.add_argument("--speed", type=int, default=32)
    ap.add_argument("--out", default="private/bench/runs/run3/settling.toml")
    args = ap.parse_args(argv)

    from cdda2img.accudisc_reader import park_spindle, read_toc
    from cdda2img.accuraterip import fetch_ar_responses
    from cdda2img.cddb import compute_cddb_disc_id

    geom = read_toc(args.device)
    track_lsns = geom.track_lsns
    # compute_cddb_disc_id and _ar_disc_ids BOTH take disc_last_lsn and add the +1
    # themselves — passing the lead-out here doubles it and 404s on a URL for a disc
    # that does not exist (indistinguishable from "not in database").
    leadout = geom.disc_last_lsn + 1
    cddb_hex = compute_cddb_disc_id(track_lsns, geom.disc_last_lsn)
    responses, _t, _b = fetch_ar_responses(
        track_lsns, geom.disc_last_lsn, int(cddb_hex, 16)
    )
    print(
        f"# disc {cddb_hex}, {len(track_lsns)} tracks, {len(responses)} AR block(s)"
        f"{' — AR unavailable, C2 only' if not responses else ''}",
        flush=True,
    )

    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        results = run_curve(
            args.device,
            track_lsns,
            leadout,
            args.read_offset,
            responses,
            args.passes,
            args.speed,
            out.parent,
        )
    finally:
        park_spindle(args.device)

    write_toml(
        out,
        results,
        {
            "disc": cddb_hex,
            "speed": args.speed,
            "passes": args.passes,
            "read_offset": args.read_offset,
            "tracks": len(track_lsns),
        },
    )
    print(summarise(results, len(track_lsns)))
    print(f"\n# wrote {len(results)} passes → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
