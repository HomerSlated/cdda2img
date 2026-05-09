# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`cdda2img` is a CLI tool for creating, importing, ripping, extracting, and verifying **RBI (Red Book Image)** archive containers of CD-DA audio discs. Subcommands:

- **`r`** — rip a physical disc via cdrdao (primary) with cd-paranoia fallback
- **`c`** — create one or more RBIs from a directory of audio files
- **`i`** — import a foreign disc image (cdrdao TOC+BIN or DDP 2.0 / GEAR Pro)
- **`x`** — extract to per-track FLAC + CUE, or raw PCM + TOC, or both
- **`l`** — list container sections and track index with offsets and checksums
- **`t`** — verify all checksums and structural invariants (23 checks, exits 1 on failure)

The `r`, `i`, and `c` pipelines all embed cdrdao-format TOC text, optional EBU R128 ReplayGain, and raw s16le PCM in a single RBI container. Metadata (album, artist, track titles, ISRC, CATALOG, remaster provenance) is sourced from CDDB, MusicBrainz, AcoustID, and Discogs lookups and an interactive confirmation menu.

This is a prototype; a Rust reimplementation is planned once the design has stabilised.

## Commands

```bash
# Install dependencies
uv sync

# Rip a physical disc (cdrdao primary; cd-paranoia fallback)
uv run python -m cdda2img r
uv run python -m cdda2img r /dev/sr0
uv run python -m cdda2img r --loudness none

# Create an RBI image from a directory of audio files
uv run python -m cdda2img c <input_dir>
uv run python -m cdda2img c <input_dir> --loudness rg --strategy ball
uv run python -m cdda2img c <input_dir> --mode master --loudness none

# Import a foreign disc image
uv run python -m cdda2img i disc.toc
uv run python -m cdda2img i /path/to/ddp_dir

# Extract an RBI image
uv run python -m cdda2img x <file.rbi>                 # FLAC + CUE (default)
uv run python -m cdda2img x <file.rbi> --raw           # raw PCM + TOC
uv run python -m cdda2img x <file.rbi> --normalize     # FLAC normalised to −18 LUFS
uv run python -m cdda2img x <file.rbi> --tracks --raw  # both

# Inspect and verify
uv run python -m cdda2img l <file.rbi>
uv run python -m cdda2img t <file.rbi>

# Run tests
uv run pytest tests/

# Run a single test
uv run pytest tests/test_transcode.py::test_transcode_roundtrip

# Lint, format, and type check
uv run ruff format src/ && uv run ruff check src/ && uv run ty check

# Run tox (multi-Python CI)
tox
```

## Architecture

All source lives under `src/cdda2img/`. The pipeline is fully wired end-to-end.

### Create pipeline (`c` subcommand)
1. `input_selector.py:select_batches()` — groups audio files into CD-sized batches (≤99 tracks, ≤80 min)
2. `transcode.py:transcode_audio()` — converts each track to 16-bit stereo 44.1 kHz PCM WAV via PyAV
3. `silence.py:trim_silence_cd_da()` — remaster mode only: trims leading/trailing silence (−55 dBFS) and appends 2-second inter-track gap
4. `concat.py:concat_wav()` — concatenates per-track WAVs into a single WAV
5. `container.py:wav_to_raw_pcm()` — strips WAV header, leaving raw s16le
6. `metadata.py:derive_album_info()` — extracts album/artist from file tags (mutagen)
7. `metadata_menu.py:run_metadata_menu()` — interactive metadata confirmation; AcoustID + Discogs lookups
8. `toc.py:generate_toc()` — derives track durations and generates cdrdao-format TOC from `RBIDisc`
9. `replaygain.py:analyse()` — optional EBU R128 loudness analysis via pyebur128 (per-track source WAVs, no concat)
10. `container.py:build_container()` — writes the RBI file

### Rip pipeline (`r` subcommand)
1. `cdda2img.py:_rip_with_fallback()` — tries `cdrdao_ripper.rip_cdrdao()` (primary); falls back to `disc_reader.rip_disc(paranoia="full")` on RuntimeError
   - `cdrdao_ripper.py:rip_cdrdao()` — runs `cdrdao read-cd`; parses TOC via `toc_parser.py`, builds disc via `cdrdao_reader.parsed_to_rbi_disc()`, byte-swaps s16be BIN via `cdrdao_reader.convert_cdrdao_bin()`; returns `RipInfo(disc, track_lsns, disc_last_lsn)`
   - `disc_reader.py:rip_disc()` — cd-paranoia fallback; queries disc via `-Q`, rips via subprocess; returns same `RipInfo`
2. `cddb.py:prepopulate_from_cddb()` — TCP CDDB query using disc TOC fingerprint; pre-populates album/artist/track titles before the metadata menu
3. Shared finalization: `_finalize_import()` (see below)

### Import pipeline (`i` subcommand)
Two source types, each producing s16le PCM, then both call `_finalize_import()`:
- **DDP 2.0** (`ddp_reader.py:import_ddp()`): parses DDPID (MCN), PQDESCR (timing + ISRC), CDTEXT.BIN; PCM (TRACK*.DAT) is already s16le — no byte-swap
- **cdrdao TOC+BIN** (`cdrdao_reader.py`): parses `.toc` text via `toc_parser.py`; byte-swaps s16be BIN → s16le WAV via `convert_cdrdao_bin_to_wav()`

### Shared rip/import finalization (`_finalize_import`)
1. `mb_lookup.py:prepopulate_from_mb()` — MusicBrainz disc ID SHA-1 fingerprint lookup; auto-applies single match
2. `metadata_menu.py:run_metadata_menu()` — interactive metadata confirmation; AcoustID (per-track Chromaprint) + Discogs lookups
3. `toc.py:generate_toc()` — generates cdrdao-format TOC with provenance comments
4. `replaygain.py:analyse()` — optional EBU R128 analysis on per-track WAV slices of the raw PCM
5. `container.py:build_container()` — writes the RBI file

### Extract pipeline (`x` subcommand)
1. `container.py:read_header()` — parses the 169-byte fixed RBI header
2. `toc_parser.py:parse_toc()` — parses the embedded cdrdao TOC into `ParsedDisc` / `ParsedTrack` dataclasses
3. `container.py:extract_data()` — dispatches to raw and/or track output
4. `track_extract.py` — slices PCM per track, wraps in WAV, encodes to FLAC via PyAV with Vorbis comment metadata; writes CUE sheet; optionally applies −18 LUFS normalisation

### Key modules
- **`rbi_format.py`** — RBI v3.0 constants, `HEADER_STRUCT`, `RBIHeader` / `RBIDisc` / `RBITocEntry` / `RBIReplayGain` dataclasses, `frames_from_timestamp()`, `timestamp_from_frames()`
- **`cdda2img.py`** — CLI entry point; `create_image()`, `import_image()`, `rip_image()`, `extract_image()` top-level functions
- **`container.py`** — `build_container()`, `read_header()`, `extract_data()`, `wav_to_raw_pcm()`
- **`input_selector.py`** — four batching strategies: `fcfs`, `aatc`, `bech`, `ball` (last two use OR-Tools CP-SAT)
- **`cdrdao_ripper.py`** — cdrdao read-cd rip (primary); parses TOC via toc_parser + cdrdao_reader; returns `RipInfo`
- **`disc_reader.py`** — cd-paranoia rip (fallback); subprocess-based; returns `RipInfo(disc, track_lsns, disc_last_lsn)`
- **`cddb.py`** — CDDB disc ID computation, TCP query, `prepopulate_from_cddb()`
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
- **`config.py`** — `Config` dataclass (`drive_offset`, `cddb_server`); `load_config()` from XDG TOML
- **`transcode.py`** — PyAV audio transcoding to Red Book PCM WAV
- **`silence.py`** — silence trimming and gap padding
- **`concat.py`** — WAV concatenation via the `wave` module
- **`track_extract.py`** — per-track FLAC extraction + CUE sheet writer
- **`audition.py`** — ffplay subprocess wrapper for interactive audition (pause/resume via SIGSTOP/SIGCONT)

## RBI Format (v3.0)

169-byte fixed header: magic `RBIMAGE\x00`, version `3.0`, uint64 offsets and lengths for TOC, ReplayGain, and PCM blocks, flags (uint32), track count, disc number/total, PCM parameters (sample rate, channels, bit depth), SHA-256 checksums for all three blocks.

Three variable-length blocks:

| Block | Contents |
|-------|----------|
| TOC | cdrdao-format text TOC; per-track pre-gap, ISRC, CATALOG (MCN), provenance comments |
| ReplayGain | 17 + 12×N bytes: per-track and album gain, peak, and LRA (float32). Optional; signalled by a flag. |
| PCM | Raw s16le — no WAV wrapper; parameters stored in fixed header |

Pre-gap audio is stored contiguously in the PCM block; the TOC records the pre-gap duration separately so extraction skips it cleanly.

Full specification: `docs/reference/rbi_spec.md`.

## Key Constraints

- Red Book limits: ≤99 tracks, ≤80 minutes per disc (`MAX_RUNTIME_MINUTES`, `MAX_TRACKS` in `input_selector.py`)
- Duration arithmetic uses integer scaling (`SCALE = 100`) to avoid floating-point bin-packing errors
- OR-Tools CP-SAT (`bech`/`ball` strategies) has no type stubs — all method calls carry `# type: ignore[attr-defined]`
- `ty` (not mypy) is the type checker; configured via `[tool.ty.environment]` in `pyproject.toml`
- Ruff line length is 120; `E501` is ignored. `S101` (assert) is allowed in tests
- Long exception messages use the `msg = ...; raise Err(msg)` pattern (TRY003)
- Tests use `example/` directory audio files (committed to repo) as fixtures
- **Byte-order invariants**: GEAR Pro DDP TRACK*.DAT is s16le — no byte-swap on import; cdrdao BIN output is s16be — always byte-swap via `convert_cdrdao_bin()` (import) or `convert_cdrdao_bin_to_wav()` (RG analysis); cd-paranoia outputs WAV (s16le) — no byte-swap for ripped data
- **Normalize vs ReplayGain**: `--normalize` is extract-time only (mutually exclusive with RG tag embedding); `--loudness rg` at create/rip/import time measures EBU R128 and stores the result in the RBI container without modifying the PCM
- **Subprocess**: `disc_reader.py` and `cdrdao_ripper.py` spawn `cd-paranoia` and `cdrdao` via `subprocess.run`; `audition.py` spawns `ffplay` via `subprocess.Popen`; intentional subprocess calls carry `# noqa: S603, S607` (see LINT-012, LINT-013, LINT-008)
- **Version** lives in `pyproject.toml` only; `container.py` and `cdda2img.py` read it via `importlib.metadata`
- **spec-before-code**: update `docs/reference/rbi_spec.md` before changing the container format

## Reference Material

Public documentation and research in `docs/`:
- `docs/reference/rbi_spec.md` — full RBI container format specification
- `docs/reference/reference.toc` — annotated cdrdao TOC grammar reference
- `docs/reference/TUI_Design.md` — TUI design notes
- `docs/man/cdda2img.1` — man page (install: `doas install -m 644 docs/man/cdda2img.1 /usr/local/share/man/man1/`)
- `docs/research/ABHOOD.md` — AB/HD ripping and offset research
- `docs/research/NONSPEC.md` — non-spec / real-world disc behaviour notes
- `docs/research/OFE.md` — offset/framing error notes
- `docs/research/REPLAYGAIN.md` — ReplayGain / EBU R128 research
- `docs/research/IEC_60908-1999.pdf.txt` — link to IEC web store for purchasing the Red Book standard
- `docs/research/Redump-Optical_Disc_Drives_CD_Compatibility_Technical_Details.txt` — Redump drive compatibility data
- `docs/research/spoons-audio-guide-cd-ripping.txt` — Spoons' audio CD ripping guide

Additional machine-local references (not committed) are documented in `CLAUDE.local.md`.

---

## CD-DA Domain Knowledge

This section documents CD-DA / subchannel / offset concepts relevant to current and planned work,
particularly foreign image import and sample offset correction.

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

| PTI  | cdrdao field  | Content                                          |
|------|---------------|--------------------------------------------------|
| 0x80 | TITLE         | Album (track 0) or per-track title               |
| 0x81 | PERFORMER     | Artist / performer                               |
| 0x82 | SONGWRITER    | Lyricist                                         |
| 0x83 | COMPOSER      | Composer                                         |
| 0x84 | ARRANGER      | Arranger                                         |
| 0x85 | MESSAGE       | Free text                                        |
| 0x86 | DISC_ID       | Label catalogue string (not the numeric MCN)     |
| 0x87 | GENRE         | 2-byte genre code + optional text                |
| 0x88 | TOC_INFO      | Binary TOC mirror — auto-generated by cdrdao     |
| 0x89 | TOC_INFO2     | Index/interval info — auto-generated by cdrdao   |
| 0x8A–0x8D | —      | Reserved, undefined                              |
| 0x8E | UPC_EAN/ISRC  | Disc level = MCN (13 digits); track level = ISRC |
| 0x8F | SIZE_INFO     | Block descriptor (3 packs) — auto-generated      |

PTI 0x8E is a convenience copy of the MCN/ISRC for software that only reads CD-Text. The
authoritative sources are the Q-channel Mode 2 (MCN) and Mode 3 (ISRC) subchannel frames.

PTI 0x88, 0x89, 0x8F are always auto-generated by cdrdao from the track layout; do not
hand-author them.

Up to 8 language blocks (0–7) are supported. Block 0 is typically ISO-8859-1 (Latin-1) English;
block character sets: 0x00 = ISO-8859-1, 0x80 = MS-JIS (Shift-JIS), 0x81 = Korean, 0x82 = GB.

### Sample Offset Correction — Core Arithmetic

One CD frame = 588 stereo sample pairs = 2352 bytes. Every drive has a fixed read offset
(positive or negative) measured in samples.

For a drive with offset N:
```
sector_shift  = N // 588       # whole sectors to shift the read window
sample_offset = N  % 588       # residual samples within the shifted sector
```

The PX-716A has offset +30: sector_shift=0, sample_offset=30 (entirely within one sector).

`cd-paranoia` (libcdio-paranoia, not the original cdparanoia) corrects at rip time:
```bash
cd-paranoia -O 30  1- output.raw    # +30: PX-716A
cd-paranoia -O -30 1- output.raw    # negative offsets use minus sign directly
cd-paranoia -O +30 1- output.raw    # explicit + prefix is also accepted
```

Raw output format flags: `-r` = s16le (little-endian), `-R` = s16be (big-endian), `-w` = WAV.
`-B` batch mode (one file per track, no span argument needed): `cd-paranoia -B -r`

### Sample Offset Correction — Post-Rip (Foreign Image Import)

When importing foreign rips (CCD, NRG, MDF, C2D, B6I, etc.) the source drive offset is unknown
or may not have been applied. Offset correction **must be applied across the full concatenated
disc audio**, not per-track in isolation — the corrected samples at a track boundary come from the
adjacent track.

Correction pipeline (adapted from `fixoffset.py` in cdrip-tools):
```
sox [track1] [track2] ... -t raw -b16 -c2 -r44100 -e signed-integer - \
  | pad / trim by offset samples \
  | split back into per-track files by sample count
```

For positive offset N (drive reads N samples ahead — audio is shifted right):
- Pad N samples of silence at end, trim N samples from start, trim to original total length.

For negative offset N:
- Pad |N| samples of silence at start, trim to original total length.

Validation: each track's sample count must be a multiple of 588 (one CD frame) — if not, the
source is not a valid CD rip.

### Blind Offset Detection via AccurateRip

When source drive offset is unknown, detect it by AccurateRip checksum matching:

1. Reconstruct disc TOC from the image format (track start MSFs, lead-out) — this is the hard
   part and differs per format.
2. Compute AccurateRip checksum (v1 and v2) of tracks as-is.
3. Query AccurateRip DB using disc ID (derived from TOC: track count, track offsets, lead-out).
4. On no match: iterate candidate offsets in range −150..+150 samples (covers all known drives),
   recompute checksum at each offset, query again.
5. Match at offset N with confidence ≥ 2–3 → apply correction of −N samples.

AccurateRip disc ID calculation and checksum algorithm: see whipper source as reference
implementation. The ±150 sample scan range covers all drives in the AccurateRip/redump databases.

### cdrdao .toc Field Reference (relevant subset)

```
CD_DA                           # disc type: CD_DA | CD_ROM | CD_ROM_XA
CATALOG "nnnnnnnnnnnnn"         # 13-digit MCN, Q-channel Mode 2

TRACK AUDIO
  COPY | NO COPY                # CONTROL bit 2
  PRE_EMPHASIS | NO PRE_EMPHASIS  # CONTROL bit 0
  TWO_CHANNEL_AUDIO | FOUR_CHANNEL_AUDIO  # CONTROL bit 3
  ISRC "CCXXXYYNNNN"            # 12 chars, no hyphens, Q-channel Mode 3
  SILENCE MM:SS:FF              # explicit digital silence (index 00)
  ZERO    MM:SS:FF              # unspecified silence (index 00)
  START   [MM:SS:FF]            # marks index 01 boundary
  FILE "f.wav" [start [length]] # MM:SS:FF offsets or byte offset for start
  INDEX MM:SS:FF                # additional index points (02, 03, ...)
```

MSF frame rate: 75 frames/second. Valid frame range: 00–74.
Track 1 standard pre-gap: 00:02:00 (150 sectors of silence, index 00).
