#!/usr/bin/env python3
"""Build a self-contained CTDB fixture directory for an external decoder to verify against.

Distinct from ``tools/ctdb_repair.py``, which *repairs* a rip: this produces
**evidence**, and so it deliberately drops the two gates that make the repair tool
safe and make it useless here:

  * no damage gate — ``ctdb_repair`` exits early when every track already matches
    CTDB, because there is nothing to repair. A clean image is still a valid
    fixture arm: a decoder that finds the offset and returns *zero* corrections has
    made a positive, falsifiable claim.
  * no verification gate — nothing is spliced, so there is no AccurateRip or CRC
    double-gate to pass. The corrections are the artefact, not a means to one.

Everything else is imported from ``ctdb_repair`` rather than reimplemented. A
fixture built by a second implementation stops being evidence about the first.

When a C2 capture is supplied, **both** decodes are run on identical inputs —
error-only and errors-and-erasures — because the interesting quantity is not the
bitmap but what the bitmap *changes*. Two decodes that agree are a real result
(the erasures bought nothing here) and must be reported as one, not quietly
dropped.

``--control-align`` adds the negative control: the same flag population placed at a
deliberately wrong alignment. Without it, the only evidence that the erasures landed
in the right grid positions is that the reported column count looks plausible — and
a coincidence of population reproduces that exactly. Measured on Tracy Chapman,
``erasure_columns`` falls 533 → 30 between the real and the misaligned bitmap while
the flag count is identical, which is what turns the plausible number into evidence.

Usage::

    uv run python tools/ctdb_fixture.py \\
        --pcm /var/tmp/fix/image.pcm --toc 33:17395:…:347208 \\
        --out /var/tmp/fix --name abba

    uv run python tools/ctdb_fixture.py \\
        --pcm pass.pcm --c2 pass.c2 --toc 0:…:162892 --out /var/tmp/fix2 --name tracy

Exit codes: 0 fixture written; 1 operational error; 2 no CTDB entry reconciles.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ctdb_repair as cr

EXIT_OK = 0
EXIT_ERR = 1
EXIT_NO_ENTRY = 2


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def entry_dict(en: cr.Entry) -> dict:
    d = asdict(en)
    d["stride_internal"] = en.stride * 2
    d["parity_bytes"] = en.npar * en.stride * 2 * 2
    return d


def build_erasures(c2_path: Path, pcm_bytes: int, align: int, dest: Path) -> int:
    """Write the packed per-word erasure bitmap; return the number of flagged words.

    The count is the acceptance check. An all-zero bitmap exercises the
    errors-and-erasures path exactly as far as no bitmap at all, and it looks
    identical in a directory listing.
    """
    nwords = pcm_bytes // 2
    packed = cr.build_erasure_bitmap(c2_path, nwords, align)
    dest.write_bytes(packed)
    return sum(bin(b).count("1") for b in packed)


def analyse_arm(
    args: argparse.Namespace,
    en: cr.Entry,
    disc: cr.Disc,
    pcm_path: Path,
    erasures: Path | None,
    control: Path | None = None,
) -> dict:
    """Fetch parity for *en* and decode: error-only, with erasures, and the control.

    The *control* run uses a bitmap with the same flag population placed at a
    deliberately wrong alignment. Without it, an erasure arm's only evidence that the
    flags landed in the right grid positions is that the column count looks plausible
    — which a coincidence of population reproduces exactly.
    """
    out = Path(args.out)
    tag = f"npar{en.npar}_entry{en.id}"
    parity = cr.fetch_parity(en, out / f"parity_{tag}.bin")

    record: dict = {"entry": entry_dict(en), "parity_file": parity.name, "runs": {}}
    plan: list[tuple[str, Path | None]] = [("error_only", None)]
    if erasures is not None:
        plan.append(("erasures", erasures))
    if control is not None:
        plan.append(("erasures_misaligned", control))

    for label, eras in plan:
        result = cr.run_ctanalyse(args.ctanalyse, pcm_path, parity, en, disc, eras)
        dest = out / f"ctanalyse_{tag}_{label}.json"
        dest.write_text(json.dumps(result, indent=1))
        record["runs"][label] = {
            "file": dest.name,
            "can_recover": result.get("can_recover"),
            "offset": result.get("offset"),
            "corrections": len(result.get("corrections", [])),
            "erasure_columns": result.get("erasure_columns", 0),
            "affected_sectors": len(result.get("affected_sectors", [])),
        }
        print(f"  {label}: {record['runs'][label]}")
    return record


def choose_entries(args: argparse.Namespace, entries: list[cr.Entry]) -> list[cr.Entry]:
    if not args.entry:
        return entries
    by_id = {e.id: e for e in entries}
    missing = [i for i in args.entry if i not in by_id]
    if missing:
        msg = f"--entry not present in the lookup: {', '.join(missing)}"
        raise SystemExit(msg)
    return [by_id[i] for i in args.entry]


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pcm", type=Path, required=True, help="whole-image s16le PCM")
    ap.add_argument("--toc", required=True, help="colon TOC L0:L1:…:LEADOUT")
    ap.add_argument("--out", type=Path, required=True, help="fixture directory")
    ap.add_argument("--name", required=True, help="basename prefix for the fixture")
    ap.add_argument("--c2", type=Path, help="matching AccuDisc C2 capture")
    ap.add_argument("--c2-align", type=int, default=-2, help="C2/audio offset, pairs")
    ap.add_argument(
        "--control-align",
        type=int,
        help="also build a deliberately misaligned bitmap at this align and decode "
        "with it — the negative control for 'the erasures landed in the right place'",
    )
    ap.add_argument("--entry", action="append", help="restrict to this CTDB entry id")
    ap.add_argument(
        "--sweep",
        type=int,
        default=cr._SWEEP_WINDOW,
        help="offset sweep half-window in stereo samples",
    )
    ap.add_argument("--xml", type=Path, help="cached CTDB lookup XML")
    ap.add_argument("--ctanalyse", default="ctanalyse", help="reference binary")
    return ap.parse_args()


def main() -> int:
    args = _parse_args()
    cr._SWEEP_WINDOW = args.sweep
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    disc = cr.disc_from_toc(args.toc)
    pcm = args.pcm.read_bytes()
    want = disc.bounds[-1] * cr._FRAME
    if len(pcm) != want:
        print(f"pcm size {len(pcm)} != [0, lead-out) size {want}", file=sys.stderr)
        return EXIT_ERR
    image_sectors = disc.bounds[-1] - disc.bounds[0]
    print(
        f"disc: {disc.n_tracks} tracks, bounds[0]={disc.bounds[0]}, "
        f"lead-out {disc.bounds[-1]}, cddb {disc.cddb_id:08x}\n"
        f"pcm window [0, {disc.bounds[-1]}) = {len(pcm) // cr._FRAME} sectors; "
        f"ctdb image window [{disc.bounds[0]}, {disc.bounds[-1]}) = {image_sectors}"
    )

    entries = cr.load_entries(args.xml, disc)
    print(f"lookup: {len(entries)} usable entries")
    (out / "entries.json").write_text(
        json.dumps([entry_dict(e) for e in entries], indent=1)
    )
    if not entries:
        return EXIT_NO_ENTRY

    sel = cr.select_entry(pcm, entries, disc)
    if sel is None:
        print(f"no entry reconciles within ±{args.sweep} samples", file=sys.stderr)
        return EXIT_NO_ENTRY

    erasures = None
    control = None
    flagged = 0
    if args.c2 is not None:
        dest = out / f"{args.name}_erasures.bin"
        flagged = build_erasures(args.c2, len(pcm), args.c2_align, dest)
        print(
            f"erasures: {dest.name}, {flagged} words flagged of {len(pcm) // 2} "
            f"(align_pairs={args.c2_align})"
        )
        if flagged == 0:
            print("  WARNING: bitmap is all zeros — the erasure arm is vacuous")
        erasures = dest
        if args.control_align is not None:
            control = out / f"{args.name}_erasures_misaligned.bin"
            n = build_erasures(args.c2, len(pcm), args.control_align, control)
            print(
                f"control: {control.name}, {n} words flagged "
                f"(align_pairs={args.control_align})"
            )

    arms: list[dict] = []
    for en in choose_entries(args, entries):
        print(f"arm: entry {en.id} npar={en.npar} conf={en.confidence}")
        arms.append(analyse_arm(args, en, disc, args.pcm, erasures, control))

    summary = {
        "toc": disc.toc,
        "bounds": disc.bounds,
        "pcm_file": args.pcm.name,
        "pcm_sectors": len(pcm) // cr._FRAME,
        "image_sectors": image_sectors,
        "selection": {
            "entry_id": sel.entry.id,
            "offset_samples": sel.offset,
            "damaged_tracks": sel.damaged,
            "unverifiable_tracks": sel.unverifiable,
            "sweep_window": args.sweep,
        },
        "erasures": (
            None
            if erasures is None
            else {
                "file": erasures.name,
                "c2_file": args.c2.name if args.c2 else None,
                "align_pairs": args.c2_align,
                "words_flagged": flagged,
                "words_total": len(pcm) // 2,
                "derived_by": "tools/ctdb_repair.py:build_erasure_bitmap",
                "control_file": control.name if control else None,
                "control_align_pairs": args.control_align,
            }
        ),
        "arms": arms,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    sums = {p.name: sha256_file(p) for p in sorted(out.iterdir()) if p.is_file()}
    (out / "sha256.json").write_text(json.dumps(sums, indent=1))
    print(f"fixture written to {out} ({len(sums)} files)")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
