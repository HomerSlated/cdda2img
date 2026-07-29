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
import hashlib
import mmap
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
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

# The CTDB parity family — a separate recovery exit from the R0-R4 audio ladder
# (zero extra reads; §25 first-exit). Ordered cheapest-capability-first for
# classification: error-only (``ctdb-noc2``, the no-C2-drive stand-in) before
# C2-erasure-assisted (``ctdb``), so a disc that error-only already repairs marks
# the C2-assisted rung redundant (``warn``) rather than the reverse. Classified as
# its own ``best_so_far`` chain, never cross-warned against the audio ladder —
# the two are alternative exits, both worth keeping in a per-disc profile.
CTDB_RUNGS: tuple[str, ...] = ("ctdb-noc2", "ctdb")

# Q-yield floor below which a whole pass is treated as a transient disturbed spin
# rather than that speed's real yield (AccuDisc 2026-07-18, §15.6).
_Q_TRANSIENT_FLOOR = 0.90

# --map-file low-nibble states (AccuDisc §15.4 / accudisc.h).
#
# The high nibble is NOT comparable across states (AccuDisc §bh.3, 2026-07-26):
# C2 is log2(fired bits), SUSPECT is log2(differing bytes), RECOVERED is a raw
# reread count. Never build a severity ramp spanning states.
MAP_STATE_NAME = {
    0x0: "PENDING",
    0x1: "OK",
    0x2: "C2",
    0x3: "HARD",
    0x4: "RECOVERED",
    0x5: "SUSPECT",
}
# Our predicate, ours alone. It used to claim to be "the exact predicate AccuDisc's
# span-finder uses" — there is no such predicate on their side (§bh.4, 2026-07-26);
# their only map consumer is the CLI glyph renderer, which ranks RECOVERED above OK.
#
# RECOVERED (0x4) is deliberately excluded here because the bench asks "what still
# needs a recovery pass", and a recovered sector has had one. That is NOT a claim
# that it is correct: on the 2026-07-26 five-speed Tracy set, 9 of 9 sectors that
# came back wrong sat one index below a RECOVERED flag (§89.5).
#
# And OK (0x1) is the weaker state, not the stronger one: all 9 of those corrupt
# sectors were themselves marked OK (§94). A silent misread at a fixed speed is
# re-read at the same speed by the verify pass, returns identical bad bytes, and
# is "confirmed" — AccuDisc §bl.1. **No map state means "these bytes are right."**
# Only an absolute gate (AccurateRip / CTDB) answers that question.
MAP_NEEDS_RECOVERY = frozenset({0x2, 0x3, 0x5})

# Default speed sweep — AccuDisc's bare `speeds` default set. Probe per DISC (the
# governor sets the reachable top from the disc's load-time media scan), so this
# is only a fallback when a live ladder probe is unavailable.
DEFAULT_SPEED_LADDER = (40, 32, 24, 16, 8, 4)

# Q-health gate: a whole-pass ratio below this is a transient collapse to discard
# and re-read, not a real speed characterisation (AccuDisc §15.6).
Q_HEALTH_FLOOR = 0.90

# Process names watched for the resource sampler (external subprocesses this
# bench spawns and cares about — accudisc's own read/repair CPU, ctanalyse's
# Reed-Solomon decode). Matched against /proc/<pid>/comm (15-char truncated
# kernel form), not the full argv.
WATCHED_PROC_NAMES = ("accudisc", "ctanalyse")

# Resource sampler tick interval (seconds) — frequent enough to catch which
# stage is pegging CPU/RAM, sparse enough that a long run's log stays readable.
PROGRESS_SAMPLE_INTERVAL = 3.0

# Live sector-progress print throttle (seconds) — AccuDisc emits progress
# tokens up to 4 Hz; that's too dense for a tee'd log over a multi-minute read.
PROGRESS_PRINT_THROTTLE = 2.0


# --- Fine-grained progress reporting (§ live stage/resource tracking) ---------


def _read_pid_rss_mb(proc_dir: Path) -> float:
    """``VmRSS`` from ``/proc/<pid>/status``, in MB (0.0 if unreadable/absent)."""
    try:
        status = (proc_dir / "status").read_text()
    except OSError:
        return 0.0
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024  # kB -> MB
    return 0.0


def _read_pid_cpu_ticks(proc_dir: Path) -> int | None:
    """``utime + stime`` (clock ticks) from ``/proc/<pid>/stat``, None if unreadable.

    Fields after ``(comm)`` start at 3 (state); utime/stime are 14/15, so index
    11/12 once split past the comm-closing ``)`` (``rsplit`` handles a comm that
    itself contains ``)``, per proc(5))."""
    try:
        stat = (proc_dir / "stat").read_text()
    except OSError:
        return None
    rest = stat.rsplit(")", 1)[1].split()
    return int(rest[11]) + int(rest[12])


def _watched_proc_stats(
    names: tuple[str, ...], prev: dict[int, tuple[int, float]]
) -> tuple[float, float, list[str]]:
    """Sum RSS (MB) and CPU% across every live process whose ``comm`` is in
    *names*, via ``/proc`` directly (no psutil dependency, matches this
    project's subprocess-first style). CPU% needs a wall-time delta, so *prev*
    (``pid -> (cpu_ticks, wall_time)``) is mutated in place across calls; a
    pid's first sample reports 0% until the next tick. Stale pids (process
    exited) are pruned from *prev* each call. Returns ``(rss_mb, cpu_pct,
    matched_comm_names)`` — empty/zero when nothing is running, never raises
    (a process can vanish between listing and reading; that pid is just
    skipped)."""
    hz = os.sysconf("SC_CLK_TCK")
    now = time.monotonic()
    total_rss = 0.0
    total_cpu = 0.0
    seen: set[int] = set()
    found: list[str] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            comm = (entry / "comm").read_text().strip()
        except OSError:
            continue
        if comm not in names:
            continue
        seen.add(pid)
        found.append(comm)
        total_rss += _read_pid_rss_mb(entry)
        cpu_ticks = _read_pid_cpu_ticks(entry)
        if cpu_ticks is None:
            continue
        prev_ticks, prev_wall = prev.get(pid, (cpu_ticks, now))
        dt = now - prev_wall
        if dt > 0:
            total_cpu += (cpu_ticks - prev_ticks) / hz / dt * 100
        prev[pid] = (cpu_ticks, now)
    for pid in list(prev):
        if pid not in seen:
            del prev[pid]
    return total_rss, total_cpu, found


class BenchProgress:
    """Fine-grained progress reporting for a matrix run: which cell (a/b) is
    running, which stage within it (x/y), and a background resource sampler
    (CPU/RSS of the watched subprocesses, disk free on *out_dir*) ticking
    independently of stage transitions. Three orthogonal signals, not one
    forced hierarchy — live sector-read progress is reported separately via
    :meth:`progress_cb` since reads dominate wall time and deserve their own
    sub-percentage, not a slot in the coarse stage count."""

    def __init__(self, total_cells: int, out_dir: Path) -> None:
        self.total_cells = total_cells
        self.out_dir = out_dir
        self.cell_idx = 0
        self.cell_label = ""
        self.stage_label = ""
        self.stage_idx = 0
        self.stage_total = 0
        self._prev_cpu: dict[int, tuple[int, float]] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def cell(self, label: str) -> None:
        """Advance to the next matrix cell (baseline capture or one rung)."""
        self.cell_idx += 1
        self.cell_label = label
        self.stage_label = ""
        self.stage_idx = self.stage_total = 0
        print(f"[{self.cell_idx}/{self.total_cells}] {label}", flush=True)

    def stage(self, idx: int, total: int, label: str) -> None:
        """Set the current stage within the active cell (e.g. 2/3 'AR gate')."""
        self.stage_idx, self.stage_total, self.stage_label = idx, total, label
        print(
            f"  [{self.cell_idx}/{self.total_cells}] stage {idx}/{total}: {label}",
            flush=True,
        )

    def progress_cb(self, unit: str = "sectors") -> Callable[[int, int], None]:
        """A throttled ``on_progress(done, total)`` callback for ``run_read``,
        printed against the currently active cell/stage context."""
        last = {"t": 0.0}

        def cb(done: int, total: int) -> None:
            if total <= 0:
                return
            now = time.monotonic()
            done_ratio_complete = done >= total
            if not done_ratio_complete and now - last["t"] < PROGRESS_PRINT_THROTTLE:
                return
            last["t"] = now
            pct = done / total * 100
            print(
                f"    [{self.cell_idx}/{self.total_cells}] "
                f"{self.stage_label or 'reading'}: {done}/{total} {unit} "
                f"({pct:.1f}%)",
                flush=True,
            )

        return cb

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=PROGRESS_SAMPLE_INTERVAL + 2)

    def _loop(self) -> None:
        while not self._stop.wait(PROGRESS_SAMPLE_INTERVAL):
            self._sample()

    def _sample(self) -> None:
        rss_mb, cpu_pct, names = _watched_proc_stats(WATCHED_PROC_NAMES, self._prev_cpu)
        try:
            free_gb = shutil.disk_usage(self.out_dir).free / 1e9
        except OSError:
            free_gb = -1.0
        proc_str = "+".join(sorted(set(names))) if names else "idle"
        print(
            f"  … [{self.cell_idx}/{self.total_cells}] "
            f"{self.cell_label}"
            f"{' / ' + self.stage_label if self.stage_label else ''}  "
            f"proc={proc_str}  rss={rss_mb:.0f}MB  cpu={cpu_pct:.0f}%  "
            f"disk_free={free_gb:.1f}G",
            flush=True,
        )


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
    # Per-track ladder-recovery outcome for R0-R4 span recovery, e.g.
    # "t8@8X,t5@unrec" — which swept speed first made each flagged track AR-verify
    # (or unrecovered). None for non-recovery rows (baseline / skip / ctdb).
    recovered_at: str | None = None
    # Which repeat this baseline was, 1-based, when --passes > 1; 0 for a single
    # pass. Every pass is emitted (data is never discarded); `pass_role` marks
    # which one fed the recovery rungs.
    pass_n: int = 0
    pass_role: str = ""  # "median" (drove the rungs) | "repeat" | "" (single pass)
    wall_s: float = 0.0
    # AccuDisc's exit code for this cell's read, and why the gates were skipped.
    # 0 = clean, 3 = completed with caveats; anything else is fatal and the capture
    # must NOT be gated. `abandoned` is the human reason, empty when the cell ran.
    read_exit: int = 0
    captured_sectors: int = 0
    abandoned: str = ""

    @property
    def q_health(self) -> float | None:
        return self.subq_ok / self.subq_total if self.subq_total else None

    @property
    def q_transient(self) -> bool:
        """Whole-pass Q collapse: AccuDisc measured ~33-39% global Q on 1-in-4
        (ZZ Top 32x) and 2-in-3 (Tracy 16x) passes where the neighbouring speed
        held ~98%, at no throughput benefit. Below ~0.90 the pass is a disturbed
        spin, not that speed's real yield."""
        q = self.q_health
        return q is not None and q < _Q_TRANSIENT_FLOOR


# --- Pure decision core (unit-tested) ------------------------------------------


def rung_recovery_flags(
    rung: str, c2: bool, *, overlap_needed: bool = True
) -> list[str]:
    """The recovery knobs for a rung (retries/c2-retries/verify/overlap/ladder).

    These are the escalating re-read aggression applied to a **flagged span only**,
    never whole-disc: ``--verify``/``--overlap``/``--ladder`` are cache-defeated
    (each re-read drags a ~5000-sector flush), so whole-disc they blow up to hours;
    scoped to a span the flush cost is amortized (AccuDisc §25.1). ``--c2-retries``
    is dropped when C2 is off — nothing to retry on (§17.3). ``overlap_needed=False``
    (Accurate Stream confirmed — see ``probe_accurate_stream``) drops ``--overlap``:
    its seam check exists to catch positioning drift between chunk reads, which an
    Accurate Stream drive doesn't have. Defaults True — keep it unless a probe has
    positively confirmed the drive doesn't need it."""
    if rung not in RUNGS:
        msg = f"unknown rung {rung!r}; known: {', '.join(RUNGS)}"
        raise ValueError(msg)
    flags: list[str] = []
    for knob, val in RUNGS[rung].items():
        if knob == "c2-retries" and not c2:
            continue  # no C2 bitmap -> nothing to retry on
        if knob == "overlap" and not overlap_needed:
            continue  # Accurate Stream confirmed -> no positioning drift to catch
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


def _rung_functioned(rung_rows: list[BenchRow]) -> bool:
    """A rung "functioned" if *any* of its speed rows produced a usable result — a
    whole-disc capture (``subq_total``/``measured_cx``) or a span-scoped recovery
    (``span`` set, including a legitimate ``skipped`` no-op on a clean disc). Only a
    rung that yields nothing at all at every speed is blacklisted."""
    return any(bool(r.span) or r.subq_total > 0 or r.measured_cx > 0 for r in rung_rows)


def _rung_integrity(rung_rows: list[BenchRow]) -> bool:
    """Whether a rung achieved integrity, aggregating across its speed rows.

    Credit comes from rows where the rung did **genuine recovery work** — a real
    flagged span (R0-R4) or a ``parity`` repair (ctdb) — never from a ``skipped``
    no-op that merely inherited a clean baseline's green (crediting the skip is the
    masking bug: it lets an audio rung that recovered *nothing* on the damaged
    passes look like a passer off the clean-speed skips). Among genuine attempts,
    "any speed passed" is the measure — a per-disc profile asks *can this method
    recover this disc (at its best speed)*, and you would run it at that speed. Only
    when the rung never had real work to do (skipped at every speed — a clean disc)
    does it inherit the baseline verdict from its skip rows."""
    active = [r for r in rung_rows if r.span not in ("", "skipped")]
    return any(integrity_pass(r) for r in (active or rung_rows))


def _classify_family(
    by_rung: dict[str, list[BenchRow]], order: tuple[str, ...]
) -> dict[str, str]:
    """Classify one ordered rung family (cheapest→dearest) with its own
    ``best_so_far`` chain: a rung that never functions → ``blacklist``; one that
    functions and passes but adds no integrity improvement over a strictly-cheaper
    rung in the *same* family that already passed → ``warn``; else ``keep``.
    Families are classified independently so the audio ladder and the CTDB parity
    exit never cross-warn each other (they are alternative exits, §1)."""
    verdict: dict[str, str] = {}
    best_so_far = False
    for lbl in order:
        rung_rows = by_rung.get(lbl)
        if not rung_rows:
            continue
        if not _rung_functioned(rung_rows):
            verdict[lbl] = "blacklist"
            continue
        ok = _rung_integrity(rung_rows)
        # Soft error: this (more expensive) rung didn't improve on a cheaper one in
        # the same family that already passed.
        verdict[lbl] = "warn" if (best_so_far and ok) else "keep"
        best_so_far = best_so_far or ok
    return verdict


def classify(rows: list[BenchRow]) -> dict[str, str]:
    """Per-rung verdict across a disc's rows (§8.5), keyed by rung label:
    ``blacklist`` (never functions) / ``warn`` (functions but redundant with a
    strictly-cheaper rung in its family) / ``keep``.

    Aggregates **all** of a rung's speed rows (never an arbitrary single one) and
    covers **both** rung families — the R0-R4 audio ladder and the CTDB parity
    family (``ctdb``/``ctdb-noc2``) — each on its own cost-ordered chain. A
    ctdb-only run therefore classifies its rungs instead of returning ``{}``."""
    by_rung: dict[str, list[BenchRow]] = {}
    for r in rows:
        by_rung.setdefault(r.rung, []).append(r)
    verdict = _classify_family(by_rung, tuple(RUNGS))
    verdict.update(_classify_family(by_rung, CTDB_RUNGS))
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


def engine_identity() -> str:
    """Which AccuDisc binary produced this run — resolved path, size, sha256, version.

    ``--version`` alone cannot answer this. Measured 2026-07-26: the old pinned
    snapshot (built 07-25 11:18, 60 160 B) and AccuDisc's `main` (built 07-26 01:45,
    56 048 B, across an ABI change that moved every field of two structs) **both report
    `accudisc 0.2.0`**. A semver names a release; a bench row needs to name a *build*,
    and between two builds of one release version they are different questions. A
    commit suffix was proposed and declined (their §bc), so this stays true.

    So the digest is the identity and the version string is a label beside it. Cheap
    (one hash of a ~56 KB binary), and it makes a run's numbers re-attachable to the
    engine that produced them after the tree has moved on — which is exactly the
    situation every cross-run comparison in RECOVERY.md is in.

    Since 2026-07-26 the snapshot pin is retired and ``tools/accudisc/accudisc`` is a
    symlink into AccuDisc's **live** build tree, so this is no longer a stable value
    within a run: see the end-of-run re-hash in :func:`main`. Resolving the symlink
    first is load-bearing for both — the identity wanted is the inode's, not the link's.
    """
    real = Path(_ACCUDISC).resolve()  # follow the tools/accudisc symlink to the inode
    try:
        digest = hashlib.sha256(real.read_bytes()).hexdigest()[:16]
        size = real.stat().st_size
    except OSError as exc:
        return f"{real} (unreadable: {exc})"
    version = "?"
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        r = subprocess.run(  # noqa: S603 — snapshot/PATH binary, fixed argv
            [_ACCUDISC, "--version"], capture_output=True, text=True, check=False
        )
        version = (
            (r.stdout or r.stderr).strip().splitlines()[0]
            if r.stdout or r.stderr
            else "?"
        )
    return f"{version}  sha256:{digest}  {size}B  {real}"


#: Stamped into every run written from 2026-07-29 onward. Runs WITHOUT this key
#: were indexed by this file's own ladder rule (dedupe on any non-zero `page2a`),
#: which is not the rule in force now — see :func:`probe_ladder`.
LADDER_RULE = "verdict@2026-07-29"


def probe_ladder(device: str) -> list[int]:
    """The drive's distinct achievable read speeds — now the *same* rule as `src/`.

    This used to be a second, independent implementation: it deduped on any
    non-zero ``page2a`` while ``drive_speed.admitted_ladder`` required
    ``req == page2a``. They agreed on every row set observed and would have
    diverged the moment a quantised row yielded a ceiling no other row reached
    (``req=16 -> page2a=10`` with no ``req=10`` row: dropped there, admitted as 10
    here). Two rules meant a bench cell and a rip could disagree about what "32x"
    named, with nothing to say which was right.

    It also bypassed the seam, spawning ``accudisc`` directly — so after the
    binding migration this file would have kept measuring the subprocess while the
    rip path measured the library.

    Both fixed by delegating. The rule now in force is AccuDisc's verdict (a rate
    comparison at three radii), which drops a rung that ``req == page2a`` admits:
    on Tracy, ``req=48 measured=22.96`` sits above ``req=40 measured=23.68``, one
    speed wearing two labels with the faster-looking one slower.

    **Past runs are NOT re-indexed.** Every archived result is keyed by the ladder
    the bench used at the time, and relabelling them retroactively would change
    what the archive means — silently, and after the fact, which is the one thing
    a measurement archive must never do. New runs carry :data:`LADDER_RULE`; runs
    without that key predate the reconciliation and are fenced off by its absence
    rather than by a date anyone has to remember.
    """
    try:
        from cdda2img.drive_speed import admitted_ladder
    except ImportError:  # pragma: no cover — src/ always importable from tools/
        return list(DEFAULT_SPEED_LADDER)
    return admitted_ladder(device) or list(DEFAULT_SPEED_LADDER)


def _flagged_bbox(
    spans: list[tuple[int, int]], cap: int = 20000
) -> tuple[int, int] | None:
    """Bounding ``(start, count)`` window over the flagged spans, for a c2lag probe.
    c2lag needs a *streaming window* of C2-firing sectors (a 1-sector post-seek
    reread flags nothing), so the whole damaged region beats a single span; capped
    so a disc-wide-scattered defect doesn't probe the entire disc. None if empty."""
    if not spans:
        return None
    start = min(s for s, _ in spans)
    end = max(s + c for s, c in spans)
    return start, min(end - start, cap)


def probe_c2lag(device: str, start: int, count: int) -> int | None:
    """Measure the C2/audio erasure alignment (``accudisc c2lag``) over a damaged
    span, in :mod:`cdda2img.ctdb_repair`'s convention — **negated**: AccuDisc reports
    ``pairs=+2``, the erasure decode wants ``align=-2`` (the same physical lag, the
    opposite sign convention).

    Per-drive, and must be measured on C2-firing media under a streaming read.
    Returns None when inconclusive (no C2 fired / evidence too thin — c2lag exits 3
    with no ``pairs=`` line), so the caller falls back to **error-only** decode
    rather than assume a lag. Measure the drive fact; never hardcode it."""
    try:
        r = subprocess.run(  # noqa: S603 — snapshot/PATH binary, fixed argv
            [
                _ACCUDISC,
                "--device",
                device,
                "c2lag",
                "--start",
                str(start),
                "--count",
                str(count),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    for line in r.stdout.splitlines():
        if line.startswith("c2lag ") and "pairs=" in line:
            for tok in line.split():
                if tok.startswith("pairs="):
                    with contextlib.suppress(ValueError):
                        return -int(tok.partition("=")[2])
    return None  # inconclusive ('absent' / thin evidence)


def probe_accurate_stream(device: str) -> bool | None:
    """Probe Accurate Stream (``accudisc features --stream``) — whether the drive
    returns the exact same samples for the exact same LBA on every read. When
    confirmed, ``--overlap``'s boundary-seam check (extend + verify against
    positioning drift) has nothing to catch and is pure overhead; when absent or
    unmeasurable, keep it — capitalise on a *confirmed* guarantee, never assume
    one (governing bench principle: no drive-specific bias, only drive-specific
    advantage where earned). Disc-independent: a drive capability, not a per-disc
    fact, so one probe per run suffices."""
    try:
        r = subprocess.run(  # noqa: S603 — snapshot/PATH binary, fixed argv
            [_ACCUDISC, "--device", device, "features", "--stream"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    for line in r.stdout.splitlines():
        if line.startswith("accurate_stream "):
            val = line.split(maxsplit=1)[1].strip()
            if val == "yes":
                return True
            if val == "no":
                return False
    return None  # unparseable / drive doesn't report it


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
    overlap_needed: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[int, dict[str, int], Path, Path]:
    """One ``accudisc read`` -> ``(returncode, summary, pcm_path, map_path)``.

    Whole-disc when *start* is None (the baseline capture — capture-stream flags
    only, no recovery knobs); a bounded ``--start/--count`` span otherwise. A
    *rung* adds that rung's recovery knobs (span reads only — never whole-disc,
    §25.1); *overlap_needed* (default True) gates whether that rung's ``--overlap``
    knob survives — see ``probe_accurate_stream``. Summary tokens are read off
    ``--progress-fd 1``; stderr is discarded. *on_progress* (optional), when
    given, is called with AccuDisc's own live ``progress <done> <total>``
    tokens off the same fd — this is the fine-grained sector-level progress
    ``BenchProgress.progress_cb`` feeds, distinct from the final summary."""
    pcm = out_dir / f"{tag}.pcm"
    mapf = out_dir / f"{tag}.map"
    cmd = [_ACCUDISC, "--device", device, "read", "--pcm", str(pcm)]
    if c2:
        cmd += ["--c2f", str(out_dir / f"{tag}.c2")]
    cmd += ["--map-file", str(mapf), "--speed", str(speed)]
    if start is not None:
        cmd += ["--start", str(start), "--count", str(count)]
    cmd += capture_flags(sub, c2)
    if sub:
        # `--sub raw` alone captures the stream and reports subq_ok/subq_total, but
        # writes nothing: the aggregate counters survive and the frames do not.
        # Without the sidecar there is no way to ask *which* frames failed, only how
        # many — the exact granularity limit that made a stable count look like a
        # stable defect (AccuDisc correspondence §ah).
        cmd += ["--subf", str(out_dir / f"{tag}.sub")]
    if rung is not None:
        cmd += rung_recovery_flags(rung, c2, overlap_needed=overlap_needed)
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
        elif on_progress is not None and line.startswith("progress "):
            parts = line.split()
            if len(parts) == 3:
                with contextlib.suppress(ValueError):
                    on_progress(int(parts[1]), int(parts[2]))
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


def fetch_ar_once(
    track_lsns: list[int], disc_last_lsn: int, cddb_id: int, tries: int = 4
) -> list[list[dict]]:
    """Fetch the AccurateRip dBAR **once per disc** (item 2 — the whole matrix then
    gates against this cache instead of re-fetching per cell, removing the
    per-cell network confound the run2 log exposed).

    ``fetch_ar_responses`` never raises: it returns ``(responses, transport, _)``
    where ``transport is None`` means *both* HTTPS and HTTP failed at the network
    level (retry-worthy on a flaky link), while a non-None transport with empty
    responses is a definitive 404 (disc genuinely not in the DB — do not retry, do
    not let a real negative masquerade as a transient). So we retry with backoff
    only while the answer is inconclusive."""
    from cdda2img.accuraterip import fetch_ar_responses

    responses: list[list[dict]] = []
    for attempt in range(1, tries + 1):
        responses, transport, _ = fetch_ar_responses(track_lsns, disc_last_lsn, cddb_id)
        if responses or transport is not None:
            return responses  # definitive: found, or reached-server 404
        if attempt < tries:
            time.sleep(min(2**attempt, 8))  # network-level failure — back off, retry
    return responses  # give up: still empty


def _capture_verdict(
    rc: int, pcm: Path, geom: tuple[list[int], int, int] | None
) -> str:
    """Empty when the capture is safe to gate; otherwise why it is not.

    Two independent checks, because they catch different failures:

    * **Exit code.** AccuDisc's contract is ``0`` clean / ``3`` completed with
      caveats / anything else fatal. A fatal exit means stop and surface their
      result — not gate the partial file and mention the code afterwards.
    * **Length.** Exit ``3`` is still "completed", so a caveat exit alone does not
      tell us the capture is whole. A short file gates as an AR failure for a
      reason that has nothing to do with the rung under test, which is worse than
      no row at all: it looks like data.
    """
    if rc not in (0, 3):
        return f"accudisc read exited {rc} (fatal per the machine interface)"
    if not pcm.is_file():
        return "no PCM file was produced"
    got = pcm.stat().st_size // 2352
    want = (geom[1] + 1) if geom is not None else 0
    if want and got < want:
        pct = 100.0 * got / want
        return f"capture truncated: {got}/{want} sectors ({pct:.1f}%), exit {rc}"
    return ""


def _track_ar_conf(
    pcm: Path,
    i: int,
    track_lsns: list[int],
    disc_last_lsn: int,
    read_offset: int,
    responses: list[list[dict]],
) -> tuple[int | None, int | None]:
    """``(conf_v1, conf_v2)`` for track index *i* from the raw PCM against the
    *cached* dBAR *responses* — mirrors :func:`verify_rip`'s per-track offset-window
    read + zero-pad, then the public :func:`match_track_pcm` (no re-fetch). A conf is
    None when no block matched that variant."""
    from cdda2img.accuraterip import match_track_pcm

    n = len(track_lsns)
    offset_bytes = read_offset * 4
    pcm_size = pcm.stat().st_size
    byte_start = track_lsns[i] * 2352 + offset_bytes
    byte_end = (
        track_lsns[i + 1] if i < n - 1 else disc_last_lsn + 1
    ) * 2352 + offset_bytes
    read_start = max(0, byte_start)
    # Defence in depth: `_capture_verdict` should already have abandoned a short
    # capture, but a track window starting past EOF must degrade to "no bytes, so
    # no match", never to a negative read length that kills the whole matrix.
    read_end = max(read_start, min(pcm_size, byte_end))
    with open(pcm, "rb") as f:
        f.seek(read_start)
        raw = f.read(read_end - read_start)
    # Zero-pad the offset window outside the file — the pad falls inside AR's
    # ±2940-frame exclusion zone, so it is checksum-neutral (as in verify_rip).
    # Pad to the exact window width rather than to (byte_end - pcm_size): on a
    # truncated capture the latter over-pads, because the missing head has already
    # been skipped by the clamped read_start.
    if byte_start < 0:
        raw = bytes(-byte_start) + raw
    want = byte_end - max(0, byte_start)
    if len(raw) < want:
        raw = raw + bytes(want - len(raw))
    _v1, _v2, conf_v1, conf_v2 = match_track_pcm(raw, i + 1, n, responses)
    return conf_v1, conf_v2


def gate_accuraterip(
    pcm: Path,
    track_lsns: list[int],
    disc_last_lsn: int,
    read_offset: int,
    responses: list[list[dict]],
) -> tuple[bool | None, bool | None]:
    """Whole-disc AR gate from the **cached** dBAR *responses* (item 2 — no network
    re-fetch). Returns ``(ar_v1_pass, ar_v2_pass)``: True/False when the disc is in
    the DB, ``None`` when it is not (absence of evidence, not a failure).

    When *responses* is non-empty the disc is in the DB and every track appears in
    every block, so the ``max_confidence is not None`` in-DB filter that
    :func:`verify_rip` applied is simply "all tracks" — a v1/v2 pass is then every
    track matching that variant (:func:`_track_ar_conf` per track)."""
    if not responses:
        return None, None
    all_v1 = all_v2 = True
    for i in range(len(track_lsns)):
        cv1, cv2 = _track_ar_conf(
            pcm, i, track_lsns, disc_last_lsn, read_offset, responses
        )
        all_v1 = all_v1 and cv1 is not None
        all_v2 = all_v2 and cv2 is not None
    return all_v1, all_v2


def gate_ctdb(
    pcm: Path,
    track_lsns: list[int],
    disc_last_lsn: int,
    cddb_id: int,
    read_offset: int,
    c2_path: Path | None = None,
    *,
    attempt_repair: bool = False,
    c2_align: int = -2,
) -> tuple[bool | None, bool | None]:
    """CTDB gate — ``(ctdb_pass, ctdb_repaired)``, both from
    :mod:`cdda2img.ctdb_repair`. *c2_align* is the C2/audio erasure alignment in
    sample pairs (per-drive; measured via ``accudisc c2lag``, default -2 for the
    PX-716A) — only used when *c2_path* feeds the erasure-assisted decode.

    * **ctdb_pass** — this rip's per-track CRC32s reconcile with a CTDB entry and
      every verifiable track matches (checksums only, no repair). ``None`` = disc
      not in CTDB, or the lookup failed (not-measured, non-blocking); ``False`` =
      in CTDB but a track mismatches or no entry reconciles; ``True`` = all match.
    * **ctdb_repaired** — attempted **only when ``attempt_repair`` is True** and
      ``ctdb_pass`` is False. Parity repair (``repair_whole_disc``) is a heavyweight
      whole-disc Reed-Solomon FEC decode (``ctanalyse``, CPU-minutes) — a
      last-resort recovery *path*, not a per-cell gate — so the matrix runs it off
      (checksums only) and it is evaluated separately. When run, it works on a
      **copy** (non-destructive), C2-erasure-assisted, AR-double-gated. ``None``
      when not attempted."""
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
    if not attempt_repair:
        return False, None  # damaged/no-reconcile; parity repair is a separate path

    # Non-destructive parity repair on a copy so the capture stays untouched.
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
            c2_align=c2_align,
            cache_dir=cache,
        )
    finally:
        tmp.unlink(missing_ok=True)
    return False, res.repaired


def select_median_pass(rows: list[BenchRow]) -> int:
    """Index of the pass whose Q yield is the median — the representative capture.

    AccuDisc's caveat (§15.6): a single capture at a rung can mislabel that speed
    entirely, because specific request values produce a *whole-pass* Q collapse
    (~33-39% global Q where a neighbouring speed holds ~98%) on some fraction of
    attempts — 1-in-4 on ZZ Top 32x, 2-in-3 on Tracy 16x. Median-of-N is what
    separates the speed's real yield from one disturbed spin.

    Median, not best: taking the best pass would launder exactly the variance we
    are trying to measure, and would make a rung that collapses 2-in-3 look as
    good as one that never collapses. With an even count the lower of the two
    middle passes is chosen, which is the conservative half.

    Falls back to the first pass when no capture yielded any Q at all (``--no-sub``,
    or a disc whose Q is unreadable throughout) — there is nothing to rank on, and
    every pass is equally representative.
    """
    scored = [(r.q_health, i) for i, r in enumerate(rows) if r.q_health is not None]
    if not scored:
        return 0
    scored.sort()
    return scored[(len(scored) - 1) // 2][1]


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
    Disc-invariant — acquire once per disc.

    Falls back to ``accudisc toc`` when the lead-in cannot be read. Without this
    a disc with a dead lead-in but a perfectly healthy program area (the Paul
    Weller CD-R, 2026-07-21) yields **no geometry at all**, which silently means
    no AR/CTDB gate for that disc — the bench would score its whole matrix with
    the absolute columns blank and look merely uninteresting rather than broken.
    """
    ftoc = out_dir / "geometry.fulltoc"
    try:
        r = subprocess.run(  # noqa: S603 — snapshot/PATH binary, fixed argv
            [_ACCUDISC, "--device", device, "fulltoc", str(ftoc)],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        print(f"# geometry: fulltoc capture failed ({exc})")
        return _geometry_from_toc(device)
    if r.returncode not in (0, 3) or not ftoc.is_file():
        print(f"# geometry: fulltoc unavailable (exit {r.returncode}) — trying toc")
        return _geometry_from_toc(device)
    geom = geometry_from_fulltoc(ftoc.read_bytes())
    if geom is None:
        print("# geometry: disc not in CD-DA archival model")
    return geom


def _geometry_from_toc(device: str) -> tuple[list[int], int, int] | None:
    """Geometry from ``accudisc toc``, which degrades past an unreadable lead-in.

    Refuses when the session-1-only policy cannot be established (see
    ``TocGeometry.session_safe``): on a multi-session disc format 0x00 reports the
    *last* session's lead-out, which would yield a wrong disc ID silently — and a
    wrong disc ID means every AR/CTDB lookup 404s, i.e. the gate reads "not in the
    database" when the truth is "we asked the wrong question"."""
    from cdda2img.accudisc_reader import read_toc
    from cdda2img.cddb import compute_cddb_disc_id

    try:
        toc = read_toc(device)
    except (RuntimeError, ValueError) as exc:
        print(f"# geometry: toc unavailable ({exc})")
        return None
    safe, why = toc.session_safe
    if not safe:
        print(f"# geometry: refusing degraded TOC — {why}")
        return None
    print(f"# geometry: source={toc.source} degrade={toc.degrade} ({why})")
    cddb_id = int(compute_cddb_disc_id(toc.track_lsns, toc.disc_last_lsn), 16)
    return toc.track_lsns, toc.disc_last_lsn, cddb_id


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
    responses: list[list[dict]],
    *,
    on_stage: Callable[[str], None] | None = None,
) -> None:
    """Fill a row's AR/CTDB columns from a whole-disc PCM. No-op without geometry.
    *responses* is the once-per-disc cached dBAR (:func:`fetch_ar_once`) — the AR gate
    never re-fetches. *on_stage* (optional) is called with a human label right before
    each gate starts — the two are opaque single calls into production code
    (accuraterip / ctdb_repair), so this is coarse (which one is running)."""
    if geom is None or not pcm.is_file():
        return
    track_lsns, disc_last_lsn, cddb_id = geom
    if on_stage:
        on_stage("AR gate")
    row.ar_v1_pass, row.ar_v2_pass = gate_accuraterip(
        pcm, track_lsns, disc_last_lsn, read_offset, responses
    )
    if on_stage:
        on_stage("CTDB gate (checksum)")
    row.ctdb_pass, row.ctdb_repaired = gate_ctdb(
        pcm, track_lsns, disc_last_lsn, cddb_id, read_offset, c2_path
    )


def _spans_by_track(
    spans: list[tuple[int, int]], track_lsns: list[int]
) -> dict[int, list[tuple[int, int]]]:
    """Group flagged ``(lba, count)`` spans by the track index that owns each span's
    start LBA (the last track start ≤ lba). Spans that begin before track 0 are
    dropped — a program-area pre-gap / lead-in is not an AR-verifiable track."""
    import bisect

    by_track: dict[int, list[tuple[int, int]]] = {}
    for lba, cnt in spans:
        i = bisect.bisect_right(track_lsns, lba) - 1
        if i < 0:
            continue
        by_track.setdefault(i, []).append((lba, cnt))
    return by_track


def _recover_rung(
    args: argparse.Namespace,
    geom: tuple[list[int], int, int] | None,
    governor: int,
    disc_id: str,
    base_speed: int,
    rung: str,
    spans: list[tuple[int, int]],
    base_pcm: Path,
    out_dir: Path,
    ladder: list[int],
    responses: list[list[dict]],
    *,
    overlap_needed: bool = True,
    bp: BenchProgress | None = None,
) -> BenchRow:
    """Recover the flagged spans by **sweeping the probed speed ladder**
    (fastest→slowest) per affected track — mirroring production
    ``_recover_failed_tracks`` — instead of re-reading only at the capture speed
    (the run2 defect: the old rung never used the fact that another speed read a
    region clean). For each track that owns a flagged span: at each ladder speed,
    re-read that track's spans with the rung's knobs, splice into a working copy of
    the baseline, and AR-verify the track against the cached *responses*; stop at the
    first speed whose result makes the track match (fast attempts first, so a
    high-speed match exits early — §3.5, speed diversity is the lever). *base_speed*
    only selected which spans the baseline flagged. Still span-scoped — the
    cache-defeated knobs never touch the whole disc (§4.2).

    Without AR *responses* (disc not in the DB, no gate to stop the sweep on) it
    falls back to a single re-read at *base_speed* — there is nothing to verify a
    ladder attempt against, so a blind sweep would just pick the last read."""
    spliced = out_dir / f"{rung}_{base_speed}x.spliced.pcm"
    shutil.copyfile(base_pcm, spliced)
    t0 = time.monotonic()
    track_lsns, disc_last_lsn, _cddb = geom if geom else ([], 0, 0)
    by_track = _spans_by_track(spans, track_lsns) if track_lsns else {}
    # No verifier → cannot select across speeds; keep the original single-speed read.
    sweep = (ladder or [base_speed]) if responses else [base_speed]
    recovered = c2res = 0
    outcomes: list[str] = []
    n_stages = (len(by_track) or 1) + 1

    for stage_i, (tidx, tspans) in enumerate(sorted(by_track.items()), start=1):
        matched_speed: int | None = None
        for speed in sweep:
            if bp:
                bp.stage(
                    stage_i,
                    n_stages,
                    f"track {tidx + 1}: recover {len(tspans)} span(s) @ {speed}x",
                )
            for si, (lba, cnt) in enumerate(tspans):
                stag = f"{rung}_{base_speed}x_t{tidx}_{speed}x_s{si}"
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
                    overlap_needed=overlap_needed,
                    on_progress=bp.progress_cb() if bp else None,
                )
                splice_span(spliced, spcm, lba)
                recovered += ssum.get("recovered", 0)
                c2res += ssum.get("c2", 0)
                for ext in (".pcm", ".c2", ".map"):
                    (out_dir / f"{stag}{ext}").unlink(missing_ok=True)
            # Stop at the first swept speed that makes THIS track AR-verify.
            if responses:
                cv1, cv2 = _track_ar_conf(
                    spliced,
                    tidx,
                    track_lsns,
                    disc_last_lsn,
                    args.read_offset,
                    responses,
                )
                if cv1 is not None or cv2 is not None:
                    matched_speed = speed
                    break
            else:
                break  # no gate → single base_speed pass only
        outcomes.append(
            f"t{tidx + 1}@{matched_speed}X" if matched_speed else f"t{tidx + 1}@unrec"
        )

    span_str = ",".join(f"{lba}+{cnt}" for lba, cnt in spans)
    row = _mk_row(
        disc_id,
        args.device,
        rung,
        base_speed,
        governor,
        {"c2": c2res, "recovered": recovered},
        span=span_str,
    )
    row.recovered_at = ",".join(outcomes) if outcomes else None
    if bp:
        bp.stage(n_stages, n_stages, "re-gate (AR + CTDB checksum)")
    _gate_row(row, spliced, None, geom, args.read_offset, responses)
    row.wall_s = round(time.monotonic() - t0, 1)  # whole cell: ladder re-reads + gate
    if not args.keep_captures:
        spliced.unlink(missing_ok=True)
    return row


def _ctdb_repair_rung(
    args: argparse.Namespace,
    geom: tuple[list[int], int, int] | None,
    governor: int,
    disc_id: str,
    speed: int,
    base_pcm: Path,
    base_c2: Path | None,
    c2_align: int = -2,
    label: str = "ctdb",
    bp: BenchProgress | None = None,
) -> BenchRow:
    """The CTDB 'repair without reads' path — CTDB checksum reconcile + Reed-Solomon
    parity rebuild on the baseline PCM, **zero extra reads**, so on an in-CTDB disc
    it is plausibly the *fastest* recovery path (capture once, rebuild by maths, no
    re-reading damaged spans). AR-double-gated inside ``repair_whole_disc``. Operates
    on the shared baseline capture, independent of the audio-recovery rungs;
    ``wall_s`` is the repair itself (the capture cost lives on the baseline row).

    *base_c2* is the erasure feed: when present, decode is erasure-assisted (up to
    npar known-position erasures per column) at the measured *c2_align*; when None,
    decode is error-only (⌊npar/2⌋ unknown-position errors) — the ``ctdb-noc2`` rung,
    a faithful stand-in for a **drive with no C2 support** (identical PCM, no erasure
    feed), and also the fallback when c2lag can't be measured. Isolate with
    ``--rungs ctdb`` (or ``ctdb,ctdb-noc2`` for the controlled pair on one capture).

    *bp*, if given, announces a single opaque stage — ``gate_ctdb``/
    ``repair_whole_disc`` are one production-code call from here (entry lookup,
    candidate selection, parity fetch, and the ``ctanalyse`` RS decode all
    happen inside it), so there's no sub-stage to report without instrumenting
    that production module; the resource sampler still shows ``ctanalyse``
    CPU/RAM live while this runs, even though the stage itself doesn't subdivide."""
    if bp:
        bp.stage(1, 1, f"{label}: checksum reconcile + parity repair (ctanalyse)")
    t0 = time.monotonic()
    row = _mk_row(disc_id, args.device, label, speed, governor, {}, span="parity")
    if geom is not None and base_pcm.is_file():
        track_lsns, disc_last_lsn, cddb_id = geom
        row.ctdb_pass, row.ctdb_repaired = gate_ctdb(
            base_pcm,
            track_lsns,
            disc_last_lsn,
            cddb_id,
            args.read_offset,
            base_c2 if (base_c2 and base_c2.exists()) else None,
            attempt_repair=True,
            c2_align=c2_align,
        )
    row.wall_s = round(time.monotonic() - t0, 1)
    return row


def _prewarm_ctdb_cache(
    track_lsns: list[int], disc_last_lsn: int, out_dir: Path
) -> None:
    """Fetch the CTDB lookup once into ``out_dir/ctdb.xml`` so every ``gate_ctdb``
    reads the cache (``load_entries`` already reuses ``xml_cache`` when present)
    rather than the first cell fetching under load (item 2). Best-effort: a failure
    just means the first gate fetches instead. The XML cache is keyed by file path,
    not disc — reusing an ``out_dir`` across different discs would read a stale
    lookup, same as the existing gate; use a fresh out dir per disc."""
    from xml.etree.ElementTree import ParseError

    from cdda2img.ctdb_repair import load_entries

    bounds = [*track_lsns, disc_last_lsn + 1]
    try:
        entries = load_entries(bounds, len(track_lsns), xml_cache=out_dir / "ctdb.xml")
    except (OSError, ParseError) as exc:
        print(f"# ctdb: pre-warm skipped ({exc})")
        return
    print(f"# ctdb: cache pre-warmed ({len(entries)} parity entries)")


def _baseline_passes(
    args: argparse.Namespace,
    geom: tuple[list[int], int, int] | None,
    governor: int,
    disc_id: str,
    speed: int,
    out_dir: Path,
    ar_responses: list[list[dict]],
    bp: BenchProgress,
) -> tuple[list[BenchRow], list[tuple[str, Path, Path]], int]:
    """``--passes`` whole-disc baseline captures at one speed.

    Returns every pass's row (data is never discarded), its capture files, and
    the index of the median-Q pass — the one representative of this speed, which
    alone feeds the recovery rungs. Non-representative captures are deleted
    immediately: 3 passes x 5 speeds is ~6.5 GB of PCM per disc on top of the
    rung captures.
    """
    n_passes = max(1, args.passes)
    pass_rows: list[BenchRow] = []
    pass_files: list[tuple[str, Path, Path]] = []  # (tag, pcm, map)

    for p in range(1, n_passes + 1):
        tag = f"base_{speed}x" if n_passes == 1 else f"base_{speed}x_p{p}"
        suffix = "" if n_passes == 1 else f" (pass {p}/{n_passes})"
        bp.cell(f"baseline @ {speed}x{suffix}")
        bp.stage(1, 3, "read (whole disc)")
        t0 = time.monotonic()
        rc, summary, p_pcm, p_map = run_read(
            args.device,
            out_dir,
            tag,
            speed,
            sub=args.sub,
            c2=args.c2,
            on_progress=bp.progress_cb(),
        )
        row = _mk_row(disc_id, args.device, "baseline", speed, governor, summary)
        row.abandoned = _capture_verdict(rc, p_pcm, geom)
        row.read_exit = rc
        row.captured_sectors = p_pcm.stat().st_size // 2352 if p_pcm.is_file() else 0
        if row.abandoned:
            # Gating a bad capture manufactures a measurement out of nothing: a
            # truncated read fails AR for a reason that has nothing to do with the
            # rung under test. Record the cell as abandoned and move on. (This is
            # also the §4 contract with AccuDisc — a non-zero exit means stop and
            # surface their result, not continue and mention it afterwards.)
            print(f"  !! baseline @ {speed}x ABANDONED: {row.abandoned}", flush=True)
        else:
            _gate_row(
                row,
                p_pcm,
                (out_dir / f"{tag}.c2") if args.c2 else None,
                geom,
                args.read_offset,
                ar_responses,
                on_stage=lambda label: bp.stage(
                    2 if label == "AR gate" else 3, 3, label
                ),
            )
        row.wall_s = round(time.monotonic() - t0, 1)  # capture + gate
        if n_passes > 1:
            row.pass_n = p
        pass_rows.append(row)
        pass_files.append((tag, p_pcm, p_map))
        flag = "  <-- Q TRANSIENT" if row.q_transient else ""
        print(
            f"  baseline @ {speed}x{'' if n_passes == 1 else f' p{p}'}  exit={rc}  "
            f"q={row.q_health}  c2={row.c2_sectors}  ar_v2={row.ar_v2_pass}  "
            f"{row.wall_s}s{flag}",
            flush=True,
        )

    pick = select_median_pass(pass_rows)
    if n_passes > 1:
        for i, row in enumerate(pass_rows):
            row.pass_role = "median" if i == pick else "repeat"
        qs = ", ".join(
            f"{r.q_health:.3f}" if r.q_health is not None else "-" for r in pass_rows
        )
        n_bad = sum(1 for r in pass_rows if r.q_transient)
        bad = f", {n_bad}/{n_passes} below {_Q_TRANSIENT_FLOOR}" if n_bad else ""
        print(f"# q-yield @ {speed}x: {qs} — median pass {pick + 1}{bad}", flush=True)

    if not args.keep_captures:
        for i, (t, pcm_f, map_f) in enumerate(pass_files):
            if i == pick:
                continue
            # The .sub sidecar is deliberately spared. Only the median pass feeds the
            # rungs, so its PCM/C2/map are the only ones worth GBs — but the *set* of
            # CRC-bad frames per pass is a different measurement, and it only exists
            # across repeated passes. Discarding the non-median sidecars throws away
            # the static-vs-transient discriminator to save 33 MB against the 226 MB
            # of PCM already being deleted on the same line.
            for f in (pcm_f, map_f, out_dir / f"{t}.c2"):
                f.unlink(missing_ok=True)

    return pass_rows, pass_files, pick


def run_matrix(
    args: argparse.Namespace,
    geom: tuple[list[int], int, int] | None,
    governor: int,
    speeds: list[int],
    rungs: list[str],
    out_dir: Path,
    *,
    accurate_stream: bool | None = None,
) -> list[BenchRow]:
    """The bench matrix: per speed, one whole-disc baseline capture, then the
    recovery rungs applied only to the map-flagged spans (skipped when the disc is
    clean, §4.2). The R0-R4 rungs sweep the probed speed ladder per flagged track
    (``_recover_rung`` mirrors production ``_recover_failed_tracks``), not just the
    capture speed. The AccurateRip dBAR is fetched **once** up front and every gate
    matches against that cache (``fetch_ar_once`` — no per-cell re-fetch); CTDB is
    cached the same way via its xml file. Rewrites the ranked report after every row
    (crash-safe over a long run) and drops the big disposable captures unless
    ``--keep-captures``. *accurate_stream* (a drive fact, not a disc fact) gates
    ``--overlap`` on every recovery rung: only a positive confirmation (``True``)
    drops it — ``None``/``False`` keep it, the conservative default."""
    overlap_needed = accurate_stream is not True
    disc_id = f"{geom[2]:08x}" if geom else args.label
    out_path = Path(args.out)
    rows: list[BenchRow] = []
    # C2/audio erasure alignment for the ctdb rung — measured once (per drive) on
    # the first C2-firing baseline via accudisc c2lag, never assumed. None until
    # measured; stays None if inconclusive, which makes the ctdb rung fall back to
    # error-only decode rather than trust a mis-aligned erasure feed.
    disc_c2_align: int | None = None
    c2lag_done = False
    total_cells = len(speeds) * (max(1, args.passes) + len(rungs))
    bp = BenchProgress(total_cells, out_dir)

    # Fetch the AccurateRip dBAR ONCE for the whole matrix (item 2): every gate then
    # matches against this cache (fetch_ar_once → gate_accuraterip) instead of the
    # per-cell re-fetch the run2 log caught timing out mid-run. Empty when the disc
    # isn't in AR (the gate then reports None — non-blocking). CTDB gets the same
    # once-per-disc treatment via its on-disk xml cache, pre-warmed here.
    ar_responses: list[list[dict]] = []
    if geom is not None:
        g_lsns, g_last, g_cddb = geom
        ar_responses = fetch_ar_once(g_lsns, g_last, g_cddb)
        print(
            f"# ar: fetched {len(ar_responses)} dBAR block(s) once "
            f"(cached for all {total_cells} cells)"
        )
        _prewarm_ctdb_cache(g_lsns, g_last, out_dir)

    def emit() -> None:
        # The header is what fences this run off from the archive. Every result in
        # rips/ is indexed by whichever ladder rule the bench used when it ran, and
        # those are NOT being re-indexed — relabelling archived measurements after
        # the fact changes what they mean, silently. A run carrying `ladder_rule`
        # used the reconciled rule; a run without the key predates it. Fenced by a
        # property of the file rather than by a date someone has to remember.
        header = (
            f"# ladder_rule = {LADDER_RULE!r}\n"
            f"# ladder = {speeds!r}\n"
            f"# runs without a ladder_rule key used the bench's own former rule\n"
            f"# (dedupe on any non-zero page2a) and are not comparable rung-for-rung\n\n"
        )
        out_path.write_text(header + "".join(row_to_toml(r) for r in rank(rows)))

    try:
        for speed in speeds:
            pass_rows, pass_files, pick = _baseline_passes(
                args, geom, governor, disc_id, speed, out_dir, ar_responses, bp
            )
            base = pass_rows[pick]
            tag, base_pcm, base_map = pass_files[pick]
            rows.extend(pass_rows)
            emit()
            spans = read_map_spans(base_map, start_lba=0)
            print(
                f"  -> using pass {pick + 1} @ {speed}x  spans={len(spans)}  "
                f"ctdb={base.ctdb_pass}/{base.ctdb_repaired}",
                flush=True,
            )
            # Measure the C2/audio lag once, on the first C2-firing baseline (the
            # ctdb rung's erasure feed needs it; probing reuses this damage).
            if not c2lag_done and args.c2 and "ctdb" in rungs and spans:
                c2lag_done = True
                bbox = _flagged_bbox(spans)
                if bbox is not None:
                    disc_c2_align = probe_c2lag(args.device, *bbox)
                    print(
                        f"# c2lag: align={disc_c2_align} "
                        f"(probed {bbox[1]} sectors @ lba {bbox[0]})"
                        if disc_c2_align is not None
                        else "# c2lag: inconclusive — ctdb rung uses error-only decode",
                        flush=True,
                    )
            for rung in rungs:
                if rung in ("ctdb", "ctdb-noc2"):
                    # "repair without reads": parity rebuild on the baseline, no
                    # re-reads and no dependence on the flagged spans (whole-disc
                    # RS FEC). ctdb = erasure-assisted (C2 feed at the measured
                    # c2lag); ctdb-noc2, and an unmeasurable c2lag, fall back to
                    # error-only on the same PCM.
                    bp.cell(f"{rung} @ {speed}x")
                    use_c2 = rung == "ctdb" and args.c2 and disc_c2_align is not None
                    c2_feed = (out_dir / f"{tag}.c2") if use_c2 else None
                    row = _ctdb_repair_rung(
                        args,
                        geom,
                        governor,
                        disc_id,
                        speed,
                        base_pcm,
                        c2_feed,
                        c2_align=disc_c2_align if disc_c2_align is not None else -2,
                        label=rung,
                        bp=bp,
                    )
                    rows.append(row)
                    emit()
                    print(
                        f"  {rung} @ {speed}x  parity  "
                        f"ctdb={row.ctdb_pass}/{row.ctdb_repaired}  {row.wall_s}s",
                        flush=True,
                    )
                    continue
                if not spans:
                    bp.cell(f"{rung} @ {speed}x")
                    bp.stage(1, 1, "skip (clean baseline, 0 flagged spans)")
                    skip = _mk_row(
                        disc_id, args.device, rung, speed, governor, {}, span="skipped"
                    )
                    skip.subq_ok, skip.subq_total = base.subq_ok, base.subq_total
                    skip.ar_v1_pass, skip.ar_v2_pass = base.ar_v1_pass, base.ar_v2_pass
                    skip.ctdb_pass = base.ctdb_pass
                    skip.ctdb_repaired = base.ctdb_repaired
                    rows.append(skip)
                    emit()  # write every row — a trailing run of skips isn't lost
                    print(f"  {rung} @ {speed}x  skipped (0 flagged spans)", flush=True)
                    continue
                bp.cell(f"{rung} @ {speed}x")
                row = _recover_rung(
                    args,
                    geom,
                    governor,
                    disc_id,
                    speed,
                    rung,
                    spans,
                    base_pcm,
                    out_dir,
                    speeds,
                    ar_responses,
                    overlap_needed=overlap_needed,
                    bp=bp,
                )
                rows.append(row)
                emit()
                print(
                    f"  {rung} @ {speed}x  spans={len(spans)}  "
                    f"recovered={row.recovered_sectors}  c2={row.c2_sectors}  "
                    f"via={row.recovered_at}  "
                    f"ar_v2={row.ar_v2_pass}  ctdb={row.ctdb_pass}/{row.ctdb_repaired}  "
                    f"{row.wall_s}s",
                    flush=True,
                )
            if not args.keep_captures:
                base_pcm.unlink(missing_ok=True)
                (out_dir / f"{tag}.c2").unlink(missing_ok=True)
                base_map.unlink(missing_ok=True)
    finally:
        bp.stop()
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
    # Accurate Stream confirmed -> --overlap dropped; unconfirmed (default) -> kept
    h = rung_recovery_flags("R4", c2=True, overlap_needed=False)
    _check("--overlap" not in h and "--ladder" in h, str(h))
    _check(
        "--overlap" in rung_recovery_flags("R4", c2=True, overlap_needed=True),
        "AS default keeps overlap",
    )
    # capture flags are separate (sub/no-sub, C2 on/off)
    _check(
        capture_flags(sub=True, c2=False) == ["--sub", "raw", "--no-c2"], "cap flags"
    )
    _check(capture_flags(sub=False, c2=True) == [], "cap flags default")

    # resource sampler: a name nothing matches -> zero/empty, never raises
    rss, cpu, names = _watched_proc_stats(("no-such-process-xyz",), {})
    _check(rss == 0.0 and cpu == 0.0 and names == [], "unmatched name -> empty")
    # real self-process comm (from /proc) must be found with nonzero RSS — other
    # live processes can share the same comm (e.g. "python3" under `uv run`), so
    # this only checks self is among the matches, not an exact single match.
    own_comm = (Path("/proc") / str(os.getpid()) / "comm").read_text().strip()
    rss2, _cpu2, names2 = _watched_proc_stats((own_comm,), {})
    _check(rss2 > 0.0 and own_comm in names2, f"self-match: rss={rss2} {names2}")

    # BenchProgress: cell/stage counters advance and the sampler thread starts
    # and stops cleanly (a live disc-free smoke test of the reporting plumbing).
    with tempfile.TemporaryDirectory() as td:
        bp = BenchProgress(total_cells=2, out_dir=Path(td))
        bp.cell("baseline @ 40x")
        _check(bp.cell_idx == 1 and bp.total_cells == 2, "cell advance")
        bp.stage(1, 3, "read (whole disc)")
        _check(bp.stage_idx == 1 and bp.stage_total == 3, "stage set")
        cb = bp.progress_cb()
        cb(50, 100)  # must not raise; throttled, so no assertion on print output
        bp.stop()

    # summary token parse (with an appended future key + a non-int guard)
    s = parse_summary("summary hard=3 c2=10 subq_ok=980 subq_total=1000 mode=x")
    _check(s == {"hard": 3, "c2": 10, "subq_ok": 980, "subq_total": 1000}, str(s))
    _check(parse_summary("progress 5 10") == {}, "non-summary line must parse empty")

    # span clustering: state low-nibble, needs-recovery = {2,3,5}
    states = bytes([0x1, 0x2, 0x12, 0x1, 0x3, 0x3, 0x1, 0x5])
    spans = cluster_spans(states, 100)
    _check(spans == [(101, 2), (104, 2), (107, 1)], str(spans))

    # c2lag bbox: whole damaged region, capped, None when empty
    _check(_flagged_bbox([(100, 2), (105, 3)]) == (100, 8), "bbox over spans")
    _check(_flagged_bbox([]) is None, "empty spans -> None bbox")
    _check(_flagged_bbox([(0, 100000)], cap=20000) == (0, 20000), "bbox capped")

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

    # classify covers the ctdb family (previously dropped → classify returned {}),
    # and the error-only rung outranks the C2-assisted one when it already repairs
    # (Tracy finding: error-only parity sufficient → C2-assist redundant). Input
    # order is deliberately reversed to prove verdicts don't depend on row order.
    c2rep = BenchRow(
        "d", "drv", "ctdb", span="parity", ctdb_pass=False, ctdb_repaired=True
    )
    noc2rep = BenchRow(
        "d", "drv", "ctdb-noc2", span="parity", ctdb_pass=False, ctdb_repaired=True
    )
    vc = classify([c2rep, noc2rep])
    _check(vc == {"ctdb-noc2": "keep", "ctdb": "warn"}, str(vc))

    # multi-speed aggregation (the collapse bug): a rung's verdict must combine ALL
    # its speed rows, not an arbitrary last one. R2 recovers real flagged spans at
    # two speeds and FAILS both; a clean-speed `skipped` row (green, inherited) must
    # NOT credit it as a passer — so R2 stays "keep", never "warn"ed off the skip.
    r0_skip = BenchRow(
        "d", "drv", "R0", span="skipped", ar_v2_pass=True, subq_ok=98, subq_total=100
    )
    r2_skip = BenchRow(
        "d", "drv", "R2", span="skipped", ar_v2_pass=True, subq_ok=98, subq_total=100
    )
    r2_fail_a = BenchRow("d", "drv", "R2", span="100+1", ar_v2_pass=False)
    r2_fail_b = BenchRow("d", "drv", "R2", span="200+1", ar_v2_pass=False)
    vm = classify([r0_skip, r2_skip, r2_fail_a, r2_fail_b])
    _check(vm == {"R0": "keep", "R2": "keep"}, str(vm))

    # controlled C2-vs-no-C2 pair where error-only FAILS and C2-assist REPAIRS: the
    # C2-assisted rung is then the keeper, not redundant (the reach advantage shows).
    noc2_fail = BenchRow(
        "d", "drv", "ctdb-noc2", span="parity", ctdb_pass=False, ctdb_repaired=False
    )
    c2_win = BenchRow(
        "d", "drv", "ctdb", span="parity", ctdb_pass=False, ctdb_repaired=True
    )
    vr = classify([noc2_fail, c2_win])
    _check(vr == {"ctdb-noc2": "keep", "ctdb": "keep"}, str(vr))

    # select_median_pass: MEDIAN Q, never best. AccuDisc measured whole-pass Q
    # collapse on a fraction of attempts at some speeds (2-in-3 on Tracy 16x), so
    # picking the best pass would launder exactly the variance being measured.
    def _q(ok: int) -> BenchRow:
        return BenchRow("d", "drv", "baseline", subq_ok=ok, subq_total=100)

    _check(select_median_pass([_q(98), _q(35), _q(97)]) == 2, "median of 3 (one bad)")
    _check(select_median_pass([_q(35), _q(37), _q(98)]) == 1, "median of 3 (two bad)")
    _check(select_median_pass([_q(98)]) == 0, "single pass")
    # even count -> lower middle (the conservative half): sorted 30,90,95,99 picks
    # 90, which is index 0 in the original order
    _check(select_median_pass([_q(90), _q(99), _q(95), _q(30)]) == 0, "even count")
    # no Q at all (--no-sub): nothing to rank on, first pass is representative
    _check(select_median_pass([BenchRow("d", "drv", "baseline")] * 3) == 0, "no Q")
    # q_transient flags a disturbed spin, not merely an imperfect one
    _check(_q(35).q_transient and not _q(98).q_transient, "q_transient floor")

    # _spans_by_track: group flagged spans by the owning track (last start <= lba);
    # a span before track 0 (a head-offset pre-gap) is dropped as non-AR-verifiable.
    bt = _spans_by_track([(50, 2), (150, 1), (250, 3), (0, 1)], [0, 100, 200])
    _check(bt == {0: [(50, 2), (0, 1)], 1: [(150, 1)], 2: [(250, 3)]}, str(bt))
    _check(
        _spans_by_track([(5, 1), (50, 1)], [10, 100]) == {0: [(50, 1)]},
        "pre-track-0 span dropped",
    )

    # gate_accuraterip with an empty (disc-not-in-DB) cache returns (None, None)
    # before touching the file — absence of evidence, not failure.
    _check(
        gate_accuraterip(Path("/nonexistent"), [0, 100], 200, 30, []) == (None, None),
        "empty AR cache -> (None, None)",
    )

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
        "--rungs",
        default="R0,R1,R2,R3,R4",
        help="comma-separated rungs: R0-R4 (audio span recovery), 'ctdb' (CTDB "
        "parity repair on the baseline, zero extra reads, C2-erasure-assisted), "
        "'ctdb-noc2' (error-only parity on the same PCM — the no-C2-drive stand-in). "
        "'--rungs ctdb,ctdb-noc2' is the controlled C2-vs-no-C2 pair.",
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
        "--passes",
        type=int,
        default=1,
        help="whole-disc baseline captures per speed (default 1). Use 3+ to get a "
        "median Q yield: a single capture can mislabel a speed, because some "
        "request values collapse Q for a whole pass on a fraction of attempts. "
        "Every pass is recorded; the median-Q pass feeds the recovery rungs",
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
    unknown = [r for r in rungs if r not in CTDB_RUNGS and r not in RUNGS]
    if unknown:
        ap.error(
            f"unknown rung(s) {unknown}; valid: {list(RUNGS)} + {list(CTDB_RUNGS)}"
        )

    # Accurate Stream is a drive fact, not a disc fact — probe once, up front, and
    # only capitalise on a *positive* confirmation (drop --overlap); an absent or
    # unmeasurable answer keeps --overlap, the conservative default (de-bias
    # principle: never assume a drive feature that wasn't actually confirmed).
    accurate_stream = probe_accurate_stream(args.device)
    print(
        f"# accurate_stream={accurate_stream} "
        f"(--overlap {'dropped' if accurate_stream is True else 'kept'} on R3/R4)"
    )

    print(
        f"# bench: {args.label} on {args.device} "
        f"(governor {governor}x, speeds {speeds}, rungs {rungs})"
    )
    engine_at_start = engine_identity()
    print(f"# engine: {engine_at_start}")

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

    rows = run_matrix(
        args,
        geom,
        governor,
        speeds,
        rungs,
        out_dir,
        accurate_stream=accurate_stream,
    )
    # Courtesy restore for the next disc/consumer — the ceiling persists across
    # handles (§15.1.3), so a slow last rung would otherwise carry over.
    set_speed_to_max(args.device)
    print(f"# wrote {len(rows)} rows → {args.out}")
    print(f"# classify: {classify(rows)}")

    # The engine is a live symlink into AccuDisc's build tree (their §bd), not a
    # snapshot, so a `cmake --build` on their side swaps the binary *between* our
    # per-command invocations. The header line records what we started with and
    # cannot see that. Re-hashing at the end turns "they promised not to rebuild
    # mid-run" into an observation — the same reason we hash the binary rather than
    # trust its version string. It is one stat+hash of a ~56 KB file.
    engine_at_end = engine_identity()
    if engine_at_end != engine_at_start:
        print(
            "# ENGINE CHANGED MID-RUN — rows are not attributable to one build.\n"
            f"#   start: {engine_at_start}\n"
            f"#   end:   {engine_at_end}\n"
            "#   Treat this run as void for cross-run comparison and re-run it.",
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
