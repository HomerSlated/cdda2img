"""AR-verify helper: check a re-ripped single track against the AccurateRip DB,
using the production verify_rip path so the checksum/offset/boundary math is
identical to the pipeline.

Strategy:
  1. Extract the disc PCM from the .rbi (already offset-corrected) to /var/tmp.
  2. Reconstruct track_lsns / disc_last_lsn / cddb_id from the embedded TOC.
  3. SELF-TEST: verify_rip on the unmodified disc PCM and print per-track CRCs.
     These MUST match the .rbi's own rip-log CRCs (the disc already holds the
     spliced cd-paranoia track 8 + fixed track 11). If they do, the
     reconstruction is proven correct and step 4 is trustworthy.
  4. If a replacement WAV is given: splice its PCM over the target track's byte
     range (with the pipeline's length guard) and verify_rip again, reporting
     that track. read_offset=0 throughout — both the .rbi PCM and a `-O 30`
     cd-paranoia rip are already offset-corrected.

Usage:
  ar_verify_track.py <file.rbi> [<replacement.wav> <track_num>]
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from cdda2img import rbi_format as R
from cdda2img.accuraterip import verify_rip
from cdda2img.cddb import compute_cddb_disc_id
from cdda2img.container import read_header, wav_to_raw_pcm
from cdda2img.toc_parser import parse_toc

_BYTES_PER_SECTOR = 2352
# /var/tmp, not /tmp: /tmp is RAM-backed tmpfs and a full-disc PCM floods it.
_VAR = Path("/var/tmp")  # noqa: S108


def _reconstruct(rbi: Path, pcm_size: int):
    # track_lsns are INDEX 01 (audio start) = start_frame + pregap_frames, which
    # is what the rip path uses for AR (verified against the rip log's Start
    # column). disc_last_lsn comes straight off the PCM: it spans LSN 0..leadout-1
    # contiguously, so sectors = pcm_size // 2352 and disc_last_lsn = sectors - 1.
    hdr = read_header(rbi)
    raw = rbi.read_bytes()
    toc_blk = hdr.find_block(R.BLOCK_TYPE_TOC)
    if toc_blk is None:
        msg = "RBI has no TOC block"
        raise SystemExit(msg)
    d = parse_toc(raw[toc_blk.offset : toc_blk.offset + toc_blk.length])
    tr = sorted(d.tracks, key=lambda t: t.track_number)
    track_lsns = [t.audio_start_frame for t in tr]
    disc_last_lsn = pcm_size // _BYTES_PER_SECTOR - 1
    cddb_id = int(compute_cddb_disc_id(track_lsns, disc_last_lsn), 16)
    return track_lsns, disc_last_lsn, cddb_id


def _extract_pcm(rbi: Path, out: Path) -> None:
    hdr = read_header(rbi)
    blk = hdr.find_block(R.BLOCK_TYPE_PCM)
    if blk is None:
        msg = "RBI has no PCM block"
        raise SystemExit(msg)
    with rbi.open("rb") as f, out.open("wb") as g:
        f.seek(blk.offset)
        remaining = blk.length
        while remaining:
            chunk = f.read(min(1 << 20, remaining))
            if not chunk:
                break
            g.write(chunk)
            remaining -= len(chunk)


def _report(tag: str, res, focus: int | None = None) -> None:
    print(f"\n== {tag} ==")
    for r in res.tracks:
        c1 = r.confidence_v1
        c2 = r.confidence_v2
        ok = (c1 is not None) or (c2 is not None)
        if r.max_confidence is None:
            status = "DISC NOT IN DB"
        elif ok:
            status = f"OK (v1 conf {c1}, v2 conf {c2})"
        else:
            status = f"MISMATCH (max {r.max_confidence})"
        star = " <<<" if focus == r.track else ""
        print(f"  Track {r.track:2d}: v1={r.v1_crc} v2={r.v2_crc}  {status}{star}")
    print(f"  transport={res.transport}")


def main(argv: list[str]) -> int:
    rbi = Path(argv[1])
    disc_pcm = _VAR / "ar_verify_disc.pcm"
    print(f"rbi          : {rbi.name}")
    print(f"extracting disc PCM -> {disc_pcm} …")
    _extract_pcm(rbi, disc_pcm)
    pcm_size = disc_pcm.stat().st_size
    print(f"  {pcm_size} bytes ({pcm_size // _BYTES_PER_SECTOR} sectors)")

    track_lsns, disc_last_lsn, cddb_id = _reconstruct(rbi, pcm_size)
    print(f"track_lsns   : {track_lsns}")
    print(f"disc_last_lsn: {disc_last_lsn}  (leadout {disc_last_lsn + 1})")
    print(f"cddb_id      : {cddb_id:08x}")

    base = verify_rip(
        disc_pcm, track_lsns, disc_last_lsn, read_offset=0, cddb_id=cddb_id
    )
    _report("SELF-TEST (unmodified .rbi PCM — must match the rip log)", base)

    if len(argv) >= 4:
        wav = Path(argv[2])
        tnum = int(argv[3])
        idx = tnum - 1
        byte_start = track_lsns[idx] * _BYTES_PER_SECTOR
        byte_end = (
            track_lsns[idx + 1] * _BYTES_PER_SECTOR
            if idx + 1 < len(track_lsns)
            else (disc_last_lsn + 1) * _BYTES_PER_SECTOR
        )
        expected = byte_end - byte_start

        new_raw_path = _VAR / "ar_verify_track.raw"
        wav_to_raw_pcm(wav, new_raw_path)
        new_raw = new_raw_path.read_bytes()
        print(
            f"\nreplacement  : {wav.name} -> {len(new_raw)} bytes "
            f"(track {tnum} expects {expected})"
        )
        if len(new_raw) != expected:
            print("  !! length mismatch — boundary disagreement, NOT splicing")
            return 1

        spliced = _VAR / "ar_verify_spliced.pcm"
        shutil.copy(disc_pcm, spliced)
        with spliced.open("r+b") as fh:
            fh.seek(byte_start)
            fh.write(new_raw)
        rr = verify_rip(
            spliced, track_lsns, disc_last_lsn, read_offset=0, cddb_id=cddb_id
        )
        _report(f"REPLACEMENT (track {tnum} = {wav.name})", rr, focus=tnum)
        old = next(t for t in base.tracks if t.track == tnum)
        new = next(t for t in rr.tracks if t.track == tnum)
        print(
            f"\n  track {tnum}: .rbi v1={old.v1_crc} -> new v1={new.v1_crc}"
            f"  ({'CHANGED' if old.v1_crc != new.v1_crc else 'identical'})"
        )
        print(f"  match: {'YES' if (new.confidence_v1 or new.confidence_v2) else 'NO'}")
        spliced.unlink(missing_ok=True)
        new_raw_path.unlink(missing_ok=True)

    disc_pcm.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
