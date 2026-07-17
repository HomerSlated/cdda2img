# AccuDisc Migration Plan — retire cdrdao + cd-paranoia for all disc activity

**Status: LIVING DOCUMENT.** Started 2026-07-17. Padded out as the AccuDisc agent's
work (speed cap + Q-channel recovery) lands. Supersedes `c2read-upgrade-plan.md` for
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
must be **capped** to the Q-optimal speed (≈24× on the PX-716A), not restored to max. This
is exactly the speed-CAP mechanism the AccuDisc agent is verifying now via SET STREAMING —
see `reference_set_streaming_cdb_layout` in memory and §4 below. There is no AccurateRip /
CTDB equivalent for subchannel data; blind re-reads + the position model are the only
recovery levers.

---

## 2. Scope — migrate vs. keep vs. already done

### 2a. MIGRATE — live-drive binary call sites (the actual work)

Grounded in a full `rg` sweep of `src/` (2026-07-17):

| # | Call site | Today | Target |
|---|-----------|-------|--------|
| M1 | `cdrdao_ripper.rip_cdrdao()` — `cdrdao read-cd` | primary rip (normal path) | AccuDisc `read --sub raw` (capped speed), single pass |
| M2 | `cdrdao_ripper.read_toc_metadata()` — `cdrdao read-toc` | metadata 2nd pass / C2-path fallback | drop — `subq_toc.build_rip_info` from the M1 pass |
| M3 | `cdda2img.py:_fast_toc()` — `cdrdao read-toc --fast-toc` (banner) | pre-rip disc geometry for the banner | AccuDisc fulltoc parse (`parse_fulltoc`) |
| M4 | `disc_reader.rip_disc()` + `-Q` query — `cd-paranoia` | full-disc read fallback when cdrdao fails | see Open decision O1 (emergency fallback fate) |
| M5 | `track_preview.py` — `cd-paranoia -Z 1` | cosmetic track-1 preview during rip | AccuDisc `read_span` of track 1 |
| M6 | `drive_speed._read_speed_cdrdao()` — `cdrdao drive-info` | speed-read fallback (accudisc speed-report is already primary) | drop the cdrdao fallback |
| M7 | `write_offset.py` — `cdrdao write` + `cdrdao read-cd` | burn-and-read-back write-offset measurement (`setup --write-offset`) | AccuDisc `write` + `read` |
| M8 | `rip_log.py` — `cdrdao version` / `cd-paranoia --version` | engine-version stamp in the rip log | AccuDisc version string |

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
`accudisc_reader.py` absorbs flag/subcommand deltas. This plan needs these to land there:

1. **Verified speed CAP** *(in progress)* — SET STREAMING (0xB6) governs read speed on the
   PX-716A and reflects in mode-page 2A; the byte-9/10 param-list-length fix unblocked it
   (memory `reference_set_streaming_cdb_layout`). cdda2img needs a way to request "read at
   ≤ N×" and trust it governs the 0xBE DAE path, verified against real throughput — not just
   the page-2A echo. The `CDROM_SELECT_SPEED` ioctl in `drive_speed.py` sets a ceiling
   without root; SET STREAMING is AccuDisc's SG_IO route.
2. **Q-optimal single pass at capped speed** — one `read --sub raw` at the capped speed
   yielding audio + C2 + P-W sub + inline `--fulltoc`/`--cdtext`, in one spin-up.
3. **Q-channel recovery** *(planned)* — multi-pass per-sector majority vote + position-model
   interpolation (position increments 1/sector, track/index piecewise-constant) to
   reconstruct never-clean Q frames from CRC-valid neighbours. Reference: redumper
   `cd/subcode.ixx`. Recovered pre-gaps are the acceptance target.
4. **Disc-geometry query** for the pre-rip banner (M3) — fulltoc is sufficient; confirm the
   snapshot exposes it cheaply (no full read).
5. **Version string** (M8).
6. **Emergency fallback contract** (O1) — behaviour when AccuDisc cannot drive a given drive.

Offset correction stays entirely in cdda2img: AccuDisc returns **raw** PCM; `apply_offset`
runs exactly once, at storage. Unchanged by this migration.

---

## 5. Phased execution (each phase: own commit, `make check`, full pytest, Py3.10, live rip)

- **Phase 0 — blocked on AccuDisc** *(current)*: speed cap + Q recovery land in AccuDisc and
  are frozen in `cli-machine-interface.md`. cdda2img waits. Nothing to commit here.
- **Phase A — capped single-pass rip (M1 + M2 + M3):** restructure `_rip_disc_stage` so the
  AccuDisc capped-speed pass is the rip, `subq_toc` supplies metadata, and the banner reads
  fulltoc. Retire `cdrdao read-cd`/`read-toc` from the rip path. Wire the speed cap
  (`drive_speed` gains a `cap_drive_speed`; the C2 path stops calling `restore_drive_speed`
  until the rip is done — closes the subq_speed_cliff regression). Update `toc_parity.py`
  to AccuDisc **first** (gate must be green before the flip). PROV `toc_source` unchanged.
- **Phase B — single-purpose call sites (M5 + M6 + M8):** track preview → `read_span`;
  drop the cdrdao speed fallback; version stamp → AccuDisc.
- **Phase C — write-offset tool (M7 + `measure_write_offset.py`):** `cdrdao write`+`read-cd`
  → AccuDisc `write`+`read`.
- **Phase D — fallback decision (M4, O1):** resolve cd-paranoia's fate; demote to optional
  emergency read or remove `disc_reader.py`.
- **Phase E — dead-module removal + docs:** delete §3 modules once soaked; update CLAUDE.md
  rip-pipeline §1, the man page, `conf/cdda2img.toml.example`, and close TODO items.

---

## 6. Open decisions

- **O1 — cd-paranoia's fate.** The stated goal is zero cd-paranoia. But cd-paranoia is the
  only current path for a drive AccuDisc can't drive. Options: (a) remove entirely once
  AccuDisc proves out across the shelf; (b) keep a minimal emergency `-Z` read fallback
  behind a flag during a transition window. Recommend (b) until the shelf soak passes, then
  (a). **Decide with the user before Phase D.**
- **O2 — `c2_recovery` config surface.** With AccuDisc as the sole engine, the `off/auto/on`
  tri-state (which chose between cdrdao and the C2 path) loses meaning. Likely collapses to a
  recovery-passes knob. Needs a config-migration story (existing user TOMLs).
- **O3 — default speed cap value.** Per-drive Q-optimal speed isn't universal (24× is a
  PX-716A figure). Probe-and-characterise per drive (extend `probe_speed_ladder` with a Q-yield
  measurement), or a conservative global default with per-drive override in `[[drives]]`.
- **O4 — pass-1 speed vs. runtime.** A capped first pass is slower than today's max-speed rip.
  Quantify the wall-clock cost on a clean disc so the trade (accuracy vs. time) is explicit.

---

## 7. Invariants / non-goals

- **AccuDisc stays external** — never shipped from this repo; git-ignored snapshot only.
- **RBI format unchanged** — no format bump for the engine swap.
- **cdrdao TOC text format retained** — RBI-embedded TOC + cdemu load + foreign-image import.
- **Foreign-image import parsers untouched** — cdrdao TOC+BIN, DDP, NRG, CCD are file formats.
- **Offset domain unchanged** — AccuDisc returns raw; single `apply_offset` at storage.
- **Prefer no data over wrong data** — a Q frame that can't be recovered is dropped, never
  guessed; a pre-gap that survives recovery is declared, one that doesn't is absent (and, per
  §1, that absence should now be *rare and real*, not a silent speed artefact).
