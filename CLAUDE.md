# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`cdda2img` is a CLI tool for creating, importing, ripping, extracting, and verifying **RBI (Red Book Image)** archive containers of CD-DA audio discs. Subcommands:

- **`rip`** — rip a physical disc via cdrdao (primary) with cd-paranoia fallback
- **`create`** — create one or more RBIs from a directory of audio files
- **`import`** — import a foreign disc image (cdrdao TOC+BIN, DDP 2.0 / GEAR Pro, Nero NRG, or CloneCD CCD/IMG)
- **`extract`** — extract to per-track FLAC + CUE, or raw PCM + TOC, or both
- **`list`** — list container sections and track index with offsets and checksums
- **`test`** — verify all block checksums and structural invariants (27 checks, exits 1 on failure)
- **`burn`** — burn an RBI back to a blank CD-DA disc via cdrdao
- **`mount`** — extract a TOC+BIN scratch copy and load it into a cdemu virtual slot
- **`catalogue`** — browse the local disc catalogue (summary, search, per-disc detail)

The `rip`, `import`, and `create` pipelines all embed cdrdao-format TOC text, optional EBU R128 ReplayGain, and raw s16le PCM in a single RBI container. Metadata (album, artist, track titles, ISRC, CATALOG, low-dynamic-range flag, original-release lookup) is sourced from CDDB, MusicBrainz, AcoustID, and Discogs lookups and an interactive confirmation menu.

This is a prototype; a Rust reimplementation is planned once the design has stabilised.

## Commands

```bash
# Install dependencies
uv sync

# Rip a physical disc (cdrdao primary; cd-paranoia fallback)
uv run python -m cdda2img rip
uv run python -m cdda2img rip --device /dev/sr0
uv run python -m cdda2img rip --loudness none

# Create an RBI image from a directory of audio files
uv run python -m cdda2img create <input_dir>
uv run python -m cdda2img create <input_dir> --loudness rg --strategy best
uv run python -m cdda2img create <input_dir> --silence notrim --loudness none

# Import a foreign disc image
uv run python -m cdda2img import disc.toc
uv run python -m cdda2img import /path/to/ddp_dir

# Extract an RBI image
uv run python -m cdda2img extract <file.rbi>                 # FLAC + CUE (default)
uv run python -m cdda2img extract <file.rbi> --raw           # raw PCM + TOC
uv run python -m cdda2img extract <file.rbi> --normalize     # FLAC normalised to −18 LUFS
uv run python -m cdda2img extract <file.rbi> --tracks --raw  # both

# Inspect and verify
uv run python -m cdda2img list <file.rbi>
uv run python -m cdda2img list <file.rbi> --prov     # dump the full decoded PROV block
uv run python -m cdda2img test <file.rbi>

# Run tests
uv run pytest tests/

# Run a single test
uv run pytest tests/test_transcode.py::test_transcode_roundtrip

# Lint, format, and type check (matches CI exactly — runs pre-commit on all files + ty)
make check

# Run tox (multi-Python CI)
tox
```

## Tools

Standalone utility scripts live in `tools/` (tracked, not part of the installed package).

- **`tools/measure_write_offset.py`** — burn-and-read-back write offset measurement.
  Generates a synthetic test signal, burns it via `cdrdao write`, rips via `cdrdao read-cd`,
  and measures where the known pulses landed. Accumulates cycles per drive in
  `rips/write_offset_<drive-slug>.toml`. Run from project root:
  ```
  uv run python tools/measure_write_offset.py --device /dev/sr0 --read-offset 30
  ```

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
1. `cdda2img.py:_rip_disc_stage()` — chooses the read path. **Normal** (`c2_recovery=off`, default): `_rip_with_fallback()` — `cdrdao_ripper.rip_cdrdao()` (primary) → `disc_reader.rip_disc(paranoia="full")` on RuntimeError. **C2 path** (`c2_recovery=on`, or `auto` + `c2_reader.drive_supports_c2()`): `c2_reader.read_disc_c2()` (raw s16le audio + C2 bitmap via `c2read --full`, with real TUI progress via c2read's stdout `progress` lines; can also capture the raw P-W subchannel in the same pass via `output_sub`) plus a `cdrdao_ripper.read_toc_metadata()` pass (`cdrdao read-toc` for pre-gaps/ISRC/MCN/CD-Text — a second read until the subchannel *decode* is wired end-to-end (`docs/reference/c2read-upgrade-plan.md`, Phase 4); also sidesteps bug #75). c2read zero-fills hard-unreadable sectors (PCM zeros + C2 all-ones = pure erasures downstream). cdrdao/c2read reads return **raw** PCM; only a cd-paranoia read fallback is offset-corrected.
   - `cdrdao_ripper.py:rip_cdrdao()` — runs `cdrdao read-cd`; parses TOC via `toc_parser.py`, builds disc via `cdrdao_reader.parsed_to_rbi_disc()`, byte-swaps s16be BIN via `cdrdao_reader.convert_cdrdao_bin()`; returns `RipInfo(disc, track_lsns, disc_last_lsn)`
   - `disc_reader.py:rip_disc()` — cd-paranoia fallback; queries disc via `-Q`, rips via subprocess; returns same `RipInfo`
   - **CTDB recovery (above cd-paranoia)**: on a partial AR mismatch, `rip_image` first tries `ctdb_repair.repair_whole_disc()` — error-only ctanalyse (or C2-erasure-assisted when the C2 path captured a bitmap) on the raw PCM, committed only if a CTDB per-track-CRC **and** AccurateRip double-gate both pass; **zero extra reads**. Only if that fails (not in CTDB / over RS capacity / a gate rejects it) does the cd-paranoia ladder below run. **Unified offset domain**: cdrdao/c2read reads stay raw through AR + ctanalyse (verify at `read_offset`); `apply_offset` runs exactly once — before the cd-paranoia ladder, or at storage. PROV: `recovery_track_<n>=ctdb_repaired@<entry_id>`. Modules: `ctdb_repair.py` (canonical logic, reuses `accuraterip`), `c2_reader.py` (c2read wrappers; `ctanalyse`/`c2read` are on `$PATH`). `tools/ctdb_repair.py` is the standalone CLI equivalent.
   - **AR-recovery (rip only)**: after the rip, `rip_image` runs `accuraterip.verify_rip`; on a *partial* mismatch (some tracks are in the AR DB but unmatched), `cdda2img._recover_failed_tracks` re-rips each failed track across the drive's probed speed ladder (`drive_speed.probe_speed_ladder`, fastest→slowest, `cfg.recovery_passes` sweeps — total attempts = passes × ladder steps), AR-verifying each cd-paranoia attempt with `accuraterip.match_track_pcm` against responses fetched once (`fetch_ar_responses`) and splicing the **first match**; a track that never matches keeps its original cdrdao audio (no unverified splice). The drive is restored to max **once** after the loop (`rip_single_track(restore_speed=False)` in the loop). Eject/reset are **not** used (no measured recovery benefit — see `tools/paranoia_recovery_test.py`). A cdrdao/cd-paranoia track-boundary disagreement falls through to a full-disc cd-paranoia re-rip. Outcomes recorded in PROV: `recovery_track_<n>=matched@<speed>X` | `unrecovered`, plus `recovery_passes` / `recovery_ladder`. `recovery_passes=0` disables recovery.
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
3. `container.py:extract_data()` — dispatches to raw and/or track output, plus optional `--rg`, `--ar`, `--log` sidecars
4. `track_extract.py` — slices PCM per track, wraps in WAV, encodes to FLAC via PyAV with Vorbis comment metadata; writes CUE sheet; optionally applies −18 LUFS normalisation

### Key modules
- **`rbi_format.py`** — RBI v6.0 constants (`VERSION_MAJOR = 6`, `VERSION_MINOR = 0`), `HEADER_STRUCT` (40-byte fixed header), `DIR_ENTRY_STRUCT` (54-byte directory entry), block type IDs (`BLOCK_TYPE_TOC`/`PROV`/`RGDB`/`ARIP`/`RLOG`/`PCM`), `RBIHeader` / `RBIDirEntry` / `RBIDisc` / `RBITocEntry` / `RBIReplayGain` dataclasses, `frames_from_timestamp()`, `timestamp_from_frames()`. `RBIDisc` carries `pre_emphasis: bool | None` (R14 aggregate disc-level flag — None means not captured), `discogs_release_id: int | None`, and the v6.0 catalogue fields: `cdtext_catalog_ref: str | None` (renamed from `disc_id` at v6.0 — clean break, no read shim — to disambiguate the CD-Text DISC_ID label string from the MCN and the label's catalogue number), `catalog_number: str | None` (the label's own alphanumeric number, e.g. `"CID U2 6"`), `label: str | None`, and `country: str | None` (ISO-3166 alpha-2, or MB pseudo-code XE/XW). `catalog: str | None` is the **on-disc MCN** (Q-ch Mode 2) — archival only, never a lookup/disambiguation key; it is the TOC `CATALOG` line and may be synthesised from `barcode` at finalisation when the disc carries no MCN (`mcn_source=barcode_derived`). `barcode: str | None` is the **service UPC/EAN** disambiguation key (MB/Discogs), routed through the resolver as `Field.BARCODE` and persisted to **PROV only** (no format bump). The two are different identifiers in different namespaces — never cross-compared (identifier_trust_model.md §1a).
- **`cdda2img.py`** — CLI entry point; `create_image()`, `import_image()`, `rip_image()`, `extract_image()` top-level functions. PROV-side helpers: `_r6_acoustid_corroborate` (R6 pre-menu fingerprint, tracks 1 and ceil(N/2)), `_emit_r9_disagreement` (NFC + casefold + reissue-suffix allow-list strip), `_r11_corroborate_with_discogs_master` (prefer-the-earlier on disagreement), `_r12_status` (`OK`/`empty`/`down`/`disabled` mapping), `_emit_mb_provenance` (writes `multi_match_isrc_disambiguated`, `release_selected_via`, `preferred_country_applied`, `mb_rejected_inconsistent`). §10 helpers: `_acoustid_gate` (§10.4 — writes `acoustid_gate=failed` on a genuine post-selection AcoustID miss; fail-only key), `_gate_adjusted_auto` (suppresses `--auto` for that disc when the gate failed, so the result is reviewed not auto-committed), `_discogs_barcode_corroborate` (§10.3.1 — follows the selected MB release's Discogs link via `mb_lookup.discogs_link_and_barcode` and compares barcodes, emitting `discogs_barcode_corroborates=YES` or `discogs_barcode_conflict=mb:<bc>|discogs:<bc>`).
- **`container.py`** — `build_container()`, `read_header()`, `extract_data()`, `wav_to_raw_pcm()`
- **`input_selector.py`** — four batching strategies: `fcfs`, `aatc`, `best` (OR-Tools CP-SAT global bin-packing), `meta` (groups by embedded disc-number tag)
- **`cdrdao_ripper.py`** — cdrdao read-cd rip (primary); parses TOC via toc_parser + cdrdao_reader; returns `RipInfo`
- **`disc_reader.py`** — cd-paranoia rip (fallback); subprocess-based; returns `RipInfo(disc, track_lsns, disc_last_lsn)`
- **`cddb.py`** — CDDB disc ID computation, TCP query (`query_cddb()`); results merged at lowest precedence by `cdda2img._run_metadata_lookups` (no high-trust apply helper)
- **`cdrdao_reader.py`** — cdrdao TOC+BIN import; s16be → s16le conversion
- **`ddp_reader.py`** — DDP 2.0 (GEAR Pro Mastering Edition) import
- **`toc.py`** — `generate_toc()`, `sanitize_title()`, `build_toc_entries()`
- **`mb_lookup.py`** — MusicBrainz disc ID + release lookup. `MBPrepopResult` carries `barcode_hints: list[tuple[str, str]]` (R16: `(mbid, barcode)` per match) and `release_selected_via: str | None` (which §10.3 rung picked the release — surfaced in PROV); `_score_candidate_by_isrcs` + `_disambiguate_by_isrcs` resolve multi-match (R1) with `_MIN_ISRC_AGREE=2` floor and strict-uniqueness tie semantics; `_resolve_via_isrc_tally` is the zero-disc-ID-match fallback (R4: ≥ ceil(N/2) ISRC convergence required). `_select_release_lexicographic` is the §10.3 deterministic release-selection ladder for disc-ID multi-match over the album's plurality release-group, returning `(winner, via)`. Lexicographic key chain: `barcode_plurality` (most common normalised barcode) → `preferred_country` (config ranking, a priority not a filter) → `date` (earliest `release_date`) → `mbid` (terminal deterministic tiebreak). The on-disc MCN is **not** a selection key (§1a — archival only); pressing selection rests on the candidates' own service barcodes (same-namespace). `via` names the highest-priority key on which the candidates actually vary — the rung that decided the winner — and is one of those four strings. `discogs_link_and_barcode(release_id)` follows a release's MB→Discogs url-relation and returns `(discogs_id, mb_barcode)` for the §10.3.1 cross-source barcode check. MB rate limit pinned at 1 req/s via `set_rate_limit` in `_setup_useragent` (R15).
- **`acoustid_lookup.py`** — AcoustID / Chromaprint per-track fingerprint lookup
- **`discogs_lookup.py`** — Discogs label, catalogue number, country lookup. `normalize_barcode` enforces the GS1 §1.3.1 check digit (R13); `lookup_master_year(release_id)` walks `release.master.main_release.year` for R11 corroboration.
- **`lookup_result.py`** — `DiscMeta` / `TrackMeta` shared result dataclasses
- **`validators.py`** — shared ISRC ISO-3901 regex + GS1 §1.3.1 GTIN-13 check-digit validators (R13). `validate_isrc` is the ISRC chokepoint (silent-drop + WARNING log on malformed input); `is_valid_gtin13` is wrapped by `barcode.normalize_barcode`.
- **Lookup caching** — the persistent R7 SQLite cache (`lookup_cache.py` / `lookup_cache.db`, 30-day TTL) was **removed** (superseded — caching wrong results for 30 days with no invalidation was the wrong trade-off). Replaced by process-lifetime in-process dicts with no TTL, discarded on process exit: `mb_lookup._DISC_ID_CACHE` (OPT-1, MB disc-ID lookups; `.clear()` to reset) and `album_art._COVER_CACHE` (OPT-2, cover-art bytes keyed on `CoverArt.source`). These de-duplicate the Phase-1 banner vs Phase-2 finalization calls within a single invocation only.
- **`metadata.py`** — `derive_album_info()` from file tags via mutagen
- **`metadata_menu.py`** — interactive metadata confirmation menu
- **`replaygain.py`** — EBU R128 analysis via pyebur128; `analyse()`, `pack_rg_block()`
- **`config.py`** — `Config` dataclass (`cddb_server`, `contact_email`, `database_backups`, `database_backup_frequency`, `catalogue_backups`, `catalogue_backup_frequency`, `drives`, `catalogue_path`, `enable_catalogue`, `default_device`, `silence_threshold`, `capacity`, `preview`, `tui`, `low_dr_threshold`, `auto`, `embedart`, `recovery_passes`, `c2_recovery`, `preferred_country`) + `DriveConfig` (per-drive `name`/`read_offset`/optional `write_offset`); `load_config()`, `save_drive()`, `save_drive_read_offset()`, `save_drive_write_offset()`, `_rewrite_config_drives()`, `_parse_preferred_country()` (parses the `preferred_country` TOML array — ISO-3166 alpha-2 + MB pseudo-codes XE/XW — into an ordered list); `[[drives]]` TOML array-of-tables round-trip; XDG path via `config_path()`. There is no global offline-mode flag; network gating is per-module via each lookup's `is_available()` (token / `fpcalc` presence).
- **`original_release.py`** — MusicBrainz release-group based lookup of the earliest known release of the same logical album. `find_original_release(disc)` returns `(found, title, year)`; `populate_original_release(disc)` assigns the result onto `RBIDisc` and skips when the user has already set the field manually via the metadata menu. Derivative secondary types (Compilation, Live, Remix, DJ-mix, Demo, etc.) are rejected — they're not "originals" in the sense the field captures. R3 verifier `_verify_release_matches_disc` gates both paths via conjunctive track-count + sum-of-durations (±2 s) + ISRC overlap + aggregate title fuzzy ≥ 80; each gate skips on missing evidence so empty-tracklist meta passes vacuously. R14 caps fuzzy candidates at year ≤ 1986 when `disc.pre_emphasis is True`.
- **`toc_parser.py`** — parses cdrdao TOC text into `ParsedDisc` / `ParsedTrack`. `ParsedDisc.pre_emphasis` aggregates per-track `PRE_EMPHASIS` flags (R14: `NO PRE_EMPHASIS` is treated as the negation, handled separately to avoid false-match).
- **`db.py`** — SQLite management for `drive_offsets.db`; `open_drive_offsets_db()`, `ensure_backup()`, `parse_frequency()`; WAL + foreign_keys; schema: `ar_drives`, `fetch_log`, `fetch_state`
- **`drive_info.py`** — sysfs drive name probe (`probe_drive_name`); AccurateRip `driveoffsets.htm` catalog (`ensure_drive_offsets` with 30-day cooldown, `find_drive_offset`); `_normalize_ar_name` handles `"VENDOR  - MODEL"` and `"- MODEL"` formats via two-pattern regex
- **`transcode.py`** — PyAV audio transcoding to Red Book PCM WAV
- **`silence.py`** — silence trimming and gap padding
- **`concat.py`** — WAV concatenation via the `wave` module
- **`track_extract.py`** — per-track FLAC extraction + CUE sheet writer
- **`audition.py`** — ffplay subprocess wrapper for interactive audition (pause/resume via SIGSTOP/SIGCONT)
- **`track_preview.py`** — cosmetic track-1 audio preview for the `rip` pipeline: grabs track 1 via cd-paranoia, loops it via ffplay in the background during the rip; best-effort (never fails a rip)

## RBI Format (v6.0)

40-byte fixed header: magic `RBIMAGE\x00`, version `6.0`, flags (uint32), track count (uint8), disc number/total (uint8/uint8), PCM parameters (sample rate uint32, channels uint8, bit depth uint8), block-directory offset (uint64), directory entry count (uint16), reserved bytes. The block directory is appended at end-of-file: each entry is 54 bytes (type ID, flags, offset, length, BLAKE3). v6.0 is a clean break from v5.0: `RBIDisc.disc_id` was renamed to `cdtext_catalog_ref` and the catalogue fields `catalog_number` / `label` / `country` were added; there is no read shim for v5.0 files.

Variable-length blocks (TOC and PCM are mandatory; the rest are optional and signalled by directory presence):

| Block | Contents |
|-------|----------|
| TOC | cdrdao-format text TOC; per-track pre-gap, ISRC, CATALOG (MCN), provenance comments |
| PROV | Provenance key=value text: creator, mode, source, ripper, drive; lookup-status / disagreement / corroboration surfaces (R9/R11/R12); `arip_transport` + `arip_dbar_b3sum` (R2); `pre_emphasis` (R14); `multi_match_isrc_disambiguated` (R1); `acoustid_corroborates` (R6); `discogs_release_id`; `duration_match_release` (stage 7); v6.0 catalogue + §10 keys: `catalog_number`/`label`/`country`, `barcode` + `mcn_source` (§1a — service UPC/EAN disambiguation key, PROV-only; and whether the TOC `CATALOG`/MCN was read from disc or `barcode_derived`), `release_selected_via` + `preferred_country_applied` (§10.2/10.3), `acoustid_gate=failed` (§10.4), `discogs_barcode_corroborates`/`discogs_barcode_conflict` (§10.3.1) |
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
- Tests use `example/` directory audio files (committed to repo) as fixtures
- **Byte-order invariants**: GEAR Pro DDP TRACK*.DAT is s16le — no byte-swap on import; cdrdao BIN output is s16be — always byte-swap via `convert_cdrdao_bin()` (import) or `convert_cdrdao_bin_to_wav()` (RG analysis); cd-paranoia outputs WAV (s16le) — no byte-swap for ripped data
- **Normalize vs ReplayGain**: `--normalize` is extract-time only (mutually exclusive with RG tag embedding); `--loudness rg` at create/rip/import time measures EBU R128 and stores the result in the RBI container without modifying the PCM
- **Subprocess**: `disc_reader.py`, `cdrdao_ripper.py`, and `track_preview.py` spawn `cd-paranoia` and `cdrdao`; `audition.py` and `track_preview.py` spawn `ffplay`; intentional subprocess calls carry `# noqa: S603, S607` (see LINT-008, LINT-012, LINT-013, LINT-017)
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
- `docs/reference/reference.toc` — annotated cdrdao TOC grammar reference
- `docs/reference/c2read-upgrade-plan.md` — c2read → read-only cdrdao replacement plan (F1–F11)
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

---

## CD-DA Domain Knowledge

This section documents CD-DA / subchannel / offset concepts relevant to current and planned work,
particularly the `rip` pipeline and `import` foreign image import pipeline.

### Q-channel Modes

The Q-channel is 96 bits per sector, present in all CD subchannels. ADR nibble selects the mode:

| ADR  | Mode   | Content                                      | Scope              |
|------|--------|----------------------------------------------|--------------------|
| 0001 | Mode 1 | TOC / track position (MSF, index, CTRL)      | Lead-in + program  |
| 0010 | Mode 2 | MCN — 13 BCD digits (UPC/EAN)                | Lead-in + program  |
| 0011 | Mode 3 | ISRC — 12 alphanumeric chars (ISO 3901)      | Per track          |

Mode 2 frames must appear at least once per 100 consecutive Mode 1 frames throughout the entire
disc (not just the lead-in). Mode 3 frames are distributed within their track's program area.

### CD-Text (R-W Subchannels, PTI 0x80–0x8F)

CD-Text lives in the R-W subchannels of the lead-in. Each pack is 18 bytes:
`[PTI][track][seq][block/charpos][12 bytes payload][CRC16 CRC16]`

CRC-16: polynomial x^16+x^12+x^5+1, init 0xFFFF, output inverted (CCITT).

| PTI       | cdrdao field  | Content                                          |
|-----------|---------------|--------------------------------------------------|
| 0x80      | TITLE         | Album (track 0) or per-track title               |
| 0x81      | PERFORMER     | Artist / performer                               |
| 0x82      | SONGWRITER    | Lyricist                                         |
| 0x83      | COMPOSER      | Composer                                         |
| 0x84      | ARRANGER      | Arranger                                         |
| 0x85      | MESSAGE       | Free text                                        |
| 0x86      | DISC_ID       | Label catalogue string (not the numeric MCN)     |
| 0x87      | GENRE         | 2-byte genre code + optional text                |
| 0x88      | TOC_INFO      | Binary TOC mirror — auto-generated by cdrdao     |
| 0x89      | TOC_INFO2     | Index/interval info — auto-generated by cdrdao   |
| 0x8A–0x8D | —             | Reserved, undefined                              |
| 0x8E      | UPC_EAN/ISRC  | Disc level = MCN (13 digits); track level = ISRC |
| 0x8F      | SIZE_INFO     | Block descriptor (3 packs) — auto-generated      |

PTI 0x8E is a convenience copy of the MCN/ISRC for software that only reads CD-Text. The
authoritative sources are the Q-channel Mode 2 (MCN) and Mode 3 (ISRC) subchannel frames.

PTI 0x88, 0x89, 0x8F are always auto-generated by cdrdao from the track layout; do not
hand-author them.

Up to 8 language blocks (0–7) are supported. Block 0 is typically ISO-8859-1 (Latin-1) English;
block character sets: 0x00 = ISO-8859-1, 0x80 = MS-JIS (Shift-JIS), 0x81 = Korean, 0x82 = GB.

### Sample Offset — Core Arithmetic

One CD frame = 588 stereo sample pairs = 2352 bytes. Every drive has a fixed read offset
(positive or negative) measured in samples. For a drive with offset N:

```python
sector_shift  = N // 588   # whole sectors to shift the read window
sample_offset = N  % 588   # residual samples within the shifted sector
```

The development drive (Plextor PX-716A) has offset **+30**: sector_shift=0, sample_offset=30
(entirely within one sector, no sector boundary crossing).

Drive offset is stored in `config.py:Config.drive_offset` and loaded from the XDG TOML config.
**Neither cdrdao nor cd-paranoia has a sample offset flag for burning.** For ripping, only
cd-paranoia supports offset correction at read time (via `-O`); cdrdao `read-cd` has no
equivalent. Offset correction for cdrdao-ripped audio must therefore be applied post-rip if
needed (see AccurateRip validation below).

### Rip Strategy (`rip` subcommand — `_rip_with_fallback`)

**Primary path — cdrdao** (`cdrdao_ripper.py:rip_cdrdao()`):
- Captures full subchannel data: MCN (Q-ch Mode 2), per-track ISRC (Q-ch Mode 3), CD-Text
  (R-W subchannels). This is the main advantage over cd-paranoia.
- **Depends on a bug-#75-fixed cdrdao for correct ISRCs.** cdrdao read-cd reads each ISRC
  inline from the streaming-audio subchannel; affected versions stale-latch the *previous*
  track's ISRC when a track's ISRC sits in its first sectors (**cdrdao bug #75**, open upstream
  2002–2026; fix submitted at github.com/cdrdao/cdrdao/issues/79 — a Boyer-Moore majority vote
  in `CdrDriver::audioRead`). With a patched cdrdao the invocation is bare `read-cd` with
  per-drive auto-detection. On an *unpatched* cdrdao, the in-tool workaround is to set the
  driver to `generic-mmc:0x0014` (the `0x0004` bit is `OPT_MMC_READ_ISRC`, forcing per-track
  `READ SUB-CHANNEL` queries); see the comment in `cdrdao_ripper.py`. The local Void package
  (`srcpkgs/cdrdao`, revision 2) carries the patch.
- BIN output is s16be — always byte-swapped to s16le by `convert_cdrdao_bin()`.
- No sample offset correction at rip time; AccurateRip validation detects whether correction
  is needed post-rip.

**Fallback path — cd-paranoia** (`disc_reader.py:rip_disc()`):
- Triggered on `RuntimeError` from the cdrdao path.
- `cd-paranoia` here is libcdio-paranoia (`/usr/bin/cd-paranoia`), distinct from the original
  `cdparanoia`. The `-O` flag accepts the drive offset directly; sign and optional `+` prefix
  are both handled correctly by its argument parser:

  ```bash
  cd-paranoia -O +30 -Z 1- output.raw   # fast pass: paranoia disabled, s16le, all tracks
  cd-paranoia -O +30    1- output.raw   # full paranoia
  cd-paranoia -O -30    1- output.raw   # negative offset: minus sign directly, no quoting
  ```

- Output format flags: `-r` = s16le, `-R` = s16be, `-w` = WAV. Span `1-` = all tracks.
- Three paranoia levels, all present in libcdio-paranoia (verified against the installed
  `cdparanoia III release 10.2 libcdio 2.1.0`) and mapped in `disc_reader.py:_PARANOIA_FLAGS`:
  `-Z` (`--disable-paranoia`, all checking off — `"off"`), `-Y` (`--disable-extra-paranoia`,
  cdda2wav-style overlap/jitter checking only — `"overlap"`), and no flag (full paranoia with
  scratch detection + repair — `"full"`). `-X` (`--abort-on-skip`) also exists. (Earlier note
  claiming `-Y`/`-X` are absent here was wrong — corrected 2026-06-10.)
- Measured on a clean disc: `-Y` ran ~1.77× slower than cdrdao `--paranoia-mode 3` (244 s vs
  138 s) — the cost is overlap re-reads, not better recovery; same paranoia-algorithm ceiling.
- The fallback currently uses `paranoia="full"`. A two-pass approach (fast `-Z` pass →
  AccurateRip validation → full paranoia only on failure) is the intended future refinement
  for this path.

**AccurateRip validation** applies after either path succeeds (see below).

### Sample Offset Correction — Post-Rip (Foreign Image Import)

When importing foreign rips (CCD, NRG, MDF, C2D, B6I, etc.) via the `import` subcommand, the source
drive offset is unknown and may not have been applied during the original rip. Offset correction
**must be applied across the full concatenated disc audio**, not per-track in isolation — the
corrected samples at a track boundary come from the adjacent track.

Correction pipeline (logic adapted from `fixoffset.py` in cdrip-tools; note that script has a
typo — `'ffprope'` in `REQUIRED` — meaning its ffprobe dependency check never fires):

```
sox [track1] [track2] ... -t raw -b16 -c2 -r44100 -e signed-integer - \
  | pad / trim by offset samples \
  | split back into per-track files by sample count
```

For positive offset N (drive reads N samples ahead — audio is shifted right):
- `pad 0 Ns | trim Ns <total_samples>` — remove N from start, pad N silence at end.

For negative offset N:
- `pad Ns 0 | trim 0 <total_samples>` — pad |N| silence at start, trim to original length.

Validation pre-condition: each track's sample count must be a multiple of 588 (one CD frame).
If not, the source is not a valid CD rip and correction should be refused with a clear error.

### Blind Offset Detection via AccurateRip

When the source drive offset is unknown (foreign import, or cdrdao-ripped audio where no `-O`
correction was applied), detect it by AccurateRip checksum matching:

1. Reconstruct disc TOC from the image format (track start MSFs, lead-out). This is the hard
   part — each foreign format encodes TOC differently; see `docs/reference/reference.toc` and
   `private/libmirage/` for format-specific parsing reference.
2. Compute AccurateRip checksum (v1 and v2) of tracks as-is.
3. Query AccurateRip DB using disc ID (derived from TOC: track count, track offsets, lead-out).
4. On no match: iterate candidate offsets in range −150..+150 samples (covers all known drives
   in the AccurateRip/redump databases), recompute checksum at each offset, query again.
5. Match at offset N with confidence ≥ 2–3 → apply correction of −N samples.

Reference implementation: whipper source (AccurateRip disc ID + checksum algorithm).

**Confidence caveats**:
- Confidence ≥ 2–3 is the minimum threshold for a reliable match; do not trust confidence 1.
- Discs with very low total DB confidence (sum across all submissions ≤ 2): a match does not
  guarantee correctness, only agreement with a small number of potentially-wrong rips.
- Autodetected offsets from tools like cyanrip can be badly wrong at low confidence (e.g.
  +1573 at confidence 12 vs the known-correct +30 for the PX-716A). Always prefer redump or
  AccurateRip database values over autodetected values when the drive is known.

**AccurateRip checksum boundary conditions**:
- AccurateRip intentionally excludes the first and last 5 frames (2940 samples) of each track
  from its checksum to reduce sensitivity to drive offset errors.
- The implemented `_ar_checksums()` uses `sum_from`/`sum_to` guards (see below). The most
  common implementation bug is **clipping** the read window at the file boundary instead of
  zero-padding — this shifts `sum_to` and mismatches the last track only. See zero-padding
  invariant in the AccurateRip Verification section below.

### AccurateRip Verification — `accuraterip.py`

Implementation: `src/cdda2img/accuraterip.py`. Called from `rip_image()` after either rip
path, before the metadata menu and container build. Verification is **informational only** —
never fails the rip. `verify_rip` skips the entire checksum loop when the disc is not in
the database (early return on empty `responses`).

**Algorithm — `_ar_checksums(frames, track, total_tracks)`**:

- `frames`: `array.array('I')` — raw PCM bytes reinterpreted as u32 LE (4 bytes = one stereo
  sample pair). Platform guard at module load: `array.array('I').itemsize != 4` raises
  `RuntimeError`.
- Multiplier `mult = i + 1` (1-based from frame 0; never reset across track boundaries).
- Boundary exclusion via guards (not by resetting the multiplier):
  - `sum_from = 2940 if track == 1 else 0` — skip the first 2939 frames of track 1
  - `sum_to = n - 2940 if track == last else n` — skip the last 2939 frames of last track
  - Include frame `i` when: `mult >= sum_from and mult <= sum_to` (`>=`, not `>`)
- `csum_lo += product & 0xFFFFFFFF`; `csum_hi += product >> 32` (overflow bits for v2)
- `v1 = csum_lo & 0xFFFFFFFF`; `v2 = (csum_lo + csum_hi) & 0xFFFFFFFF`
- Algorithm mirrors ARver `arver/audio/_audio.c:accuraterip()`.

**Boundary zero-padding invariant (critical — do not clip)**:

With drive offset `+N`, the read window for the last track is `(disc_last_lsn+1)*2352 + N*4`
bytes, which overshoots the PCM file by exactly `N*4` bytes. The correct fix is to
**zero-pad the raw buffer**, not clip it:

```python
if byte_start < 0:
    raw = bytes(-byte_start) + raw        # negative offset: pad zeros at start (track 1)
if byte_end > pcm_size:
    raw = raw + bytes(byte_end - pcm_size) # positive offset: pad zeros at end (last track)
```

Why zero-padding works: the padded samples fall within the ±2940-frame exclusion zone and
contribute nothing to the checksum. Clipping instead shortens the array by N elements,
shifts `sum_to = n - 2940` down by N, and excludes N frames that the database included —
causing a mismatch on the last track only.

Empirically confirmed: Madness *Divine Madness* (22 tracks), drive_offset=+30. Tracks 1–21
verified OK at conf 14; track 22 showed MISMATCH before the fix, OK at conf 13 after.
The conf 13 (vs 14 on other tracks) means one of the 14 same-offset submitters had this
exact clipping bug in their software — their track 22 CRC went into a different block.

**Disc IDs — `_ar_disc_ids(track_lsns, disc_last_lsn)`**:

Inputs are LSNs (not LBA). `lsn_leadout = disc_last_lsn + 1`.

```python
id1 = (sum(track_lsns) + lsn_leadout) & 0xFFFFFFFF
id2 = (sum((lsn or 1) * (i+1) for i, lsn in enumerate(track_lsns))
       + lsn_leadout * (n+1)) & 0xFFFFFFFF
```

Formula from ARver `arver/disc/fingerprint.py`. The `lsn or 1` guard handles LSN=0 for
track 1 on discs with no pre-gap offset.

**URL — `_ar_url(track_count, id1, id2, cddb_id)`**:

```
http://www.accuraterip.com/accuraterip/{id1[-1]}/{id1[-2]}/{id1[-3]}/
    dBAR-{n:03d}-{id1}-{id2}-{cddb_id:08x}.bin
```

The directory path uses the **last three characters of `id1` in reverse order** (LSBs first,
not first three characters). `cddb_id` is a 32-bit integer: `int(compute_cddb_disc_id(...), 16)`.

**dBAR binary format — `_parse_dbar(data, n_tracks)`**:

The response contains one or more consecutive blocks, each representing a different
drive-offset group in the AccurateRip database:

```
Block header:  13 bytes  <BLLL  n_tracks, id1, id2, cddb_id
Per-track:      9 bytes  <BLL   conf, crc, crc450
                         × n_tracks
```

Each track entry carries a **single** AccurateRip checksum (`crc`) — *not* separate v1
and v2 fields. Whether that value is a v1 or a v2 checksum depends on the submitting
ripper: v1-era rippers wrote a v1 checksum, v2-era rippers a v2 checksum, into the same
slot. The second 4-byte field (`crc450`) is the frame-450 sub-CRC used only for blind
offset detection — it is **not** the v2 checksum. `verify_rip` therefore computes both v1
and v2 locally and tests **each against `crc`**, tallying each variant's confidence from
whichever blocks matched; a v2-era block (often the highest-confidence one) is how v2
confidence is earned. A track not matched in any block gets `confidence=None`.

(History: matching the computed v2 against `crc450` instead of `crc` made `confidence_v2`
perpetually `None` — fixed by reading the single `crc` field for both variants.)

**Confidence interpretation**:

- Each block's confidence is the count of submissions that produced that CRC. The block
  with the highest confidence is typically from EAC-corrected rips at the "standard" offset.
- A minority-offset drive (e.g. Plextor PX-716A at +30) matches a lower-confidence block
  (conf ≈ 14) rather than the dominant block (conf ≈ 136). Conf 14 means 14 independent
  drives at this exact offset all agreed — this is **not** a sign of a bad rip.
- All-tracks mismatch on a disc that IS in the database (all `max_confidence` not None)
  almost always means `drive_offset` is missing or wrong. `print_ar_report` detects this
  case and prints a concise hint rather than 22 per-track MISMATCH lines:
  `"AccurateRip: disc found (max confidence 136) but no CRC match at drive_offset=0"`
- Partial mismatches (some tracks OK, some not) indicate genuine data corruption and always
  trigger per-track output.
- Do not trust confidence 1 as a reliable match. For blind offset detection, require ≥ 2–3.

**Drive offset config**:

`_resolve_drive_offset(device, cfg)` in `cdda2img.py` resolves the offset via three-tier lookup:
1. **`cfg.drives`** — `[[drives]]` TOML entries, keyed by normalised sysfs drive name; user-confirmed, always authoritative. Populated automatically on first successful AR catalog match.
2. **AccurateRip catalog** (`drive_info.py`) — `drive_offsets.db` at `$XDG_DATA_HOME/cdda2img/`; fetched from `http://www.accuraterip.com/driveoffsets.htm` with 30-day cooldown; auto-applied when submissions ≥ 3, interactive prompt when lower (no-op without TTY).
3. **`cfg.drive_offset`** — global integer fallback in `cdda2img.toml`; only used when the drive is absent from the catalog or the user declines.

On first run with no config file and a TTY, `_prompt_create_config()` offers to create it from `conf/cdda2img.toml.example`. The example path uses `Path(__file__).parent.parent.parent / "conf"` (dev-tree relative); replace with `importlib.resources` when packaging is set up.

### cdrdao .toc Field Reference (relevant subset)

See `docs/reference/reference.toc` for the full annotated grammar. Key fields:

```
CD_DA                               # disc type: CD_DA | CD_ROM | CD_ROM_XA
CATALOG "nnnnnnnnnnnnn"             # 13-digit MCN, Q-channel Mode 2

TRACK AUDIO
  COPY | NO COPY                    # CONTROL bit 2
  PRE_EMPHASIS | NO PRE_EMPHASIS    # CONTROL bit 0
  TWO_CHANNEL_AUDIO | FOUR_CHANNEL_AUDIO  # CONTROL bit 3
  ISRC "CCXXXYYNNNN"                # 12 chars, no hyphens, Q-channel Mode 3
  SILENCE MM:SS:FF                  # explicit digital silence (index 00)
  ZERO    MM:SS:FF                  # unspecified silence (index 00)
  START   [MM:SS:FF]                # marks index 01 boundary
  FILE "f.wav" [start [length]]     # MM:SS:FF offsets or byte offset for start
  INDEX MM:SS:FF                    # additional index points (02, 03, ...)
```

MSF frame rate: 75 frames/second. Valid frame range: 00–74.
Track 1 standard pre-gap: 00:02:00 (150 sectors of silence, index 00).
cdrdao BIN output is **s16be** — always byte-swap to s16le on import (`convert_cdrdao_bin()`
for rip path, `convert_cdrdao_bin_to_wav()` for RG analysis path).
