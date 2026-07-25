"""toc_parity.py — field-by-field parity: cdrdao read-toc vs AccuDisc subq assembly.

The acceptance gate for the AccuDisc single-pass metadata path (upgrade plan F7):
both engines read the same disc, both results are reduced to the same track
model, and every field is diffed. Green across the disc shelf = permission to
prefer the single-pass path.

Live mode (captures both sides from the drive; ~5 min total):
    uv run python tools/toc_parity.py --device /dev/sr0

Offline mode (re-diff existing captures):
    uv run python tools/toc_parity.py --cdrdao-toc meta.toc \\
        --fulltoc x.fulltoc --sub x.sub [--cdtext x.cdtext]

Exit code: 0 = all fields match (or the only diffs are cdrdao CD-Text mojibake,
where AccuDisc is provably correct), 1 = real differences, 2 = capture failure.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cdda2img.accudisc_reader import read_disc_c2
from cdda2img.subq_toc import build_rip_info
from cdda2img.toc_parser import ParsedDisc, parse_toc


def _capture_live(device: str, workdir: Path) -> tuple[Path, Path, Path, Path]:
    """Run both engines against the drive; returns (toc, fulltoc, sub, cdtext)."""
    fulltoc = workdir / "parity.fulltoc"
    sub = workdir / "parity.sub"
    cdtext = workdir / "parity.cdtext"
    toc = workdir / "parity.toc"

    print(f"[1/2] AccuDisc subchannel capture ({device}) …", flush=True)
    # Metadata-only whole-disc pass: sub + lead-in dumps, no PCM/C2 written (the
    # parity gate only needs the subchannel and the lead-in, not the ~600 MB audio).
    try:
        read_disc_c2(
            device, output_sub=sub, output_fulltoc=fulltoc, output_cdtext=cdtext
        )
    except RuntimeError as exc:  # exit 1/2 — usage or fatal device/transport
        print(exc, file=sys.stderr)
        sys.exit(2)
    if not fulltoc.exists() or not sub.exists():
        sys.exit(2)

    print("[2/2] cdrdao read-toc …", flush=True)
    cmd = ["cdrdao", "read-toc", "--device", device, str(toc)]
    result = subprocess.run(cmd, check=False, capture_output=True)  # noqa: S603
    if result.returncode != 0 or not toc.exists():
        print(result.stderr.decode(errors="replace"))
        sys.exit(2)
    return toc, fulltoc, sub, cdtext


def _is_cdrdao_mojibake(cdrdao: object, subq: object) -> bool:
    """True when cdrdao's string is the Latin-1 misreading of subq's UTF-8 string.

    cdrdao read-toc decodes CD-Text charset-0x00 strictly as ISO-8859-1 per spec.
    When a disc actually carries UTF-8 (every cdrdao/CDEmu-authored disc does --
    the authoring copies the TOC file's raw bytes), each non-ASCII character comes
    back double-encoded (a U+2019 apostrophe, E2 80 99, becomes three Latin-1 junk
    chars). AccuDisc + cdtext.py decode UTF-8-first and are correct, so detecting the
    exact byte relationship lets
    the soak score these as "AccuDisc superior", not as parity failures against a
    reference that is itself wrong on this axis.
    """
    if not isinstance(cdrdao, str) or not isinstance(subq, str):
        return False
    try:
        return subq.encode("utf-8").decode("latin-1") == cdrdao
    except (UnicodeEncodeError, UnicodeDecodeError):
        return False


def _diff(ref: ParsedDisc, info) -> tuple[int, int]:
    """Diff the two track models. Returns (real_differences, cdrdao_mojibake)."""
    differences = 0
    mojibake = 0

    def check(name: str, a: object, b: object, *, text: bool = False) -> None:
        nonlocal differences, mojibake
        if a == b:
            return
        if text and _is_cdrdao_mojibake(a, b):
            mojibake += 1
            print(f"  MOJIBAKE {name}: cdrdao={a!r} is Latin-1 misread; subq={b!r} ✓")
            return
        differences += 1
        print(f"  DIFF {name}: cdrdao={a!r} subq={b!r}")

    disc = info.disc
    check("catalog", ref.catalog, disc.catalog)
    check("cdtext_catalog_ref", ref.disc_id, disc.cdtext_catalog_ref)
    check("album", ref.title, disc.album, text=True)
    check("artist", ref.performer, disc.artist, text=True)
    check("pre_emphasis", ref.pre_emphasis, disc.pre_emphasis)
    check("n_tracks", len(ref.tracks), len(disc.tracks))
    check(
        "track_lsns",
        [t.start_frame + t.pregap_frames for t in ref.tracks],
        info.track_lsns,
    )
    for r, m in zip(ref.tracks, disc.tracks):
        n = r.track_number
        check(f"track{n}.start_frame", r.start_frame, m.start_frame)
        check(f"track{n}.duration", r.duration_frames, m.duration_frames)
        check(f"track{n}.pregap", r.pregap_frames, m.pregap_frames)
        check(f"track{n}.isrc", r.isrc, m.isrc)
        check(f"track{n}.pre_emphasis", r.pre_emphasis, m.pre_emphasis)
        check(f"track{n}.copy", r.copy_permitted, m.copy_permitted)
        check(f"track{n}.index_points", r.index_points, m.index_points)
        check(f"track{n}.title", r.title, m.title, text=True)
        check(f"track{n}.performer", r.performer, m.performer, text=True)
    return differences, mojibake


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--device", default=None, help="live capture from this drive")
    ap.add_argument("--cdrdao-toc", type=Path, help="existing cdrdao read-toc file")
    ap.add_argument(
        "--fulltoc", type=Path, help="existing AccuDisc read --fulltoc dump"
    )
    ap.add_argument("--sub", type=Path, help="existing AccuDisc read --subf capture")
    ap.add_argument("--cdtext", type=Path, help="existing AccuDisc read --cdtext dump")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory(prefix="toc_parity_") as tmp:
        if args.device:
            toc, fulltoc, sub, cdtext = _capture_live(args.device, Path(tmp))
        elif args.cdrdao_toc and args.fulltoc and args.sub:
            toc, fulltoc, sub = args.cdrdao_toc, args.fulltoc, args.sub
            cdtext = args.cdtext or Path(tmp) / "missing.cdtext"
        else:
            ap.error("need --device, or --cdrdao-toc + --fulltoc + --sub")

        ref = parse_toc(toc.read_bytes())
        info = build_rip_info(
            fulltoc.read_bytes(),
            sub.read_bytes(),
            cdtext.read_bytes() if cdtext.exists() else None,
        )
        if info.prov:
            print(f"subq provenance: {info.prov}")
        differences, mojibake = _diff(ref, info)

    moji_note = (
        f" ({mojibake} cdrdao CD-Text mojibake, AccuDisc correct)" if mojibake else ""
    )
    if differences:
        print(f"PARITY: {differences} difference(s){moji_note}")
        return 1
    if mojibake:
        # cdrdao mis-decoded UTF-8 CD-Text; AccuDisc got it right. Not a failure —
        # AccuDisc is strictly superior here, so the gate stays green.
        print(f"PARITY: ALL MATCH{moji_note}")
        return 0
    print("PARITY: ALL MATCH")
    return 0


if __name__ == "__main__":
    sys.exit(main())
