#!/usr/bin/env python3
"""Measure PlexTools ``.pxi`` images: audio origin, tail shortfall, and — with
``--ar`` — the sample offset the stored audio actually needs.

This is the instrument for TODO N7. It exists because the first ``.pxi`` we
reverse-engineered appeared to stop 120 bytes short of a whole final sector, and
kgr's read of that ("not a bug, it has a purpose") was right: the bytes are not
missing from the tail, they are present at the head, and the assumed origin was
120 bytes too high.

Three measurements, in increasing cost:

**Origin.** ``origin = size - total_frames * 2352`` is one equation in one
unknown, so each file determines its own origin exactly — not merely modulo the
sector size. Agreement across images is then a real cross-check rather than an
identity. Reported per file so a disagreement is visible instead of averaged.

**Head content.** Where the header's zero fill ends. On a disc whose LBA 0 is
true digital silence this says nothing (zeros either way), which is exactly why
one image could not settle the question; a disc with a non-zero noise floor at
the very start puts a visible boundary at the origin. Read as corroboration
only: a run of ``0xFF`` decodes as -1 at every alignment, so it carries no
framing information of its own.

**AccurateRip offset** (``--ar``, network). The discriminator for the question
byte arithmetic cannot answer: does the file hold *raw* audio, or audio the
ripper already offset-corrected? Both readings predict identical bytes, so no
amount of staring at one image resolves it. AccurateRip is offset-sensitive by
construction, so feeding it the audio *from the measured origin* and asking which
shift verifies gives a direct answer:

    winner == +30  ->  raw, written by a drive with a +30 read offset (PX-716A)
    winner ==   0  ->  already corrected by the ripper

Note the answer is a ranked list, not a scalar: a widely-pressed disc verifies at
several offsets at once (accuraterip.detect_offset's docstring, and Tracy Chapman
at 0/-669/-1333/-1997). Those cohorts sit hundreds of samples apart, so they do
not blur the 0-vs-30 distinction this tool is asking about — but read the list,
not just its head.

Usage (from the project root):

    uv run python tools/pxi_probe.py IMAGE.pxi [MORE.pxi ...] [--ar]
    uv run python tools/pxi_probe.py /path/to/dir --ar

Read-only: it never writes a container, never materialises the audio, and
memory-maps the image rather than copying it (a disc is ~400-800 MB and the
scratch-scope rule in CLAUDE.md forbids that landing under /tmp).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cdda2img import pxi_reader as pxi

_SECTOR = 2352
_HEAD_SCAN_FROM = 0x8600  # past the index table, into the zero gap


def _zero_gap_end(path: Path, upto: int) -> int | None:
    """First non-zero byte of the trailing run before *upto*, or None if all zero.

    Scans the gap between the index table and the assumed audio start. The
    interesting quantity is where the fill *stops*, so the run is walked back to
    its start rather than reporting the last zero.
    """
    with open(path, "rb") as fh:
        fh.seek(_HEAD_SCAN_FROM)
        blob = fh.read(upto - _HEAD_SCAN_FROM)
    last = max((i for i, b in enumerate(blob) if b), default=None)
    if last is None:
        return None
    i = last
    while i >= 0 and blob[i]:
        i -= 1
    return _HEAD_SCAN_FROM + i + 1


def _ar_offsets(path: Path, disc, origin: int, total_frames: int) -> str:
    """Ranked AccurateRip offset candidates for the audio at *origin*."""
    import numpy as np

    from cdda2img.accuraterip import detect_offset, fetch_ar_responses
    from cdda2img.cddb import compute_cddb_disc_id

    lsns = [t.start_frame + t.pregap_frames for t in disc.tracks]
    last = disc.tracks[-1]
    disc_last = last.start_frame + last.pregap_frames + last.duration_frames - 1
    cddb_id = int(compute_cddb_disc_id(lsns, disc_last), 16)

    # Memory-mapped at the measured origin: detect_offset's crc450 prefilter
    # touches a few kB per track, so the image is never read in full.
    frames = np.memmap(path, dtype="<u4", mode="r", offset=origin)
    frames = frames[: total_frames * (_SECTOR // 4)]

    responses, _transport, _b3 = fetch_ar_responses(lsns, disc_last, cddb_id)
    if not responses:
        return "disc not in AccurateRip (no evidence either way)"
    matches = detect_offset(frames, lsns, disc_last, responses)
    if not matches:
        return "in AccurateRip, verifies at NO offset in the swept radius"
    return "  ".join(
        f"{m.offset:+d}({m.tracks_matched}/{m.total_tracks}"
        f"{'' if m.confirmed else ' UNCONFIRMED'})"
        for m in matches[:4]
    )


def probe(path: Path, *, do_ar: bool) -> None:
    size = path.stat().st_size
    disc, has_cdtext, total_frames = pxi._parse_pxi(path)
    want = total_frames * _SECTOR
    origin = size - want

    print(f"{path.name}")
    print(
        f"   tracks={len(disc.tracks)}  lead-out={total_frames}  "
        f"CD-Text={'yes' if has_cdtext else 'no'}  size={size}"
    )
    print(
        f"   measured origin = size - lead-out*2352 = {origin} (0x{origin:X})"
        f"   [reader uses 0x{pxi._AUDIO_OFFSET:X}"
        f", delta {origin - pxi._AUDIO_OFFSET:+d}]"
    )
    whole = (size - origin) % _SECTOR == 0
    print(f"   whole sectors from measured origin: {whole}")

    gap = _zero_gap_end(path, pxi._AUDIO_OFFSET)
    if gap is None:
        print("   header zero fill runs to the audio start (LBA 0 is silent here)")
    else:
        print(
            f"   header zero fill ends at 0x{gap:X}"
            f" ({'MATCHES measured origin' if gap == origin else 'differs from origin'})"
        )

    if do_ar:
        try:
            print(
                f"   AccurateRip offsets from origin: {_ar_offsets(path, disc, origin, total_frames)}"
            )
        except Exception as exc:
            print(f"   AccurateRip probe failed: {type(exc).__name__}: {exc}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("paths", nargs="+", type=Path, help=".pxi files or a directory")
    ap.add_argument(
        "--ar",
        action="store_true",
        help="query AccurateRip for the offset the stored audio needs (network)",
    )
    args = ap.parse_args()

    files: list[Path] = []
    for p in args.paths:
        files.extend(sorted(p.glob("*.pxi")) if p.is_dir() else [p])
    if not files:
        print("no .pxi files found", file=sys.stderr)
        return 1

    for f in files:
        try:
            probe(f, do_ar=args.ar)
        except Exception as exc:
            print(f"{f.name}: FAILED {type(exc).__name__}: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
