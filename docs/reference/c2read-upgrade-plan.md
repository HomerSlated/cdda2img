# c2read Upgrade Plan — cdrdao-free Ripping

Status: PROPOSED (2026-07-04). No implementation yet.

Goal: enumerate, rank, and design every feature `c2read` (plus its Python consumers)
needs for the **rip pipeline to function without cdrdao**. Disc burning is explicitly
out of scope (cdrdao remains the burn engine for `burn`); so is `mount` (cdemu needs a
TOC+BIN scratch copy, which the extract path already produces).

Design philosophy (unchanged from c2read's charter): **the C tool reads and reports;
all policy and decoding live in Python.** New c2read flags dump raw SCSI responses;
new/extended Python modules (`subchannel.py`, a new `cdtext.py`, a new assembly module)
turn them into a `ParsedDisc`/`RBIDisc`. This keeps the C surface small, testable, and
warning-clean, and keeps every judgement call in the language the rest of the pipeline
is written in.

Migration strategy is the strangler pattern used for B-1..B-5: cdrdao stays the default
primary until the c2read path proves byte- and field-parity on real discs; only then is
the default flipped and cdrdao demoted to an optional engine.

---

## 1. What cdrdao provides today (feature assessment)

The rip pipeline consumes exactly three cdrdao invocations:

| cdrdao call | What the pipeline takes from it |
|---|---|
| `read-cd` | audio BIN (s16be) **plus** the full TOC file: track layout, pre-gap lengths, per-track ISRC, MCN (`CATALOG`), CD-Text, `PRE_EMPHASIS`/`COPY` flags |
| `read-toc` | the same TOC file without audio (used by the C2 path today — the 181 s second pass) |
| `drive-info` | current/max read speed, CD-DA-correct kBps (consumed by `drive_speed.py`, where MODE SENSE page 2A is known to lie on the PX-716A) |

c2read already covers: raw audio via READ CD (0xBE) + C2 pointers, lead-out/track
boundaries via READ TOC format 0 (`--toc`), C2 capability probe (`--features`), read
speed set (`--speed`), spindle park (`--stop`).

Replacing cdrdao therefore decomposes into ten features:

- **F1 — Single-pass combined capture (audio + C2 + subchannel)** [C]
  READ CD byte 10 sub-channel selection alongside the C2 error field: one pass yields
  PCM + C2 bitmap + 96 B/sector raw P-W subcode. Includes the capability probe
  (which combos the drive honours, field ordering, C2 alignment) and, if the generic
  0xBE combo fails on Plextors, the vendor 0xD8 READ CD-DA path redumper uses.
- **F2 — Q-subchannel decode** [Python — **already shipped**]
  `src/cdda2img/subchannel.py`: CRC-16/GSM, P-W→Q extraction, ADR 1/2/3 decode
  (position / MCN / ISRC), raw full-TOC lead-out parse, aggregate scan. Residual: expose
  the INDEX byte (q[2]) on `ChannelQ`.
- **F3 — Track layout, pre-gap and index detection from the Q stream** [Python]
  Index-00 spans → per-track pre-gap lengths; index ≥ 02 → INDEX points; per-track
  CONTROL aggregation (pre-emphasis, copy, 4-channel); reconciliation against the
  lead-in TOC. The hardest feature: real Q streams are dirty.
- **F4 — MCN + ISRC extraction with whole-stream majority vote** [Python]
  Upgrade `scan_subcode`'s first-value-wins `RegionDatum` to a per-value vote across
  every clean frame. Structurally immune to cdrdao bug #75 by construction.
- **F5 — CD-Text capture + decode** [C dump + Python decode]
  c2read `--cdtext`: READ TOC/PMA/ATIP format 0x05 raw dump. New `cdtext.py`: 18-byte
  packs, CRC-16-CCITT, PTI decode, language blocks, charsets.
- **F6 — Full TOC / session structure** [C dump + Python decode]
  c2read `--fulltoc`: READ TOC format 0x02 raw dump. Python: sessions, ADR/CTRL per
  track, multisession (Enhanced CD) detection → exclude data tracks from the audio rip.
- **F7 — TOC assembly + cdrdao-format TOC text emission** [Python]
  The join point: combine F3+F4+F5+F6 into a `ParsedDisc`/`RBIDisc` and emit
  cdrdao-grammar TOC text that `toc_parser.py` round-trips identically. This is what
  actually deletes the 181 s `read-toc` pass.
- **F8 — Robust audio reads (retry + zero-fill)** [C]
  Fix the known desync bug (a failed chunk is skipped **without writing**, shortening
  the PCM file); add per-sector retry narrowing and zero-fill + all-C2-flagged marking
  for genuinely unreadable sectors.
- **F9 — Machine-parseable progress** [C]
  Stable `progress <done> <total>` lines for the TUI (subsumes the deferred
  progress-bar TODO for the C2 path).
- **F10 — Drive speed report** [C]
  Replace `cdrdao drive-info` in `drive_speed.py`: GET PERFORMANCE (0xAC) and/or MODE
  SENSE 2A with the CD-DA-correct arithmetic, validated empirically against cdrdao's
  numbers on the PX-716A.
- **F11 — C1/C2/CU disc-health census (Plextor Q-Check)** [C + Python, optional]
  Vendor-command error census (cdrtools `readcd -cxscan` protocol): per 75-sector
  interval, counts of C1 (corrected — invisible to MMC), C2, and CU errors. Archival
  disc-health record + early rot warning; Plextor-gated (NEC variant exists, unbuilt).

---

## 2. Difficulty ranking (hardest → easiest)

Effort in focused sessions (a session ≈ one evening). Hardware-empirical work (live
drive probes) is flagged, since it serialises on the one PX-716A.

| Rank | Feature | Effort | Why |
|---|---|---|---|
| 1 | F3 layout/pre-gap/index from Q | L (2–3) | Dirty-data robustness: CRC-failed frames, BCD glitches, position slips, missing index-00 spans, TOC reconciliation. Most edge cases per line of code. |
| 2 | F5 CD-Text | M–L (1–2) | Binary pack format, tab-continuation strings, multi-block languages, charsets (Latin-1 now, MS-JIS deferred). Good references exist. |
| 3 | F1 combined capture | M–L (1–2, hw) | C plumbing is easy; the cost is the per-drive empirical matrix (combo support, field order, C2 shift) and possibly a second read method (0xD8). |
| 4 | F7 TOC assembly + emission | M (1) | Pure glue but the output grammar must round-trip `toc_parser.py` exactly; needs a golden parity test vs cdrdao. |
| 5 | F8 audio hardening | M (0.5–1) | Retry ladder + zero-fill semantics; sense-code classification. |
| 6 | F10 speed report | S–M (0.5–1, hw) | 0xAC parsing is simple; the work is validating against cdrdao's CD-DA-correct numbers on a drive whose page 2A lies. |
| 7 | F11 C1/C2/CU census | S–M (0.5–1, hw) | Protocol fully documented in readcd source (three 0xEA CDBs); needs the O_RDWR open + a Python census driver + PROV wiring. |
| 8 | F6 full TOC / sessions | S (0.5) | `parse_fulltoc_leadout` already parses the response shape; extend to all points + sessions. |
| 9 | F4 MCN/ISRC majority vote | S (0.5) | Counter over already-decoded values + existing validators. |
| 10 | F9 progress lines | S (<0.5) | Format the existing counters; parse in `c2_reader.py`. |
| 11 | F2 Q decode | done (XS residual) | Ship the INDEX property; everything else exists and is validated. |

---

## 3. Dependency groups and build order

Dependency graph:

```
F1 ──► F2 ──► F3 ──► ┐
        └──► F4 ──► ├──► F7
F5 ─────────────────┤
F6 ─────────────────┘
F8 (independent; touches the same C read loop as F1)
F9 (independent)
F10 (independent)
F11 (independent; Plextor-gated, optional)
```

Groups ordered by dependency, hardest-first between chains, difficulty ranking retained
within each group:

| Group | Order | Rationale |
|---|---|---|
| G1 — Q stack | **F1 → F2Δ → F3 → F4** | Hard dependency chain; contains the overall-hardest feature (F3), so it leads. |
| G2 — CD-Text | **F5** | Independent; hardest remaining. |
| G3 — lead-in TOC | **F6** | Easy, but must precede F7. |
| G4 — assembly | **F7** | Join node; requires G1+G2+G3 complete. |
| G5 — independents | **F8 → F10 → F11 → F9** | No dependencies; internal difficulty order. |

**Linear build order: F1, F2Δ, F3, F4, F5, F6, F7, F8, F10, F11, F9.**

Two pragmatic notes (deviations allowed without re-planning):
- F8 edits the same C read loop as F1 — folding the zero-fill fix into the F1 commit
  avoids touching the loop twice; the retry ladder can still land later.
- F9 is trivial and unblocks the user-visible TUI progress item — it can be pulled
  forward to ride along with any C-side commit.

---

## 4. Reference material audit

### Present and sufficient

| Source | Location | Feeds |
|---|---|---|
| redumper `scsi/mmc.ixx` | private/code/redumper/ | F1 (READ CD byte-10 enums, buffer sizes incl. `READ_CD_SUB_SIZES`, 0xD8 `READ_CDDA_SubCode`), F5/F6 (`READ_TOC_Format::CD_TEXT`, `CD_TEXT_Descriptor`) |
| redumper `cd/cd_common.ixx` `read_sector` | 〃 | F1 — the canonical combined C2+RAW-sub READ CD, Plextor C2-shift compensation, 0xBE→0xD8 selection |
| redumper `drive/test.ixx` | 〃 | F1 — capability-probe pattern (tests every sector-order/sub combo against the live drive) |
| redumper `drive.ixx` drive DB | 〃 | F1 — **PX-716A row is CHECKED: +30 read offset, C2 shift 295, ReadMethod::D8, SectorOrder::DATA_C2_SUB** |
| redumper `cd/subcode.ixx`, `crc/crc16_gsm.ixx` | 〃 | F2 (already ported), F3 |
| redumper `cd/toc.ixx` | 〃 | F3/F6/F7 — building a TOC (incl. indices and qtoc) from a captured subcode stream; the closest existing implementation of exactly what F3 does |
| cdrdao `dao/CdrDriver.cc` | private/code/cdrdao/ | F3 — `analyzeTrackSearch`/`Scan`, `findIndex` (binary-search index detection: the *other* strategy, useful as a cross-check), `readCatalogScan`; F5 — `readCdTextPacks`/`readCdTextData` |
| cdrdao `dao/GenericMMC.cc`, `PWSubChannel96.cc` | 〃 | F1/F3 — READ SUB-CHANNEL ISRC/MCN (the per-track query alternative), P-W deinterleave |
| libmirage `mirage/cdtext-coder.c` | private/code/libmirage/ | F5 — the cleanest CD-Text pack decoder (CRC, packs → fields, blocks) |
| IEC 60908:1999 | private/research/ | F2/F3 normative backing (subcode Q structure, CONTROL bits, MSF) |
| `docs/reference/reference.toc` + libmirage `images/image-toc/` | repo / private/code/ | F7 — emission grammar ground truth (what `toc_parser.py` and cdrdao both accept) |
| `src/cdda2img/subchannel.py` + `tools/disc_scan.py` | repo | F2 shipped; F3/F4 build directly on it |
| private/testdata/c2/ (5-pass PX-716A captures) | local | F1 — alignment ground truth (δ=−30, k=−2 established for 0xBE+C2 without sub) |
| cdspeedctl source | private/code/cdspeedctl/ | F10 candidate reference (check its speed-report path during F10) |
| cdrtools `readcd/readcd.c` | private/code/schily-2024-03-21/ | F11 — complete Plextor Q-Check protocol (`plextor_init_cx_scan`/`_read_cx_values`/`_end_scan`: 0xEA subs 0x15/0x16/0x17, counter offsets) + NEC 0xF3 variant; F8 — mode page 0x01 error-recovery tuning (`domode`: TB/DCR bits, retry count) and the cache-defeating retry choreography (10 in-place retries, then seek-away reads between attempts) |

### Gaps and actions

1. **MMC (T10) specification — absent.** No local MMC-3/MMC-6 draft. redumper/cdrdao
   encode everything in source, but a from-scratch C implementation of READ CD byte-10
   semantics, READ TOC formats 2/5 response layouts, and GET PERFORMANCE deserves the
   normative text. **Action (before F1): fetch a public T10 working draft (e.g.
   mmc3r10g / mmc6r02g) into `private/research/`.**
2. **CD-Text pack-format annex.** Verify whether IEC_60908-1999.pdf includes the
   CD-Text annex (**action: check before F5**); if not, libmirage + cdrdao
   `readCdTextPacks` + the MMC format-5 layout are jointly sufficient — the CLAUDE.md
   PTI table already documents the field semantics.
3. **PX-716A empirical facts for combined reads — not documentable in advance.**
   Which 0xBE combos the drive honours, actual field order, and whether the redumper
   C2-shift 295 applies to 0xBE (our 0xBE+C2-without-sub data says k=−8 bytes, not
   295 — the 295 is presumably a D8-path artefact) must come from the F1 live probe.
   This is an experiment, not a document; the plan treats it as part of F1.

---

## 5. Per-feature strategies

### F1 — Single-pass combined capture (audio + C2 + subchannel)

**Goal:** one `c2read --full --pcm X --c2 Y --sub Z` pass yields everything the rip
needs from the program area.

**c2read changes:**
- `--sub raw|q` + `--subf FILE`: set CDB byte 10 to 001b (raw P-W, 96 B/sector) or
  010b (formatted Q, 16 B/sector); write the per-sector subcode stream to FILE.
  Sector buffer becomes 2352 + 294 + 96 = 2742 B; keep transfers < 64 KiB → chunk ≤ 23
  sectors when all three fields are on (auto-clamp, warn once).
- Extend `--features` into a combo probe (pattern: redumper `drive/test.ixx`): for each
  of {C2, sub=RAW, sub=Q, C2+RAW, C2+Q} issue a 3-sector smoke read and report
  supported/refused per combo, machine-parseable. Verdict line gains
  `combo C2+RAW ok|failed` so `c2_reader.drive_supports_c2` can gate the single-pass
  path specifically.
- **Field-order/alignment verification is Python-side policy** (c2read only reports):
  a short tools/ probe reads a sector span with known audio (or just LBA-position Q),
  decodes Q from the presumed sub slice, and checks `position_lba()` tracks the
  requested LBA — Q frames self-identify, so mis-ordered slices fail CRC or produce
  absurd positions immediately. Same trick verifies C2 alignment against the
  established k=−2.
- **Plextor 0xD8 fallback (contingency only):** if the PX-716A refuses 0xBE C2+RAW,
  implement `--d8` (READ CD-DA 0xD8, sub-code mode 8 = DATA_C2_SUB, 2742 B/sector) from
  redumper `cmd_read_cdda` — the drive DB row for the PX-716A is CHECKED with exactly
  this config, including the C2-shift-295 compensation (read one extra sector, take C2
  from the shifted position). Do not build it speculatively.

**Validation:** live probe on the PX-716A (clean disc + the damaged Tracy Chapman
disc): combo matrix, Q-position lock, C2 alignment re-check with sub enabled (the extra
transfer may shift drive behaviour — verify k is unchanged), throughput vs the current
95 s baseline.

**Sub-channel caveat to carry forward:** raw P-W subcode is *not* error-corrected
(Q has only its CRC; ~1 bad Q frame per hundreds is normal). All consumers (F3/F4)
must treat single frames as unreliable and aggregate.

### F2Δ — Q decode residual

Add `index` property to `ChannelQ` (`_bcd(self.raw[2])`, −1 on invalid nibbles) +
tests. Everything else (CRC-16/GSM, `extract_q`, MCN, ISRC, position, full-TOC
lead-out) is shipped and validated against redumper captures.

### F3 — Track layout, pre-gap + index detection from the Q stream

**Goal:** from one pass's Q stream + the format-0 TOC, produce per track: pre-gap
length, INDEX ≥ 02 points, aggregated CONTROL flags (pre-emphasis / copy / 4ch).

**Approach (redumper-style stream derivation, not cdrdao-style seek-scan):**
1. Decode every sector's Q; keep only CRC-valid ADR=1 frames (typically ≥ 99%).
2. For each track N ≥ 2: the pre-gap is the span of frames with `track == N`,
   `index == 0` immediately preceding N's TOC start. Track 1's pre-gap is not readable
   (LBA < 0) → standard 150 frames, as today.
3. INDEX ≥ 02: transitions of `index` within the track body → additional INDEX points.
4. CONTROL: majority over each track's valid frames (Q CONTROL is authoritative over
   the lead-in TOC's copy, which can disagree; record disagreement in PROV).
5. Reconcile: TOC (format 0) remains the authority for track *starts* (it is
   error-corrected; Q is not). Q supplies only what the TOC lacks: pre-gap lengths,
   index points, per-track flags. Never move a track boundary based on Q alone.

**Robustness rules (the actual hard part):**
- A pre-gap span must be *contiguous-ish*: tolerate isolated CRC-failed frames inside
  it, but require ≥ 2 clean index-00 frames to declare a pre-gap at all (a single
  glitched frame must not invent one).
- Sanity-cap pre-gaps (e.g. > 10 s → suspicious, log + clamp to observed span).
- Position slips (the track-5 lesson: the drive can return coherent wrong audio):
  frames whose `position_lba()` deviates from the expected running LBA by more than a
  couple of sectors are dropped from aggregation.
- Discs with damaged lead-ins/pre-gaps must degrade to cdrdao-equivalent defaults
  (pre-gap 0 / SILENCE for track 1), never fail the rip.

**Validation:** golden test vs cdrdao `read-toc` output on the local disc shelf —
pre-gap-by-pre-gap, flag-by-flag diff (the F7 parity harness, run early in analyse-only
mode). Unit tests from synthetic Q streams (clean, glitched, slipped, missing-pregap).

### F4 — MCN + ISRC majority vote

Replace `RegionDatum`'s first-clean-value semantics with a `Counter` per region;
winner = plurality among validator-passing values (`validators.validate_isrc`,
`barcode`-style check digit for MCN), with a minimum-observations floor (≥ 2) before
trusting a value at all. Report runner-up counts (PROV disagreement surface). The
whole-stream vote sees every ~100th frame of every track — dozens of samples per
track — which is the Boyer-Moore fix for bug #75 applied at the capture layer where it
belongs.

### F5 — CD-Text capture + decode

**c2read:** `--cdtext FILE`: READ TOC/PMA/ATIP (0x43) format 0x05, two-step allocation
(read 4-byte header first, re-issue with the full length — responses can exceed 64 KiB
in theory; cap at 4 KiB × blocks actually present), dump the raw response verbatim.

**Python `cdtext.py`:**
1. Split into 18-byte packs; verify CRC-16 (poly x¹⁶+x¹²+x⁵+1, init 0xFFFF, output
   inverted — already documented in CLAUDE.md); drop bad packs (log count).
2. Group by block (language) — block 0 first; decode the SIZE_INFO (0x8F) packs for
   charset + language codes.
3. Reassemble PTI strings across pack boundaries (NUL-terminated, tab = "same as
   previous track" shorthand); map PTI 0x80/0x81 → album/track title/performer,
   0x86 → `cdtext_catalog_ref`, 0x8E → MCN/ISRC cross-check only (Q is authoritative).
4. Charset: ISO-8859-1 now; MS-JIS deferred (log + skip non-Latin blocks; none in the
   local collection).

**References:** libmirage `cdtext-coder.c` (primary), cdrdao `readCdTextPacks`
(response-layout ground truth: some drives return packs with, some without, the CRC
bytes — handle both by detecting response stride 18 vs 16).

**Validation:** discs with known CD-Text from the shelf; parity vs cdrdao's TOC
`CD_TEXT` blocks via the F7 harness. Unit tests from captured raw dumps in
tests/fixtures/.

### F6 — Full TOC / session structure

**c2read:** `--fulltoc FILE`: READ TOC format 0x02 raw dump (11-byte descriptors —
`parse_fulltoc_leadout` already parses this exact shape from redumper captures).

**Python:** extend to a full parse: per-session A0 (first track + disc type), A1 (last
track), A2 (lead-out), numbered track points with ADR/CTRL. Multisession policy:
audio rip covers session 1 only; a data track in a later session (Enhanced CD) is
reported and excluded; `disc_last_lsn` = session-1 lead-out. A data track *inside*
session 1 (mixed mode) is out of scope for CD-DA archival → refuse with a clear error
(current behaviour is cdrdao-dependent and undefined; make it explicit).

### F7 — TOC assembly + cdrdao-format TOC emission

**Goal:** a `subq_toc.py` (name TBD) that takes {format-0 TOC, full TOC, Q-stream scan
(F3/F4), CD-Text (F5)} and returns the same `RipInfo`-feeding structures the cdrdao
path produces, plus cdrdao-grammar TOC text.

**Approach:**
- Build `ParsedDisc`/`ParsedTrack` directly (not by string round-trip): track starts
  from TOC, pre-gaps/indices/flags from F3, MCN/ISRC from F4, CD-Text fields from F5.
- Emit TOC text via the *existing* `toc.py:generate_toc` path where possible — the
  container's embedded TOC is already generated (not cdrdao-copied) for create/import,
  so the rip path converging on the same emitter removes a format variant rather than
  adding one. Gap analysis first: what does cdrdao's read-toc TOC carry that
  `generate_toc` doesn't (INDEX lines, NO COPY variants, per-track CD-TEXT blocks)?
  Extend `generate_toc` + `toc_parser.py` symmetrically where needed (spec-before-code:
  `rbi_spec.md` TOC-block section if anything new is embedded).
- **Parity harness (the acceptance gate):** `tools/toc_parity.py` — run cdrdao
  `read-toc` and the c2read pipeline on the same disc, parse both with `toc_parser.py`,
  diff field-by-field. Green across the disc shelf (incl. the pre-emphasis disc and
  CD-Text discs) = permission to flip the default.
- **Pipeline integration:** `_rip_disc_stage`'s C2 path drops `read_toc_metadata`
  (single read, ~95 s total); the normal path gains a `ripper=c2read` alternative
  behind config until parity is proven. `c2_recovery=auto` becomes defensible as the
  default once the second pass is gone (record decision then, not now).

### F8 — Robust audio reads (retry + zero-fill)

- **Bug fix (do first, it corrupts current output):** a failed chunk currently skips
  `lba += n` without writing → PCM/C2 files lose n×2352/n×294 bytes and every later
  sample lands at the wrong offset. Fix: always write *something* per sector.
- Retry ladder on chunk failure: halve the chunk down to single sectors; retry each
  failed sector k times (default 2); on final failure write 2352 zero bytes + C2
  bitmap all-ones (a self-consistent downstream signal: "everything here is an
  erasure" — ctanalyse then treats it exactly right) and count it in `read_errors` +
  a `hard <lba> <count>` stderr line.
- **Cache-defeat between retries** (readcd pattern, same intent as redumper's
  flush-read): re-reading the same LBA back-to-back often just returns the drive
  cache. Between retries, issue a throwaway 1-sector read elsewhere (readcd
  alternates first/last/random sector after its first 10 in-place attempts). Cheap,
  and — unlike eject/CDROMRESET, which we measured as useless — unmeasured by us, so
  worth carrying into the ladder and evaluating on the damaged disc.
- **Mode page 0x01 (read error recovery) tuning — experiment** (readcd `domode`):
  byte 2 error-recovery bits (TB = transfer block on failure; readcd uses 0x20, or
  0x21 with DCR for uncorrected clone reads) + byte 3 retry count. A low drive-side
  retry count with TB set could make the drive return best-effort data + C2 flags
  quickly instead of grinding through internal retries (today a defective sector can
  stall a chunk for seconds and then return *nothing*). Needs MODE SELECT — a
  write-class command → O_RDWR open (see F11 note). Restore the saved page on exit.
  Strictly an experiment: page-01 semantics for CD-DA differ per drive (CIRC always
  runs; the page governs retry/reporting behaviour), so adopt only what the damaged
  disc validates.
- Classify sense keys in the summary (3/xx medium vs 4/xx hardware vs unit-attention)
  — report only; policy stays in Python.

### F9 — Machine-parseable progress

Emit `progress <sectors_done> <sectors_total>\n` on **stdout** (stderr keeps the human
line), unbuffered, at most ~4 Hz. `c2_reader.read_disc_c2` grows a callback that feeds
the existing `RipUI` cumulative-counter progress (per the timing-independent-UI
feedback: counters, not dwell). This closes the deferred TUI throbber item for c2read;
`read-toc`'s throbber disappears with F7 rather than being instrumented.

### F10 — Drive speed report

`--speed-report`: print `speed current_kbps <n> max_kbps <m>` from, in order of
preference: GET PERFORMANCE (0xAC, performance-data type 0) if the drive supports it;
else MODE SENSE 10 page 2A with the CD-DA arithmetic cdrdao's `drive-info` applies
(read cdrdao's `GenericMMC::driveInfo` for the exact correction — page 2A raw fields
report current > max on the PX-716A). Check `cdspeedctl` (private/code/) for an
existing 0xAC implementation to crib. Acceptance: matches `cdrdao drive-info` output on
the PX-716A across the speed ladder (set 4X/12X/40X via the existing ioctl, read back).
Then `drive_speed.py:read_drive_speed` swaps its subprocess from cdrdao to c2read.

### F11 — C1/C2/CU disc-health census (Plextor Q-Check, optional)

**Goal:** an archival error census — per-second (75-sector) counts of C1 (corrected
at the first CIRC stage), C2 (corrected at the second), and CU (uncorrectable) —
recorded at rip time. C1 is the early-warning signal MMC cannot expose: a disc whose
C1 rate is climbing is degrading long before C2/CU appear, which is exactly what a
*catalogue* wants to know across re-rips years apart.

**Protocol (from cdrtools readcd, complete in source):** gate on INQUIRY vendor
`PLEXTOR`; `0xEA` sub 0x15 (init scan) → loop { read 75 sectors normally, `0xEA` sub
0x16 → 26-byte counter block: C1 = E11+E21+E31 (u16 at offsets 16/14/12), CU (u16 at
20), C2 (u16 at 22) } → `0xEA` sub 0x17 (end scan). A NEC variant (0xF3) exists in
readcd; not built until a NEC drive shows up.

**c2read:** `--cxscan` flag: runs the census over `[start, lead-out)`, one
machine-parseable line per interval (`cx <lba> <c1> <c2> <cu>`) + a summary.
**Requires O_RDWR open**: 0xEA is a vendor opcode, which the kernel's unprivileged
SG_IO filter blocks on read-only fds; a write-open (cdrom group has rw on /dev/srN)
lifts the filter without root. This same door is what F8's MODE SELECT needs — do the
O_RDWR switch once, for both.

**Python:** a `tools/` census driver first (plot/aggregate, PROV wiring decision
later). **Not on the rip's critical path**: it costs a full extra pass, so it is an
opt-in scan (config/flag), not a default rip stage. Positions are per-interval only —
this *complements* the C2 pointer bitmap (byte-accurate, free during the rip); it
never replaces it.

**Caveats:** counters are read-to-read non-deterministic (as our C2 experiment
showed) — treat magnitudes, not exact values, as the signal; speed-dependent (record
the scan speed alongside the counts).

---

## 6. End state and payoff

- **C2 path:** one 95 s read captures audio + C2 + subchannel + (lead-in) CD-Text —
  the 181 s `read-toc` pass is gone, making `c2_recovery=auto` a sensible default and
  every rip erasure-capable at zero extra cost.
- **Normal path:** c2read can replace `cdrdao read-cd` outright (same single pass with
  C2 off if unsupported), pending the F7 parity gate.
- **cdrdao's remaining role:** burning (`burn`) only.
- **Bug-#75 class eliminated structurally:** ISRC/MCN come from a whole-stream majority
  vote instead of any single latched read.
- **`drive_speed.py`** loses its cdrdao subprocess dependency (F10).

Sequencing recap: **F1 → F2Δ → F3 → F4 → F5 → F6 → F7 → F8 → F10 → F11 → F9**, with
F8's zero-fill bug fix folded into F1's read-loop work, and F9 free to ride along
early. Prerequisite actions: fetch a T10 MMC draft; verify the IEC 60908 CD-Text
annex; run the F1 combo probe on the PX-716A before committing to 0xBE-vs-0xD8.

---

## 7. Appendix: cdrtools (readcd) review — 2026-07-04

`private/code/schily-2024-03-21/readcd/` + `libscg/` were reviewed for strategies the
plan had missed. Outcome — two adoptions, three dismissals:

**Adopted:**
- **Plextor Q-Check C1/C2/CU census** (`-cxscan`) → new feature F11. The only item
  that surfaces C1 (corrected-error) rates, which no MMC command exposes.
- **Drive-side retry tuning + cache-defeat retry choreography** → folded into F8.
  Mode page 0x01 (TB bit, retry count via `domode`) plus readcd's seek-away reads
  between retry attempts.

**Dismissed:**
- **`-edc-corr` (Heiko Eissfeldt host-side ECC/EDC loop):** corrects the L-EC layer
  of 2048-byte *data* sectors read in uncorrected audio mode. CD-DA has no L-EC layer
  — the only code above the (drive-internal, host-inaccessible) CIRC is CTDB parity,
  which we already decode. Conceptually it validates the ctanalyse approach (the host
  out-looping drive firmware); practically it is inapplicable to audio.
- **`-c2scan`:** MMC READ CD with the C2 field — c2read already is this, with
  byte-accurate bitmaps rather than counts.
- **libscg:** a cross-platform SCSI transport abstraction; our unprivileged Linux
  SG_IO path already works and portability is not a goal for c2read.

One environmental note from the review: vendor opcodes (0xEA/0xF3) and MODE SELECT
require the device fd to be opened O_RDWR to pass the kernel's unprivileged SG_IO
command filter — still root-free for cdrom-group users, but a deliberate switch from
c2read's current O_RDONLY-everywhere stance (make it per-flag, not global).
