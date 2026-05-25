# cdda2img

**cdda2img** is a command-line tool for creating and extracting **RBI (Red Book Image)**
archive containers of CD-DA audio discs. It ingests audio files in any format, transcodes
to lossless Red Book PCM, measures and stores EBU R128 ReplayGain metadata natively, and
packs everything into a single self-describing, checksum-verified container.

## Why?

A CD-DA disc has no filesystem — its audio is a continuous PCM stream indexed by a Table
of Contents. The `.cda` "files" visible in your OS are stubs, not data on the disc.
Archiving a CD-DA disc means preserving the PCM stream and the TOC together,
integrity-verified, in a format that will still be readable in twenty years.

Existing container formats — CUE/BIN, CCD/IMG, MDS/MDF, NRG — were built for disc burning
tools: proprietary, multi-file, incompletely documented. RBI is a single open container
with a fully documented binary spec designed for long-term archival.

This is an active prototype. A Rust reimplementation is planned once the design stabilises.

## Features

- **Format-agnostic ingestion** — any audio format supported by PyAV (FLAC, MP3, OGG,
  M4A, WAV, …); no ffmpeg subprocess required
- **Red Book transcoding** — 16-bit stereo 44.1 kHz s16le PCM, enforced by PyAV
- **Silence trim mode** — `--silence trim` (default) removes leading/trailing silence
  (configurable threshold in -dBFS) and inserts 2-second Red Book inter-track gaps;
  `--silence notrim` preserves the source audio as-is
- **Physical disc ripping** — `rip` subcommand rips directly from `/dev/sr0` (or any
  optical drive); primary path uses cdrdao (captures MCN, ISRC, and CD-Text in one pass);
  fallback uses cd-paranoia (full paranoia correction) when cdrdao fails; AccurateRip
  partial-mismatch triggers an automatic cd-paranoia re-rip of affected tracks
- **AccurateRip v1/v2 verification** — per-track checksum computed against the AccurateRip
  database after every rip; matches against all drive-offset groups; reports confidence
  and mismatch status per track; results are stored in an ARIP block inside the RBI
  container. Drive offset is resolved automatically before each rip:
  1. per-drive `[[drives]]` config entries (user-confirmed, always authoritative)
  2. AccurateRip drive offset catalog (auto-applied at ≥ 3 submissions, interactive prompt
     below that threshold)
  3. global `drive_offset` fallback in `cdda2img.toml`

  Confirmed offsets are persisted to `[[drives]]` so subsequent rips skip the catalog lookup
- **Disc image import** — `import` subcommand converts professional mastering images to
  RBIs verbatim (1:1 audio with byte-order conversion only); pre-gaps, CATALOG, ISRC,
  and CD-TEXT are preserved:
  - *DDP 2.0* (GEAR Pro Mastering Edition) — parses DDPID (MCN), PQDESCR (timing + ISRC),
    and CDTEXT.BIN; audio stored as s16le (no byte-swap)
  - *cdrdao TOC+BIN* — parses `.toc` text; byte-swaps s16be→s16le
  - *Nero NRG* — DAO CD-DA images in NER5 (64-bit offsets) and NERO (32-bit) variants;
    parses DAOX/DAOI track blocks, CDTX (CD-Text), and MTYP; audio stored as s16le
  - *CloneCD CCD/IMG* — parses `.ccd` index file; byte-swaps s16be→s16le

  Pass `--info` for a dry-run that prints image metadata without importing
- **Automatic metadata lookup** — disc is identified before the interactive menu fires:
  1. *CDDB* — TCP query (default: retrobridge.org:888); pre-populates album, artist, year,
     and track titles from the disc TOC fingerprint
  2. *MusicBrainz disc ID* — SHA-1 TOC fingerprint lookup; single matches are auto-applied;
     multiple are presented for selection; barcode hints from MB feed Discogs lookup
  3. *Discogs* — two-phase barcode lookup: the raw MCN/barcode is matched by substring to a
     canonical 13-digit EAN and written to `disc.catalog` (always, even without enrichment);
     if exactly one Discogs result matches the album title, full metadata is merged
  4. *Interactive menu* — confirm or correct all fields; can invoke AcoustID/Chromaprint
     per-track acoustic fingerprinting for discs not in the MB disc database
- **Release intelligence** — two factual signals captured at archive time and recorded
  in both the RBI provenance block and the disc catalogue:
  - *Low dynamic range* — boolean derived from the measured EBU R128 album LRA against
    a configurable threshold (default 5.0 LU); no guesswork about loudness-war eras
  - *Original release* — MusicBrainz release-group lookup identifies the earliest known
    release of the same logical album; reports `(found, title, year)` so a remaster of a
    1985 album shows what its first edition was, while a 1983 original shows nothing
- **ReplayGain 2.0** — EBU R128 loudness analysis via pyebur128; stored as a binary block
  inside the RBI container; embedded as Vorbis comment tags in extracted FLACs; computed
  per-track and at album level
- **Multi-disc bin-packing** — four strategies, including global optimisation via
  OR-Tools CP-SAT (`best`), which minimises total disc count across an entire collection
  in a single pass
- **Flexible extraction** — `extract` flags are additive; omitting all is equivalent to `--all`:
  - `--tracks` — per-track FLAC + CUE to `extracted/<artist>/<album>/`
  - `--raw` — TOC + BIN (disc-native s16be) to `extracted/raw/`
  - `--rg` — ReplayGain block as `.rg.json`
  - `--ar` — AccurateRip report as `.accurip`
  - `--log` — rip log as `.log`
  - `--normalize` — EBU R128 normalisation at −18 LUFS on extracted FLACs
- **Burn** — `burn` subcommand writes an RBI back to disc via cdrdao; supports per-drive
  write offset from config, speed selection, and optional confirmation bypass (`--yes`)
- **Virtual disc mount** — `mount` extracts a TOC+BIN scratch copy and loads it into a
  cdemu virtual slot; the mounted disc is then visible to cdrdao, whipper, or any other
  ripper for re-ripping, verification, or playback
- **List and verify** — `list` prints container structure, track index, and optional block
  content (`--info`, `--rg`, `--ar`, `--log` flags); `test` verifies all SHA-256 block
  checksums and structural invariants, exits non-zero on failure
- **Disc catalogue** — SQLite database at `$XDG_DATA_HOME/cdda2img/cdda2img.db`; populated
  automatically after every rip, import, or create; browsable via `cdda2img catalogue` with
  a summary page, full-text search across artist and album, and a per-disc track listing
  with AccurateRip status and confidence per track
- **SHA-256 checksums** for all blocks — stored in the block directory, verified on every
  extract and test
- **Open, documented format** — `docs/reference/rbi_spec.md` fully specifies every field

## RBI Format

A single binary file. 40-byte fixed header containing the magic bytes `RBIMAGE\x00`,
format version (v4.0), track count, disc number/total, PCM parameters, and a pointer
to the block directory appended at the end of the file. Variable-length blocks:

| Block | Contents |
|-------|----------|
| TOC   | cdrdao-format text TOC; per-track pre-gap durations, ISRC, and CATALOG (MCN) |
| PROV  | Provenance key=value text: creator, mode, source, ripper, drive |
| RGDB  | 17 + 12×N bytes: per-track and album EBU R128 gain, peak, and LRA values |
| ARIP  | AccurateRip v1/v2 checksums and confidence per track |
| RLOG  | Structured rip log: drive, engine, offsets, per-track AR results, SHA-256 self-seal |
| PCM   | Raw s16le — no WAV wrapper; parameters stored in the fixed header |

Each block has a SHA-256 checksum stored in the block directory. All blocks except TOC
and PCM are optional. Pre-gap audio is stored contiguously in the PCM block; the TOC
records the pre-gap duration separately so extraction skips it cleanly.
Full specification: `docs/reference/rbi_spec.md`.

## Installation

**Requirements:** Python 3.10+, [ffmpeg](https://ffmpeg.org/) (system install)

```bash
uv sync
```

## Usage

```bash
# Rip a disc (CDDB + MusicBrainz auto-identification)
uv run python -m cdda2img rip
uv run python -m cdda2img rip --device /dev/sr0
uv run python -m cdda2img rip --loudness none --output mydisc.rbi

# Create an RBI from a directory of audio files
uv run python -m cdda2img create /music/album
uv run python -m cdda2img create /music/album --silence trim --loudness rg --strategy best
uv run python -m cdda2img create /music/album --silence notrim --loudness none
uv run python -m cdda2img create /music/album --silence-threshold 60

# Import a foreign disc image (master mode, 1:1)
uv run python -m cdda2img import /path/to/ddp_dir
uv run python -m cdda2img import disc.toc --loudness none --output mydisc.rbi
uv run python -m cdda2img import album.nrg
uv run python -m cdda2img import disc.ccd
uv run python -m cdda2img import disc.toc --info   # dry-run: show metadata only

# Extract everything (default — equivalent to --all)
uv run python -m cdda2img extract album.rbi

# Extract only FLACs + CUE, or only TOC + BIN, or pick individual blocks
uv run python -m cdda2img extract album.rbi --tracks
uv run python -m cdda2img extract album.rbi --raw
uv run python -m cdda2img extract album.rbi --tracks --rg --ar

# Extract FLACs normalised to −18 LUFS instead of embedding RG tags
uv run python -m cdda2img extract album.rbi --tracks --normalize

# Burn an RBI back to disc via cdrdao
uv run python -m cdda2img burn album.rbi
uv run python -m cdda2img burn album.rbi --device /dev/sr0 --speed 8
uv run python -m cdda2img burn album.rbi --write-offset -30 --yes

# Inspect a container; show AccurateRip report; verify all checksums
uv run python -m cdda2img list album.rbi
uv run python -m cdda2img list album.rbi --ar
uv run python -m cdda2img test album.rbi

# Browse the disc catalogue
uv run python -m cdda2img catalogue
uv run python -m cdda2img catalogue --db /path/to/custom.db

# Mount as a virtual disc via cdemu (first free slot)
uv run python -m cdda2img mount album.rbi
uv run python -m cdda2img mount album.rbi --slot 1 --mnt-dir /tmp/mnt
```

**Batching strategies** (`--strategy`):

| Strategy | Description |
|----------|-------------|
| `fcfs`   | First-come-first-served: fill one disc in input order, stop |
| `aatc`   | All-as-they-come: fill discs in input order, as many as needed (default) |
| `best`   | Global bin-packing to minimise total disc count (OR-Tools CP-SAT; order not preserved) |
| `meta`   | Group tracks by embedded disc-number tag; untagged tracks form a final group |

## Development

```bash
make check   # lint, format, and type-check (matches CI exactly)
uv run pytest tests/
```

## License

GPLv3 or later

---

*Copyright © 2026 Haze N Sparkle*
