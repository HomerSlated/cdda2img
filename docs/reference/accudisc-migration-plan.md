# AccuDisc Migration Plan — retire cdrdao + cd-paranoia for all disc activity

**Status: LIVING DOCUMENT.** Started 2026-07-17; reconciled the same day against AccuDisc's
0.1.0 interface reply (`private/AccuDisc.md`). Supersedes `c2read-upgrade-plan.md` for
read-engine strategy — c2read was the prototype; **AccuDisc** (separate external project,
https://github.com/HomerSlated/accudisc, git-ignored snapshot in `tools/accudisc/`) is the
production engine that replaces it.

Goal: **every live-drive operation goes through AccuDisc.** cdrdao and cd-paranoia are
removed as *drive engines*. The cdrdao *TOC text format* stays — it is the RBI's native
TOC representation and a foreign-image import source; retiring the binary is not retiring
the format.

---

## 1. Strategy inversion (the reason this plan exists)

The old two-pass model was **fast read, then re-read on failure**:

> Pass 1: rip PCM at max speed. Pass 2: on AR mismatch, re-read failed spans (speed ladder).

That inverts, because **Q-channel (subchannel) accuracy is speed-sensitive in a way PCM
is not.** The subchannel has no C1/C2/CU error correction — those layers belong exclusively
to the main audio channel. So at high CAV rim speed the audio still verifies clean (AR 19/19,
0 CU) while the Q stream silently collapses (measured 98.2% @ 24× → 47.4% @ 32× on the
PX-716A), and short pre-gaps drop below the "≥2 clean frames to declare" floor and **vanish
from the TOC with no error raised**. A full-disc archival tool cannot lose pre-gaps; a
track-ripper doesn't care, which is why conventional tools never surfaced this.

New model — **one careful pass, then targeted recovery on failure**:

> Pass 1: **one Q-optimal-speed** full read — audio + C2 bitmap + raw P-W subchannel +
> lead-in (fulltoc + CD-Text) in a single spin-up. Accurate, not maximal.
> Pass 2: **only on failure**, spanned re-reads with recovery strategies (PCM: AR-verified
> offset splice; Q: multi-pass per-sector majority vote + position-model interpolation).
> Every recovery re-read already carries Q for free, so PCM and Q recovery share one sweep.

**Consequence for pass 1 speed:** the hope that pass 1 could be max-speed is dead. Pass 1
must be **capped** to the Q-optimal speed (≈24× on the PX-716A), not restored to max. The
cap mechanism is **confirmed** (SET STREAMING 0xB6, including an LBA-ranged ceiling — see
§4 and `reference_set_streaming_cdb_layout` in memory); what's now settled is *who applies
it* (D1) and *at what privilege* (D3). There is no AccurateRip / CTDB equivalent for
subchannel data; blind re-reads + the position model are the only recovery levers, and the
`subq_ok/subq_total` counter (now shipped) is what tells us a pass needs them.

---

## 2. Scope — migrate vs. keep vs. already done

### 2a. MIGRATE — live-drive binary call sites (the actual work)

Grounded in a full `rg` sweep of `src/` (2026-07-17):

| # | Call site | Today | Target |
|---|-----------|-------|--------|
| M1 | `cdrdao_ripper.rip_cdrdao()` — `cdrdao read-cd` | primary rip (normal path) | AccuDisc `read --sub raw` (capped speed), single pass |
| M2 | `cdrdao_ripper.read_toc_metadata()` — `cdrdao read-toc` | metadata 2nd pass / C2-path fallback | drop — `subq_toc.build_rip_info` from the M1 pass |
| M3 | `cdda2img.py:_fast_toc()` — `cdrdao read-toc --fast-toc` (banner) | pre-rip disc geometry for the banner | AccuDisc `fulltoc` (✅ confirmed lead-in only, no full spin) → `parse_fulltoc` |
| M4 | `disc_reader.rip_disc()` + `-Q` query — `cd-paranoia` | full-disc read fallback when cdrdao fails | see O1 (emergency fallback fate) — `features` C2_UNVERIFIED now gives the clean "can't drive" signal |
| M5 | `track_preview.py` — `cd-paranoia -Z 1` | cosmetic track-1 preview during rip | AccuDisc `read_span` of track 1 |
| M6 | `drive_speed._read_speed_accudisc` (**`speed-report` — REMOVED in 0.1.0**) + `_read_speed_cdrdao()` (`cdrdao drive-info`) | speed read; accudisc primary + cdrdao fallback | port the primary to the new `speed` subcommand (breaking — see §4) **and** drop the cdrdao fallback |
| M7 | `write_offset.py` — `cdrdao write` + `cdrdao read-cd` | burn-and-read-back write-offset measurement (`setup --write-offset`) | AccuDisc `write` + `read` |
| M8 | `rip_log.py` — `cdrdao version` / `cd-paranoia --version` | engine-version stamp in the rip log | `accudisc --version` → `accudisc 0.1.0` (✅ shipped) |

### 2b. ALREADY DONE

- **Burn** — `disc_writer.py:_run_accudisc_write()` (`accudisc write --toc --bin`,
  `--simulate` for laser-off). Header says "cdrdao no longer plays any role" (ffb45c3).
  The **C2 rip path** (`accudisc_reader.read_disc_c2` / `read_span`) already exists and is
  the foundation M1 builds on.

### 2c. KEEP — cdrdao *format*, not the *binary* (explicitly NOT migration targets)

- `toc.py` / `toc_parser.py` — generate/parse cdrdao-format TOC **text**; this is the RBI's
  embedded TOC representation and cdemu's load format. Unaffected by removing the binary.
- `cdrdao_reader.py` — **imports** foreign cdrdao TOC+BIN disc images (a file format the
  `import` subcommand accepts; the binary is never invoked) + the s16be→s16le byte-swap.
- `cdemu` mount path (`cdemu.py:mount_rbi`) — cdemu consumes a scratch `.toc`; no
  cdrdao/cd-paranoia binary involved.
- Comment/docstring references to cdrdao behaviour in `subchannel.py`, `subq_toc.py`,
  `cdtext.py`, `barcode.py`, `container.py`, `ddp_reader.py` — documentation, not calls.

### 2d. TOOLS (`tools/`, tracked, lower priority — opportunistic)

- `tools/toc_parity.py:41` — still drives **c2read** (`c2read --full`); the acceptance gate
  must diff **AccuDisc** vs `cdrdao read-toc` to stay meaningful. High value: this is the
  parity gate that authorises preferring the single pass.
- `tools/measure_write_offset.py` — pairs with M7 (`cdrdao write` + `read-cd`).
- Diagnostic/experiment scripts still on c2read or cd-paranoia (`cx_census.py`,
  `c2read_recovery_test.py`, `c2timing.py`, `modepage_experiment.py`,
  `paranoia_recovery_test.py`, `compare_discid.py`, `replay_paranoia_progress.py`,
  `disc_scan.py`, `trace_*`). Reference/retired-baseline scripts; migrate as touched, keep
  the paranoia ones as historical comparison baselines.

---

## 3. Modules that become dead once M1–M8 land (delete last, after soak)

`cdrdao_ripper.py`, `cdrdao_progress.py`, `cdrdao_write_progress.py`, `disc_reader.py`,
`c2_reader.py` (the retired c2read wrapper — AccuDisc replaced it). Removal is the final
phase, gated on the AccuDisc path proving out across the disc shelf. Until then they remain
as fallback/reference so a regression has somewhere to fall back to.

---

## 4. What cdda2img consumes from the AccuDisc agent (the contract)

The frozen subprocess contract lives in the AccuDisc repo's `docs/cli-machine-interface.md`;
`accudisc_reader.py` absorbs flag/subcommand deltas. Status reconciled against AccuDisc's
2026-07-17 reply (`private/AccuDisc.md`, engine **v0.1.0**).

**Shipped — consume now:**

1. **Q-CRC health counters** — the `read --progress-fd N` `summary` token carries
   `subq_total=<n> subq_ok=<n> subq_bad=<n>` (CRC-16/X.25). This was the #1 gating item: a
   degraded Q capture is now one field, not an inference from missing pre-gaps, and it is the
   **pass-2 trigger** — a low `subq_ok/subq_total` ratio (≈0.47 @ 32×, ≈0.98 @ 24×) is the
   "failure" that sends us back for Q recovery. (AccuDisc named them `subq_ok`/`subq_bad`,
   not the proposed `subq_crc_*`; parse their names.)
2. **Speed CAP mechanism** — SET STREAMING (0xB6) confirmed on the PX-716A, including an
   **LBA-ranged** ceiling (`speed 24 --start L --count N`, page 2A honours it) — exactly what
   the pass-1-capped / pass-2-scoped model needs. *Ownership settled: D1.* The ranged 0xB6
   path needs `CAP_SYS_RAWIO` (D3/O5); report-only `speed` does not.
3. **Cheap disc-geometry query** — `fulltoc` is lead-in only, no full spin: safe as the M3
   pre-rip banner.
4. **Version string** — `accudisc --version` → `accudisc 0.1.0` (M8).
5. **"Cannot drive this drive" verdict** — `features --c2`: `C2_SUPPORTED` (exit 0),
   `C2_UNSUPPORTED` (exit 1, genuine no-C2), `C2_UNVERIFIED` (exit 1, couldn't test — empty
   tray or works-but-unadvertised). This is the clean signal O1's transition fallback needs.
6. **Per-sector status map** — `read --map-file PATH` (`MAP_SHARED`, one `ACCUDISC_MAP_*`
   byte/sector, no header, exactly `count` bytes). Feeds the TUI disc map *and* is the return
   channel for reconstructed-Q confidence. Consume when the TUI disc map lands.
7. **C2/audio alignment probe** — `c2lag` (oracle-free; `pairs=2` on PX-716A matches our
   k=−2). Report-only; re-probe alignment without the AR oracle.
8. **ATIP media identification** — `media` (`atip leadin=… type=CD-R|CD-RW …`); feeds the
   disc-kind guard.

**Pending — direction agreed, not yet shippable:**

- **Q-channel recovery** — multi-pass per-sector consensus + position-model interpolation,
  AccuDisc-owned; hands back reconstructed Q + per-sector confidence via the map. cdda2img
  keeps the gate (internal position-model consistency — no AR/CTDB equivalent for
  subchannel). Reference: redumper `cd/subcode.ixx`. Lower priority than the cap.
- **Disc-kind guard** (BLANK/AUDIO/NEITHER) — AccuDisc is building it and wants our
  token/exit shape before it ships. *Shape settled: D2.*
- **Q-yield-per-speed characterisation** — extends `speeds`; feeds the per-drive cap override
  (D1). AccuDisc will re-run `speeds` on the 0xB6 path with `measured=` throughput — that run
  is also the empirical test behind D3.

**Speed model — settled by AccuDisc's 2026-07-18b reply (§15.0–15.2).** Four facts now fixed,
all bearing on `drive_speed.py` (M6):

1. **`speed-report` is gone; `speed` replaces it.** `_read_speed_accudisc` regexes the *old*
   `speed max_kbps N current_kbps M` line, so bumping the snapshot silently breaks it (falls
   through to the cdrdao fallback). This is the first fix, ahead of the rest of the migration.
2. **`page2a` ≠ `measured`, and both are truthful.** `page2a` = the accepted ceiling *after the
   drive quantizes the request* (req 16 → accepted 8 on the PX-716A); `measured` = actual
   throughput (a whole-disc CAV average, below the outer-rim ceiling). **`measured` is the
   ladder ground truth** — never page-2A `current` (that's the ceiling, the wrong quantity).
   The old "page 2A lies" framing was wrong (`accudisc device.c:217`).
3. **Retire `probe_speed_ladder` + `_SPEED_PROBE`.** The authoritative ladder is `accudisc
   speeds` (warm-up + timed 1 s read per rung → `measured`), default set `{40,32,24,16,8,4}`,
   **probed per disc** — the governor sets the reachable top from the disc's load-time media
   scan (ABBA self-throttles to 32×; ZZ Top/Tracy hold 40×), so a per-drive table is wrong.
   A self-throttled ceiling read at load is free "disc-wide marginal" triage.
4. **The restore model inverts (the `subq_speed_cliff` root fix).** The ceiling is **drive
   state that persists across handles/processes** — closing the device does *not* reset it. So:
   (a) do **not** restore between ops; (b) **every AccuDisc read must set its own `--speed`
   explicitly** — an inherited ceiling from a prior op would silently clamp the next; (c) one
   courtesy restore-to-max at session end (which only reaches the *governor* ceiling, never the
   physical max — that's fine). `restore_drive_speed`'s mid-pipeline blast-to-max — the actual
   regression — is deleted, not just re-shaped. See D1 (restore resolution).

Offset correction stays entirely in cdda2img: AccuDisc returns **raw** PCM; `apply_offset`
runs exactly once, at storage. Unchanged by this migration.

---

## 5. Phased execution (each phase: own commit, `make check`, full pytest, Py3.10, live rip)

- **Phase 0 — mostly unblocked (2026-07-17).** The #1 gating item (Q-CRC counters), the
  speed-cap mechanism, the geometry query, the version string, and the cannot-drive verdict
  all shipped in AccuDisc 0.1.0. Still pending upstream: Q recovery (direction agreed) and
  the disc-kind guard (shape settled, not built). **Immediate, migration-independent:** fix
  `drive_speed._read_speed_accudisc` for the `speed-report`→`speed` rename (§4) — due when
  the snapshot bumps to 0.1.0, regardless of the rest.
- **Phase A — capped single-pass rip (M1 + M2 + M3):** restructure `_rip_disc_stage` so the
  AccuDisc capped-speed pass is the rip, `subq_toc` supplies metadata, and the banner reads
  `fulltoc`. Retire `cdrdao read-cd`/`read-toc` from the rip path. Cap policy per D1: let
  AccuDisc auto-cap on `--sub raw`, pass `--speed N` only for a per-drive override; the C2
  path stops calling `restore_drive_speed` until the rip is done (closes the
  subq_speed_cliff regression). Assert the cap took via `subq_ok/subq_total`. Update
  `toc_parity.py` to AccuDisc **first** (gate must be green before the flip).
- **Phase B — single-purpose call sites (M5 + M6 + M8):** track preview → `read_span`; speed
  reader → `speed` subcommand + drop the cdrdao fallback; version stamp → AccuDisc.
- **Phase C — write-offset tool (M7 + `measure_write_offset.py`):** `cdrdao write`+`read-cd`
  → AccuDisc `write`+`read`.
- **Phase D — fallback decision (M4, O1):** resolve cd-paranoia's fate; demote to optional
  emergency read (gated on the `C2_UNVERIFIED` verdict) or remove `disc_reader.py`.
- **Phase E — dead-module removal + docs:** delete §3 modules once soaked; update CLAUDE.md
  rip-pipeline §1, the man page, `conf/cdda2img.toml.example`, and close TODO items.

---

## 6. Decisions

### Settled 2026-07-17 (this reconciliation)

- **D1 — speed-cap ownership (was O3).** **AccuDisc auto-caps to a Q-safe ceiling when
  `--sub raw` is requested; cdda2img overrides per-drive via `[[drives]]`.** AccuDisc owns
  the characterisation (only it can measure Q-yield-per-speed) and the 0xB6 mechanism that
  actually governs the 0xBE streaming path — cdda2img's privilege-free `CDROM_SELECT_SPEED`
  ioctl (0xBB) may cap READ(10) but not the DAE read (the original trap), so ownership
  follows efficacy. Runtime guard: the `subq_ok/subq_total` ratio (§4, shipped item 1)
  validates the cap took *whatever the mechanism*; a low ratio warns + triggers pass 2.
  cdda2img still applies its ioctl pre-cap as belt-and-braces (same ceiling, harmless).
  **Restore resolution (AccuDisc §15.1, tested):** the ceiling persists across handles, so
  there is no per-op restore — set each op's `--speed` explicitly and do one restore-to-max at
  session end. `restore_drive_speed`'s mid-pipeline blast-to-max is removed (it was the
  `subq_speed_cliff` cause); the canonical restore is *save-current-then-set-that-value-back*,
  and RDD-restore is rejected by the PX-716A so there is no "restore defaults" verb.
- **D2 — disc-kind guard shape. LOCKED both sides 2026-07-18 (AccuDisc §18d).** Token-primary,
  exit-secondary. Subcommand **`disc`** (`accudisc disc`; subcommand == first output token).
  Final line:
  `disc kind=<BLANK|AUDIO|NEITHER> profile=0x<nn> disc_status=<0|1|2> erasable=<0|1>
  audio_tracks=<n> data_tracks=<n> reason=<slug>`. Exit **0 = actionable** (BLANK/AUDIO),
  **3 = classified-but-not-actionable** (NEITHER — reuses "completed-with-caveats"),
  **2 = couldn't classify**. Classification precedence is **AUDIO-first** — ≥1 audio track →
  AUDIO (so a burned audio CD-R rips, not "blank"); else CD-R/RW profile + `disc_status 0` →
  BLANK; else NEITHER. Mixed-mode → `kind=AUDIO` with `data_tracks>0` surfaced (our session-1
  policy skips the data track; the count logs why). `erasable` distinguishes reusable BLANK
  CD-RW from one-shot CD-R for the burn path. `reason=` on **every** line (parser stability):
  `audio`/`blank` on the actionable branch, else a NEITHER slug (`data_cd`, `closed_data`,
  `appendable`, `no_medium`, `not_cd_profile`, `unreadable`). cdda2img composes it: never branch
  on the exit alone — read `kind=`, require BLANK before burn / AUDIO before rip, NEITHER →
  refusal quoting `reason`. Execution deferred (O1-adjacent); interface settled.
- **D3 — CAP_SYS_RAWIO deployment (new, O5).** **Default to no elevated privilege; adopt
  `setcap cap_sys_rawio+ep tools/accudisc/accudisc` only if AccuDisc's measured 0xBE
  throughput shows the privilege-free path doesn't throttle the streaming read.** The ranged
  0xB6 cap needs the capability; the whole-disc ioctl doesn't, but its efficacy on 0xBE is
  the open empirical question (AccuDisc re-runs `speeds` with `measured=` on the 0xB6 path).
  Until that measurement says otherwise, don't mandate the capability. The `subq_ok` counter
  catches a cap that silently didn't apply, so a mis-provisioned capability degrades to
  warn+pass-2, never a silent bad archive. Scoped file-capability on one binary — not root.

### Still open

- **O1 — cd-paranoia's fate.** The stated goal is zero cd-paranoia, but it is the only
  current path for a drive AccuDisc can't drive. Options: (a) remove entirely once AccuDisc
  proves out across the shelf; (b) keep a minimal emergency `-Z` read behind a flag during a
  transition window. Recommend (b) then (a). The `C2_UNVERIFIED` verdict (§4 item 5) makes
  the "unsupported drive → fall back" branch mechanically clean. **Decide before Phase D.**
- **O2 — `c2_recovery` config surface. RESOLVED 2026-07-18 → §8.7.** Collapses to a single
  `recovery_profile = <name>` entry; ffmpeg-style selection (no profile → bare flags); C2 toggle
  lives inside the profile (Axis F), no separate `c2_recovery` key; legacy-key loader shim maps
  `off`→`fast`, `auto`/`on`→`archive` with a deprecation warning.
- **O4 — pass-1 speed vs. runtime.** A capped first pass is slower than today's max-speed
  rip. Quantify the wall-clock cost on a clean disc so the accuracy-vs-time trade is explicit.

---

## 7. Invariants / non-goals

- **AccuDisc stays external** — never shipped from this repo; git-ignored snapshot only.
- **RBI format unchanged** — no format bump for the engine swap.
- **cdrdao TOC text format retained** — RBI-embedded TOC + cdemu load + foreign-image import.
- **Foreign-image import parsers untouched** — cdrdao TOC+BIN, DDP, NRG, CCD are file formats.
- **Offset domain unchanged** — AccuDisc returns raw; single `apply_offset` at storage.
- **No mandatory root** — the privilege-free path is the default; any elevated capability
  (D3) is scoped, opt-in, and gated on a measurement that proves it necessary.
- **Prefer no data over wrong data** — a Q frame that can't be recovered is dropped, never
  guessed; a pre-gap that survives recovery is declared, one that doesn't is absent (and, per
  §1, that absence should now be *rare and real*, not a silent speed artefact).

---

## 8. Recovery profiles, the sub / no-sub option, and the combined bench

Added 2026-07-18 from Keith's four directives + AccuDisc's §15 bench-handshake reply. This
section is the design the bench proves out; nothing here is committed to code yet.

### 8.1 The sub / no-sub option — full PROV vs. reduced PROV (Directive 1)

One user choice sets both the capture flags *and* the speed policy, because they're the same
lever: `--sub raw` needs the Q-safe cap, no-sub has no reason to cap and runs at max.

- **Full PROV (`--sub`, capped):** audio + C2 + raw P-W sub + lead-in. Captures MCN, per-track
  ISRC, and precise inter-track pre-gaps / INDEX ≥02. The Q-safe cap applies.
- **Reduced PROV (no-sub, uncapped, fast):** audio + C2 + lead-in only. **Keeps** everything
  identification and verification need — PCM, C2, TOC track offsets (`fulltoc`), **CD-Text**
  (lead-in R-W via `cdtext`, *not* program-area Q, so it survives), the track-1 program pre-gap
  (TOC-derived since the ABBA fix), AR/CTDB, MB/CDDB Disc-ID. **Loses only** program-area Q:
  MCN (already archival-only, synthesised from barcode as `mcn_source=barcode_derived`),
  per-track ISRC (often back-fillable from MB), and precise inter-track pre-gap/index geometry
  (falls back to `subq_toc`'s TOC-only geometry). PCM recovery (C2-gated AR/CTDB splice + CTDB
  parity) is retained on this path.

The loss is precisely the fields already deemed archival-only, and *zero* identification or
verification accuracy — so the fast path is a legitimate user option, not a degraded rip.
Config default = full PROV (`archive`); CLI/config override to fast.

### 8.2 The recovery method-space (Directive 3a)

Every valid method is independently invocable, composable in groups, and bundled into named
profiles. Five orthogonal axes:

| Axis | Values | Owner |
|---|---|---|
| A. Subchannel | sub / no-sub | cdda2img (flag) |
| B. Speed | Q-safe cap / max / explicit Nx (per-disc `speeds` ladder, `measured`) | AccuDisc caps on `--sub raw` (D1); cdda2img overrides |
| C. Audio recovery | none / targeted re-read rungs R0–R4 / CTDB parity rebuild / both | AccuDisc rungs; cdda2img `ctdb_repair` |
| D. Q handling | none / discard-&-retry-at-safe-speed / **our cross-pass consensus** (open, bench-gated) | shared |
| E. Verify gate | informational / require-green-before-accept | cdda2img (AR/CTDB) |
| F. C2 pointers | on / off — **some drives have no C2 at all**, so a non-C2 path must stay whole | AccuDisc (`--c2f`); cdda2img toggle |

Axis F is load-bearing: "every valid recovery method invocable" makes C2 a toggle, and a
C2-incapable drive (`features --c2` → `C2_UNSUPPORTED`/`C2_UNVERIFIED`) must still rip via a
non-C2 profile, not fail. Only `--c2-retries` is intrinsically C2-dependent; `retries` /
`verify` / `overlap` / `ladder` must function C2-less (confirmation requested of AccuDisc, §17.3).

AccuDisc's R0–R4 (a strict-superset escalation; **set-speed is a separate axis, swept
orthogonally** — §15.3):

| rung | `--retries` | `--c2-retries` | `--verify` | `--overlap` | `--ladder` |
|------|:-:|:-:|:-:|:-:|:-:|
| R0 | 2 | — | — | — | — |
| R1 | 2 | 3 | — | — | — |
| R2 | 2 | 3 | 2 | — | — |
| R3 | 2 | 3 | 2 | 4 | — |
| R4 | 2 | 3 | 2 | 4 | 8,4 |

`--ladder` is the *in-rung* speed-diversity knob; when sweeping `--speed` externally with R4,
the ladder list must sit at/below the pass speed.

### 8.3 Named profiles (curated axis-points; docs explain each in full)

Profiles are named bundles, **not** silently applied — see §8.7 (selection is ffmpeg-style: no
profile requested → bare flags, no profile). `archive` is the *recommended / derived-optimal*
bundle, invocable by name, and the one the bench ranking (§8.5) should reproduce — but it is not
auto-applied absent an explicit request.

- **`archive`** — A=sub, B=Q-safe cap, C=R-rungs+CTDB parity, D=discard-&-retry
  (`subq_ok/subq_total < ~0.90` → discard the pass), E=informational, F=C2 on. The
  recommended default bundle; §8.5's ranking should land here (most complete + verified, fastest
  that keeps it).
- **`compat`** — `archive` minus C2 (A=sub, B=Q-safe cap, C=R-rungs without `--c2-retries` +
  CTDB parity, D=discard-&-retry, E=informational, **F=C2 off**). The mandatory non-C2 profile
  for drives that report `C2_UNSUPPORTED`/`C2_UNVERIFIED`. AccuDisc verified (§18d) that every
  recovery *mechanism* survives C2-less — only `--c2-retries` is a no-op — but the **detection
  surface narrows**: without C2's per-sector erasure locator, re-reads fire only on hard errors,
  `--verify` PCM disagreement, and `--overlap` seam mismatch, and the CTDB erasure-assisted
  repair degrades to error-only. "Recovery whole, detection narrower" — documented, not oversold.
- **`fast`** — A=no-sub, B=max, C=CTDB+splice available, D=none, E=informational, F=C2 on. §8.1's path.
- **`paranoid`** — A=sub, B=Q-safe cap, C=both maxed, D=aggressive, E=require-green, F=C2 on. Slowest.
- **`blind`** — A=no-sub, B=max, C=CTDB-rebuild-only-if-AR-fails, D=none, F=C2 off. Keith's
  speculative fastest. **Never the default** — CTDB parity only covers discs already in the
  CueTools DB, so a first-seen pressing has no net; `archive` degrades gracefully where `blind` can't.

### 8.4 Standardised bench schema (Directive 3 + 4; AccuDisc §15.5 accepted with additions)

One TOML row per `disc × rung × span`. Frozen enough to ship for user-submitted rows.

```
disc_id           # AR/CDDB fingerprint (dedup + merge key)
drive             # normalised sysfs name
rung              # "R0".."R4" | "combined" | raw knob-set hash
span              # "start+count", empty = whole disc (recovery metrics are span-scoped)
set_speed         # requested Nx
measured_cx       # AccuDisc measured throughput (centi-X) — ground truth
governor_ceiling  # page-2A current at load, pre-cap — self-throttle triage signal
subq_ok           # Q frames CRC-valid (AccuDisc)
subq_total        # Q frames seen — the ratio's denominator (varies per pass)
c2_sectors        # residual C2 after this rung (AccuDisc)
recovered_sectors # problem seen, clean copy won (AccuDisc)
suspect_sectors   # reads disagreed, best-effort delivered — NOT verified (AccuDisc)
hard_sectors      # unreadable, zero-filled (AccuDisc)
ar_v1_pass        # cdda2img
ar_v2_pass        # cdda2img
ctdb_pass         # cdda2img (checksums)
ctdb_repaired     # cdda2img (parity rebuild succeeded, if that path ran)
discid_green      # cdda2img (MB/CDDB Disc-ID resolved)
wall_s            # whole-pipeline wall time
```

`--map-file` byte encoding for the span-finder (§15.4): `PENDING 0x0, OK 0x1, C2 0x2, HARD
0x3, RECOVERED 0x4, SUSPECT 0x5`; low nibble = state, high nibble = severity; **needs-recovery
= state ∈ {0x2, 0x3, 0x5}**.

**Q-yield measurement caveat (§15.6, load-bearing):** Q-yield-per-speed comes from a real
`--sub raw` capture (not the audio-only `speeds` probe), and must be the **median of ≥3
captures per rung** — a transient whole-pass Q collapse at specific request values (ZZ Top 32×,
Tracy 16×) would mislabel a single capture; flag any pass with global Q < ~0.90 as a transient
to discard.

### 8.5 Classification + ranking (Directives 3b, 3c)

The bench auto-classifies each method/combo per disc-class:

- **Hard error** (doesn't function) → **blacklist**.
- **Soft error** (functions but no improvement, or worse) → **warn**.
- **Improves** → keep.

Then ranks the survivors by: **(i) full data integrity, or best-effort-before-hard-failure —
compulsory**, then **(ii) speed — fastest that still achieves (i)**. The `archive` default is
whatever wins this ranking. Selector rule over §8.4 rows: among rungs where the integrity gate
passes (`ar_v2_pass ∧ ctdb_pass ∧ subq_ok/subq_total ≥ 0.90`, or `ctdb_repaired`), pick lowest
`wall_s`.

### 8.6 The bench itself (Directive 4 — our half ∪ AccuRip's)

Our half is the half AccuRip excludes — the **absolute** gate (AR v1/v2 + CTDB checksums **+
CTDB parity repair as one path**, §5) — combined with AccuDisc's relative-signal half into one
suite. Lives in `tools/` (evolving `toc_parity.py` + `c2read_recovery_test.py`, retargeted to
AccuDisc + AR/CTDB). Drives `accudisc read` at each rung × swept speed, gates the audio,
emits §8.4 rows, and prints the correlation table that confirms/refutes Keith's "each speed ≈
±the same success rate" hypothesis. Static-Q recoverability (axis D consensus) is a hypothesis
the bench tests, **not** a settled negative. Bench-shippable for user-submitted profiles.

### 8.7 Config surface & profile selection (Directive, 2026-07-18 — resolves O2)

The entire recovery axis-space collapses to **one** config entry:

```toml
recovery_profile = "archive"   # or compat / fast / paranoid / blind / <user profile>
```

- **Selection is ffmpeg-style (Keith's Option 2): no profile requested → no profile applied,
  bare flags only.** Absent both a config `recovery_profile` and a `--profile` flag, the rip
  runs on the individual recovery flags' own defaults — which therefore must themselves target
  the compulsory "(i) full data integrity" goal. A profile is purely an opt-in named bundle
  that expands to a flag set.
- **Any profile field is overridable by a flag** (e.g. `--profile archive --no-c2`), but a
  profile field has **no** corresponding per-field config entry — the axis knobs live *only*
  inside profiles (and as CLI flags), never as loose config keys. This is what keeps the config
  from sprawling back into the `off/auto/on` + `recovery_passes` + … zoo it replaced.
- **The C2 toggle lives here**, inside the profile (Axis F) — `compat`/`blind` carry C2-off; a
  `C2_UNSUPPORTED` drive selects (or is auto-switched to) a C2-off profile. There is no separate
  `c2_recovery` config key.

Migration for existing TOMLs: the old `c2_recovery = off|auto|on` key is dropped; a loader
shim maps a present legacy key to the nearest profile (`off`→`fast`, `auto`/`on`→`archive`) with
a one-time deprecation warning, then ignores it.
