# TODO

## ⏸ PAUSED — Awaiting hardware (Lite-On SH-20A1S, up to 1 week from 2026-04-26)

Physical CD-DA drive work is blocked pending hardware arrival. All software tasks
in the active scope are complete and CI passes. Resume from the Physical Media
section below once the drive is available and tested.

---

## ✅ DONE — Research: Redump drive requirements + lead-in/lead-out documentation (2026-04-26)

- [x] **`private/ABHOOD.md` §5.4 added** — "CD Drive Technical Requirements for Accurate
  Dumping": scrambled-mode dumping, full P–W subchannel requirements, C2 error pointer
  semantics (MMC, not Red Book), lead-in depth (≥75 sectors, up to 150 for large positive
  write offsets), lead-out depth (≥75 sectors, more for large negative offsets), write
  offset vs drive offset distinction, `DATA_C2_SUB` vs `DATA_SUB_C2` ordering, redumper
  as preferred tool, DIC restrictions for Audio CDs.
- [x] **`private/NONSPEC.md` created** — "Lead-in and Lead-out: What They Contain, What
  They're Forced to Contain, and Where the Spec Breaks." Full technical discussion covering:
  spec-conformant lead-in layout (Q-channel TOC, P=0x00, zero main channel, CD TEXT in
  R–W); spec-conformant lead-out (P=0xFF, zero main channel, lead-out Q address); the
  pre-gap and HTOA as an intentional spec exploit; disc write offsets (manufacturing
  imprecision, Red Book does not define them, ±500–3000 samples seen in practice, how
  positive/negative offsets push audio into lead-in/lead-out respectively); drive offset
  vs disc write offset (net correction formula); copy protection attacks on the lead-in
  (Key2Audio corrupted main-channel TOC, fake second session, SafeDisc weak sectors);
  pre-mastering edge cases (non-zero lead-out main channel from early CD-R tools, why
  Redump checksums programme area only).

---

## ✅ DONE — Stale file cleanup (2026-04-26)

- [x] **Deleted** `test_normalize.py` — dead ffmpeg-normalize exploration script
- [x] **Deleted** `tests/test_transcode.py` — thin roundtrip test, superseded; better test planned
- [x] **Deleted** `src/cdda2img/unique_name.py` — dead module, not imported anywhere
- [x] **Deleted** `modules.md` — vestigial MkDocs placeholder
- [x] **Moved** `src/cdda2img/test_tui.py` → `docs/test_tui.py` — Textual TUI prototype,
  misplaced in `src/`; moved via `git mv` to preserve history

---

## ✅ DONE — Lint override register and LINT-007 fix (2026-04-27)

- [x] **LINT.md created** — documents all 10 lint suppressions and intentional unused
  variables with UIDs (LINT-001 through LINT-010), rationale, alternatives considered,
  and final decision. Every `# type: ignore`, `# noqa`, and `_`-prefixed unused variable
  in active source now carries its UID ref for cross-referencing.
- [x] **LINT-007 resolved** — `assert state is not None  # noqa: S101` in
  `replaygain.py:_measure_concat()` replaced with an explicit boundary guard
  (`if not paths: raise ValueError(...)`) at function entry. Loop refactored so `state`
  is initialised unconditionally from `paths[0]` before iterating `paths[1:]`; `ty` can
  now prove `state` is non-None at `_state_results()` without any suppression. The
  `# noqa: S101` and the `assert` are gone entirely.

---

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

## ✅ DONE — l/t commands, FLAG_MASTER_MODE, source RG provenance, tag case, container tests (2026-04-26)

- [x] **`l` (list) subcommand** — `list_container()` in `container.py`; prints section table
  (Fixed header / Metadata / TOC / ReplayGain block / PCM audio with offsets, human-readable
  sizes, total duration) followed by a numbered track listing.
- [x] **`t` (test) subcommand** — `verify_container()` in `container.py`; runs 23 checks:
  magic bytes, version, reserved flags, track/disc bounds, PCM params, section layout
  continuity, file size, UTF-8 metadata, SHA-256 checksums for all three blocks, TOC
  parse, track-count match. Exits with code 1 on any failure.
- [x] **`FLAG_MASTER_MODE`** (bit 2, `0x00000004`, even = "safe to ignore") — added to
  `rbi_format.py`; `FLAGS_RESERVED_MASK` updated to `0xFFFFFFFA`; `RBIHeader.is_master`
  property added; `build_container()` accepts `extra_flags`; `create_image()` passes
  `FLAG_MASTER_MODE` when `--mode master`; `rbi_spec.md` flags table updated.
- [x] **Source file RG tag provenance in TOC** — `read_source_rg_tags()` added to
  `metadata.py` (normalises ID3/Vorbis/iTunes tag name variants); `generate_toc()` writes
  `// SOURCE_RG: KEY="VALUE"` comment lines per track when tags present (cdrdao-compatible,
  ignored by TOC parser, preserved as provenance for future reference).
- [x] **Vorbis comment tag case audit** — `extract_tracks()` metadata dict corrected:
  `"title"/"artist"/"album"` → `"TITLE"/"ARTIST"/"ALBUM"` (written verbatim by libavformat);
  `"album_artist"/"track"/"disc"/"comment"` left as-is (libavformat maps these to
  `ALBUMARTIST`/`TRACKNUMBER`/`DISCNUMBER`/`DESCRIPTION` internally).
- [x] **Container roundtrip tests** — new `tests/test_container.py` (8 tests): header fields
  with/without RG, SHA-256 checksum integrity for all three blocks, TOC round-trip,
  RG block serialisation round-trip, FLAC extraction with embedded RG tags,
  `FLAG_MASTER_MODE` round-trip. All 8 pass in ~5s (module-scoped fixtures avoid repeated
  transcode + RG analysis).

---

## ✅ DONE — Subprocess elimination and audition.py cleanup (2026-04-26)

- [x] **`replaygain.py` measurement rewrite** — replaced `ffmpeg ebur128` subprocess
  (calls 1/2) with `pyebur128` + PyAV. `_decode_interleaved()` uses `AudioResampler`
  (not `to_ndarray(format=...)` which is unsupported in the installed PyAV version).
  `_measure_concat()` feeds all tracks to a single `R128State` sequentially — no concat
  subprocess, correct programme-level integrated loudness. Validated numerically against
  ffmpeg: ΔLUFS < 0.05, Δpeak < 0.005.
- [x] **`replaygain.py:embed_rg_tags()` rewrite** (call 3) — replaced `ffmpeg -c copy
  -metadata` subprocess with PyAV stream copy: reads existing FLAC container metadata,
  merges RG tags (uppercase), remuxes audio packets unchanged via `add_stream(template=)`.
  Tested: all 7 RG Vorbis comment keys present and uppercase in extracted FLACs.
- [x] **`audition.py:compute_rg()` elimination** (call 5) — deleted function; replaced
  with `replaygain.analyse([path])`. Deleted local `embed_rg_tags()` (mutagen, lowercased
  keys); replaced with `replaygain.embed_rg_tags()`. Removed `re` and `mutagen` imports.
- [x] **`audition.py:extract_clip()` rewrite** (call 4) — replaced `ffmpeg` subprocess
  with PyAV seek + decode + `AudioResampler(format="s16", layout="stereo")` + FLAC encode.
  Extracted `_window_frames()` generator (seek, skip-before-start, stop-at-end, resampler
  flush) to keep `extract_clip()` under the C901 complexity limit. Verified: 10.00 s
  output, 44100 Hz stereo, no metadata tags.
- [x] **`audition.py` lint fixes** — C901 on `main()` fixed by extracting `_handle_key()`;
  three RUF001 Unicode minus signs fixed; `_ffmpeg()` helper deleted (no longer used).
- [x] **`rbi_format.py`** — RUF003 Unicode minus in comment fixed.
- [x] **Suppress `ffmpeg_normalize` log noise** — `logging.getLogger("ffmpeg_normalize")
  .setLevel(logging.ERROR)` in `_normalize_flac()`; silences the
  "Using loudness target X because --auto-lower-loudness-target" WARNING that leaked to
  stdout mid-line. Behaviour unchanged: `auto_lower_loudness_target=True` is correct —
  it prevents clipping when true-peak headroom is insufficient for a full -18 LUFS boost.

---

## ✅ DONE — RBI v2.0, ReplayGain, and CLI refactor (2026-04-25)

- [x] **RBI spec v2.0** — bumped major version (breaking change); added `rg_start` (uint64),
  `rg_end` (uint64), `rg_checksum` (32 bytes) to the fixed header; moved `metadata` to
  offset 169; defined `FLAG_RG_PRESENT` (bit 0, even = "safe to ignore"); defined RG block
  layout (§7): 17 + 12×N bytes, column-major `track_gain`/`track_peak`/`track_range` arrays;
  added §9 validation rules 14–15; updated `rbi_spec.md` and `rbi_format.py` accordingly.
- [x] **`replaygain.py`** — EBU R128 analysis via `ffmpeg ebur128` filter; per-track
  measured independently, album measured over virtual concat; `pack_rg_block()` /
  `unpack_rg_block()` serialise to/from the binary RG block format.
- [x] **Remove `--normalize` from `c` subcommand** — dead `_normalize()` function deleted;
  `FFmpegNormalize` import removed; normalization deferred to `x --normalize` (extract pipeline).
- [x] **`--mode {master|remaster}`** — `remaster` (default): silence trim + 2-second inter-track
  gap; `master`: silence trim disabled, transcode only. `--loudness` still controls RG in both
  modes; "RG always in master" can be tightened later if needed.
- [x] **`--loudness {rg|none}`** — `rg` (default): measure EBU R128 and embed RG block in
  container gap; `none`: skip. Per-track measurement uses `source_wavs` (post-trim or
  post-transcode list) — never the concatenated blob.
- [x] **Fix `SILENCE_PAD_DUR`** — corrected `"1"` → `"2"` (Red Book inter-track gap convention).
- [x] **`build_container()` updated** — accepts optional `rg_block: bytes | None`; computes
  `rg_start`, `rg_end`, checksum, and sets `FLAG_RG_PRESENT` when block is provided; writes
  block in the gap between TOC and PCM.

---

## ReplayGain and Loudness

### Rules (never violate these)

- **Normalize and ReplayGain are mutually exclusive.** Never apply both to the same
  audio. Applying both produces incorrect output: a player will re-apply a gain offset
  to audio that has already been level-adjusted.
- **Per-track ReplayGain must be computed from individual track audio before
  concatenation**, not from the concatenated PCM blob. The concatenated blob yields
  only a single album-level measurement; per-track values require per-track audio.
- **Normalization is a delivery choice, not an archive choice.** The RBI always stores
  clean PCM. Normalization belongs at extract time only (`x --normalize`), for delivery
  to devices without ReplayGain support. Never apply normalization at create time.
- **For FLAC track extraction (`--tracks`)**: either normalize the output
  (`x --normalize`) OR embed ReplayGain tags — never both.

### Create pipeline (`c` subcommand)

- [x] **Source file RG tags** — if source files already have `REPLAYGAIN_*` tags,
  record their values as provenance metadata in the TOC (as cdrdao comments); the
  authoritative RG values in the RBI RG block are always freshly computed from the
  ingested audio, not copied from source tags.

### Extract pipeline (`x` subcommand)

- [x] **`--tracks` output** — RG Vorbis comment tags (`REPLAYGAIN_TRACK_GAIN`,
  `REPLAYGAIN_TRACK_PEAK`, `REPLAYGAIN_ALBUM_GAIN`, `REPLAYGAIN_ALBUM_PEAK`,
  `REPLAYGAIN_REFERENCE_LOUDNESS`, `REPLAYGAIN_TRACK_RANGE`, `REPLAYGAIN_ALBUM_RANGE`)
  embedded in extracted FLACs when RG block is present; suppressed when `--normalize` is given.
- [x] **`--normalize` flag on `x` subcommand** — applies EBU R128 normalisation to
  each extracted FLAC at −18 LUFS via `FFmpegNormalize`; mutually exclusive with RG tag
  embedding; no-op if `--tracks` not active.
- [x] **`--raw` output** — writes `.rg.json` sidecar alongside `.toc` and `.s16le`
  when the RBI contains an RG block.
- [x] **`--tracks` without RG block** — if RBI has no RG block (or RG checksum fails),
  compute ReplayGain from the extracted FLACs post-extraction via `analyse()` and embed
  tags with mutagen; prints album gain summary and any LRA warnings.

---

## Tests (deferred — code verified working in practice)

- [ ] `input_selector.py` — tests for all four strategies (`fcfs`, `aatc`, `bech`, `ball`)
- [ ] `silence.py` — output shorter than input, has correct pad duration
- [x] Container roundtrip — write RBI, read back, verify checksums and track list
- [ ] Foreign format sample bank — acquire images in each supported format (see acquisition options below); use as fixtures

---

## Foreign Image Format Support (deferred — needs sample files)

Goal: read CUE/BIN, CCD/IMG/SUB, MDS/MDF, NRG and similar CDDA image formats as
input to the `c` pipeline. Audio-only scope: for mixed-mode discs, extract audio
tracks and discard data tracks. Writing foreign formats is supported for internal
testing/validation only, not for distribution.

Reference: `private/libmirage/images/` contains parser source for all formats below.

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

## Physical Media / CD Drive (deferred — AWAITING HARDWARE)

**Hardware arriving**: Lite-On SH-20A1S DVD/CD Rewritable Drive. Expected within 1 week
of 2026-04-26. Resume this section once the drive is connected and tested.

**Drive evaluation criteria** (from `private/ABHOOD.md` §5.4 and `private/NONSPEC.md`):
- Scrambled-mode dumping support
- Full subchannel P–W readback; raw `DATA_C2_SUB` or `DATA_SUB_C2` ordering
- Reliable C2 error pointer support (Redump hard requirement)
- Lead-in read depth ≥ 75 sectors (150 preferred for write-offset edge cases)
- Lead-out read depth ≥ 75 sectors
- Check AccurateRip drive offset database for this model's known sample offset

Goal: read physical CD-DA discs. Creating our own disc writing/reading code is out
of scope; use third-party tools, preferring Python libraries where available.
Re-evaluate if existing tools prove limiting.

- [ ] Test Lite-On SH-20A1S: verify C2, subchannel, lead-in/lead-out depth against
  Redump criteria using `redumper` or `DiscImageCreator`; record drive offset
- [ ] Evaluate 3rd-party options: `pycdio` (libcdio bindings), `whipper` (implements
  AccurateRip; usable as subprocess), `cdrdao` (already used for ripping)
- [ ] New `r` subcommand: `cdda2img r /dev/sr0` — rip disc directly to RBI
- [ ] Parse subchannel Q data for MCN and CD-TEXT (see `private/libmirage/mirage/cdtext-coder.c`)

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

### MusicBrainz track-length verification (silence trim guard)

MusicBrainz track lengths derived from the CD TOC are computed as
`INDEX_01[n+1] − INDEX_01[n]`, so they include trailing baked-in silence **and** the
pre-gap of the next track. A correct rip (pre-gap appended to previous track) will
therefore produce a local duration that closely matches the MusicBrainz length. A local
duration that significantly exceeds it indicates excess silence — typically a ripper that
appended the pre-gap on top of already-present baked-in silence.

This step runs after transcoding (accurate WAV durations available) and before
`silence.py`, and sets a per-track trim target that `silence.py` respects as a floor.

Lookup reliability, in descending order:
- **Disc ID** (from TOC input) — exact frame-accurate lengths for that pressing
- **AcoustID fingerprint** — identifies the recording; lengths may vary across pressings
- **Text search** — lowest confidence; may match a different version or regional pressing

For directory input (no disc ID), use AcoustID to resolve each track to a MusicBrainz
recording, then select the release whose per-track lengths best fit the local durations
(minimise total absolute delta across all tracks). Log the lookup method and confidence
level so the user can see the basis for any trim decisions.

Trim decision logic per track (applied in remaster mode only):

```
excess = local_duration − mb_track_length

if excess > 10s:          warn + skip (version mismatch — don't trim on MB data)
elif excess > 0.5s:       trim to (mb_track_length − STANDARD_PREGAP)
                          # MB length includes original pre-gap; pipeline re-adds it
elif excess < −5s:        warn (local is significantly shorter — different edit?)
else:                     apply standard silence-threshold trim as normal
```

- [ ] Extend `metadata.py` to return per-track lengths alongside title/artist data
- [ ] Compute per-track `excess` after transcoding; store as trim target in pipeline state
- [ ] Pass trim targets into `silence.py`; treat as a hard floor (never trim past target)
- [ ] Log lookup method (disc ID / AcoustID / text search) and per-track trim decisions
- [ ] In master mode, skip this step entirely — audio is preserved as-is

---

## Audio Processing (deferred)

### Delivery mode audition (WIP — `src/cdda2img/audition.py`)

The loudness processing level is not user-selectable. The standard is fixed at −18 LUFS
(ReplayGain 2.0 / ITU-R BS.1770-3) and the only delivery choices are:

- **Unprocessed** — no loudness adjustment; clean archival audio
- **Normalised** — EBU R128 at −18 LUFS, audio modified, no tags
- **ReplayGain** — unmodified audio with REPLAYGAIN_* Vorbis tags; player applies gain

The audition tool allows the user to compare all three on the loudest 10-second passage
before committing. It is implemented as `src/cdda2img/audition.py` (run with
`uv run python -m cdda2img.audition <file>`) and will be integrated into the TUI as
a panel on the extract screen.

- [x] Find loudest 10-second window (peak-frame centring via PyAV + numpy)
- [x] Extract clip and prepare all three variants (PyAV + FFmpegNormalize + pyebur128)
- [x] Embed REPLAYGAIN_* tags in the RG variant (PyAV stream copy via `replaygain.embed_rg_tags()`)
- [x] Interruptible looping playback (ffplay subprocess, SIGSTOP/SIGCONT for pause)
- [ ] Integrate into TUI extract panel (replaces standalone CLI module)

### Master / Remaster modes
- [x] `--mode master` — silence trim disabled; transcode to Red Book spec only
- [x] `--mode remaster` (default) — silence trim enabled; `--loudness` controls RG
- [x] Fix `SILENCE_PAD_DUR = "1"` — corrected to `"2"` (Red Book 2-second inter-track gap)
- [x] Expose mode in the RBI header `flags` field (`FLAG_MASTER_MODE`, bit 2)

---

## Subprocess Elimination

Six `subprocess` calls exist across `replaygain.py` and `audition.py`, all invoking
`ffmpeg` or `ffplay`. The S603/S607 ruff warnings are suppressed as false positives
(trusted internal tool, not user input), but eliminating the subprocesses entirely
would improve portability, testability, and performance (no process spawn overhead).

Priority order: measurement (calls 1/2/5) → clip extraction (call 4) → tag
stream-copy (call 3) → playback (call 6).

---

### Call 1 & 2: EBU R128 measurement — `replaygain.py:_measure_single()` / `_measure_concat()`

Currently: `subprocess.run(["ffmpeg", "-af", "ebur128=peak=true", "-f", "null", "-"])`
and the N-file concat variant using `filter_complex`.

**Option A — `pyebur128`** (recommended)
Python bindings to `libebur128` (the reference C implementation). Correct true-peak
via 4× oversampled sinc interpolation. Workflow: decode with PyAV → feed sample arrays
to `pyebur128.Meter`. For album: feed all tracks sequentially to a single Meter instance
(no concat needed — `libebur128` accumulates state across `add_frames()` calls).
- Requires: `pyebur128` (pip) + `libebur128` (system package, e.g. `libebur128-dev`)
- Pro: reference-accurate true-peak; removes both measurement subprocesses
- Con: compiled extension + system library; slightly more setup than pure Python

**Option B — `pyloudnorm`**
Pure Python BS.1770 implementation. Workflow: decode with PyAV → numpy array →
`pyloudnorm.Meter.integrated_loudness()`. Album: concatenate numpy arrays.
- Requires: `pyloudnorm`, `numpy` (already present via PyAV)
- Pro: zero compiled dependencies
- Con: true-peak is sample peak only (no oversampling) — values in the RBI block
  will be slightly underestimated, which affects headroom calculations in players
  with hardware limiting

**Decision point**: `pyloudnorm` is fine for archival metadata (the error is small
and consistent), but `pyebur128` is the correct choice if true-peak accuracy matters.

- [x] Evaluate `pyebur128` availability and install story on target platforms
- [x] Replace `_measure_single()` and `_measure_concat()` with chosen library
- [x] Verify numerical agreement with current ffmpeg-based values on test files

---

### Call 3: FLAC tag stream-copy — `replaygain.py:embed_rg_tags()`

Currently: `subprocess.run(["ffmpeg", "-y", "-i", ..., "-c", "copy", "-metadata", ...])`.
Used because mutagen normalises all Vorbis comment keys to lowercase.

**Option A — PyAV stream copy** (preferred)
`av.open(out, "w").add_stream(template=in_stream)` creates a stream-copy mux path;
metadata is written via `out_c.metadata.update(tags)` which goes through libavformat
and preserves uppercase, exactly as the `extract_tracks()` path already does.
- Removes the subprocess and the temp-file/replace dance
- Needs empirical testing: FLAC stream copy in PyAV requires the STREAMINFO block to
  be handled correctly by the muxer; lossy formats are more commonly tested

**Option B — Direct Vorbis comment block manipulation**
Parse the FLAC file's `METADATA_BLOCK_VORBIS_COMMENT` block directly using Python
`struct` and rewrite it. Preserves exact case. No external dependencies.
- Very low-level; fragile if the block doesn't exist yet or needs to be created

Recommendation: try Option A first; fall back to Option B only if PyAV stream copy
proves unreliable for FLAC.

- [x] Probe PyAV FLAC stream copy: verify STREAMINFO is preserved and tags are uppercase
- [x] Replace `embed_rg_tags()` subprocess with PyAV stream copy if probe passes

---

### Call 4: Clip extraction — `audition.py:extract_clip()`

Currently: `_ffmpeg("-ss", start, "-t", duration, "-i", src, "-c:a", "flac", "-map_metadata", "-1", ...)`

**PyAV** (direct replacement, no new dependencies)
Seek to `start` using `container.seek(int(start / time_base))`, decode frames for
`duration` seconds, re-encode to FLAC via `add_stream("flac")` — the same pattern
used in `track_extract.py:_wav_bytes_to_flac()`. The only new piece is computing PTS
from wall-clock time using the stream's `time_base`.
- Removes subprocess; consistent with existing PyAV encode pattern in the codebase
- `-map_metadata -1` equivalent: simply don't call `out_c.metadata.update(...)`

- [x] Replace `extract_clip()` with PyAV seek + decode/encode; verify clip boundaries

---

### Call 5: EBU R128 in audition — `audition.py:compute_rg()`

Duplicate of calls 1/2 (single-file measurement, same ffmpeg invocation and stderr
parsing). Once calls 1/2 are replaced, this becomes a one-line call to
`replaygain.analyse([path])` and the duplicate implementation is deleted.

- [x] After calls 1/2 are replaced: replace `compute_rg()` with `replaygain.analyse()`

---

### Call 6: Audio playback — `audition.py:Player`

Currently: `subprocess.Popen(["ffplay", "-nodisp", "-loop", "0", ...])` with
SIGSTOP/SIGCONT for pause/resume. Requires `ffplay` to be installed separately.

**Option A — `sounddevice` + `soundfile`** (recommended)
Streaming callback model: `sounddevice.OutputStream` runs a callback that reads
chunks from a decoded buffer. Pause/resume implemented via `threading.Event` —
the callback blocks on the event when paused. Volume offset applied as numpy scalar
multiply. Looping: wrap read position back to zero.
- Pure Python (with C extension); PortAudio handles platform audio
- Removes the `ffplay` dependency
- More code than SIGSTOP/SIGCONT: need a callback, a thread-safe ring buffer or
  pre-loaded array, event management, and clean teardown
- `soundfile` reads FLAC natively; no PyAV decode needed for playback

**Option B — keep `ffplay` subprocess**
SIGSTOP/SIGCONT is genuinely elegant: OS-level freeze with zero CPU, instant resume,
no buffer management. Works perfectly on Linux (the target platform). The only cost
is a second binary requirement alongside `ffmpeg`.
- Pragmatic: `ffplay` ships with ffmpeg on most distros; not a real extra dependency
- Not portable to Windows (no SIGSTOP)

Given the project's Linux focus and the TUI integration planned for `audition.py`,
keeping `ffplay` is a reasonable choice until the TUI target platform is confirmed.
Replace with `sounddevice` if Windows support becomes a requirement.

- [ ] Decide: `sounddevice` callback player vs retain `ffplay`; defer until TUI work begins
- [ ] If `sounddevice`: implement `Player` class with `threading.Event` pause/resume

---

## TUI (deferred — implement after CLI is feature-complete)

Goal: a fixed-layout terminal UI (audio console view) wrapping the full CLI feature
set. Suggested library: **Textual** (async-native, rich widget set, good VU meter
support via `sparkline`/custom widgets).

Planned elements:
- Peak/RMS VU meter (real-time, updated during transcode/normalise)
- Track name and progress as each track is processed
- Current processing stage (transcode → trim → RG compute → pack)
- Album/artist, disc N/M, output target type
- Strategy and mode display
- Delivery mode audition panel (compare unprocessed / normalised / ReplayGain before
  committing to extract; see `src/cdda2img/audition.py` for the standalone prototype)

- [ ] Design layout and widget hierarchy (see `docs/TUI_Design.md`)
- [ ] Implement real-time progress feed from pipeline stages
- [ ] Implement VU meter widget (driven by PyAV decoded frames)
- [ ] Integrate audition panel into TUI extract screen

---

## RBI Format — ongoing evaluation

Continue evaluating the spec for improvements as the implementation matures.
Borrow ideas from other formats (CUE/BIN, MDS, CloneCD) where they address gaps.

- [x] Define `flags` bit 0 (`FLAG_RG_PRESENT`) and bit 2 (`FLAG_MASTER_MODE`)
- [ ] Define remaining `flags` bit assignments: CD-TEXT present, MCN present, AccurateRip verified
- [ ] Consider embedding AccurateRip checksums in the container (new optional block
  after PCM, signalled by a flag)
- [ ] Evaluate whether CD-TEXT block should be a separate optional section or
  encoded within the TOC text

---

## Research Pool

Maintain a local collection of CDDA reference material in `private/`.

Current holdings:
- `private/IEC_60908-1999.pdf` — Red Book standard (IEC 60908:1999, second edition)
- `private/libmirage/` — image format parser source (MDS, CCD, NRG, TOC, CUE, CD-TEXT coder)
- `private/spoons-audio-guide-cd-ripping.txt` — dBpoweramp Spoon's Audio Guide: drive
  features, copy protection, secure ripping practice
- `private/ABHOOD.md` — A Brief History of Optical Discs; comprehensive research notes
  including §5.4: CD Drive Technical Requirements for Accurate Dumping (Redump criteria)
- `private/NONSPEC.md` — Lead-in and lead-out: spec content, write offsets, copy-protection
  attacks, pre-mastering edge cases
- `private/OFE.md` — The Orange Forum Embargo: Orange Book paywalling and its implications
  for open-source tools

To add:
- [ ] AccurateRip protocol documentation (EAC forum posts / whipper source) — needed for
  computing and verifying AccurateRip v1/v2 checksums
- [ ] Drive offset database snapshot (AccurateRip or similar) — needed before implementing
  drive offset correction in the `r` subcommand

---

## Rust Reimplementation (future)

This Python codebase is a prototype. Once the design has stabilised — formats,
pipeline, metadata strategy, and TUI layout — implement a Rust version.

Design decisions taken in Python should be made with Rust portability in mind:
- Prefer explicit data structures over dynamic dispatch
- Keep I/O boundaries clear (parsing, processing, output are separate stages)
- Avoid Python-specific conveniences that have no clean Rust equivalent
