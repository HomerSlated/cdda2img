# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`cdda2img` is a CLI tool for creating, importing, ripping, extracting, and verifying **RBI (Red Book Image)** archive containers of CD-DA audio discs. Subcommands:

- **`rip`** — rip a physical disc via AccuDisc (single pass: audio + C2 + subchannel + lead-in)
- **`create`** — create one or more RBIs from a directory of audio files
- **`import`** — import a foreign disc image (cdrdao TOC+BIN, DDP 2.0 / GEAR Pro, Nero NRG, or CloneCD CCD/IMG)
- **`extract`** — extract to per-track FLAC + CUE, or a TOC + s16le WAV image, or both
- **`list`** — list container sections and track index with offsets and checksums
- **`test`** — verify all block checksums and structural invariants (27 checks, exits 1 on failure)
- **`burn`** — burn an RBI back to a blank CD-DA disc via AccuDisc (`write --toc --bin`; `--simulate` for a laser-off test write)
- **`mount`** — extract a TOC+BIN scratch copy and load it into a cdemu virtual slot
- **`catalogue`** — browse the local disc catalogue (summary, search, per-disc detail)
- **`doctor`** — report every dependency (Python packages, the AccuDisc engine, external binaries, native libraries) and exit 1 if a required one is missing. Checks only: it never installs, downloads, or modifies anything

The `rip`, `import`, and `create` pipelines all embed cdrdao-format TOC text, optional EBU R128 ReplayGain, and raw s16le PCM in a single RBI container. Metadata (album, artist, track titles, ISRC, CATALOG, low-dynamic-range flag, original-release lookup) is sourced from CDDB, MusicBrainz, AcoustID, and Discogs lookups and an interactive confirmation menu.

This is a prototype; a Rust reimplementation is planned once the design has stabilised.

## Tools

Standalone utility scripts live in `tools/` (tracked, not part of the installed package).

- **`tools/recovery_bench.py`** — **the last CLI consumer in the tree** (2026-08-01). It
  resolves its own `tools/accudisc/accudisc` and drives eight subcommands, including
  `read --map-file` with the per-rung recovery flags that *are* the experiment. Not ported
  with the rest: (a) `c2lag` has **no API equivalent at all** — asked of AccuDisc; (b) it is
  the instrument behind the seven shipped profiles and RECOVERY.md's numbers, so a
  behaviour change during the port silently invalidates every cross-run comparison, and
  re-validating needs a hardware bench run. Everything else it needs *is* in the API
  (`retries`, `c2_retries`, `verify_passes`, `overlap_sectors`, `speed_ladder`,
  `status_map`, `probe_accurate_stream`, `set_speed`), so this is scheduling, not a
  blocker — except for `c2lag`.
- **`tools/toc_parity.py`** — field-by-field parity gate: `cdrdao read-toc` (the independent
  *reference* implementation — the one place the cdrdao binary is still invoked) vs the AccuDisc
  subchannel assembly (`subq_toc.build_rip_info`), live or from saved captures. Green across
  the disc shelf is the acceptance condition for preferring the single-pass path.
- **AccuDisc** — the low-level CD-DA read engine `accudisc_reader.py` drives, a **separate
  project** (https://github.com/HomerSlated/accudisc), external like cdrdao / cd-paranoia and
  **not shipped from this repo**. `tools/accudisc/accudisc` (git-ignored) is a **symlink into
  the AccuDisc build tree** — retained **only** for `tools/recovery_bench.py`, the last
  consumer of the CLI (see its entry); nothing in `src/` or `tests/` executes it — `~/Git/accudisc/build/cli/accudisc` — not a copy. It was a
  snapshot until the API was complete; maintaining a separate one stopped paying once both
  transports resolved to the same build. The subprocess contract is frozen in the AccuDisc
  repo's `docs/cli-machine-interface.md`. **Two symlinks, not one**: `tools/accudisc/pybinding`
  → their `bindings/python` is how the Python binding is found (see the transport bullet under
  `accudisc_reader.py`). Both are git-ignored and machine-local; recreate with
  `ln -sfn ~/Git/accudisc/build/cli/accudisc tools/accudisc/accudisc` and
  `ln -sfn ~/Git/accudisc/bindings/python tools/accudisc/pybinding`.
  - **Consequence, both directions.** The binding links `libaccudisc.so.0` from that *same*
    build tree, so an A/B across transports (`tools/binding_ab.py`) compares carriers against
    one library build and nothing else — the confound is structurally absent rather than
    controlled for. In exchange, our read path tracks their HEAD live: their rebuild or
    checkout changes what `cdda2img rip` does with no action here. **A measurement run needs
    a quiet build tree**, agreed with them in advance — a rebuild mid-A/B silently compares
    two library versions.
- **`tools/disc_ab.py` / `tools/binding_ab.py` — DELETED 2026-08-01.** Both existed to compare the binding against the CLI. kgr's ruling retired the CLI and, with it, the question: the CLI is built to the same API and cannot do anything the API does not define, so there is nothing to measure, and a disagreement would be an AccuDisc bug rather than a delta to characterise. Their banked result stands — Tracy, req=40, binding 112.69 s vs subprocess 112.75 s, PCM and C2 byte-identical, which is what closed AccuDisc's whole-disc-entry-point item.
- **`tools/write_smoke.py`** — burns through `write_disc` onto a **CDEmu blank**
  (`cdemu create-blank --writer-id=WRITER-TOC --medium-type=cdr74`) and reads it back.
  The round trip is the point: `WriteResult.OK` proves only the return path, since a
  burn that wrote the wrong bytes or the right bytes at the wrong LBA returns `OK` too.
  Payload is a known pattern, never silence (zeros are also what a burn that wrote
  nothing produces). Passes byte-identical; scope is **virtual writer only** — no laser
  timing, no media quality, no real DAO lead-in, and `CAVEATS` is unreachable without a
  CD-Text SIZE_INFO mismatch. A burn on the PX-716A remains the acceptance test.
- **`tools/sink_prescreen.py`** — device-free timing of the de-interleave-and-write loop
  against synthetic chunks at the real geometry. Answered "is the Python sink
  *intrinsically* too slow" (no: 654k sectors/s, 0.55 s per disc, 273× headroom at 32×).
  It cannot see the nonlinear cache-drain failure — that needed `disc_ab.py`.
- **`tools/make_preemph_disc.py`** — generate a `PRE_EMPHASIS` CD-DA test image (cdrdao
  TOC+BIN, pure-Python tone). cdemu-load it (`cdemu load 0 preemph.toc` → /dev/sr1) to
  validate pre-emphasis detection end-to-end through the subchannel/`subq_toc` read path.
- **c2read — RETIRED 2026-07-24.** The C prototype AccuDisc superseded, its Python
  wrapper (`c2_reader.py`), its tests, its experiment suite (`c2read_recovery_test.py`,
  `c2bench.py`, `c2timing.py`, `cx_census.py`, `modepage_experiment.py`) and its plan doc
  are archived in `private/deprecated/c2read-20260724.tar.gz` and deleted from the tree.
  Its 6/6 result on the damaged reference disc is what justified replacing cd-paranoia in
  `_recover_failed_tracks` (2026-07-05); that finding stands, the tool is gone. The two
  tools that shelled out to it — `tools/ctdb_repair.py` and `tools/toc_parity.py` — were
  **retargeted to AccuDisc 2026-07-24** (via `cdda2img.accudisc_reader`), so nothing in
  the tree references `c2read` any more.
- **`tools/measure_write_offset.py`** — burn-and-read-back write offset measurement.
  Generates a synthetic test signal, burns it via `accudisc write`, reads it back via `accudisc read`,
  and measures where the known pulses landed. Accumulates cycles per drive in
  `rips/write_offset_<drive-slug>.toml`. Run from project root:
  ```
  uv run python tools/measure_write_offset.py --device /dev/sr0 --read-offset 30
  ```

- **`install.sh`** (repo root, shipped) — the four install steps plus verification:
  `pipx install`, `pipx inject` of AccuDisc's binding wheel, man page, `file(1)` magic
  rule, then `cdda2img doctor`. The wheel search is the non-obvious part: AccuDisc
  installs it under **its own** prefix (`$PREFIX/share/accudisc/wheel/accudisc-*.whl`)
  because the cffi extension's RUNPATH binds it to one `libaccudisc.so.0` — **the
  directory is the contract, the filename is not**, so it is globbed and version-sorted.
  Three routes: `--accudisc-prefix`, `pkg-config --variable=wheeldir accudisc`, then a
  prefix list. The pkg-config route is tested for a **non-empty answer, not exit 0** —
  measured here, a `.pc` predating the variable returns rc=0 with empty stdout, so
  `if pkg-config …` takes the success branch and yields `""`. A missing wheel warns and
  the install still succeeds (only `rip`/`burn`/`mount` need an engine). `uninstall`
  never touches the user's config — it holds measured drive offsets.
- **`tools/mkdist.sh`** (`make dist`) — **user** distribution tarball into `dist/` (80
  files, 344K — not a developer checkout). **Two independent filters, both load-bearing.**
  (1) `git archive`, never `tar --exclude`: it ships only tracked files, so every
  gitignored private path (`private/`, `.claude/`, `.remember/`, `CLAUDE.local.md`,
  `tools/accudisc/`, `backups/`, `rips/`, `example/`) is excluded **by construction**, and
  a private directory added later is covered with no action. An exclude list is the
  reverse — included unless someone remembered — and fails silently; "no binaries" falls
  out of the same rule. (2) A `DIST_PATHS` pathspec narrows that to what a user needs:
  `pyproject.toml`, `README.md` (also *required* — `readme =` makes hatchling fail without
  it), `LICENSE`, `install.sh`, `src/cdda2img/`, `docs/man/cdda2img.1`. No `tests/`,
  `tools/`, `Makefile`, `uv.lock`, `.github/`, `CLAUDE.md`, or any other `docs/`. Filter 2
  only ever *removes*, so narrowing it cannot weaken filter 1's privacy guarantee — and
  being an allow-list it fails **incomplete and loud** (the install breaks) rather than
  leaky and silent. A pre-flight asserts every `DIST_PATHS` entry exists in HEAD, so a
  moved file is named instead of silently dropped. Refuses a dirty tree (`git archive`
  reads HEAD, so uncommitted work would be absent) and re-scans the finished archive for
  private prefixes, which can only fire if something private was *committed*.

## Commit and push workflow

1. **Run `make check`** — catches everything the pre-commit hook will catch (trailing
   whitespace in all files, unused imports, ruff format, TOML/YAML validity, lock drift,
   ty). Do this *before* writing the commit message; if it fixes anything, the tests
   should be re-run to confirm nothing regressed.
2. **Write `private/COMMIT_MSG`** — the sync script reads this file directly.
3. **Run `uv run python scripts/sync.py`** — runs `ruff format` + `ruff check --fix` as a
   pre-flight, stages all changes, commits using `COMMIT_MSG`, pushes to `origin/main`,
   and runs `backup.py backup` (timestamped tarball in `backups/`).

If the commit still aborts (e.g. a C901 complexity error that ruff cannot auto-fix),
fix the issue manually and re-run `uv run python scripts/sync.py` — the same commit message
remains valid (the stale-message guard only fires when `COMMIT_MSG` matches
`.git/COMMIT_MSG.old`, which is only updated on a *successful* commit).

## Architecture

All source lives under `src/cdda2img/`. The pipeline is fully wired end-to-end.

### Create pipeline (`create` subcommand)
1. `input_selector.py:select_batches()` — groups audio files into CD-sized batches (≤99 tracks, ≤80 min)
2. `transcode.py:transcode_audio()` — converts each track to 16-bit stereo 44.1 kHz PCM WAV via PyAV
3. `silence.py:trim_silence_cd_da()` — `--silence trim` mode only: trims leading/trailing silence (threshold from `Config.silence_threshold`, default −55 dBFS) and appends 2-second inter-track gap
4. `concat.py:concat_wav()` — concatenates per-track WAVs into a single WAV
5. `container.py:wav_to_raw_pcm()` — strips WAV header, leaving raw s16le
6. `metadata.py:derive_album_info()` — extracts album/artist from file tags (mutagen)
7. `metadata_menu.py:run_metadata_menu()` — interactive metadata confirmation; AcoustID + Discogs lookups
8. `toc.py:generate_toc()` — derives track durations and generates cdrdao-format TOC from `RBIDisc`
9. `replaygain.py:analyse()` — optional EBU R128 loudness analysis via pyebur128 (per-track source WAVs, no concat)
10. `container.py:build_container()` — writes the RBI file

### Rip pipeline (`rip` subcommand)
0. `cdda2img.py:_resolve_drive_offset(device, cfg) → (int, int | None, str | None)` — resolves `(read_offset, write_offset, drive_name)` before the rip:
   1. `cfg.drives` (`[[drives]]` TOML entries keyed by normalised sysfs name) — always authoritative; supplies both `read_offset` and optional `write_offset`.
   2. AccurateRip catalog: `drive_info.ensure_drive_offsets(conn)` + `find_drive_offset(conn, name)` — auto-applies at ≥ `_MIN_AR_CONFIDENCE=3` submissions; prompts if lower; no-op without a TTY.
   3. Fallback: `read_offset=0` with a warning when the drive is not configured and no AccurateRip match is applied. There is no global `drive_offset` field on `Config`.
   Confirmed read offsets written via `config.save_drive_read_offset()` (atomic rename); `OSError` swallowed with warning. `drive_name` feeds `PROVENANCE_DRIVE_NAME`/`PROVENANCE_DRIVE_OFFSET` in the container TOC so `list` shows the drive.
1. `cdda2img.py:_rip_disc_stage()` — **one engine, one pass.** A single `accudisc_reader.read_disc_c2()` call captures raw s16le audio + the C2 bitmap + raw P-W subchannel, plus the full-TOC and CD-Text lead-in dumps on the **same `Device`** (one spin-up); real TUI progress comes from the read sink's own chunk callback. `subq_toc.build_rip_info()` then assembles the disc metadata from those captures (rip_type `accudisc`) — pre-gaps/INDEX/CONTROL from the Q stream, majority-voted MCN/ISRC (structurally immune to cdrdao bug #75), track starts from the error-corrected full TOC (Q never moves a boundary). Track 1's pre-gap is derived from the full TOC, not the Q stream: when track 1's INDEX 01 is at LBA > 0 (a program-area pre-gap that the `[0, lead-out)` read captured — e.g. ABBA *Gold*'s 33 frames), those frames are declared as track 1's pre-gap (`start_frame=0`, `pregap_frames=start`); dropping them shifted every boundary and the lead-out down and broke the round-trip disc ID (fixed 2026-07-12). Validated at full field parity against `cdrdao read-toc` (`tools/toc_parity.py`). **There is no second metadata pass and no second engine** — if assembly fails, the rip fails (`subq_toc` already degrades to TOC-only geometry when the Q stream can't be anchored, so reaching that point means the full TOC itself is unusable). `cfg.c2_recovery` **defaults to `auto` (flipped from `off` 2026-07-29, user-authorised 2026-07-05)** and controls exactly one thing: whether the C2 bitmap is *written* to a scratch file. C2 pointers are requested on the wire on every read regardless — the seam sets `ACCUDISC_C2_PTRS` unconditionally — so the sector is 2646 B either way and the read costs the same. `off` therefore bought ~48 MB of scratch and gave up the erasure boost that roughly doubles what ctanalyse can reconstruct. **That argument is about the read; the flip also changes the repair** — with a bitmap present, CTDB recovery now runs **C2-erasure-assisted first, error-only as fallback** by default, where it previously went straight to error-only. Those are genuine alternatives (an over-flagging bitmap can spend erasure budget on clean words and turn a decodable stride undecodable), and both orders are covered by `tests/test_ctdb_repair.py` §D1 — but the default now selects the first. `off` remains honoured as an escape hatch. AccuDisc zero-fills hard-unreadable sectors (PCM zeros + C2 all-ones = pure erasures downstream) and returns **raw** PCM — `apply_offset` runs exactly once, at storage. (AccuDisc is a separate external project (https://github.com/HomerSlated/accudisc) — not shipped here; its Python binding is reached via the git-ignored `tools/accudisc/pybinding` symlink in a checkout, or an injected wheel in an install.)
   - **CTDB recovery (first exit)**: on a partial AR mismatch, `rip_image` first tries `ctdb_repair.repair_whole_disc()` on the raw PCM, committed only if a CTDB per-track-CRC **and** AccurateRip double-gate both pass; **zero extra reads**. When the C2 path captured a bitmap it runs **C2-erasure-assisted first, then error-only as a fallback** — they are genuine alternatives, since an over-flagging bitmap can spend erasure budget on clean words and turn a decodable stride undecodable. Only if all attempts fail (not in CTDB / over RS capacity / a gate rejects it) does the AccuDisc speed-ladder below run — the two are alternative exits in cost order. **Unified offset domain**: AccuDisc reads stay raw through AR + ctanalyse + the recovery ladder (verify at `read_offset`); `apply_offset` runs exactly once, at storage. **CTDB image domain**: CTDB's parity and per-track CRCs cover `[bounds[0], bounds[-1])` — first-track INDEX 01 to lead-out — *not* our `[0, lead-out)` PCM. `laststride` is derived from that image (never from `len(pcm)`) and `ctanalyse` is given `--toc` so it narrows the mapped file to the same window; the two domains coincide only when track 1's INDEX 01 is at LBA 0, and every regression fixture must therefore use `bounds[0] != 0` (`tests/test_ctdb_repair.py`). PROV: `recovery_track_<n>=ctdb_repaired@<entry_id>` on success; `ctdb_declined=<reason>` (+ `ctdb_entry`/`ctdb_offset`/`ctdb_erasures`) when the attempt was made and rejected — a declined repair used to leave no trace at all. Modules: `ctdb_repair.py` (canonical logic, reuses `accuraterip`), `accudisc_reader.py` (AccuDisc subcommand wrappers; `accudisc` is symlinked in `tools/accudisc/`, `ctanalyse` is on `$PATH`). `tools/ctdb_repair.py` is the standalone CLI equivalent.
   - **AR-recovery ladder (rip only, unconditional fallback)**: after the rip, `rip_image` runs `accuraterip.verify_rip`; on a *partial* mismatch (some tracks are in the AR DB but unmatched), `cdda2img._recover_failed_tracks` re-reads each failed track's raw sector window via `accudisc_reader.read_span` across the drive's probed speed ladder (`drive_speed.probe_speed_ladder`, fastest→slowest, `cfg.recovery_passes` sweeps — total attempts = passes × ladder steps), AR-verifying each attempt's offset-corrected slice with `accuraterip.match_track_pcm` against responses fetched once (`fetch_ar_responses`) and splicing the **first match's verified corrected bytes** at `track_start*2352 + read_offset*4` into the still-raw PCM (sample-exact — neighbouring tracks are never perturbed); a track that never matches keeps its original audio (no unverified splice). rip_type gains `+c2rec`. The drive is restored to max **once** after the loop; eject/reset are **not** used (no measured recovery benefit). The sweep across passes × speeds is the recovery mechanism — validated 6/6 on the damaged reference disc (the retired `c2read_recovery_test.py`, archived in `private/deprecated/`). cd-paranoia is gone from the tree entirely — it has no read or recovery role. Outcomes recorded in PROV: `recovery_track_<n>=matched@<speed>X` | `unrecovered`, plus `recovery_passes` / `recovery_ladder`. `recovery_passes=0` disables recovery.
   - **Speed ladder (§9.3)**: `drive_speed.admitted_ladder` derives the ladder per **disc**, not per drive, from `accudisc speeds` rows (`speed req=N page2a=M measured=X.XX`). **Three rules in priority order (2026-07-29).** (1) **Verdict** — admit rows AccuDisc judged `admitted`. Their verdict is a *rate* comparison at three radii, so it sees what `req == page2a` structurally cannot: both of that test's operands derive from the same advertised ceiling, so the equality cross-checks the drive's *quantiser*, never its ceiling. Measured on Tracy with the uncap latched, `req=48 page2a=48 measured=22.96` sits **above** `req=40 page2a=40 measured=23.68` — page 2A advertises the 48× *data* ceiling while CD-DA is governed to 40×, so they are one speed wearing two labels and the faster-*looking* one is slower. The old rule gave `[48, 40, 32, 24, 8, 4]`; the verdict rule gives `[40, 32, 24, 8, 4]`, matching AccuDisc's own `ladder admitted=` line. The gate is "some row was **judged**", not "some row has a verdict": `unknown` is a verdict and it is truthy, so presence-gating would send an all-`unknown` probe (`points=1`, where nothing is judged) to an empty ladder and then to the degrade guard. (2) **`req == page2a`** — the previous rule, for an engine reporting no verdict; still catches outright quantisation. (3) **`measured`** — when *every* row reports `page2a == 0` (the page did not report, which is not the same as "quantised to zero"). Outcome guard: an empty ladder is not a reachable state — degrade to one rung at the drive's max and warn; the guard is on the outcome because the causes are open-ended. Measured evidence that the ladder is drive **×** disc: the PX-716A admitted `[32, 24, 8, 4]` on ABBA *Gold* in July and `[8, 4]` on the same disc on 2026-07-25, its governor having throttled the degraded media. **Never cache a ladder per drive.** The probe leaves the drive at its last rung, so `admitted_ladder` restores it. `probe_speed_ladder` is the legacy CDROM_SELECT_SPEED probe, retained until P5 wires `ResolvedStrategy` in; both returned `[8, 4]` on that disc, so the swap is behaviour-neutral there.
2. CDDB query (`cddb.py:query_cddb()`, TCP, disc TOC fingerprint) runs inside `_finalize_import` via `_run_metadata_lookups`, in parallel with the MB lookup. CDDB is merged at the **absolute lowest precedence** (applied dead last, after MB/Discogs/AcoustID *and* the stage-7 duration matcher; every richer source overwrites it) — its flat "Artist / Title" TTITLE can't separate title from performer cleanly. There is no high-trust CDDB apply helper. The **stage-7 duration matcher** (`mb_lookup.duration_match_lookup`) runs just *above* CDDB (OPT-3 reorder): a last-resort source that fires only when nothing above set an MB release id, text-searching MB by album/artist and picking the candidate whose total duration matches the physical disc (fill-blank, so it supplies only fields no other source did). See `duration_match_release` in PROV. **OPT-3 tradeoff**: stage-7's gate needs an album/artist seed already present, so a disc whose *only* metadata source is CDDB never reaches stage-7 (CDDB is merged after it) — accepted to let the duration match outrank CDDB on every other disc.
3. Shared finalization: `_finalize_import()` (see below)

### Import pipeline (`import` subcommand)
Four source types, each producing s16le PCM, then all call `_finalize_import()`:
- **DDP 2.0** (`ddp_reader.py:import_ddp()`): parses DDPID (MCN), PQDESCR (timing + ISRC), CDTEXT.BIN; PCM (TRACK*.DAT) is already s16le — no byte-swap
- **cdrdao TOC+BIN** (`cdrdao_reader.py`): parses `.toc` text via `toc_parser.py`; byte-swaps s16be BIN → s16le WAV via `convert_cdrdao_bin_to_wav()`
- **Nero NRG** (`nrg_reader.py:import_nrg()`): parses NER5 (64-bit offsets) and NERO (32-bit) DAOX/DAOI track blocks, CDTX (CD-Text), MTYP; PCM is s16le — no byte-swap
- **CloneCD CCD/IMG** (`ccd_reader.py:import_ccd()`): parses the `.ccd` index file; byte-swaps the companion `.img` (s16be → s16le)

### Shared rip/import finalization (`_finalize_import`)
1. `mb_lookup.py:prepopulate_from_mb()` — MusicBrainz disc ID SHA-1 fingerprint lookup; auto-applies single match
2. `metadata_menu.py:run_metadata_menu()` — interactive metadata confirmation; AcoustID (per-track Chromaprint) + Discogs lookups
3. `toc.py:generate_toc()` — generates cdrdao-format TOC with provenance comments
4. `replaygain.py:analyse()` — optional EBU R128 analysis on per-track WAV slices of the raw PCM
5. `container.py:build_container()` — writes the RBI file

### Extract pipeline (`extract` subcommand)
1. `container.py:read_header()` — parses the 40-byte fixed RBI header plus the block directory at end-of-file; returns `RBIHeader` with `find_block(type_id)`
2. `toc_parser.py:parse_toc()` — parses the embedded cdrdao TOC into `ParsedDisc` / `ParsedTrack` dataclasses
3. `container.py:extract_data()` — dispatches to raw and/or track output, plus optional `--rg`, `--ar`, `--log` sidecars. `--raw` writes the embedded TOC (its `FILE` line rewritten to reference the `.wav`) plus an s16le WAV image built by `_write_wav_header` + a verbatim stream-copy of the stored PCM — **no byte-swap** (the RBI already stores s16le, and a `.wav` FILE is read little-endian in cdrdao-TOC semantics; this replaced the old s16be `.bin` that existed only to feed cdrdao's big-endian burn input)
4. `track_extract.py` — slices PCM per track, wraps in WAV, encodes to FLAC via PyAV with Vorbis comment metadata; writes CUE sheet; optionally applies −18 LUFS normalisation

### Key modules
- **`rbi_format.py`** — RBI v6.0 constants (`VERSION_MAJOR = 6`, `VERSION_MINOR = 0`), `HEADER_STRUCT` (40-byte fixed header), `DIR_ENTRY_STRUCT` (54-byte directory entry), block type IDs (`BLOCK_TYPE_TOC`/`PROV`/`RGDB`/`ARIP`/`RLOG`/`PCM`), `RBIHeader` / `RBIDirEntry` / `RBIDisc` / `RBITocEntry` / `RBIReplayGain` dataclasses, `frames_from_timestamp()`, `timestamp_from_frames()`. `RBIDisc` carries `pre_emphasis: bool | None` (R14 aggregate disc-level flag — None means not captured), `discogs_release_id: int | None`, and the v6.0 catalogue fields: `cdtext_catalog_ref: str | None` (renamed from `disc_id` at v6.0 — clean break, no read shim — to disambiguate the CD-Text DISC_ID label string from the MCN and the label's catalogue number), `catalog_number: str | None` (the label's own alphanumeric number, e.g. `"CID U2 6"`), `label: str | None`, and `country: str | None` (ISO-3166 alpha-2, or MB pseudo-code XE/XW). `catalog: str | None` is the **on-disc MCN** (Q-ch Mode 2) — archival only, never a lookup/disambiguation key; it is the TOC `CATALOG` line and may be synthesised from `barcode` at finalisation when the disc carries no MCN (`mcn_source=barcode_derived`). `barcode: str | None` is the **service UPC/EAN** disambiguation key (MB/Discogs), routed through the resolver as `Field.BARCODE` and persisted to **PROV only** (no format bump). The two are different identifiers in different namespaces — never cross-compared (identifier_trust_model.md §1a).
- **`cdda2img.py`** — CLI entry point; `create_image()`, `import_image()`, `rip_image()`, `extract_image()` top-level functions. PROV-side helpers: `_r6_acoustid_corroborate` (R6 pre-menu fingerprint, tracks 1 and ceil(N/2)), `_emit_r9_disagreement` (NFC + casefold + reissue-suffix allow-list strip), `_r11_corroborate_with_discogs_master` (prefer-the-earlier on disagreement), `_r12_status` (`OK`/`empty`/`down`/`disabled` mapping), `_emit_mb_provenance` (writes `multi_match_isrc_disambiguated`, `release_selected_via`, `preferred_country_applied`, `mb_rejected_inconsistent`). §10 helpers: `_acoustid_gate` (§10.4 — writes `acoustid_gate=failed` on a genuine post-selection AcoustID miss; fail-only key), `_gate_adjusted_auto` (suppresses `--auto` for that disc when the gate failed, so the result is reviewed not auto-committed), `_discogs_barcode_corroborate` (§10.3.1 — follows the selected MB release's Discogs link via `mb_lookup.discogs_link_and_barcode` and compares barcodes, emitting `discogs_barcode_corroborates=YES` or `discogs_barcode_conflict=mb:<bc>|discogs:<bc>`).
- **`cli.py` / `depcheck.py`** — the console-script entry point and the dependency pre-flight behind it. **Both are standard-library only, and that is a correctness constraint rather than tidiness**: `import cdda2img.cdda2img` eagerly pulls in `av`, `mutagen`, `numpy`, `ortools` and `unidecode`, so a check reached *through* that import raises `ImportError` before it can report anything — able to diagnose every dependency except the ones actually missing. `cli.main()` therefore dispatches `doctor` and runs `depcheck.preflight_or_exit()` **above** `from cdda2img.cdda2img import main`, and `doctor` is deliberately **not** an argparse subcommand (argparse lives on the far side of the import it exists to survive). `preflight_or_exit` names *every* missing dependency in one run and exits 1 — Python's own `ImportError` names one module per run, so a user three short learns it three times; it costs ~0.7 ms (a `find_spec` each, no imports) against a ~340 ms application import. Presence requires `spec.origin is not None`: a PEP 420 namespace package (an empty directory on `sys.path` — the `tools/accudisc/` phantom) resolves without error and must not count as installed. `RUNTIME_PYTHON` carries the distribution→import mapping that nothing can derive (`pyacoustid`→`acoustid`, `discogs-client`→`discogs_client`; `packages_distributions()` only covers packages already installed, which is the case that does not need checking), kept from drifting by `tests/test_depcheck.py`, which diffs it against `[project.dependencies]` and reads `depcheck.py`'s own AST to prove the stdlib-only rule — an ordinary import test cannot, because the test environment has everything installed. The AccuDisc group is a **single required item** since the CLI retirement (2026-08-01) — the binding, and nothing else; the `accudisc` binary is deliberately *not* reported at all, because AccuDisc's own install puts it on `$PATH` and a line saying `ok` about an artefact this application never executes is the same defect the binary line was added to fix, inverted. A binding reachable only via the git-ignored `tools/accudisc/pybinding` shim reports `WARN`, never `ok`: it works on this machine and nowhere else, the false pass that left the binding transport inert for two days. A **Package data** group checks the shipped recovery profiles resolve and include the built-in `track-ladder` — added 2026-07-31 because `doctor` reported *21 ok, 0 warnings* on a pipx install where `rip` aborted instantly: every dependency was present, the package's own data files were not, and a checker that only inspects dependencies certifies an application that cannot start. It duplicates `recovery_profile.shipped_profiles_dir()` for the stdlib-only reason above, pinned by `test_profile_location_matches_the_loader`.
- **`container.py`** — `build_container()`, `read_header()`, `extract_data()`, `wav_to_raw_pcm()`, `resolve_temp_dir()`, `TempFiles`. `TempFiles(base)` allocates a **unique `mkdtemp` subdirectory** per invocation (create/import/rip) — every scratch fragment (`.pcm`, pre/norm WAVs, per-track temps, and the AccuDisc `.cdtext`/`.sub`/`.fulltoc`/`.c2` sidecars) lives inside it, and `cleanup()` is a single `rmtree`. This isolation is a correctness invariant, not just tidiness: with the former fixed `all_tracks.*` names in a shared `/var/tmp`, a no-CD-Text disc silently read the *previous* rip's leftover `all_tracks.cdtext` and baked in the wrong album (paired with the `subq_toc` binding guard as belt-and-braces).
- **`input_selector.py`** — four batching strategies: `fcfs`, `aatc`, `best` (OR-Tools CP-SAT global bin-packing), `meta` (groups by embedded disc-number tag)
- **`accudisc_reader.py`** — **the** disc path (there is no other), and **the seam**: every AccuDisc call in `src/` lives here, guarded by a test (`test_no_module_outside_the_seam_imports_accudisc`). Wraps the external **AccuDisc** engine (https://github.com/HomerSlated/accudisc; `read_disc_c2`/`read_span`/`read_span_bytes`/`read_toc`/`read_lead_in`/`drive_supports_c2`/`probe_combos`/`park_spindle`/`write_disc`/`read_speed`/`speed_ladder_rows`/`engine_version`/`eject`) through its **Python binding only** — `import accudisc`, a cffi API-mode extension over `libaccudisc`.
  - **One transport, since 2026-08-01 (kgr's ruling).** The `accudisc` CLI is no longer invoked anywhere in `src/` or `tests/`; the subprocess arm, `CDDA2IMG_ACCUDISC_TRANSPORT`, `active_transport()`, `_try_binding`, `parse_toc_output`, all six stdout regexes and both A/B tools (`tools/binding_ab.py`, `tools/disc_ab.py`) are **deleted**. The reasoning is theirs and is stronger than "the binding won the benchmark" (it did: Tracy req=40, 112.69 s vs 112.75 s, byte-identical): **the CLI is built to the same API and cannot perform an operation the API does not define, so an A/B has nothing to measure — and if the two ever disagree, that is an AccuDisc bug rather than a delta to characterise.** Testing only through the API surfaces a shortfall as something we cannot do, instead of a discrepancy to reconcile against a CLI that would by then be broken. Consequences: a missing/unimportable binding is now **fatal**, and so is an **ABI mismatch** (it used to degrade — the extension is broken while the binary is fine — and that reasoning still holds; what changed is that "the binary is fine" no longer helps). `_binding(what)` raises naming the operation; `_call` translates `AbiMismatch` (remedy: rebuild) and `AccuDiscError` (a real device/media failure) into `RuntimeError`, kept as separate arms because the remedies differ.
  - **Two silent-difference traps found during the migration, both would have type-checked and run.** (1) `Device.get_speed()` returns `(max_kbps, current_kbps)`; `read_speed()` returns `(current, max)` — same types, same units, so a straight hand-over silently swaps every caller's reading of the drive. Verified on hardware with the fields deliberately made to differ (drive at 8x: `(1411, 7056)` on both carriers); at a drive's default they are equal and the order is unobservable. `0` maps to `None` — an unsigned zero means "did not report", not "0 kB/s". (2) `Features.combos` keys are `c2_sub_raw`/`c2_sub_q`; this module publishes `c2+sub_raw`/`c2+sub_q`, and `c2+sub_raw` is the key gating the single-pass capture. Absorbed by `_COMBO_KEY_ALIASES`. Also `C2Verdict.UNVERIFIED` maps to **False** — "could not tell", not a weaker yes. `_log_read_caveats` is now more load-bearing, not less: exit 3 was a CLI projection (`(hard_errors || sectors_suspect || sectors_flagged) ? 3 : 0`) that `Device.read` does not return, so this side rebuilding it is the **only** source of the caveat signal. `read_to_file` is deliberately not used (no progress callback — the seam de-interleaves itself in `_split_streams`); nor are `Device.read_span`/`read_to_file` for `read_span`, for the same reason.
  - **Ladder policy is NOT part of the carrier work (open item).** The binding exposes `verdict`, `min_x`/`max_x` and `Device.admitted_ladder()`, and adopting AccuDisc's admission rule would fix the known gap in `drive_speed.admitted_ladder`. Live evidence, Tracy: `req=48 page2a=48 measured=22.96` sits above `req=40 page2a=40 measured=23.68` — 48 is **slower** than 40 — yet our `req == page2a` test admits both. AccuDisc's verdict calls it `duplicate:40`. Changing this changes what the recovery ladder does, so it is a policy change with its own evidence. `min_x`/`max_x` are `None`, not `0.0`, when no gradient was measured: flattening that would make "not measured" indistinguishable from "this rung stalled".
  - **How the binding is found.** `tools/accudisc/pybinding` (git-ignored) is a symlink at AccuDisc's `bindings/python`, resolved by `_binding_search_path()` and **appended** to `sys.path` — appended, never prepended, so a properly installed `accudisc` wins and the shim retires itself silently. It cannot be a declared dependency: their `build_accudisc.py` sets an RPATH into their build tree, making the artefact correct in exactly one directory. `cffi` is a *runtime* dependency of an API-mode extension (a module that builds cleanly still dies at `from ._accudisc import ffi, lib`) and is declared in the dev group. **`_import_binding` proves identity before trusting the import**: with `tools/` on `sys.path`, `import accudisc` *succeeds* and binds `tools/accudisc/` as an empty PEP 420 namespace package, raising no `ImportError`. `doctor` reports a shim-only binding as **WARN, never ok** — it works here and nowhere else, the false pass that left the binding inert for two days.
- **`cddb.py`** — CDDB disc ID computation, TCP query (`query_cddb()` returns **all** TOC-matched candidates); results merged at lowest precedence by `cdda2img._run_metadata_lookups` (no high-trust apply helper). `consensus_from_candidates()` collapses the candidate list to **one** consensus `DiscMeta` (via `cdda2img._cddb_consensus`) instead of trusting the arbitrary first entry: **Stage 1** heuristic prune — null any implausible `release_date` (year outside `_CD_ERA_MIN_YEAR=1982`..next-year — kills the ABBA `DYEAR=1974` pre-CD-era class; the recording year is not the CD release date) and drop degenerate (no album + no titles) candidates; **Stage 2** per-field plurality vote (`_vote`, `strip().casefold()` normalisation, lexicographic tie-break for a total deterministic order — no interactive picker) across survivors for every string disc-field and each track's title/performer. Sparse-but-valid entries are kept (an empty field casts no vote). PROV: `cddb_candidates`, `cddb_years_pruned`.
- **`cdrdao_reader.py`** — cdrdao TOC+BIN import; s16be → s16le conversion
- **`ddp_reader.py`** — DDP 2.0 (GEAR Pro Mastering Edition) import
- **`toc.py`** — `generate_toc()` (emits per-track `COPY`/`PRE_EMPHASIS` flags and `INDEX` ≥ 02 lines from `RBITocEntry` — rbi_spec §6.1.10), `sanitize_title()`, `build_toc_entries()`
- **`subchannel.py`** — raw P-W subcode decoder (redumper `.subcode` / `accudisc read --sub raw`, byte-identical): CRC-16/GSM, `ChannelQ` (ADR 1/2/3 = position/MCN/ISRC, `index`), `scan_subcode()` with per-value majority voting (`RegionDatum.votes`/`runner_up`, ≥2-observation floor), `derive_track_layout()` (pre-gaps from index-00 spans, INDEX ≥ 02 points, CONTROL majority; >2-sector position-slip defence), `parse_fulltoc()` + `session1_audio_tracks()` (session-1-only policy; Enhanced-CD data excluded; mixed-mode refused)
- **`cdtext.py`** — CD-Text pack decoder for raw READ TOC format-0x05 dumps: 18/16-byte stride detection, per-pack CRC (reuses `crc16_gsm`), NUL-separated string reassembly, TAB shorthand, SIZE_INFO; Latin-1/ASCII block 0 only (MS-JIS skipped). Strings decode **UTF-8-first with Latin-1 fallback** (`_decode_text`): cdrdao-authored discs and CDEmu-mounted images carry raw UTF-8 despite declaring charset 0x00 — decoding per spec baked mojibake into titles (fixed 2026-07-05; real capture fixture `tests/fixtures/cdemu_utf8.cdtext`)
- **`subq_toc.py`** — `build_rip_info(fulltoc_raw, sub_data, cdtext_raw)` — the F7 join point: assembles a `RipInfo` (with `prov` keys `toc_source=subq@accudisc`, `subq_frames`, `isrc_vote_track_<n>`) from one AccuDisc pass; TOC authoritative for boundaries, Q supplies pre-gaps/flags/MCN/ISRC, CD-Text supplies titles; degrades to TOC-only geometry when the Q stream can't be anchored. **CD-Text↔disc binding guard** (`_cdtext_matches_disc`): a CD-Text block whose SIZE_INFO track range (or, absent SIZE_INFO, observed titled-track range) does not match the disc's actual first/last track is discarded (`prov` key `cdtext_rejected=track_range_mismatch`) — defence against a stale sidecar or drive-cached lead-in from the previously-loaded disc baking a wrong album in. Prefer no CD-Text over wrong CD-Text.
- **`mb_lookup.py`** — MusicBrainz disc ID + release lookup. `MBPrepopResult` carries `barcode_hints: list[tuple[str, str]]` (R16: `(mbid, barcode)` per match) and `release_selected_via: str | None` (which §10.3 rung picked the release — surfaced in PROV); `_score_candidate_by_isrcs` + `_disambiguate_by_isrcs` resolve multi-match (R1) with `_MIN_ISRC_AGREE=2` floor and strict-uniqueness tie semantics; `_resolve_via_isrc_tally` is the zero-disc-ID-match fallback (R4: ≥ ceil(N/2) ISRC convergence required). `_select_release_lexicographic` is the §10.3 deterministic release-selection ladder for disc-ID multi-match over the album's plurality release-group, returning `(winner, via)`. Lexicographic key chain: `barcode_plurality` (most common normalised barcode) → `preferred_country` (config ranking, a priority not a filter) → `date` (earliest `release_date`) → `mbid` (terminal deterministic tiebreak). `_plurality_release_group` establishes the album's RG; on an **even RG split** (`None`) `_plurality_release_group_by_barcode` is the fallback — a unique-plurality barcode (≥2 releases, one RG) pins the RG so the ladder can run (the ABBA *Gold* vs *Forever Gold* TOC-collision case; fires only on positive barcode evidence, else still declines). `disc_id_from_rbi` computes the MB disc-ID lead-out from the **last track's absolute end** (`start_frame+pregap+duration+150`), **not** `disc.total_frames+150` — the latter omits any track-1 head offset (`start_frame>0`) and yields a wrong SHA-1 → spurious 404. The on-disc MCN is **not** a selection key (§1a — archival only); pressing selection rests on the candidates' own service barcodes (same-namespace). `via` names the highest-priority key on which the candidates actually vary — the rung that decided the winner — and is one of those four strings. `discogs_link_and_barcode(release_id)` follows a release's MB→Discogs url-relation and returns `(discogs_id, mb_barcode)` for the §10.3.1 cross-source barcode check. MB rate limit pinned at 1 req/s via `set_rate_limit` in `_setup_useragent` (R15).
- **`acoustid_lookup.py`** — AcoustID / Chromaprint per-track fingerprint lookup
- **`discogs_lookup.py`** — Discogs label, catalogue number, country lookup. `normalize_barcode` enforces the GS1 §1.3.1 check digit (R13); `lookup_master_year(release_id)` walks `release.master.main_release.year` for R11 corroboration.
- **`lookup_result.py`** — `DiscMeta` / `TrackMeta` shared result dataclasses
- **`validation.py`** — the two-stage declarative validator (accudisc-migration-plan.md §9.5). One generic engine (`validate_spec` structural → `validate_sanity` semantic → `validate` short-circuiting both) over per-consumer tables: `PROFILE_SCHEMA` (§9.2) and `CONFIG_SCHEMA` (§9.6), plus the nested `DRIVE_SCHEMA` for `[[drives]]`. The stages are distinct because a value can be well-formed and still illegal — AccuDisc's `--retries 256` parses as a fine integer, then its unguarded `uint8_t` cast turns it into 0→2, the opposite of what was asked. Unknown keys are **errors**; `bool` is refused where an `int` belongs (Python's bool-is-an-int wart would wave `passes = true` through as 1) while a genuine `int` IS accepted for a `float`; sequence defaults are declared as tuples and handed out as fresh lists so no caller can mutate the schema. `CONFIG_SCHEMA` is **not yet the loader's authority** — that is phase P3.
- **`recovery_profile.py`** — recovery profiles: `Profile` (§9.2 shape), `load_profile` / `list_profiles` (user dir shadows shipped `cdda2img/profiles/*.toml`; filename and `name` field must agree, or PROV would name a strategy that did not run), `resolve_recovery` (§9.4's four rungs — `--ad-*` flags **exclusively**, else `--profile`, else `cfg.default_profile`, else built-in `track-ladder`), `rungs_for` / `bind_ladder` (ladder policy against the drive's admitted rungs). `ResolvedStrategy` is the only object the rip path sees and records which rung produced it (`recovery_source` in PROV). Rung 1 does not merge: blending an escape hatch with a profile yields a configuration nobody asked for. Rung 4 is a profile, not bare flags — bare flags are AccuDisc's R0 (`--retries` defaults to 2), a real floor but well below `track-ladder`'s measured 19/20. An invalid or unknown profile **raises**; it is never silently replaced by a default, because every measurement taken afterwards would be mislabelled.
- **`src/cdda2img/profiles/*.toml`** — the seven shipped profiles, one per bench arm: `track-ladder` (default, 19/20), `track-constant` (14/19), `max-variation` (13/20, fast at low n and 0/9 at high n), `whole-disc` (11/20), and the experimental `sector-runup` / `sector-hammer` / `span-fixed` (2/20, 2/20, 1/16). The experimental three are kept as **controls**, not candidates: `sector-hammer` anchors the zero point of the variation axis and `span-fixed` is what separates span size from variation as the active ingredient.
- **`validators.py`** — shared ISRC ISO-3901 regex + GS1 §1.3.1 GTIN-13 check-digit validators (R13). `validate_isrc` is the ISRC chokepoint (silent-drop + WARNING log on malformed input); `is_valid_gtin13` is wrapped by `barcode.normalize_barcode`.
- **Lookup caching** — the persistent R7 SQLite cache (`lookup_cache.py` / `lookup_cache.db`, 30-day TTL) was **removed** (superseded — caching wrong results for 30 days with no invalidation was the wrong trade-off). Replaced by process-lifetime in-process dicts with no TTL, discarded on process exit: `mb_lookup._DISC_ID_CACHE` (OPT-1, MB disc-ID lookups; `.clear()` to reset) and `album_art._COVER_CACHE` (OPT-2, cover-art bytes keyed on `CoverArt.source`). These de-duplicate the Phase-1 banner vs Phase-2 finalization calls within a single invocation only.
- **`metadata.py`** — `derive_album_info()` from file tags via mutagen
- **`metadata_menu.py`** — interactive metadata confirmation menu
- **`replaygain.py`** — EBU R128 analysis via pyebur128; `analyse()`, `pack_rg_block()`
- **`config.py`** — `Config` dataclass (`cddb_server`, `contact_email`, `database_backups`, `database_backup_frequency`, `catalogue_backups`, `catalogue_backup_frequency`, `drives`, `catalogue_path`, `enable_catalogue`, `default_device`, `silence_threshold`, `capacity`, `preview`, `tui`, `low_dr_threshold`, `auto`, `embedart`, `recovery_passes`, `c2_recovery`, `preferred_country`, `default_profile`) + `DriveConfig` (per-drive `name`/`read_offset`/optional `write_offset`); `load_config(strict=True)` (§9.6 — validates against `validation.CONFIG_SCHEMA` and raises `ConfigError` listing **every** failure; `strict=False` warns, drops the offending keys and continues, and exists for `setup` alone, which must be able to open a broken config in order to repair it. `main()` validates once early and every subcommand except `setup` exits non-zero on a bad config, so a mistake surfaces before a drive is touched rather than mid-rip. The old per-field "parse, warn, substitute a default" ladder — `_bounded_int`, `_parse_dup_policy`, `_parse_c2_recovery` — is **deleted**: it meant a mistyped `recovery_passes` produced a rip that quietly did something else. `_build()` now does normalisation only; anything that can reject a value lives in the schema so all rejections are reported together), `save_drive()`, `save_drive_read_offset()`, `save_drive_write_offset()`, `_rewrite_config_drives()`, `_parse_preferred_country()` (parses the `preferred_country` TOML array — ISO-3166 alpha-2 + MB pseudo-codes XE/XW — into an ordered list); `[[drives]]` TOML array-of-tables round-trip; XDG path via `config_path()`. There is no global offline-mode flag; network gating is per-module via each lookup's `is_available()` (token / `fpcalc` presence).
- **`original_release.py`** — MusicBrainz release-group based lookup of the earliest known release of the same logical album. `find_original_release(disc)` returns `(found, title, year)`; `populate_original_release(disc)` assigns the result onto `RBIDisc` and skips when the user has already set the field manually via the metadata menu. Derivative secondary types (Compilation, Live, Remix, DJ-mix, Demo, etc.) are rejected — they're not "originals" in the sense the field captures. R3 verifier `_verify_release_matches_disc` gates both paths via conjunctive track-count + sum-of-durations (±2 s) + ISRC overlap + aggregate title fuzzy ≥ 80; each gate skips on missing evidence so empty-tracklist meta passes vacuously. R14 caps fuzzy candidates at year ≤ 1986 when `disc.pre_emphasis is True`.
- **`toc_parser.py`** — parses cdrdao TOC text into `ParsedDisc` / `ParsedTrack`. `ParsedDisc.pre_emphasis` aggregates per-track `PRE_EMPHASIS` flags (R14: `NO PRE_EMPHASIS` is treated as the negation, handled separately to avoid false-match). `ParsedTrack` also carries per-track `pre_emphasis` / `copy_permitted` / `index_points` (INDEX ≥ 02 offsets relative to the audio start — rbi_spec §6.1.10), round-tripped symmetrically by `generate_toc`.
- **`db.py`** — SQLite management for `drive_offsets.db`; `open_drive_offsets_db()`, `ensure_backup()`, `parse_frequency()`; WAL + foreign_keys; schema: `ar_drives`, `fetch_log`, `fetch_state`
- **`drive_info.py`** — sysfs drive name probe (`probe_drive_name`); AccurateRip `driveoffsets.htm` catalog (`ensure_drive_offsets` with 30-day cooldown, `find_drive_offset`); `_normalize_ar_name` handles `"VENDOR  - MODEL"` and `"- MODEL"` formats via two-pattern regex
- **`transcode.py`** — PyAV audio transcoding to Red Book PCM WAV
- **`silence.py`** — silence trimming and gap padding
- **`concat.py`** — WAV concatenation via the `wave` module
- **`track_extract.py`** — per-track FLAC extraction + CUE sheet writer
- **`audition.py`** — ffplay subprocess wrapper for interactive audition (pause/resume via SIGSTOP/SIGCONT)
- **`track_preview.py`** — cosmetic track-1 audio preview for the `rip` pipeline: grabs track 1 via `accudisc_reader.read_span` (through the seam — cd-paranoia has no role anywhere in `src/`), loops it via ffplay in the background during the rip; best-effort (never fails a rip)

## RBI Format (v6.0)

40-byte fixed header: magic `RBIMAGE\x00`, version `6.0`, flags (uint32), track count (uint8), disc number/total (uint8/uint8), PCM parameters (sample rate uint32, channels uint8, bit depth uint8), block-directory offset (uint64), directory entry count (uint16), reserved bytes. The block directory is appended at end-of-file: each entry is 54 bytes (type ID, flags, offset, length, BLAKE3). v6.0 is a clean break from v5.0: `RBIDisc.disc_id` was renamed to `cdtext_catalog_ref` and the catalogue fields `catalog_number` / `label` / `country` were added; there is no read shim for v5.0 files.

Variable-length blocks (TOC and PCM are mandatory; the rest are optional and signalled by directory presence):

| Block | Contents |
|-------|----------|
| TOC | cdrdao-format text TOC; per-track pre-gap, ISRC, CATALOG (MCN), provenance comments |
| PROV | Provenance key=value text: creator, mode, source, ripper, drive; lookup-status / disagreement / corroboration surfaces (R9/R11/R12); `arip_transport` + `arip_dbar_b3sum` (R2); `pre_emphasis` (R14); `multi_match_isrc_disambiguated` (R1); `acoustid_corroborates` (R6); `discogs_release_id`; `duration_match_release` (stage 7); v6.0 catalogue + §10 keys: `catalog_number`/`label`/`country`, `barcode` + `mcn_source` (§1a — service UPC/EAN disambiguation key, PROV-only; and whether the TOC `CATALOG`/MCN was read from disc or `barcode_derived`), `release_selected_via` + `preferred_country_applied` (§10.2/10.3), `acoustid_gate=failed` (§10.4), `discogs_barcode_corroborates`/`discogs_barcode_conflict` (§10.3.1); `cdtext_rejected=track_range_mismatch` (subq_toc CD-Text↔disc binding guard); `cddb_candidates`/`cddb_years_pruned` (CDDB consensus reducer); `ctdb_declined`/`ctdb_entry`/`ctdb_offset`/`ctdb_erasures` (CTDB parity-repair attempt outcome) |
| RGDB | 17 + 12×N bytes: per-track and album EBU R128 gain, peak, and LRA (float32) |
| ARIP | 13 + 15×N bytes: per-track AccurateRip v1/v2 CRCs, confidence, status, disc IDs |
| RLOG | Structured rip log: drive, engine, offsets, per-track AR results; BLAKE3 self-seal |
| PCM | Raw s16le — no WAV wrapper; parameters stored in fixed header |

Each block carries its BLAKE3 digest in the directory entry (SHA-256 in v4.x). `BLOCK_FLAG_SKIP` signals blocks safe to ignore for forwards compatibility. Pre-gap audio is stored contiguously in the PCM block; the TOC records the pre-gap duration separately so extraction skips it cleanly.

Full specification: `docs/reference/rbi_spec.md`.

## Key Constraints

- Red Book limits: ≤99 tracks, ≤80 minutes per disc (`MAX_RUNTIME_MINUTES`, `MAX_TRACKS` in `input_selector.py`)
- Duration arithmetic uses integer scaling (`SCALE = 100`) to avoid floating-point bin-packing errors
- OR-Tools CP-SAT (`best` strategy) has no type stubs — all method calls carry `# type: ignore[attr-defined]`
- `ty` (not mypy) is the type checker; configured via `[tool.ty.environment]` in `pyproject.toml`
- Ruff line length is 88 (the `ruff format` target); `E501` is ignored. `S101` (assert) is allowed in tests
- Long exception messages use the `msg = ...; raise Err(msg)` pattern (TRY003)
- Tests use `example/` directory audio files as fixtures. These are **not** committed — `example/` is gitignored (licensing), so `git ls-files example/` is empty and the directory is machine-local. The tests that need it skip without it. (This line said "committed to repo" until 2026-07-31; it was wrong, and the correction matters because `tools/mkdist.sh` relies on tracked-set membership to keep non-redistributable audio out of the tarball.)
- **Byte-order invariants**: GEAR Pro DDP TRACK*.DAT is s16le — no byte-swap on import; cdrdao BIN output is s16be — always byte-swap via `convert_cdrdao_bin()` (import) or `convert_cdrdao_bin_to_wav()` (RG analysis); cd-paranoia outputs WAV (s16le) — no byte-swap for ripped data
- **Normalize vs ReplayGain**: `--normalize` is extract-time only (mutually exclusive with RG tag embedding); `--loudness rg` at create/rip/import time measures EBU R128 and stores the result in the RBI container without modifying the PCM
- **Subprocess**: **no AccuDisc binary is spawned anywhere** — `accudisc_reader.py` is the seam and it calls the Python API only (2026-08-01; no cdrdao or cd-paranoia binary either). `drive_speed.py`, `disc_writer.py`, `rip_log.py` and `write_offset.py` all delegate to the seam. `ctdb_repair.py` spawns `ctanalyse` (on `$PATH`, not AccuDisc); `audition.py` and `track_preview.py` spawn `ffplay`; `album_art.py`, `cdemu.py` and `setup.py` spawn their own tools. Intentional subprocess calls carry `# noqa: S603, S607` (see LINT-008, LINT-012, LINT-013, LINT-017)
- **Version** lives in `pyproject.toml` only; `container.py` and `cdda2img.py` read it via `importlib.metadata`
- **spec-before-code**: update `docs/reference/rbi_spec.md` before changing the container format
- **AccurateRip transport (R2)**: HTTPS is preferred; HTTP fallback only on `URLError` / `OSError`. A 404 over HTTPS is a legitimate negative ("disc not in DB") and does *not* fall back to HTTP. Responses are capped at `_AR_DBAR_MAX = 1 MB`. Per-block `(id1, id2, cddb_id)` header verification drops mismatching blocks at WARNING level — protects against a poisoned plaintext response splicing unrelated discs' blocks. `verify_rip` returns `ARVerifyResult(tracks, transport, dbar_b3sum)`.
- **MCN / ISRC validation (R13)**: MCN check digit (GS1 §1.3.1 Modulo-10) is enforced inside `barcode.normalize_barcode`; invalid inputs return None + log DEBUG (silent-drop pattern — routine when scanning third-party metadata, must not surface in normal rips). ISRCs from MB pass through `validators.validate_isrc` at ingress (`_parse_release`) and again at the merge sites (`_merge_into_disc`, `_overwrite_disc`); malformed values are dropped, not propagated.
- **Original-release narrowing (R3)**: prefer no-answer over wrong-answer. A track-count mismatch against the disc's own MB release is positive evidence of upstream RG misidentification and falls through to fuzzy. Network failure during the verify is not evidence of mismatch — the answer stands. `_MIN_ISRC_AGREE=2` floor and strict-uniqueness tie semantics for the R1 disambiguator.
- **MB rate limit (R15)**: pinned to 1 req/s in `_setup_useragent`. Don't silently inherit a future library default change.
- **Lookup caching (OPT-1/OPT-2)**: caching is process-lifetime only (`mb_lookup._DISC_ID_CACHE`, `album_art._COVER_CACHE`) — no persistence, no TTL, discarded on process exit. The former persistent R7 SQLite cache was removed; there is no longer a 30-day-TTL on-disk metadata cache.
- **Network gating**: there is no global offline-mode flag. Each lookup module gates itself via its own `is_available()` (Discogs needs `DISCOGS_TOKEN`; AcoustID needs `fpcalc` on PATH + an AcoustID key; etc.). With caching now process-lifetime only, there is no way to reproduce a prior rip's network metadata offline across separate invocations.
- **Deferred work**: tracked in `docs/reference/TODO.md` under the `## Open`
  section at the top of the file. The current live item is OPT-4 (per-field
  trust-score model, unified with the structural C1/C2 defect classes in
  `docs/reference/trust_model_design.md`).

## Reference Material

Public documentation and research in `docs/`:
- `docs/reference/rbi_spec.md` — full RBI container format specification
- `docs/reference/RECOVERY.md` — the failed-read recovery toolkit: component inventory (roles, status, dependencies, conflicts, combinations), c2read user guide, and the developer deep-dive (science, evidence, design philosophy, adoption/rejection history). Living doc — update alongside recovery-strategy changes
- `docs/reference/reference.toc` — annotated cdrdao TOC grammar reference
- `docs/reference/TUI_Design.md` — TUI design notes
- `docs/man/cdda2img.1` — man page (install: `doas install -m 644 docs/man/cdda2img.1 /usr/local/share/man/man1/`)
- `docs/research/ABHOOD.md` — AB/HD ripping and offset research
- `docs/research/NONSPEC.md` — non-spec / real-world disc behaviour notes
- `docs/research/OFE.md` — offset/framing error notes
- `docs/research/REPLAYGAIN.md` — ReplayGain / EBU R128 research
- `docs/research/RIP-ENGINE-BENCHMARK.md` — cdrdao vs cd-paranoia rip-speed / paranoia-level benchmark (clean disc)
- `docs/research/IEC_60908-1999.pdf.txt` — link to IEC web store for purchasing the Red Book standard
- `docs/research/Redump-Optical_Disc_Drives_CD_Compatibility_Technical_Details.txt` — Redump drive compatibility data
- `docs/research/spoons-audio-guide-cd-ripping.txt` — Spoons' audio CD ripping guide

Additional machine-local references (not committed) are documented in `CLAUDE.local.md`.

CD-DA subchannel, CD-Text, sample-offset, and AccurateRip verification-algorithm reference
(Q-channel modes, CD-Text PTI table, offset arithmetic, AccurateRip checksum/disc-ID/dBAR
format, cdrdao TOC field grammar) has moved to the `cd-da-domain-reference` skill
(`.claude/skills/cd-da-domain-reference/SKILL.md`) — invoke it when working on subchannel
decoding, offset correction, AccurateRip verification, or CD-Text/TOC parsing code.
