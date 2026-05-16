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
- **Physical disc ripping** — `r` subcommand rips directly from `/dev/sr0` (or any
  optical drive); primary path uses cdrdao (captures MCN, ISRC, and CD-Text in one pass);
  fallback uses the `cd-paranoia` binary (full paranoia correction) when cdrdao fails
- **AccurateRip v1/v2 verification** — per-track checksum computed against the AccurateRip
  database after every rip; matches against all drive-offset groups; reports confidence
  and mismatch status per track; results are stored in an ARIP block inside the RBI
  container for future reference. Drive offset is resolved automatically before each rip:
  checks the per-drive `[[drives]]` config entries first, then queries the AccurateRip drive
  offset catalog (auto-applied at ≥ 3 submissions, interactive prompt below that threshold),
  then falls back to the global `drive_offset` setting. Confirmed offsets are persisted to
  `[[drives]]` in `cdda2img.toml` so subsequent rips skip the catalog lookup
- **Disc image import** — `i` subcommand imports professional mastering images as
  master-mode RBIs; pre-gaps, CATALOG, ISRC, and CD-TEXT are preserved; per-track
  ReplayGain is measured from individual track slices:
  - *DDP 2.0* (GEAR Pro Mastering Edition) — the only open-source DDP 2.0 reader for
    Linux; parses DDPID (MCN), PQDESCR (timing + ISRC), and CDTEXT.BIN; enables
    professional glass-mastering archives without a Windows dependency
  - *cdrdao TOC+BIN* — byte-swaps s16be→s16le from disc-native format
  - *Nero NRG* — DAO CD-DA images in NER5 (new, 64-bit offsets) and NERO (old, 32-bit
    offsets) format variants; single-session only; parses DAOX/DAOI track blocks, CDTX
    (CD-Text), and MTYP media type; audio stored as s16le (Windows-native, no byteswap)
- **Automatic metadata lookup** — disc is identified before the metadata menu fires:
  - *CDDB* — TCP query against configurable server (default: retrobridge.org:888);
    auto-populates album, artist, year, and track titles from the disc TOC
  - *MusicBrainz disc ID* — SHA-1 TOC fingerprint lookup; single matches are
    auto-applied; multiple are presented for selection
  - *AcoustID + Chromaprint* — per-track acoustic fingerprint lookup for recordings
    not yet in the MB disc database
  - *Discogs* — supplementary label, catalogue number, and country lookup
- **Release intelligence** — detects remasters from release title keywords and
  release-group first-release-date; embeds `PROVENANCE_REMASTERED_SOURCE` (NO /
  POSSIBLE / YES) and original release year in the TOC so your archive records whether
  the source predates the loudness war
- **ReplayGain 2.0** — EBU R128 loudness analysis via pyebur128 (the reference C library);
  stored as a binary block inside the RBI container; embedded as Vorbis comment tags in
  extracted FLACs; computed per-track and at album level without any concat step
- **Multi-disc bin-packing** — four strategies, including global optimisation via
  OR-Tools CP-SAT (`ball`), which minimises total disc count across an entire collection
  in a single pass
- **Flexible extraction** — `x` flags are additive; omitting all is equivalent to `--all`:
  - `--tracks` — per-track FLAC + CUE to `extracted/<artist>/<album>/`
  - `--raw` — TOC + BIN (disc-native s16be) to `extracted/raw/`
  - `--rg` — ReplayGain block as `.rg.json`
  - `--ar` — AccurateRip report as `.accurip`
  - `--log` — rip log as `.log`
  - `--normalize` — EBU R128 normalisation at −18 LUFS on extracted FLACs
- **Virtual disc mount** — `m` extracts a TOC+BIN scratch copy and loads it into a cdemu
  virtual slot; the mounted disc is then visible to cdrdao, whipper, or any other ripper
  for re-ripping, verification, or playback
- **List and verify** — `l` prints container structure, track index, and optional block
  content (`--info`, `--rg`, `--ar`, `--log` flags); `t` runs 27 structural and checksum
  checks, exits non-zero on failure
- **Disc catalogue** — SQLite database at `$XDG_DATA_HOME/cdda2img/cdda2img.db`; populated
  automatically after every rip, import, or create; browsable via `cdda2img d` with a summary
  page, full-text search across artist and album, and a per-disc track listing with AccurateRip
  status and confidence per track
- **SHA-256 checksums** for all blocks — stored in the block directory, verified on every
  extract and test
- **Open, documented format** — `docs/reference/rbi_spec.md` fully specifies every field

### Planned

- Foreign format import — read CUE/BIN, MDS/MDF, CCD/IMG/SUB, NRG as input;
  read-only plugins only, always converted to RBI first
- TUI — Textual-based terminal UI with real-time VU metering and delivery-mode audition
  (compare unprocessed / normalised / ReplayGain before committing to an extract)
- Rust reimplementation, once the design has stabilised

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
cdda2img r
cdda2img r --loudness none              # skip ReplayGain analysis

# Create — remaster mode with ReplayGain, globally optimal disc packing
cdda2img c /music/album --mode remaster --loudness rg --strategy ball

# Create — master mode (transcode only, no processing, no ReplayGain)
cdda2img c /music/album --mode master --loudness none

# Extract everything (default — equivalent to --all)
cdda2img x album.rbi

# Extract only FLACs + CUE; or only TOC + BIN; or pick individual blocks
cdda2img x album.rbi --tracks
cdda2img x album.rbi --raw
cdda2img x album.rbi --tracks --rg --ar

# Extract FLACs normalised to −18 LUFS instead of embedding RG tags
cdda2img x album.rbi --tracks --normalize

# Import a DDP 2.0 mastering image (GEAR Pro; master mode, 1:1)
cdda2img i /path/to/ddp_dir
cdda2img i /path/to/ddp_dir --output mydisc.rbi

# Import a cdrdao TOC+BIN disc image (master mode, s16be→s16le)
cdda2img i disc.toc
cdda2img i disc.toc --loudness none --output mydisc.rbi

# Import a Nero NRG image (master mode, DAO CD-DA only)
cdda2img i album.nrg
cdda2img i album.nrg --loudness none

# Inspect a container; show AccurateRip report; verify all checksums
cdda2img l album.rbi
cdda2img l album.rbi --ar
cdda2img t album.rbi

# Browse the disc catalogue
cdda2img d
cdda2img d --db /path/to/custom.db

# Mount as a virtual disc via cdemu (first free slot)
cdda2img m album.rbi
cdda2img m album.rbi --slot 1 --mnt-dir /tmp/mnt
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
