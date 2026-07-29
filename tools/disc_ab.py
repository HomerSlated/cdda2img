#!/usr/bin/env python3
"""Whole-disc A/B/A: does `read_disc_c2` on the binding keep up with the CLI?

The one question `tools/sink_prescreen.py` could not answer. That harness timed
the de-interleave loop against memory and found ~273x headroom, which says the
Python sink is not *intrinsically* too slow — but the failure that would justify
a library-side whole-disc entry point is nonlinear and needs a drive: a sink
that falls behind at 32-40x drains the drive's cache, the drive stops streaming,
and the read collapses to a seek-per-chunk crawl. No amount of synthetic
throughput predicts where that knee sits.

This is deliberately **not** `tools/binding_ab.py`. That tool asks whether the
binding returns the same bytes (correctness, per span, with a determinism
control). This one asks whether it returns them as fast (throughput, whole disc).
Merging them would mean one tool with two acceptance criteria and two failure
modes, and the span-level positive control there — read it twice, compare — is
meaningless at whole-disc scale where a single arm is minutes long.

Why A/B/A and not A/B
---------------------
The drive's governor moves. The PX-716A admitted `[32,24,8,4]` on ABBA *Gold* in
July and `[8,4]` on the same disc a fortnight later, having throttled degraded
media. Over three whole-disc reads the drive also warms, and the disc's radius
changes what CAV delivers. So the subprocess arm is run **twice, bracketing the
binding arm**:

    A1  subprocess   \\  |A1 - A2| is the noise floor. A binding-vs-subprocess
    B   binding       )  delta smaller than the drift between two runs of the
    A2  subprocess   /   SAME transport is not a measurement of anything.

If A1 and A2 disagree by more than the A/B delta, the run reports that and
declines to draw a conclusion rather than reporting the smaller number.

What is pinned, and why each one
--------------------------------
**Speed.** Both arms are given the same `--speed`, and the drive is asked what it
actually settled on before each arm starts. Arm A sets it through `accudisc
read --speed N`; arm B through `req.speed_x`. Those are different code paths to
the same drive state, and if they land on different rungs the wall-clock delta
is measuring the governor, not the transport.

**Engine version, at start AND end.** Both transports resolve one
`libaccudisc.so.0` out of AccuDisc's build tree (`readelf -d` on the extension
shows `RUNPATH .../build/src`), which is what makes this comparison
confound-free — and also what makes a rebuild mid-run silently compare two
library versions. Capturing the banner at both ends converts "they agreed not to
build" into "we verified they did not".

**Stream set.** Both arms capture PCM + C2 + raw subchannel, which is what the
rip path actually asks for. A pcm-only comparison would run at 2352 B/sector
instead of 2742 and understate the sink's work by the two streams that need
de-interleaving.

Correctness is checked too, but as a gate rather than the result: if the two
transports return different bytes, the timing comparison is meaningless and the
tool says so first.

Usage
-----
    TMPDIR=/var/tmp uv run python tools/disc_ab.py --device /dev/sr0 --speed 24

Exit codes: 0 the comparison ran and the arms agree byte-for-byte; 1 the streams
differ (a real finding, timing suppressed); 2 the comparison could not be run.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cdda2img import accudisc_reader as ar

#: Streams the rip path actually captures. Keep all three: dropping C2 or sub
#: changes the sector length and with it the amount of de-interleaving under test.
_STREAMS = ("pcm", "c2", "sub")


@dataclass
class Arm:
    """One whole-disc read: which transport, how long it took, what it produced."""

    name: str
    transport: str
    seconds: float
    sectors: int
    digests: dict[str, str]
    speed_reported: tuple[int | None, int | None]

    @property
    def sectors_per_second(self) -> float:
        return self.sectors / self.seconds if self.seconds > 0 else float("inf")

    @property
    def speed_x(self) -> float:
        """Achieved throughput in X (1x = 75 sectors/s), for comparison with `speeds`."""
        return self.sectors_per_second / 75.0


def _digest(path: Path) -> str:
    h = hashlib.blake2b(digest_size=16)
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _run_arm(
    name: str, transport: str, device: str, speed: int | None, workdir: Path
) -> Arm:
    """One whole-disc read on *transport*, timed, hashed, then deleted.

    The files are removed as soon as they are hashed: three whole-disc arms is
    ~1.3 GB of PCM alone, and a run that dies on ENOSPC halfway through the third
    arm has spent the drive time and produced nothing.
    """
    os.environ[ar.TRANSPORT_ENV] = transport
    # Read back rather than assume: the policy is consulted per call, and a
    # binding that failed to import would silently serve this arm from the
    # subprocess — making the whole comparison a subprocess-vs-subprocess null.
    actual = ar.active_transport()
    if actual != transport:
        msg = (
            f"arm {name}: asked for the {transport} transport, got {actual}. "
            f"Refusing to report a comparison between an arm and itself."
        )
        raise SystemExit(msg)

    out = {s: workdir / f"{name}.{s}" for s in _STREAMS}
    sectors = 0

    def progress(done: int, _total: int) -> None:
        nonlocal sectors
        sectors = done

    t0 = time.perf_counter()
    ar.read_disc_c2(
        device,
        output_pcm=out["pcm"],
        output_c2=out["c2"],
        output_sub=out["sub"],
        read_speed=speed,
        progress_cb=progress,
    )
    elapsed = time.perf_counter() - t0

    # Asked *after* the read: the drive's governor can move a rung mid-read on
    # degraded media, and the value before the read is not what was in force.
    reported = ar.read_speed(device)

    digests = {s: _digest(p) for s, p in out.items() if p.exists()}
    if not sectors:
        # No progress tokens (binding path always emits; the subprocess path
        # needs --progress-fd). Fall back to the PCM length so the rate is still
        # computable rather than dividing by zero.
        sectors = out["pcm"].stat().st_size // 2352 if out["pcm"].exists() else 0
    for p in out.values():
        p.unlink(missing_ok=True)

    return Arm(name, transport, elapsed, sectors, digests, reported)


def _report(arms: list[Arm], versions: tuple[str, str]) -> int:
    a1, b, a2 = arms
    print()
    print(f"# engine at start: {versions[0]}")
    print(f"# engine at end:   {versions[1]}")
    if versions[0] != versions[1]:
        print(
            "# WARNING: the engine banner CHANGED during the run. Both transports\n"
            "# resolve one libaccudisc out of AccuDisc's build tree, so this run\n"
            "# compared two library versions and the delta below is not a carrier\n"
            "# measurement. Discard it and rerun against a quiet tree."
        )
    print()
    for arm in arms:
        cur, mx = arm.speed_reported
        print(
            f"{arm.name:3s} {arm.transport:11s} {arm.seconds:8.2f} s  "
            f"{arm.sectors:7d} sectors  {arm.sectors_per_second:9.1f} sec/s  "
            f"= {arm.speed_x:5.2f}x   (page2a current={cur} max={mx})"
        )

    # Correctness gates the timing: two transports that disagree about the bytes
    # are not two ways of doing the same thing, and comparing their speed would
    # be comparing a read with something that is not that read.
    print()
    mismatched = [s for s in _STREAMS if len({arm.digests.get(s) for arm in arms}) > 1]
    if mismatched:
        print(f"STREAMS DIFFER between arms: {', '.join(mismatched)}")
        for arm in arms:
            for s in mismatched:
                print(f"    {arm.name:3s} {s:4s} {arm.digests.get(s, '(absent)')}")
        print(
            "\nThis is a correctness finding and it suppresses the timing verdict.\n"
            "NOTE it is not automatically a binding defect: a differing arm may be\n"
            "the disc reading differently, which is why A1 and A2 are both here. If\n"
            "A1 != A2 the disc is the variable; if A1 == A2 != B the carrier is."
        )
        return 1
    print(f"streams identical across all three arms ({', '.join(_STREAMS)})")

    drift = abs(a1.seconds - a2.seconds)
    delta = b.seconds - (a1.seconds + a2.seconds) / 2
    print()
    print(f"subprocess drift |A1-A2| : {drift:6.2f} s")
    print(
        f"binding vs subprocess    : {delta:+6.2f} s "
        f"({delta / ((a1.seconds + a2.seconds) / 2) * 100:+.1f}%)"
    )
    if abs(delta) <= drift:
        print(
            "\nVERDICT: no measurable difference. The binding-vs-subprocess delta is\n"
            "within the drift between two runs of the SAME transport, so this run\n"
            "cannot distinguish them — which is the answer the migration needed."
        )
    elif delta > 0:
        print(
            f"\nVERDICT: the binding is slower by {delta:.2f} s beyond the noise floor.\n"
            "Worth reporting to AccuDisc with the sector rate: if the binding arm's\n"
            "throughput sits well under the rung it asked for, that is the cache-drain\n"
            "knee sink_prescreen.py could not see, and it is the case for a\n"
            "library-side whole-disc entry point."
        )
    else:
        print(
            f"\nVERDICT: the binding is FASTER by {-delta:.2f} s beyond the noise floor."
        )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--device", default="/dev/sr0")
    ap.add_argument(
        "--speed",
        type=int,
        default=None,
        help="pin both arms to this rung (X). Omit to leave the drive alone — but "
        "then the governor is free to move between arms and the comparison is weaker.",
    )
    ap.add_argument(
        "--workdir",
        type=Path,
        # S108: /var/tmp is the deliberate choice, not a lapse. /tmp here is a
        # RAM-backed tmpfs of ~7.8 GB and a whole-disc capture floods it; the
        # per-PID subdirectory below is what keeps two runs from colliding.
        default=Path(os.environ.get("TMPDIR", "/var/tmp")),  # noqa: S108
        help="scratch for the PCM/C2/sub streams; each arm is deleted after hashing. "
        "NOT /tmp — it is a RAM-backed tmpfs here and a whole disc floods it.",
    )
    args = ap.parse_args()

    free = shutil.disk_usage(args.workdir).free
    if free < 1_200_000_000:
        print(
            f"only {free / 1e9:.1f} GB free in {args.workdir}; need ~1.2 GB",
            file=sys.stderr,
        )
        return 2

    workdir = args.workdir / f"disc_ab_{os.getpid()}"
    workdir.mkdir(parents=True)
    version_start = ar.engine_version()
    try:
        arms = [
            _run_arm("A1", "subprocess", args.device, args.speed, workdir),
            _run_arm("B", "binding", args.device, args.speed, workdir),
            _run_arm("A2", "subprocess", args.device, args.speed, workdir),
        ]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return _report(arms, (version_start, ar.engine_version()))


if __name__ == "__main__":
    raise SystemExit(main())
