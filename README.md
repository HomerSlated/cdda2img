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

### Now

- **Format-agnostic ingestion** — any audio format supported by PyAV (FLAC, MP3, OGG,
  M4A, WAV, …); no ffmpeg subprocess required
- **Red Book transcoding** — 16-bit stereo 44.1 kHz s16le PCM, enforced by PyAV
- **Master / Remaster modes** — master preserves audio as-is; remaster applies silence
  trimming (−55 dBFS) and 2-second Red Book inter-track gaps
- **Disc image import** — `i` subcommand imports professional mastering images as
  master-mode RBIs; pre-gaps, CATALOG, ISRC, and CD-TEXT are preserved; per-track
  ReplayGain is measured from individual track slices:
  - *DDP 2.0* (GEAR Pro Mastering Edition) — the only open-source DDP 2.0 reader for
    Linux; parses DDPID (MCN), PQDESCR (timing + ISRC), and CDTEXT.BIN; enables
    professional glass-mastering archives without a Windows dependency
  - *cdrdao TOC+BIN* — byte-swaps s16be→s16le from disc-native format
- **ReplayGain 2.0** — EBU R128 loudness analysis via pyebur128 (the reference C library);
  stored as a binary block inside the RBI container; embedded as Vorbis comment tags in
  extracted FLACs; computed per-track and at album level without any concat step
- **Multi-disc bin-packing** — four strategies, including global optimisation via
  OR-Tools CP-SAT (`ball`), which minimises total disc count across an entire collection
  in a single pass
- **Extract to FLAC + CUE** or raw s16le PCM + TOC, or both; optional EBU R128
  normalisation at −18 LUFS on extract (mutually exclusive with ReplayGain tags)
- **List and verify** — `l` prints the full container index with offsets and sizes;
  `t` runs 23 structural and checksum checks, exits non-zero on failure
- **SHA-256 checksums** for TOC, ReplayGain, and PCM blocks — stored in the fixed header,
  verified on every extract and test
- **Open, documented format** — `docs/rbi_spec.md` fully specifies every field

### Planned

- `r` subcommand — rip a physical CD-DA disc directly to RBI via `/dev/sr0`
  (hardware: Plextor PX-716A, AccurateRip offset +30; byte-swap infrastructure already
  in place via `cdrdao_reader.py`)
- AccurateRip v1/v2 checksum verification per track
- Foreign format import — read CUE/BIN, MDS/MDF, CCD/IMG/SUB, NRG as input;
  read-only plugins only, always converted to RBI first
- MusicBrainz + AcoustID metadata lookup chain with confidence ranking
- **Release intelligence** — automatically detect remasters; surface the original
  release date so you know whether your archive predates the loudness war
- **Music collection catalogue** — SQLite database of all created RBIs, queryable
  by album, artist, remaster status, and more
- TUI — Textual-based terminal UI with real-time VU metering and delivery-mode audition
  (compare unprocessed / normalised / ReplayGain before committing to an extract)
- Rust reimplementation, once the design has stabilised

## RBI Format

A single binary file. Fixed-size header containing the magic bytes `RBIMAGE\x00`,
format version (v3.0), uint64 offsets, and SHA-256 checksums for three variable-length
blocks:

| Block | Contents |
|-------|----------|
| TOC | cdrdao-format text TOC; per-track pre-gap durations, ISRC, and CATALOG (MCN) |
| ReplayGain | 17 + 12×N bytes: per-track and album gain, peak, and LRA values |
| PCM | Raw s16le — no WAV wrapper; parameters stored in the fixed header |

Pre-gap audio is stored contiguously in the PCM block; the TOC records the pre-gap
duration separately so extraction skips it cleanly. The ReplayGain block is optional
and signalled by a flag. Full specification: `docs/rbi_spec.md`.

## Installation

**Requirements:** Python 3.10+, [ffmpeg](https://ffmpeg.org/) (system install)

```bash
uv sync
```

## Usage

```bash
# Create — remaster mode with ReplayGain, globally optimal disc packing
cdda2img c /music/album --mode remaster --loudness rg --strategy ball

# Create — master mode (transcode only, no processing, no ReplayGain)
cdda2img c /music/album --mode master --loudness none

# Extract to per-track FLAC files + CUE sheet (default)
cdda2img x album.rbi

# Extract to raw PCM + TOC, with EBU R128 normalisation applied
cdda2img x album.rbi --raw --normalize

# Import a DDP 2.0 mastering image (GEAR Pro; master mode, 1:1)
cdda2img i /path/to/ddp_dir
cdda2img i /path/to/ddp_dir --output mydisc.rbi

# Import a cdrdao TOC+BIN disc image (master mode, s16be→s16le)
cdda2img i disc.toc
cdda2img i disc.toc --loudness none --output mydisc.rbi

# Inspect a container; verify all checksums
cdda2img l album.rbi
cdda2img t album.rbi
```

**Batching strategies** (`--strategy`):

| Strategy | Description |
|----------|-------------|
| `fcfs`   | First-come-first-served: fill one disc in input order |
| `aatc`   | All-as-they-come: fill discs in input order (default) |
| `bech`   | Best-each: pack each disc as full as possible in turn |
| `ball`   | Best-all: global bin-packing to minimise total disc count (OR-Tools CP-SAT) |

A fifth strategy `tags` is planned: uses embedded disc-number metadata to recreate the
original multi-disc track selection exactly, with overflow handling via `ball`.

## License

GPLv3 or later

---

*Copyright © 2026 Haze N Sparkle*
