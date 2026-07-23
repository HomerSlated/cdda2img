#!/usr/bin/env python3
"""Triage a freshly-loaded disc: is it worth benching, and with which target?

One whole-disc C2 capture answers everything the recovery bench needs to know before
it starts, and costs a single spin-up:

  * geometry + AccurateRip resolvability -- **verified, not assumed**. Discs with a
    program-area pre-gap on track 1 (ABBA Gold, Sheryl Crow: `track 1 lba 33 pregap
    33`) are the trap: the as-reported LSN is the correct disc-ID input, but a wrong
    convention yields a well-formed URL for a disc that does not exist and AccurateRip
    answers 404 -- byte-identical to "not in the database". So when AR returns nothing
    this re-checks with track 1 forced to 0 and reports which convention resolves,
    rather than declaring the disc absent.
  * the live C2-flagged set and which tracks fail AR -- the recovery targets.
  * a verdict: clean control (nothing to recover), or benchable with a suggested
    target track.

Ordering matters: the flagged set MUST be found in-session. On Tracy the flagged LBAs
observed an hour apart had zero overlap, and a battery swept over the stale list scored
1.00 everywhere because nothing in it was mis-reading any more.

Usage (from project root):
    TMPDIR=/var/tmp uv run python tools/disc_triage.py --device /dev/sr0
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

_SECTOR_BYTES = 2352
_C2_BYTES = 294


def _verify_track(
    pcm: bytes,
    track: int,
    track_lsns: list[int],
    leadout: int,
    read_offset: int,
    responses: list[list[dict]],
) -> tuple[str, str, int | None]:
    """(v1, v2, best confidence or None). Mirrors accuraterip.verify_rip's window
    arithmetic — clamp AND zero-pad, or a positive read offset silently shortens
    track 1 / the last track and shifts the checksum's exclusion boundary."""
    from cdda2img.accuraterip import match_track_pcm

    idx = track - 1
    byte_start = track_lsns[idx] * _SECTOR_BYTES + read_offset * 4
    end_lsn = track_lsns[idx + 1] if idx + 1 < len(track_lsns) else leadout
    byte_end = end_lsn * _SECTOR_BYTES + read_offset * 4

    raw = pcm[max(0, byte_start) : min(len(pcm), byte_end)]
    if byte_start < 0:
        raw = bytes(-byte_start) + raw
    if byte_end > len(pcm):
        raw = raw + bytes(byte_end - len(pcm))

    v1, v2, c1, c2 = match_track_pcm(raw, track, len(track_lsns), responses)
    conf = c1 if c1 is not None else c2
    return v1, v2, conf


def resolve_ar(track_lsns: list[int], disc_last_lsn: int):
    """Fetch AR, trying the pregap-stripped track-1 convention if the reported one
    finds nothing. Returns (responses, cddb_hex, convention)."""
    from cdda2img.accuraterip import fetch_ar_responses
    from cdda2img.cddb import compute_cddb_disc_id

    variants = [("as-reported", track_lsns)]
    if track_lsns and track_lsns[0] != 0:
        variants.append(("pregap-stripped", [0, *track_lsns[1:]]))

    for label, lsns in variants:
        cddb_hex = compute_cddb_disc_id(lsns, disc_last_lsn)
        responses, _t, _b = fetch_ar_responses(lsns, disc_last_lsn, int(cddb_hex, 16))
        if responses:
            return responses, cddb_hex, label
    return [], compute_cddb_disc_id(track_lsns, disc_last_lsn), "none resolved"


def _profile(n: int) -> str:
    """Suggested recovery strategy for a track with ``n`` C2-flagged sectors.

    Aggregate over every target benched so far (5 targets, 4 discs, one drive):

        track-ladder    13/14  0.93   never zeroed on ANY target
        track-constant  10/13  0.77
        max-variation   10/14  0.71   but 0/3 on the worst target (ABBA t19)
        whole-disc       9/14  0.64
        sector-runup      2/14 0.14   both wins from one degenerate n=1 target
        sector-hammer     2/14 0.14   likewise
        span-fixed        1/10 0.10

    So the recommendation is a CASCADE, not a choice: try ``max-variation`` first
    because it is cheap (4-10 s) and usually works, then fall back to ``track-ladder``
    which is the only strategy that has never failed a whole target. That captures the
    speed win without inheriting max-variation's failure mode at high n.

    An earlier version of this function recommended ``sector-hammer`` at n=1 on the
    strength of one disc where it won in 1 second. It went 0/3 at n~1 on the very next
    disc. **n is not sufficient** -- it predicts when max-variation becomes unsafe, but
    it does not make single-sector reads reliable. Sector-level recovery is 2/14
    overall and is not recommended at any n.

    PROVISIONAL: one drive, few discs, n=3 trials per cell. A prediction to check, not
    a setting to apply blind.
    """
    if n >= 6:
        return "→ track-ladder (max-variation went 0/3 at this n; skip the fast path)"
    return "→ try max-variation (~4-10 s), fall back to track-ladder if it stalls"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--device", default="/dev/sr0")
    ap.add_argument("--read-offset", type=int, default=30)
    ap.add_argument("--speed", type=int, default=32)
    ap.add_argument("--keep", default="", help="directory to keep the capture in")
    args = ap.parse_args(argv)

    from cdda2img.accudisc_reader import park_spindle, read_disc_c2, read_toc

    geom = read_toc(args.device)
    lsns = geom.track_lsns
    leadout = geom.disc_last_lsn + 1  # geometry bound; NOT the disc-ID input
    responses, cddb_hex, convention = resolve_ar(lsns, geom.disc_last_lsn)

    print(f"# {len(lsns)} tracks, lead-out {leadout}, cddb {cddb_hex}")
    print(f"# track 1 lsn {lsns[0]}{'  (program-area pre-gap)' if lsns[0] else ''}")
    print(f"# AccurateRip: {len(responses)} block(s), disc-ID convention: {convention}")
    if not responses:
        print(
            "# WARNING: no AR blocks under either convention. Could be a genuine "
            "absence, but check the computed key before assuming so.",
            file=sys.stderr,
        )

    # Honour TMPDIR rather than hardcoding a path: a whole-disc capture is ~640 MB+
    # and /tmp is a RAM-backed tmpfs on this machine, so the documented convention is
    # to invoke with TMPDIR=/var/tmp.
    out = Path(args.keep).resolve() if args.keep else Path(tempfile.gettempdir())
    out.mkdir(parents=True, exist_ok=True)
    pcm_p, c2_p = out / ".triage.pcm", out / ".triage.c2"

    print(f"# whole-disc C2 capture @ {args.speed}x ...", flush=True)
    try:
        read_disc_c2(args.device, pcm_p, c2_p, read_speed=args.speed)
        pcm, c2 = pcm_p.read_bytes(), c2_p.read_bytes()
    finally:
        if not args.keep:
            pcm_p.unlink(missing_ok=True)
            c2_p.unlink(missing_ok=True)
        park_spindle(args.device)

    flagged = [
        s
        for s in range(len(c2) // _C2_BYTES)
        if any(c2[s * _C2_BYTES : (s + 1) * _C2_BYTES])
    ]
    per_track: dict[int, list[int]] = {}
    failing: list[int] = []
    for t in range(1, len(lsns) + 1):
        start = lsns[t - 1]
        end = lsns[t] if t < len(lsns) else leadout
        per_track[t] = [f for f in flagged if start <= f < end]
        if responses:
            _v1, _v2, conf = _verify_track(
                pcm, t, lsns, leadout, args.read_offset, responses
            )
            if conf is None:
                failing.append(t)

    print(f"\nC2-flagged sectors: {len(flagged)}")
    if flagged:
        print(f"  {flagged[:24]}{' ...' if len(flagged) > 24 else ''}")
    print(f"AR-failing tracks: {failing or 'none'}")

    if not responses:
        print("\nVERDICT: no AccurateRip gate — cannot score recovery on this disc.")
        return 3
    if not failing:
        print(
            "\nVERDICT: CLEAN CONTROL — every track AR-verifies, nothing to recover. "
            "The strategy bench would exit 4 rather than bank a null result."
        )
        return 4

    # Suggest the target with the most flagged sectors: the hardest, and the one that
    # discriminates. An easy target makes every strategy win on attempt 1 and the
    # ranking collapses to per-attempt cost (which is what run 1 on Tracy did).
    ranked = sorted(failing, key=lambda t: -len(per_track[t]))
    target = ranked[0]
    print("\nVERDICT: BENCHABLE.")
    for t in ranked:
        print(
            f"  track {t:2d}: {len(per_track[t])} flagged sector(s)  {_profile(len(per_track[t]))}"
        )
    print(
        f"\n  suggested target: track {target} "
        f"({len(per_track[target])} flagged — hardest, so most discriminating)\n"
        f"  TMPDIR=/var/tmp uv run python tools/strategy_bench.py \\\n"
        f"      --device {args.device} --read-offset {args.read_offset} \\\n"
        f"      --trials 3 --budget 300 --track {target} \\\n"
        f"      --out private/bench/runs/run3/<disc>/strategy_t{target}.toml"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
