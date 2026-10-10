#!/usr/bin/env python3
"""Capture a sector span and locate a sample displacement against verified audio.

Written 2026-10-10. A whole-disc pass at 40x returned tracks 6 to 11 of one disc
bit-intact but displaced by 12 samples: every AccurateRip sum failed, CTDB saw
damage beyond its capacity, and the audio itself was right. C2 cannot see this,
because no byte is wrong; it is in the wrong place.

Two subcommands:

``capture``  one ``Device.read`` of ``[start, start + count)`` with audio, C2
             pointers and raw P-W subchannel (the whole-disc pass's stream shape),
             or audio only with ``--plain`` (the recovery ladder's shape). The
             three streams are written to files. The drive speed is put back and
             the spindle parked afterwards.
``locate``   compares the captured audio with the PCM block of a verified RBI of
             the same disc and reports, per run of sectors, the displacement in
             samples. For each change it prints the C2 count and the decoded Q
             position of the sectors around it.

The reference is the *stored* PCM, which is the raw stream with the drive's read
offset applied, so an undisturbed capture sits at ``-read_offset`` (-30 on a
PX-716A) and a displaced one differs from that.

    uv run python tools/slip_probe.py capture --start 57535 --count 15160 \\
        --speed 40 --out /var/tmp/slip_probe
    uv run python tools/slip_probe.py locate --dir /var/tmp/slip_probe \\
        --ref "rips/cdda2img/Tracy Chapman (second copy).rbi"

Scratch scope rule: ``--out`` goes on real disk (never /tmp) and is deleted before
the session that made it ends.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cdda2img import accudisc_reader as seam
from cdda2img import rbi_format as rf
from cdda2img.container import read_header
from cdda2img.subchannel import decode_q

_SECTOR = 2352
_SAMPLES = 588
_C2 = 294
_SUB = 96
_SEARCH = 2000  # samples either side when a sector stops matching


def _capture(args: argparse.Namespace) -> int:
    out: Path = args.out
    if str(out.resolve()).startswith("/tmp"):  # noqa: S108
        print("refusing to write a capture under /tmp (RAM-backed)")
        return 2
    geom = seam.read_toc(args.device)
    leadout = geom.disc_last_lsn + 1
    print(f"toc: lead-out {leadout}")
    if args.expect_leadout is not None and leadout != args.expect_leadout:
        print(f"wrong disc: expected lead-out {args.expect_leadout}; nothing read")
        return 3
    out.mkdir(parents=True, exist_ok=True)
    before = seam.read_speed(args.device)
    module = seam._binding("span capture")
    names = ("pcm",) if args.plain else ("pcm", "c2", "sub")
    files = {n: (out / f"span.{n}").open("wb") for n in names}
    chunks: list[tuple[int, int]] = []
    per_second: dict[int, int] = {}
    t0 = time.monotonic()

    def sink(chunk: object) -> None:
        nsec = chunk.nsec  # type: ignore[attr-defined]
        chunks.append((nsec, chunk.sector_len))  # type: ignore[attr-defined]
        second = int(time.monotonic() - t0)
        per_second[second] = per_second.get(second, 0) + nsec
        seam._split_streams(chunk, files)

    extra = {} if args.plain else {"c2": module.C2.PTRS, "sub": module.Sub.RAW}
    try:
        with module.Device(args.device) as dev:
            result = dev.read(
                args.start,
                args.count,
                sink=sink,
                copy=False,
                speed_x=args.speed,
                **extra,
            )
    finally:
        for f in files.values():
            f.close()
        elapsed = time.monotonic() - t0
        if not args.keep_spinning:
            if before[0]:
                seam.set_speed(args.device, max(1, round(before[0] / 176.4)))
            seam.park_spindle(args.device)
    stats = result.stats
    meta = {
        "start": args.start,
        "count": args.count,
        "speed_asked": args.speed,
        "shape": "plain" if args.plain else "pcm+c2+sub_raw",
        "elapsed_s": round(elapsed, 2),
        "sectors_delivered": sum(n for n, _ in chunks),
        "chunks": len(chunks),
        "chunk_sectors": sorted({n for n, _ in chunks}),
        "sector_len": sorted({s for _, s in chunks}),
        "speed_before_kbps": before,
        # The setting is not the rate: a span read from a parked spindle spends
        # its first seconds spinning up. Sectors per wall second, as Nx.
        "rate_x_by_second": {
            k: round(v / 75, 1) for k, v in sorted(per_second.items())
        },
        "stats": {
            k: getattr(stats, k)
            for k in dir(stats)
            if not k.startswith("_")
            and isinstance(getattr(stats, k), (int, float, bool, str))
        },
        "engine": seam.engine_version(),
    }
    (out / "span.json").write_text(json.dumps(meta, indent=2, default=str))
    print(json.dumps(meta, indent=2, default=str))
    return 0


def _reference(ref: Path) -> np.ndarray:
    entry = read_header(ref).find_block(rf.BLOCK_TYPE_PCM)
    if entry is None:
        msg = f"{ref} has no PCM block"
        raise SystemExit(msg)
    return np.memmap(
        ref, dtype="<u4", mode="r", offset=entry.offset, shape=(entry.length // 4,)
    )


def _shift_of(cap: np.ndarray, ref: np.ndarray, pos: int, hint: int) -> int | None:
    """Displacement d with ``cap == ref[pos + d : pos + d + 588]``, or None."""
    order = [hint, *(hint + s * k for k in range(1, _SEARCH + 1) for s in (1, -1))]
    for d in order:
        lo = pos + d
        if lo < 0 or lo + _SAMPLES > len(ref):
            continue
        if np.array_equal(cap, ref[lo : lo + _SAMPLES]):
            return d
    return None


def _q_text(sub: bytes | None, i: int) -> str:
    if sub is None:
        return "-"
    q = decode_q(sub[i * _SUB : (i + 1) * _SUB])
    if not q.valid:
        return "q:bad-crc"
    lba = q.position_lba()
    if lba is None:
        return f"q:adr{q.adr}"
    return f"q:trk{q.track_number} idx{q.index} lba{lba}"


def _locate(args: argparse.Namespace) -> int:  # noqa: C901
    d: Path = args.dir
    meta = json.loads((d / "span.json").read_text())
    start, count = meta["start"], meta["count"]
    cap = np.fromfile(d / "span.pcm", dtype="<u4")
    ref = _reference(args.ref)
    c2_path, sub_path = d / "span.c2", d / "span.sub"
    c2 = np.fromfile(c2_path, dtype=np.uint8) if c2_path.exists() else None
    sub = sub_path.read_bytes() if sub_path.exists() else None
    if len(cap) != count * _SAMPLES:
        print(f"capture holds {len(cap)} samples, expected {count * _SAMPLES}")
        return 2

    shifts: list[int | None] = []
    hint = -args.read_offset
    for i in range(count):
        sector = cap[i * _SAMPLES : (i + 1) * _SAMPLES]
        if not sector.any():
            # Silence matches at any shift, so it takes its neighbour's.
            shifts.append(shifts[-1] if shifts else hint)
            continue
        found = _shift_of(sector, ref, (start + i) * _SAMPLES, hint)
        shifts.append(found)
        if found is not None:
            hint = found

    def c2_bits(i: int) -> int:
        if c2 is None:
            return -1
        return int(np.unpackbits(c2[i * _C2 : (i + 1) * _C2]).sum())

    print(f"span lba {start}..{start + count - 1}, expected shift {-args.read_offset}")
    run_start = 0
    changes: list[int] = []
    for i in range(1, count + 1):
        if i == count or shifts[i] != shifts[run_start]:
            s = shifts[run_start]
            label = "no match" if s is None else f"shift {s:+d}"
            print(f"  lba {start + run_start:>6}..{start + i - 1:>6}  {label}")
            if i < count:
                changes.append(i)
            run_start = i
    if c2 is not None:
        flagged = [i for i in range(count) if c2_bits(i)]
        print(f"c2: {len(flagged)} sector(s) flagged in the span")
    for i in changes[: args.max_changes]:
        print(f"change at lba {start + i}:")
        for j in range(max(0, i - 4), min(count, i + 5)):
            s = shifts[j]
            label = "none" if s is None else f"{s:+d}"
            print(
                f"    lba {start + j}  shift {label:>6}  c2 bits {c2_bits(j):>4}"
                f"  {_q_text(sub, j)}"
            )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sp = ap.add_subparsers(dest="cmd", required=True)
    c = sp.add_parser("capture")
    c.add_argument("--device", default="/dev/sr0")
    c.add_argument("--start", type=int, required=True)
    c.add_argument("--count", type=int, required=True)
    c.add_argument("--speed", type=int, default=40)
    c.add_argument("--plain", action="store_true", help="audio only, no C2 or sub")
    c.add_argument("--expect-leadout", type=int)
    c.add_argument(
        "--keep-spinning",
        action="store_true",
        help="leave speed and spindle alone, for a capture that another follows",
    )
    c.add_argument("--out", type=Path, required=True)
    c.set_defaults(fn=_capture)
    lo = sp.add_parser("locate")
    lo.add_argument("--dir", type=Path, required=True)
    lo.add_argument("--ref", type=Path, required=True)
    lo.add_argument("--read-offset", type=int, default=30)
    lo.add_argument("--max-changes", type=int, default=12)
    lo.set_defaults(fn=_locate)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
