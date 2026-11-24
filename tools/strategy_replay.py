#!/usr/bin/env python3
"""Replay a recovery strategy offline: the rip's own code, a simulated drive.

Answers "does the strategy path do what we think?" without touching a drive. It
runs ``cdda2img._recover_by_strategy`` unchanged, at the drive's real read offset
and against the real AccurateRip dBAR, on a capture rebuilt from a verified RBI
with chosen sectors corrupted. Only ``accudisc_reader.read_span_windows`` is
replaced, by a drive whose behaviour each scenario sets:

0. self-test: the uncorrupted track AR-matches through ``_corrected_from_window``
   (proves the raw rebuild, the offset, the TOC and the dBAR before anything else).
1. good drive: the flagged sector is corrupt and every re-read returns it intact.
   Must match on read 1 and restore the track byte-exact.
2. stale drive: every re-read returns the same corrupt bytes. Must not match, and
   must leave the capture byte-identical (no unverified write).
3. misplaced drive: re-reads return the NEXT sector's good bytes. Must not match:
   the control that shows the AR gate can fail on a misplaced splice.
4. unflagged neighbour: the sector BEFORE the flagged one is corrupt. Run with
   both sector-hammer and sector-runup to show what each read covers.
5. two flagged sectors, each read correctly only at 8x: sector-hammer stepping the
   ladder once per round of targets (``ladder = "full"``, since 2026-11-24) must
   recover in the 8x round; the same profile held at 8x (``ladder = "single"``)
   recovers on read 2. Before the per-round fix, stepping per read never read both
   at 8x together and never recovered.
   5c: five flagged sectors, each read correctly only at 24x, 25 reads allowed. Per
   round, the 24x round repairs all five (read 15); per read, sector j was only
   ever read at rung j, so four of them never met 24x.

The rebuilt capture is a SPARSE file of the disc's full size under /var/tmp holding
only the track's window, deleted on exit (scratch scope rule).

Usage (from the project root; one HTTPS request to AccurateRip, no drive I/O):
    uv run python tools/strategy_replay.py "Tracy Chapman_2.rbi" \
        --track 8 --sector 113044 --second 114437 --read-offset 30
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from cdda2img import accudisc_reader as ar
from cdda2img import cdda2img as app
from cdda2img import rbi_format as R
from cdda2img.accuraterip import fetch_ar_responses, match_track_pcm
from cdda2img.cddb import compute_cddb_disc_id
from cdda2img.container import read_header
from cdda2img.recovery_profile import load_profile
from cdda2img.strategy_reads import strategy_kind
from cdda2img.toc_parser import parse_toc

_B = 2352
_LADDER = [40, 32, 24, 8, 4]


class Disc:
    """The verified RBI, read as the raw capture the drive would have delivered."""

    def __init__(self, rbi: Path, read_offset: int) -> None:
        hdr = read_header(rbi)
        toc = hdr.find_block(R.BLOCK_TYPE_TOC)
        pcm = hdr.find_block(R.BLOCK_TYPE_PCM)
        if toc is None or pcm is None:
            msg = f"{rbi}: no TOC or PCM block"
            raise SystemExit(msg)
        with rbi.open("rb") as f:
            f.seek(toc.offset)
            parsed = parse_toc(f.read(toc.length))
        tracks = sorted(parsed.tracks, key=lambda t: t.track_number)
        self.track_lsns = [t.audio_start_frame for t in tracks]
        self.disc_last_lsn = pcm.length // _B - 1
        self.cddb_id = int(
            compute_cddb_disc_id(self.track_lsns, self.disc_last_lsn), 16
        )
        self.rbi, self.pcm_offset, self.pcm_length = rbi, pcm.offset, pcm.length
        self.read_offset = read_offset

    def raw(self, lo: int, hi: int) -> bytes:
        """Raw sectors [lo, hi): stored PCM is raw shifted left by the offset, so
        raw byte i is stored byte i - offset*4 (zeros where that is off the disc)."""
        a = lo * _B - self.read_offset * 4
        b = hi * _B - self.read_offset * 4
        out = bytearray((hi - lo) * _B)
        s, e = max(0, a), min(b, self.pcm_length)
        if e > s:
            with self.rbi.open("rb") as f:
                f.seek(self.pcm_offset + s)
                out[s - a : e - a] = f.read(e - s)
        return bytes(out)


def corrupt(sector: bytes) -> bytes:
    """A sector with 200 samples in its middle bit-flipped: wrong, and loud."""
    buf = bytearray(sector)
    for i in range(800, 1600):
        buf[i] ^= 0x55
    return bytes(buf)


class Drive:
    """A fake ``read_span_windows``. *serve(lba, speed)* returns one sector."""

    def __init__(self, serve: Any) -> None:
        self.serve = serve
        self.reads: list[tuple[int, int, int | None]] = []

    def __call__(self, device, start, count, windows, read_speed=None, **_kw):
        self.reads.append((start, count, read_speed))
        pieces = []
        for lo, hi in windows:
            a, b = max(lo, start), min(hi, start + count)
            pieces.append(b"".join(self.serve(x, read_speed) for x in range(a, b)))
        return ar.WindowsRead(
            pieces=pieces, speed_honoured_x=read_speed, cache_defeat="done"
        )


def run(
    disc: Disc,
    responses: list,
    track: int,
    profile: Any,
    bad: set[int],
    flagged: set[int],
    serve: Any,
    *,
    fixed_speed: int | None = None,
) -> tuple[str, str, list[tuple[int, int, int | None]], bool, bool]:
    """One replay. Returns (outcome, reads record, drive log, restored, untouched)."""
    w = app._raw_track_window(
        disc.track_lsns, disc.disc_last_lsn, track - 1, disc.read_offset
    )
    truth = disc.raw(w.lo, w.hi)
    capture = bytearray(truth)
    for x in bad:
        o = (x - w.lo) * _B
        capture[o : o + _B] = corrupt(truth[o : o + _B])
    leadout = disc.disc_last_lsn + 1
    damage = bytes(1 if x in flagged else 0 for x in range(leadout))
    drive = Drive(serve)
    with (
        mock.patch.object(ar, "read_span_windows", drive),
        tempfile.TemporaryDirectory(dir="/var/tmp", prefix="strategy_replay_") as d,
    ):
        pcm = Path(d) / "capture.pcm"
        with pcm.open("wb") as f:
            f.truncate(leadout * _B)  # sparse: only the window below is real
            f.seek(w.lo * _B)
            f.write(capture)
        outcomes, prov = app._recover_by_strategy(
            strategy_kind(profile) or "",
            profile,
            device="/dev/replay-no-drive",
            failed_tracks=[SimpleNamespace(track=track)],
            track_lsns=disc.track_lsns,
            disc_last_lsn=disc.disc_last_lsn,
            pcm_file=pcm,
            responses=responses,
            ladder=_LADDER,
            read_offset=disc.read_offset,
            disc_damage=damage,
            ui=None,
            seed=1,
            fixed_speed=fixed_speed,
        )
        with pcm.open("rb") as f:
            f.seek(w.lo * _B)
            after = f.read(w.hi * _B - w.lo * _B)
    # The track's own bytes: raw [start*2352 + off*4, end*2352 + off*4) of the window.
    t0 = (disc.track_lsns[track - 1] - w.lo) * _B + disc.read_offset * 4
    t1 = t0 + w.track_bytes
    restored = after[t0:t1] == truth[t0:t1]
    untouched = after == bytes(capture)
    return (
        outcomes[track],
        prov.get(f"strategy_reads_track_{track}", ""),
        drive.reads,
        restored,
        untouched,
    )


def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    p.add_argument("rbi", type=Path)
    p.add_argument("--track", type=int, default=8)
    p.add_argument("--sector", type=int, default=113044)
    p.add_argument("--second", type=int, default=114437)
    p.add_argument("--read-offset", type=int, default=30)
    a = p.parse_args()

    disc = Disc(a.rbi, a.read_offset)
    responses, transport, _ = fetch_ar_responses(
        disc.track_lsns, disc.disc_last_lsn, disc.cddb_id
    )
    if not responses:
        print(f"AccurateRip returned nothing ({transport}); cannot replay")
        return 1
    x, y, t = a.sector, a.second, a.track
    hammer = load_profile("sector-hammer")
    runup = load_profile("sector-runup")
    good = lambda lba, _s: disc.raw(lba, lba + 1)
    fails = 0

    def check(name: str, got: tuple, want_match: bool, want_restored: bool) -> None:
        nonlocal fails
        outcome, record, reads, restored, untouched = got
        matched = outcome != "unrecovered"
        ok = matched == want_match and (restored if want_restored else untouched)
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        print(
            f"      outcome={outcome}  reads={len(reads)}  restored={restored}  untouched={untouched}"
        )
        print(f"      first reads: {record.split(',')[:6]}")

    # 0. self-test: the rebuilt, uncorrupted track verifies at this offset.
    w = app._raw_track_window(
        disc.track_lsns, disc.disc_last_lsn, t - 1, disc.read_offset
    )
    corrected = app._corrected_from_window(disc.raw(w.lo, w.hi), w, disc.read_offset)
    _v1, _v2, c1, c2 = match_track_pcm(corrected, t, len(disc.track_lsns), responses)
    ok0 = bool(c1 or c2)
    fails += not ok0
    print(
        f"{'PASS' if ok0 else 'FAIL'}  0 self-test: uncorrupted track {t} AR conf v1={c1} v2={c2}"
    )
    if not ok0:
        return 1

    check(
        "1 good drive: hammer recovers on read 1",
        run(disc, responses, t, hammer, {x}, {x}, good),
        True,
        True,
    )

    def stale(lba: int, _s: int) -> bytes:
        s = disc.raw(lba, lba + 1)
        return corrupt(s) if lba == x else s

    check(
        "2 stale drive: no match, capture untouched",
        run(disc, responses, t, hammer, {x}, {x}, stale),
        False,
        False,
    )
    check(
        "3 misplaced drive (sector X+1 served for X): no match",
        run(
            disc,
            responses,
            t,
            hammer,
            {x},
            {x},
            lambda lba, _s: disc.raw(lba + 1, lba + 2),
        ),
        False,
        False,
    )
    check(
        "4a unflagged neighbour X-1 corrupt, X flagged: hammer cannot reach it",
        run(disc, responses, t, hammer, {x - 1}, {x}, good),
        False,
        False,
    )
    check(
        "4b same fault: sector-runup's 35-sector read covers X-1",
        run(disc, responses, t, runup, {x - 1}, {x}, good),
        True,
        True,
    )

    def only_8x(lba: int, speed: int) -> bytes:
        s = disc.raw(lba, lba + 1)
        return s if speed == 8 or lba not in (x, y) else corrupt(s)

    check(
        f"5a two flagged ({x}, {y}), each good only at 8x: hammer stepping per round",
        run(disc, responses, t, hammer, {x, y}, {x, y}, only_8x),
        True,
        True,
    )
    single = dataclasses.replace(hammer, ladder="single")
    check(
        "5b same, hammer held at 8x (ladder = single)",
        run(disc, responses, t, single, {x, y}, {x, y}, only_8x, fixed_speed=8),
        True,
        True,
    )
    lo, hi = (
        disc.track_lsns[t - 1],
        disc.track_lsns[t] if t < len(disc.track_lsns) else disc.disc_last_lsn + 1,
    )
    five = {x + 150 * j for j in range(5)}
    if not all(lo <= s < hi for s in five):
        print(f"SKIP  5c: {sorted(five)} not all inside track {t} [{lo}, {hi})")
    else:

        def only_24x(lba: int, speed: int) -> bytes:
            s = disc.raw(lba, lba + 1)
            return s if speed == 24 or lba not in five else corrupt(s)

        many = dataclasses.replace(hammer, passes=25)
        check(
            "5c five flagged, each good only at 24x, 25 reads: the 24x round repairs all",
            run(disc, responses, t, many, five, five, only_24x),
            True,
            True,
        )
    print(f"\n{fails} unexpected result(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
