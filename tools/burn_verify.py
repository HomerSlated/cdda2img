#!/usr/bin/env python3
"""Verify a burned disc against the source image it was burned from.

Takes an RBI produced by ripping the *physical* disc and the cdrdao ``.toc`` the
burn was fed from, and reports three independently pass/fail layers: geometry,
identifiers/CD-Text, and PCM.

Two things this tool is careful about, both of which produce a confidently wrong
answer if skipped:

**The source is not just its audio files.** ``SILENCE``/``ZERO`` directives, and
the pre-gap a bare ``START`` generates on a track whose file holds audio only,
are frames the *writer* synthesises. They exist on the disc and in no file. On
ABBA *Gold* they account for 230 frames — sum the WAVs alone and every track
boundary after the first is wrong. We therefore model the source as an ordered
list of :class:`Segment` runs, file-backed or synthetic, and the sum of those
segments must equal the disc's lead-out before any PCM claim is made.

**The lag is measured, never predicted.** A burn round-trip crosses two offset
domains (the writer's write offset, the reader's read offset) whose sign
conventions are easy to invert; a predicted lag turns a clean pass into a
phantom bug or hides a real one. We cross-correlate at several points across the
disc, which also separates a constant offset (benign, explainable) from drift
(a real defect).

Usage::

    uv run python tools/burn_verify.py --rbi disc.rbi --source /var/tmp/cdr.toc
"""

from __future__ import annotations

import argparse
import re
import sys
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cdda2img.container import read_header
from cdda2img.rbi_format import BLOCK_TYPE_PCM, BLOCK_TYPE_TOC
from cdda2img.toc_parser import ParsedDisc, parse_toc

BYTES_PER_FRAME = 2352
SAMPLES_PER_FRAME = 588  # stereo samples (4 bytes each) per CD frame

_TRACK_SPLIT_RE = re.compile(r"^TRACK\s+AUDIO", re.MULTILINE)
_SILENCE_RE = re.compile(r"^\s*(?:SILENCE|ZERO)\s+(\d+:\d+:\d+)", re.MULTILINE)
_FILE_RE = re.compile(r'^\s*FILE\s+"([^"]+)"\s+(\S+)\s+(\d+:\d+:\d+)', re.MULTILINE)
_START_RE = re.compile(r"^\s*START\s+(\d+:\d+:\d+)", re.MULTILINE)


def msf(ts: str) -> int:
    m, s, f = (int(x) for x in ts.split(":"))
    return (m * 60 + s) * 75 + f


@dataclass
class Segment:
    """A contiguous run of disc frames contributed by one source directive.

    ``path is None`` marks frames the writer synthesises (SILENCE/ZERO, or a
    generated pre-gap): present on the disc, absent from every file.
    """

    start_frame: int  # absolute LBA
    frames: int
    path: Path | None = None
    data_offset: int = 0  # byte offset of the WAV data chunk


@dataclass
class SourceTrack:
    number: int
    start_frame: int  # absolute LBA of INDEX 00 (pre-gap start)
    pregap_frames: int
    frames: int  # total, including pre-gap
    segments: list[Segment] = field(default_factory=list)

    @property
    def audio_start_frame(self) -> int:
        return self.start_frame + self.pregap_frames


# ── loading ──────────────────────────────────────────────────────────────────


def load_rbi(path: Path) -> tuple[ParsedDisc, np.ndarray, int]:
    """Return (parsed TOC, stereo int16 samples, frame count) for an RBI."""
    header = read_header(path)
    toc_entry = header.find_block(BLOCK_TYPE_TOC)
    pcm_entry = header.find_block(BLOCK_TYPE_PCM)
    if toc_entry is None or pcm_entry is None:
        msg = f"{path}: missing TOC or PCM block"
        raise SystemExit(msg)
    with path.open("rb") as f:
        f.seek(toc_entry.offset)
        disc = parse_toc(f.read(toc_entry.length))
    pcm = np.memmap(
        path,
        dtype=np.int16,
        mode="r",
        offset=pcm_entry.offset,
        shape=(pcm_entry.length // 2,),
    ).reshape(-1, 2)
    return disc, pcm, pcm_entry.length // BYTES_PER_FRAME


def _wav_data_span(path: Path) -> tuple[int, int]:
    """Return (data-chunk byte offset, data byte length) for a Red Book WAV."""
    with wave.open(str(path), "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (2, 2, 44100):
            msg = f"{path}: not Red Book 16-bit stereo 44.1 kHz"
            raise SystemExit(msg)
        nframes = w.getnframes()
    with path.open("rb") as f:
        f.seek(12)
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                msg = f"{path}: no data chunk"
                raise SystemExit(msg)
            size = int.from_bytes(hdr[4:8], "little")
            if hdr[0:4] == b"data":
                return f.tell(), min(size, nframes * 4)
            f.seek(size + (size & 1), 1)


def load_source(toc_path: Path) -> tuple[ParsedDisc, list[SourceTrack]]:
    """Parse a source .toc into absolute-addressed segments.

    Segment order within a track block is the order the directives appear, which
    is the order the writer lays them on the disc.
    """
    raw = toc_path.read_bytes()
    disc = parse_toc(raw)
    text = raw.decode("utf-8")

    marks = list(_TRACK_SPLIT_RE.finditer(text))
    if len(marks) != len(disc.tracks):
        msg = f"{toc_path}: {len(marks)} TRACK blocks but {len(disc.tracks)} parsed"
        raise SystemExit(msg)

    tracks: list[SourceTrack] = []
    cursor = 0
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        block = text[m.start() : end]

        segs: list[Segment] = []
        # Directives in source order: SILENCE/ZERO runs and FILE spans.
        for d in sorted(
            [*_SILENCE_RE.finditer(block), *_FILE_RE.finditer(block)],
            key=lambda x: x.start(),
        ):
            if d.re is _SILENCE_RE:
                n = msf(d.group(1))
                segs.append(Segment(cursor, n))
                cursor += n
            else:
                name, _start, length = d.group(1), d.group(2), d.group(3)
                audio = (toc_path.parent / name).resolve()
                if not audio.is_file():
                    msg = f"{toc_path}: missing audio file {audio}"
                    raise SystemExit(msg)
                off, blen = _wav_data_span(audio)
                declared = msf(length)
                have = blen // BYTES_PER_FRAME
                if declared != have:
                    print(
                        f"  note: {name} declares {declared} frames, file holds "
                        f"{have} — using {min(declared, have)}",
                        file=sys.stderr,
                    )
                n = min(declared, have)
                segs.append(Segment(cursor, n, audio, off))
                cursor += n

        start_ts = _START_RE.search(block)
        start_frames = msf(start_ts.group(1)) if start_ts else 0
        pre_synth = sum(s.frames for s in segs if s.path is None)
        track_start = segs[0].start_frame if segs else cursor

        # A bare START with no preceding SILENCE means the writer generates the
        # pre-gap; the file holds audio only. Splice that run in at the front.
        if start_frames > pre_synth:
            gen = start_frames - pre_synth
            for s in segs:
                s.start_frame += gen
            segs.insert(0, Segment(track_start, gen))
            cursor += gen

        tracks.append(
            SourceTrack(
                number=disc.tracks[i].track_number,
                start_frame=track_start,
                pregap_frames=start_frames,
                frames=sum(s.frames for s in segs),
                segments=segs,
            )
        )
    return disc, tracks


def seg_samples(seg: Segment) -> np.ndarray:
    if seg.path is None:
        return np.zeros((seg.frames * SAMPLES_PER_FRAME, 2), dtype=np.int16)
    return np.memmap(
        seg.path,
        dtype=np.int16,
        mode="r",
        offset=seg.data_offset,
        shape=(seg.frames * SAMPLES_PER_FRAME * 2,),
    ).reshape(-1, 2)


# ── layers ───────────────────────────────────────────────────────────────────


def report_geometry(
    src_tracks: list[SourceTrack], rip_disc: ParsedDisc, rip_frames: int
) -> bool:
    print("\n=== 1. GEOMETRY ===")
    src_total = sum(t.frames for t in src_tracks)
    synth = sum(s.frames for t in src_tracks for s in t.segments if s.path is None)
    filed = src_total - synth
    print(f"  source: {filed} file frames + {synth} writer-generated = {src_total}")
    print(f"  rip PCM frames: {rip_frames}   delta {rip_frames - src_total:+}")
    print(f"  tracks: source {len(src_tracks)}  rip {len(rip_disc.tracks)}")
    if len(src_tracks) != len(rip_disc.tracks):
        print("  → geometry FAIL (track count)")
        return False

    ok = src_total == rip_frames
    print(
        f"\n  {'trk':>3} {'src INDEX00':>12} {'rip INDEX00':>12} {'d':>4} "
        f"{'src pregap':>11} {'rip pregap':>11} {'src len':>9} {'rip len':>9}"
    )
    for s, r in zip(src_tracks, rip_disc.tracks, strict=True):
        d = r.start_frame - s.start_frame
        rlen = r.pregap_frames + r.duration_frames
        bad = d != 0 or s.pregap_frames != r.pregap_frames or rlen != s.frames
        ok = ok and not bad
        print(
            f"  {s.number:>3} {s.start_frame:>12} {r.start_frame:>12} {d:>+4} "
            f"{s.pregap_frames:>11} {r.pregap_frames:>11} {s.frames:>9} "
            f"{rlen:>9}{'  <-- differs' if bad else ''}"
        )
    print(f"  → geometry {'PASS' if ok else 'DIFFERS'}")
    return ok


def report_identifiers(src_disc: ParsedDisc, rip_disc: ParsedDisc) -> bool:
    print("\n=== 2. IDENTIFIERS + CD-TEXT ===")
    ok = True

    def cmp(label: str, a: object, b: object) -> None:
        nonlocal ok
        if a == b:
            print(f"  [ok ] {label:<18} {a!r}")
        else:
            ok = False
            print(f"  [DIFF] {label:<18} source={a!r}  rip={b!r}")

    cmp("MCN / CATALOG", src_disc.catalog, rip_disc.catalog)
    cmp("disc title", src_disc.title, rip_disc.title)
    cmp("disc performer", src_disc.performer, rip_disc.performer)

    bad = {"ISRC": 0, "TITLE": 0, "PERFORMER": 0}
    for s, r in zip(src_disc.tracks, rip_disc.tracks, strict=True):
        for key, a, b in (
            ("ISRC", s.isrc, r.isrc),
            ("TITLE", s.title, r.title),
            ("PERFORMER", s.performer, r.performer),
        ):
            if a != b:
                bad[key] += 1
                print(
                    f"  [DIFF] track {s.track_number:>2} {key:<9} "
                    f"source={a!r} rip={b!r}"
                )
    n = len(src_disc.tracks)
    for key, cnt in bad.items():
        print(f"  {key:<10} {n - cnt}/{n} identical")
    ok = ok and not any(bad.values())
    print(f"  → identifiers {'PASS' if ok else 'DIFFERS'}")
    return ok


def measure_lag(
    ref: np.ndarray, rip: np.ndarray, at: int, max_lag: int
) -> tuple[int, int, int]:
    """Return (best lag in stereo samples, best SAD, runner-up SAD).

    *ref* is a source window whose absolute disc position is *at*. A positive
    lag means that window appears *later* in the rip than in the source.
    """
    window = len(ref)
    ref64 = ref.astype(np.int64)
    sads: list[tuple[int, int]] = []
    for lag in range(-max_lag, max_lag + 1):
        lo = at + lag
        if lo < 0 or lo + window > len(rip):
            continue
        sads.append((
            int(np.abs(rip[lo : lo + window].astype(np.int64) - ref64).sum()),
            lag,
        ))
    sads.sort()
    return sads[0][1], sads[0][0], (sads[1][0] if len(sads) > 1 else -1)


def _probe_lags(
    src_tracks: list[SourceTrack], rip_pcm: np.ndarray, max_lag: int, window: int
) -> list[tuple[int, int, int, int]]:
    """Measure the lag at three points, so a constant offset is distinguishable
    from drift. Returns (track number, lag, SAD, runner-up SAD) per probe."""
    probes = []
    for idx in (0, len(src_tracks) // 2, len(src_tracks) - 1):
        big = max(
            (s for s in src_tracks[idx].segments if s.path is not None),
            key=lambda s: s.frames,
            default=None,
        )
        if big is None or big.frames * SAMPLES_PER_FRAME < window + 2 * max_lag:
            continue
        data = seg_samples(big)
        rel = (len(data) - window) // 2
        at = big.start_frame * SAMPLES_PER_FRAME + rel
        probes.append((
            src_tracks[idx].number,
            *measure_lag(data[rel : rel + window], rip_pcm, at, max_lag),
        ))
    return probes


def _diff_track(
    track: SourceTrack, rip_pcm: np.ndarray, lag: int
) -> tuple[int, int, int, int]:
    """Compare one track's segments against the rip at *lag*.

    Returns (differing samples, compared samples, max |delta|, first differing
    frame or -1). Segments are clipped to the rip, so a writer-synthesised
    segment that falls outside it simply contributes nothing."""
    tdiff = tsamp = tmax = 0
    first = -1
    for seg in track.segments:
        data = seg_samples(seg)
        lo = seg.start_frame * SAMPLES_PER_FRAME + lag
        hi = lo + len(data)
        if lo < 0:
            data = data[-lo:]
            lo = 0
        if hi > len(rip_pcm):
            data = data[: len(rip_pcm) - lo]
        if len(data) == 0:
            continue
        d = rip_pcm[lo : lo + len(data)].astype(np.int32) - data.astype(np.int32)
        nz = np.flatnonzero(d.any(axis=1))
        tdiff += int(nz.size)
        tsamp += len(data)
        if d.size:
            tmax = max(tmax, int(np.abs(d).max()))
        if nz.size and first < 0:
            first = (lo + int(nz[0])) // SAMPLES_PER_FRAME
    return tdiff, tsamp, tmax, first


def report_pcm(
    src_tracks: list[SourceTrack], rip_pcm: np.ndarray, max_lag: int
) -> bool:
    print("\n=== 3. PCM ===")
    window = 1 << 16

    probes = _probe_lags(src_tracks, rip_pcm, max_lag, window)
    print(f"  lag probes (window {window} samples, search ±{max_lag}):")
    for num, lag, sad, runner in probes:
        sharp = "unique peak" if sad == 0 or runner > sad * 4 else "WEAK peak"
        print(
            f"    track {num:>2}: lag {lag:+d}  SAD {sad}  runner-up {runner}"
            f"  [{sharp}]"
        )
    lags = {p[1] for p in probes}
    if len(lags) != 1:
        print(f"  → PCM FAIL: lag not constant {sorted(lags)} — drift, not offset")
        return False
    lag = lags.pop()
    print(f"  measured constant lag: {lag:+d} stereo samples ({lag / 44.1:+.3f} ms)")

    # 3b. Per-track diff at that lag, segment by segment.
    print(f"\n  per-track diff at lag {lag:+d}:")
    print(
        f"  {'trk':>3} {'samples':>10} {'differing':>11} {'max|d|':>8} "
        f"{'first diff frame':>17}"
    )
    tot_diff = tot = 0
    worst = 0
    for t in src_tracks:
        tdiff, tsamp, tmax, first = _diff_track(t, rip_pcm, lag)
        tot_diff += tdiff
        tot += tsamp
        worst = max(worst, tmax)
        print(
            f"  {t.number:>3} {tsamp:>10} {tdiff:>11} {tmax:>8} "
            f"{(first if first >= 0 else '-'):>17}"
        )
    pct = 100.0 * tot_diff / max(tot, 1)
    print(
        f"\n  aggregate: {tot_diff}/{tot} stereo samples differ ({pct:.6f}%), "
        f"max |delta| {worst}"
    )
    ok = tot_diff == 0
    print(f"  → PCM {'PASS (sample-exact at the measured lag)' if ok else 'DIFFERS'}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify a burn against its source")
    ap.add_argument("--rbi", required=True, type=Path)
    ap.add_argument("--source", required=True, type=Path)
    ap.add_argument("--max-lag", type=int, default=200)
    args = ap.parse_args()

    rip_disc, rip_pcm, rip_frames = load_rbi(args.rbi)
    src_disc, src_tracks = load_source(args.source)
    print(f"readback : {args.rbi}")
    print(f"source   : {args.source}")

    g = report_geometry(src_tracks, rip_disc, rip_frames)
    i = report_identifiers(src_disc, rip_disc)
    p = report_pcm(src_tracks, rip_pcm, args.max_lag)

    print("\n=== SUMMARY ===")
    for label, res in (("geometry", g), ("identifiers", i), ("PCM", p)):
        print(f"  {label:<12} {'PASS' if res else 'DIFFERS'}")
    return 0 if (g and i and p) else 1


if __name__ == "__main__":
    raise SystemExit(main())
