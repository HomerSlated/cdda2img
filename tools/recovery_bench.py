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
import tempfile
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


def rung_recovery_flags(rung: str, c2: bool) -> list[str]:
    """The recovery knobs for a rung (retries/c2-retries/verify/overlap/ladder).

    These are the escalating re-read aggression applied to a **flagged span only**,
    never whole-disc: ``--verify``/``--overlap``/``--ladder`` are cache-defeated
    (each re-read drags a ~5000-sector flush), so whole-disc they blow up to hours;
    scoped to a span the flush cost is amortized (AccuDisc §25.1). ``--c2-retries``
    is dropped when C2 is off — nothing to retry on (§17.3)."""
    if rung not in RUNGS:
        msg = f"unknown rung {rung!r}; known: {', '.join(RUNGS)}"
        raise ValueError(msg)
    flags: list[str] = []
    for knob, val in RUNGS[rung].items():
        if knob == "c2-retries" and not c2:
            continue  # no C2 bitmap -> nothing to retry on
        flags += [f"--{knob}", val]
    return flags


def capture_flags(sub: bool, c2: bool) -> list[str]:
    """Capture-stream toggles (Axis A sub/no-sub, Axis F C2 on/off) — separate from
    the recovery knobs so a baseline whole-disc capture carries no recovery flags."""
    flags: list[str] = []
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
    → ``blacklist``; one that functions but yields no integrity improvement over a
    strictly-cheaper rung → ``warn``; else ``keep``. Keyed by rung label.

    A rung "functioned" if it produced a usable result — a whole-disc capture
    (``subq_total``/``measured_cx``) *or* a span-scoped recovery (``span`` set,
    including a legitimate ``skipped`` no-op on a clean disc). Only a rung that
    yields nothing at all is blacklisted."""
    verdict: dict[str, str] = {}
    by_rung = {r.rung: r for r in rows}
    ordered = [lbl for lbl in RUNGS if lbl in by_rung]
    best_so_far = False
    for lbl in ordered:
        row = by_rung[lbl]
        functioned = bool(row.span) or row.subq_total > 0 or row.measured_cx > 0
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


def _parse_speed_line(stdout: str) -> tuple[int, int]:
    """``(max_x, current_x)`` from an ``accudisc speed`` page-2A line; ``(0, 0)``
    if absent. Line form: ``page2A  max 40x (...)  current 32x (...)``."""

    def field(line: str, label: str) -> int:
        if label not in line:
            return 0
        tok = line.split(label, 1)[1].strip().split("x", 1)[0]
        try:
            return int(tok)
        except ValueError:
            return 0

    for line in stdout.splitlines():
        if line.startswith("page2A"):
            return field(line, "max "), field(line, "current ")
    return 0, 0


def set_speed_to_max(device: str) -> int:
    """Request the drive's **page-2A max** (a valid, in-range value) and return the
    resulting page-2A ``current`` — the drive clamps to its governor, so this both
    reveals the governor ceiling *and* clears any residual speed a prior run left.

    Requesting an **out-of-range** value (e.g. 99, above a 40x max) is *rejected*
    by the drive, leaving the stale residual — the bug that misread ABBA's 32x
    governor as a stuck 8x carried over from the un-restored ZZ Top run. Always
    request the reported max. Returns 0 when unavailable."""
    try:
        rep = subprocess.run(  # noqa: S603 — snapshot/PATH binary, fixed argv
            [_ACCUDISC, "--device", device, "speed"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return 0
    maxx, _ = _parse_speed_line(rep.stdout)
    if not maxx:
        return 0
    try:
        r = subprocess.run(  # noqa: S603 — snapshot/PATH binary, fixed argv
            [_ACCUDISC, "--device", device, "speed", str(maxx)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return 0
    _, cur = _parse_speed_line(r.stdout)
    return cur


def probe_ladder(device: str) -> list[int]:
    """The drive's **distinct achievable** read speeds for the loaded disc, from
    ``accudisc speeds`` (timed reads — ground truth, and they warm the disc so the
    governor settles to its true ceiling). Descending; default set when empty.

    The drive quantizes requests to discrete accepted ceilings and caps at its
    governor, so several requests collapse to one speed (ABBA req 40/32 -> 32x).
    Dedupe on the accepted ceiling (``page2a``) so the matrix sweeps each real
    speed once, not redundant requests that all deliver the same rate. The highest
    entry is the governor ceiling."""
    try:
        r = subprocess.run(  # noqa: S603 — snapshot/PATH binary, fixed argv
            [_ACCUDISC, "--device", device, "speeds"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return list(DEFAULT_SPEED_LADDER)
    achieved: set[int] = set()
    for line in r.stdout.splitlines():
        if line.startswith("speed ") and "page2a=" in line:
            for tok in line.split():
                if tok.startswith("page2a="):
                    with contextlib.suppress(ValueError):
                        v = int(tok[7:])
                        if v:
                            achieved.add(v)
    return sorted(achieved, reverse=True) or list(DEFAULT_SPEED_LADDER)


def run_read(
    device: str,
    out_dir: Path,
    tag: str,
    speed: int,
    sub: bool,
    c2: bool,
    *,
    rung: str | None = None,
    start: int | None = None,
    count: int | None = None,
) -> tuple[int, dict[str, int], Path, Path]:
    """One ``accudisc read`` -> ``(returncode, summary, pcm_path, map_path)``.

    Whole-disc when *start* is None (the baseline capture — capture-stream flags
    only, no recovery knobs); a bounded ``--start/--count`` span otherwise. A
    *rung* adds that rung's recovery knobs (span reads only — never whole-disc,
    §25.1). Summary tokens are read off ``--progress-fd 1``; stderr is discarded."""
    pcm = out_dir / f"{tag}.pcm"
    mapf = out_dir / f"{tag}.map"
    cmd = [_ACCUDISC, "--device", device, "read", "--pcm", str(pcm)]
    if c2:
        cmd += ["--c2f", str(out_dir / f"{tag}.c2")]
    cmd += ["--map-file", str(mapf), "--speed", str(speed)]
    if start is not None:
        cmd += ["--start", str(start), "--count", str(count)]
    cmd += capture_flags(sub, c2)
    if rung is not None:
        cmd += rung_recovery_flags(rung, c2)
    cmd += ["-q", "--progress-fd", "1"]
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


def splice_span(dst_pcm: Path, span_pcm: Path, start_lba: int) -> None:
    """Overwrite ``dst_pcm``'s ``[start_lba, ...)`` region with ``span_pcm``'s raw
    s16le bytes (2352 B/sector) in place — sample-exact, neighbours untouched
    (mirrors ``_recover_failed_tracks``'s splice, raw domain both sides)."""
    data = span_pcm.read_bytes()
    with open(dst_pcm, "r+b") as f:
        f.seek(start_lba * 2352)
        f.write(data)


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


# --- Matrix orchestration (baseline capture + span-targeted recovery) ----------


def _mk_row(
    disc_id: str,
    drive: str,
    rung: str,
    speed: int,
    governor: int,
    summary: dict[str, int],
    span: str = "",
) -> BenchRow:
    """Build a row from an accudisc read ``summary`` (relative signals only; the
    AR/CTDB columns are filled later by :func:`_gate_row`)."""
    return BenchRow(
        disc_id=disc_id,
        drive=drive,
        rung=rung,
        span=span,
        set_speed=speed,
        governor_ceiling=governor,
        subq_ok=summary.get("subq_ok", 0),
        subq_total=summary.get("subq_total", 0),
        c2_sectors=summary.get("c2", 0),
        recovered_sectors=summary.get("recovered", 0),
        suspect_sectors=summary.get("suspect", 0),
        hard_sectors=summary.get("hard", 0),
    )


def _gate_row(
    row: BenchRow,
    pcm: Path,
    c2_path: Path | None,
    geom: tuple[list[int], int, int] | None,
    read_offset: int,
) -> None:
    """Fill a row's AR/CTDB columns from a whole-disc PCM. No-op without geometry."""
    if geom is None or not pcm.is_file():
        return
    track_lsns, disc_last_lsn, cddb_id = geom
    row.ar_v1_pass, row.ar_v2_pass = gate_accuraterip(
        pcm, track_lsns, disc_last_lsn, read_offset, cddb_id
    )
    row.ctdb_pass, row.ctdb_repaired = gate_ctdb(
        pcm, track_lsns, disc_last_lsn, cddb_id, read_offset, c2_path
    )


def _recover_rung(
    args: argparse.Namespace,
    geom: tuple[list[int], int, int] | None,
    governor: int,
    disc_id: str,
    speed: int,
    rung: str,
    spans: list[tuple[int, int]],
    base_pcm: Path,
    out_dir: Path,
) -> BenchRow:
    """Apply one recovery rung to the flagged spans: re-read each span with the
    rung's knobs, splice into a copy of the baseline PCM, re-gate whole-disc.
    Span-scoped by construction — the cache-defeated knobs never touch the whole
    disc (§25.1)."""
    spliced = out_dir / f"{rung}_{speed}x.spliced.pcm"
    shutil.copyfile(base_pcm, spliced)
    t0 = time.monotonic()
    recovered = c2res = 0
    for i, (lba, cnt) in enumerate(spans):
        stag = f"{rung}_{speed}x_s{i}"
        _rc, ssum, spcm, _sm = run_read(
            args.device,
            out_dir,
            stag,
            speed,
            sub=args.sub,
            c2=args.c2,
            rung=rung,
            start=lba,
            count=cnt,
        )
        splice_span(spliced, spcm, lba)
        recovered += ssum.get("recovered", 0)
        c2res += ssum.get("c2", 0)
        for ext in (".pcm", ".c2", ".map"):
            (out_dir / f"{stag}{ext}").unlink(missing_ok=True)
    span_str = ",".join(f"{lba}+{cnt}" for lba, cnt in spans)
    row = _mk_row(
        disc_id,
        args.device,
        rung,
        speed,
        governor,
        {"c2": c2res, "recovered": recovered},
        span=span_str,
    )
    row.wall_s = round(time.monotonic() - t0, 1)
    _gate_row(row, spliced, None, geom, args.read_offset)
    if not args.keep_captures:
        spliced.unlink(missing_ok=True)
    return row


def run_matrix(
    args: argparse.Namespace,
    geom: tuple[list[int], int, int] | None,
    governor: int,
    speeds: list[int],
    rungs: list[str],
    out_dir: Path,
) -> list[BenchRow]:
    """The bench matrix: per speed, one whole-disc baseline capture, then the
    recovery rungs applied only to the map-flagged spans (skipped when the disc is
    clean, §25.3). Rewrites the ranked report after every row (crash-safe over a
    long run) and drops the big disposable captures unless ``--keep-captures``."""
    disc_id = f"{geom[2]:08x}" if geom else args.label
    out_path = Path(args.out)
    rows: list[BenchRow] = []

    def emit() -> None:
        out_path.write_text("".join(row_to_toml(r) for r in rank(rows)))

    for speed in speeds:
        tag = f"base_{speed}x"
        t0 = time.monotonic()
        rc, summary, base_pcm, base_map = run_read(
            args.device, out_dir, tag, speed, sub=args.sub, c2=args.c2
        )
        base = _mk_row(disc_id, args.device, "baseline", speed, governor, summary)
        base.wall_s = round(time.monotonic() - t0, 1)
        _gate_row(
            base,
            base_pcm,
            (out_dir / f"{tag}.c2") if args.c2 else None,
            geom,
            args.read_offset,
        )
        rows.append(base)
        emit()
        spans = read_map_spans(base_map, start_lba=0)
        print(
            f"  baseline @ {speed}x  exit={rc}  q={base.q_health}  "
            f"c2={base.c2_sectors}  spans={len(spans)}  ar_v2={base.ar_v2_pass}  "
            f"ctdb={base.ctdb_pass}/{base.ctdb_repaired}  {base.wall_s}s",
            flush=True,
        )
        for rung in rungs:
            if not spans:
                skip = _mk_row(
                    disc_id, args.device, rung, speed, governor, {}, span="skipped"
                )
                skip.subq_ok, skip.subq_total = base.subq_ok, base.subq_total
                skip.ar_v1_pass, skip.ar_v2_pass = base.ar_v1_pass, base.ar_v2_pass
                skip.ctdb_pass, skip.ctdb_repaired = base.ctdb_pass, base.ctdb_repaired
                rows.append(skip)
                print(f"  {rung} @ {speed}x  skipped (0 flagged spans)", flush=True)
                continue
            row = _recover_rung(
                args, geom, governor, disc_id, speed, rung, spans, base_pcm, out_dir
            )
            rows.append(row)
            emit()
            print(
                f"  {rung} @ {speed}x  spans={len(spans)}  "
                f"recovered={row.recovered_sectors}  c2={row.c2_sectors}  "
                f"ar_v2={row.ar_v2_pass}  ctdb={row.ctdb_pass}/{row.ctdb_repaired}  "
                f"{row.wall_s}s",
                flush=True,
            )
        if not args.keep_captures:
            base_pcm.unlink(missing_ok=True)
            (out_dir / f"{tag}.c2").unlink(missing_ok=True)
            base_map.unlink(missing_ok=True)
    return rows


# --- Self-test (no drive needed) -----------------------------------------------


def _check(cond: bool, msg: str) -> None:
    """Test assertion that survives ``python -O`` (unlike ``assert``, which is
    also disallowed by ruff S101 outside ``tests/``)."""
    if not cond:
        raise AssertionError(msg)


def _selftest() -> int:
    # rung recovery knobs: C2 off drops only --c2-retries; no capture flags here
    f = rung_recovery_flags("R4", c2=True)
    _check("--c2-retries" in f and "--ladder" in f and "--sub" not in f, str(f))
    g = rung_recovery_flags("R4", c2=False)
    _check("--c2-retries" not in g and "--overlap" in g, str(g))
    # capture flags are separate (sub/no-sub, C2 on/off)
    _check(
        capture_flags(sub=True, c2=False) == ["--sub", "raw", "--no-c2"], "cap flags"
    )
    _check(capture_flags(sub=False, c2=True) == [], "cap flags default")

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

    # splice: overwrite one sector in place, neighbours byte-for-byte untouched
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "b.pcm"
        span = Path(td) / "s.pcm"
        base.write_bytes(b"\x00" * (2352 * 4))  # 4 clean sectors
        span.write_bytes(b"\xff" * 2352)  # 1 recovered sector
        splice_span(base, span, 2)  # replace sector index 2
        out = base.read_bytes()
        _check(out[: 2352 * 2] == b"\x00" * (2352 * 2), "pre-span untouched")
        _check(out[2352 * 2 : 2352 * 3] == b"\xff" * 2352, "span written")
        _check(out[2352 * 3 :] == b"\x00" * 2352, "post-span untouched")

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
    ap.add_argument(
        "--keep-captures",
        action="store_true",
        help="keep per-rung PCM/C2 captures (default: delete after gating; a full "
        "matrix is ~13 GB of disposable PCM per disc otherwise)",
    )
    ap.add_argument("--out", default="rips/recovery_bench.toml")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    out_dir = Path(args.out).resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)
    # Clear any residual speed a prior run/disc left (a valid max request — an
    # un-restored slow drive from the previous disc otherwise contaminates this
    # one, e.g. ZZ Top's 4x carrying into ABBA and masking its 32x governor).
    set_speed_to_max(args.device)
    # The ladder comes from a warm `speeds` probe (distinct achievable speeds); the
    # governor ceiling is simply its highest entry (max achievable). The probe's
    # real reads spin the disc, so the governor settles to its true value.
    speeds = (
        [int(x) for x in args.speeds.split(",")]
        if args.speeds
        else probe_ladder(args.device)
    )
    governor = speeds[0] if speeds else 0
    rungs = [r.strip() for r in args.rungs.split(",") if r.strip()]

    print(
        f"# bench: {args.label} on {args.device} "
        f"(governor {governor}x, speeds {speeds}, rungs {rungs})"
    )

    # Disc geometry (track_lsns / lead-out / cddb_id) is disc-invariant — acquire
    # once, from a cheap lead-in fulltoc, and feed every row's AR/CTDB gate.
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

    rows = run_matrix(args, geom, governor, speeds, rungs, out_dir)
    # Courtesy restore for the next disc/consumer — the ceiling persists across
    # handles (§15.1.3), so a slow last rung would otherwise carry over.
    set_speed_to_max(args.device)
    print(f"# wrote {len(rows)} rows → {args.out}")
    print(f"# classify: {classify(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
