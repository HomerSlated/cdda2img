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


def _stale_extension() -> str | None:
    """Reason the loaded extension is older than the library, or None if it is fine.

    AccuDisc's build refreshes the interpreter-specific extension
    (``_accudisc.cpython-3XX-*.so``) but not always the ``abi3`` one — and which
    of those Python picks is decided by the interpreter, not by us. On 3.14 both
    are loadable and the specific build wins; on **3.10**, this project's floor
    and its default venv, only ``abi3`` is loadable. So a rebuild can leave us
    loading a days-old extension against a current ``libaccudisc``.

    Their ``_check_version_skew`` will not catch that: it compares ``[:2]`` —
    major.minor — and a struct-layout change inside 0.2.x leaves both sides
    saying 0.2. The ``size`` guards on ``accudisc_read_req`` / ``read_stats`` do
    hold, so this is not "anything could happen"; the exposure is the structs
    without one. But for an A/B specifically, *any* build skew is fatal to the
    result: it would compare an old binding against a current subprocess and
    report the difference as a carrier finding.

    mtime is a weak signal — a copy or a ``touch`` defeats it — so this refuses
    only on the unambiguous case (extension strictly older than the library) and
    stays quiet otherwise. It is a guard against the accident, not against an
    adversary.
    """
    root = ar._binding_search_path()
    if root is None:
        return None
    # pybinding -> <accudisc-repo>/bindings/python, so the library its RUNPATH
    # points at is <accudisc-repo>/build/src/libaccudisc.so.0. Derived rather
    # than configured: one symlink is the single point of truth for both.
    repo = Path(root).resolve().parent.parent
    lib = repo / "build" / "src" / "libaccudisc.so.0"
    exts = sorted((Path(root) / "accudisc").glob("_accudisc*.so"))
    if not lib.exists() or not exts:
        return None
    # Whichever this interpreter would actually load, not whichever is newest.
    tag = f"cpython-{sys.version_info.major}{sys.version_info.minor}"
    loadable = [e for e in exts if tag in e.name] or [
        e for e in exts if "abi3" in e.name
    ]
    if not loadable:
        return None
    ext = loadable[0]
    if ext.stat().st_mtime >= lib.stat().st_mtime:
        return None
    return (
        f"{ext.name} was built {time.strftime('%Y-%m-%d %H:%M', time.localtime(ext.stat().st_mtime))}, "
        f"older than libaccudisc.so.0 ({time.strftime('%Y-%m-%d %H:%M', time.localtime(lib.stat().st_mtime))}). "
        f"On Python {sys.version_info.major}.{sys.version_info.minor} that is the extension "
        f"this run would load, so arm B would be an older binding against a current library — "
        f"a build difference reported as a carrier difference. Rebuild it "
        f"(python build_accudisc.py) or run with --allow-stale if you know better."
    )


def _engine_version_only() -> str:
    """The library version alone — with the `[transport: ...]` suffix stripped.

    `engine_version()` appends the active transport, which is exactly the thing
    this harness changes between arms. Comparing the full banner at start and end
    therefore reports "the engine changed" on every single run: a guard that fires
    unconditionally, which is indistinguishable from no guard at all. The first
    run of this tool did precisely that and blamed AccuDisc's build tree for a
    field we were mutating on purpose.

    The check being made is "did libaccudisc move under us", so the string it
    compares must contain the library version and nothing that the measurement
    itself perturbs.
    """
    return ar.engine_version().split("[transport:")[0].strip()


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


def _classify_stream(stream: str, a1: Arm, b: Arm, a2: Arm) -> tuple[str, str]:
    """Which of the four patterns this stream shows, and what it means.

    The whole reason arm A runs twice. Three digests admit exactly four
    arrangements, and only one of them is evidence about the transport:

    ``identical``        all three agree — the strongest result available.
    ``carrier``          A1 == A2 != B. The disc reproduced and the binding did
                         not. This is the finding the harness exists to catch.
    ``cold-first-pass``  A1 != B == A2. The two *adjacent* arms agree and the
                         first one does not. A whole-disc read warms the drive
                         and settles its servo; treating this as a carrier defect
                         would blame the binding for being second in the queue.
    ``nondeterministic`` all three differ. The stream is not reproducible on this
                         disc at all, so it can neither convict nor acquit.
                         Expected for raw subchannel, which has no CIRC
                         protection and only a per-frame CRC.
    """
    d1, db, d2 = (arm.digests.get(stream) for arm in (a1, b, a2))
    if d1 == db == d2:
        return "identical", "all three arms agree"
    if d1 == d2 != db:
        return "carrier", "both subprocess arms agree, binding differs"
    if d1 != db == d2:
        return (
            "cold-first-pass",
            "binding matches the SECOND subprocess arm; the first differs",
        )
    return "nondeterministic", "all three differ — this stream does not reproduce"


def _report(arms: list[Arm], versions: tuple[str, str]) -> int:
    a1, b, a2 = arms
    print()
    print(f"# engine at start: {versions[0]}")
    print(f"# engine at end:   {versions[1]}")
    if versions[0] != versions[1]:
        print(
            "# WARNING: the engine version CHANGED during the run. Both transports\n"
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
    verdicts = {s: _classify_stream(s, a1, b, a2) for s in _STREAMS}
    for stream, (kind, note) in verdicts.items():
        print(f"  {stream:4s} {kind:18s} {note}")
        if kind != "identical":
            for arm in arms:
                print(f"         {arm.name:3s} {arm.digests.get(stream, '(absent)')}")

    # ONLY the A1 == A2 != B pattern indicts the carrier. The first version of
    # this tool suppressed the timing verdict on any mismatch at all, which threw
    # away a valid result on the first real run: the disc under test is known to
    # read non-deterministically, so "some stream differed" was never going to be
    # rare enough to gate on.
    carrier = [s for s, (kind, _) in verdicts.items() if kind == "carrier"]
    if carrier:
        print(
            f"\nCARRIER FINDING on {', '.join(carrier)}: both subprocess arms agree "
            f"with each other and disagree with the binding.\nThe disc reproduced; "
            f"the transport did not. Timing suppressed — a transport that returns\n"
            f"different bytes is not a faster way of doing the same thing."
        )
        return 1

    drift = abs(a1.seconds - a2.seconds)
    delta = b.seconds - (a1.seconds + a2.seconds) / 2
    adjacent = b.seconds - a2.seconds
    print()
    print(f"subprocess drift |A1-A2| : {drift:6.2f} s   <- the noise floor")
    print(
        f"binding vs mean(A1,A2)   : {delta:+6.2f} s "
        f"({delta / ((a1.seconds + a2.seconds) / 2) * 100:+.1f}%)"
    )
    # B and A2 are consecutive, so they share whatever warming A1 paid for. When
    # A1 is the cold outlier this is the cleaner of the two comparisons; it is
    # printed alongside rather than instead, because choosing the flattering one
    # after seeing both is how a harness stops being a harness.
    print(
        f"binding vs A2 (adjacent) : {adjacent:+6.2f} s "
        f"({adjacent / a2.seconds * 100:+.1f}%)"
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
    ap.add_argument(
        "--allow-stale",
        action="store_true",
        help="run even if the binding extension is older than libaccudisc (see _stale_extension)",
    )
    args = ap.parse_args()

    stale = _stale_extension()
    if stale and not args.allow_stale:
        print(f"refusing to run: {stale}", file=sys.stderr)
        return 2

    free = shutil.disk_usage(args.workdir).free
    if free < 1_200_000_000:
        print(
            f"only {free / 1e9:.1f} GB free in {args.workdir}; need ~1.2 GB",
            file=sys.stderr,
        )
        return 2

    workdir = args.workdir / f"disc_ab_{os.getpid()}"
    workdir.mkdir(parents=True)
    version_start = _engine_version_only()
    try:
        arms = [
            _run_arm("A1", "subprocess", args.device, args.speed, workdir),
            _run_arm("B", "binding", args.device, args.speed, workdir),
            _run_arm("A2", "subprocess", args.device, args.speed, workdir),
        ]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return _report(arms, (version_start, _engine_version_only()))


if __name__ == "__main__":
    raise SystemExit(main())
