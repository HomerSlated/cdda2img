# Copilot Instructions for cdda2img

This file provides guidance for AI assistants working on this repository.

## Quick Start

```bash
# Install dependencies and pre-commit hooks
uv sync
uv run pre-commit install

# Run tests
uv run pytest tests/
uv run pytest tests/test_transcode.py::test_transcode_roundtrip  # single test

# Check code quality (pre-commit, ruff format/check, ty type-check)
make check

# Run tests across Python versions
tox
```

## Project Overview

**cdda2img** is a Python CLI tool for creating, importing, ripping, extracting, and verifying **RBI (Red Book Image)** archive containers of CD-DA audio discs.

### Subcommands

- **`r`** — rip a physical disc via cdrdao (primary) with cd-paranoia fallback
- **`c`** — create one or more RBIs from a directory of audio files
- **`i`** — import a foreign disc image (cdrdao TOC+BIN or DDP 2.0/GEAR Pro)
- **`x`** — extract to per-track FLAC + CUE, or raw PCM + TOC, or both
- **`l`** — list container sections and track index with offsets and checksums
- **`t`** — verify all checksums and structural invariants (23 checks, exits 1 on failure)

All `r`, `i`, and `c` pipelines embed cdrdao-format TOC text, optional EBU R128 ReplayGain, and raw s16le PCM in a single RBI container.

## High-Level Architecture

All source lives under `src/cdda2img/`. The tool implements four major pipelines:

### Create Pipeline (`c` subcommand)

1. `input_selector.py:select_batches()` — groups audio files into CD-sized batches (≤99 tracks, ≤80 min)
2. `transcode.py:transcode_audio()` — converts each track to 16-bit stereo 44.1 kHz PCM WAV via PyAV
3. `silence.py:trim_silence_cd_da()` — remaster mode: trims leading/trailing silence (−55 dBFS) and appends 2-second inter-track gap
4. `concat.py:concat_wav()` — concatenates per-track WAVs into a single WAV
5. `container.py:wav_to_raw_pcm()` — strips WAV header, leaving raw s16le
6. `metadata.py:derive_album_info()` — extracts album/artist from file tags (mutagen)
7. `metadata_menu.py:run_metadata_menu()` — interactive metadata confirmation; AcoustID + Discogs lookups
8. `toc.py:generate_toc()` — derives track durations and generates cdrdao-format TOC from `RBIDisc`
9. `replaygain.py:analyse()` — optional EBU R128 loudness analysis via pyebur128 (per-track source WAVs, no concat)
10. `container.py:build_container()` — writes the RBI file

### Rip Pipeline (`r` subcommand)

1. **Offset resolution** (`cdda2img.py:_resolve_drive_offset()`):
   - Checks `cfg.drives` (`[[drives]]` TOML entries, keyed by normalised sysfs name) — authoritative
   - Queries AccurateRip catalog (`drive_info.py`) — auto-applies at ≥ 3 submissions; prompts if lower
   - Falls back to global `cfg.drive_offset`
   - Confirmed offsets are persisted to TOML for future rips

2. **Primary rip path** (`cdrdao_ripper.py:rip_cdrdao()`):
   - Runs `cdrdao read-cd` to capture full subchannel data (MCN, ISRC, CD-Text)
   - Parses TOC via `toc_parser.py`; builds `RBIDisc` via `cdrdao_reader.parsed_to_rbi_disc()`
   - Byte-swaps s16be BIN to s16le via `convert_cdrdao_bin()`
   - Returns `RipInfo(disc, track_lsns, disc_last_lsn)`

3. **Fallback path** (`disc_reader.py:rip_disc(paranoia="full")`):
   - Triggered on `RuntimeError` from cdrdao
   - Uses libcdio-paranoia (`/usr/bin/cd-paranoia`) with `-O <offset>` flag
   - Returns same `RipInfo` structure

4. **Finalization** (shared with import):
   - AccurateRip v1/v2 verification (`accuraterip.py:verify_rip()`)
   - CDDB metadata lookup (`cddb.py:prepopulate_from_cddb()`)
   - MusicBrainz disc ID lookup (`mb_lookup.py:prepopulate_from_mb()`)
   - Shared metadata menu, TOC generation, ReplayGain analysis, and container build

### Import Pipeline (`i` subcommand)

Supports two source formats, both producing s16le PCM before shared finalization:

- **DDP 2.0** (`ddp_reader.py:import_ddp()`): parses DDPID (MCN), PQDESCR (timing + ISRC), CDTEXT.BIN; PCM is already s16le
- **cdrdao TOC+BIN** (`cdrdao_reader.py`): parses `.toc` text via `toc_parser.py`; byte-swaps s16be BIN to s16le

### Extract Pipeline (`x` subcommand)

1. `container.py:read_header()` — parses 169-byte fixed RBI header
2. `toc_parser.py:parse_toc()` — parses embedded cdrdao TOC into `ParsedDisc` / `ParsedTrack`
3. `container.py:extract_data()` — dispatches to raw and/or track output
4. `track_extract.py` — slices PCM per track, wraps in WAV, encodes to FLAC via PyAV; writes CUE sheet; optionally applies −18 LUFS normalisation

## Key Modules

- **`rbi_format.py`** — RBI v3.0 constants, `RBIHeader` / `RBIDisc` / `RBITocEntry` / `RBIReplayGain` dataclasses, frame/timestamp conversion
- **`cdda2img.py`** — CLI entry point; `create_image()`, `import_image()`, `rip_image()`, `extract_image()` top-level functions
- **`container.py`** — `build_container()`, `read_header()`, `extract_data()`, `wav_to_raw_pcm()`
- **`input_selector.py`** — four batching strategies: `fcfs`, `aatc`, `bech`, `ball` (last two use OR-Tools CP-SAT for global optimisation)
- **`cdrdao_ripper.py`** — cdrdao read-cd rip (primary); parses TOC and builds disc
- **`disc_reader.py`** — cd-paranoia rip (fallback); subprocess-based
- **`cddb.py`** — CDDB disc ID computation, TCP query
- **`cdrdao_reader.py`** — cdrdao TOC+BIN import; s16be → s16le conversion
- **`ddp_reader.py`** — DDP 2.0 (GEAR Pro Mastering Edition) import
- **`toc.py`** — `generate_toc()`, `sanitize_title()`, `build_toc_entries()`
- **`toc_parser.py`** — parses cdrdao TOC text into `ParsedDisc` / `ParsedTrack`
- **`mb_lookup.py`** — MusicBrainz disc ID + release lookup
- **`acoustid_lookup.py`** — AcoustID / Chromaprint per-track fingerprint lookup
- **`discogs_lookup.py`** — Discogs label, catalogue number, country lookup
- **`lookup_result.py`** — `DiscMeta` / `TrackMeta` shared result dataclasses
- **`metadata.py`** — `derive_album_info()` from file tags via mutagen
- **`metadata_menu.py`** — interactive metadata confirmation menu
- **`replaygain.py`** — EBU R128 analysis via pyebur128; `analyse()`, `pack_rg_block()`
- **`config.py`** — `Config` dataclass + `DriveConfig` (per-drive offset); TOML round-trip; XDG paths
- **`db.py`** — SQLite management for AccurateRip drive offsets; WAL + foreign_keys
- **`drive_info.py`** — sysfs drive name probe; AccurateRip catalog fetch with 30-day cooldown
- **`transcode.py`** — PyAV audio transcoding to Red Book PCM WAV
- **`silence.py`** — silence trimming and gap padding
- **`concat.py`** — WAV concatenation via the `wave` module
- **`track_extract.py`** — per-track FLAC extraction + CUE sheet writer
- **`audition.py`** — ffplay subprocess wrapper for interactive audition (pause/resume via SIGSTOP/SIGCONT)
- **`accuraterip.py`** — AccurateRip v1/v2 checksum computation and database verification

## Key Conventions

### Byte Order Invariants (Critical)

- **GEAR Pro DDP TRACK*.DAT**: s16le — no byte-swap on import
- **cdrdao BIN output**: s16be — always byte-swap via `convert_cdrdao_bin()` (import) or `convert_cdrdao_bin_to_wav()` (RG analysis)
- **cd-paranoia output**: WAV (s16le) — no byte-swap for ripped data
- **Verification**: zero-padding required when offset causes read window to overshoot file boundary (see `accuraterip.py`; clipping instead causes last-track checksums to mismatch)

### Normalize vs ReplayGain

- **`--normalize`**: extract-time only, mutually exclusive with RG tag embedding; applies −18 LUFS EBU R128 normalisation
- **`--loudness rg`**: create/rip/import time; measures EBU R128 and stores result in RBI container without modifying PCM

### Subprocess Calls

`disc_reader.py` and `cdrdao_ripper.py` spawn `cd-paranoia` and `cdrdao`; `audition.py` spawns `ffplay`. Intentional subprocess calls carry `# noqa: S603, S607`.

### Type Checking

- **Type checker**: `ty` (not mypy); configured via `[tool.ty.environment]` in `pyproject.toml`
- **OR-Tools CP-SAT**: has no type stubs — all method calls carry `# type: ignore[attr-defined]`

### Ruff Configuration

- **Line length**: 120; `E501` (line too long) is ignored
- **Assertions allowed in tests**: `S101` ignored per-file in `tests/`
- **Long exception messages**: use `msg = ...; raise Err(msg)` pattern (TRY003)
- **Format**: runs with `preview = true` (check `pyproject.toml` for exact settings)

### Version Management

**Version lives in `pyproject.toml` only**. No version string should be hardcoded in source files. The version is read at runtime via `importlib.metadata` by `container.py` and `cdda2img.py`.

### RBI Format

- **Invariant**: Update `docs/reference/rbi_spec.md` before changing the container format
- **Structure**: 169-byte fixed header + three variable-length blocks (TOC, ReplayGain, PCM)
- **TOC block**: cdrdao-format text with pre-gap, ISRC, CATALOG, and provenance comments
- **ReplayGain block**: optional; 17 + 12×N bytes (per-track and album gain, peak, LRA)
- **PCM block**: raw s16le with no WAV wrapper; parameters stored in fixed header

### Testing Fixtures

Tests use audio files in `example/` directory (committed to repo) as fixtures.

## Build, Test & Lint

```bash
# Install with pre-commit hooks
make install  # uv sync + pre-commit install

# Check code quality (runs pre-commit on all files + ty type-check)
make check

# Run tests
uv run pytest tests/
uv run pytest tests/test_transcode.py::test_transcode_roundtrip

# Test with doctests included
make test

# Multi-Python testing (requires multiple Python versions installed)
tox

# Build wheel
make build

# View help
make help
```

## Pre-Commit Hooks

The `.pre-commit-config.yaml` enforces:
- Trailing whitespace removal
- Import sorting (ruff isort)
- Code formatting (ruff format)
- Linting (ruff check)
- TOML/YAML validity
- Lock file consistency with `pyproject.toml`

Run `uv run pre-commit run -a` to check all files before committing.

## Documentation

- **`docs/reference/rbi_spec.md`** — full RBI container format specification (update before changing format)
- **`docs/reference/reference.toc`** — annotated cdrdao TOC grammar reference
- **`docs/reference/TUI_Design.md`** — TUI design notes
- **`docs/research/`** — CD-DA domain knowledge, AccurateRip/offset research, ReplayGain notes
- **`docs/man/cdda2img.1`** — man page

## Common Tasks

### Running a Single Test

```bash
uv run pytest tests/test_transcode.py::test_transcode_roundtrip -v
```

### Adding a New Subcommand or Feature

1. Add CLI entry point in `cdda2img.py`
2. Implement the feature in a new or existing module under `src/cdda2img/`
3. Create or extend tests in `tests/`
4. Update `README.md` with new functionality in the feature list
5. Run `make check` and `uv run pytest tests/` to verify
6. Run `tox` if touching Python version-specific code

### Modifying the RBI Container Format

1. **Update `docs/reference/rbi_spec.md` first** — spec is the source of truth
2. Update `rbi_format.py` dataclasses, `HEADER_STRUCT`, constants
3. Update container write logic in `container.py:build_container()`
4. Update container read logic in `container.py:read_header()` / `extract_data()`
5. Add migration/compatibility tests
6. Run full test suite and multi-Python tox

### Checking Out from main

Always run `make install` after pulling changes to ensure dependencies are up-to-date:

```bash
git pull origin main
make install
```

## References

- Full architecture and domain knowledge documented in `CLAUDE.md` (see "Architecture" and "CD-DA Domain Knowledge" sections)
- RBI specification: `docs/reference/rbi_spec.md`
- AccurateRip verification algorithm: `src/cdda2img/accuraterip.py` (inline comments detail boundary handling and zero-padding)
