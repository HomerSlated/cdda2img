#!/usr/bin/env python3
"""Phase-0 pre-screen: can a Python sink keep up with the drive?

The API migration's one open question for ``read_disc_c2`` is whether routing
every sector through a Python callback costs anything real against the CLI's C
callback. AccuDisc are waiting on a wall-clock A/B/A before building anything
(their §bz.4), and this does **not** substitute for it — see "What this cannot
answer" below. It exists to de-risk that disc session: a cheap, device-free
screen that can return *obviously fine* or *obviously doomed*, and nothing in
between.

What it measures
----------------
The de-interleave-and-write loop, in isolation, fed synthetic chunks with the
real geometry. AccuDisc's engine (``src/read/engine.c:409``) sizes a chunk as
``min(65535 / sector_len, 32)`` sectors, so with C2 + raw sub the sink sees 23
sectors of 2742 bytes; the CLI's ``read_sink()`` and the binding's
``read_to_file()`` then split each sector into its 2352/294/96 parts. Both
transports do this per sector — the library never writes the file — so the delta
between them is a C callback against a Python callback, which is what makes it
bounded and measurable rather than architectural.

Three sink variants are timed, because the answer determines *where* a fix would
go if one is needed:

``per_sector``  what ``Device.read_to_file`` does today: one write per stream
                per sector, i.e. 3 x nsec writes per chunk.
``joined``      slice per sector but concatenate per stream, then one write per
                stream per chunk (3 writes). Same byte output, ~23x fewer write
                calls. If this is much faster, the fix lives in AccuDisc's
                binding and needs no new C entry point at all.
``bulk``        PCM-only, where the chunk is already contiguous audio and needs
                no de-interleave: one write per chunk. Not a candidate for the
                C2 path -- it is the floor, to show what the de-interleave costs.

The verdict is a headroom factor: sustained sink throughput in sectors/s over
the drive's demand at a given speed (1x = 75 sectors/s). Under ~3x headroom is
worth taking seriously, because this benchmark has no drive, no SCSI transport
and no competing load, all of which cost the real sink time it does not spend
here.

Read the ``/dev/null`` numbers, not the ``--out`` ones
------------------------------------------------------
``--out`` includes the filesystem write and an fsync, which sounds more honest
and is not: **that cost is common-mode.** The CLI's ``fwrite()`` puts the same
bytes in the same file on the same filesystem, so it cancels in the A/B and is
not part of the delta between transports. Worse, it swamps what is. The first
run of this tool made that visible by producing an impossible ordering —
pcm+c2+sub measured *faster* than pcm-only, which cannot be true of the sink,
since it is strictly more work per sector. Under ``--out`` the variant column is
measuring page-cache state and run order.

So ``--out`` is useful for one thing only: an absolute upper bound on total sink
time. For comparing variants, or for the migration question, use the default.

What this cannot answer
-----------------------
Whether the sink stalls the streaming read at 32-40x and drains the drive
cache. That failure is nonlinear and this harness cannot see it: it measures
the sink against memory, with no drive to fall behind. A comfortable headroom
number here means "the disc session is worth running", not "the question is
settled". Only the A/B/A answers it.

Usage
-----
    uv run python tools/sink_prescreen.py                 # /dev/null, CPU only
    uv run python tools/sink_prescreen.py --out /var/tmp  # include filesystem
    uv run python tools/sink_prescreen.py --sectors 360000 --out /var/tmp
"""

from __future__ import annotations

import argparse
import os
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

# AccuDisc's per-sector component sizes (include/accudisc/accudisc.h:43-47).
BYTES_AUDIO = 2352
BYTES_C2 = 294
BYTES_SUB_RAW = 96

# src/read/engine.c:26,31 — a chunk is min(65535 / sector_len, 32) sectors.
MAX_XFER = 65535
CHUNK_MAX = 32

SECTORS_PER_SECOND_1X = 75
FULL_DISC_SECTORS = 80 * 60 * SECTORS_PER_SECOND_1X  # 360_000, an 80-min disc


@dataclass(frozen=True)
class Layout:
    """One read's sector layout and the chunk size the engine will pick for it."""

    name: str
    audio_len: int
    c2_len: int
    sub_len: int

    @property
    def sector_len(self) -> int:
        return self.audio_len + self.c2_len + self.sub_len

    @property
    def chunk_sectors(self) -> int:
        return min(MAX_XFER // self.sector_len, CHUNK_MAX)


# The rip path asks for all three; the bench tools in tools/ ask for pcm+c2.
LAYOUT_FULL = Layout("pcm+c2+sub", BYTES_AUDIO, BYTES_C2, BYTES_SUB_RAW)
LAYOUT_C2 = Layout("pcm+c2", BYTES_AUDIO, BYTES_C2, 0)
LAYOUT_PCM = Layout("pcm only", BYTES_AUDIO, 0, 0)


def _sink_per_sector(
    chunk: memoryview, lay: Layout, files: dict[str, BinaryIO]
) -> None:
    """Verbatim ``Device.read_to_file``: one write per stream per sector."""
    nsec = len(chunk) // lay.sector_len
    for i in range(nsec):
        base = i * lay.sector_len
        if "pcm" in files:
            files["pcm"].write(chunk[base : base + lay.audio_len])
        if "c2" in files and lay.c2_len:
            off = base + lay.audio_len
            files["c2"].write(chunk[off : off + lay.c2_len])
        if "sub" in files and lay.sub_len:
            off = base + lay.audio_len + lay.c2_len
            files["sub"].write(chunk[off : off + lay.sub_len])


def _sink_joined(chunk: memoryview, lay: Layout, files: dict[str, BinaryIO]) -> None:
    """Same bytes, one write per stream per chunk instead of per sector."""
    nsec = len(chunk) // lay.sector_len
    sl = lay.sector_len
    if "pcm" in files:
        a = lay.audio_len
        files["pcm"].write(b"".join([chunk[i * sl : i * sl + a] for i in range(nsec)]))
    if "c2" in files and lay.c2_len:
        o, n = lay.audio_len, lay.c2_len
        files["c2"].write(
            b"".join([chunk[i * sl + o : i * sl + o + n] for i in range(nsec)])
        )
    if "sub" in files and lay.sub_len:
        o, n = lay.audio_len + lay.c2_len, lay.sub_len
        files["sub"].write(
            b"".join([chunk[i * sl + o : i * sl + o + n] for i in range(nsec)])
        )


def _sink_bulk(chunk: memoryview, lay: Layout, files: dict[str, BinaryIO]) -> None:
    """No de-interleave: the chunk is already contiguous audio. The floor."""
    files["pcm"].write(chunk)


SINKS = {
    "per_sector": _sink_per_sector,
    "joined": _sink_joined,
    "bulk": _sink_bulk,
}


def _stream_names(lay: Layout, variant: str) -> list[str]:
    """Which output streams this layout/variant writes. ``bulk`` is PCM alone."""
    if variant == "bulk":
        return ["pcm"]
    names = ["pcm"]
    if lay.c2_len:
        names.append("c2")
    if lay.sub_len:
        names.append("sub")
    return names


def _open_streams(
    stack: ExitStack, names: list[str], outdir: Path | None
) -> tuple[dict[str, BinaryIO], list[Path]]:
    """Open one file per stream on *stack*; return the handles and any real paths."""
    files: dict[str, BinaryIO] = {}
    paths: list[Path] = []
    for n in names:
        if outdir is None:
            p = Path(os.devnull)
        else:
            p = outdir / f"sink_prescreen_{n}.tmp"
            paths.append(p)
            stack.callback(p.unlink, missing_ok=True)
        files[n] = stack.enter_context(p.open("wb"))
    return files, paths


def run(lay: Layout, variant: str, sectors: int, outdir: Path | None) -> float:
    """Time *variant* over *sectors* sectors. Returns sustained sectors/s."""
    nsec = lay.chunk_sectors
    # One buffer reused for every chunk, viewed rather than copied — the sink
    # receives a memoryview over library memory on the real path (copy=False),
    # so slicing cost is representative and allocation cost is not imported.
    buf = memoryview(bytes(os.urandom(nsec * lay.sector_len)))
    sink = SINKS[variant]
    chunks, done = divmod(sectors, nsec)

    with ExitStack() as stack:
        files, _ = _open_streams(stack, _stream_names(lay, variant), outdir)

        t0 = time.perf_counter()
        for _ in range(chunks):
            sink(buf, lay, files)
        if done:
            sink(buf[: done * lay.sector_len], lay, files)
        # The flush is inside the timed region deliberately: a sink that buffered
        # its way to a good number and paid for it at close would otherwise
        # report the buffering, not the work.
        for fh in files.values():
            fh.flush()
            if outdir is not None:
                os.fsync(fh.fileno())
        elapsed = time.perf_counter() - t0

    return sectors / elapsed if elapsed > 0 else float("inf")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--sectors",
        type=int,
        default=FULL_DISC_SECTORS // 6,
        help="sectors to push per variant (default 60000, 1/6 of an 80-min disc)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="directory for real output files; omit for /dev/null (CPU only). "
        "Use /var/tmp, not /tmp — /tmp is a RAM-backed tmpfs here.",
    )
    ap.add_argument(
        "--speeds",
        type=int,
        nargs="+",
        default=[8, 16, 24, 32, 40, 48],
        help="drive speeds to report headroom against",
    )
    args = ap.parse_args()

    dest = "/dev/null (CPU only)" if args.out is None else f"{args.out} (with fsync)"
    print(f"# sink pre-screen — {args.sectors} sectors per variant → {dest}")
    print(f"# a whole 80-min disc is {FULL_DISC_SECTORS} sectors")
    if args.out is not None:
        print(
            "# WARNING: --out includes the filesystem write, which is COMMON-MODE —\n"
            "# the CLI writes the same bytes to the same file, so it cancels in the\n"
            "# A/B and is not the transport delta. It also swamps it: expect the\n"
            "# variant ordering below to be unreliable (page-cache state and run\n"
            "# order dominate). Use these as an absolute upper bound only."
        )
    print()

    results: dict[tuple[str, str], float] = {}
    for lay in (LAYOUT_FULL, LAYOUT_C2, LAYOUT_PCM):
        variants = ["per_sector", "joined"] + (
            ["bulk"] if lay.sector_len == BYTES_AUDIO else []
        )
        print(
            f"{lay.name:12s}  sector_len={lay.sector_len:5d}  chunk={lay.chunk_sectors} sectors"
        )
        for v in variants:
            rate = run(lay, v, args.sectors, args.out)
            results[lay.name, v] = rate
            disc_s = FULL_DISC_SECTORS / rate
            print(
                f"    {v:11s} {rate:12,.0f} sectors/s   "
                f"whole disc: {disc_s:6.2f} s of sink time"
            )
        print()

    print("# headroom = sink throughput / drive demand (1x = 75 sectors/s)")
    print("# the rip path is pcm+c2+sub; under ~3x deserves attention")
    hdr = "  ".join(f"{s:>3d}x" for s in args.speeds)
    print(f"\n{'variant':24s}  {hdr}")
    for (lname, v), rate in results.items():
        if lname != LAYOUT_FULL.name:
            continue
        cells = "  ".join(
            f"{rate / (s * SECTORS_PER_SECOND_1X):>3.0f}x" for s in args.speeds
        )
        print(f"{lname + ' / ' + v:24s}  {cells}")

    print(
        "\n# NB: this cannot see the failure that matters — a Python sink falling\n"
        "# behind the drive and draining its cache at 32-40x. It has no drive to\n"
        "# fall behind. A good number here means the A/B/A is worth running."
    )


if __name__ == "__main__":
    main()
