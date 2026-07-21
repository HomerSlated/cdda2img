"""fix_offset.py — detect and repair the sample offset of a CD rip.

Two jobs, either of which can be run alone:

  **Detect** — recover the offset a rip was made at, by sliding its checksum
  window against AccurateRip until the database agrees
  (``accuraterip.detect_offset``). Works on a set of per-track lossless files
  or on one of our own ``.rbi`` containers. This is also how you identify
  *media*: a pressed disc read on a correctly-configured drive detects at ~0,
  whereas a CD-R detects at the offset its burner baked in, so a stable
  non-zero reading on otherwise clean audio is evidence the disc is a copy.

  **Repair** — rewrite a per-track rip at a corrected offset. The whole point,
  and the reason this cannot be done file-by-file: shifting a rip moves samples
  *across track boundaries*. Track 4's first samples belong at the end of track
  3. So the tracks are concatenated, the single stream is shifted, and the
  result is re-split at the original track lengths. Only the very start and end
  of the disc become zeros, because only there is the audio genuinely absent.

Sign convention (the same one verify_rip uses for ``read_offset``, and the same
one EAC/arverify report): a **positive** offset means the audio sits N samples
late in the file, so correcting it drops N samples from the front. Feed the
detected value straight back in as ``--offset``.

Usage:
    # what offset is this rip at?
    uv run python tools/fix_offset.py detect rips/some-album/
    uv run python tools/fix_offset.py detect "rips/cdda2img/Tracy Chapman.rbi"

    # rewrite it (writes a new directory; never touches the originals)
    uv run python tools/fix_offset.py apply --offset -30 rips/some-album/
    uv run python tools/fix_offset.py apply --detect rips/some-album/

Notes:
  * A rip whose track 1 does not start at LSN 0 (a program-area pre-gap, e.g.
    ABBA "Gold") cannot be reconstructed from files alone — pass ``--pregap N``
    sectors so the disc IDs come out right.
  * Every track must be a whole number of 588-sample sectors. A file that is
    not did not come off a CD unmodified, and an offset does not apply to it.
  * ``.rbi`` input is detect-only: rewriting a container means rebuilding its
    TOC and block digests, which is the pipeline's job, not this tool's.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import av
import numpy as np
from av.audio.frame import AudioFrame

from cdda2img import rbi_format as R
from cdda2img.accuraterip import (
    OffsetMatch,
    detect_offset,
    fetch_ar_responses,
)
from cdda2img.cddb import compute_cddb_disc_id
from cdda2img.container import read_header, resolve_temp_dir
from cdda2img.toc_parser import parse_toc

_BYTES_PER_SAMPLE = 4  # stereo s16le
_SAMPLES_PER_SECTOR = 588
_BYTES_PER_SECTOR = _SAMPLES_PER_SECTOR * _BYTES_PER_SAMPLE
_AUDIO_SUFFIXES = (".flac", ".wav", ".wv", ".ape", ".aiff", ".aif", ".m4a", ".alac")


# ── loading ───────────────────────────────────────────────────────────────────


@dataclass
class Rip:
    """A rip flattened to one contiguous s16le stream on disk.

    *pcm_path* is a scratch file so the stream can be memory-mapped rather than
    held in RAM — a full disc is ~850 MB.
    """

    pcm_path: Path
    lengths: list[int]  # samples per track
    sources: list[Path]  # empty for .rbi input
    pregap: int = 0  # sectors before track 1 (program-area pre-gap)

    @property
    def total_samples(self) -> int:
        return sum(self.lengths)

    @property
    def track_lsns(self) -> list[int]:
        lsns, running = [], self.pregap
        for length in self.lengths:
            lsns.append(running)
            running += length // _SAMPLES_PER_SECTOR
        return lsns

    @property
    def disc_last_lsn(self) -> int:
        return self.pregap + self.total_samples // _SAMPLES_PER_SECTOR - 1

    @property
    def cddb_id(self) -> int:
        return int(compute_cddb_disc_id(self.track_lsns, self.disc_last_lsn), 16)


def decode_pcm(path: Path) -> bytes:
    """Decode any PyAV-readable lossless file to raw s16le 44.1 kHz stereo."""
    chunks: list[bytes] = []
    with av.open(str(path)) as container:
        if not container.streams.audio:
            msg = f"{path.name}: no audio stream"
            raise SystemExit(msg)
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="stereo", rate=44100)
        for packet in container.demux(stream):
            for frame in packet.decode():
                if not isinstance(frame, AudioFrame):
                    continue
                frame.pts = None
                for out in resampler.resample(frame):
                    chunks.append(out.to_ndarray().astype(np.int16).tobytes())
        for out in resampler.resample(None):  # flush
            chunks.append(out.to_ndarray().astype(np.int16).tobytes())
    return b"".join(chunks)


def _source_files(inputs: list[Path]) -> list[Path]:
    files: list[Path] = []
    for item in inputs:
        if item.is_dir():
            files.extend(
                sorted(p for p in item.iterdir() if p.suffix.lower() in _AUDIO_SUFFIXES)
            )
        else:
            files.append(item)
    if not files:
        msg = "no audio files found"
        raise SystemExit(msg)
    return files


def load_tracks(inputs: list[Path], scratch: Path, pregap: int = 0) -> Rip:
    """Decode a per-track rip into one contiguous scratch stream."""
    files = _source_files(inputs)
    pcm_path = scratch / "concat.pcm"
    lengths: list[int] = []
    with pcm_path.open("wb") as out:
        for path in files:
            pcm = decode_pcm(path)
            if len(pcm) % _BYTES_PER_SECTOR:
                short = len(pcm) % _BYTES_PER_SECTOR // _BYTES_PER_SAMPLE
                msg = (
                    f"{path.name}: {len(pcm) // _BYTES_PER_SAMPLE} samples is not a "
                    f"whole number of 588-sample sectors ({short} over) — this did "
                    "not come off a CD unmodified, so an offset does not apply"
                )
                raise SystemExit(msg)
            lengths.append(len(pcm) // _BYTES_PER_SAMPLE)
            out.write(pcm)
            print(f"  decoded {path.name}: {lengths[-1]} samples")
    return Rip(pcm_path=pcm_path, lengths=lengths, sources=files, pregap=pregap)


def load_rbi(rbi: Path, scratch: Path) -> Rip:
    """Extract an .rbi container's PCM and track geometry (detect-only)."""
    header = read_header(rbi)
    pcm_blk = header.find_block(R.BLOCK_TYPE_PCM)
    toc_blk = header.find_block(R.BLOCK_TYPE_TOC)
    if pcm_blk is None or toc_blk is None:
        msg = f"{rbi.name}: missing PCM or TOC block"
        raise SystemExit(msg)

    pcm_path = scratch / "disc.pcm"
    with rbi.open("rb") as src, pcm_path.open("wb") as dst:
        src.seek(pcm_blk.offset)
        remaining = pcm_blk.length
        while remaining:
            chunk = src.read(min(1 << 20, remaining))
            if not chunk:
                break
            dst.write(chunk)
            remaining -= len(chunk)

    raw = rbi.read_bytes()
    disc = parse_toc(raw[toc_blk.offset : toc_blk.offset + toc_blk.length])
    tracks = sorted(disc.tracks, key=lambda t: t.track_number)
    lsns = [t.audio_start_frame for t in tracks]
    total_sectors = pcm_blk.length // _BYTES_PER_SECTOR
    # Track lengths from consecutive INDEX 01 positions; the last runs to the
    # end of the stored PCM.
    ends = [*lsns[1:], total_sectors]
    lengths = [(e - s) * _SAMPLES_PER_SECTOR for s, e in zip(lsns, ends)]
    return Rip(pcm_path=pcm_path, lengths=lengths, sources=[], pregap=lsns[0])


# ── detection ─────────────────────────────────────────────────────────────────


def report(rip: Rip, matches: list[OffsetMatch]) -> None:
    if not matches:
        print("\n  disc is not in AccurateRip — nothing to detect against")
        return
    print(f"\n  {'offset':>8}  {'v1':>7}  {'v2':>7}  {'probe450':>8}  {'conf':>6}")
    for m in matches:
        mark = "  <- confirmed" if m.confirmed else ""
        print(
            f"  {m.offset:>+8d}  {m.tracks_v1:>3d}/{m.total_tracks:<3d}  "
            f"{m.tracks_v2:>3d}/{m.total_tracks:<3d}  {m.tracks_450:>8d}  "
            f"{m.confidence:>6d}{mark}"
        )
    confirmed = [m for m in matches if m.confirmed]
    if not confirmed:
        print("\n  NO OFFSET RECONCILES THIS RIP with AccurateRip.")
        print("  Either the audio is damaged, or this is a pressing the database")
        print("  has never seen. Detection cannot distinguish those two.")
        return
    if len(confirmed) > 1:
        print(
            f"\n  {len(confirmed)} offsets verify: "
            f"{', '.join(f'{m.offset:+d}' for m in confirmed)}"
        )
        print("  This is normal for a widely-submitted album — the same master is")
        print("  pressed at different absolute positions and each is its own")
        print("  AccurateRip cohort. Pick using something outside the audio (the")
        print("  drive's known offset, a CTDB cross-check); confidence ranks them")
        print("  by cohort population, not by correctness.")
    best = confirmed[0]
    print(
        f"\n  best: {best.offset:+d} "
        f"({best.tracks_matched}/{best.total_tracks} tracks, conf {best.confidence})"
    )


def detect(rip: Rip) -> list[OffsetMatch]:
    print(f"  tracks       : {len(rip.lengths)}")
    print(
        f"  total        : {rip.total_samples} samples "
        f"({rip.total_samples // _SAMPLES_PER_SECTOR} sectors)"
    )
    print(f"  track_lsns   : {rip.track_lsns}")
    print(f"  lead-out     : {rip.disc_last_lsn + 1}")
    print(f"  cddb id      : {rip.cddb_id:08x}")
    responses, transport, _ = fetch_ar_responses(
        rip.track_lsns, rip.disc_last_lsn, rip.cddb_id
    )
    print(f"  AccurateRip  : {len(responses)} blocks via {transport}")
    return detect_offset(rip.pcm_path, rip.track_lsns, rip.disc_last_lsn, responses)


# ── repair ────────────────────────────────────────────────────────────────────


def _shifted_slice(
    fh, total_samples: int, start: int, length: int, offset: int
) -> bytes:
    """Track samples [start, start+length) of the stream shifted by *offset*,
    zero-filled where the shifted window falls off either end of the disc."""
    lo, hi = start + offset, start + offset + length
    clipped_lo, clipped_hi = max(0, lo), min(total_samples, hi)
    if clipped_hi <= clipped_lo:
        return bytes(length * _BYTES_PER_SAMPLE)
    fh.seek(clipped_lo * _BYTES_PER_SAMPLE)
    body = fh.read((clipped_hi - clipped_lo) * _BYTES_PER_SAMPLE)
    return (
        bytes((clipped_lo - lo) * _BYTES_PER_SAMPLE)
        + body
        + bytes((hi - clipped_hi) * _BYTES_PER_SAMPLE)
    )


def _write_wav(pcm: bytes, path: Path) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(pcm)


def _write_flac(pcm: bytes, path: Path, metadata: dict[str, str]) -> None:
    from cdda2img.track_extract import _pcm_to_wav_bytes, _wav_bytes_to_flac

    _wav_bytes_to_flac(_pcm_to_wav_bytes(pcm, 44100, 2, 16), path, metadata, 44100)


def _source_metadata(path: Path) -> dict[str, str]:
    try:
        with av.open(str(path)) as c:
            return {k: v for k, v in c.metadata.items() if isinstance(v, str)}
    except Exception:
        return {}


def _output_dir(rip: Rip, offset: int, explicit: Path | None) -> Path:
    if explicit is not None:
        explicit.mkdir(parents=True, exist_ok=True)
        return explicit
    base = rip.sources[0].parent
    for suffix in ("", *(f"_{i}" for i in range(1, 100))):
        candidate = base / f"fixedoffset_{offset:+d}{suffix}"
        if not candidate.exists():
            candidate.mkdir(parents=True)
            return candidate
    msg = "could not find a free output directory name"
    raise SystemExit(msg)


def repair(rip: Rip, offset: int, fmt: str, out_dir: Path | None) -> Path:
    """Rewrite the rip at a corrected offset. Originals are never touched."""
    if not rip.sources:
        msg = ".rbi input is detect-only — see the module docstring"
        raise SystemExit(msg)
    target = _output_dir(rip, offset, out_dir)
    total = rip.total_samples
    start = 0
    with rip.pcm_path.open("rb") as fh:
        for source, length in zip(rip.sources, rip.lengths):
            pcm = _shifted_slice(fh, total, start, length, offset)
            dest = target / f"{source.stem}.{fmt}"
            if fmt == "wav":
                _write_wav(pcm, dest)
            else:
                _write_flac(pcm, dest, _source_metadata(source))
            print(f"  wrote {dest.name}")
            start += length
    return target


# ── CLI ───────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fix_offset.py", description="Detect and repair CD rip sample offsets."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "inputs", nargs="+", type=Path, help="audio files, a directory, or one .rbi"
    )
    common.add_argument(
        "--pregap",
        type=int,
        default=0,
        help="sectors before track 1 (program-area pre-gap)",
    )

    sub.add_parser("detect", parents=[common], help="report candidate offsets")

    ap = sub.add_parser("apply", parents=[common], help="rewrite at a corrected offset")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--offset", type=int, help="offset to correct, in samples")
    group.add_argument(
        "--detect",
        action="store_true",
        help="detect via AccurateRip and use the best confirmed offset",
    )
    ap.add_argument("--format", choices=("flac", "wav"), default="flac")
    ap.add_argument(
        "--out", type=Path, help="output directory (default: alongside the source)"
    )

    args = parser.parse_args(argv)
    scratch = Path(tempfile.mkdtemp(prefix="fixoffset-", dir=resolve_temp_dir()))
    try:
        inputs: list[Path] = args.inputs
        if len(inputs) == 1 and inputs[0].suffix.lower() == ".rbi":
            print(f"source: {inputs[0].name} (RBI container)")
            rip = load_rbi(inputs[0], scratch)
        else:
            print("decoding sources…")
            rip = load_tracks(inputs, scratch, args.pregap)

        if args.command == "detect":
            report(rip, detect(rip))
            return 0

        offset = args.offset
        if offset is None:
            matches = detect(rip)
            report(rip, matches)
            confirmed = [m for m in matches if m.confirmed]
            if not confirmed:
                print("\nrefusing to rewrite: no offset verifies against AccurateRip")
                return 1
            if (
                len(confirmed) > 1
                and confirmed[0].confidence == confirmed[1].confidence
            ):
                print(
                    "\nrefusing to rewrite: the top two offsets are tied — "
                    "choose one explicitly with --offset"
                )
                return 1
            offset = confirmed[0].offset
            print(f"\nusing detected offset {offset:+d}")

        if offset == 0:
            print("offset is 0 — nothing to correct")
            return 0
        target = repair(rip, offset, args.format, args.out)
        print(f"\ncorrected rip written to {target}")
        return 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
