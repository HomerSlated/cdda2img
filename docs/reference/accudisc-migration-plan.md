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

## 1b. STATUS — M1–M8 landed 2026-07-25 (Phases A–E)

**cdrdao and cd-paranoia are gone from `src/`.** `grep -rnE '"(cdrdao|cd-paranoia)"'
src/cdda2img/` returns nothing. 1237 tests pass; `make check` clean.

| # | Done | Note |
|---|------|------|
| M1 | ✅ | `_rip_disc_stage` is a single AccuDisc `read` — no engine choice left |
| M2 | ✅ | second metadata pass deleted; assembly failure now raises (no fallback) |
| M3 | ✅ | banner uses `fulltoc` + `cdtext` via new `accudisc_reader.read_lead_in` |
| M4 | ✅ | **O1 resolved as (a) — full removal.** `disc_reader.py` deleted |
| M5 | ✅ | `track_preview` uses `read_span`; real progress replaces file-size polling |
| M6 | ✅ | **was already broken** — it called `speed-report`, which AccuDisc removed, so it failed every call and silently fell through to cdrdao |
| M7 | ✅ | `write_offset` on `accudisc write`/`read`; **byte-swap removed** (cdrdao BIN was s16be, AccuDisc is s16le — a mechanical port would have corrupted every measurement) and the `FILE` line now emits both fields in MSF, which AccuDisc's parser requires |
| M8 | ✅ | version stamp from `accudisc --version` |

**Deleted (Phase E):** `cdrdao_ripper.py`, `cdrdao_progress.py`, `cdrdao_write_progress.py`,
`disc_reader.py`, plus their tests and the two retired cd-paranoia tools
(`paranoia_recovery_test.py`, `replay_paranoia_progress.py`). `RipInfo` moved to
`rbi_format.py` — it was the one live symbol `disc_reader` still owned.

**Also landed:** `disc_writer` now keys disc-not-blank on AccuDisc's `result=not_blank`
machine token (shipped in their `a76ede2`), retiring the stderr scrape that their contract
warned against.

**Not done, deliberately:** §9's profile/validator/strict-config block (P1–P5) — a feature
layer the plan already sequences separately. **Not yet validated:** a full live rip through
the single-engine path.

**cdrdao still invoked in `tools/`** — by design in `toc_parity.py` (it *is* the independent
reference side of the parity gate) and opportunistically in `compare_discid.py`,
`disc_scan.py`, `trace_album_live.py`, `trace_metadata_provenance.py`.

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

- `tools/toc_parity.py` — **DONE 2026-07-24**: retargeted from c2read to AccuDisc via
  `accudisc_reader.read_disc_c2` (metadata-only pass, no PCM). The gate now diffs
  **AccuDisc** vs `cdrdao read-toc`, exactly its purpose; the cdrdao reference side stays
  until cdrdao is removed. `tools/ctdb_repair.py` likewise retargeted (`read_toc` /
  `drive_supports_c2` / `read_disc_c2` + `park_spindle`).
- `tools/measure_write_offset.py` — pairs with M7 (`cdrdao write` + `read-cd`).
- Diagnostic/experiment scripts still on cd-paranoia (`paranoia_recovery_test.py`,
  `compare_discid.py`, `replay_paranoia_progress.py`,
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
suite. Lives in `tools/` (`toc_parity.py` for the metadata gate + the strategy bench
`strategy_bench.py`/`disc_triage.py` for recovery, all on AccuDisc + AR/CTDB; the retired
`c2read_recovery_test.py` was its prototype, archived in `private/deprecated/`). Drives
`accudisc read` at each rung × swept speed, gates the audio,
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

---

## 9. Bench-grounded profiles, validator, and strict config (2026-07-24)

Supersedes the profile *taxonomy* of §8.3 and the *default semantics* of §8.7. The §8
rationale (sub/no-sub, C2 toggle, verify, CTDB, the axis space) stays; only the profile
**names/values** and the **no-profile fallback** change, now that the recovery bench has
run (4 discs, 1 drive, n=3/cell; `private/bench/runs/run3/SUMMARY.md`). **Not yet code.**

### 9.1 What the bench settled (replaces the §8.3 buzzword profiles)

The `archive`/`compat`/`fast`/`paranoid`/`blind` names were coined before any measurement
and don't map to a measured behaviour. They are **retired**. The shipped profiles are the
strategies the bench actually ranked. Aggregate (8 targets):

```
track-ladder     19/20  0.95   most reliable; sole 3/3 at the hardest target (ABBA t19, n=47)
track-constant   14/19  0.74
max-variation    13/20  0.65   fast at low n; 0/9 at high n
whole-disc       11/20  0.55
sector-runup      2/20  0.10   experimental
sector-hammer     2/20  0.10   experimental (= the old _recover_rung shape)
span-fixed        1/16  0.06   experimental
```

Established, mechanism-free: recovery happens at **track granularity**; the **speed ladder
earns its cost at high n** (t19: ladder 3/3 vs constant 1/3), so the ladder is not
optional; sector-level recovery is **not viable** (2/20, both wins one degenerate n=1
target); **n (flagged sectors in the track), not damage radius, governs** which strategy
wins (ABBA inner-edge t1 n=1 == outer-edge disc t19 n=1). Values are provisional data;
the validator + resolution are the stable interface.

### 9.2 Profile schema (one shape; folds the §8 axes in as fields)

```toml
name         = "track-ladder"    # sanitised [a-z0-9_-]; the default profile
experimental = false             # true => hidden from triage suggestions, still selectable

# capture (pass 1)
sub          = true              # true = full PROV (§8.1); false = no-sub fast path
c2           = true              # request C2 bitmap; auto-forced off on C2_UNSUPPORTED/UNVERIFIED

# recovery re-read (pass 2 — the bench axis)
granularity  = "track"           # sector | track | whole-disc
ladder       = "full"            # full | single   (bound at runtime, §9.3)
speed        = "max"             # single only: max | mid | min | 0.0–1.0
passes       = 3
run_up       = 0                 # sectors before target (sector granularity)
span         = 0                 # span-fixed only
variation    = "none"            # none | speed | full   (full = max-variation)

# recovery adjuncts
ctdb         = "auto"            # off | auto (parity repair when it is the fastest verified path)
verify       = false             # false = informational | true = require-green-before-accept
budget_s     = 300
```

- **`sub` is a profile field AND a `--sub/--no-sub` override flag** — the profile stays a
  complete recovery spec; the flag preserves the quick toggle. **DECIDED 2026-07-24 (Keith):
  `sub` is a per-profile field, not a §8.1-style orthogonal axis.**
- **`ctdb="auto"` default** — parity repair fires only when CTDB has the disc and it beats
  a re-read (Keith: "no reason not to use parity repair when it's the fastest path").
- **`c2` auto-degrades** — `c2=true` on a C2-incapable drive runs C2-off, logged; no
  profile fails merely because a drive lacks C2 (§8 Axis F stays load-bearing).

### 9.3 Speed ladder — bound to `accudisc speeds`, never hardcoded

`probe_speed_ladder` is retired (§4.3). The ladder is derived per disc from `accudisc
speeds`, admitting **only rungs the drive honoured exactly**:

> **Strict rule** (whenever *any* row reports a non-zero `page2a`):
> `ladder = [page2a for (req, page2a, _) in rows if req == page2a]`, descending.
> **Fallback** (when *every* row reports `page2a == 0`): admit on `measured`,
> collapsing rungs with equal `measured`.
> **Outcome guard:** if the ladder resolves **empty by any path**, degrade to a single
> rung at the drive's reported maximum and warn.

`measured` is informational for admission ordering only (throughput telemetry, never a
settable value, radius-dependent on CAV and only *ordinally* comparable across drives —
AccuDisc probes the middle half); a row where `req != page2a` means the drive quantised
the request and is dropped. Worked example (PX-716A): rows 40/32→✗, 32/32→32, 24/24→24,
16/8→✗, 8/8→8, 4/4→4 ⇒ **[32, 24, 8, 4]**.

**Why the fallback and the guard exist (AccuDisc 2026-07-24 §32.4 + our §33.5).**
`page2a == 0` means "mode page 2A did not report", **not** "quantised to zero" — on a
drive with no usable page 2A every row is 0, so the bare equality test admits nothing and
the ladder is silently **empty**. AccuDisc caught that. A second cause we then found: a
drive reporting a real `page2a` that never *equals* `req` (supports only 10×/20× while we
probe {40,32,24,16,8,4}) — non-zero, so the fallback won't fire, empty again. Hence the
guard is on the **outcome**, not the cause: an empty ladder is not a reachable state.

Policy resolution: `full` → the whole admitted list fastest→slowest; `single`+`speed` →
the admitted rung nearest max / mid / min / `fraction×max`; `variation="full"` → random
admitted rung per attempt.

### 9.4 No-profile fallback (replaces §8.7's ffmpeg-style default)

§8.7 assumed AccuDisc flags carry integrity-targeting defaults, so "no profile → bare
flags" was safe. That fallback is **superseded** — but note the corrected reasoning below,
because the premise this section originally gave was wrong.

**CORRECTION (AccuDisc 2026-07-24 §32.3).** We asserted "AccuDisc has no flag defaults".
Wrong for exactly one flag: **`--retries` defaults to 2** when omitted (and `0` is treated
as omitted — zero retries cannot be requested). The other four are off-when-omitted as
assumed. So "bare flags" is not *nothing*; it is `retries=2` with everything else off,
i.e. effectively AccuDisc's **R0** rung. **The decision stands on better ground:** R0 is a
real recovery floor but well below our measured best (`track-ladder`, 19/20 over 8
targets), so a rip with no profile requested still gets our default profile rather than
falling through to bare flags.

Resolution (single pure, unit-testable `resolve_recovery`):

```
1. any --ad-* recovery flag present   → honour AccuDisc flags ONLY; no profile; no merge
2. --profile NAME                      → load+validate NAME; error+exit if absent/invalid
3. cfg.default_profile set             → load+validate it
4. none of the above                   → built-in "track-ladder" (the bench winner)
```

`--ad-*` is a namespaced AccuDisc passthrough (`--ad-speed`, `--ad-retries`, `--ad-c2`,
`--ad-recovery …`; final set pinned from AccuDisc `docs/cli-machine-interface.md`).
`ResolvedStrategy` is the only object the rip path sees; the source (flags/profile/default/
builtin) is recorded in PROV as `recovery_source`.

### 9.5 Two-stage validator (modular, schema-driven, shared config↔profile)

New `src/cdda2img/validation.py`, generic engine + per-consumer schema.

- **Stage 1 — spec (structural):** declarative `FIELD_SPECS` — per field `(type, required,
  enum?, default?)`. Presence, type, shape, enum membership, unknown-key report.
- **Stage 2 — sanity (semantic):** ordered `SANITY_RULES` predicates over the spec-valid
  dict — `passes>=1`, `budget_s>0`, `run_up>=0`, `span>=0`, `speed` fraction in `[0,1]`;
  coherence: `variation="speed"` ⇒ `ladder="full"`; `span>0` ⇒ `granularity="sector"`;
  `ladder="single"` ⇒ a `speed` selector.

```python
def validate_spec(data, schema)   -> list[Error]   # stage 1
def validate_sanity(data, schema) -> list[Error]   # stage 2
def validate(data, schema)        -> list[Error]   # 1 then 2, short-circuit
```

Two schemas (`PROFILE_SCHEMA`, `CONFIG_SCHEMA`) over one engine — upstream change edits a
table, not code. The two stages are distinct because a field can pass format yet hold an
illegal value (Keith).

**Frozen AccuDisc recovery-flag contract (AccuDisc 2026-07-24 §32.3).** Stage-1 ranges for
the `--ad-*` passthrough and the profile fields that map onto these bind here:

| flag | type | legal range | omitted | no-op when |
|---|---|---|---|---|
| `--retries K` | u8 | 1–255 (`0` → 2) | **2** | never |
| `--c2-retries N` | u8 | 0–255 | 0 = off | **silently** when C2 is off |
| `--verify P` | u8 | 0–255 (`0` and `1` = one pass) | 1 pass | `P < 2` |
| `--overlap K` | u8 | 0–255, **silently clamped to 8** | 0 = off | clamped below chunk size |
| `--ladder LIST` | ≤ **8** u16 rungs, comma/space separated | — | reread at current speed | nothing triggers a reread |

Semantics the profile schema must respect: `--retries` is per-sector attempts *after a
chunk read fails*; `--ladder` rung *n* serves rescue/consensus attempt *n*, saturating at
the last rung; **verify passes stream at the base speed**, so a speed-diverse sweep means
whole passes at different `--speed`, not per-chunk switching (which is what
`_recover_failed_tracks` already does).

**We must range-check these ourselves.** AccuDisc parses all five with `strtol` cast to
`uint8_t` *unguarded*, so `--retries 256` silently becomes 0→2 and `--verify 258` becomes
2; negatives wrap. They will make out-of-range a hard argument error (exit 2); until that
lands our stage 1 is the only guard. This is the canonical example of why the two stages
are separate — `256` parses as a perfectly good integer and is an illegal value.

### 9.6 Config becomes strict

- `load_config(strict=True)` (default) raises `ConfigError` listing every stage-1/stage-2
  failure. **An invalid config forces error + exit in every subcommand except `setup`.**
- `main()` loads once, early; on `ConfigError`, if `cmd != "setup"` print errors + "run
  `cdda2img setup`" and exit non-zero; `setup` uses a raw/lenient load so it can repair.
  Migrate the ~8 in-body `load_config()` calls to the single early `cfg`.
- New `setup` section **"Config: Edit ($EDITOR)"** — open config in `$EDITOR`, re-validate
  on save. `setup --validate-config` becomes the real two-stage validator (today it only
  checks TOML-parses + unknown-keys — neither stage).
- Lenient per-field fallbacks (`_bounded_int`, `_parse_c2_recovery`) are replaced by the
  schema; "warn and default" is dropped.

### 9.7 Profile creation (new `setup` section "Profiles: Create")

Emits a profile from selected flags/answers into the user profiles dir. Guards:
1. **Name sanitiser** — lowercase, `[a-z0-9_-]` only; any other char → error, no silent
   mangling.
2. **Overwrite guard** — reject a name matching any **shipped** or existing **user**
   profile: "Profile already exists, please choose a different name." No `--force`.

Storage: shipped → package `conf/profiles/*.toml` (immutable, ships all 7); user →
`$XDG_CONFIG_HOME/cdda2img/profiles/*.toml`. Resolution searches **user then shipped**;
shipped names reserved. Creation writes atomically (`.tmp`+rename) through §9.5's validator
so a profile cannot be born invalid.

### 9.8 Delta to the §5 phasing

Insert before §5 Phase E (dead-module removal), after the read/write migration:

- **Phase P1** — ✅ **DONE 2026-07-25** (`f91b9ec`). `validation.py` engine + both schemas
  + 50 tests. `CONFIG_SCHEMA` is not yet the loader's authority — that is P3.
- **Phase P2** — ✅ **DONE 2026-07-25**. `conf/profiles/` (7 files),
  `recovery_profile.py` (`Profile`, `load_profile`, `list_profiles`,
  `resolve_recovery`, `rungs_for`, `bind_ladder`), `drive_speed.admitted_ladder`
  (§9.3), `Config.default_profile`, 37 tests.

  > **§9.3 confirmed against hardware, with a correction to the worked example.**
  > `accudisc speeds` exists and emits `speed req=N page2a=M measured=X.XX`, as the
  > rule assumes. But the example ladder `[32, 24, 8, 4]` is **not** a property of the
  > PX-716A: re-probing the same drive with the same disc on 2026-07-25 gave
  > `[8, 4]`, because the drive's governor had throttled ABBA *Gold* as it degraded
  > (every request ≥8× reported `page2a=8`). The ladder is **drive × disc**, and the
  > strict `req == page2a` rule is what keeps that honest — it drops the rungs the
  > drive refused, whatever the reason. Never cache a ladder per drive. Two further
  > notes: the probe leaves the drive at its last rung, so the binder restores it;
  > and legacy `probe_speed_ladder` returned `[8, 4]` on the same disc, so P5's
  > swap is behaviour-neutral there.
- **Phase P3** — config → strict: `load_config(strict)`, `main()` bootstrap, `setup`
  Config:Edit, migrate in-body loads, retire the §8.7 legacy-key shim into this path.
- **Phase P4** — `setup` Profiles:Create (§9.7).
- **Phase P5** — `rip --profile` + `--ad-*` passthrough; wire `ResolvedStrategy` into
  `_recover_failed_tracks` (which already implements `track-ladder` — the whole-track
  ladder sweep — so the default path is largely a no-op rename + explicit binding).

Verification additions: `--profile nope`→exit; same profile on two drives → each binds its
own admitted ladder; invalid config → every non-setup subcommand exits, `setup` repairs;
shipped-name collision and illegal-char both rejected; `--ad-*` present → profile ignored
(`recovery_source=ad-flags`); AccuDisc mid-rip error → we exit non-zero, temp dir gone,
AccuDisc's message shown verbatim.

---

## 10. Cross-project contracts and findings from the 2026-07-24 coordination round

Correspondence: our §32–§40, AccuDisc's 07-24 → 07-24h, in the AccuDisc repo's
`private/docs/c2read-to-accudisc.md`. Work items live in `TODO.md`; this section
records what is **binding between the two projects**.

### 10.1 `escape_toc_string` is a cross-project contract — do not weaken it

AccuDisc's `adsc_toc_parse_cue` is line-oriented with no quote tracking. A newline
inside a quoted `CD_TEXT` value is parsed as TOC directives: their PoC produced a
phantom track, a shifted lead-out and an attacker-chosen ISRC, returned as
`ACCUDISC_OK`. Until their quote-aware parse ships, **our `escape_toc_string` is the
only thing protecting their burn layout** from MusicBrainz free text — it strips
control characters first and unconditionally. Verified holding under their payload.
Not a cdrdao-only concern; do not refactor on that assumption. They will report when
the fix lands, at which point this reverts to defence in depth.

> **UPDATE 2026-07-24 (AccuDisc §2026-07-24i):** their fix **shipped** — `a619854`
> on `accudisc` `main`. `adsc_toc_parse_cue` now tracks quote context; an
> unterminated quote at end-of-line is `ACCUDISC_ERR_INVAL` (cdrdao's own
> flex-lexer rule), and `accudisc_write()` refuses the injected shape at intake,
> before SEND CUE SHEET. Regression pinned in their `tests/test_tocparse.c`, suite
> 19/19. `escape_toc_string` is therefore **defence in depth on the burn side, not
> load-bearing** — but we **keep it**, for two reasons AccuDisc and we both hold:
> (1) our `import` path reads foreign `.toc` on the *read* side, where
> `accudisc_write()` never runs, so escaping is still the **primary** guard there
> (this is the open `toc_parser.py` audit in `TODO.md`); (2) belt-and-suspenders
> across a producer/consumer trust boundary is worth keeping even once the far side
> is hardened. Their parser hostile-input sweep (our §40.3 question) is filed `[P2]`
> and still ahead of them.

### 10.2 CD-Text write: pass-through, and the acceptance corpus

`write --cdtext FILE` consumes byte-identically what `read --cdtext FILE` emits — no
transcoding, structurally guaranteed (no code path reads the payload). Accepted on
`len >= 22 && (len-4) % 18 == 0` plus a header cross-check. **The multiple-of-4 pack
refusal was killed before shipping** — three counterexamples: 33 packs (CDEmu), 35
(real PX-716A, redumper), 42 (libmirage encoder). AccuDisc ring-fills across the wrap
rather than padding short blocks.

> **CORRECTION 2026-07-24 (AccuDisc §m, B3 landed `a97f9f9`):** this ring-fill is **not a
> divergence from cdrdao** — an earlier shared premise (our §35.3) that cdrdao "cycles
> pre-built *blocks* and cannot emit a non-multiple-of-4 stream" is **false**. cdrdao's
> `CdTextEncoder::buildSubChannels` ring-fills *packs* (`prun = prun->next_; if NULL prun
> = packs_`), setting the block count to `lcm(npacks,4)/4` (33→33, 35→35, 42→21) and
> cycling that minimal set over the lead-in — so cdrdao already burns non-mult-of-4
> CD-Text on real discs. AccuDisc's B3 is byte-identical in method (verified against
> `setRawRWdata`/`getRawRWdata`). Consequence: the 33/35-pack path is a **proven
> mechanism, not a weak oracle** — a mismatch there is a diff, not a puzzle; no
> "4-aligned-only" caveat applies to the acceptance fixture. Decode-side note (does not
> affect us — `cdtext.py` shares no CD+G path): CD-Text lead-in R-W uses **no
> Reed-Solomon and no interleave**, only the 6-bit packing + per-pack 16-bit CRC, unlike
> CD+G program-area R-W.

Zero-CRC (approved by Keith): valid → write; **all zeroes → recompute from payload,
payload untouched, noted on stderr**; non-zero-and-wrong → refuse (escape flag). This
is the one place "AccuDisc only moves bits" carries an asterisk, and it is documented
in their man page and machine interface, not buried. Origin is drive-side, not
redumper (redumper's dump path is a verbatim pass-through; `cd/toc.ixx:423` documents
`PLEXTOR PX-W5224TA: crc of last pack is always zeroed`). Open hypothesis: an
allocation-length short transfer — our two real-hardware captures show valid at 148 B
and zeroed at 634 B.

### 10.3 The burn invariant (adopted both sides)

> Exit 0 must never mean "burned the disc but silently dropped metadata." A metadata
> failure is a hard failure, not a downgrade. If any requested CD-Text cannot be
> written, refuse the burn — never write the audio and drop the metadata.

Earned from cdrdao: a single U+2010 dropped all 20 CD_TEXT blocks and exited 0. The
sharper defect is that `CdTextItem::updateEncoding()` returns **void** — there is no
channel by which a caller *could* learn the encode failed. Validate at intake, before
any media is touched. Every diagnostic must name the offending item (cdrdao's cannot:
`log_message(-2, "…\"%s\"…")` has no vararg).

**`accudisc write` exit-code contract (reconciled AccuDisc `b547a60`, their §s;
`disc_writer.py` updated 2026-07-24, §50).** The tool-wide convention now applies to write
too — and the disc-not-blank case **moved from exit 3 to exit 2**:

| exit | meaning | disc written? | `disc_writer.py` |
|---|---|---|---|
| 0 | clean burn | yes | return |
| 2 | could-not-complete: **disc not blank**, or transport/device failure | **no** | raise; not-blank message when stderr contains `not blank`, else generic |
| 3 | **completed with caveats** (e.g. CD-Text SIZE_INFO ≠ `.toc`) | **yes** | **surface the caveat, return success — do NOT raise** |

This is the burn invariant made operational: exit 3 is precisely "written, but a metadata
mismatch you must be told about", so we print it loudly and succeed. Note the caveat cannot
fire on our burns *today* — v0 passes no `--cdtext` (the §10.6 gap) — but the not-blank move
affects every burn. (C API equivalent: `accudisc_write()` returns a **positive**
`ACCUDISC_WROTE_WITH_CAVEATS`; test `rc > 0`, not `rc != ACCUDISC_OK`.)

**Not-blank keying (interim → final, §t/§51).** Exit 2 alone can't tell not-blank from a
transport fault, so the interim keys on `"not blank"` in stderr. That depends on stderr
*wording*, which AccuDisc's contract reserves the right to change — so AccuDisc agreed to
emit a machine token (`summary … result=not_blank` / `result=error`) on `--progress-fd` for
burn-didn't-start cases. When it ships we switch to keying on `result=not_blank` and drop
the stderr scrape (tracked in `TODO.md`). Until then the interim degrades to the generic
exit-2 message if the wording changes, so no burn misbehaves.

### 10.4 Step D CD-Text acceptance needs a physical disc

**No stored image of ours can serve** — every RBI carries a *synthesised* TOC whose
`CD_TEXT` comes from metadata lookup, not from the pressing. ABBA *Gold* is the proof:
`accudisc cdtext` on the real disc returns `absent`. ABBA remains the article for
audio, MCN, ISRC and pre-gaps, which it does carry. Requirement: **any physical CD
that genuinely carries CD-Text**, read once, no burn. Pending Keith.

> **UPDATE 2026-07-24 (AccuDisc §n retraction, `8bda198`):** the **Stanley Road CD-R may
> now qualify** — it was set aside on two grounds, "very limited CD-Text" *and* "a
> degraded lead-in", and **the second ground is void**: its `degrade=leadin_unreadable`
> was an AccuDisc transfer-length bug, not media damage. A full TOC is `37 + 11*ntracks`
> bytes — **odd whenever the track count is even** — and ATAPI moves 16 bits at a time, so
> the odd length was rejected by the host adapter (`DID_ERROR`) before the drive answered.
> Stanley Road has **12 tracks** → `169` bytes → odd → exactly this class. It *does* carry
> real CD-Text (cdrdao: `Found CD-TEXT data`, disc `TITLE "Stanley Road"` / `PERFORMER
> "Paul Weller"` + `SIZE_INFO`), sparse and with empty-string fields — arguably a *better*
> edge-case article than a rich one.
>
> **CONFIRMED 2026-07-24 on the real disc, PX-716A, with `8bda198`:**
> ```
> source=fulltoc degrade=none pregaps=none sessions=1..1 disc_type=0x00 session_count=1
> ```
> `source=fulltoc degrade=none` where it previously read `source=toc
> degrade=leadin_unreadable` — the full TOC now reads cleanly. **Stanley Road's lead-in is
> not degraded and never was.** It is therefore a **live Step-D CD-Text article**: real
> on-disc CD-Text, clean lead-in, and sparse/empty-string fields that exercise edge cases a
> rich disc would not. (Note the earlier cdrdao run was never a clean control — it also
> fell back off its raw-TOC path, but with a different signature, *bogus data returned*
> rather than a transport error, via its plextor-driver raw read. The `accudisc` re-test is
> what settles it.)

### 10.5 The review question (adopted verbatim by both projects)

> **What does this accept if the producer is hostile, or merely wrong?**

Three instances in one session, three projects, same shape — *the boundary was trusted
because the usual producer happens to be well-behaved*: cdrdao assumed encodable input;
AccuDisc assumed escaped input; we assume a benign foreign `.toc` on import. AccuDisc
is running it across their drive-response parsers too, on the grounds that a drive is a
producer as well.

### 10.6 OPEN DECISION — v0 pass-through cannot author CD-Text from strings (2026-07-24j)

AccuDisc corrected a conflation in our §41: their `a619854` intake refuses the
**injection** shape (newline-in-quoted-string), **not** an un-encodable character inside
a legitimate single-line `TITLE`. In v0 **pass-through** there is nothing to refuse,
because `accudisc write --cdtext FILE` burns the raw **format-05 blob** verbatim and
never sees the `.toc` strings (`tocparse.c` ignores `CD_TEXT` blocks). So the reason
cdrdao's lead-in-drop cannot recur through AccuDisc is *"pass-through never encodes"*,
not a check — `fold_cdtext()` at TOC-generation is doing the real work.

The consequence for **our burn cutover**: a pass-through burn needs a *source* format-05
blob. **Re-burning a captured disc has one** (`read --cdtext`). **Authoring a fresh disc
from MusicBrainz metadata does not** — there is no strings→packs step in v0, and
**`cdtext.py` is decode-only** (no encoder in our tree today; cdrdao is currently what
encodes `CD_TEXT` strings→packs at burn). So once cdrdao is removed, our folded CD-Text
strings have **no path onto a freshly-authored disc** until one of:

- **(a)** we only ever *re-burn captured discs* (zero new code; but no metadata-authored
  CD-Text ever);
- **(b)** we carry **our own strings→packs encoder** to feed `--cdtext` (real port —
  libmirage `cdtext-coder.c` or cdrdao's `CdTextItem`; this also unblocks §10.4, since it
  would let us author a genuine CD-Text disc for AccuDisc's Step D);
- **(c)** we wait on **AccuDisc's authored v1** (their deferred strings→packs mode, which
  fails an un-encodable codepoint *before* the burn per their RECORDING_PLAN §11.9 rule 4).

**DECIDED 2026-07-24 (Keith): (c).** AccuDisc builds authored CD-Text mode
(strings/`CD_TEXT` → 18-byte packs → format-05 blob), **promoted off deferred v1 onto
the critical path** — pack encoding is bit-formatting, which is libaccudisc's scope and
mirrors the decoder they already ship; option **(b) is off the table** (don't port an
encoder). **Our interim is (a): re-burn captured discs only** — the burn cutover is safe
to make now for re-burns; a *fresh* metadata-authored disc burns without CD-Text until
their authored mode lands. **`fold_cdtext()` stays** — it is exactly the charset-folded
input their encoder consumes, and their encoder fails-before-burn on any residual
un-encodable codepoint (their RECORDING_PLAN §11.9 rule 4), never silent-drops.
Sequencing: their pass-through v0 ships first (enables re-burns), authored mode
immediately after.

Authored-mode first-cut scope, **LOCKED** (§43–§44, AccuDisc §l → RECORDING_PLAN §11.1),
grounded in `generate_toc`: **block 0, single language, single-byte charset, pack types
0x80 TITLE (disc+track) + 0x81 PERFORMER (disc+track) + 0x86 DISC_ID (disc-level,
conditional on `cdtext_catalog_ref`) + mandatory 0x8f SIZE_INFO.** 0x86 is required or a
set field silently drops on the round-trip. **0x82 SONGWRITER out** (we never author it).
**0x8e UPC/ISRC out** — our `CATALOG`/`ISRC` are top-level Q-subcode directives that reach
the disc via the subchannel, not CD-Text packs; encoding them as packs would duplicate
metadata that already arrives by another path. Correction from AccuDisc §l: 0x86 is *new*
to their encoder, not a mirror of their decoder (which decodes 0x80/0x81 only) — but the
18-byte pack machinery is type-agnostic, so it is a constant not a subsystem, and
authored-mode acceptance is a **byte-for-byte blob compare** (write→read-back→compare)
that never decodes, so a decoder blind to 0x86 still proves a 0x86 round-trip.
