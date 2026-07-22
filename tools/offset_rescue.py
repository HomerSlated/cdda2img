"""offset_rescue.py — identify, verify, and offset-rescue a disc that rips
cleanly but fails AccurateRip on every track.

The target case: a CD-R copied from an original where the read offset, the write
offset, or both were wrong or unapplied (possibly on two different drives). The
copy's TOC matches the original, so the disc ID is right and the AccurateRip
lookup succeeds — but every track's checksum is computed over audio sitting at
an unknown displacement, so nothing matches. The displacement was never
recorded. It is, however, calculable: sweep the checksum window until the
database agrees (``accuraterip.detect_offset``).

Stages, each reported separately so a failure is attributable:

  1. CAPTURE   — one AccuDisc pass: audio + C2 + raw subchannel + full TOC + CD-Text
  2. IDENTIFY  — assemble the TOC from the subchannel, compute disc IDs, ask
                 MusicBrainz and AccurateRip whether they know this disc
  3. VERIFY    — AccurateRip at the drive's configured read offset
  4. RESCUE    — on failure, sweep for an offset at which the disc does verify
  5. CONFIRM   — re-verify at the rescued offset; optionally write corrected PCM

Nothing is written back to the disc or to any container; this is a diagnostic.

Usage:
    uv run python tools/offset_rescue.py --device /dev/sr0 --speed 24
    uv run python tools/offset_rescue.py --reuse            # skip the read
    uv run python tools/offset_rescue.py --reuse --write-pcm rescued.pcm
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cdda2img.accuraterip import (
    ARVerifyResult,
    detect_offset,
    fetch_ar_responses,
    verify_rip,
)
from cdda2img.cddb import compute_cddb_disc_id

_DEFAULT_OUT = Path("/var/tmp/offset_rescue")  # noqa: S108 — disk-backed by design
_SECTOR = 2352


class _Geometry(NamedTuple):
    """The subset of RipInfo this diagnostic needs."""

    track_lsns: list[int]
    disc_last_lsn: int
    disc: object


def banner(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


# ── stage 1: capture ──────────────────────────────────────────────────────────


def capture(device: str, out: Path, speed: int | None) -> dict[str, Path]:
    """One AccuDisc pass. Returns the sidecar paths."""
    from cdda2img.accudisc_reader import park_spindle, read_disc_c2

    paths = {
        "pcm": out / "disc.pcm",
        "c2": out / "disc.c2",
        "sub": out / "disc.sub",
        "fulltoc": out / "disc.fulltoc",
        "cdtext": out / "disc.cdtext",
    }
    last = [0.0]

    def progress(done: int, total: int) -> None:
        now = time.monotonic()
        if now - last[0] < 1.0 and done < total:
            return
        last[0] = now
        pct = 100.0 * done / total if total else 0.0
        print(f"\r  reading {done}/{total} sectors ({pct:5.1f}%)", end="", flush=True)

    print(f"  device {device}" + (f" at {speed}x" if speed else " at max speed"))
    read_disc_c2(
        device,
        paths["pcm"],
        paths["c2"],
        output_sub=paths["sub"],
        output_cdtext=paths["cdtext"],
        # No --fulltoc: format 0x02 fails on this drive (see read_geometry).
        read_speed=speed,
        progress_cb=progress,
    )
    print()
    park_spindle(device)
    return paths


# ── stage 2: identify ─────────────────────────────────────────────────────────


def read_geometry(device: str):
    """Track LSNs and lead-out via ``accudisc toc``.

    ``toc`` prefers READ TOC format 0x02 (lead-in) and degrades to format 0x00
    automatically, reporting which served the answer via ``source=`` /
    ``degrade=``. We parse only the track/leadout lines, which are identical
    either way, so the degrade is transparent here — offset rescue needs
    boundaries and lead-out and nothing else.

    Note that *neither* format carries pre-gaps: format 0x02's descriptors hold
    INDEX 01 (track starts) and A0/A1/A2, never INDEX 00. Pre-gaps live only in
    the program-area Q subchannel, which is why the rip pipeline derives them
    from the ``--sub raw`` capture (``subq_toc._derive_layout``) and not from
    any TOC. Verified on the Stanley Road CD-R, whose lead-in is completely
    unreadable yet yields all 11 pre-gaps from Q.

    Offset rescue needs boundaries and lead-out only, so a degrade is harmless
    here — but it is surfaced, because a dying lead-in is provenance worth
    seeing next to a verification result.
    """
    from cdda2img.accudisc_reader import read_toc

    toc = read_toc(device)
    if toc.degraded:
        safe, why = toc.session_safe
        print(f"  TOC: source={toc.source} degrade={toc.degrade} — {why}")
        if not safe:
            msg = f"refusing degraded TOC: {why}"
            raise SystemExit(msg)
    return toc.track_lsns, toc.disc_last_lsn


def load_rbi(rbi: Path, out: Path) -> tuple[dict[str, Path], list[int], int]:
    """Use a stored .rbi container as the source instead of the drive.

    Two uses: auditing a container you already hold, and testing this harness
    end to end without hardware (extract, shift by a known wrong offset, and see
    whether the sweep puts it back). Geometry comes from the embedded TOC, so
    no drive is touched.
    """
    from cdda2img import rbi_format as R
    from cdda2img.container import read_header
    from cdda2img.toc_parser import parse_toc

    header = read_header(rbi)
    pcm_blk = header.find_block(R.BLOCK_TYPE_PCM)
    toc_blk = header.find_block(R.BLOCK_TYPE_TOC)
    if pcm_blk is None or toc_blk is None:
        msg = f"{rbi.name}: missing PCM or TOC block"
        raise SystemExit(msg)

    out.mkdir(parents=True, exist_ok=True)
    pcm = out / "disc.pcm"
    with rbi.open("rb") as src, pcm.open("wb") as dst:
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
    paths = {kind: out / f"disc.{kind}" for kind in ("pcm", "c2", "sub", "cdtext")}
    return paths, lsns, pcm_blk.length // _SECTOR - 1


def _minimal_disc(lsns: list[int], disc_last_lsn: int, paths: dict[str, Path]):
    """An RBIDisc carrying just enough for the disc-ID and lookup helpers,
    enriched with whatever the capture's subchannel and CD-Text yielded."""
    from cdda2img.rbi_format import RBIDisc, RBITocEntry
    from cdda2img.subchannel import scan_subcode
    from cdda2img.subq_toc import _first_cdtext_block, _voted_isrcs, _voted_mcn

    mcn: str | None = None
    isrcs: dict[int, str] = {}
    if paths["sub"].exists():
        scan = scan_subcode(paths["sub"].read_bytes(), leadout_lba=disc_last_lsn + 1)
        mcn = _voted_mcn(scan)
        isrcs = _voted_isrcs(scan, {})

    album = artist = ""
    block = _first_cdtext_block(
        paths["cdtext"].read_bytes() if paths["cdtext"].exists() else None
    )
    if block is not None:
        album = block.album_title or ""
        artist = block.album_performer or ""

    ends = [*lsns[1:], disc_last_lsn + 1]
    tracks = [
        RBITocEntry(
            track_number=i + 1,
            title=(block.track_title(i + 1) if block else "") or "",
            performer="",
            start_frame=start,
            duration_frames=end - start,
            isrc=isrcs.get(i + 1),
        )
        for i, (start, end) in enumerate(zip(lsns, ends))
    ]
    return RBIDisc(album=album, artist=artist, tracks=tracks, catalog=mcn)


def identify(
    device: str,
    paths: dict[str, Path],
    pcm_size: int,
    geometry: tuple[list[int], int] | None = None,
):
    """Assemble geometry from the disc and ask the databases who this is.

    *geometry* overrides the drive read (used for .rbi input)."""
    from cdda2img.mb_lookup import disc_id_from_rbi, prepopulate_from_mb

    track_lsns, disc_last_lsn = geometry or read_geometry(device)
    disc = _minimal_disc(track_lsns, disc_last_lsn, paths)
    info = _Geometry(track_lsns, disc_last_lsn, disc)
    sectors = pcm_size // _SECTOR
    print(f"  tracks          : {len(info.track_lsns)}")
    print(f"  PCM             : {pcm_size} bytes ({sectors} sectors)")
    print(f"  track_lsns      : {info.track_lsns}")
    print(
        f"  disc_last_lsn   : {info.disc_last_lsn} (lead-out {info.disc_last_lsn + 1})"
    )
    if sectors - 1 != info.disc_last_lsn:
        print(
            f"  ! PCM sector count disagrees with the TOC lead-out "
            f"({sectors - 1} vs {info.disc_last_lsn}) — geometry is suspect"
        )
    print(f"  MCN             : {disc.catalog or '-'}")
    isrcs = sum(1 for t in disc.tracks if t.isrc)
    print(f"  ISRCs           : {isrcs}/{len(disc.tracks)}")
    print(f"  CD-Text         : {disc.album or '-'} / {disc.artist or '-'}")

    cddb_id = int(compute_cddb_disc_id(info.track_lsns, info.disc_last_lsn), 16)
    mb_id = disc_id_from_rbi(disc)
    print(f"  CDDB disc id    : {cddb_id:08x}")
    print(f"  MB disc id      : {mb_id}")

    print("\n  MusicBrainz lookup…")
    try:
        mb = prepopulate_from_mb(disc, verbose=False)
        if mb.disc.album:
            print(f"    MATCH: {mb.disc.artist or '?'} — {mb.disc.album}")
            print(
                f"    release: {mb.disc.mb_release_id}  "
                f"({mb.disc.release_date or 'no date'})"
            )
        else:
            print("    no MusicBrainz match for this disc ID")
    except Exception as exc:
        print(f"    lookup failed: {exc}")

    return info, cddb_id


# ── stages 3 & 5: verify ──────────────────────────────────────────────────────


def verify(pcm: Path, info, cddb_id: int, offset: int) -> ARVerifyResult:
    result = verify_rip(pcm, info.track_lsns, info.disc_last_lsn, offset, cddb_id)
    in_db = [r for r in result.tracks if r.max_confidence is not None]
    if not in_db:
        print(f"  disc is NOT in AccurateRip (transport={result.transport})")
        return result
    ok = 0
    for r in result.tracks:
        good = r.confidence_v1 is not None or r.confidence_v2 is not None
        ok += good
        state = (
            f"OK   v1={r.confidence_v1} v2={r.confidence_v2}"
            if good
            else (
                "DAMAGED (crc450 matches — right pressing, corrupt bytes)"
                if r.confidence_450
                else "MISMATCH"
            )
        )
        print(f"    track {r.track:2d}  v1={r.v1_crc} v2={r.v2_crc}  {state}")
    print(f"  --> {ok}/{len(in_db)} tracks verified at read_offset={offset:+d}")
    return result


def _outcome(result: ARVerifyResult) -> tuple[int, int]:
    in_db = [r for r in result.tracks if r.max_confidence is not None]
    ok = sum(
        1 for r in in_db if r.confidence_v1 is not None or r.confidence_v2 is not None
    )
    return ok, len(in_db)


# ── stage 4: rescue ───────────────────────────────────────────────────────────


def _describe_reference(responses: list[list[dict]]) -> None:
    """How good is the evidence we are sweeping against? A reference is only as
    trustworthy as its corroboration, and a block with no frame-450 data makes
    the cheap prefilter inert (detection still works, via full-track checksums,
    but slower)."""
    weak = 0
    for i, resp in enumerate(responses):
        confs = [e["conf"] for e in resp]
        n450 = sum(1 for e in resp if e["crc450"])
        top = max(confs)
        weak += top <= 1
        print(
            f"    block {i}: confidence {min(confs)}-{top}, "
            f"frame-450 data on {n450}/{len(resp)} tracks"
        )
    if weak == len(responses):
        print("    ! every block is a SINGLE uncorroborated submission (confidence 1).")
        print("      Matching one would not constitute verification.")


def rescue(pcm: Path, info, cddb_id: int, configured: int, radius: int):
    responses, transport, _ = fetch_ar_responses(
        info.track_lsns, info.disc_last_lsn, cddb_id
    )
    print(f"  AccurateRip: {len(responses)} blocks via {transport}")
    if not responses:
        print("  nothing to sweep against — disc not in the database")
        return None
    _describe_reference(responses)
    t0 = time.monotonic()
    matches = detect_offset(
        pcm, info.track_lsns, info.disc_last_lsn, responses, radius=radius
    )
    print(f"\n  swept +/-{radius} samples in {time.monotonic() - t0:.1f}s\n")
    print(f"  {'offset':>8}  {'v1':>8}  {'v2':>8}  {'probe450':>8}  {'conf':>6}")
    for m in matches:
        mark = "  <- verifies" if m.confirmed else ""
        print(
            f"  {m.offset:>+8d}  {m.tracks_v1:>3d}/{m.total_tracks:<4d}  "
            f"{m.tracks_v2:>3d}/{m.total_tracks:<4d}  {m.tracks_450:>8d}  "
            f"{m.confidence:>6d}{mark}"
        )

    confirmed = [m for m in matches if m.confirmed]
    if not confirmed:
        print("\n  NO offset reconciles this rip with AccurateRip.")
        print("  Distinguish the two causes before concluding:")
        print(f"   - displacement beyond the swept +/-{radius}: retry with a larger")
        print("     --radius. Only track 1 and the last track carry boundary")
        print("     exclusions, so INTERIOR tracks can legitimately match at any")
        print("     offset — a wider sweep is meaningful, not a fishing trip.")
        print("   - the audio simply is not the recording the database holds. If a")
        print("     wide sweep produces no hit on ANY track, that is the answer.")
        return None

    best = confirmed[0]
    if len(confirmed) > 1 and confirmed[1].confidence == best.confidence:
        print("\n  AMBIGUOUS: the top two offsets tie on cohort confidence.")
        print("  Not auto-selecting — rerun with --offset to force one.")
        return None
    print(
        f"\n  RESCUED at offset {best.offset:+d} "
        f"({best.tracks_matched}/{best.total_tracks} tracks, conf {best.confidence})"
    )
    delta = best.offset - configured
    print(f"  configured read offset is {configured:+d}, so this disc's audio sits")
    print(f"  {delta:+d} samples from where a correctly-made copy would put it.")
    print("  That figure is a composite: the source ripper's read-offset error plus")
    print("  the burner's write-offset error, not any single drive's offset.")
    return best


# ── main ──────────────────────────────────────────────────────────────────────


def _acquire(args, device: str, paths: dict[str, Path]):
    """Stage 1: get PCM (and geometry, for .rbi input) from disc, cache or file."""
    geometry: tuple[list[int], int] | None = None

    banner("STAGE 1 — CAPTURE")
    if args.from_rbi is not None:
        print(f"  source: {args.from_rbi.name} (stored container, no disc read)")
        paths, lsns, last_lsn = load_rbi(args.from_rbi, args.out)
        geometry = (lsns, last_lsn)
    elif args.reuse and paths["pcm"].exists():
        print(f"  reusing capture in {args.out}")
    else:
        paths = capture(device, args.out, args.speed)
        print(f"  captured to {args.out}")

    if args.misalign:
        from cdda2img.offset_correct import apply_offset

        print(
            f"\n  --misalign {args.misalign:+d}: displacing the source PCM to "
            "simulate a rip made at the wrong offset."
        )
        print(f"  A working sweep must therefore report {-args.misalign:+d}.")
        apply_offset(paths["pcm"], args.misalign)

    return paths, geometry


def _resolve_offset(args, device: str, cfg) -> int:
    """The offset stage 3 verifies at, before any rescue."""
    if args.read_offset is not None:
        return args.read_offset
    if args.from_rbi is not None:
        # Stored container PCM was offset-corrected once, at storage.
        print("  stored PCM is already offset-corrected — verifying at 0")
        return 0
    from cdda2img.drive_info import probe_drive_name

    name = probe_drive_name(device)
    match = next((d for d in cfg.drives if d.name == name), None)
    offset = match.read_offset if match else 0
    print(f"  drive {name!r}: configured read offset {offset:+d}")
    return offset


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="offset_rescue.py", description=__doc__)
    ap.add_argument(
        "--device", default=None, help="drive (default: config default_device)"
    )
    ap.add_argument("--speed", type=int, default=24, help="read speed (default 24)")
    ap.add_argument("--out", type=Path, default=_DEFAULT_OUT, help="capture directory")
    ap.add_argument("--reuse", action="store_true", help="reuse an existing capture")
    ap.add_argument(
        "--read-offset",
        type=int,
        default=None,
        help="override the configured drive read offset",
    )
    ap.add_argument(
        "--offset", type=int, default=None, help="skip the sweep and force this offset"
    )
    ap.add_argument(
        "--radius",
        type=int,
        default=2939,
        help="sweep half-width in samples (default 2939 = AccurateRip's own limit; "
        "larger is valid for interior tracks, which have no boundary exclusion)",
    )
    ap.add_argument(
        "--write-pcm",
        type=Path,
        default=None,
        help="write the rescued (offset-corrected) PCM here",
    )
    ap.add_argument(
        "--from-rbi",
        type=Path,
        default=None,
        help="use a stored .rbi as the source instead of reading a disc",
    )
    ap.add_argument(
        "--misalign",
        type=int,
        default=None,
        help="displace the source PCM by N samples before verifying — simulates a "
        "rip made at the wrong offset, to test whether the sweep recovers it",
    )
    args = ap.parse_args(argv)

    from cdda2img.config import load_config

    cfg = load_config()
    device = args.device or cfg.default_device
    args.out.mkdir(parents=True, exist_ok=True)

    paths = {
        kind: args.out / f"disc.{kind}"
        for kind in ("pcm", "c2", "sub", "fulltoc", "cdtext")
    }

    paths, geometry = _acquire(args, device, paths)
    read_offset = _resolve_offset(args, device, cfg)

    banner("STAGE 2 — IDENTIFY")
    info, cddb_id = identify(device, paths, paths["pcm"].stat().st_size, geometry)

    banner(f"STAGE 3 — VERIFY at the configured offset ({read_offset:+d})")
    before = verify(paths["pcm"], info, cddb_id, read_offset)
    ok, total = _outcome(before)
    if total and ok == total:
        print("\n  Disc already verifies. No rescue needed.")
        return 0
    if total and ok:
        print(f"\n  PARTIAL mismatch ({ok}/{total}) — that is damage, not an offset")
        print("  problem. A sweep may still find a better alignment; continuing.")

    banner("STAGE 4 — RESCUE")
    if args.offset is not None:
        print(f"  forced offset {args.offset:+d}")
        best_offset = args.offset
    else:
        best = rescue(paths["pcm"], info, cddb_id, read_offset, args.radius)
        if best is None:
            return 1
        best_offset = best.offset

    banner(f"STAGE 5 — CONFIRM at the rescued offset ({best_offset:+d})")
    after = verify(paths["pcm"], info, cddb_id, best_offset)
    ok_after, total_after = _outcome(after)
    print(f"\n  before: {ok}/{total} tracks    after: {ok_after}/{total_after} tracks")

    if args.write_pcm:
        import shutil

        from cdda2img.offset_correct import apply_offset

        print(f"\n  writing rescued PCM to {args.write_pcm}")
        shutil.copyfile(paths["pcm"], args.write_pcm)
        apply_offset(args.write_pcm, best_offset)
        print("  the copy is now in the reference alignment: re-verifying at 0 …")
        verify(args.write_pcm, info, cddb_id, 0)

    return 0 if ok_after == total_after and total_after else 1


if __name__ == "__main__":
    raise SystemExit(main())
