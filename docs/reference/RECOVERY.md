# RECOVERY — the CD-DA failed-read recovery strategy

The single source of record for read-recovery across the two-project system:

- **cdda2img** — the ripping application: gating (AccurateRip, CTDB), parity
  repair, offset-domain management, and orchestration of the recovery loop.
- **AccuDisc** — the low-level read engine (separate project,
  https://github.com/HomerSlated/accudisc): C2 capture, zero-fill erasures,
  retry/cache-defeat, speed ladder, the frame-accurate status surface, and the
  drive-capability probes. *It only moves bits* — no lookups, no analysis, no
  network I/O, ever.

This file is **hardlinked** at `cdda2img/docs/reference/RECOVERY.md` and
`accudisc/docs/reference/RECOVERY.md`: one document, both repos. It supersedes and
replaces the former three (`cdda2img/docs/reference/RECOVERY.md`,
`accudisc/reference/RECOVERY_STRATEGY.md`, `accudisc/docs/ACCUDISC_RECOVERY.md`).

**Living document.** Update it in the same change that alters recovery behaviour
on either side; the component numbering (§2) is the cross-reference key both
projects cite.

Last updated: 2026-07-20 (three-doc consolidation + the 3-disc recovery
test-suite report, §12). Prior substantive dates preserved inline.

---

## Part I — Strategy

### 1. The organising principle: five roles, one cost order

Every recovery component plays exactly one role, and the roles compose in a
fixed sequence — **triage → gate → locate → repair-without-reads → re-acquire**.
That sequence *is* the strategy.

| Role | Components | Needs | Home |
|---|---|---|---|
| **Triage** | C1/C2/CU census (2.12) | Plextor vendor opcode | AccuDisc driver + status surface |
| **Gates** | AR v1/v2 (2.4), CTDB CRC (2.2) — absolute; intra-read (2.6), overlap (2.7) — relative | DB coverage / nothing | absolute → cdda2img; relative → AccuDisc |
| **Locators** | C2 pointers (2.1), zero-fill erasures (2.8), RS syndromes | C2-capable drive / nothing | AccuDisc (C2, zero-fill); cdda2img (RS) |
| **Repairers** | CTDB RS parity (2.3); cross-pass consensus (2.11, planned) | CTDB coverage / nothing | cdda2img (parity); AccuDisc (consensus) |
| **Re-acquirers** | speed-ladder sweeps (2.5), retry ladder (2.9), mode-page tuning (2.10) | nothing | AccuDisc read engine + driver |
| **Infrastructure** | offset-domain management (2.13) | known drive offset | shared: AccuDisc supplies the offset, cdda2img owns the domain |

**The exit chain on a failed gate** (the load-bearing idea):

> CTDB parity repair (zero extra reads) → multi-pass speed-ladder sweep (extra
> reads, gate-verified) → store best-effort + log `unrecovered`.

The two repair exits are **alternatives tried in cost order — never run together
for the same incident.** If parity repairs, the sweep never runs. The one
uncovered cell — a disc failing in *neither* database — is the reserved niche for
the relative methods (2.6/2.7/2.11).

### 1.1 The one invariant to never break

**Absolute gates outrank relative gates, always.**

- *Absolute* (AR, CTDB): compare your bytes against the pressing's canonical
  bytes — crowd-sourced, cross-drive, cross-decade. They can pronounce a rip
  *correct*.
- *Relative* (intra-read, overlap, consensus, C2): can only ever prove
  *stability*. A drive that misreads **deterministically** passes every relative
  check forever.

Relative methods are therefore quarantined to the **DB-gap niche** and must never
be presented as verification. Their output is a relative claim, recorded as such
in provenance. `exit 0 ≠ verified`.

### 1.2 Design philosophy

1. **The PCM is the artifact; everything else is a claim about it.** All trust
   flows from gates, and gates must be independent of the read mechanism they
   judge.
2. **Absolute gates outrank relative ones** (§1.1).
3. **Exits in cost order.** Zero extra reads (parity repair) before targeted
   re-reads (sweep) before giving up honestly (store best-effort, log
   `unrecovered`, never splice unverified data).
4. **Erasures beat errors.** An RS code corrects twice as many known-position
   erasures as unknown-position errors. Much of the toolkit (C2 capture,
   zero-fill) exists to convert the latter into the former.
5. **No speculative building.** Every component was validated on real damaged
   media before integration, and several plausible ideas were killed by
   measurement (§10). Components 2.6/2.7/2.11 stay unbuilt until a disc that
   needs them exists.
6. **One offset domain.** Raw end-to-end, corrected exactly once at storage.
   Every checksum bug seen in the wild (including one in cdda2img's own history)
   was an offset-domain bug.

---

## Part II — Component inventory

Every component with its role, status, dependencies, conflicts, and productive
combinations. The `#` is the stable cross-reference key. "AccuDisc status" is the
read-engine side; "caller" means cdda2img (or any application driving AccuDisc).

### 2.1 C2 error pointers

- **Name**: C2 error pointers (MMC `READ CD` 0xBE with the C2 field; 294 bitmap
  bytes/sector, one bit per audio byte).
- **Role**: locator. Marks byte positions where the drive's CIRC decoder failed.
  Never a gate, never a repairer.
- **AccuDisc status**: supported; requested by default on `read`, probed by
  `features` (claim + smoke + 5-combo, exit 0 iff usable). Bitmap passed through
  untouched; C2-guided rescue via `--c2-retries`.
- **cdda2img status**: included as the erasure feed into CTDB parity repair. The
  targeting-hint role was tested and dropped (R3, §10).
- **Dependencies**: drive must implement the C2 field (most modern drives do;
  verified per-drive via `features`, never assumed). Unprivileged `SG_IO` on a
  read-only fd suffices.
- **Config**: `c2_recovery = auto | on | off` in `cdda2img.toml`. `off` never
  disables recovery, only the C2 capture/erasure boost.
- **Conflicts**: must never be a standalone correctness gate (R4 — structurally
  blind to positioning/jitter slips). No operational conflicts.
- **Combinations**: (a) erasure positions for RS parity decode — validated,
  roughly doubles correction capacity, C2's **one production job**; (b) planned
  weight input for cross-pass consensus (2.11); (c) feeds the census (2.12)
  statistics indirectly.

### 2.2 CTDB per-track CRCs

- **Name**: CTDB per-track CRC32 (zlib `crc32` over track PCM, from
  db.cuetools.net records).
- **Role**: gate (absolute, crowd-sourced). First half of the parity-repair
  double gate; also yields the CTDB consensus offset during the verification
  sweep.
- **AccuDisc status**: out of scope, permanently (network gate — caller's).
- **cdda2img status**: included.
- **Dependencies**: online service + the disc pressing being in CTDB.
- **Optional**: no explicit flag; the repair attempt (hence the CRC gate) fires
  automatically on an AR partial mismatch in the raw domain. Network absence
  degrades it silently to "not applicable".
- **Conflicts**: none.
- **Combinations**: gates CTDB parity repair (2.3) together with AR (2.4) — a
  repair commits only if **both** agree.

### 2.3 CTDB parity data

- **Name**: CTDB Reed-Solomon parity blocks (RS over GF(2^16), npar parity words
  per interleaved column).
- **Role**: repairer — the **only** component that produces correct bytes with
  zero extra reads.
- **AccuDisc status**: out of scope, permanently (repairer — caller's). AccuDisc
  *feeds* it: C2 bitmap + zero-fill erasures + targeted span reads.
- **cdda2img status**: included (`ctdb_repair.repair_whole_disc`; standalone C
  `ctanalyse` for the RS math, Python commits the repair).
- **Dependencies**: online service + disc in CTDB + damage within RS capacity
  (⌊npar/2⌋ unknown-position errors, or up to npar known-position erasures, per
  column).
- **Optional**: fires automatically (2.2); the C2-erasure *boost* is optional via
  `c2_recovery`.
- **Conflicts**: alternative exit to the multi-pass sweep (2.5) — cost order,
  mutually exclusive per incident.
- **Combinations**: error-only decode (no C2), or erasure-assisted decode (with
  the C2 bitmap / zero-fill marks); always double-gated by CTDB CRC + AR before
  commit. Non-destructive (works on a copy).

### 2.4 AccurateRip v1/v2 checksums

- **Name**: AccurateRip per-track v1/v2 checksums + submission confidences (+ the
  `crc450` frame-450 sub-CRC in every dBAR entry).
- **Role**: gate (absolute, crowd-sourced) — the primary correctness gate for the
  whole pipeline.
- **AccuDisc status**: out of scope, permanently (absolute gate — caller's).
- **cdda2img status**: included (verification always; recovery gate; per-block
  header verification against response splicing). `crc450` wired for partial
  verification (2026-07-05): a track failing the full CRC but matching the
  frame-450 sub-CRC is graded **DAMAGED** (right pressing/offset, corrupt bytes
  elsewhere), PROV `ar450_track_<n>=matched@<conf>`. Never a pass on its own,
  never a recovery splice gate. Blind-offset-detection use still open (Part IX).
- **Dependencies**: online service (accuraterip.com) + disc pressing in the AR DB
  + correct offset-domain handling (2.13). HTTPS preferred; HTTP fallback only on
  `URLError`/`OSError`; a 404 over HTTPS is a legitimate negative, no fallback.
- **Optional**: verification always on (informational, never fails a rip); ladder
  recovery requires it and is disabled by `recovery_passes = 0`.
- **Conflicts**: none. Confidence semantics: a low-confidence block matched by a
  minority-offset drive is *not* a bad rip; confidence 1 is never trusted.
- **Combinations**: gates the multi-pass sweep (first match wins); second half of
  the parity-repair double gate; drives the triage decision (partial vs
  all-tracks mismatch → read errors vs offset misconfiguration).

### 2.5 Multi-pass reads at varying speeds

- **Name**: speed-ladder sweeps — whole-track re-reads across the drive's probed
  discrete speeds, fastest→slowest, `recovery_passes` sweeps.
- **Role**: re-acquirer. The unconditional fallback: needs no drive feature and
  no parity, only a gate.
- **AccuDisc status**: primitive supported. Span reads at `--speed X`, speed never
  auto-restored; the caller owns the sweep loop. Per-sector speed diversity via
  `--ladder` inside one invocation.
- **cdda2img status**: included (`_recover_failed_tracks`; AccuDisc engine since
  the c2read→AccuDisc flip, 2026-07-11 — previously cd-paranoia, R2).
- **Dependencies**: a settable read speed (best-effort `CDROM_SELECT_SPEED` /
  `--speed`); requires an external gate (AR) to know when to stop.
- **Optional**: `recovery_passes` config (default 3; 0 disables).
- **Auto-detected**: the ladder is probed live per drive (`drive_speed.
  probe_speed_ladder` / `accudisc speeds`: set each candidate, read back the
  achieved speed).
- **Conflicts**: alternative exit to parity repair (2.3). Not self-sufficient —
  without a gate there is nothing to verify a candidate against.
- **Combinations**: sequential after parity-repair failure; each attempt
  internally uses the retry ladder (2.9) and zero-fill (2.8); verified per attempt
  via `match_track_pcm` in the raw offset domain (2.13); spliced sample-exactly on
  first match. Evidence: 6/6 recoveries on the damaged reference disc, no
  consistent winning speed — the *sweep* is the mechanism.

### 2.6 Intra-read verification

- **Name**: re-read + compare within a single pass.
- **Role**: gate (relative — proves stability, not correctness).
- **AccuDisc status**: supported (`--verify P` independent cache-defeated passes).
- **cdda2img status**: not wired; reserved for the DB-gap niche, deliberately
  unscheduled (needs a justifying disc that defeats every existing method).
- **Conflicts**: redundant and strictly weaker when an absolute gate is available.
- **Combinations**: with boundary overlap (2.7) as one self-consistency layer;
  feeds cross-pass consensus (2.11).

### 2.7 Boundary overlap checking

- **Name**: overlap consecutive read chunks; cross-check the overlap regions.
- **Role**: gate (relative) — specifically detects positioning/jitter slips, the
  one error class C2 cannot see.
- **AccuDisc status**: supported (`--overlap K` seam check + slip classification),
  plus the **Accurate Stream probe** (`features --stream`) that tells you whether
  overlap is needed at all.
- **cdda2img status**: not wired; DB-gap reserve, same bar as 2.6.
- **Conflicts**: costs throughput — measured ~1.77× slower than a plain read for
  *zero* recovery benefit on clean discs → must stay opt-in / last-resort. On an
  Accurate-Stream drive it is near-redundant (its whole purpose is catching slips
  that Accurate Stream prevents); on a non-Accurate-Stream drive it is the only
  defence against the C2-invisible class, and there it earns its cost.
- **Combinations**: with 2.6/2.11 as the DB-gap arbiter; C2 flags as a
  slip-vs-decode classifier (a region that changes between passes *without* C2
  flags is slip-shaped).

### 2.8 Zero-fill erasure marking

- **Name**: zero-fill of hard-unreadable sectors (PCM zeros + C2 all-ones +
  zeroed subchannel; `hard <lba>` reported).
- **Role**: locator/glue — converts "unknown data at unknown positions" into
  "known erasures at known positions", and keeps the PCM/C2/sub streams
  length-consistent (no desync).
- **AccuDisc status**: supported, always on. The **failure contract of every
  read**; the frame-accurate status surface is the richer replacement for the
  `hard <lba>` stderr line.
- **cdda2img status**: included (relied upon downstream).
- **Conflicts**: none. Synthetic all-ones C2 is masked out of the C2 *statistics*
  so it can't distort the census or verdict exit code.
- **Combinations**: feeds RS erasure decode (2.3) directly; zeroed samples in AR's
  exclusion zones are checksum-neutral.

### 2.9 Retry ladder + cache-defeat

- **Name**: per-sector retry ladder with cache-defeat (`--retries K`, default 2):
  on a failed chunk, retry per-sector; between attempts issue a throwaway read
  ~5000 sectors away so the drive cannot serve its cache.
- **Role**: re-acquirer (micro scale — within one pass, per sector).
- **AccuDisc status**: supported (`--retries K`, seek-away, sense classification).
- **cdda2img status**: included (used inside every sweep read).
- **Conflicts**: interacts with mode-page tuning (2.10): drive-internal retries
  (default 10) multiply with ours.
- **Combinations**: nested inside every read of the speed sweep (2.5); sense-key
  classes (medium / hardware / other) tallied for the summary — a hardware-heavy
  pattern implicates the *transport* (e.g. a flaky USB bridge), not the disc.

### 2.10 Mode-page 01 error-recovery tuning

- **Name**: drive-side read-error-recovery tuning (MODE SELECT, page 0x01: error
  bits e.g. TB=0x20; retry count).
- **Role**: re-acquirer tuning — the only component that changes the *drive's*
  behaviour rather than ours.
- **Status**: **rejected for adoption** (experiment 2026-07-05,
  `tools/modepage_experiment.py`); retained as a manual diagnostic
  (`--recovery ERR,RETR`, saved page restored on every exit path) for genuinely
  dying media. PX-716A default: error byte 0x00, retries 10.
- **Dependencies**: O_RDWR on the device node (MODE SELECT filtered on read-only
  fds; the `cdrom` group grants rw — still no root); drive must honour the page.
- **Conflicts**: none found — inert for the miscorrection defect class (§3.6): the
  retry count is never consulted because **no SG_IO command ever fails** on a
  miscorrection-type defect. Governs the command-failure regime only.
- **Note**: the drive *does* honour and persist the page (verified by MODE SENSE →
  SELECT → SENSE readback); any implementation must read the page back and fail
  loudly on a mismatch, so a page-ignoring drive can never silently invalidate an
  experiment.

### 2.11 Cross-pass consensus voting

- **Name**: per-sector (or per-sample) majority vote across N independent passes,
  optionally C2-weighted.
- **Role**: repairer (relative) — the repair-of-last-resort for DB-gap discs.
  Where 2.6/2.7 *detect* inconsistency, this *arbitrates* it.
- **AccuDisc status**: supported (per-sector form: any-two-byte-identical among ≤6
  speed-diverse samples). C2-*weighted* voting: implementable, deferred.
- **cdda2img status**: not wired; DB-gap reserve.
- **Conflicts**: same-speed consensus was observed insufficient on the reference
  disc (persistent same-speed miscorrections) — any implementation must vote
  across *speed-diverse* passes (R6). Weaker than any absolute gate; must never
  outrank one.
- **Combinations**: consumes passes from 2.5; C2 flags as vote weights; overlap
  checks (2.7) as the slip filter on its inputs.

### 2.12 C1/C2/CU error census

- **Name**: Plextor Q-Check census (vendor 0xEA, subcmds 0x15/0x16/0x17; C1 =
  E11+E21+E31, C2, CU counters at 75-sector intervals; `accudisc cxscan`,
  aggregated by `tools/cx_census.py`).
- **Role**: triage — disc-health early warning. C1/C2 rates rise long before
  anything becomes uncorrectable or AR notices; the census decides whether the
  rest of the toolkit ever needs to fire.
- **AccuDisc status**: supported via the Plextor vendor driver (opt-in, external
  `.so`; INQUIRY vendor gate refuses on other drives). O_RDWR fd.
- **cdda2img status**: included (opt-in tool; PROV/catalogue wiring deferred).
- **Conflicts**: none (a separate scan pass, not concurrent with ripping).
- **Combinations**: validates/locates alongside C2 pointers (first census run put
  all 256 CU errors and every hotspot inside the known defect span); trend
  tracking across sessions is the intended long-term use.

### 2.13 Offset-domain management

- **Name**: unified offset domain — raw PCM end-to-end; AR verifies at
  `read_offset`; ctanalyse finds its own consensus offset; recovery reads/splices
  raw with margin sectors; `apply_offset` runs exactly once, at storage.
- **Role**: infrastructure — every absolute gate is meaningless without it.
- **AccuDisc status**: honored by abstention. AccuDisc never offset-corrects; it
  supplies the factual per-drive offset (`info` / REDUMP table). The one-domain
  discipline is the caller's.
- **cdda2img status**: included (completed 2026-07-05).
- **Dependencies**: a known per-drive read offset (config `[[drives]]`, else the
  AccurateRip drive catalogue).
- **Combinations**: underpins 2.2/2.3/2.4/2.5; recovery window math (margin =
  ⌈|offset|/588⌉ sectors, zero-padded at disc edges *inside* AR's exclusion zone)
  and the sample-exact splice (`track_start*2352 + read_offset*4`) live here.

### 2.14 Rejected components and methods

See §10 for the full chronology. Summary: **R1** eject/reset (no benefit),
**R2** cd-paranoia as re-reader (added nothing over gate-verified re-reads),
**R3** C2-guided targeted re-reads as a time optimizer (variance dominates),
**R4** C2 as a standalone gate (structurally blind to slips), **R5** readcd
`-edc-corr` (no EDC/ECC in audio sectors), **R6** same-speed consensus
(converges on the wrong answer), **R7** vendor 0xD8 raw-read path (unnecessary —
all five 0xBE combos work).

---

## Part III — The science (developer deep-dive)

### 3.1 CIRC, C2 pointers, and the census (2.1, 2.12)

CD audio is protected by **CIRC** — two concatenated Reed-Solomon stages over
GF(2^8) with cross-interleaving. C1 = RS(32,28) on the de-multiplexed frame; a
delay-line de-interleave then scatters burst errors; C2 = RS(28,24) uses C1's
failure flags as erasures to correct the scattered remains. The interleave is why
a 2.4 mm scratch is survivable. What survives both stages uncorrected is reported
— if the host asks — as **C2 error pointers**, per-byte flags in the READ CD
response.

Key empirical facts (5-pass confusion matrix vs an AR-verified oracle, PX-716A):

- **Precision ~0.98–0.996** — a flagged byte is almost always genuinely wrong.
- **Recall ~99% for genuine decode failures** in the media-defect region.
- **Zero recall for positioning errors**: one pass returned **8,852 coherent
  wrong samples on a defect-free track with no flags at all** — the drive lost
  servo tracking, streamed ~15 sectors from the wrong place, and re-synced. CIRC
  decoded those sectors perfectly; they were simply the wrong sectors. **This
  single observation dictates the entire trust architecture: C2 is a locator,
  never a gate.**
- C2 flags are **non-deterministic** read-to-read (46–60 flagged sectors per pass
  over the same damage; tiny intersection of wrong-sample sets between passes).
- The C2 bitmap **lags the audio by 2 sample pairs** on the PX-716A — a real
  per-drive alignment (probe it, don't hardcode). Pinned by TP-argmax against an
  AR-verified oracle: precision 0.993 at the correct lag, 0.27 at the inverted
  sign (a sharp, unambiguous peak). Sign conventions: `ctdb_repair.
  build_erasure_bitmap(align_pairs=-2)` and `accudisc c2lag` (`pairs=+2`) express
  the *same* physical lag.

The Plextor Q-Check census reads the same decoder's internal counters directly.
Because C1/C2 rates climb long before anything becomes CU, the census sees disc
rot years before AR can: the first run showed conf-200 AR passes on tracks whose
C2-correction counts were in the thousands. Triage, not recovery — but it decides
*when* recovery will be needed.

### 3.2 The defects, and the failure regimes

`READ CD` data transfers bypass the audio-playback concealment path; you get the
decoder's real output plus, optionally, its confession (C2). Four distinct
failure regimes require different tools:

| Regime | What the drive does | Signal | Counter-tool |
|--------|--------------------|--------|--------------|
| **Decode failure** | CIRC gives up on bytes | C2 flags fire | C2 capture, rescue rereads, erasure-fed parity repair |
| **Miscorrection / marginal** | wrong bytes, no error | nothing (or unstable rereads) | verify passes, consensus, speed diversity, external checksums |
| **Positioning slip** | servo loses lock, streams coherent audio from the wrong place, re-syncs | nothing — CIRC decodes it perfectly | overlap checking + shift detection |
| **Command failure** | sector read fails outright (CHECK CONDITION) | SCSI sense (medium/hardware) | retry ladder + cache-defeat, then zero-fill |

The slip regime dictates the trust architecture (§3.1). Reads of marginal regions
behave as a **stochastic lottery**: whether a revolution decodes depends on
rotational phase, servo state, and linear velocity. There is no "magic speed" —
*speed diversity* is the lever, and same-speed repetition can fail persistently.

### 3.3 AccurateRip (2.4)

Per track, over u32 little-endian sample pairs with a 1-based multiplier `i`:

```
v1 = Σ (i × frame_i)                mod 2^32
v2 = v1 + Σ overflow_high_bits      (the 64-bit product's high words)
```

The first and last 5 CD frames (2,940 samples) of the *disc* are excluded, which
makes edge zero-padding checksum-neutral — the foundation of both the AR window
shift and the recovery window's edge padding. Disc identity is a triple (id1,
id2, cddb_id) from the TOC; the response (dBAR) is a list of blocks, one per
drive-offset population, each carrying per-track `(confidence, crc, crc450)`.

Semantics that matter:

- The single `crc` slot holds *either* a v1 or a v2 checksum depending on the
  submitter's ripper era — compute both locally and test each.
- **Confidence is a population count, not a quality score.** A minority-offset
  drive matching a conf-14 block against a conf-136 majority is a *correct* rip by
  14 independent witnesses. Confidence 1 is never trusted.
- **Partial mismatch** (some tracks match) → sector damage → recovery.
  **All-tracks mismatch on an in-DB disc** → the offset is wrong → configuration,
  not recovery.
- `crc450` is a v1-style checksum over the single sector at track offset 450 with
  a **local** 1-based multiplier (1..588). Zero-guard required (many blocks store
  `crc450=0`); tracks shorter than 451 sectors have no window.

### 3.4 CTDB: CRCs and parity (2.2, 2.3)

CTDB stores, per submitted pressing, per-track CRC32s and a Reed-Solomon parity
record over the disc PCM arranged as interleaved columns of 16-bit words
(GF(2^16)). With npar parity words per column, RS decoding corrects up to
⌊npar/2⌋ unknown-position errors per column — or up to npar *erasures* when the
positions are known. The decode pipeline (standalone `ctanalyse`, analyse-only;
Python applies the repair):

1. locate the CTDB entry and consensus offset (CRC sweep);
2. compute syndromes; with a C2/zero-fill erasure bitmap, build the erasure
   locator, compute modified syndromes, run Berlekamp-Massey on the clean tail,
   combine into the errata locator; Chien search + Forney values;
3. **re-validate syndromes after correction** — the guard against over-capacity
   miscorrection (RS decoding beyond capacity produces confidently wrong output);
4. commit only if the CTDB per-track CRCs **and** AR both pass (double gate).

The C2-erasure boost is a *modifier*, not a separate method: error-only decode
already repaired the reference damage; erasures extend reach when damage density
approaches capacity. Driver-author gotcha: build erasure bitmaps with
`np.packbits(bool_arr, bitorder='little')`, never fancy-indexed `|=` (duplicate-
byte updates are silently dropped, and C2 flags cluster).

### 3.5 Multi-pass speed sweeps (2.5)

The model that survived the experiments: an uncorrectable read at a given moment
is a **stochastic lottery**. Consequences, all observed:

- single-pass recovery at *any* fixed speed is unreliable;
- same-speed repetition can fail persistently (systematic miscorrection at that
  speed);
- **speed diversity is the lever** — winning speeds across the recoveries were
  32X, 32X, 4X, 40X, 40X, 32X: no consistent winner, so no "magic speed" exists;
- the paranoia engine contributed nothing measurable: plain raw READ CD re-reads,
  gate-verified, recover at the same rate (6/6).

Implementation shape (shipped): ladder probed per drive; fastest→slowest ×
`recovery_passes` (fast attempts are cheap, spend them first); each attempt is a
raw window read → offset-corrected slice → `match_track_pcm` → sample-exact
splice of the *verified corrected bytes* at `track_start*2352 + read_offset*4`.
Splicing corrected bytes at the shifted position means the ±offset samples that
"belong" to neighbouring tracks' checksum windows are never touched — proven by an
11/11 whole-disc re-verify after three independent splices.

**Statistics discipline** (load-bearing for the bench, §12): a single sequential
run confounds speed with attempt order and sample size one. Only a randomized
`--characterize` mode (Wilson 95% CIs, shuffled trial order, re-seating held
constant) can attribute an effect to speed.

### 3.6 The re-acquisition stack: retries, cache-defeat, mode page 01 (2.9, 2.10)

Drives cache aggressively; a naive re-read after a failure often returns cached
(possibly interpolated) data. The retry ladder issues a seek-away read (~5,000
sectors distant) between per-sector attempts so every retry is a genuine
re-read. Sense keys are classified (medium / hardware / other) — a
hardware-heavy pattern points at the transport.

Mode page 0x01 (read error recovery) exposes an error byte and a retry count
(PX-716A default: 0x00, 10 retries). The hypothesis was that fast-fail
(`0x20,1`) would shorten failed attempts and/or change C2 honesty.

**Experiment verdict (2026-07-05): inert for this defect class — rejected for
adoption.** Three arms (default, `0x20,1`, `0x00,1`), 5 interleaved reps each,
whole-track 40X reads with C2 vs an AR-verified oracle: latency 4.8 s in every arm
(the retry count is never consulted because **no SG_IO command ever fails** on a
miscorrection defect — `hard=0` across every run this disc has produced),
precision 0.992–0.994 / recall 0.991–0.994 in all arms, AR 0/5 everywhere.
Mode-page policy governs the command-failure regime; disc rot of this kind never
enters it.

The experiment paid for itself twice: it forced the empirical re-measurement of
the **C2/audio alignment** (−2 sample pairs; §3.1) and upgraded the C2 honesty
numbers on real rot at full speed (per-read FN 2–9 pairs out of ~600 wrong; recall
99.2% pooled).

### 3.7 The DB-gap: relative methods (2.6, 2.7, 2.11)

For a disc in neither AR nor CTDB there is no absolute gate, and the pipeline's
honest answer is "rip, report, cannot verify". The reserved design:

- **overlap checking** (2.7) detects slips: consecutive chunks overlap by k
  sectors and the overlap regions must be byte-identical; a mismatch localizes a
  positioning error — precisely the C2-invisible class;
- **intra-read verification** (2.6) re-reads within the pass and compares;
- **consensus voting** (2.11) arbitrates: per-sector majority across
  speed-diverse passes, optionally down-weighting C2-flagged bytes.

They are unbuilt on purpose. The bar: a real disc that (a) fails AR/CTDB
coverage, (b) resists the sweep, and (c) would plausibly yield to consensus. Until
that disc exists, building this is speculation with a real maintenance cost — and
its output would still be a *relative* claim, recorded as such, never presented as
verified.

---

## Part IV — The AccuDisc read/recovery surface

*How the read engine exposes recovery. Recovery is not a mode — it is a set of
independent knobs on `accudisc read`, thin wrappers over the library read API.*

### 4.1 The three orthogonal axes

| axis | flags | what it controls | default |
|------|-------|------------------|---------|
| **capture** | `--no-c2` / `--c2beb`, `--c2f`, `--sub raw\|q` + `--subf`, `--fulltoc`, `--cdtext`, `--pcm` | *what data* leaves the drive per spin | C2 **on**, sub off, PCM off |
| **recovery** | `--retries K`, `--c2-retries N`, `--verify P`, `--overlap K`, `--ladder LIST` | *how hard* to fight for a correct read | retries 2, rest **off** |
| **speed** | `--speed X`, `--uncap` | drive rotational speed (SET STREAMING; `--uncap` needs a driver) | drive default |

Two measured facts about capture that shape everything (§12.7):

- **C2 is free** — it rides in the same READ CD as the PCM (294 B/sector
  appended). Capturing it costs ~0%. *Always* take it. `--no-c2` exists only for
  the deliberately-minimal fast path.
- **Subchannel costs ~25%** — raw P–W is a separate, slower decode (~20× vs ~25×
  effective). It is the price of the "one careful pass."

### 4.2 The escalation ladder (the tested permutations)

Each rung is a superset of the one above. **The `scope` column is load-bearing —
read it before the flags**: the identical `--verify 2` is a ~20-second repair or a
~4-hour mistake depending on it.

| rung | scope | added flags | purpose | advantage | disadvantage |
|------|-------|-------------|---------|-----------|--------------|
| **capture pass** | whole-disc | `--c2f … --sub raw --subf …` | the "one careful pass": PCM+C2+Q+TOC in a single spin | complete picture at ~1.25× the minimal cost; the correct **default** | +25% over minimal |
| **R0 baseline** | whole-disc | `--retries 2` | plain read, error-retry only | free on clean sectors | no recovery |
| **R1** | whole-disc | `+ --c2-retries 3` | hunt a C2-clean copy of each flagged sector via cache-defeated same-speed re-reads | **cheap and effective** (cut C2 58→2 on ABBA at +0 s); cost ∝ damage → whole-disc-safe | same-speed re-reads share the drive's failure mode; can't fix a *deterministic* miscorrect |
| **R2** | **flagged spans** | `+ --verify 2` | read everything ≥2× cache-defeated, resolve by consensus | catches non-deterministic errors the single pass hides; marks irreconcilable as `suspect` | **~5–15× slower** — reads the whole *scope* again; whole-disc = the ~4 h blow-up |
| **R3** | **flagged spans** | `+ --verify 2 --c2-retries 3 --overlap 4` | full careful recovery: consensus + C2-hunt + seam check | **reached C2 = 0 on every damaged span** in the suite; the recommended repair rung | slowest short of the ladder; `--overlap` for the positioning-slip class only |
| **R4** | **flagged spans** | `+ --ladder 8,4` | escalate residual problem sectors to careful-mode speed | recovers the most (rescues speed-marginal sectors R1–R3 can't) | highest cost; only earns it when R3 leaves residue |

**The scope rule that must not be mis-read:** a rung is whole-disc-safe iff its
re-read cost scales with **damage**, not with **scope**. `--retries` re-reads only
errored sectors; `--c2-retries` re-reads only C2-flagged sectors — both ~0 on
clean media, so they ride the whole-disc capture pass. `--verify P` re-reads
**everything in scope P times** (cache-defeated), and `--overlap`/`--ladder`
compound that — cost scales with the sector count you point them at. Run R2–R4
over **only the map-derived flagged spans** (`--start L --count N`), never
whole-disc. Production model: whole-disc capture (R0/R1) → cluster flagged spans
from the `--map-file` → R2–R4 on those spans → splice → re-gate whole-disc.

### 4.3 How the knobs combine and conflict

- **Capture ⟂ recovery.** Orthogonal, but recovery is **audio-keyed**: it acts on
  C2/HARD/SUSPECT sectors, *not* on Q-CRC failures (§12.7, Part VII). Capturing Q
  does not make recovery repair Q.
- **`--verify` and `--c2-retries` are complementary.** `--verify` attacks
  *non-deterministic* errors (reads disagree → vote); `--c2-retries` attacks
  *C2-flagged* sectors (hunt a clean copy). Both share the R6 warning: same-speed
  consensus converges on the *wrong* answer for a deterministic miscorrect — which
  is why `--ladder` exists.
- **`--ladder` ⊐ `--verify`.** The ladder is the speed-diverse form of consensus.
  Prefer it over piling on `--verify N` at one speed.
- **`--overlap` is single-purpose** — the C2-invisible positioning-slip class. It
  costs ~1.77× for zero benefit on discs without slips. Opt-in (R3+), never a
  default. Run `features --stream` first: `accurate_stream yes` → skip it.
- **`--speed` interacts with everything and lies.** The requested Nx is *not* the
  achieved rate (§12.7). Set it, then confirm via wall-time or GET PERFORMANCE.
  Some request values are actively Q-hazardous (§12.7).
- **C2 is optional, and the non-C2 path is whole.** A `--no-c2` read still
  delivers audio + raw sub + Q + lead-in, and `--retries`/`--verify`/`--overlap`/
  `--ladder` all still *actively recover* (driven by hard errors + PCM consensus +
  seam checks instead of C2 flags). Only `--c2-retries` is a legitimate no-op
  without C2. What changes is the **detection surface, not the mechanism**:
  without C2 there is no per-sector erasure locator, so a `--no-c2` map never
  contains state `0x2` and needs-recovery reduces to `{0x3 HARD, 0x5 SUSPECT}`.
- **Recovery combos must each read from scratch.** `--verify`/`--c2-retries`
  defeat the drive cache internally; chaining combos so one warms the cache for
  the next measures cache hits, not recovery.

### 4.4 Access via the C API

Everything below is in `include/accudisc/accudisc.h` (the ABI contract; bindings
are generated against it, never against `src/`).

```c
accudisc_device *dev = accudisc_open("/dev/sr0", 0, &err);

/* Capability gate: claim + smoke + combos; and the slip-risk probe. */
accudisc_features f;   accudisc_probe_features(dev, &f);
uint8_t as;            accudisc_probe_accurate_stream(dev, lba, &as);

/* One commanded read with caller-selected strategies. */
uint8_t *map = /* count bytes, caller-owned, e.g. mmap'd shared */;
accudisc_read_req req = {
    .lba = start, .count = n,
    .c2  = ACCUDISC_C2_PTRS,        /* C2 capture              (2.1) */
    .retries = 2,                    /* retry ladder            (2.9) */
    .c2_retries = 4,                 /* C2 rescue               (2.1) */
    .verify_passes = 3,              /* intra-read verification (2.6) */
    .overlap_sectors = 2,            /* boundary overlap        (2.7) */
    .speed_ladder = (uint16_t[]){32,16,8,4}, .ladder_len = 4,
    .speed_x = 24,                   /* streaming speed         (2.5) */
    .status_map = map,               /* frame-accurate status surface */
    .cancel = &cancel_flag,
};
accudisc_read_stats st;
accudisc_read_cdda(dev, &req, sink_fn, sink_user, &st);
```

- **Data delivery**: the sink callback receives chunks; per sector the layout is
  AUDIO (2352) ‖ C2 (294/296) ‖ SUB (96/16), always captured in a *single* READ CD
  transfer so the three never desync. Accepted rescue/consensus reads replace the
  whole sector.
- **Status map**: one byte per sector, relaxed atomic stores — poll from any
  thread/process at zero syscall cost. Low nibble = state
  (`ACCUDISC_MAP_PENDING/OK/C2/HARD/RECOVERED/SUSPECT`), high nibble = severity.
  All states are **relative claims**.
- **Zero-fill contract**: a hard-unreadable sector is delivered as PCM 0 / C2
  all-ones / SUB 0; the synthetic C2 is excluded from stats. This is the erasure
  feed for downstream RS repair.
- **Probes**: `accudisc_probe_c2_lag(...)` — lag in sample pairs plus peak/runner
  agreement (`ACCUDISC_ERR_NOTFOUND` = inconclusive); `accudisc_probe_speed_ladder
  (...)` — per rung requested vs page-2A-reported vs timed-read-measured (centi-x).

### 4.5 Access via the binary

The subprocess contract is frozen in `docs/cli-machine-interface.md`:

- **Exit codes**: `0` clean · `1` usage/local · `2` fatal · `3` completed with
  caveats (`read`: hard/suspect/residual-C2; `cdtext`/`fulltoc`/`scan`: data
  absent). `features` exits 0 iff C2 is usable.
- **`--progress-fd N`**: `progress <done> <total>` lines, then one
  `summary hard= c2= recovered= suspect= rereads= slips= subq_ok= subq_total=`
  line. **Parse tokens, not positions** (the contract permits appended keys).
- **`--map-file F`**: the status map as a file — exactly `count` bytes, byte *i* =
  LBA `start+i`, `ACCUDISC_MAP_*` encoding, live through a `MAP_SHARED` mapping. No
  header. Progress = non-PENDING count.
- **`--map`**: human terminal rendering (`.` ok, `r` recovered, `!` C2, `?`
  suspect, `X` hard) — not an interface, do not parse.
- Human stderr (progress line, summary block, log messages) is explicitly **not**
  parseable interface.

A typical secure span-recovery invocation:

```sh
accudisc read --start 111142 --count 9480 \
    --retries 3 --c2-retries 4 --verify 3 --overlap 2 \
    --ladder 32,16,8,4 --speed 24 \
    --pcm t8.pcm --c2f t8.c2 --sub raw --subf t8.sub \
    --map-file t8.map --progress-fd 3 -q
```

### 4.6 MMC over SG_IO, and vendor opcodes

AccuDisc talks to drives with standard **MMC** over Linux `SG_IO` — unprivileged
(`cdrom` group; plain reading works on a read-only fd). Recovery-relevant
commands: `READ CD (0xBE)` (C2 field + subchannel in one transfer), `READ
TOC/PMA/ATIP (0x43)` (formats 0/2/5, byte-identical), `MODE SENSE/SELECT (10)`
(page 2A; error page 01 needs `ACCUDISC_OPEN_RDWR`), `CDROM_SELECT_SPEED` ioctl,
and decoded **sense data** (key 0x3 medium, 0x4 hardware — tallied separately).

**Proprietary opcodes** (e.g. Plextor 0xEA behind `cxscan`) live exclusively in
external driver `.so` files. The gate order: identify drive → locate driver →
*the caller's attach call is the permission grant* → selftest (read/set/re-read
real device state) → use; any failure falls back to generic MMC. The core stays
pure MMC/SG; only factual data tables (read offsets) are core-eligible. All five
0xBE C2/subchannel combos work on tested hardware — no vendor raw-read path
(0xD8) needed.

### 4.7 Separation of duties — what AccuDisc will not do for you

**AccuDisc reads and reports. It never verifies, never looks anything up, never
repairs beyond re-acquisition, and never post-processes** (no offset correction,
no de-emphasis, no gap handling). Every quality claim is **relative**. Only
absolute gates (AR/CTDB, or an existing verified image) establish correctness,
and those live in the caller (AccuDisc does no network I/O, ever).

The caller's recovery loop, in cost order:

1. **Gate** the delivered PCM against an absolute reference, per track, in the
   *raw* (uncorrected) offset domain — apply the drive offset exactly once, at
   storage.
2. On failure, **locate**: C2 stream + zero-filled sectors are known-position
   erasures; the status map and `first/last_flagged` span bound the damage.
3. **Repair without reads** first: RS parity (CTDB) with the erasure feed. Re-
   validate syndromes and re-gate; over-capacity decodes lie.
4. **Re-acquire**: targeted `read --start/--count` sweeps across *diverse speeds*
   (repeat invocations with different `--speed`; the set speed persists between
   invocations on purpose), and/or a secure re-read with
   `--verify/--overlap/--c2-retries/--ladder`. Gate each candidate; splice only
   verified bytes, sample-exactly.
5. **Record** what remains: keep the best-effort PCM, the status map, and the
   summary counters as provenance. Never present a relative pass as verification.

How to use each failure signal correctly:

| Signal | Meaning | Correct use | Incorrect use |
|--------|---------|-------------|---------------|
| C2 flag fired | byte almost certainly wrong | erasure position; rescue target | — |
| C2 clear | *no claim* | — | treating as "byte correct" |
| `HARD` / zero-fill | known-position total loss | erasure position | counting the zeros as audio |
| `RECOVERED` | two independent reads agreed | keep, but still gate | skipping the gate |
| `SUSPECT` | no two reads ever agreed | re-acquisition target; exclude from splices | delivering silently |
| `slips > 0` | positioning instability seen | distrust *unflagged* regions too | assuming damage is only where flagged |
| exit 3 vs 0 | relative caveat vs none | branch clean/degraded cheaply | reading exit 0 as "verified" |

---

## Part V — Users (plain-language guide)

*You don't need the theory above to rip a disc. AccuDisc tells you how the read
went; it cannot tell you the data is correct — only a comparison against an
outside reference (AccurateRip/CTDB) can.*

### 5.1 What can go wrong

- **The drive knows it failed** — scratches/rot beyond the disc's error
  correction. The drive flags the bad bytes (C2 pointers).
- **The drive got it wrong and doesn't know** — error correction "fixes" data
  into something wrong, or the drive momentarily reads the *wrong part of the
  disc* perfectly. No flag. The sneaky case, and most of the machinery exists
  because of it.
- **The drive gives up entirely** — the sector comes back as an error, not data.

### 5.2 The knobs (`accudisc read`), everything off by default except C2

- **C2 flags (default on).** The drive marks bytes it knows are bad. Free.
- **Retries (`--retries K`).** Retry a failed sector up to K times, reading
  elsewhere in between so the drive can't replay its cache. A sector that never
  succeeds is written as **silence** and clearly marked — files never lose sync.
- **C2 rescue (`--c2-retries N`).** For every flagged sector, hunt a clean copy;
  the best copy wins.
- **Verify passes (`--verify P`).** Read everything P times and compare;
  disagreements re-read until two agree ("recovered") or give up ("suspect").
- **Overlap checking (`--overlap K`).** Reads each batch slightly past its end and
  compares the overlap — catches the drive drifting position, which no flag
  reports. Run `accudisc features --stream` first: `accurate_stream yes` → skip it.
- **Speed ladder (`--ladder 32,16,8,4`).** Problem sectors retried at different
  speeds — a spot that fails at 32× often reads fine at 8×, and *variety* matters
  more than slowness.

### 5.3 Quick recipes

```sh
# Does my drive support C2 error pointers? (exit 0 = yes, usable)
accudisc --device /dev/sr0 features

# Does my drive have Accurate Stream? (if yes, --overlap is unnecessary)
accudisc --device /dev/sr0 features --stream

# Show the disc layout / raw full TOC / CD-Text (lead-in only — instant)
accudisc --device /dev/sr0 toc
accudisc --device /dev/sr0 fulltoc disc.fulltoc
accudisc --device /dev/sr0 cdtext disc.cdtext

# The full archival capture: audio + C2 + subchannel + a live status map, ONE pass
accudisc --device /dev/sr0 read --pcm disc.pcm --c2f disc.c2 \
         --sub raw --subf disc.sub --fulltoc disc.fulltoc --map-file disc.map

# Re-read one span slowly
accudisc --device /dev/sr0 read --start 111142 --count 9481 --speed 8 --pcm span.pcm

# What speeds does the drive actually achieve on this disc? (measured, not requested)
accudisc --device /dev/sr0 speeds

# Disc-health census on a Plextor drive (then aggregate with tools/cx_census.py)
accudisc --device /dev/sr0 cxscan > census.txt

# Park the spindle when done (motor keeps spinning otherwise)
accudisc --device /dev/sr0 park
```

### 5.4 What a ripper typically does (cdda2img is the reference)

1. runs a fast `read` with C2 on;
2. checks the result against **AccurateRip/CTDB**;
3. if a track fails, uses the C2 flags + silence-marks as a *damage map* to drive
   repair — **CTDB parity can rebuild bytes without re-reading** (first exit), or
   re-reads the failed span at several speeds until the checksum passes;
4. records anything unrecoverable honestly rather than hiding it.

If the disc isn't in any database, the relative signals (suspect counts, the
status map) are all there is — treat them as "how confident the drive was", never
as proof of correctness.

*(Note: `c2read` was the retired C prototype AccuDisc replaced. It survives only
as a reference/fallback; new work targets `accudisc`.)*

---

## Part VI — History: discovered, adopted, rejected

### 10. Chronology of the evidence

All on the same reference disc (a 1988 pressing with a 40×-repeatable defect
region in track 8) unless noted:

1. **C2 confusion matrix** (5 passes vs AR-verified oracle): precision ~99%,
   recall ~99% for decode errors, **total blindness to a positioning slip** → C2
   demoted from candidate gate to locator (R4). Also fixed: READ CD returns s16le;
   raw reads sit at the drive's +30 read offset; C2/audio alignment k=−2.
2. **CTDB parity pipeline** (ctdb_probe → ctanalyse → ctdb_repair): error-only RS
   repair validated end-to-end (CRC + AR double gate); C2-erasure decode validated;
   over-capacity refusal and false-positive guards tested. Adopted as the first
   exit.
3. **c2timing**: C2-guided *targeted* re-reads are not a time win (R3) — variance
   dominates, plus a run-up penalty. Conclusion: C2 = erasures for parity, not a
   re-read optimizer.
4. **paranoia_recovery_test** (cd-paranoia era): single-speed single-pass recovery
   unreliable; eject/reseat/reset worthless (R1); the passes × speeds sweep is what
   recovers.
5. **cdrtools review**: adopted the Q-Check census idea and mode-page tuning as an
   experiment; dismissed `-edc-corr` (R5) and the libscg transport (SG_IO suffices
   unprivileged).
6. **c2read upgrade (Phases 1–5)**: single-pass capture of audio+C2+sub+lead-in;
   zero-fill contract; retry ladder + cache-defeat; census; speed report. The C2
   rip path became one read.
7. **c2read_recovery_test** (2026-07-05): 3/3 sequential recoveries, AR-only gate,
   no C2, no CTDB (+ 3/3 earlier baseline = 6/6, all byte-identical at conf 200) →
   the paranoia engine retired from recovery (R2); `_recover_failed_tracks`
   rewritten onto the raw-domain span read; offset domain unified completely.
8. **Census first light**: all 256 CU errors inside the known track-8 defect span;
   thousands of stage-2 corrections on four tracks that AR passes at conf 200 —
   disc-rot early warning demonstrated.
9. **Mode-page 01 experiment** (2026-07-05): drive-side error-recovery tuning is
   inert for the miscorrection class — rejected for adoption (2.10); flag kept as a
   manual diagnostic.
10. **C2 alignment pinned** (2026-07-05): TP-argmax sweep found the bitmap lags the
    audio by exactly 2 sample pairs (precision 0.993 at the peak, 0.27 at the wrong
    sign); the historical "k=−2" note was the same lag in the opposite convention.
    Honest recall on real rot at 40X: 99.2% pooled, FN 2–9 pairs per read.
11. **c2read → AccuDisc flip** (2026-07-11): the C2 read path moved to the external
    AccuDisc engine (subprocess, git-ignored `tools/accudisc/` snapshot); flag →
    subcommand deltas absorbed in `accudisc_reader.py`. Both drive probes (C2 lag,
    speed ladder) shipped and live-validated.

### 10.1 Rejected outright (don't rebuild)

- **R1** eject/reseat/reset between passes — no measured benefit; speed diversity
  did all the work.
- **R2** cd-paranoia engine as the re-reader — plain gate-verified re-reads recover
  6/6; the paranoia algorithm added nothing. (cd-paranoia's remaining roles: the
  full-disc *read* fallback when cdrdao fails, and the cosmetic track-1 preview.)
- **R3** C2-guided *targeted* re-reads as a time optimizer — variance dominates the
  per-read saving; recovery is a stochastic lottery.
- **R4** C2 as a standalone correctness gate — the 8,852-sample slip refutes it
  empirically. Excellent hint, unacceptable gate. **The single most important
  constraint in the model.**
- **R5** readcd `-edc-corr` — operates on data-sector EDC/ECC that CD-DA audio
  sectors don't have.
- **R6** same-speed consensus — persistent same-speed miscorrections converge on
  the *wrong* answer; consensus must be speed-diverse.
- **R7** vendor 0xD8 raw-read path — unnecessary; all five 0xBE C2/subchannel
  combos work on tested MMC drives.

---

## Part VII — Q-CRC semantics for callers

`subq_ok / subq_total` (the `Q-ok %` in every summary line) is the fraction of
subchannel-Q frames whose **CRC-16 validated**. Operationally:

- **A CRC-valid ADR=1 Q frame is trustworthy** to ~1 in 65,536 false-accept — the
  CRC-16 *is* the confidence gate. The threshold for "an index/pregap boundary is
  correctly recovered" is simply: the boundary's ADR=1 Q frames pass CRC. No softer
  heuristic is needed (`accudisc pregaps` / `index_map` gate on exactly this).
- A frame that **fails** CRC carries *no usable position* — its MSF/track/index
  bytes are garbage and must not be decoded (decoding CRC-fail frames manufactures
  phantom boundaries).
- **Q-ok % is a whole-pass health signal, not a per-sector one.** A cratered global
  figure means the *pass* was disturbed (§12.7) — re-read it. A steady
  ~98–99.8% with a few hundred scattered bad frames is the normal static floor.
- **What Q affects:** LBAs / pregap positions / index marks / MCN / ISRC — hence
  strict archival and cue-accuracy. **What Q does *not* affect:** AccurateRip, CTDB,
  and MusicBrainz/CDDB Disc-ID are computed from **audio + the TOC track offsets**,
  not per-frame Q. So a disc can rip with poor Q-ok % and still gate green on
  AR/CTDB — *provided the TOC LBAs are right.*

---

## Part VIII — Agents (cross-project coordination contract)

- **Document identity.** This single hardlinked file is the source of record for
  both projects. Component numbers (§2) are the cross-reference key. When either
  project changes recovery behaviour, update this doc in the same change.
- **Interfaces you may rely on** (stable, additive-only): the AccuDisc exit-code
  convention, `--progress-fd` tokens, the `--map-file` byte format, the `features`
  output keys, stream geometry (2352/294/96, zero-fill, no offset correction),
  speed persistence across invocations, and `include/accudisc/accudisc.h`. Full
  contract: `accudisc/docs/cli-machine-interface.md`. **Never parse human stderr.**
- **Invariants you must not violate:**
  1. Relative signals (C2, consensus, overlap, the status map) never outrank or
     substitute for absolute gates. `exit 0 ≠ verified`.
  2. AccuDisc performs no lookups, no network I/O, no post-processing, no silent
     offset correction — do not add "convenient" processing.
  3. Vendor/proprietary anything stays in external drivers behind the attach gate;
     the core stays pure MMC/SG. Factual data tables are the only exception.
  4. AUDIO/C2/SUB for a sector always come from one READ CD transfer; replacements
     are whole-sector.
  5. Streams are always exactly `count` sectors (zero-fill contract).
  6. No per-chunk speed switching in verify passes (measured recalibration thrash);
     ladder rungs apply to per-sector arbitration reads only.
- **Evidence discipline.** Components rejected with measurements (R1–R7) stay
  rejected unless new evidence is produced. Unbuilt items (C2-weighted voting,
  mode-page 01 diagnostic, the DB-gap relative layer) stay unbuilt until a
  justifying disc exists.

---

## Part IX — Open questions

- ~~**crc450 blind-offset detection**~~ — **CLOSED 2026-07-21**, shipped as
  `accuraterip.detect_offset`. See Part XI, which also records what building it
  exposed: a rip can verify at *several* offsets at once.
- **CTDB CRC as a sweep gate** for discs in CTDB but not AR (the sweep gates on AR
  only). A read/gate split is the moment to make the gate pluggable rather than
  hardcoded to AR.
- **Q valid-frame variance**: per-rip usable Q-frame counts vary wildly (157k vs
  72k on the same disc); the voting floors and TOC-authority absorb it, but it is
  unexplained.
- **The justifying disc** for the DB-gap relative layer (§3.7) has not been found —
  by design, nothing is built until it is.
- **The recovery-bench methodology** (§12) — turning the working harness into a
  valid characterization instrument for the *stochastic* audio-recovery ladder.

---

## Sources & licensing note

The MMC command semantics behind the speed/streaming behaviour (SET STREAMING
0xB6, GET PERFORMANCE 0xAC, the §6.39 performance-descriptor model) are documented
in **T10 MMC-5**, a proprietary, licensed document that must never be
redistributed. The *summaries and empirical findings* here are transformative and
fact-based (field offsets and observed drive behaviour are not copyrightable) and
are freely shareable. AccuDisc's `private/` git-ignore rule enforces the
non-redistribution of the source document by construction.

---

## Part X — The 3-disc recovery test-suite report (2026-07-20)

*Harness: `cdda2img/tools/recovery_bench.py`. Drive: Plextor PX-716A (read
offset +30, Accurate Stream = yes). Data: `private/bench/runs/run1/` (per-disc,
2026-07-19) and `run2/tracy_full.toml` (the full Tracy matrix with fine-grained
progress, 40 rows, 2026-07-19). **Provisional** — the harness is still maturing;
read §12.5 before drawing method conclusions.*

### 12.1 What the bench does

Per swept speed it takes **one whole-disc baseline capture**, clusters the
map-flagged needs-recovery spans (state ∈ {C2, HARD, SUSPECT}), then applies each
recovery rung to **only those spans**, splices into a copy of the baseline, and
re-gates the whole disc against AccurateRip and CTDB. `ctdb`/`ctdb-noc2` are
different: parity repair on the baseline PCM with **zero extra reads** (`ctdb` =
C2-erasure-assisted at the measured c2lag; `ctdb-noc2` = error-only, the
faithful stand-in for a drive with no C2). The AccuDisc side supplies the
relative signals (C2, Q-health, the status map); cdda2img supplies the absolute
gates (AR v1/v2, CTDB CRC + parity) and the disc geometry.

### 12.2 Disc coverage

| Disc | Speeds | Rungs | C2 | Damage profile | Result |
|------|--------|-------|----|----|--------|
| ZZ Top | 40,32,24,16,8,4 | R0–R4 | on | clean | 36 rows, Q 0.998–0.999, 0 C2, AR/CTDB pass all speeds, no cliff |
| ABBA *Gold* | 32,24,8,4 | R0–R4 | on | clean-ish | R1 cut C2 58→2 at +0 s (damage-proportional rescue) |
| Tracy Chapman | 40,32,24,8,4 | R0–R4 | **off** | — | `spans=0` at every speed — C2 off = no localization surface |
| Tracy Chapman | 40,32,24,8,4 | R0–R4 + ctdb + ctdb-noc2 | on | localized, **intermittent** | the full matrix — §12.3 |

**Gap:** ZZ Top and ABBA predate the ctdb rungs (no CTDB parity data for either);
Tracy is the only disc with full coverage. A fair cross-disc CTDB comparison needs
a backfill pass on both (deferred).

### 12.3 The Tracy full matrix (run2)

Tracy is in AccurateRip and CTDB (cddb `99087b0b`, 11 tracks, lead-out 162891).

**Baselines — damage is intermittent, not speed-monotone:**

| Speed | Q-ok % | C2 | AR v2 | CTDB | wall |
|-------|--------|----|----|------|------|
| 40× | 0.980 | 0 | ✅ | ✅ | 127.8 s |
| 32× | 0.980 | 0 | ✅ | ✅ | 125.1 s |
| **24×** | 0.981 | **2** | ❌ | ❌ | 164.3 s |
| 8× | 0.980 | 0 | ✅ | ✅ | 294.2 s |
| **4×** | 0.980 | **4** | ❌ | ❌ | 555.8 s |

The disc reads *clean* at 40/32/8× and *dirty* at 24/4× **in one run** — clean at
8× but dirty at 4×, clean at 32× but dirty at 24×. That non-monotone pattern is
the stochastic-lottery model (§3.2/§3.5) made visible: this is marginal damage
that manifests per-pass, **not** a speed cliff.

**R0–R4 (audio span recovery) on the two dirty baselines:** every rung re-read the
1–4 flagged sectors and recovered 0–3 of them by the C2 counter — but **every
single row failed AR v2 and CTDB** (`ar_v2=false, ctdb=false` throughout). Zero
verified recoveries from the audio ladder in this run.

| Rung @ dirty speed | recovered | C2 left | AR v2 | wall |
|--------------------|-----------|---------|-------|------|
| R0 @ 24× | 0 | 2 | ❌ | 24.0 s |
| R1 @ 24× | 1 | 1 | ❌ | 12.5 s |
| R3 @ 24× | 1 | 1 | ❌ | 13.9 s |
| R4 @ 4× | 3 | 0 | ❌ | 37.8 s |
| *(all other R0–R4 dirty rows)* | 0–2 | 0–3 | ❌ | 15–32 s |

**ctdb / ctdb-noc2 (parity repair, zero reads) — the only verified recoveries:**

| Rung @ speed | CTDB checksum | repaired | wall |
|--------------|---------------|----------|------|
| ctdb @ 40/32/8× | ✅ pass | (n/a — clean) | 8.6–8.7 s |
| ctdb-noc2 @ 40/32/8× | ✅ pass | (n/a — clean) | 8.6–8.7 s |
| **ctdb-noc2 @ 24×** | ❌ | ✅ **repaired** | 29.4 s |
| **ctdb @ 24×** | ❌ | ✅ **repaired** | 32.1 s |
| **ctdb-noc2 @ 4×** | ❌ | ✅ **repaired** | 39.0 s |
| **ctdb @ 4×** | ❌ | ✅ **repaired** | 42.8 s |

### 12.4 Findings

1. **CTDB parity repair recovered every dirty baseline; the audio ladder recovered
   none.** This is the run's one firm result, and it is trustworthy precisely
   because RS decode is *deterministic* over a fixed capture (unlike the stochastic
   re-reads). It directly validates the §1 cost order: parity repair (zero extra
   reads, ~30–43 s) is the correct first exit.
2. **Error-only parity was sufficient at Tracy's damage level.** `ctdb-noc2` (no C2
   erasure feed) repaired both dirty passes, and slightly *faster* than the
   C2-assisted `ctdb` (29.4 vs 32.1 s at 24×; 39.0 vs 42.8 s at 4×). The C2 boost
   extends *reach* near RS capacity (§3.4) — at 1–4 damaged sectors it is not yet
   needed. (This does not diminish C2's value on heavier damage; it bounds where
   the boost starts to matter.)
3. **C2-off = no localization, but detection is intact.** In the `tracy_noc2` run
   every speed reported `spans=0`: `--no-c2` never populates the C2 map state, so
   the span-finder sees nothing to target. That is *localization* lost, not
   *detection* — AR v1/v2 and CTDB checksums are C2-independent and still flag the
   mismatch (§4.3). The no-C2 fallback is: gate flags a mismatch → no localization →
   either a blind whole-disc re-read or straight to `ctdb-noc2` parity repair.
4. **A hypothesis was raised and retracted.** An early read of `tracy_noc2.log`
   looked like C2-off audio was *worse* than C2-on at the same speed. A paired
   same-speed confirmation read showed **both** conditions failing AR that time —
   Tracy's track 8 is independently flaky run-to-run; the earlier contrast was two
   unlucky single samples, not a causal C2 effect. No claim about C2 *request*
   changing audio fidelity survives this run.
5. **Accurate Stream confirmed on the PX-716A** (`features --stream` = yes), so the
   bench correctly dropped `--overlap` on R3/R4. Accurate Stream guarantees
   byte-identical reads of *intact* data; it does **not** guarantee identical
   outcomes on a genuinely damaged sector, where the drive's real-time
   retry/correction logic can decide differently per attempt — consistent with the
   intermittent baselines in §12.3.

### 12.5 Validity assessment

**Sound:** the infrastructure works end-to-end on real hardware across all three
discs — geometry from a lead-in fulltoc, the AR gate, the CTDB checksum gate,
non-destructive parity repair, span clustering, sample-exact splice, crash-safe
row emission, and the fine-grained progress/resource sampler (verified: it showed
`proc=ctanalyse rss=368MB` only while a repair actually ran, `idle` otherwise).

**But the current dataset cannot characterize the R0–R4 audio ladder**, for five
concrete reasons:

1. **One damaged disc, intermittently damaged.** ZZ Top and ABBA are clean; Tracy
   manifests damage on only 2 of 5 passes. R0–R4 were therefore exercised on
   exactly two dirty baselines, **n=1 each**.
2. **n=1 against a stochastic process.** §3.5's own discipline: only the randomized
   `--characterize` mode (Wilson CIs) can attribute an effect to a method. "R0–R4
   recovered nothing" is within noise, not a conclusion.
3. **R0–R4 re-read the flagged span at the *sweep* speed**, whereas production
   `_recover_failed_tracks` sweeps the *full* ladder per failed track. The bench
   never uses the fact that 8× read a region clean to repair the 24× capture — it
   under-tests the shipped recovery's single most effective lever (speed
   diversity). Only R4 has an internal `--ladder 8,4`.
4. **`wall_s` is not a common cost basis.** baseline = capture+gate (125–555 s);
   ctdb = repair only (8–43 s, *excludes* the capture); R0–R4 = span reread+gate on
   top of an existing baseline. `rank()` sorts these incomparable numbers together;
   the true end-to-end cost of the ctdb path is baseline capture **+** repair.
5. **Network confound + two harness bugs.** The run2 log shows recurring
   `AccurateRip https fetch failed: read timed out` during re-gates (it re-fetches
   per cell). And `classify()` (a) collapses all speeds to one arbitrary row per
   rung via `{r.rung: r for r in rows}`, and (b) is blind to the ctdb rungs — so its
   final verdict `{R0..R4: keep}` credits the audio rungs for the *clean* passes
   while they recovered nothing on the dirty ones.

### 12.6 Recommendation and next iteration

**Keep the harness; fix the methodology; then re-run — at the governing
weeks-to-months pace, not urgently.** "Accept as final" is wrong (the suite cannot
yet answer the question it was built for); "blind re-run" is wrong (the same design
reproduces the same un-attributable data). Before the next run:

1. Make R0–R4 sweep the probed ladder for each flagged span (mirror
   `_recover_failed_tracks`), **or** explicitly re-scope R0–R4 as "same-speed span
   re-read only" so results aren't read as production recovery.
2. Add **repeated passes per cell** for a recovery *rate*, not a single sample —
   reuse the existing `--characterize` statistical discipline.
3. **Cache AR/CTDB responses once per disc** (removes the network confound and most
   of the wall-time noise; matters on a flaky connection).
4. Fix `classify()`: key per-(rung, speed), and teach it the ctdb rungs.
5. Source at least one **consistently or gradably damaged** disc so the audio
   ladder is exercised more than twice, and the C2-boost's reach advantage (§12.4
   finding 2) can be found near RS capacity.

**Deliverable target (unchanged):** a portfolio of per-disc recovery profiles —
ranked, disc-specific recommended methods — not one universal method. This run
produced raw rows and one firm finding (parity-repair-first is correct); it did
not yet produce a profile.

### 12.7 Operational facts carried forward (prior 3-disc suite, 2026-07-18)

These are the operational drive facts an earlier three-disc suite established
(clean / disc-wide-marginal / localized-static profiles); Part IV depends on them
and they remain valid independent of the run1/run2 stochastic-sampling caveats.

1. **Audio is never lost.** Zero HARD sectors across all three discs at every
   speed. C2 errors are always either *speed-marginal* (clear by slowing) or
   *borderline* (clear by re-read consensus). **R3 reached C2 = 0 on every damaged
   span** — but C2 = 0 is a *relative* signal, not an absolute pass: §12.4 shows a
   run2 case where R3 cleared C2 to a residual 0–1 yet AR v2/CTDB still failed (a
   deterministic miscorrection C2 never flagged), which is exactly why C2 is a
   locator, not a gate (§1.1). Driving C2 to zero clears the *detectable* audio
   damage; only an absolute gate can confirm the bytes are right. The fragile layer
   is not the CIRC-protected audio — it is the subchannel.
2. **Capture cost: C2 is free, subchannel ~25%.** C2 rides the same READ CD as the
   PCM (~0% overhead — always take it); raw P–W sub is a separate, slower decode
   (~20× vs ~25× effective) — the price of the "one careful pass."
3. **Recovery is audio-keyed; Q is not targetable.** *Targeted re-reads*
   (`--verify`/`--c2-retries`/`--ladder`) fix **localized audio**, and only that.
   *Whole-disc speed* is the **only** lever that moves Q — and only its
   transient/speed-marginal part; residual Q (0.2 / 0.8 / 2.0% across the three
   discs) is largely **static** physical damage and no re-read combo improved it
   (Q carries a CRC but no error correction). "Slow the whole rip" and "re-read
   these spans" answer *different* problems.
4. **Speed request ≠ achieved rate, and some rungs are Q-hazardous.** Two gaps:
   (a) *request → accepted ceiling* — the drive quantizes to discrete steps (req 16
   → ceiling 8; `page2a` = accepted ceiling); (b) *ceiling → throughput* — CAV makes
   the ceiling an outer-rim target (req 40 → page2a 40 → measured ~18). Use
   `measured` as ground truth. Worse, specific request values intermittently crater
   Q at no throughput benefit: ZZ Top 32× (1 collapse in 4) and Tracy 16× (2 in 3)
   dropped whole-pass Q to ~33–39% while equivalent-speed neighbours held
   ~98–99.8%. **Gate on whole-pass Q-ok %: a pass whose global Q craters (< ~90%)
   is invalid — discard and re-read.** For `--sub` work, prefer the request that
   maps cleanly to the careful-mode floor (8×).
5. **The governor is free triage.** A drive that self-throttles its ceiling at init
   (ABBA → 32× before a sector is read) is signalling *disc-wide marginality* in
   advance; one that holds full speed (Tracy, ZZ Top → 40×) has localized or no
   damage. Read `accudisc speed` at load time and branch on it.

**Calling-program strategy** (priority: (1) completeness — recover all data,
best-effort before failure; (2) speed — only after (1)):

```
0. PROBE at load:  accudisc speed   (governor self-throttled? → disc-wide marginal)
                   accudisc fulltoc (offset quirks, e.g. ABBA's LBA-33)
1. CAREFUL PASS at careful-mode speed (~8x eff) with C2 + raw sub + --map-file.
      GATE: if whole-pass Q-ok craters (< ~90%) -> transient; discard + re-read.
2. AUDIO recovery: scan the map for C2/HARD/SUSPECT spans; run R3 over each span.
      escalate to R4 (--ladder 8,4) only if R3 leaves residue.  expected: C2 -> 0.
3. Q: accept the residual (static + irreducible; no re-read improves it). Record
      as-is for strict archival; ignore if the caller needs only audio + AR/CTDB
      (Q-CRC does not affect AR/CTDB or Disc-ID — but the TOC LBAs do).
4. GATE (caller-owned, absolute): AR v1/v2, CTDB. These, not our relative checks,
      pronounce the rip correct (§1 invariant).

SPEED-OPTIMISED VARIANT (only when Q is NOT required):
   fast pass at the governor ceiling + targeted audio recovery (step 2).
   ~2x faster on a damaged disc; leaves Q catastrophic. Never for archival.
```

Because targeted Q recovery is (strongly indicated to be) ineffective, "do we need
Q recovery" reduces to "do we need Q *at all*" — an archival-policy decision, not a
recovery-engine capability.

---

## Part XI — Offset detection: three mechanisms compared (2026-07-21)

Recovering *what offset a rip was made at* is a different question from recovering
*audio*, but it uses the same corpora and it settles two practical matters: whether
an unknown rip can be normalised to a known alignment, and whether a disc is a
pressing or a copy. A pressed disc read on a correctly-configured drive detects at
≈0; a CD-R detects at whatever composite its burner baked in, so a stable non-zero
reading on otherwise clean audio is positive evidence of a copy — and the value
fingerprints the machine that made it.

Three mechanisms now exist in this codebase. They are not redundant.

### 11.1 The three

| | `accuraterip.detect_offset` | `ctdb_repair.select_entry` / `tools/ctdb_probe.py` | ctanalyse `FindOffset` |
|---|---|---|---|
| **Corpus** | AccurateRip dBAR | CTDB per-track CRC32 | CTDB parity (RS syndromes) |
| **Search radius** | ±2939 samples | ±700 samples (`_SWEEP_WINDOW`) | ±2939 samples (internal stride 11760) |
| **Method** | frame-450 sub-CRC sweep, then exact v1/v2 confirmation | `zlib.crc32` of a whole track at every shift | one-column syndrome + Berlekamp–Massey per candidate |
| **Complexity** | O(n + radius) — sliding recurrence | O(n × radius) — a full CRC per shift | one whole-disc syndrome pass, then ~µs per candidate |
| **Damage tolerance** | prefilter survives damage outside 588 frames; confirmation is per-track, so a bad track costs one track | none — one bad sample voids that track's CRC | **yes, by construction** — accepts up to `allowed_errors`, escalating 0 → npar/2−1 |
| **Ambiguity** | reports every cohort, ranked | first qualifying candidate wins | first accepted offset wins |
| **Also tells you** | per-track match counts, cohort population | *which pressing* (CTDB entry id), which tracks are damaged | enough state to then repair |

### 11.2 The convergent ±2939

Both bounds are five sectors, arrived at independently. AccurateRip excludes 2940
frames at each disc edge from its checksums, so a larger shift would drag real audio
across the exclusion boundary and change *which samples are summed at all* — no
single shift could then reconcile it. CTDB excludes one full stride at each end of
the codeword grid (§3.4), and its internal stride of 11760 words is 5880 stereo
samples, half of which is again 2940. Different structures, same limit: **an offset
search is only ever meaningful inside the guard band the format reserved for it.**

### 11.3 What the AccurateRip detector exposed

Two things worth carrying forward, neither of which the other two mechanisms would
have surfaced, because both stop at the first answer they like.

**A rip can verify at several offsets at once, and the alternatives are not noise.**
Tracy Chapman's debut verifies fully at 8 offsets; the top two (0 and −669) tie at
identical cohort confidence. ABBA *Gold* verifies at 5, but decisively — 0 at
confidence 3575 against 1127 for the runner-up. The −669 twin also appears in CTDB
(entry 67116, long noted as "unexplained"), so it is a property of the *discs*, not
of one database: the same master pressed at different absolute positions, each
pressing its own cohort. Confidence ranks cohorts by population, which is not the
same as correctness. **Disambiguation has to come from outside the audio** — a known
drive offset, cross-corpus agreement, or the user. `detect_offset` therefore returns
a ranked list and `tools/fix_offset.py` refuses to rewrite a rip when the top two
candidates tie.

**The frame-450 prefilter has a systematic false-positive class.** ABBA *Gold* has an
offset where the sub-CRC matches on all 19 tracks and not one full-track checksum
agrees. Random collision is excluded (~1.4 M comparisons against 2³² predicts
0.0003 of them); the shape of it — every track, one offset — is what submitters
computing crc450 under a different convention look like. Hence the two-stage design
is not an optimisation with a safety net bolted on: the prefilter *nominates* and
only exact checksums *decide*. Anything gated on `.offset` without `.confirmed` is
wrong.

### 11.4 Which to reach for

1. **Disc in AccurateRip** → `detect_offset`. Widest corpus, widest radius, and the
   only one that shows you the alternatives.
2. **Disc in CTDB but not AR** → `ctdb_repair.select_entry`. It also names the
   pressing, which `detect_offset` cannot.
3. **Disc too damaged for either CRC to match** → ctanalyse `FindOffset`. The only
   mechanism that still works, because it asks "can this be *decoded* at this
   offset", not "does this *equal* the reference". This is its whole point and the
   reason it stays.
4. **When the answer matters** → run more than one. Agreement across two corpora is
   the practical answer to §11.3's ambiguity, since a coincidental cohort tie in
   AccurateRip is unlikely to be mirrored by the same tie in CTDB.

### 11.5 Carried forward

- The CTDB CRC sweep is the expensive one: 1401 whole-track CRC32 passes, which is
  why `select_entry` deliberately sweeps the *smallest* interior track. The
  sliding-window recurrence that makes the AccurateRip sweep O(n + radius) does not
  port directly — CRC32 is not a weighted sum — but rolling-CRC techniques exist and
  this is the obvious candidate for the same treatment. Not attempted; noted.
- `tools/fix_offset.py` covers the repair half: concatenate a per-track rip, shift
  the single stream, re-split at the original lengths. Doing it per file cannot be
  correct, because a shift moves samples **across track boundaries** — verified on a
  real rip, where a +100-sample shift moved audio at all 10 track boundaries (2 of
  them non-silent; the silent 8 are exactly why per-file shifting looks fine and
  quietly corrupts the ones that matter).

---

## Part XII — The limit of recovery: discs that can never verify (2026-07-21)

Every other Part in this document is about closing the gap between a read and
the truth. This one bounds the exercise: **some discs have no truth left to
reach, and the recovery engine working perfectly is what proves it.**

### 12.1 The observation

Two CD-R copies from Keith's shelf failed AccurateRip on *every* track. Both
read flawlessly — zero HARD sectors, zero C2, clean subchannel at 24× — so
nothing in Part II–IV had anything to act on. Neither could be offset-rescued
(Part XI), and the negative is trustworthy rather than merely unexplained:

- A **planted-offset positive control** passed on both discs. Synthesise
  reference checksums from the disc's own PCM at a known offset, then sweep: the
  machinery recovers it at full track count, on that exact data path. So a
  failure to find a real offset is a fact about the disc, not the code.
- The stronger case (disc 2) was swept **±500 000 samples (±11 s)** against a
  usable reference — AccurateRip confidence 4, CTDB 5, frame-450 data on all 10
  tracks. Zero hits at any offset on any track.
- **Not lossy-sourced.** Applying the auCDtect discriminator (it is the
  *consistency of the spectral cutoff across time segments* that betrays a
  codec, never the cutoff value — a quiet or dull passage has no cutoff to find):
  per-second cutoff spread sd 3.25 kHz / 2.72 kHz where a codec gives < 0.3 kHz,
  5–15 % of segments above 21 kHz, and both discs reach 22.05 kHz — exactly
  Nyquist. Genuine full-bandwidth audio.

### 12.2 The explanation, and why it is unfixable

One of the two still carries an authentic TOC: its MusicBrainz disc ID matches
the real release, so it is a copy of a real pressing, not a reassembly from
files. That original, Keith reports, was **badly damaged** before it was
discarded years ago.

A period ripper does not fail on unrecoverable samples — it **conceals** them,
interpolating from neighbours. Concealment does not lowpass anything, so
bandwidth stays full and the audio sounds fine; it simply makes the samples
*different*. Every checksum over that track is destroyed, permanently, and the
CD-R faithfully preserves the concealed audio.

So: **a disc can be correctly identified, perfectly playable, physically clean
to read, and permanently unverifiable.** The loss happened once, at rip time,
years before the disc reached this drive. No re-read strategy in Part II — no
speed diversity, no C2 erasure decode, no CTDB parity, no offset sweep — can
recover information that was never written to the disc.

### 12.3 Why this matters to the architecture

This is the cleanest demonstration yet of the §1 relative/absolute split.

- **The relative signals said the read was perfect, and they were right.** Zero
  HARD, zero C2, clean Q. AccuDisc returning that on a permanently unverifiable
  disc is a *correct and complete* result, not a missed recovery. Anything that
  treated "AR failed" as "the reader should try harder" would burn hours here
  and find nothing, on a disc that is being read exactly right.
- **Only the absolute gate can say the audio is not the pressing's**, and it
  did — immediately, on the first pass, at zero extra cost.

The practical rule: **an all-tracks AccurateRip mismatch is not a read problem
and must never trigger the re-read ladder.** It is either an offset (rescuable,
Part XI) or a provenance fact about the disc (not rescuable by anyone). The
production gate at `cdda2img.py:_ar_has_partial_mismatch` already encodes half
of this — it lets only *partial* mismatches reach recovery, on the reasoning
that a total mismatch means misconfiguration rather than read errors. Part XI
supplies what to do with the other half.

### 12.4 What a rip of such a disc should record

Verification is not achievable, so the honest deliverable is **provenance, not a
pass**: the identification (which succeeded — CD-Text, disc ID, MusicBrainz),
the read-quality evidence (zero HARD/C2, Q yield), the offset sweep's negative
*and its search radius*, the reference strength that negative was measured
against (a confidence-1 reference makes a mismatch nearly meaningless; the two
discs differed sharply here), and the lead-in health signal
(`degrade=leadin_unreadable` — on the Stanley Road disc the lead-in is entirely
dead while the program area still returns 147–150 good Q frames per boundary,
which is the lead-in failing *first*).

That set lets a future reader distinguish "we never checked" from "we checked
thoroughly and this disc cannot be verified" — which is the only useful thing
left to say about it.
