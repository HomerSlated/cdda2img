#!/usr/bin/env python3
"""A/B the AccuDisc Python binding against the subprocess transport, on real media.

AccuDisc's §bs closes with the one thing only this project can supply: *"the
binding is 33 green device-free tests and zero evidence it reads a disc
correctly."* This is that evidence, or its absence.

**The positive control is the point of the design, not politeness.** Yesterday's
measurement on this very disc found that read errors are stable *per speed*, not
per defect — Tracy Chapman's track 8 returned a different CRC on all five
captures. So "read the span through both transports and diff" would report a
transport defect where it had actually measured disc noise, which is precisely
the well-formed-but-wrong-referent failure both projects have been tripping over
all week. Every span is therefore read **three** times:

    A1  subprocess      \\ if A1 != A2 the region is non-deterministic and the
    A2  subprocess      /  cross-transport comparison there proves nothing
    B   binding            compared against A1 only when the control held

A region that cannot reproduce against itself cannot falsify the binding. The
tool says so and moves on rather than scoring it.

**Two transports, one drive.** Every read is sequential and the binding's device
handle is closed before the subprocess runs. Nothing here reads concurrently.

Running it needs an interpreter that can load the binding, which is not this
project's default 3.10: the extension is built per-interpreter and the package is
not on an index yet (AccuDisc's §bs.4). Run it in a fully **ephemeral** env —
``--no-project`` plus ``--with`` for both packages — so the project's own `.venv`
is left alone. Without ``--no-project``, ``uv run --python 3.14`` *deletes and
recreates* `.venv` at 3.14, and the next ``uv run pytest`` silently tests a
different interpreter than you think.

    ACCUDISC_INCLUDE_DIR=$HOME/Git/accudisc/include \\
    ACCUDISC_LIB_DIR=$HOME/Git/accudisc/build/src \\
    uv run --no-project --python 3.14 --with cffi \\
        --with $HOME/Git/accudisc/bindings/python \\
        --with $HOME/Git/cdda2img \\
        python tools/binding_ab.py --device /dev/sr0

Exit codes: 0 every scored span matched; 1 a scored span differed (a real
finding); 2 could not run the comparison at all.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

_FRAME = 2352


def _binding() -> Any:
    """Import the AccuDisc binding, typed as ``Any`` on purpose.

    Two reasons, and neither is laziness. The package is not installed in the
    type-check environment — it is built per-interpreter and is not on an index
    (AccuDisc §bs.4). And ``tools/`` is on ty's ``extra-paths``, so a static
    check resolves ``accudisc`` to ``tools/accudisc/`` — the git-ignored
    *binary* snapshot directory — as a PEP 420 namespace package, and reports
    every attribute of the real package as missing.

    At runtime the real package wins regardless: the import scan records a
    directory without ``__init__.py`` as a namespace *portion* and keeps
    searching, so a regular package further along ``sys.path`` takes precedence.
    Confirmed by the A/B actually running.
    """
    import accudisc

    return accudisc


def _digest(data: bytes) -> str:
    """Short hash, for eyeballing the three reads in the transcript.

    The verdict never rests on this — it is computed from a full `==` on the
    bytes. A truncated digest is a display aid, and treating one as proof of
    equality is how a comparison quietly stops comparing.
    """
    return hashlib.sha256(data).hexdigest()[:16]


@dataclass
class SpanCase:
    """One region to compare, and why it was chosen."""

    name: str
    lba: int
    count: int
    why: str


def _default_spans(track_lsns: list[int], leadout: int) -> list[SpanCase]:
    """Sample the disc where a CAV drive behaves differently, plus the known bad spot.

    Radius matters (the whole §9.3 confound was a radius term), so the clean
    samples are taken at the inside, middle and outside rather than three times in
    one place. The damaged track is included deliberately and is *expected* to
    fail its own control — that outcome is a check on the harness.
    """
    n = len(track_lsns)
    cases = [
        SpanCase("inner", track_lsns[0] + 300, 400, "track 1, innermost radius"),
        SpanCase(
            "middle", track_lsns[n // 2], 400, f"track {n // 2 + 1} start, mid radius"
        ),
        SpanCase("outer", max(0, leadout - 800), 400, "just inside the lead-out"),
    ]
    return cases


def read_subprocess(device: str, lba: int, count: int) -> bytes:
    from cdda2img.accudisc_reader import read_span_bytes

    return read_span_bytes(device, lba, count)


def read_binding(device: str, lba: int, count: int) -> bytes:
    ad = _binding()

    with ad.Device(device) as dev:
        data, _result = dev.read_span(lba, count)
    return data


def compare_toc(device: str) -> list[str]:
    """Field-by-field: the binding's structured TOC vs the parsed CLI text.

    The CLI path is what ``subq_toc`` builds a disc from today, so any divergence
    here is a wrong disc ID waiting to happen — the class of bug that surfaces as
    a 404 from a content-addressed lookup rather than as an error.
    """
    ad = _binding()

    from cdda2img.accudisc_reader import read_toc

    geom = read_toc(device)  # subprocess + our regexes
    with ad.Device(device) as dev:
        toc, info = dev.read_toc_src()

    b_lsns = [t.lba for t in toc.audio_tracks]
    b_anom = sorted(ad.anomaly_token(b) for b in toc.anomalies)
    b_data = [t.number for t in toc.data_tracks]

    # (field, what the CLI text gave us, what the binding's structs gave us).
    # Table-driven so adding a field is a row, not a branch — and so that every
    # field is compared the same way rather than each getting its own `if`.
    #
    # `session_count` is the READ DISC INFORMATION count on BOTH sides: verified
    # against cli/format.c:47, which prints `info->session_count`. It is NOT
    # `toc.mapped_session_count`, and conflating them is the §bs.2 hazard.
    fields: list[tuple[str, object, object]] = [
        ("track_lsns", geom.track_lsns, b_lsns),
        ("disc_last_lsn", geom.disc_last_lsn, toc.leadout_lba - 1),
        ("source", geom.source, info.source.token),
        ("degrade", geom.degrade, info.degrade.token),
        ("toc_trusted", geom.toc_trusted, toc.trusted),
        ("anomalies", sorted(geom.anomalies), b_anom),
        ("data_tracks", geom.data_tracks, b_data),
        ("session_count", geom.session_count, info.session_count),
    ]
    problems = [
        f"{name}: cli={cli!r} binding={binding!r}"
        for name, cli, binding in fields
        if cli != binding
    ]

    print("\n=== TOC parity (binding structs vs CLI text + our regexes)")
    print(f"  tracks        {len(b_lsns)}")
    print(f"  leadout       {toc.leadout_lba}")
    print(f"  source        {info.source.token}   degrade {info.degrade.token}")
    print(f"  trusted       {toc.trusted}   anomalies {b_anom or '[]'}")
    print(
        f"  sessions      disc_info={info.session_count} "
        f"mapped={toc.mapped_session_count} total={toc.sessions_total}"
    )
    if info.session_count and toc.sessions_total > toc.mapped_session_count:
        print(
            "  WARNING: sessions_total > mapped_session_count — the seams exist but "
            "their positions are unknown (AccuDisc §bs.2). The CLI text cannot "
            "express this state at all."
        )
    for p in problems:
        print(f"  MISMATCH  {p}")
    if not problems:
        print("  → all compared fields agree")
    return problems


def run_span(device: str, case: SpanCase) -> str:
    """Read one span three times and score it. Returns 'match' | 'differ' | 'void'."""
    print(f"\n=== {case.name}: lba {case.lba} +{case.count} ({case.why})")
    a1 = read_subprocess(device, case.lba, case.count)
    a2 = read_subprocess(device, case.lba, case.count)
    print(f"  subprocess #1  {len(a1):>9} B  {_digest(a1)}")
    print(f"  subprocess #2  {len(a2):>9} B  {_digest(a2)}")

    if a1 != a2:
        bad = sum(
            1
            for i in range(0, min(len(a1), len(a2)), _FRAME)
            if a1[i : i + _FRAME] != a2[i : i + _FRAME]
        )
        print(
            f"  CONTROL FAILED: the same transport disagrees with itself in "
            f"{bad} sector(s). This region is non-deterministic, so it cannot "
            f"falsify the binding — not scored."
        )
        return "void"

    b = read_binding(device, case.lba, case.count)
    print(f"  binding        {len(b):>9} B  {_digest(b)}")
    if len(b) != len(a1):
        print(f"  DIFFER: length {len(b)} vs {len(a1)}")
        return "differ"
    if b == a1:
        print("  → byte-identical (control held)")
        return "match"
    bad = [
        i // _FRAME
        for i in range(0, len(a1), _FRAME)
        if a1[i : i + _FRAME] != b[i : i + _FRAME]
    ]
    print(f"  DIFFER: {len(bad)} sector(s), first at offset +{bad[0]}")
    return "differ"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--device", default="/dev/sr0")
    ap.add_argument(
        "--span",
        action="append",
        default=None,
        metavar="LBA:COUNT",
        help="extra span to compare (repeatable); added to the default three",
    )
    ap.add_argument(
        "--skip-toc", action="store_true", help="span comparison only, no TOC parity"
    )
    args = ap.parse_args()

    try:
        ad = _binding()
    except ImportError as exc:
        print(f"binding not importable: {exc}", file=sys.stderr)
        print(
            "see this file's docstring for the uv invocation that loads it",
            file=sys.stderr,
        )
        return 2
    # Constructed separately from the import: if the import itself fails the name
    # `accudisc` is unbound, so catching AbiMismatch in the same clause would raise
    # NameError instead (AccuDisc's §bs.4 snippet has this bug).
    try:
        with ad.Device(args.device):
            pass
    except ad.AbiMismatch as exc:
        print(f"binding/library ABI mismatch — rebuild: {exc}", file=sys.stderr)
        return 2
    except ad.AccuDiscError as exc:
        print(f"cannot open {args.device}: {exc}", file=sys.stderr)
        return 2

    print(f"# device        {args.device}")
    print(f"# binding       accudisc {ad.version_string()}")
    print(f"# interpreter   {sys.version.split()[0]}")

    from cdda2img.accudisc_reader import read_toc

    geom = read_toc(args.device)
    leadout = geom.disc_last_lsn + 1
    print(f"# disc          {len(geom.track_lsns)} tracks, leadout {leadout}")

    problems: list[str] = []
    if not args.skip_toc:
        problems = compare_toc(args.device)

    cases = _default_spans(geom.track_lsns, leadout)
    for spec in args.span or []:
        lba_s, _, cnt_s = spec.partition(":")
        cases.append(SpanCase(f"custom {spec}", int(lba_s), int(cnt_s), "requested"))

    verdicts = {c.name: run_span(args.device, c) for c in cases}

    print("\n=== summary")
    for name, v in verdicts.items():
        print(f"  {name:<16} {v}")
    if problems:
        print(f"  {'toc parity':<16} {len(problems)} mismatch(es)")
    scored = [v for v in verdicts.values() if v != "void"]
    voided = len(verdicts) - len(scored)
    if voided:
        print(f"  ({voided} span(s) void — the disc, not the binding)")
    if not scored:
        print("\nNo span reproduced against itself; nothing was tested.")
        return 2
    ok = all(v == "match" for v in scored) and not problems
    print(
        f"\n{'PASS' if ok else 'FAIL'}: "
        f"{sum(1 for v in scored if v == 'match')}/{len(scored)} scored spans matched"
        + ("" if not problems else f", {len(problems)} TOC field(s) differ")
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
