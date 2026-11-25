"""Run every read path the AccuDisc read policy now drives, once, on a device.

The adoption of AccuDisc 0.53-0.59 (kgr, 2026-11-25: "I would rather do my tests
against 0.59.0 with all new features enabled") was tested on fakes and one
binding-signature check; neither issues a read. This issues one of each, on a
CDEmu disc (virtual: no drive claim needed) or any device you name:

1. a capture (``read_disc_c2``), never budgeted, saving its full TOC for the
   session bounds;
2. inside ``drive_work.budget`` + ``recovery_policy``:
   - a speed-ladder attempt (``read_span_attempt``, GATED_REREAD);
   - a whole-disc windowed read (``read_span_windows``), where the policy should
     decline the cache defeat for want of a target;
   - a span detail read (``read_span_detail``, SPAN_RESCUE), only with C2;
3. a ``verify_against_capture`` (compare_read under REFERENCE_VERIFY), which must
   find no disagreement with the capture.

It prints what each returned and the PROV the rip would record. Scratch goes to
``--scratch`` (default under /var/tmp) and is deleted at the end.

    cdemu load 0 /var/tmp/preemph_disc/preemph.toc
    uv run python tools/policy_smoke.py --device /dev/sr1
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path

from cdda2img import accudisc_reader as ar
from cdda2img import drive_speed, drive_work


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    ap.add_argument("--device", default="/dev/sr1")
    # /var/tmp, not /tmp: the capture is disc-sized and /tmp is RAM (scratch rule);
    # mkdtemp below makes a private directory inside it.
    ap.add_argument(
        "--scratch",
        type=Path,
        default=Path("/var/tmp"),  # noqa: S108
    )
    ap.add_argument(
        "--ladder",
        help="admitted rungs, comma-separated, instead of probing (CDEmu's probe "
        "fails, leaving one rung, which the verify step rightly refuses)",
    )
    args = ap.parse_args()

    missing = ar.missing_rip_features()
    print(f"engine: {ar.engine_version()}  missing features: {missing or 'none'}")
    if missing:
        return 1
    work_dir = Path(tempfile.mkdtemp(prefix="policy_smoke_", dir=args.scratch))
    try:
        ladder = [int(x) for x in args.ladder.split(",")] if args.ladder else None
        return _run(args.device, work_dir, ladder)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _run(device: str, work_dir: Path, ladder: list[int] | None) -> int:
    pcm, fulltoc = work_dir / "capture.pcm", work_dir / "capture.fulltoc"
    counters: list[ar.CaptureCounters] = []
    ar.read_disc_c2(device, pcm, output_fulltoc=fulltoc, stats_cb=counters.append)
    sectors = pcm.stat().st_size // 2352
    readable = ar.readable_extent(fulltoc.read_bytes())
    honoured = counters[0].speed_honoured_x if counters else None
    print(f"capture: {sectors} sectors, readable={readable}, honoured={honoured}")

    ladder = ladder or drive_speed.admitted_ladder(device)
    c2 = ar.drive_supports_c2(device)
    print(f"ladder={ladder} c2_usable={c2}")
    failures = 0
    with (
        drive_work.budget(5000, 1000, 10000) as work,
        ar.recovery_policy(ladder, c2_usable=c2, readable=readable) as policy,
    ):
        speed = ladder[0]
        got = ar.read_span_attempt(
            device, 0, 16, speed, cache_defeat=True, readable=readable
        )
        print(
            f"gated attempt: {len(got.pcm) // 2352} sectors, "
            f"honoured={got.speed_honoured_x}, defeat={got.cache_defeat}, "
            f"c2_flagged={got.c2_flagged}"
        )
        failures += got.pcm != pcm.read_bytes()[: 16 * 2352]
        whole = ar.read_span_windows(
            device, 0, sectors, [(0, 16)], speed, readable=readable
        )
        print(f"whole-disc windows: defeat={whole.cache_defeat}")
        failures += whole.pieces[0] != got.pcm
        if c2:
            detail = ar.read_span_detail(
                device, 0, 16, read_speed=speed, readable=readable
            )
            print(
                f"span detail: verify={detail.verify_passes} "
                f"states={sorted(set(detail.states))}"
            )
        print(f"PROV recovery_policy={policy.record()}")
        print(f"PROV recovery_policy_overrides={policy.overrides_record()}")
        print(f"PROV recovery_drive_work={work.record()}")
        print(f"PROV recovery_engine_work={work.engine_record()}")
        if work.engine_record() is None:
            print("FAIL: the engine reported no spend")
            failures += 1

    verify = ar.verify_against_capture(
        device,
        pcm,
        ref_speed_x=honoured or ladder[0],
        exclude_speed_x=0,
        admitted=ladder,
        c2_usable=c2,
        readable=readable,
        on_speed=lambda x: print(f"verify at {x}X"),
    )
    print(f"verify: {verify}")
    if verify.first_disagreement is not None:
        failures += 1
    print("PASS" if not failures else f"FAIL ({failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
