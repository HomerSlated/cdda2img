# TODO

## ✅ DONE — Update spec and format definition to v1.2 (2026-04-19)

- [x] `rbi_spec.md` — revised header layout table, removed `toc_end == pcm_start` invariant, updated validation rules, bumped to v1.2
- [x] `rbi_format.py` — new constants, updated offsets, uint64 struct format, revised dataclasses, HEADER_STRUCT with compile-time size assertion

---

## RBI Format — finalise spec before further code (agreed 2026-04-18)

### ✅ Breaking changes
- [x] Magic extended to 8 bytes: `RBIMAGE\x00`
- [x] Format version replaced with two `uint8` fields: `version_major=1`, `version_minor=2`
- [x] All four offset fields promoted to `uint64`
- [x] PCM block is now raw s16le (no WAV wrapper); WAV reconstructed on extract

### ✅ Additive fixed-header fields
- [x] `flags` (uint32)
- [x] `track_count` (uint8)
- [x] `disc_number` (uint8), `disc_total` (uint8)
- [x] `pcm_sample_rate` (uint32), `pcm_channels` (uint8), `pcm_bit_depth` (uint8)

### ✅ Structural
- [x] `toc_end == pcm_start` invariant removed; offsets are fully independent
- [x] `rbi_spec.md` updated to 121-byte fixed header layout
- [x] `rbi_format.py` updated with `HEADER_STRUCT`, compile-time size assertion, revised dataclasses

---

## Pipeline — wire up the full create/extract flow

- [x] Create `src/cdda2img/toc.py` — `sanitize_title`, `get_track_durations`, `build_toc_entries`, `generate_toc`
- [x] Create `src/cdda2img/container.py` — `build_container`, `read_header`, `extract_data`, `TempFiles`, `resolve_temp_dir`
- [x] Create `src/cdda2img/metadata.py` — `derive_album_info` with mutagen tag extraction and interactive confirm
- [x] Implement track concatenation (`concat.py`, using `wave` module — simpler and correct since format is guaranteed uniform)
- [x] Wire `normalize_pcm()` using `ffmpeg-normalize` Python API (controlled by `USE_NORMALIZATION` flag in `cdda2img.py`)
- [x] Wire up `cdda2img.py:main()` — full `c`/`x` pipeline, multi-disc support, roundtrip smoke-tested

---

## Roadmap

This document captures the agreed scope and direction for `cdda2img`. The Python
implementation is a prototype; a Rust reimplementation is planned once the design
has stabilised. Decisions should favour clarity and correctness over premature
optimisation.

### Input formats
- Physical CD-DA and mixed-mode disc (audio tracks only; data tracks discarded)
- Directory of audio files (non-recursive, any format supported by PyAV)
- RBI image file
- Foreign CDDA image files (CUE/BIN, CCD/IMG/SUB, MDS/MDF, NRG — audio component only)
- M3U, CUE, or TOC playlist/cuesheet paired with the audio files they reference

### Output formats
- RBI image file — the only officially supported write format
- Extracted TOC + raw PCM s16le (`--raw`)
- Extracted FLAC tracks with embedded metadata + CUE (`--tracks`)
- Foreign CDDA image formats — for internal testing and validation only, not distributed

### Audio processing modes
- **Master** — no processing; preserves source audio as-is
- **Remaster** — selective processing (silence trimming, EBU R128 normalisation, etc.)

---

## Tests (deferred — code verified working in practice)

- [ ] `input_selector.py` — tests for all four strategies (`fcfs`, `aatc`, `bech`, `ball`)
- [ ] `silence.py` — output shorter than input, has correct pad duration
- [ ] Container roundtrip — write RBI, read back, verify checksums and track list
- [ ] Foreign format sample bank — acquire images in each supported format (see acquisition options below); use as fixtures

---

## Foreign Image Format Support (deferred — needs sample files)

Goal: read CUE/BIN, CCD/IMG/SUB, MDS/MDF, NRG and similar CDDA image formats as
input to the `c` pipeline. Audio-only scope: for mixed-mode discs, extract audio
tracks and discard data tracks. Writing foreign formats is supported for internal
testing/validation only, not for distribution.

Reference: `extra/libmirage/images/` contains parser source for all formats below.

### Read (audio tracks only → RBI or FLAC+CUE)
- [ ] CUE/BIN — text CUE sheet + raw binary audio; structurally close to existing TOC support
- [ ] CCD/IMG/SUB — CloneCD binary header + raw sectors + subchannel data
- [ ] MDS/MDF — Alcohol 120% binary format
- [ ] NRG — Nero binary format
- [ ] M3U — simple playlist; pair with audio files in the same directory
- [ ] TOC (cdrdao) — already parsed for RBI extract; extend to accept as `c` input

### Write (internal testing only — not distributed)
- [ ] CUE/BIN — straightforward given existing TOC generation

### Sample bank (needed before implementation)
- Internet Archive (`archive.org`) — legitimate CD rips in various formats
- Redump.org — authoritative preservation database; checksums + source software info
- Create test images using CloneCD, Alcohol 120%, ImgBurn (most reliable for format
  accuracy since source disc is controlled)
- Store samples in `tests/fixtures/foreign/` — not committed if large; document
  acquisition steps in `tests/fixtures/foreign/README.md`

### CLI change needed
`c` command gains format auto-detection from file extension, plus an explicit
`--input-format` option when auto-detection is ambiguous.

---

## Physical Media / CD Drive (deferred — requires hardware)

Goal: read physical CD-DA discs. Creating our own disc writing/reading code is out
of scope; use third-party tools, preferring Python libraries where available.
Re-evaluate if existing tools prove limiting.

- [ ] Evaluate 3rd-party options: `pycdio` (libcdio bindings), `whipper` (implements
  AccurateRip; usable as subprocess), `cdrdao` (already used for ripping)
- [ ] New `r` subcommand: `cdda2img r /dev/sr0` — rip disc directly to RBI
- [ ] Parse subchannel Q data for MCN and CD-TEXT (see `extra/libmirage/mirage/cdtext-coder.c`)

### MCN (Media Catalogue Number)
MCN is a physical disc property (EAN-13 barcode); omit silently when the input does
not provide one. Include in the TOC `CATALOG` field when available.

- [ ] cdrdao rip input: parse `CATALOG "..."` line from `.toc` file if present
- [ ] `.sub` file input: scan for Mode 2 Q packets (ADR nibble = 0x2, TNO = 0x00), extract 13 BCD digits
- [ ] Audio files from directory: no MCN — omit `CATALOG` line

### CD-TEXT
- [ ] Read CD-TEXT from subchannel data (physical disc) and from `.sub` files
- [ ] Write CD-TEXT into generated TOC for CUE/BIN and RBI output
- [ ] Propagate CD-TEXT fields (performer, title, ISRC) to FLAC metadata on extract

### C2 error data and drive offset correction
Goal: implement accuracy verification similar to AccurateRip.

- [ ] Evaluate C2 pointer support in available drives (drive must support C2 reporting)
- [ ] Implement drive sample offset correction (offset database or user-supplied value)
- [ ] Compute AccurateRip v1/v2 checksums per track for verification against the
  AccurateRip database, or internally across multiple rips

---

## Metadata Strategy (deferred)

Goal: derive accurate track metadata from all available sources. Apply the following
sources in order of preference; merge where possible rather than replacing.

1. **Embedded tags** — IDv3 (MP3), Vorbis comments (FLAC/OGG), iTunes atoms (M4A),
   CD-TEXT, TOC `TITLE`/`PERFORMER` fields, CUE sheet `TITLE`/`PERFORMER`
2. **MusicBrainz lookup** — by disc ID (from TOC) or text search (album + artist)
3. **AcoustID / Chromaprint fingerprint** — fingerprint each decoded audio track,
   query the AcoustID API, resolve to MusicBrainz recording
4. **Heuristic** — infer from directory and file names (e.g. `01 - Track Title.flac`)
5. **Interactive prompt** — fall back to asking the user (existing `derive_album_info` flow)

- [ ] Add `python-musicbrainzngs` (or `musicbrainz`) and `pyacoustid` to dependencies
- [ ] Implement `metadata.py` lookup chain; return a confidence-ranked result set
- [ ] Present conflicts to the user when sources disagree above a threshold
- [ ] Store resolved metadata in RBI TOC; preserve original source tag in `comment` field

---

## Audio Processing (deferred)

### Normalisation preview
Allow the user to audition a range of LUFS targets before committing to a full
normalisation run.

- [ ] Initial analysis pass (stdout suppressed): capture the auto-lower warning
  `"WARNING: Using loudness target {target} ..."` to determine the upper bound of
  the usable range; lower bound is −70 LUFS
- [ ] Locate the loudest 10-second segment in the un-normalised audio (scan peak
  amplitude, centre a 10 s window on the peak frame)
- [ ] Generate 20 normalised samples spaced equally across `[target, −70]` LUFS
- [ ] Play samples with keystroke cycling (j/k or ←/→); `y` confirms current target
- [ ] Run full normalisation pass using the confirmed target
- [ ] Consider integrating into the TUI rather than as a standalone CLI flow

### Master / Remaster modes
- [ ] `--mode master` — no audio processing; copy source audio as-is (silence trim and
  normalisation both disabled)
- [ ] `--mode remaster` (default current behaviour) — silence trim + optional EBU R128
  normalisation
- [ ] Expose mode in the RBI header `flags` field (bit to be defined in spec)

---

## TUI (deferred — implement after CLI is feature-complete)

Goal: a fixed-layout terminal UI (audio console view) wrapping the full CLI feature
set. Suggested library: **Textual** (async-native, rich widget set, good VU meter
support via `sparkline`/custom widgets).

Planned elements:
- Peak/RMS VU meter (real-time, updated during transcode/normalise)
- Track name and progress as each track is processed
- Current processing stage (transcode → trim → normalise → pack)
- Album/artist, disc N/M, output target type
- Strategy and mode display
- Normalisation preview panel (audition targets, confirm)

- [ ] Design layout and widget hierarchy
- [ ] Implement real-time progress feed from pipeline stages
- [ ] Implement VU meter widget (driven by PyAV decoded frames)
- [ ] Wire normalisation preview into TUI panel

---

## RBI Format — ongoing evaluation

Continue evaluating the spec for improvements as the implementation matures.
Borrow ideas from other formats (CUE/BIN, MDS, CloneCD) where they address gaps.

- [ ] Define `flags` bit assignments (currently reserved): master/remaster mode,
  CD-TEXT present, MCN present, AccurateRip verified
- [ ] Consider embedding AccurateRip checksums in the container (new optional block
  after PCM, signalled by a flag)
- [ ] Evaluate whether CD-TEXT block should be a separate optional section or
  encoded within the TOC text

---

## Research Pool

Maintain a local collection of CDDA reference material in `extra/`.

Current holdings:
- `extra/IEC_60908-1999.pdf` — Red Book standard
- `extra/libmirage/` — image format parser source (MDS, CCD, NRG, TOC, CUE, CD-TEXT)

To add:
- [ ] dBpoweramp Spoon's Audio Guide (CD ripping): https://dbpoweramp.com/spoons-audio-guide-cd-ripping
- [ ] AccurateRip protocol documentation (EAC forum posts / whipper source)
- [ ] Drive offset database (AccurateRip or similar)

---

## Rust Reimplementation (future)

This Python codebase is a prototype. Once the design has stabilised — formats,
pipeline, metadata strategy, and TUI layout — implement a Rust version.

Design decisions taken in Python should be made with Rust portability in mind:
- Prefer explicit data structures over dynamic dispatch
- Keep I/O boundaries clear (parsing, processing, output are separate stages)
- Avoid Python-specific conveniences that have no clean Rust equivalent
