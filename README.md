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
- **Physical disc ripping** — `rip` reads directly from `/dev/sr0` (or any optical
  drive) via [AccuDisc](https://github.com/HomerSlated/accudisc), in **one pass and
  one spin-up**: raw s16le audio, the C2 error bitmap, the raw P-W subchannel, and the
  lead-in's full TOC and CD-Text. Track boundaries come from the error-corrected TOC;
  pre-gaps, INDEX points, MCN and per-track ISRC are assembled from the Q stream by
  majority vote. There is no second metadata pass and no second engine
- **Two-stage recovery on a partial AccurateRip mismatch**, in cost order:
  1. *CTDB parity repair* — Reed-Solomon reconstruction against crowd-sourced parity
     from the CUETools database, with **zero extra reads**. Where a C2 bitmap was
     captured it is fed in as erasures (roughly doubling what can be reconstructed);
     a repair is committed only if a CTDB per-track CRC **and** AccurateRip both accept it
  2. *Speed-ladder re-read* — only if CTDB declines. Each failed track's sector window is
     re-read across the drive's admitted speeds, fastest to slowest, and the first
     AccurateRip-verified result is spliced in sample-exactly. A track that never matches
     keeps its original audio: no unverified splice ever lands
- **AccurateRip v1/v2 verification** — per-track checksum computed against the AccurateRip
  database after every rip; matches against all drive-offset groups; reports confidence
  and mismatch status per track; results are stored in an ARIP block inside the RBI
  container. Drive offset is resolved automatically before each rip:
  1. per-drive `[[drives]]` config entries (user-confirmed, always authoritative)
  2. AccurateRip drive offset catalog (auto-applied at ≥ 3 submissions, interactive prompt
     below that threshold)
  3. `+0` with a warning, when the drive is neither configured nor matched

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
  - *PlexTools PXI* — Plextor's own bundled ripper; undocumented format, reverse-engineered.
    Embedded CD-Text, per-track INDEX points and MCN; audio stored as s16le (no byte-swap)

  Pass `--info` for a dry-run that prints image metadata without importing
- **Automatic metadata lookup** — disc is identified before the interactive menu fires:
  1. *CDDB* — TCP query (default: gnudb.gnudb.org:8880); pre-populates album, artist, year,
     and track titles from the disc TOC fingerprint
  2. *MusicBrainz disc ID* — SHA-1 TOC fingerprint lookup; a single match is applied;
     multiple matches are resolved by ISRC tally, then a deterministic release-selection
     ladder over the album's plurality release-group (on-disc MCN match, then barcode
     plurality, then `preferred_country` priority, then earliest release date, then a
     terminal MBID tie-break); barcode hints from MB feed the Discogs lookup
  3. *Discogs* — barcode lookup for label, catalogue number and country. The **barcode**
     (the service-side UPC/EAN) is the disambiguation key; the **MCN** read off the disc is
     archival only and is never used to select a release — they are different identifiers in
     different namespaces and are never cross-compared. The selected MusicBrainz release's
     Discogs link is also followed and its barcode compared against MusicBrainz's, as a
     cross-source corroboration recorded in provenance
  4. *AcoustID gate* — after the release is selected, per-track Chromaprint fingerprints are
     checked against the chosen release's album; a non-corroborating result records
     `acoustid_gate=failed` and suppresses `--auto` for that disc (informational; never fails
     the rip)
  5. *Interactive menu* — opens on every rip, import, and create unless `--auto` (or
     `auto = true` in config); confirm or correct all fields. A match-confidence
     recommendation (STRONG/MEDIUM/LOW/NONE) is shown for context but is display-only and
     never skips the menu on its own
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
  - `--raw` — TOC + WAV (s16le, self-describing RIFF header) to `extracted/`
  - `--rg` — ReplayGain block as `.rg.json`
  - `--ar` — AccurateRip report as `.accurip`
  - `--log` — rip log as `.log`
  - `--normalize` — EBU R128 normalisation at −18 LUFS on extracted FLACs
- **Burn** — `burn` subcommand writes an RBI back to disc via AccuDisc (`--simulate` for a
  laser-off test write); supports per-drive
  write offset from config, speed selection, and optional confirmation bypass (`--yes`).
  **Validated against a virtual (CDEmu) writer only** — byte-identical on a burn/read-back
  round trip, which exercises the byte layout and TOC grammar but not laser timing, real
  DAO lead-in or media quality. Treat burning to physical media as unproven and verify a
  burned disc by ripping it back
- **Virtual disc mount** — `mount` extracts a TOC+BIN scratch copy and loads it into a
  cdemu virtual slot; the mounted disc is then visible to cdrdao, whipper, or any other
  ripper for re-ripping, verification, or playback
- **List and verify** — `list` prints container structure, track index, and optional block
  content (`--info`, `--rg`, `--ar`, `--log`, `--prov` flags; `--prov` dumps every decoded
  provenance `key=value`, including keys no other view surfaces such as `acoustid_gate` and
  `release_selected_via`); `test` verifies all block checksums and structural invariants,
  exits non-zero on failure
- **Disc catalogue** — SQLite database at `$XDG_DATA_HOME/cdda2img/cdda2img.db`; populated
  automatically after every rip, import, or create; browsable via `cdda2img catalogue` with
  a summary page, full-text search across artist and album, and a per-disc track listing
  with AccurateRip status and confidence per track
- **BLAKE3 checksums** for all blocks — stored in the block directory, verified on every
  extract and test (SHA-256 in legacy v4.x containers)
- **Open, documented format** — `docs/reference/rbi_spec.md` fully specifies every field

## RBI Format

A single binary file. 40-byte fixed header containing the magic bytes `RBIMAGE\x00`,
format version (v6.0), track count, disc number/total, PCM parameters, and a pointer
to the block directory appended at the end of the file. Variable-length blocks:

| Block | Contents |
|-------|----------|
| TOC   | cdrdao-format text TOC; per-track pre-gap durations, ISRC, and CATALOG (MCN) |
| PROV  | Provenance key=value text: creator, mode, source, ripper, drive; release-selection, AcoustID-gate, and Discogs-barcode corroboration surfaces |
| RGDB  | 17 + 12×N bytes: per-track and album EBU R128 gain, peak, and LRA values |
| ARIP  | AccurateRip v1/v2 checksums and confidence per track |
| RLOG  | Structured rip log: drive, engine, offsets, per-track AR results, BLAKE3 self-seal |
| PCM   | Raw s16le — no WAV wrapper; parameters stored in the fixed header |

Each block has a BLAKE3 checksum stored in the block directory (SHA-256 in v4.x containers).
All blocks except TOC and PCM are optional. Pre-gap audio is stored contiguously in the PCM
block; the TOC records the pre-gap duration separately so extraction skips it cleanly.
Full specification: `docs/reference/rbi_spec.md`.

## Installation

**Requirements:** Python 3.10+. Anything that touches a physical disc — `rip`, `burn`
and `mount` — additionally needs [AccuDisc](https://github.com/HomerSlated/accudisc),
the CD-DA read/write engine, **as its Python binding**. Since 2026-08-01 that is the
only route: cdda2img calls AccuDisc's API and never runs its `accudisc` executable, so
having the binary on `$PATH` is not a substitute. Creating, importing, extracting, and
verifying RBI images need none of it.

Audio transcoding does *not* need an ffmpeg installation — PyAV carries the FFmpeg
libraries in its own wheel. The `ffplay` binary is used only for the rip's track-1
preview, which is cosmetic and never fails a rip.

```bash
./install.sh     # installed copy — see below
uv sync          # development checkout
```

`install.sh` does the four things an install needs and then verifies the result:
it `pipx install`s the application, finds and injects AccuDisc's Python binding if
one is installed, puts the man page under `--prefix` (default `/usr/local`), adds a
`file(1)` rule for `.rbi` images, and finally runs `cdda2img doctor`. Every step is
a command you can run by hand; the script exists because finding the AccuDisc wheel
is genuinely awkward — it lives under *AccuDisc's* prefix, since its compiled
extension is only valid beside the `libaccudisc.so.0` it was built against.

A missing wheel does not fail the install — `create`, `import`, `extract`, `list` and
`test` never touch a drive, so that machine is a legitimate one. It *does* make
`cdda2img doctor` exit 1, because the engine is a required dependency of the disc
subcommands; the installer reports that verdict without adopting it.
`./install.sh --help` lists the options,
`--dry-run` prints every command without running any, and `./install.sh uninstall`
reverses it — leaving your config alone, because it holds drive offsets that took
measurement to obtain.

Then check what the machine actually has:

```bash
cdda2img doctor
```

It reports every dependency — Python packages, the AccuDisc engine, external binaries,
native libraries — plus the package's own data files, and for each missing one, what it
would have enabled and the command that would install it. It **checks only**: nothing there installs, downloads, or modifies
anything, and it makes no network requests. Exit status is 1 if something *required* is
missing, 0 otherwise; a missing optional dependency is reported without failing, since
the absence of `ffplay` costs the rip's track-1 preview and not the rip.

A shorter form of the same check runs automatically before every other subcommand, so a
missing package produces a list of everything that is absent rather than an `ImportError`
naming one at a time.

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

# Burn an RBI back to disc via AccuDisc (--simulate for a laser-off test write)
uv run python -m cdda2img burn album.rbi
uv run python -m cdda2img burn album.rbi --device /dev/sr0 --speed 8
uv run python -m cdda2img burn album.rbi --write-offset -30 --yes

# Inspect a container; show AccurateRip report; dump full provenance; verify all checksums
uv run python -m cdda2img list album.rbi
uv run python -m cdda2img list album.rbi --ar
uv run python -m cdda2img list album.rbi --prov
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

## Security verification

Source files that have passed an internal security audit are accompanied by a
GPG **detached signature** — an armored `<file>.sig` sitting next to the file it
signs. These signatures are committed and pushed, so anyone with a clone can
confirm a given file is byte-for-byte what was audited and signed.

Signatures may appear beside files in `src/cdda2img/`, `tests/`,
`tests/fixtures/`, `tools/`, and `tools/wayback/`. The signing public key is
committed at `docs/guardian_public.asc`.

```bash
# One-time: import the signing public key into your keyring
gpg --import docs/guardian_public.asc

# Verify a signed file (exit status 0 = good signature)
gpg --verify src/cdda2img/cdemu.py.sig src/cdda2img/cdemu.py
```

A missing or failing signature is not necessarily alarming: a `.sig` is removed
whenever its file changes (the file must be re-audited and re-signed), so an
unsigned file simply means "not currently covered by a signature." A signature
that *fails* on an unchanged file, however, means the file or the signature was
tampered with — investigate before trusting it.

Full timestamped audit reports are kept locally under `private/guardian/` and
are **not** distributed; only the per-file signatures and the public key are.

## License

GPLv3 or later

---

*Copyright © 2026 Haze N Sparkle*
