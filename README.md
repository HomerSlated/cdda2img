# cdda2img

**cdda2img** is a command-line tool for creating and extracting archive images of
Red Book standard CD-DA audio discs. It ingests a directory of audio files
(any format supported by ffmpeg), transcodes them to lossless Red Book PCM,
and writes a self-contained, checksum-verified **RBI (Red Book Image)** container
with an embedded cdrdao TOC. Extraction produces per-track FLAC files with
embedded metadata and a CUE sheet, or raw PCM with a TOC, or both.

## Why?

The CD-DA format, defined by the *Red Book* standard (IEC 60908:1999), stores audio
as a continuous PCM stream — it has no filesystem, no filenames, and no directories.
The "Track 01.cda" files that appear on your desktop are OS-generated stubs, not data
present on the disc. Archiving a CD-DA disc means preserving its audio stream and its
Table of Contents together, in a format that is self-describing and integrity-verified.

Existing CDDA container formats — CUE/BIN, CCD/IMG/SUB, MDS/MDF, NRG — were designed
for disc burning tools and are proprietary, incompletely documented, and typically
spread across multiple files. The goal of this project is a single, open, fully
documented container format suitable for long-term archival, that handles the complete
pipeline from raw audio files to a finished disc image without requiring access to
physical media.

This is an active prototype. A Rust reimplementation is planned once the design
has stabilised.

## Features

- **Create** RBI images from a directory of audio files (any ffmpeg-supported format)
- **Multi-disc batching** with four packing strategies — from simple first-come-first-served
  to optimal global bin-packing via OR-Tools CP-SAT
- **Silence trimming** and configurable inter-track gap
- **Optional EBU R128 normalisation** via ffmpeg-normalize
- **Metadata extraction** from file tags (mutagen) with interactive confirm
- **Extract** to per-track FLAC files with embedded Vorbis tags and CUE sheet
- **Extract** to raw s16le PCM + cdrdao TOC
- **SHA-256 integrity checksums** for TOC and PCM blocks
- **Multi-disc support** — disc number/total stored in the container

## RBI Format

RBI is a binary container format with a 121-byte fixed header: 8-byte magic
(`RBIMAGE\x00`), format version (major/minor uint8), uint64 offsets for the TOC and
PCM blocks, flags, track count, disc number/total, PCM parameters (sample rate,
channels, bit depth), and SHA-256 checksums.

The PCM block stores raw s16le — the canonical Red Book encoding — rather than a WAV
wrapper. WAV headers are reconstructed on extraction from the stored parameters. The
embedded TOC is a cdrdao-format text file.

The specification is documented in `rbi_spec.md` in the repository.

## Installation

**Requirements:**

- Python 3.10+
- [ffmpeg](https://ffmpeg.org/) (system install — must be in `PATH`)

**Install with uv (recommended):**

```bash
uv sync
```

**Or with pip:**

```bash
pip install cdda2img
```

Python dependencies (installed automatically): PyAV, ffmpeg-normalize, OR-Tools, Textual.

## Usage

```bash
# Create an RBI image from a directory of audio files
cdda2img c /music/album

# Create with EBU R128 normalisation and optimal disc packing
cdda2img c /music/album --normalize --strategy ball

# Extract to per-track FLAC files + CUE sheet (default)
cdda2img x album.rbi

# Extract to raw PCM + TOC
cdda2img x album.rbi --raw

# Extract both formats
cdda2img x album.rbi --tracks --raw
```

**Batching strategies:**

| Strategy | Description |
|----------|-------------|
| `fcfs`   | First-come-first-served: fill one disc in input order |
| `aatc`   | All-as-they-come: fill discs in input order (default) |
| `bech`   | Best-each: pack each disc as full as possible in turn (order not preserved) |
| `ball`   | Best-all: global bin-packing to minimise total disc count (order not preserved) |

The `bech` and `ball` strategies use OR-Tools CP-SAT and may be slow for large collections.

## Roadmap

The following are planned but not yet implemented:

- **ReplayGain support** — compute and store ReplayGain 2.0 metadata in the RBI
  container; embed as Vorbis comment tags on FLAC extraction
- **Physical disc ripping** — read audio directly from CD drives using third-party tools
- **Foreign format import** — read CUE/BIN, CCD/IMG/SUB, MDS/MDF, and NRG images
  as input to the create pipeline
- **Metadata lookup** — MusicBrainz and AcoustID fingerprinting, with fallback to
  heuristics and interactive prompt
- **TUI** — a Textual-based terminal interface with real-time progress and VU metering
- **AccurateRip verification** — rip accuracy checksums stored in the container

## License

GPLv3 or later

---

*Copyright © 2025 Homer*
