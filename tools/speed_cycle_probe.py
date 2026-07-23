#!/usr/bin/env python3
"""Does SPEED CYCLING re-seat a slipping disc and change which sectors mis-read?

Hypothesis under test (2026-07-23, Tracy Chapman / PX-716A). A freshly-clamped disc
holds a fixed error state: `tools/settling_curve.py` ran 8 whole-disc passes at a
constant 32x over 14 minutes and track 8 failed AccurateRip on every one, with no
decay — so there is no progressive "settling". Yet earlier the same disc went from
defective to perfectly clean across an hour that happened to run the overread battery,
i.e. ~375 reads cycling 40/32/24/8/4x. The one thing that hour had and the flat curve
lacked is hundreds of ACCELERATIONS AND DECELERATIONS.

This drive has a known-worn spindle o-ring and thin discs slip on it. A spin-up or
spin-down is precisely when a slipping disc can rotate relative to its clamp and land
in a new eccentricity phase. If that is the mechanism, then the production recovery
speed ladder (fastest -> slowest) is not only probing "which speed reads best" — it is
repeatedly RE-SEATING the disc, buying fresh chances at a good clamping. That would be
a materially different explanation of why the ladder works.

Protocol per round: probe whole-disc (baseline error set) -> burst of short reads at
alternating extreme speeds to force accel/decel -> probe again. A change in the flagged
set or the AR verdict across the burst, more often than across an equal-length constant
speed control, supports the re-seat mechanism.

Control matters: whole-disc passes alone already relocate the flagged LBA sometimes
(the curve saw 129481 / 113043 / 114575). So the burst must be compared against a
constant-speed control of similar duration, not against "nothing".

Usage (from project root):
    TMPDIR=/var/tmp uv run python tools/speed_cycle_probe.py \
        --device /dev/sr0 --read-offset 30 --rounds 3 \
        --out private/bench/runs/run3/tracy_chapman/speed_cycle.toml
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field
from pathlib import Path

_SECTOR_BYTES = 2352
_C2_BYTES = 294


@dataclass
class Observation:
    label: str  # "baseline" | "after-burst" | "after-control"
    round_index: int
    flagged: list[int] = field(default_factory=list)
    ar_fail: list[int] = field(default_factory=list)


def _verify_track(
    pcm: bytes,
    track: int,
    track_lsns: list[int],
    leadout: int,
    read_offset: int,
    responses: list[list[dict]],
) -> bool:
    """Mirrors accuraterip.verify_rip's window arithmetic — clamp AND zero-pad, or a
    positive read offset silently shortens track 1 / the last track and shifts
    _ar_checksums' exclusion boundary into a spurious miss."""
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

    _v1, _v2, c1, c2 = match_track_pcm(raw, track, len(track_lsns), responses)
    return c1 is not None or c2 is not None


def probe(
    device: str,
    track_lsns: list[int],
    leadout: int,
    read_offset: int,
    responses: list[list[dict]],
    speed: int,
    tmp_dir: Path,
) -> tuple[list[int], list[int]]:
    """One whole-disc C2 capture → (flagged LBAs, AR-failing tracks)."""
    from cdda2img.accudisc_reader import read_disc_c2

    pcm_p, c2_p = tmp_dir / ".cycle.pcm", tmp_dir / ".cycle.c2"
    read_disc_c2(device, pcm_p, c2_p, read_speed=speed)
    pcm, c2 = pcm_p.read_bytes(), c2_p.read_bytes()
    flagged = [
        s
        for s in range(len(c2) // _C2_BYTES)
        if any(c2[s * _C2_BYTES : (s + 1) * _C2_BYTES])
    ]
    fail = [
        t
        for t in range(1, len(track_lsns) + 1)
        if responses
        and not _verify_track(pcm, t, track_lsns, leadout, read_offset, responses)
    ]
    pcm_p.unlink(missing_ok=True)
    c2_p.unlink(missing_ok=True)
    return flagged, fail


def _burst(
    device: str,
    lbas: list[int],
    cycles: int,
    speeds: list[int],
    tmp_dir: Path,
    tag: str,
) -> float:
    """``cycles`` short reads, cycling through ``lbas`` and ``speeds`` independently.

    Every arm goes through here, so all arms are matched on **read count**. The first
    version got that wrong: it matched the control on wall-clock DURATION, and since
    the constant-speed arm has no speed-change overhead its tight loop issued far more
    reads than the cycling arm.

    The three arms this supports isolate one factor each:
      * ``burst``   — many speeds, ONE lba   → speed transitions, localized
      * ``control`` — one speed,   ONE lba   → no transitions,   localized
      * ``sweep``   — one speed,   MANY lbas → no transitions,   sled traversal

    That is the discriminating design. ``burst`` beating ``control`` alone is
    ambiguous — it could be the speed cycling, or it could be that hammering one spot
    degrades tracking. If ``sweep`` also beats ``control``, the active factor is
    traversal, not speed; if only ``burst`` does, it is the speed transitions. The
    distinction matters because the bench's ``_recover_rung`` re-reads individual
    flagged sectors in place (0/25) while whole-disc passes came back clean ~27% of
    the time.
    """
    from cdda2img.accudisc_reader import read_span

    tmp = tmp_dir / f".{tag}.pcm"
    t0 = time.monotonic()
    for i in range(cycles):
        read_span(
            device,
            lbas[i % len(lbas)],
            64,
            tmp,
            read_speed=speeds[i % len(speeds)],
        )
    tmp.unlink(missing_ok=True)
    return time.monotonic() - t0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--device", default="/dev/sr0")
    ap.add_argument("--read-offset", type=int, default=30)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--cycles", type=int, default=12, help="speed changes per burst")
    ap.add_argument("--burst-speeds", default="4,40")
    ap.add_argument("--probe-speed", type=int, default=32)
    ap.add_argument("--burst-lba", type=int, default=113000)
    ap.add_argument("--out", default="private/bench/runs/run3/speed_cycle.toml")
    args = ap.parse_args(argv)

    from cdda2img.accudisc_reader import park_spindle, read_toc
    from cdda2img.accuraterip import fetch_ar_responses
    from cdda2img.cddb import compute_cddb_disc_id

    geom = read_toc(args.device)
    lsns = geom.track_lsns
    leadout = geom.disc_last_lsn + 1  # geometry bound; NOT the disc-ID input
    cddb_hex = compute_cddb_disc_id(lsns, geom.disc_last_lsn)
    responses, _t, _b = fetch_ar_responses(lsns, geom.disc_last_lsn, int(cddb_hex, 16))
    print(f"# disc {cddb_hex}, {len(responses)} AR block(s)", flush=True)

    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    speeds = [int(s) for s in args.burst_speeds.split(",") if s]
    # Sweep arm: same read count, one speed, but spread across the program area so the
    # sled must traverse — isolates traversal from speed transitions.
    sweep_lbas = [round(leadout * f / 8) for f in range(1, 8)]
    obs: list[Observation] = []

    def _probe(label: str, rnd: int) -> Observation:
        flagged, fail = probe(
            args.device,
            lsns,
            leadout,
            args.read_offset,
            responses,
            args.probe_speed,
            out.parent,
        )
        o = Observation(label, rnd, flagged, fail)
        obs.append(o)
        print(
            f"  {label:<14} c2_flagged={len(flagged):<3} ar_fail={fail or '-'}  "
            f"{flagged[:8]}",
            flush=True,
        )
        return o

    try:
        for rnd in range(1, args.rounds + 1):
            print(f"[round {rnd}/{args.rounds}]", flush=True)
            _probe("baseline", rnd)
            # COUNTERBALANCED: the cycling arm ran first in every round of the first
            # version, so any within-round drift loaded entirely onto the control.
            # Rotating the arm order lets that drift cancel across rounds instead.
            arms: list[tuple[str, list[int], list[int]]] = [
                ("burst", speeds, [args.burst_lba]),
                ("control", [args.probe_speed], [args.burst_lba]),
                ("sweep", [args.probe_speed], sweep_lbas),
            ]
            rot = (rnd - 1) % len(arms)
            for tag, arm_speeds, arm_lbas in arms[rot:] + arms[:rot]:
                secs = _burst(
                    args.device, arm_lbas, args.cycles, arm_speeds, out.parent, tag
                )
                print(
                    f"  {tag}: {args.cycles} reads, speeds={arm_speeds}, "
                    f"{len(arm_lbas)} lba(s) in {secs:.0f}s",
                    flush=True,
                )
                _probe(f"after-{tag}", rnd)
    finally:
        park_spindle(args.device)

    lines = ["# speed-cycle re-seat probe", "", f"disc = {cddb_hex!r}", ""]
    for o in obs:
        lines += [
            "[[obs]]",
            f"round = {o.round_index}",
            f"label = {o.label!r}",
            f"c2_flagged_count = {len(o.flagged)}",
            f"c2_flagged = {o.flagged}",
            f"ar_fail = {o.ar_fail}",
            "",
        ]
    out.write_text("\n".join(lines))

    print("\n=== changes in the flagged set ===")
    for i in range(1, len(obs)):
        prev, cur = obs[i - 1], obs[i]
        moved = set(prev.flagged) != set(cur.flagged)
        ar_moved = set(prev.ar_fail) != set(cur.ar_fail)
        print(
            f"{prev.label} -> {cur.label} (r{cur.round_index}): "
            f"flagged {'CHANGED' if moved else 'same'}, "
            f"AR {'CHANGED' if ar_moved else 'same'}"
        )
    print(f"\n# wrote {len(obs)} observations → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
