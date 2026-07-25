#!/usr/bin/env python3
"""Prove the C2 erasure-bitmap origin shift on a disc whose image does not start at LBA 0.

The claim under test
--------------------
``ctdb_repair.build_erasure_bitmap`` builds one bit per word over **our** PCM domain,
``[0, lead-out)``. ``ctanalyse`` consumes it over **CTDB's** image domain,
``[bounds[0], bounds[-1])``, by skipping the first ``word_base = bounds[0] * 1176``
bits (``word_base / 8`` bytes) before mapping bits to grid cells. Those two domains
differ by a whole program-area pre-gap on any disc with track 1 INDEX 01 > 0.

Until now that shift was only ever checked by arithmetic. Every CTDB fixture and every
disc we had repaired had ``bounds[0] == 0``, where the shift is a no-op — so the code
path had never once been *executed* with a non-zero origin.

Why this test is decisive
-------------------------
A repair that merely succeeds proves nothing: with damage inside error-only capacity
the decoder recovers whether or not the erasures were any good (false-positive erasures
cost a slot, they do not corrupt). So the damage here is placed **between** the two
capacities. With ``npar = 8`` a column corrects ``e + 2t <= npar``:

    error-only   ->  t <= 4    K errors in one column, K > 4  => MUST fail
    erasures     ->  e <= 8    the same K, if flagged         => MUST succeed

Damaging K = 6 words in a single column therefore splits the two paths. Erasure-assisted
decoding can only win if the erasure positions land on the actual damage — which is
exactly the origin arithmetic. Three runs:

    correct bitmap   -> repaired, used_c2=True   (origin right)
    no bitmap        -> refused                  (proves K exceeds error-only capacity)
    bitmap shifted   -> refused                  (proves the test can SEE a wrong origin)

The third run is the falsifiability check. Without it a pass could just mean ctanalyse
ignores the bitmap. It shifts the flags by ``word_base`` words — precisely the error a
broken origin would make — and must fail.

Usage
-----
    TMPDIR=/var/tmp uv run python tools/ctdb_erasure_origin_test.py \
        --rbi "Gold: Greatest Hits.rbi" --work /var/tmp/ctdb-origin
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cdda2img import container
from cdda2img import ctdb_repair as C
from cdda2img.rbi_format import BLOCK_TYPE_PCM, BLOCK_TYPE_TOC
from cdda2img.toc_parser import parse_toc

_FRAME = 2352
_SPP = 588
_WPF = 1176  # 16-bit words per frame


def _extract(rbi: Path, work: Path) -> tuple[Path, list[int], int]:
    """Pull the PCM block out of *rbi* and derive the CTDB bounds from its TOC."""
    hdr = container.read_header(rbi)
    pcm_e = hdr.find_block(BLOCK_TYPE_PCM)
    toc_e = hdr.find_block(BLOCK_TYPE_TOC)
    if pcm_e is None or toc_e is None:
        msg = "RBI lacks a PCM or TOC block"
        raise SystemExit(msg)

    pcm_path = work / "disc.pcm"
    with open(rbi, "rb") as f, open(pcm_path, "wb") as out:
        f.seek(toc_e.offset)
        toc_bytes = f.read(toc_e.length)
        f.seek(pcm_e.offset)
        remaining = pcm_e.length
        while remaining:
            chunk = f.read(min(1 << 24, remaining))
            if not chunk:
                break
            out.write(chunk)
            remaining -= len(chunk)

    disc = parse_toc(toc_bytes)
    # audio_start_frame is INDEX 01 — the same boundary CTDB's bounds[] use, and the
    # same one the drive reports in its full TOC. Deriving it any other way is how the
    # track-1 pre-gap got dropped in the first place.
    lsns = [t.audio_start_frame for t in disc.tracks]
    last = disc.tracks[-1].audio_start_frame + disc.tracks[-1].duration_frames - 1
    return pcm_path, lsns, last


def _synth_c2(nsec: int, words: list[int], out: Path, align_pairs: int = -2) -> None:
    """Write a 294 B/sector C2 capture flagging exactly *words* (global PCM words).

    ``build_erasure_bitmap`` undoes the drive's C2/audio lag, so we must apply it here:
    with ``align_pairs=-2`` it reads ``er[i] = pair[i + 2]``, so flagging audio pair *p*
    means setting C2 pair ``p + 2``. Getting this backwards would make the test fail for
    a reason that has nothing to do with the origin, so it is asserted below.
    """
    raw = np.zeros((nsec, 294), dtype=np.uint8)
    lag = -align_pairs
    for w in words:
        pair = w // 2 + lag
        sec, within = divmod(pair, _SPP)
        if not 0 <= sec < nsec:
            msg = f"word {w} outside the {nsec}-sector capture"
            raise SystemExit(msg)
        raw[sec, (within * 4) // 8] = 0xFF  # whole byte: 2 pairs, both flagged
    raw.tofile(out)


def _damage(pcm_path: Path, words: list[int]) -> None:
    """Flip every bit of each named word, in place."""
    with open(pcm_path, "r+b") as f:
        for w in words:
            f.seek(w * 2)
            cur = int.from_bytes(f.read(2), "little")
            f.seek(w * 2)
            f.write((cur ^ 0xFFFF).to_bytes(2, "little"))


def _run(
    label: str,
    pristine: Path,
    damaged_words: list[int],
    lsns: list[int],
    last: int,
    work: Path,
    c2: Path | None,
) -> tuple[bool, str, bool]:
    """One repair attempt on a fresh damaged copy. Returns (repaired, reason, byte-exact)."""
    trial = work / f"trial_{label}.pcm"
    shutil.copyfile(pristine, trial)
    _damage(trial, damaged_words)

    res = C.repair_whole_disc(
        trial,
        lsns,
        last,
        0,  # cddb_id: only the AR gate uses it, and that gate is off here
        0,
        c2_path=c2,
        ctanalyse_bin=str(Path(__file__).resolve().parent / "ctanalyse" / "ctanalyse"),
        cache_dir=work,
        verify_ar_gate=False,  # AR is a second opinion; the CTDB CRC gate is the subject
    )
    exact = trial.read_bytes() == pristine.read_bytes() if res.repaired else False
    trial.unlink(missing_ok=True)
    return res.repaired, f"{res.reason} (used_c2={res.used_c2})", exact


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rbi", type=Path, required=True)
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--errors", type=int, default=6, help="damaged words in one column")
    ap.add_argument("--column", type=int, default=5000)
    ap.add_argument("--row", type=int, default=17000)
    args = ap.parse_args()

    args.work.mkdir(parents=True, exist_ok=True)
    pcm_path, lsns, last = _extract(args.rbi, args.work)
    bounds = [*lsns, last + 1]
    nsec = pcm_path.stat().st_size // _FRAME
    print(f"disc: {len(lsns)} tracks, bounds[0]={bounds[0]}, lead-out={bounds[-1]}")
    if bounds[0] == 0:
        print("REFUSED: bounds[0] == 0 — this disc cannot exercise the origin shift")
        return 2

    entries = C.load_entries(bounds, len(lsns), xml_cache=args.work / "ctdb.xml")
    if not entries:
        print("REFUSED: disc not in CTDB")
        return 2
    sel = C.select_entry(pcm_path.read_bytes(), entries, bounds, len(lsns))
    if sel is None:
        print("REFUSED: no CTDB entry reconciles with this rip")
        return 2
    print(
        f"entry {sel.entry.id}: npar={sel.entry.npar} stride={sel.entry.stride} "
        f"offset={sel.offset:+d} damaged={sel.damaged or 'none'}"
    )
    if sel.damaged:
        print("REFUSED: baseline is not clean — inject damage into a clean image only")
        return 2
    if sel.entry.npar != 8:
        print(f"NOTE: npar={sel.entry.npar}, capacities differ from the 4/8 assumed")

    stride = sel.entry.stride * 2
    word_base = bounds[0] * _WPF
    # Spacing the damage by exactly one stride keeps every word in the same column
    # whatever offset ctanalyse settles on — column = (w - 2*off - stride) % stride.
    local = [(args.row + 1 + r) * stride + args.column for r in range(args.errors)]
    words = [w + word_base for w in local]
    print(
        f"damaging {len(words)} words in column {args.column}, "
        f"rows {args.row}..{args.row + args.errors - 1} "
        f"(error-only capacity {sel.entry.npar // 2}, erasure capacity {sel.entry.npar})"
    )

    _synth_c2(nsec, words, args.work / "correct.c2")
    # The wrong-origin control: place the flags where they would belong if ctanalyse
    # did NOT skip word_base — i.e. shifted down by exactly the pre-gap.
    _synth_c2(nsec, [w - word_base for w in words], args.work / "shifted.c2")

    trials = [
        ("correct", args.work / "correct.c2", True),
        ("erroronly", None, False),
        ("shifted", args.work / "shifted.c2", False),
    ]
    results = []
    for label, c2, expect in trials:
        repaired, reason, exact = _run(
            label, pcm_path, words, lsns, last, args.work, c2
        )
        ok = repaired == expect and (exact if expect else True)
        results.append(ok)
        print(
            f"  {label:10s} expect={'repair' if expect else 'refuse':6s} "
            f"got={'repair' if repaired else 'refuse':6s} "
            f"byte-exact={exact} -> {'PASS' if ok else 'FAIL'}   [{reason}]"
        )

    verdict = all(results)
    print(
        "\nVERDICT: "
        + (
            "origin shift CONFIRMED on real media with bounds[0] != 0"
            if verdict
            else "INCONCLUSIVE — see the failing trial above"
        )
    )
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
