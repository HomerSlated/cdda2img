#!/usr/bin/env python3
"""recovery_bench.py — the combined recovery/verification test-bench.

This is *our half* of the two-project bench described in
``docs/reference/accudisc-migration-plan.md`` §8, unified with AccuDisc's half:

* AccuDisc supplies the **relative** signals per ``disc x rung x span`` — C2,
  Q-CRC health, timing, per-sector status map (``read`` machine channel +
  ``--map-file``, ``speeds``).
* cdda2img supplies the **absolute** gate AccuDisc deliberately does not own —
  AccurateRip v1/v2, CTDB (checksums *and*, on our side, parity repair as one
  path), and MB/CDDB Disc-ID resolution.

The bench runs ``accudisc read`` at each recovery rung (R0-R4, a strict-superset
escalation) across a swept speed ladder, gates the resulting audio, and emits one
standardised row (§8.4) per ``disc x rung x span``. From those rows it (a)
classifies each method/combo — blacklist (hard error) / warn (soft error, no or
worse improvement) / keep — and (b) ranks the survivors by the compulsory goal
first: **(i) full data integrity or best-effort-before-hard-failure, then
(ii) speed**. The winning row *is* the derived-optimal profile; the schema is
frozen (AccuDisc §17.1) so a shipped bench can ingest user-submitted rows.

Run from the project root::

    uv run python tools/recovery_bench.py --device /dev/sr0 --label "ZZ Top"

The decision core (rung construction, summary parsing, span clustering,
classification, ranking, row I/O) is pure and unit-tested inline
(``--selftest``). The live-drive orchestration is best-effort and requires a
physical disc; it is wired to the real ``accudisc`` machine interface
(``accudisc/docs/cli-machine-interface.md``) and to :mod:`cdda2img.accuraterip`.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import mmap
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Allow ``uv run python tools/recovery_bench.py`` from the project root even
# when the package is not installed (mirrors the other tools/ scripts).
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_ACCUDISC = str(Path(__file__).resolve().parent / "accudisc" / "accudisc")
if not Path(_ACCUDISC).is_file():
    _ACCUDISC = "accudisc"  # fall back to $PATH

# --- Recovery rungs (AccuDisc §15.3) -------------------------------------------
# Strict-superset escalation: each rung adds one knob. Set-speed is a SEPARATE
# axis (swept orthogonally), NOT baked into a rung; --ladder is the in-rung
# speed-diversity knob and must sit at/below the swept pass speed.
RUNGS: dict[str, dict[str, str]] = {
    "R0": {"retries": "2"},
    "R1": {"retries": "2", "c2-retries": "3"},
    "R2": {"retries": "2", "c2-retries": "3", "verify": "2"},
    "R3": {"retries": "2", "c2-retries": "3", "verify": "2", "overlap": "4"},
    "R4": {
        "retries": "2",
        "c2-retries": "3",
        "verify": "2",
        "overlap": "4",
        "ladder": "8,4",
    },
}

# --map-file low-nibble states (AccuDisc §15.4 / accudisc.h). "Needs recovery"
# is the exact predicate AccuDisc's span-finder uses.
MAP_STATE_NAME = {
    0x0: "PENDING",
    0x1: "OK",
    0x2: "C2",
    0x3: "HARD",
    0x4: "RECOVERED",
    0x5: "SUSPECT",
}
MAP_NEEDS_RECOVERY = frozenset({0x2, 0x3, 0x5})

# Default speed sweep — AccuDisc's bare `speeds` default set. Probe per DISC (the
# governor sets the reachable top from the disc's load-time media scan), so this
# is only a fallback when a live ladder probe is unavailable.
DEFAULT_SPEED_LADDER = (40, 32, 24, 16, 8, 4)

# Q-health gate: a whole-pass ratio below this is a transient collapse to discard
# and re-read, not a real speed characterisation (AccuDisc §15.6).
Q_HEALTH_FLOOR = 0.90


@dataclass
class BenchRow:
    """One standardised bench result (migration-plan §8.4). Row key is
    ``disc_id x rung x span``. AccuDisc columns are relative signals; the
    ``ar_*`` / ``ctdb_*`` / ``discid_green`` columns are our absolute gate."""

    disc_id: str
    drive: str
    rung: str
    span: str = ""  # "start+count"; empty = whole disc
    set_speed: int = 0
    measured_cx: int = 0  # AccuDisc measured throughput, centi-X (ground truth)
    governor_ceiling: int = 0  # page-2A current at load, pre-cap (self-throttle triage)
    subq_ok: int = 0
    subq_total: int = 0
    c2_sectors: int = 0
    recovered_sectors: int = 0
    suspect_sectors: int = 0
    hard_sectors: int = 0
    ar_v1_pass: bool | None = None
    ar_v2_pass: bool | None = None
    ctdb_pass: bool | None = None
    ctdb_repaired: bool | None = None
    discid_green: bool | None = None
    wall_s: float = 0.0

    @property
    def q_health(self) -> float | None:
        return self.subq_ok / self.subq_total if self.subq_total else None


# --- Pure decision core (unit-tested) ------------------------------------------


def rung_read_flags(rung: str, sub: bool, c2: bool) -> list[str]:
    """AccuDisc ``read`` flags for a rung + the sub/no-sub (Axis A) and C2 (Axis F)
    toggles. Only ``--c2-retries`` is intrinsically C2-dependent, so it is dropped
    when C2 is off; the other knobs must function C2-less (AccuDisc §17.3)."""
    if rung not in RUNGS:
        msg = f"unknown rung {rung!r}; known: {', '.join(RUNGS)}"
        raise ValueError(msg)
    flags: list[str] = []
    for knob, val in RUNGS[rung].items():
        if knob == "c2-retries" and not c2:
            continue  # no C2 bitmap → nothing to retry on
        flags += [f"--{knob}", val]
    if sub:
        flags += ["--sub", "raw"]
    if not c2:
        flags += ["--no-c2"]
    return flags


def parse_summary(line: str) -> dict[str, int]:
    """Parse a ``summary key=<n> ...`` line into ``{key: int}``. Token-based, not
    positional — the contract permits appended keys (subq_total/subq_ok/... land
    here at runtime even though the frozen doc example predates them)."""
    out: dict[str, int] = {}
    parts = line.split()
    if not parts or parts[0] != "summary":
        return out
    for tok in parts[1:]:
        if "=" not in tok:
            continue
        key, _, val = tok.partition("=")
        try:
            out[key] = int(val)
        except ValueError:
            continue  # non-integer value (unknown future field) — skip
    return out


def cluster_spans(states: bytes, start_lba: int) -> list[tuple[int, int]]:
    """Cluster contiguous needs-recovery sectors (state in {C2,HARD,SUSPECT}) out
    of a ``--map-file`` byte array into ``(start_lba, count)`` spans."""
    spans: list[tuple[int, int]] = []
    run_start: int | None = None
    for i, b in enumerate(states):
        needs = (b & 0x0F) in MAP_NEEDS_RECOVERY
        if needs and run_start is None:
            run_start = i
        elif not needs and run_start is not None:
            spans.append((start_lba + run_start, i - run_start))
            run_start = None
    if run_start is not None:
        spans.append((start_lba + run_start, len(states) - run_start))
    return spans


def integrity_pass(row: BenchRow) -> bool:
    """The compulsory goal (i): full data integrity, or best-effort-before-failure.

    A transient Q collapse always fails (the capture is untrustworthy). Otherwise
    green if **any verified route** succeeded: raw AR v2, raw CTDB checksums, or a
    CTDB parity rebuild (``repair_whole_disc`` AR-double-gates, so ``ctdb_repaired``
    True means verified-after-repair — this is a recovery *path*, not a failure).
    With no verified route, fail only on **negative** evidence (a gate returned
    False); all-``None`` means the disc is in no database — not measured, so
    best-effort passes (absence of evidence is not failure)."""
    q = row.q_health
    if q is not None and q < Q_HEALTH_FLOOR:
        return False
    if row.ar_v2_pass or row.ctdb_pass or row.ctdb_repaired:
        return True
    return not (row.ar_v2_pass is False or row.ctdb_pass is False)


def classify(rows: list[BenchRow]) -> dict[str, str]:
    """Per-rung verdict across a disc's rows (§8.5): a rung that never functions
    (no summary / all hard) → ``blacklist``; one that functions but yields no
    integrity improvement over a strictly-cheaper rung → ``warn``; else ``keep``.
    Keyed by rung label."""
    verdict: dict[str, str] = {}
    by_rung = {r.rung: r for r in rows}
    ordered = [lbl for lbl in RUNGS if lbl in by_rung]
    best_so_far = False
    for lbl in ordered:
        row = by_rung[lbl]
        functioned = row.subq_total > 0 or row.measured_cx > 0
        if not functioned:
            verdict[lbl] = "blacklist"
            continue
        ok = integrity_pass(row)
        # Soft error: this (more expensive) rung didn't improve on a cheaper one
        # that already passed.
        verdict[lbl] = "warn" if (best_so_far and ok) else "keep"
        best_so_far = best_so_far or ok
    return verdict


def rank(rows: list[BenchRow]) -> list[BenchRow]:
    """Rank pipelines by the compulsory goal then speed (§8.5): integrity-passing
    rows first, and within each group fastest (lowest ``wall_s``) first. The top
    integrity-passing row is the derived-optimal profile for this disc."""
    return sorted(rows, key=lambda r: (not integrity_pass(r), r.wall_s))


def row_to_toml(row: BenchRow) -> str:
    """Serialise a row to a ``[[result]]`` TOML table (the frozen §8.4 schema)."""
    lines = ["[[result]]"]
    for f in dataclasses.fields(row):
        val = getattr(row, f.name)
        if val is None:
            continue  # omit un-measured columns rather than write a null
        if isinstance(val, bool):
            lines.append(f"{f.name} = {str(val).lower()}")
        elif isinstance(val, str):
            lines.append(f'{f.name} = "{val}"')
        else:
            lines.append(f"{f.name} = {val}")
    return "\n".join(lines) + "\n"


# --- Live-drive orchestration (best-effort; needs a physical disc) -------------


def accudisc_speed_current(device: str) -> int:
    """Governor ceiling at load: page-2A ``current`` Nx via ``accudisc speed``.
    A self-throttled ceiling flags disc-wide marginality up front (AccuDisc §5).
    Returns 0 when unavailable."""
    try:
        r = subprocess.run(  # noqa: S603 — snapshot/PATH binary, fixed argv
            [_ACCUDISC, "--device", device, "speed"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return 0
    for line in r.stdout.splitlines():
        if line.startswith("page2A") and "current" in line:
            # "page2A  max 40x (...)  current 32x (...)" — grab the x after current
            after = line.split("current", 1)[1].strip()
            tok = after.split("x", 1)[0]
            try:
                return int(tok)
            except ValueError:
                return 0
    return 0


def probe_ladder(device: str) -> list[int]:
    """Per-disc speed ladder from ``accudisc speeds`` — the authoritative source
    (``measured`` = ground truth). Returns the requested rungs it reported, or the
    default set when unavailable."""
    try:
        r = subprocess.run(  # noqa: S603 — snapshot/PATH binary, fixed argv
            [_ACCUDISC, "--device", device, "speeds"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return list(DEFAULT_SPEED_LADDER)
    reqs: list[int] = []
    for line in r.stdout.splitlines():
        if line.startswith("speed ") and "req=" in line:
            for tok in line.split():
                if tok.startswith("req="):
                    with contextlib.suppress(ValueError):
                        reqs.append(int(tok[4:]))
    return reqs or list(DEFAULT_SPEED_LADDER)


def run_capture(
    device: str,
    out_dir: Path,
    rung: str,
    speed: int,
    sub: bool,
    c2: bool,
) -> tuple[int, dict[str, int], Path, Path]:
    """One ``accudisc read`` pass at (rung, speed, sub, c2). Returns
    ``(returncode, summary_dict, pcm_path, map_path)``. The summary tokens are
    read off ``--progress-fd 1``; stderr is discarded here (the bench cares about
    the machine channel, not the human log)."""
    pcm = out_dir / f"{rung}_{speed}x.pcm"
    c2f = out_dir / f"{rung}_{speed}x.c2"
    mapf = out_dir / f"{rung}_{speed}x.map"
    cmd = [
        _ACCUDISC,
        "--device",
        device,
        "read",
        "--pcm",
        str(pcm),
        "--c2f",
        str(c2f),
        "--map-file",
        str(mapf),
        "--speed",
        str(speed),
        "-q",
        "--progress-fd",
        "1",
        *rung_read_flags(rung, sub=sub, c2=c2),
    ]
    summary: dict[str, int] = {}
    proc = subprocess.Popen(  # noqa: S603 — snapshot/PATH binary, fixed argv
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert proc.stdout is not None  # noqa: S101
    for line in proc.stdout:
        if line.startswith("summary"):
            summary = parse_summary(line)
    proc.wait()
    return proc.returncode, summary, pcm, mapf


def read_map_spans(map_path: Path, start_lba: int) -> list[tuple[int, int]]:
    """mmap a ``--map-file`` and cluster its needs-recovery spans (§15.4)."""
    if not map_path.is_file() or map_path.stat().st_size == 0:
        return []
    with open(map_path, "rb") as f:
        m = mmap.mmap(f.fileno(), 0, prot=mmap.PROT_READ)
        try:
            return cluster_spans(bytes(m), start_lba)
        finally:
            m.close()


def gate_accuraterip(
    pcm: Path,
    track_lsns: list[int],
    disc_last_lsn: int,
    read_offset: int,
    cddb_id: int,
) -> tuple[bool | None, bool | None]:
    """Whole-disc AR gate via :func:`cdda2img.accuraterip.verify_rip`. Returns
    ``(ar_v1_pass, ar_v2_pass)``: True/False when the disc is in the DB, ``None``
    when it is not (absence of evidence, not a failure)."""
    from cdda2img.accuraterip import verify_rip

    res = verify_rip(pcm, track_lsns, disc_last_lsn, read_offset, cddb_id)
    in_db = [t for t in res.tracks if t.max_confidence is not None]
    if not in_db:
        return None, None
    return (
        all(t.confidence_v1 is not None for t in in_db),
        all(t.confidence_v2 is not None for t in in_db),
    )


def gate_ctdb(
    pcm: Path,
    track_lsns: list[int],
    disc_last_lsn: int,
    cddb_id: int,
    read_offset: int,
    c2_path: Path | None = None,
) -> tuple[bool | None, bool | None]:
    """CTDB gate — ``(ctdb_pass, ctdb_repaired)``, both from
    :mod:`cdda2img.ctdb_repair`.

    * **ctdb_pass** — this rip's per-track CRC32s reconcile with a CTDB entry and
      every verifiable track matches (checksums only, no repair). ``None`` = disc
      not in CTDB, or the lookup failed (not-measured, non-blocking); ``False`` =
      in CTDB but a track mismatches or no entry reconciles; ``True`` = all match.
    * **ctdb_repaired** — attempted only when ``ctdb_pass`` is False (there is
      damage to mend). Runs ``repair_whole_disc`` on a **copy** — non-destructive,
      the capture is left intact — C2-erasure-assisted when a bitmap is present,
      and AR-double-gated by ``repair_whole_disc`` itself. ``None`` when not
      attempted."""
    from xml.etree.ElementTree import ParseError

    from cdda2img.ctdb_repair import load_entries, repair_whole_disc, select_entry

    bounds = [*track_lsns, disc_last_lsn + 1]
    n = len(track_lsns)
    cache = pcm.parent
    try:
        entries = load_entries(bounds, n, xml_cache=cache / "ctdb.xml")
    except (OSError, ParseError) as exc:
        print(f"# ctdb: lookup failed ({exc})")
        return None, None
    if not entries:
        return None, None  # disc not in CTDB — non-blocking
    sel = select_entry(pcm.read_bytes(), entries, bounds, n)
    if sel is not None and not sel.damaged:
        return True, None  # all verifiable tracks match — checksum pass

    # ctdb_pass is False (a track mismatches, or no entry reconciles): try a
    # non-destructive parity repair on a copy so the capture stays untouched.
    tmp = pcm.with_suffix(".ctdbtry.pcm")
    try:
        shutil.copyfile(pcm, tmp)
        res = repair_whole_disc(
            tmp,
            track_lsns,
            disc_last_lsn,
            cddb_id,
            read_offset,
            c2_path=c2_path if (c2_path and c2_path.exists()) else None,
            cache_dir=cache,
        )
    finally:
        tmp.unlink(missing_ok=True)
    return False, res.repaired


def geometry_from_fulltoc(
    fulltoc_raw: bytes,
) -> tuple[list[int], int, int] | None:
    """Pure: raw fulltoc bytes → ``(track_lsns, disc_last_lsn, cddb_id)``, or None
    when the disc is outside the CD-DA archival model (no session-1 lead-out, etc.).

    Boundaries are TOC-authoritative (``subq_toc.build_rip_info`` takes track
    starts and the lead-out from the full TOC; the Q stream only supplies
    pregaps/ISRC, which do not move AR/CDDB disc IDs), so an **empty** sub is
    sufficient here — it degrades pregaps to defaults with a warning while leaving
    ``track_lsns``/``disc_last_lsn`` exact."""
    from cdda2img.cddb import compute_cddb_disc_id
    from cdda2img.subq_toc import build_rip_info

    try:
        info = build_rip_info(fulltoc_raw, b"")
    except ValueError:
        return None
    cddb_id = int(compute_cddb_disc_id(info.track_lsns, info.disc_last_lsn), 16)
    return info.track_lsns, info.disc_last_lsn, cddb_id


def capture_geometry(
    device: str,
    out_dir: Path,
) -> tuple[list[int], int, int] | None:
    """Acquire disc geometry for the AR/CTDB gate from a cheap standalone
    ``fulltoc`` (lead-in only, no full spin), then :func:`geometry_from_fulltoc`.
    Disc-invariant — acquire once per disc."""
    ftoc = out_dir / "geometry.fulltoc"
    try:
        r = subprocess.run(  # noqa: S603 — snapshot/PATH binary, fixed argv
            [_ACCUDISC, "--device", device, "fulltoc", str(ftoc)],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        print(f"# geometry: fulltoc capture failed ({exc})")
        return None
    if r.returncode not in (0, 3) or not ftoc.is_file():
        print(f"# geometry: fulltoc unavailable (exit {r.returncode})")
        return None
    geom = geometry_from_fulltoc(ftoc.read_bytes())
    if geom is None:
        print("# geometry: disc not in CD-DA archival model")
    return geom


# --- Self-test (no drive needed) -----------------------------------------------


def _check(cond: bool, msg: str) -> None:
    """Test assertion that survives ``python -O`` (unlike ``assert``, which is
    also disallowed by ruff S101 outside ``tests/``)."""
    if not cond:
        raise AssertionError(msg)


def _selftest() -> int:
    # rung construction: C2 off drops --c2-retries but keeps the rest
    f = rung_read_flags("R4", sub=True, c2=True)
    _check("--c2-retries" in f and "--sub" in f and "--ladder" in f, str(f))
    g = rung_read_flags("R4", sub=True, c2=False)
    _check("--c2-retries" not in g and "--no-c2" in g and "--overlap" in g, str(g))

    # summary token parse (with an appended future key + a non-int guard)
    s = parse_summary("summary hard=3 c2=10 subq_ok=980 subq_total=1000 mode=x")
    _check(s == {"hard": 3, "c2": 10, "subq_ok": 980, "subq_total": 1000}, str(s))
    _check(parse_summary("progress 5 10") == {}, "non-summary line must parse empty")

    # span clustering: state low-nibble, needs-recovery = {2,3,5}
    states = bytes([0x1, 0x2, 0x12, 0x1, 0x3, 0x3, 0x1, 0x5])
    spans = cluster_spans(states, 100)
    _check(spans == [(101, 2), (104, 2), (107, 1)], str(spans))

    # integrity gate: v2 fail blocks; transient Q collapse blocks; None is ok
    _check(not integrity_pass(BenchRow("d", "drv", "R0", ar_v2_pass=False)), "v2 fail")
    _check(
        not integrity_pass(BenchRow("d", "drv", "R0", subq_ok=470, subq_total=1000)),
        "transient Q collapse must block",
    )
    _check(
        integrity_pass(
            BenchRow("d", "drv", "R0", ar_v2_pass=True, subq_ok=980, subq_total=1000)
        ),
        "clean row must pass",
    )
    # the live Tracy case: raw AR failed but CTDB parity rebuilt it → integrity green
    _check(
        integrity_pass(
            BenchRow(
                "d",
                "drv",
                "R0",
                ar_v2_pass=False,
                ctdb_pass=False,
                ctdb_repaired=True,
                subq_ok=980,
                subq_total=1000,
            )
        ),
        "parity-repaired row must pass despite raw AR fail",
    )

    # ranking: integrity-passing rows first, then fastest
    slow_ok = BenchRow(
        "d", "drv", "R3", ar_v2_pass=True, wall_s=200, subq_ok=98, subq_total=100
    )
    fast_ok = BenchRow(
        "d", "drv", "R0", ar_v2_pass=True, wall_s=90, subq_ok=98, subq_total=100
    )
    bad = BenchRow("d", "drv", "R1", ar_v2_pass=False, wall_s=50)
    order = [r.rung for r in rank([slow_ok, bad, fast_ok])]
    _check(order == ["R0", "R3", "R1"], str(order))

    # classify: dead rung blacklisted, redundant improvement warned
    dead = BenchRow("d", "drv", "R0")  # no summary, no throughput
    good = BenchRow("d", "drv", "R1", ar_v2_pass=True, subq_ok=98, subq_total=100)
    redun = BenchRow("d", "drv", "R2", ar_v2_pass=True, subq_ok=98, subq_total=100)
    v = classify([dead, good, redun])
    _check(v == {"R0": "blacklist", "R1": "keep", "R2": "warn"}, str(v))

    # row round-trips to TOML without nulls
    toml = row_to_toml(good)
    _check("[[result]]" in toml and "ar_v2_pass = true" in toml, "toml body")
    _check("ctdb_pass" not in toml, "None columns must be omitted")

    # geometry: build (track_lsns, lead-out, cddb_id) from a real Tracy Chapman
    # fulltoc capture with an empty sub — proves the wired geometry path.
    fixture = _SRC.parent / "tests" / "fixtures" / "tracy.fulltoc"
    if fixture.is_file():
        geom = geometry_from_fulltoc(fixture.read_bytes())
        if geom is None:  # also narrows the type for the unpack below
            msg = "tracy.fulltoc must build geometry"
            raise AssertionError(msg)
        lsns, last, cddb = geom
        _check(lsns[0] == 0 and last == 162891, f"tracy geometry {lsns[:2]} {last}")
        _check(cddb == 0x99087B0B, f"tracy cddb {cddb:08x}")

    print("recovery_bench selftest: OK")
    return 0


# --- CLI -----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument(
        "--selftest",
        action="store_true",
        help="run the pure-logic self-test and exit (no drive)",
    )
    ap.add_argument("--device", default="/dev/sr0")
    ap.add_argument("--label", default="disc", help="human disc label for the report")
    ap.add_argument(
        "--rungs", default="R0,R1,R2,R3,R4", help="comma-separated rungs to run"
    )
    ap.add_argument(
        "--speeds",
        default="",
        help="comma-separated Nx sweep (default: probe per disc)",
    )
    ap.add_argument(
        "--sub",
        action="store_true",
        default=True,
        help="capture subchannel (full PROV; default on)",
    )
    ap.add_argument("--no-sub", dest="sub", action="store_false")
    ap.add_argument(
        "--c2",
        action="store_true",
        default=True,
        help="capture C2 pointers (default on)",
    )
    ap.add_argument("--no-c2", dest="c2", action="store_false")
    ap.add_argument(
        "--read-offset",
        type=int,
        default=0,
        help="drive read offset in samples for the AR gate (e.g. +30 for PX-716A)",
    )
    ap.add_argument("--out", default="rips/recovery_bench.toml")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    out_dir = Path(args.out).resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)
    speeds = (
        [int(x) for x in args.speeds.split(",")]
        if args.speeds
        else probe_ladder(args.device)
    )
    governor = accudisc_speed_current(args.device)
    rungs = [r.strip() for r in args.rungs.split(",") if r.strip()]

    print(
        f"# bench: {args.label} on {args.device} "
        f"(governor {governor}x, speeds {speeds}, rungs {rungs})"
    )

    # Disc geometry (track_lsns / lead-out / cddb_id) is disc-invariant — acquire
    # once, from a cheap lead-in fulltoc, and feed every rung's AR gate.
    geom = capture_geometry(args.device, out_dir)
    if geom is None:
        print(
            "# geometry unavailable — AR gate skipped (rows carry relative signals only)"
        )
    else:
        track_lsns, disc_last_lsn, cddb_id = geom
        print(
            f"# geometry: {len(track_lsns)} tracks, lead-out lsn {disc_last_lsn}, "
            f"cddb {cddb_id:08x}, read-offset {args.read_offset:+d}"
        )

    rows: list[BenchRow] = []
    for speed in speeds:
        for rung in rungs:
            t0 = time.monotonic()
            rc, summary, pcm, mapf = run_capture(
                args.device,
                out_dir,
                rung,
                speed,
                sub=args.sub,
                c2=args.c2,
            )
            wall = time.monotonic() - t0
            row = BenchRow(
                disc_id=(f"{cddb_id:08x}" if geom else args.label),
                drive=args.device,
                rung=rung,
                set_speed=speed,
                governor_ceiling=governor,
                subq_ok=summary.get("subq_ok", 0),
                subq_total=summary.get("subq_total", 0),
                c2_sectors=summary.get("c2", 0),
                recovered_sectors=summary.get("recovered", 0),
                suspect_sectors=summary.get("suspect", 0),
                hard_sectors=summary.get("hard", 0),
                wall_s=round(wall, 1),
            )
            if geom is not None and pcm.is_file():
                row.ar_v1_pass, row.ar_v2_pass = gate_accuraterip(
                    pcm, track_lsns, disc_last_lsn, args.read_offset, cddb_id
                )
                c2f = (out_dir / f"{rung}_{speed}x.c2") if args.c2 else None
                row.ctdb_pass, row.ctdb_repaired = gate_ctdb(
                    pcm, track_lsns, disc_last_lsn, cddb_id, args.read_offset, c2f
                )
            rows.append(row)
            spans = read_map_spans(mapf, start_lba=0)
            print(
                f"  {rung} @ {speed}x  exit={rc}  q={row.q_health}  "
                f"c2={row.c2_sectors}  spans={len(spans)}  "
                f"ar_v2={row.ar_v2_pass}  ctdb={row.ctdb_pass}/{row.ctdb_repaired}"
                f"  {wall:.1f}s"
            )

    Path(args.out).write_text("".join(row_to_toml(r) for r in rank(rows)))
    print(f"# wrote {len(rows)} rows → {args.out}")
    print(f"# classify: {classify(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
