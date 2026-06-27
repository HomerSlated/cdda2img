"""Live resolver-trail harness: drive _run_metadata_lookups on a real foreign
image and dump the collect->resolve trust resolver's reasoning.

NOT a test — a one-shot introspection of the B-4/B-6 resolver on real MB/Discogs
lookup data, using the _shadow_out seam that already exists for the equivalence
proof. Skips the container build (serialization is covered by the suite).

Asserts the two INTENTIONAL behaviours of the B4 arc, not a vague "it ran":
  (1) B-6 fires IFF MB and Discogs both carry catalogue fields AND disagree:
      committed takes Discogs, legacy oracle took MB.
  (2) dropped-B2 invariant: if the disc has CD-Text, committed album/artist/title
      stay equal to the on-disc baseline (CD-Text baseline NOT lowered).
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

from cdda2img.config import load_config
from cdda2img.nrg_reader import import_nrg


def _fmt(v: object) -> str:
    return "∅" if v in (None, "") else repr(v)


def main(nrg: Path) -> int:  # noqa: C901  # linear introspection dump, not branchy logic
    # /var/tmp, not /tmp: /tmp is RAM-backed tmpfs and a full-disc PCM floods it.
    pcm_out = Path("/var/tmp") / "resolver_trail_pcm.raw"  # noqa: S108
    print(f"== parsing {nrg.name} -> {pcm_out} ==")
    disc, _flag = import_nrg(nrg, pcm_out)
    has_cdtext = bool(disc.album or disc.artist)
    base_album, base_artist = disc.album, disc.artist
    print(
        f"  tracks={len(disc.tracks)} mcn={_fmt(disc.cdtext_catalog_ref)} "
        f"album={_fmt(disc.album)} artist={_fmt(disc.artist)}"
    )

    cfg = load_config()
    from cdda2img.cdda2img import _run_metadata_lookups

    shadow: dict[str, object] = {}
    provenance: dict[str, str] = {}
    print("== running live lookups (MB disc-ID + Discogs + stage-7) ==")
    committed, _mb = _run_metadata_lookups(
        disc,
        pcm_out,
        provenance,
        do_cddb=False,
        cddb_track_lsns=None,
        cddb_disc_last_lsn=None,
        cddb_server=None,
        cddb_verbose=False,
        mb_verbose=False,
        preferred_country=cfg.preferred_country or [],
        ui=None,
        _shadow_out=shadow,
    )

    proposals = shadow["proposals"]
    legacy = shadow["merged"]

    # --- per-field proposal trail (disc-level only; tracks are voluminous) ---
    print("\n== DISC-LEVEL PROPOSAL TRAIL (highest trust wins) ==")
    by_field: dict[str, list] = {}
    for p in proposals:  # type: ignore[union-attr]
        if p.track_number is not None:
            continue
        by_field.setdefault(p.field.name, []).append(p)
    for fname, ps in sorted(by_field.items()):
        ps_sorted = sorted(ps, key=lambda p: int(p.trust), reverse=True)
        winner = ps_sorted[0]
        contested = len({p.value for p in ps}) > 1
        tag = "  <-- CONTESTED" if contested else ""
        print(f"\n  {fname}:{tag}")
        for p in ps_sorted:
            mark = "*" if p is winner else " "
            print(
                f"   {mark} {p.source.name:<12} trust={int(p.trust):>3} "
                f"({p.trust.name:<9}) = {_fmt(p.value)}"
            )

    # --- catalogue contention check (the B-6 stimulus) ---
    print("\n== B-6 CATALOGUE CONTENTION (the only intentional divergence) ==")
    fired = []
    for fname in ("CATALOG_NUMBER", "LABEL"):
        ps = by_field.get(fname, [])
        srcs = {p.source.name: p.value for p in ps}
        mb_val = next((p.value for p in ps if p.source.name == "MB_DISC_ID"), None)
        dg_val = next((p.value for p in ps if p.source.name == "DISCOGS"), None)
        committed_val = getattr(committed, fname.lower())
        legacy_val = getattr(legacy, fname.lower())
        contested = mb_val is not None and dg_val is not None and mb_val != dg_val
        print(
            f"  {fname}: MB={_fmt(mb_val)} Discogs={_fmt(dg_val)} sources={list(srcs)}"
        )
        if contested:
            ok = committed_val == dg_val and legacy_val == mb_val
            fired.append((fname, ok, committed_val, legacy_val))
            print(
                f"    CONTESTED -> committed={_fmt(committed_val)} "
                f"legacy={_fmt(legacy_val)}  B-6 {'OK' if ok else 'FAIL'}"
            )
        else:
            print("    not contested (B-6 dormant — wrong stimulus, not a fail)")

    # --- dropped-B2 invariant: CD-Text baseline preserved ---
    print("\n== DROPPED-B2 INVARIANT (CD-Text baseline must survive) ==")
    if has_cdtext:
        b2_ok = committed.album == base_album and committed.artist == base_artist
        print(f"  baseline album={_fmt(base_album)} artist={_fmt(base_artist)}")
        print(
            f"  committed album={_fmt(committed.album)} artist={_fmt(committed.artist)}"
        )
        print(
            f"  -> {'OK (baseline survived)' if b2_ok else 'FAIL (baseline lowered!)'}"
        )
    else:
        b2_ok = True
        print("  no CD-Text on this disc — invariant vacuous")

    # --- committed vs legacy oracle: full disc-field diff ---
    print("\n== COMMITTED vs LEGACY ORACLE (disc fields) ==")
    diffs = 0
    for f in dataclasses.fields(committed):
        if f.name == "tracks":
            continue
        cv, lv = getattr(committed, f.name), getattr(legacy, f.name)
        if cv != lv:
            diffs += 1
            print(f"  {f.name}: committed={_fmt(cv)} legacy={_fmt(lv)}")
    if diffs == 0:
        print("  (identical — resolver reproduced legacy on every disc field)")

    print("\n== VERDICT ==")
    print(f"  B-6 contested fields: {[f[0] for f in fired] or 'none (dormant)'}")
    if fired:
        print(f"  B-6 correct: {all(f[1] for f in fired)}")
    print(f"  dropped-B2 invariant: {'OK' if b2_ok else 'FAIL'}")
    print(
        f"  resolver/legacy disc-field divergences: {diffs} "
        f"(expected: only B-6 catalogue)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
