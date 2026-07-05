# RECOVERY — the failed-read recovery toolkit

Reference for cdda2img's read-recovery strategy: the components, their roles, how they
combine, and the evidence behind every adoption and rejection.

**Living document.** Updated as the strategy is refined.

Last updated: 2026-07-05 (multi-pass recovery test + cd-paranoia retirement, f79fb2a;
then the mode-page-01 experiment verdict + the C2 alignment pinning, same day).

---

## 1. Component role matrix

Every component plays one of five roles, and the roles compose in a fixed cost order
(triage → gate → locate → repair-without-reads → re-acquire).

| Role | Components | Needs |
|---|---|---|
| Triage | C1/C2/CU census (12) | Plextor drive |
| Gates | AR v1/v2 (4), CTDB CRC (2) — absolute; intra-read / overlap (6/7) — relative | DB coverage / nothing |
| Locators | C2 pointers (1), zero-fill erasures (8), RS syndromes | C2-capable drive / nothing |
| Repairers | CTDB parity (3); cross-pass consensus vote (11, planned) | CTDB coverage / nothing |
| Re-acquirers | speed-ladder sweeps (5), retry ladder (9), mode-page tuning (10) | nothing |
| Infrastructure | offset-domain management (13) | known drive offset |

The **exit chain** on a failed gate: CTDB parity repair (zero extra reads) → multi-pass
speed-ladder sweep (extra reads, gate-verified) → store best-effort + log. The two
recovery exits are alternatives tried in cost order, never run together for the same
incident. The one uncovered cell — a disc failing in *neither* database — is the
reserved niche for components 6/7/11.

---

## 2. Component inventory

### 2.1 C2 error pointers

1. **Name**: C2 error pointers (MMC `READ CD` 0xBE with the C2 error field, byte 9 =
   0x12; 294 bitmap bytes per 2352-byte sector, one bit per audio byte).
2. **Role**: locator. Marks the byte positions where the drive's CIRC decoder failed.
   Never a gate, never a repairer.
3. **Status**: included (as the erasure feed into CTDB parity repair; the targeting-hint
   role was tested and dropped — see rejected R3).
4. **Dependencies**: general hardware capability — the drive must implement the C2 error
   field of READ CD (most modern drives do; verified per-drive, not assumed).
   Unprivileged `SG_IO` on a read-only fd suffices.
5. **Optional**: yes — `c2_recovery = auto | on | off` in `cdda2img.toml`. `off` never
   disables recovery itself, only the C2 capture/erasure boost.
6. **Auto-detected**: yes — `c2read --features` (capability claim + smoke read + 5-combo
   probe; exit 0 iff C2 is genuinely usable); `c2_reader.drive_supports_c2()` gates the
   `auto` setting.
7. **Conflicts**: must never be used as a standalone correctness gate (rejected R4:
   structurally blind to positioning/jitter slips). No operational conflicts.
8. **Combinations**: (a) erasure positions for RS parity decode — validated, roughly
   doubles correction capacity; (b) planned weight input for cross-pass consensus
   voting (11); (c) feeds the census (12) indirectly via the same decoder statistics.

### 2.2 CTDB per-track CRCs

1. **Name**: CTDB per-track CRC32 checksums (zlib `crc32` over track PCM, from
   db.cuetools.net lookup records).
2. **Role**: gate (absolute, crowd-sourced). First half of the parity-repair double
   gate; also yields the CTDB consensus offset during the verification sweep.
3. **Status**: included.
4. **Dependencies**: online service (db.cuetools.net) + the disc pressing being in CTDB.
5. **Optional**: no explicit flag; the repair attempt (and hence the CRC gate) fires
   automatically on an AR partial mismatch in the raw domain. Network absence degrades
   it silently to "not applicable".
6. **Auto-detected**: n/a as a capability; coverage is discovered per disc by the lookup
   itself (an empty response = not in CTDB).
7. **Conflicts**: none.
8. **Combinations**: gates CTDB parity repair (2.3) together with AR (2.4) — a repair is
   committed only if **both** agree. Could additionally gate the multi-pass sweep for
   discs in CTDB but not AR (not currently wired; the sweep gates on AR only).

### 2.3 CTDB parity data

1. **Name**: CTDB Reed-Solomon parity blocks (CUETools DB recovery records; RS over
   GF(2^16), npar parity words per interleaved column).
2. **Role**: repairer — the **only** component that produces correct bytes with zero
   extra reads.
3. **Status**: included (`ctdb_repair.repair_whole_disc`, standalone C `ctanalyse` for
   the RS math, Python commits the repair).
4. **Dependencies**: online service + disc in CTDB + damage within RS capacity
   (⌊npar/2⌋ unknown-position errors, or up to npar known-position erasures, per
   column).
5. **Optional**: fires automatically (see 2.2); the C2-erasure *boost* is optional via
   `c2_recovery`.
6. **Auto-detected**: n/a; applicability is discovered by attempting the decode
   (over-capacity → clean refusal, syndrome re-validation guards miscorrection).
7. **Conflicts**: alternative exit to the multi-pass sweep (2.5) — cost order, mutually
   exclusive per incident: if parity repairs, the sweep never runs.
8. **Combinations**: error-only decode (no C2), or erasure-assisted decode (with the C2
   bitmap / zero-fill marks); always double-gated by CTDB CRC + AR before commit.

### 2.4 AccurateRip v1/v2 checksums

1. **Name**: AccurateRip per-track v1/v2 checksums + submission confidences (+ the
   dormant `crc450` frame-450 sub-CRC in every dBAR entry).
2. **Role**: gate (absolute, crowd-sourced) — the primary correctness gate for the whole
   pipeline.
3. **Status**: included (verification always; recovery gate; per-block header
   verification against response splicing). `crc450`: parsed, unused — candidate for
   cheap blind-offset detection and partial verification of unrecoverable tracks.
4. **Dependencies**: online service (accuraterip.com) + disc pressing in the AR DB +
   correct offset-domain handling (2.13).
5. **Optional**: verification is always on (informational, never fails a rip);
   ladder recovery requires it and is disabled by `recovery_passes = 0`.
6. **Auto-detected**: n/a; coverage discovered per disc (HTTPS 404 = legitimate "not in
   DB", no HTTP fallback on 404).
7. **Conflicts**: none. Note the confidence semantics: a low-confidence block matched by
   a minority-offset drive is *not* a bad rip; confidence 1 is never trusted.
8. **Combinations**: gates the multi-pass sweep (first match wins); second half of the
   parity-repair double gate; drives the triage decision (partial vs all-tracks
   mismatch → read errors vs offset misconfiguration).

### 2.5 Multi-pass reads at varying speeds

1. **Name**: speed-ladder sweeps — whole-track re-reads across the drive's probed
   discrete speeds, fastest→slowest, `recovery_passes` sweeps.
2. **Role**: re-acquirer. The unconditional fallback: needs no drive feature and no
   parity, only a gate.
3. **Status**: included (c2read engine since 2026-07-05, commit f79fb2a; previously
   cd-paranoia — see rejected R2).
4. **Dependencies**: none beyond a settable read speed (best-effort
   `CDROM_SELECT_SPEED` / c2read `--speed`); requires an external gate (currently AR)
   to know when to stop.
5. **Optional**: yes — `recovery_passes` config (default 3; 0 disables).
6. **Auto-detected**: the ladder is probed live per drive (`drive_speed.
   probe_speed_ladder`: set each candidate, read back the achieved speed).
7. **Conflicts**: alternative exit to parity repair (2.3). Not self-sufficient — without
   AR (or a future CTDB/relative gate) there is nothing to verify a candidate read
   against, and the sweep cannot run.
8. **Combinations**: sequential after parity-repair failure; each attempt internally
   uses the retry ladder (2.9) and zero-fill (2.8); verified per attempt via
   `match_track_pcm` in the raw offset domain (2.13); spliced sample-exactly on first
   match. Evidence: 6/6 recoveries on the damaged reference disc, no consistent winning
   speed — the *sweep* is the mechanism.

### 2.6 Intra-read verification

1. **Name**: intra-read verification (re-read + compare within a single pass).
2. **Role**: gate (relative — proves stability, not correctness).
3. **Status**: planned, deliberately unscheduled (needs a justifying disc that defeats
   every existing method).
4. **Dependencies**: none (pure software over repeated reads).
5. **Optional**: would be (flag/config).
6. **Auto-detected**: n/a.
7. **Conflicts**: redundant (and strictly weaker) when an absolute gate is available —
   only meaningful in the DB-gap niche.
8. **Combinations**: with boundary overlap checking (2.7) as one self-consistency
   layer; feeds cross-pass consensus (2.11).

### 2.7 Boundary overlap checking

1. **Name**: boundary overlap checking (overlap consecutive read chunks; cross-check
   the overlap regions — cd-paranoia's core defence).
2. **Role**: gate (relative) — specifically detects positioning/jitter slips, the one
   error class C2 cannot see.
3. **Status**: planned, deliberately unscheduled (same justification bar as 2.6).
4. **Dependencies**: none.
5. **Optional**: would be.
6. **Auto-detected**: n/a.
7. **Conflicts**: as 2.6. Costs throughput (overlap re-reads); measured on a clean disc,
   cd-paranoia's overlap mode ran ~1.77× slower than cdrdao for zero recovery benefit —
   which is why it must stay opt-in/last-resort.
8. **Combinations**: with 2.6/2.11 as the DB-gap arbiter; C2 flags as slip-vs-decode
   classifier (a region that changes between passes *without* C2 flags is slip-shaped).

### 2.8 Zero-fill erasure marking

1. **Name**: zero-fill of hard-unreadable sectors (c2read: PCM zeros + C2 bitmap
   all-ones + zeroed subchannel; `hard <lba>` reported on stderr).
2. **Role**: locator/glue — converts "unknown data at unknown positions" into "known
   erasures at known positions", and keeps the PCM/C2/sub output streams
   length-consistent (no desync).
3. **Status**: included (always on).
4. **Dependencies**: none.
5. **Optional**: no — it is the failure contract of every c2read read.
6. **Auto-detected**: n/a.
7. **Conflicts**: none. Synthetic all-ones C2 is masked out of the C2 *statistics* so it
   can't distort the census or the verdict exit code.
8. **Combinations**: feeds RS erasure decode (2.3) directly; downstream, zeroed samples
   in AR's exclusion zones are checksum-neutral.

### 2.9 Retry ladder + cache-defeat

1. **Name**: per-sector retry ladder with cache-defeat (c2read `--retries K`, default
   2): on a failed chunk, retry per-sector; between attempts issue a throwaway read
   ~5000 sectors away so the drive cannot serve its cache.
2. **Role**: re-acquirer (micro scale — within one pass, per sector).
3. **Status**: included.
4. **Dependencies**: none.
5. **Optional**: yes — `--retries` flag (0 disables retries; zero-fill still applies).
6. **Auto-detected**: n/a.
7. **Conflicts**: interacts with mode-page tuning (2.10): drive-internal retries
   (default 10) multiply with our retries — one motivation for the fast-fail
   experiment.
8. **Combinations**: nested inside every read of the speed sweep (2.5); sense-key
   classes (medium / hardware / other) are tallied for the read summary.

### 2.10 Mode-page 01 error-recovery tuning

1. **Name**: drive-side read-error-recovery tuning (MODE SELECT, mode page 0x01: error
   bits, e.g. TB = transfer-bad-blocks 0x20; retry count byte).
2. **Role**: re-acquirer tuning — the only component that changes the *drive's*
   behaviour rather than ours.
3. **Status**: **rejected for adoption** (experiment run 2026-07-05,
   `tools/modepage_experiment.py`); retained as a manual diagnostic flag
   (`c2read --recovery ERR,RETR`, saved page restored on every exit path) for
   genuinely dying media. Measured drive default (PX-716A): error byte 0x00,
   retries 10.
4. **Dependencies**: O_RDWR on the device node (MODE SELECT is filtered on read-only
   fds; the cdrom group grants rw — still no root); drive must honour the page.
5. **Optional**: yes — experiment flag only; never set implicitly.
6. **Auto-detected**: no.
7. **Conflicts**: none found — the experiment showed the parameter is simply inert for
   the miscorrection defect class (see §4.6): three arms (default, `0x20,1`, `0x00,1`)
   were statistically identical in latency, C2 flag volume, C2 precision/recall, and
   AR match rate.
8. **Combinations**: would only matter in the command-failure regime (sectors the
   drive reports unreadable) — the regime where zero-fill (2.8) and the retry ladder
   (2.9) operate. Re-evaluate only if such a disc appears.

### 2.11 Cross-pass consensus voting

1. **Name**: per-sector (or per-sample) majority vote across N independent passes,
   optionally C2-weighted.
2. **Role**: repairer (relative) — the repair-of-last-resort for DB-gap discs. Where
   2.6/2.7 *detect* inconsistency, this *arbitrates* it.
3. **Status**: planned, deliberately unscheduled (same bar as 2.6/2.7).
4. **Dependencies**: none (C2 weighting needs 2.1).
5. **Optional**: would be.
6. **Auto-detected**: n/a.
7. **Conflicts**: same-speed consensus was observed to be insufficient on the reference
   disc (persistent same-speed miscorrections) — any implementation must vote across
   *speed-diverse* passes. Weaker than any absolute gate; must never outrank one.
8. **Combinations**: consumes passes from 2.5; C2 flags as vote weights; overlap checks
   (2.7) as the slip filter on its inputs.

### 2.12 C1/C2/CU error census

1. **Name**: Plextor Q-Check error census (vendor opcode 0xEA, subcommands
   0x15/0x16/0x17; C1 = E11+E21+E31, C2, CU counters at 75-sector intervals; c2read
   `--cxscan`, aggregated by `tools/cx_census.py`).
2. **Role**: triage — disc-health early warning. C1/C2 rates rise long before anything
   becomes uncorrectable or AR notices; the census decides whether the rest of the
   toolkit ever needs to fire (re-archive while recovery is still cheap).
3. **Status**: included (opt-in tool; PROV/catalogue wiring deliberately deferred).
4. **Dependencies**: exact-drive specific — Plextor vendor opcode (INQUIRY vendor gate
   inside c2read; refuses on other drives). O_RDWR fd.
5. **Optional**: yes — `--cxscan` flag; never runs in the pipeline.
6. **Auto-detected**: the Plextor gate is automatic; the census itself is manual.
7. **Conflicts**: none (it is a separate scan pass, not concurrent with ripping).
8. **Combinations**: validates/locates alongside C2 pointers (first census run put all
   256 CU errors and every hotspot inside the known defect span); trend tracking across
   sessions is the intended long-term use.

### 2.13 Offset-domain management

1. **Name**: unified offset domain — raw PCM end-to-end; AR verifies at `read_offset`;
   ctanalyse finds its own consensus offset; recovery reads/splices raw with margin
   sectors; `apply_offset` runs exactly once, at storage.
2. **Role**: infrastructure — every absolute gate is meaningless without it.
3. **Status**: included (completed 2026-07-05: the last early-correction path was
   removed with the cd-paranoia ladder).
4. **Dependencies**: a known per-drive read offset (config `[[drives]]`, else the
   AccurateRip drive catalogue).
5. **Optional**: no.
6. **Auto-detected**: partially — the AR drive-offset catalogue auto-applies at ≥3
   submissions, prompts below that; user config is always authoritative.
7. **Conflicts**: the cd-paranoia *read* fallback still corrects at read time (`-O`),
   so that one path verifies at offset 0 — the sole domain exception, isolated and
   documented.
8. **Combinations**: underpins 2.2/2.3/2.4/2.5; the recovery window math (margin =
   ⌈|offset|/588⌉ sectors, zero-padded at disc edges inside AR's exclusion zone) and
   the sample-exact splice (`track_start*2352 + read_offset*4`) live here.

### 2.14 Rejected components and methods

- **R1 — eject / reseat / drive reset between recovery passes.** Rejected: no measured
  recovery benefit across the paranoia_recovery_test experiment set. Speed diversity
  was doing all the work; the mechanical theatre added time and wear.
- **R2 — the cd-paranoia engine as the recovery re-reader.** Retired 2026-07-05: 6/6
  AR-gated recoveries with plain c2read re-reads proved the sweep across passes ×
  speeds is the mechanism, not the paranoia engine. cd-paranoia's remaining roles are
  the full-disc *read* fallback when cdrdao fails, and the cosmetic track-1 preview.
- **R3 — C2-guided targeted re-reads as a time optimizer.** Rejected: c2timing showed
  no reliable wall-clock win (n=3, baseline mean 20.0 s vs c2-guided 27.8 s, huge
  variance) — recovery is a stochastic lottery; the per-read saving is swamped by
  attempt-count variance plus a run-up penalty when seeking straight to the defect.
- **R4 — C2 as a standalone correctness gate.** Refuted empirically: a defect-free
  track returned 8,852 wrong samples with zero C2 flags (a positioning slip — coherent
  wrong audio that decodes cleanly). C2 models CIRC decode failure only; it cannot see
  servo errors. Precision ~99% makes it an excellent *hint*, and its blindness makes it
  an unacceptable *gate*.
- **R5 — readcd `-edc-corr` (EDC/ECC software correction).** Dismissed: operates on
  data-sector EDC/ECC structures that do not exist in CD-DA audio sectors.
- **R6 — same-speed consensus re-reading.** Folded into 2.11's constraints: persistent
  same-speed miscorrections on the reference disc mean consensus must be speed-diverse
  to converge on truth.
- **R7 — vendor 0xD8 raw-read path.** Contingency dropped: the PX-716A (and MMC drives
  generally) accept all five 0xBE C2/subchannel combos, making the vendor opcode
  unnecessary for capture.

---

## 3. Users — using c2read standalone

`c2read` is a small standalone C tool (built from `tools/c2read/`, symlinked onto
`$PATH`) that talks raw MMC commands to an optical drive over Linux `SG_IO`. It needs
**no root** — membership of the `cdrom` group is enough (two features need the
read-write device permission that group grants: `--cxscan` and `--recovery`). cdda2img
drives it internally, but everything below works from any shell. This section is the
precursor/supplement to the eventual man page.

### 3.1 What it reads

An audio CD sector is 2,352 bytes (588 stereo 16-bit samples). c2read can ask the drive
to return, per sector, any combination of:

- **audio** — the PCM itself (little-endian, so files are usable as-is);
- **C2** — 294 bytes = one bit per audio byte, set where the drive's error correction
  *failed* for that byte;
- **subchannel** — the 96-byte control stream woven between sectors (positions, track
  numbers, the disc's catalogue number and per-track ISRCs).

### 3.2 Quick recipes

```sh
# Does my drive support C2 error pointers? (exit 0 = yes, usable)
c2read --device /dev/sr0 --features

# Show the disc layout (track start addresses + lead-out)
c2read --device /dev/sr0 --toc

# Rip the whole disc: audio + C2 bitmap
c2read --device /dev/sr0 --full --pcm disc.pcm --c2 disc.c2

# The full archival capture: audio + C2 + subchannel + lead-in extras, in ONE pass
c2read --device /dev/sr0 --full --pcm disc.pcm --c2 disc.c2 \
       --sub raw --subf disc.sub --fulltoc disc.fulltoc --cdtext disc.cdtext

# Re-read one span (here: sectors 111142-120622) slowly
c2read --device /dev/sr0 --start 111142 --count 9481 --speed 8 --pcm span.pcm

# Disc-health census on a Plextor drive (then aggregate it)
c2read --device /dev/sr0 --cxscan > census.txt

# What speed is the drive set to right now?
c2read --device /dev/sr0 --speed-report

# Stop the disc spinning (no eject)
c2read --device /dev/sr0 --stop
```

### 3.3 Feature breakdown

| Flag | What it does, simply |
|---|---|
| `--features` | Probes whether C2 reporting actually works on this drive: checks the capability claim, does a smoke read, and tries all five audio/C2/subchannel combinations. Trust the exit code (0 = usable). |
| `--toc` / `--fulltoc F` / `--cdtext F` | Reads the disc's table of contents (simple text / raw multi-session dump) and CD-Text (album/track titles, if the disc has any — no file is created when absent). These come from the disc lead-in: instant, no audio reading. |
| `--full` / `--start`+`--count` | Read everything, or exactly the sector range you name. |
| `--pcm F` / `--c2 F` / `--sub raw\|q` + `--subf F` | Choose which streams to write. PCM is standard s16le; the C2 file has 294 bytes per sector; sub is raw 96-byte (or drive-formatted 16-byte Q) frames. |
| `--speed X` | Ask the drive to read at X-times speed first (best effort). Slower reads sometimes succeed where fast ones fail — the basis of recovery sweeps. |
| `--retries K` | If a chunk fails, retry each sector up to K times, deliberately reading elsewhere in between so the drive can't just replay its cache. Sectors that still fail are written as silence with every C2 bit set, so output files never lose sync. |
| `--ranges` | Print the C2-flagged sector ranges at the end — a quick damage map. |
| `--cxscan` | Plextor drives only: the drive's own error-rate self-test (C1/C2/CU counts every 75 sectors). High C1/C2 with zero CU means "still perfectly readable, but degrading" — re-archive now, cheaply. |
| `--speed-report` | Print the drive's maximum and current read speed. |
| `--recovery E,R` | Experiment flag: temporarily change how hard the drive itself retries before reporting failure (restored on exit). Don't use casually. |
| `--stop` | Spin the platter down without ejecting. Drives don't do this by themselves after reads. |
| `--chunk N`, `--any`, `--c2beb`, `-q` | Tuning/plumbing: sectors per command (auto-clamped), non-audio sector tolerance, the 296-byte C2 variant, quiet mode. |

### 3.4 Exit codes and progress

- `0` — read completed, C2 flagged at least something.
- `3` — read completed, no C2 flags (a clean disc — or nothing but zero-filled
  hard-unreadable sectors; check the stderr summary).
- `1` — I/O error; `2` — usage error.
- Machine-readable `progress <done> <total>` lines are printed on stdout (rate-limited
  to ~4/s); the human status line and per-sector `hard <lba>` reports go to stderr.

---

## 4. Developers — design, science, and history

### 4.1 Design philosophy

1. **The PCM is the artifact; everything else is a claim about it.** A rip is "good"
   only relative to a gate. All trust flows from gates, and gates must be independent
   of the read mechanism they judge.
2. **Absolute gates outrank relative ones.** AR and CTDB compare against the pressing's
   canonical bytes (crowd-sourced, cross-drive, cross-decade). Self-consistency
   (overlap, consensus, C2) can only ever prove *stability* — a drive that misreads
   deterministically passes every relative check forever. Relative methods are reserved
   for the niche where no absolute gate exists.
3. **Exits in cost order.** Zero extra reads (parity repair) before targeted re-reads
   (sweep) before giving up honestly (store best-effort, log `unrecovered`, never
   splice unverified data).
4. **Erasures beat errors.** An RS code corrects twice as many known-position erasures
   as unknown-position errors. Much of the toolkit (C2 capture, zero-fill) exists to
   convert the latter into the former.
5. **No speculative building.** Every component was validated on real damaged media
   before integration, and several plausible ideas were killed by measurement (see
   §4.8). Components 6/7/11 stay unbuilt until a disc that needs them exists.
6. **One offset domain.** Raw end-to-end, corrected exactly once at storage. Every
   checksum bug we have seen in the wild (including one in our own history) was an
   offset-domain bug.

### 4.2 CIRC, C2 pointers, and the census (2.1, 2.12)

CD audio is protected by CIRC — two concatenated Reed-Solomon stages over GF(2^8) with
cross-interleaving: C1 = RS(32,28) on the de-multiplexed frame, then, after a
delay-line de-interleave that scatters burst errors, C2 = RS(28,24). C1 corrects small
random errors; what it cannot fix it *flags*, and C2 uses those flags as erasures to
correct bursts. What survives both stages uncorrected is reported (if the host asks)
as **C2 error pointers** — per-byte flags in the READ CD response.

Key empirical facts (5-pass confusion matrix vs an AR-verified oracle, PX-716A):

- **Precision ~0.98–0.996** — when the drive flags a byte it is almost always wrong.
- **Recall ~99% for genuine decode failures** in the media-defect region.
- **Zero recall for positioning errors**: one pass returned 8,852 coherent wrong
  samples on a defect-free track with no flags at all — the drive lost servo tracking,
  streamed ~15 sectors from the wrong place, and re-synced. CIRC decoded those sectors
  perfectly; they were simply the wrong sectors. This single observation dictates the
  entire trust architecture: C2 is a locator, never a gate.
- C2 flags are **non-deterministic** read-to-read (46–60 flagged sectors per pass over
  the same damage; tiny intersection of wrong-sample sets between passes) — consistent
  with marginal signals falling either side of the decoder's threshold per revolution.
- The C2 bitmap **lags the audio by 2 sample pairs** on the PX-716A — a real per-drive
  alignment that must be applied before using flags as erasure positions. Pinned
  empirically 2026-07-05 by TP-argmax against an AR-verified oracle (precision 0.993 at
  the correct lag, 0.27 at the inverted sign — a sharp, unambiguous peak). Beware sign
  conventions: `ctdb_repair.build_erasure_bitmap(align_pairs=-2)` and
  `modepage_experiment (k=+2)` express the *same* physical lag.

The Plextor Q-Check census (vendor 0xEA) reads the same decoder's internal counters
directly: C1 (= E11+E21+E31), C2-stage corrections, and CU (uncorrectable) per
75-sector interval. Because C1/C2 rates climb long before anything becomes CU, the
census sees disc rot years before AR can: our first run showed conf-200 AR passes on
tracks whose C2-correction counts were in the thousands. Triage, not recovery — but it
decides *when* recovery will be needed.

### 4.3 AccurateRip (2.4)

Per track, over u32 little-endian sample pairs with a 1-based multiplier `i`:

```
v1 = Σ (i × frame_i)                mod 2^32
v2 = v1 + Σ overflow_high_bits      (the 64-bit product's high words)
```

The first and last 5 CD frames (2,940 samples) of the *disc* are excluded (guards on
track 1 / last track), which is what makes edge zero-padding checksum-neutral — the
foundation of both the AR window shift and the recovery window's edge padding. Disc
identity is a triple (id1, id2, cddb_id) derived from the TOC; the response (dBAR) is a
list of blocks, one per drive-offset population, each carrying per-track
`(confidence, crc, crc450)`.

Semantics that matter:

- The single `crc` slot holds *either* a v1 or a v2 checksum depending on the
  submitter's ripper era — so we compute both locally and test each against it.
- **Confidence is a population count, not a quality score.** A minority-offset drive
  matching a conf-14 block against a conf-136 majority is a *correct* rip by 14
  independent witnesses.
- Partial mismatch (some tracks match) means sector damage → recovery. All-tracks
  mismatch on an in-DB disc means the offset is wrong → configuration, not recovery.
- `crc450` is a v1-style checksum of a single frame at offset 450 — enough to test
  candidate offsets cheaply or to say "mostly right" about a track that cannot fully
  match. Parsed, currently unused (candidate future work).

### 4.4 CTDB: CRCs and parity (2.2, 2.3)

CTDB stores, per submitted pressing, per-track CRC32s and a Reed-Solomon parity record
computed over the disc PCM arranged as interleaved columns of 16-bit words (GF(2^16)).
With npar parity words per column, RS decoding corrects up to ⌊npar/2⌋
unknown-position errors per column — or up to npar *erasures* when the error positions
are known. The decode pipeline (standalone `ctanalyse`, analyse-only; Python applies
the repair):

1. locate the CTDB entry and consensus offset (CRC sweep);
2. compute syndromes; with a C2/zero-fill erasure bitmap, build the erasure locator,
   compute modified syndromes, run Berlekamp-Massey on the clean tail, combine into the
   errata locator; Chien search + Forney values;
3. **re-validate syndromes after correction** — the guard against over-capacity
   miscorrection (RS decoding beyond capacity produces confidently wrong output);
4. commit only if the CTDB per-track CRCs **and** AR both pass (double gate).

The C2-erasure boost is a *modifier*, not a separate method: error-only decode already
repaired our reference damage; erasures extend reach when damage density approaches
capacity. Gotcha for driver authors: build erasure bitmaps with
`np.packbits(bool_arr, bitorder='little')`, never fancy-indexed `|=` (duplicate-byte
updates are silently dropped, and C2 flags cluster).

### 4.5 Multi-pass speed sweeps (2.5)

The model that survived the experiments: an uncorrectable read at a given moment is a
**stochastic lottery**. The defect zone's signal is marginal; each revolution the
decoder either clears it or doesn't, influenced by rotational phase, servo state and
linear velocity. Consequences, all observed:

- single-pass recovery at *any* fixed speed is unreliable;
- same-speed repetition can fail persistently (systematic miscorrection at that speed);
- **speed diversity is the lever** — winning speeds across our recoveries were 32X,
  32X, 4X, 40X, 40X, 32X: no consistent winner, so no "magic speed" exists to find;
- the paranoia engine contributed nothing measurable: plain raw READ CD re-reads,
  gate-verified, recover at the same rate (6/6).

Implementation shape (shipped): ladder probed per drive; fastest→slowest ×
`recovery_passes` (fast attempts are cheap, so spend them first); each attempt is a raw
window read → offset-corrected slice → `match_track_pcm` → sample-exact splice of the
*verified corrected bytes* at `track_start*2352 + read_offset*4`. Splicing corrected
bytes into the raw file at the shifted position means the ±offset samples that
"belong" to neighbouring tracks' checksum windows are never touched — proven by an
11/11 whole-disc re-verify after three independent splices.

Statistics discipline: the sequential mode answers "does it recover within budget";
only the randomized `--characterize` mode (Wilson 95% CIs, shuffled trial order,
re-seating held constant) can attribute an effect to speed. A single sequential run
confounds speed with attempt order and sample size one.

### 4.6 The re-acquisition stack: retries, cache-defeat, mode page 01 (2.9, 2.10)

Drives cache aggressively; a naive re-read after a failure often returns the cached
(possibly interpolated) data instead of touching the platter. c2read's retry ladder
issues a seek-away read (~5,000 sectors distant) between per-sector attempts so every
retry is a genuine re-read. Sense keys from failures are classified (medium / hardware
/ other) in the summary — a hardware-heavy pattern points at the transport (we have a
known intermittently-faulty USB bridge), not the disc.

Below all of this sits the drive's own firmware policy: mode page 0x01 (read error
recovery) exposes an error-handling byte and a retry count (PX-716A default: 0x00, 10
retries). The hypothesis was that fast-fail (`0x20,1` — TB: transfer the bad block
anyway; 1 retry) would shorten failed sweep attempts and/or change C2 honesty.

**Experiment verdict (2026-07-05, `tools/modepage_experiment.py`): the parameter is
inert for this defect class — rejected for adoption.** Three arms (default, `0x20,1`,
`0x00,1`), 5 interleaved reps each, whole-track 40X reads with C2, measured against an
AR-verified oracle: latency 4.8 s in every arm (the retry count is never consulted
because **no SG_IO command ever fails** on a miscorrection-type defect — `hard=0`
across every run this disc has ever produced), flag volume ≈ wrong-pair volume in
every read, precision 0.992–0.994 and recall 0.991–0.994 in all arms, AR 0/5
everywhere. Mode-page recovery policy governs the command-failure regime; disc rot of
this kind never enters it. The flag remains available as a manual diagnostic for media
that genuinely fails commands.

The experiment paid for itself twice over anyway (§4.8 items 9–10): it forced an
empirical re-measurement of the **C2/audio alignment** — the bitmap lags the audio by
2 sample pairs (TP-argmax sweep: precision collapses to 0.27 at the wrong sign,
peaks 0.993 at the right one), confirming the production erasure feed
(`ctdb_repair.build_erasure_bitmap`, `align_pairs=-2` in its shift convention) is
correct — and it upgraded the C2 honesty numbers on real rot at full speed: per-read
FN of 2–9 pairs out of ~600 wrong (recall 99.2% pooled). Both strengthen the
erasure-assisted repair design.

### 4.7 The DB-gap: relative methods (2.6, 2.7, 2.11)

For a disc in neither AR nor CTDB there is no absolute gate, and today the pipeline's
honest answer is "rip, report, cannot verify". The reserved design for that niche:

- **overlap checking** (2.7) detects slips: consecutive chunks overlap by k sectors and
  the overlap regions must be byte-identical; a mismatch localizes a positioning error
  — precisely the C2-invisible class;
- **intra-read verification** (2.6) re-reads within the pass and compares;
- **consensus voting** (2.11) arbitrates: per-sector majority across speed-diverse
  passes, optionally down-weighting C2-flagged bytes.

They are unbuilt on purpose. The bar: a real disc that (a) fails AR/CTDB coverage, (b)
resists the sweep, and (c) would plausibly yield to consensus. Until that disc exists,
building this layer is speculation with a real maintenance cost — and its output would
still be a *relative* claim, recorded as such in provenance, never presented as
verified.

### 4.8 History: discovered, adopted, rejected

Chronology of the evidence (all on the same reference disc — a 1988 pressing with a
40×-repeatable defect region in track 8 — unless noted):

1. **C2 confusion matrix** (5 passes vs AR-verified oracle): precision ~99%, recall
   ~99% for decode errors, **total blindness to a positioning slip** → C2 demoted from
   candidate gate to locator (R4). Also fixed: READ CD returns s16le; raw reads sit at
   exactly the drive's +30 read offset; C2/audio alignment k=−2.
2. **CTDB parity pipeline** (ctdb_probe → ctanalyse → ctdb_repair): error-only RS
   repair validated end-to-end (CRC + AR double gate); C2-erasure decode validated
   (erasure feed = byte-identical repair via a different path); over-capacity refusal
   and false-positive guards tested. Adopted as the first exit.
3. **c2timing**: C2-guided *targeted* re-reads are not a time win (R3) — variance
   dominates, plus a run-up penalty. Adopted conclusion: C2 = erasures for parity, not
   a re-read optimizer.
4. **paranoia_recovery_test** (cd-paranoia era): single-speed single-pass recovery
   unreliable; eject/reseat/reset worthless (R1); the passes × speeds sweep is what
   recovers. Shipped as the cd-paranoia ladder (interim).
5. **cdrtools review**: adopted the Q-Check census idea and mode-page tuning as an
   experiment; dismissed `-edc-corr` (R5) and the libscg transport (SG_IO suffices
   unprivileged).
6. **c2read upgrade (Phases 1–5)**: single-pass capture of audio+C2+sub+lead-in;
   zero-fill contract; retry ladder + cache-defeat; census; speed report. The C2 rip
   path became one read.
7. **c2read_recovery_test** (2026-07-05): 3/3 sequential recoveries, AR-only gate, no
   C2, no CTDB (+ 3/3 earlier baseline = 6/6, all byte-identical at conf 200) → the
   paranoia engine retired from recovery (R2); `_recover_failed_tracks` rewritten onto
   `c2_reader.read_span` in the raw domain; offset domain unified completely (the
   CTDB-failure early correction and the boundary-bail full-disc re-rip deleted).
8. **Census first light**: all 256 CU errors inside the known track-8 defect span;
   thousands of stage-2 corrections on four tracks that AR passes at conf 200 —
   disc-rot early warning demonstrated.
9. **Mode-page 01 experiment** (2026-07-05): drive-side error-recovery tuning is inert
   for the miscorrection defect class — no latency, honesty, or quality effect across
   default / `0x20,1` / `0x00,1` (5 reps each, oracle-measured). Rejected for adoption
   (2.10); flag kept as a manual diagnostic.
10. **C2 alignment pinned** (2026-07-05, same session): the first honesty run produced
   precision 0.27 — flags equal in count to the wrong pairs but misplaced. A TP-argmax
   sweep found the bitmap lags the audio by exactly 2 sample pairs (precision 0.993 at
   the peak); the historical "k=−2" note was the same lag in the opposite sign
   convention, and the production erasure feed was verified correct. Honest recall on
   real rot at 40X: 99.2% pooled, FN 2–9 pairs per read.

### 4.9 Open questions

- **crc450**: wire up for cheap blind-offset detection and partial verification of
  `unrecovered` tracks?
- **CTDB CRC as a sweep gate** for discs in CTDB but not AR (currently the sweep gates
  on AR only).
- **Q valid-frame variance**: per-rip usable Q-frame counts vary wildly (157k vs 72k on
  the same disc); the voting floors and TOC-authority absorb it, but it is not
  explained.
- **The justifying disc** for §4.7 has not yet been found — by design, nothing is built
  until it is.
