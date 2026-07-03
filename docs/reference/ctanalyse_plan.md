# ctanalyse — implementation plan

CTDB parity repair, stage 2 (`private/NEXT.md` item 7). Stages 0–1 (lookup + CRC verify)
were validated end-to-end in `tools/ctdb_probe.py`; this plan covers the repair tool.

## Goal

A standalone C program, `tools/ctanalyse/`, that **analyses** a damaged whole-disc PCM
against downloaded CTDB Reed–Solomon parity and reports the corrections — it never
modifies data. Python owns everything else: CTDB lookup, entry selection, parity fetch,
invoking ctanalyse, applying the corrections, and independent re-verification.

```
Python (policy, network, writes)          C (pure offline math)
─────────────────────────────────         ─────────────────────────────
lookup2.php → pick entry ──────────┐
p.cuetools.net/<id> → parity ──────┤
                                   ├────► ctanalyse --pcm --parity --npar
                                   │        --stride --toc → JSON (stdout)
apply corrections (old-byte check) ◄──────┘
re-verify: CTDB CRC + OUR AccurateRip
both pass → keep; else discard splice
```

## Settled design invariants

- **Analyse-only.** Output is JSON:
  `{can_recover, offset, npar, corrections: [{byte, old, new}...], affected_sectors,
  corrected_errors, crc_before, crc_after}`. Corrections are `(byte_offset, old_u16,
  new_u16)` in **our sample domain** — the detected parity offset (e.g. −669) is unwound
  by ctanalyse before emission. Python verifies each `old` matches before writing; any
  mismatch aborts the entire splice.
- **ctanalyse finds the offset itself** via the RS syndrome (`FindOffset`, tolerant to
  ±stride/2). The Python CRC offset-sweep is verification-only — it cannot locate the
  offset of a *damaged* track; the syndrome can.
- **Two-level safety gate, all-or-nothing per disc:** ctanalyse reports `crc_after`;
  Python independently re-runs our AccurateRip on the spliced PCM. Both must pass or the
  repair is discarded (cd-paranoia audio kept). A miscorrection fails the CRC and is
  rejected — this is what makes repair safe without an "unreadable sectors only" policy.
- **Erasure-aware decode (item 8, IMPLEMENTED 2026-07-03).** `rs_decode_column()` does
  errors-and-erasures (e + 2t ≤ npar). The C2-honesty probe (tools/c2read + c2bench.py)
  confirmed the PX-716A's C2 pointers are precise (~99%) though blind to positioning slips,
  so C2 is fed as erasures (never trusting *un*flagged samples). `ctanalyse --erasures`
  consumes a per-word bitmap; `ctdb_repair.py` builds it from a c2read C2 capture, gated by
  `--c2-mode` / config `c2_recovery`. Validated on real 40× damage (AR conf 200). See
  ALGORITHMS.md §6.
- **Pure C (C99), x86-64-v1 baseline** (nothing beyond SSE2 — the compiler's `-O2`
  default; also keeps the source portable to any architecture). Void, Debian, Ubuntu,
  Arch and Fedora all still target v1; RHEL 9/10 (v2/v3) are supersets.
- **One source tree, runtime dispatch — no clones.** The GF syndrome kernel sits behind
  a function-pointer table selected by a CPUID probe at startup; `--impl=scalar|ssse3|
  avx2|auto` forces a path for benchmarking. The scalar path is permanently compiled in
  as the guaranteed fallback. Tier milestones are **git tags** (`ctanalyse-v1-minimal`,
  `ctanalyse-v2-simd`, …) plus archived binaries — not source forks. An OpenCL tier, if
  the benchmark ever justifies it, dlopens `libOpenCL` (no hard link dependency).
- **Threads are tier zero.** RS columns are independent; pthreads across columns gives
  ×cores on every CPU with no ISA requirement. Designed in from the minimal version
  (`--threads N`, default = online CPUs).
- **Licence:** the RS core is a port of Masayuki Miyazaki's GPL Reed–Solomon library as
  adapted in CUETools (`CUETools.Parity/`, GPL); cdda2img is GPL-3, so this is
  compatible. ctanalyse carries the upstream attribution in its source headers. The C#
  reference sources are UTF-16LE with Japanese comments — transcode before reading
  (`iconv -f UTF-16LE -t UTF-8`).

## Reference sources (local, `private/code/cuetools.net/`)

| File | What to extract |
|------|-----------------|
| `CUETools.Parity/Galois.cs` | GF(2¹⁶) field: polynomial, exp/log table construction, `gfconv` |
| `CUETools.Parity/RsDecode.cs` | Modified Berlekamp–Massey, Chien search, Forney (Miyazaki) |
| `CUETools.Parity/RsEncode.cs` | Encoder (needed only for the test oracle) |
| `CUETools.Parity/Parity2Syndrome.cs` | Wire parity ↔ syndrome conversion (the CTDB download format) |
| `CUETools.CDRepair/CDRepair.cs` | Stride layout, Horner syndrome accumulation, `FindOffset`, `CDRepairFix` correction emission |
| `CUETools.CTDB/CUEToolsDB.cs` | Parity fetch protocol (`p.cuetools.net/<id>`) |
| `CUETools/CUETools.TestParity/*.cs` | Hard-coded test vectors (see Phase 3 gates) |

## Phase 0 — algorithm extraction

Read the C# precisely and write `tools/ctanalyse/ALGORITHMS.md`: field polynomial and
symbol convention, the interleaved grid (`stride` columns × `stridecount` rows; wire
stride 5880 doubles to 11760 internally — pin down exactly what unit each is in),
syndrome recurrence, how parity relates to syndromes for *verification* (what "clean"
looks like), how `FindOffset` derives the offset from syndromes, and the exact byte
format of the `p.cuetools.net` parity file (resolved: syndrome-major u16le,
`npar × internal_stride × 2` = 376,320 bytes for our disc). **DONE** — see
`tools/ctanalyse/ALGORITHMS.md`; the C is written against that document, not against
the C# directly.

## Phase 1 — test data

All corpora are regenerable; they live in `private/testdata/ctanalyse/` (gitignored,
~1.2 GB), produced by a new `tools/make_ctanalyse_testdata.py`:

1. **`good.pcm`** — whole-disc PCM (383 MB) extracted from the PCM block of
   `Tracy Chapman.rbi` (AR-verified conf 200; CTDB-verified via the stage-0/1 probe).
2. **`bad40x.pcm`** — full-disc cd-paranoia rip at 40x, paranoia off, `-O 30`
   (`TMPDIR=/var/tmp`; requires the physical disc). Realistic damage, unbounded.
3. **`splice8.pcm`** — `good.pcm` with the 40x rip's track 8 spliced over
   `[LBA 111142, 120622)`. Bounded, reproducible damage with a known repair target:
   track-8 CTDB CRC `c9719806`.
4. **`ctdb.xml` + `parity.bin`** — cached CTDB lookup response and the parity file for
   entry 67116 (conf 5789+, npar 16, stride 5880), so later phases run offline. The
   "parity" file actually contains per-column **syndromes** (u16le, syndrome-major,
   `npar × internal_stride × 2` = 376,320 bytes, Range-fetched) — see
   `tools/ctanalyse/ALGORITHMS.md` §4; this simplifies ctanalyse to pure decode (no
   encoder port needed).

## Phase 2 — Python orchestration (before any C exists)

`tools/ctdb_repair.py` — standalone driver, deliberately **not** in `src/` yet (per the
settled scope): reuses `ctdb_probe.py`'s lookup; selects the best entry (highest `npar`
among entries our *clean* tracks reconcile to via the CRC offset-sweep); fetches parity
only when committing to a repair; invokes ctanalyse via subprocess; applies corrections
with the old-byte check; re-verifies CTDB CRC + our AccurateRip
(`cdda2img.accuraterip.match_track_pcm`); reports.

Developed and tested against a **stub ctanalyse** (a script emitting canned JSON), so
the splice/verify/abort logic is proven before a line of C exists — including the
abort-on-old-byte-mismatch and the discard-on-failed-gate paths.

## Phase 3 — ctanalyse, scalar v1

> **STATUS 2026-07-02: BUILT AND VALIDATED.** All gates pass: self-test (gfconv
> vectors + decode round-trips 0..8 errors, refusal at 9), clean disc cancels at
> −669 with `crc_before` == the entry's published `crc32` (`b4e8c508`), the 40x
> corpus repairs to consensus (679 corrections) and the driver-verified result is
> byte-identical to the AR-verified rip, and a synthetic 9-errors-in-one-column
> disc gets an honest `can_recover: false`. Timing: 2.9 s single-thread / 1.2 s
> on 12 cores whole-disc — already memory-bandwidth-bound, so Phase 4's ceiling
> is low and OpenCL is effectively retired. Gate 2's standalone Python oracle was
> subsumed by the C self-test round-trips plus the live gates.

Layout: `tools/ctanalyse/{Makefile, main.c, galois16.[ch], rs_decode.[ch],
cdrepair.[ch], jsonout.[ch], ALGORITHMS.md}`. Build: `make` → plain `cc -O2 -std=c99
-pthread`, no dependencies.

CLI: `ctanalyse --pcm F --parity F --npar 16 --stride 5880 --toc 0:12032:...:162892
[--impl auto] [--threads N]` → JSON on stdout, diagnostics on stderr, exit 0 even for
`can_recover: false` (that is a successful analysis; non-zero = operational failure).

Kernel (scalar): Horner syndrome with **per-constant split tables** — each syndrome k
multiplies by the fixed constant α^k, so precompute two 256-entry u16 tables per
constant (`mul = T_lo[x & 0xFF] ^ T_hi[x >> 8]`): 1 KB per constant, 16 KB total for
npar 16, fully L1-resident. Threads partition the stride columns.

Correctness gates, in order — a build must pass all before benchmarking:

1. **Unit vectors** ported from `CUETools.TestParity`: the `gfconv` results, the RS
   syndrome vector `{219, 96, 208, 202, 116, 211, 182, 129}`, encode/decode CRC
   `377539636`, and the ±48-sample offset-detection cases.
2. **Python oracle property tests:** a slow pure-Python GF(2¹⁶)/RS reference (written
   from ALGORITHMS.md) round-trips random data — encode → corrupt ≤ npar/2 symbols per
   column → ctanalyse must locate and correct exactly; corrupt more → must report
   `can_recover: false`. Removes any need for a C#/.NET runtime.
3. **Clean-disc live gate:** `good.pcm` + `parity.bin` → syndromes consistent, offset
   found = −669, zero corrections. Directly validates parity-format and offset handling
   against real CTDB data before any repair is attempted.
4. **Splice acceptance (the headline test):** `splice8.pcm` → corrections restore
   track 8 to CRC `c9719806` **in our sample domain**, and the Phase-2 driver's
   independent AccurateRip re-verify passes. This doubles as the regression test for the
   −669 domain-unwinding trap: get it wrong and CTDB's CRC can pass while our AR fails.
5. **`bad40x.pcm` stress:** recover if within RS capacity (npar 16 → 8 error symbols
   per column), else an *honest* `can_recover: false`. Either outcome is a pass; garbage
   output is the only failure.

Tag `ctanalyse-v1-minimal`; archive the binary.

## Phase 4 — hardware tiers + benchmark

Sized expectation first: 383 MB = ~191.6M GF symbols × npar 16 ≈ 3.1G table-lookup MACs
— likely single-digit seconds scalar single-threaded, sub-second threaded. The tiers are
a **research experiment** (was CUETools' OpenCL ever necessary for decode-only use?),
not a requirement.

- **v2 tier:** SSSE3 `PSHUFB` 4-bit split tables (GF-Complete SPLIT(16,4) style), ~8
  u16 multiplies per instruction. `__attribute__((target("ssse3")))`, CPUID-dispatched.
- **v3 tier:** AVX2 `VPSHUFB`, same scheme at 256-bit.
- **v4 / GFNI:** documented, not implemented — GF(2¹⁶) on GF(2⁸)-native GFNI is
  tower-field research; only revisit if we're still compute-bound after AVX2 (unlikely:
  by then the pass is memory-bandwidth-bound streaming 383 MB).
- **OpenCL tier:** only if the CPU numbers say so; dlopen; prediction is it retires.

Benchmark protocol: the 383 MB corpus, warm cache, `--impl` × `--threads {1, ncpu}`
matrix, ≥5 runs each (hyperfine); record the table in `docs/research/` and the verdict
in this plan. Tag each tier.

## Phase 5 — deferred (explicitly out of scope now)

Pipeline integration (conf-gated third recovery rung after the cd-paranoia speed
ladder), PROV keys (`repaired_via=ctdb:<id>@conf<n>`, `repair_offset`,
`recovery_track_<n>=ctdb_repaired`, affected sectors), and binary distribution
(optional external download). Kept out of `src/` until ctanalyse has earned trust on
the test corpus.

## Risks

- **The −669 offset is used but not yet explained** (suspected: CTDB's userbase skews to
  one dominant modern drive at +699). The double gate means a wrong offset model fails
  verification instead of corrupting audio, and gate 3 tests offset handling on clean
  data — but the anomaly stays on the research list.
- **Parity wire-format unknowns** (`Parity2Syndrome` details) — resolved by Phase 0
  before any C is written.
- **RS capacity on real damage:** the 40x rip may exceed 8 errors/column; the honest
  `can_recover: false` path is a first-class outcome with its own gate.
- **CTDB service dependency:** lookup/parity are cached in the test corpus so
  development never hammers `db.cuetools.net`.
