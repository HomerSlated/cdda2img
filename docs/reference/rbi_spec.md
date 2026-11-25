# RBI Format Specification
## Red Book Image — CD-DA Archive Container
### Version 6.0 · Format version `major=6, minor=0`

---

## 1. Introduction

RBI (Red Book Image) is an open, single-file container format for archiving and mastering Red Book standard (IEC 60908:1999) CD-DA audio discs. It stores a human-readable TOC, raw PCM audio, and optional metadata blocks in a single binary file, with BLAKE3 integrity verification for every block.

RBI is deliberately CD-DA-only. It does not attempt to represent raw physical sectors, subchannel data, copy-protection artefacts, or data tracks. This makes it unsuitable as a bit-for-bit clone format, but well-suited as a high-fidelity audio archive and mastering source.

### Backwards compatibility

**RBI v6.0 is a clean break — no backwards compatibility.** A v6.0 reader **MUST** reject any file whose `version_major != 6`. Unlike the v4→v5 transition (which kept a dual-checksum read path), v6.0 carries **no** compatibility shim: earlier containers are not migrated or read. The motivating change is the disc-level field rename (`disc_id` → `cdtext_catalog_ref`, §6.2) and the new catalogue fields (§6.3.1); a reader that accepted older files would have to recognise both field shapes indefinitely. (Historical: the fixed header was redesigned at v4.0 — 40 bytes vs. the 169-byte v3.0 header — around an extensible block-directory model.)

**Version history:**

| Version | Change summary |
|---------|---------------|
| v1.x    | Initial format; 121-byte fixed header |
| v2.0    | Header grew to 169 bytes; added RG block in TOC/PCM gap |
| v3.0    | Added per-track pre-gap storage and ISRC in TOC; breaking change from v2 |
| v4.0    | Redesigned around extensible block directory; provenance moved out of TOC; new PROV, ARIP, RLOG block types |
| v4.1    | Added optional ART block (embedded front-cover image, JPEG) and the `art_source` / `lookup_status_art` PROV keys; backwards-compatible minor bump |
| v5.0    | Block checksums changed from SHA-256 to BLAKE3; RLOG self-seal changed from `SHA-256:` to `BLAKE3:` label; PROV key `arip_dbar_sha256` renamed to `arip_dbar_b3sum`; breaking change from v4.x |
| v6.0    | Disc-level field `disc_id` renamed to `cdtext_catalog_ref` (in-memory; the cdrdao `DISC_ID` TOC keyword is unchanged); added `catalog_number` / `label` / `country` PROV keys (catalogue intelligence); clean break — no read shim for v5.x or earlier |

---

## 2. Comparison with Existing Formats

| Property              | CUE/BIN        | CCD/IMG/SUB     | MDS/MDF (Alcohol 120%) | NRG (Nero)      | **RBI**                     |
|-----------------------|---------------|-----------------|------------------------|-----------------|-----------------------------|
| File count            | 2             | 3               | 2                      | 1               | **1**                       |
| Container type        | Text + binary | Text + binary×2 | Binary + binary        | Binary (chunks) | **Binary**                  |
| Magic / signature     | None          | None            | `MEDIA DESCRIPTOR`     | `NERO`/`NER5`   | **`RBIMAGE\x00`**           |
| Endianness            | N/A           | N/A             | Little-endian          | Big-endian      | **Little-endian**           |
| Specification status  | De facto      | Proprietary     | Proprietary            | Proprietary     | **Open**                    |
| Integrity checking    | None          | None            | None                   | None            | **BLAKE3 (per block)**      |
| TOC format            | Plain text    | INI text        | Binary structs         | Binary chunks   | **Plain text (cdrdao-compatible)** |
| Audio storage         | Raw sectors   | Raw sectors     | Raw sectors            | Raw sectors     | **Raw PCM (s16le)**         |
| Subchannel data       | Optional      | Yes (.SUB)      | Optional               | No              | **No**                      |
| Multi-session         | Limited       | Yes             | Yes                    | Yes             | **No (CD-DA only)**         |
| CD-TEXT               | Yes           | Yes             | No                     | Yes (CDTX chunk)| **Yes (in TOC)**            |
| ISRC / MCN            | Yes           | Yes             | Yes                    | Yes             | **In TOC**                  |
| Max file size (index) | —             | —               | 4 GB (uint32 offsets)  | 8 GB (v2 uint64)| **Unlimited (uint64)**      |
| Extensible metadata   | No            | No              | No                     | No              | **Yes (block directory)**   |

---

## 3. File Structure Overview

```
┌─────────────────────────────────────────────────────────┐
│  FIXED HEADER (40 bytes)                                │
│    Magic (8) · Version (2) · Flags (4)                  │
│    Track count (1) · Disc number (1) · Disc total (1)   │
│    PCM parameters (6) · Dir offset (8) · Dir count (2)  │
│    Reserved (7)                                         │
├─────────────────────────────────────────────────────────┤
│  BLOCK AREA (variable, blocks in any order)             │
│    TOC block   — cdrdao-compatible plain-text TOC       │
│    PCM block   — raw interleaved audio (s16le)          │
│    PROV block  — provenance key=value text (optional)   │
│    RGDB block  — EBU R128 ReplayGain data (optional)    │
│    ARIP block  — AccurateRip results (optional)         │
│    RLOG block  — rip log text (optional)                │
│    ART  block  — front-cover image, JPEG (optional)     │
│    CTDB block  — CUETools DB results (optional, reserved) │
├─────────────────────────────────────────────────────────┤
│  BLOCK DIRECTORY (dir_count × 54 bytes)                 │
│    One entry per block: type_id, flags, offset, length, │
│    BLAKE3 checksum                                      │
└─────────────────────────────────────────────────────────┘
```

The fixed header contains the minimum information needed to locate and authenticate all data: audio parameters for direct playback, and a pointer to the block directory. The block directory is the authoritative manifest of all content; it is always appended after all blocks so that it can be written in a single pass. Directory entries **SHOULD** be ordered by ascending block offset, but readers **MUST** be prepared to process entries in any order.

Readers locate the directory by seeking to `dir_offset`, then read `dir_count` directory entries. All block locations and checksums are in the directory. Readers that encounter an unrecognised block type with `BLOCK_FLAG_SKIP` set **MAY** ignore it; readers encountering an unrecognised block type without `BLOCK_FLAG_SKIP` **MUST** reject the file.

---

## 4. Fixed Header

### 4.1 Binary layout

All multi-byte integer fields are **little-endian** unless otherwise noted.

| Offset | Size (bytes) | Type      | Field             | Description |
|--------|-------------|-----------|-------------------|-------------|
| 0      | 8           | bytes     | `magic`           | `RBIMAGE\x00` (0x52 0x42 0x49 0x4D 0x41 0x47 0x45 0x00) |
| 8      | 1           | uint8     | `version_major`   | Format major version; current value: `6` |
| 9      | 1           | uint8     | `version_minor`   | Format minor version; current value: `0` |
| 10     | 4           | uint32 LE | `flags`           | Feature bitmask (see §4.2); currently only `FLAG_MASTER_MODE` defined |
| 14     | 1           | uint8     | `track_count`     | Number of audio tracks (1–99) |
| 15     | 1           | uint8     | `disc_number`     | This disc's position in a set (1-based; `1` for single discs) |
| 16     | 1           | uint8     | `disc_total`      | Total discs in set (`1` for single discs) |
| 17     | 4           | uint32 LE | `pcm_sample_rate` | Audio sample rate in Hz; Red Book standard: `44100` |
| 21     | 1           | uint8     | `pcm_channels`    | Number of audio channels; Red Book standard: `2` |
| 22     | 1           | uint8     | `pcm_bit_depth`   | Bits per sample; Red Book standard: `16` |
| 23     | 8           | uint64 LE | `dir_offset`      | Byte offset from file start to beginning of block directory |
| 31     | 2           | uint16 LE | `dir_count`       | Number of entries in block directory |
| 33     | 7           | bytes     | `reserved`        | Must be `0x00 × 7`; reserved for future inline fields |

**Fixed header size:** 40 bytes

### 4.2 Flags

| Bit | Mask         | Name               | Description |
|-----|--------------|--------------------|-------------|
| 2   | `0x00000004` | `FLAG_MASTER_MODE` | Container was created in master mode (no silence trimming or inter-track gap was applied to the source audio). Affects pre-gap interpretation in TOC. |

All other bits are currently reserved, and a writer **MUST** set them to `0`. A bit's position says what a reader does when it does not recognise it: even-numbered bits mean "safe to ignore if not understood" (a reader **MAY** proceed and **SHOULD** warn); odd-numbered bits mean "must understand to read correctly", and a reader encountering an unknown odd-position flag **MUST** reject the file (rule 4). No odd-position flag is defined in this revision.

---

## 5. Block Directory

### 5.1 Location and structure

The block directory begins at `dir_offset` bytes from the start of the file. It consists of exactly `dir_count` consecutive entries, each 54 bytes. The directory is always written last, after all blocks; `dir_offset` is patched into the fixed header once all block offsets are known.

`dir_offset + dir_count × 54 == file_size` in all well-formed files.

### 5.2 Directory entry layout

| Offset | Size (bytes) | Type      | Field          | Description |
|--------|-------------|-----------|----------------|-------------|
| 0      | 4           | bytes     | `type_id`      | 4-byte ASCII block type identifier (see §5.4) |
| 4      | 2           | uint16 LE | `block_flags`  | Block-level flags (see §5.3) |
| 6      | 8           | uint64 LE | `offset`       | Byte offset from file start to first byte of block |
| 14     | 8           | uint64 LE | `length`       | Block length in bytes |
| 22     | 32          | bytes     | `checksum`     | BLAKE3 digest of block content (`length` bytes at `offset`); SHA-256 in v4.x |

**Directory entry size:** 54 bytes

### 5.3 Block flags

| Bit | Mask     | Name                 | Description |
|-----|----------|----------------------|-------------|
| 0   | `0x0001` | `BLOCK_FLAG_SKIP`    | A reader that does not recognise `type_id` **MAY** skip this block and proceed. |

All other bits are reserved and **MUST** be `0`. The required blocks (`TOC ` and `PCM `) **MUST NOT** set `BLOCK_FLAG_SKIP`. All optional blocks **MUST** set `BLOCK_FLAG_SKIP`.

A reader **MUST** reject a file containing a `type_id` it does not recognise that lacks `BLOCK_FLAG_SKIP` (§3, rule 32). For the block types this revision defines, a verifier checks `block_flags` against this section and §5.4 (rule 33). The flags on an *unrecognised* block are governed by rule 32 alone: the revision that defined the block may also have defined its flags.

### 5.4 Block type identifiers

| `type_id`    | Name             | Required | `BLOCK_FLAG_SKIP` | Description |
|--------------|------------------|----------|-------------------|-------------|
| `b"TOC "`    | TOC block        | Yes      | No                | cdrdao-compatible plain-text TOC (UTF-8) |
| `b"PCM "`    | PCM block        | Yes      | No                | Raw interleaved s16le audio |
| `b"PROV"`    | Provenance block | No       | Yes               | Rip provenance and extended metadata (key=value UTF-8) |
| `b"RGDB"`    | ReplayGain block | No       | Yes               | EBU R128 / ReplayGain 2.0 data (binary) |
| `b"ARIP"`    | AccurateRip block| No       | Yes               | AccurateRip verification results (binary) |
| `b"RLOG"`    | Rip log block    | No       | Yes               | Structured rip log text (UTF-8) |
| `b"ART "`    | Album-art block  | No       | Yes               | Embedded front-cover image, JPEG (binary; see §6.8) |
| `b"CTDB"`    | CUETools DB      | No       | Yes               | CUETools database results (RESERVED — format not yet defined) |

A conforming writer **MUST NOT** write more than one entry of a given `type_id`. A conforming reader **MUST** reject a file containing duplicate `type_id` entries for any required block (`TOC ` or `PCM `); for optional blocks, it **SHOULD** use the first entry and warn.

---

## 6. Block Definitions

### 6.1 TOC Block (`b"TOC "`)

The TOC block is a UTF-8 encoded plain-text file in cdrdao TOC format. It is self-contained and human-readable. Comments (`//`) **MAY** appear for human readability. Undefined `//` comments **MUST** be ignored by readers; all proprietary cdda2img metadata belongs in the PROV block. The one exception is the defined extension comment `// TRACK_TITLE_UNICODE:` (see §6.1.8), which readers **MUST** preserve and parse.

#### 6.1.1 Structure

```
CD_DA

CATALOG "0724383697724"

CD_TEXT {
  LANGUAGE_MAP {
    0: 9
  }
  LANGUAGE 0 {
    TITLE "<album title>"
    PERFORMER "<album artist>"
    DISC_ID "<label catalogue ref>"
  }
}

// Track 1
// TRACK_TITLE_UNICODE: "<original Unicode title as JSON string>"
TRACK AUDIO
NO COPY
NO PRE_EMPHASIS
TWO_CHANNEL_AUDIO
ISRC "GBAYE9300135"
CD_TEXT {
  LANGUAGE 0 {
    TITLE "<sanitised ASCII title>"
    PERFORMER "<track artist>"
  }
}
FILE "<album>.bin" MM:SS:FF MM:SS:FF

// Track 2 (with pre-gap)
TRACK AUDIO
NO COPY
NO PRE_EMPHASIS
TWO_CHANNEL_AUDIO
CD_TEXT {
  LANGUAGE 0 {
    TITLE "<track title>"
    PERFORMER "<track artist>"
  }
}
FILE "<album>.bin" MM:SS:FF MM:SS:FF
START MM:SS:FF

// Track 3
...
```

The `CATALOG` line is optional; included only when an MCN (Media Catalogue Number / EAN-13) is available. All-zeros MCNs are treated as absent and omitted.

The `ISRC` line is optional per track; included when an ISO 3901 ISRC code is available.

The `START` line is optional per track; present only for tracks that have a pre-gap.

The `COPY`/`NO COPY` and `PRE_EMPHASIS`/`NO PRE_EMPHASIS` lines record the track's
Q-channel CONTROL flags when the source captured them (§6.1.10); sources that cannot
capture them write the defaults `NO COPY` / `NO PRE_EMPHASIS`.

`INDEX` lines are optional per track; present only when the source captured INDEX ≥ 02
points (§6.1.10).

#### 6.1.2 Timestamp format

CD-DA frame timestamps use the format `MM:SS:FF` where:
- `MM` = minutes (00–79)
- `SS` = seconds (00–59)
- `FF` = frames (00–74); 1 frame = 1/75 second

The first `MM:SS:FF` in each `FILE` line is the start position within the PCM block (the start of the slot, which includes any pre-gap); the second is the total slot duration (pre-gap + audio). When no `START` line is present, the slot is entirely audio.

Conversion: `total_frames = MM × 75 × 60 + SS × 75 + FF`

#### 6.1.3 FILE reference

The filename in each `FILE` line uses the extension `.bin` and the sanitised album title as the stem (e.g. `"Led Zeppelin - IV.bin"`). The extension is an internal identifier only — the PCM block contains raw s16le audio, not a binary blob. On extraction, a WAV or FLAC file is reconstructed from the raw PCM using the audio parameters in the fixed header.

#### 6.1.4 ISRC

The `ISRC` line contains the ISO 3901 International Standard Recording Code (12 characters: country code 2, registrant 3, year 2, designation 5). Written immediately after `TWO_CHANNEL_AUDIO` and before the `CD_TEXT` block. Absent when the source did not provide an ISRC.

#### 6.1.5 Pre-gap storage

Tracks on a CD-DA disc may have a pre-gap: a period of silence (or, rarely, audio) preceding the track's INDEX 01 point. RBI stores pre-gap audio contiguously in the PCM block as part of the following track's slot.

For a track with a pre-gap of duration P frames and audio of duration D frames:

- `FILE` line: `start_timestamp` = PCM offset to the beginning of the slot (pre-gap start); `slot_duration` = P + D frames.
- `START` line: duration = P frames (the pre-gap length). This tells the reader where the audio starts within the slot.
- Audio-only offset: `audio_start_frame = start_frame + pregap_frames`
- Audio-only duration: `duration_frames = slot_frames − pregap_frames`

**Extraction rule**: when slicing PCM for a track, skip `pregap_frames` frames from `start_frame` before reading `duration_frames` frames of audio.

**Master mode**: pre-gaps are always preserved in the PCM block when `FLAG_MASTER_MODE` is set. The pre-gap bytes exist in the PCM at `start_frame * bytes_per_frame`.

#### 6.1.6 Character sanitisation

Track and album titles are sanitised before embedding:
- Curly quotes (`'`, `'`, `"`, `"`) → straight equivalents
- Em/en dashes (`—`, `–`) → hyphen-minus (`-`)
- Ellipsis (`…`) → three periods (`...`)
- Leading two-digit track number prefix (`01 `) stripped
- Remaining non-ASCII characters removed

Sanitisation is applied to both `TITLE` and `PERFORMER` values in disc-level and track-level `CD_TEXT` blocks.

#### 6.1.7 DISC_ID field

The `DISC_ID` field in the disc-level `LANGUAGE 0` block corresponds to CD-Text PTI `0x86` — a label catalogue reference string. It is optional; omitted when no value is available. Double-quote characters within the value are replaced with single quotes before embedding. The TOC keyword stays `DISC_ID` (cdrdao grammar); the in-memory `RBIDisc` field that carries it is named `cdtext_catalog_ref` as of v6.0 (it was `disc_id` in v5.x — renamed to remove the collision with the *MusicBrainz Disc ID* and `mb_release_id`). This is a label string, **not** the numeric MCN (`CATALOG`) and **not** the alphanumeric label catalogue number (`catalog_number`, §6.3.1).

Field ordering within `LANGUAGE 0`: `TITLE`, `PERFORMER`, `DISC_ID` (if present).

#### 6.1.8 TRACK_TITLE_UNICODE extension comment

The cdrdao TOC grammar constrains `TITLE` strings to ASCII (non-ASCII characters in titles are removed by sanitisation). When the original title contains non-ASCII characters, the original Unicode title is preserved via a defined extension comment immediately before the `TRACK AUDIO` line:

```
// TRACK_TITLE_UNICODE: <json-string>
```

where `<json-string>` is the original title encoded as a JSON string literal (double-quoted, with standard JSON escaping). This comment is present only when the original title differs from the sanitised ASCII title; it is absent for pure-ASCII titles.

Readers implementing cdda2img `x` (extract) **MUST** parse this comment and use the recovered Unicode title as the FLAC `TITLE` vorbis comment and output filename. Readers that do not implement extraction **MAY** ignore it.

Grammar (ABNF):
```
unicode-comment = "//" SP "TRACK_TITLE_UNICODE:" SP json-string
json-string     = DQUOTE *( json-char ) DQUOTE
```

#### 6.1.9 Canonical formatting rules

The `generate_toc()` function produces a canonical, deterministic TOC encoding. The same `RBIDisc` always produces identical bytes. Canonical rules:

1. **Encoding**: UTF-8, Unix line endings (`\n`), file ends with `\n`.
2. **Blank lines**: one blank line after the `CD_DA` line; one after `CATALOG` (if present); one after the disc-level `CD_TEXT` block; one blank line at the end of each track block.
3. **Indentation**: `LANGUAGE_MAP`, `LANGUAGE N`, and their closing `}` lines are indented 2 spaces inside `CD_TEXT {`; inner fields (`TITLE`, `PERFORMER`, `DISC_ID`) are indented 4 spaces.
4. **Disc-level field order**: `CD_DA` → `CATALOG` (if present) → `CD_TEXT { LANGUAGE_MAP ... LANGUAGE 0 { TITLE PERFORMER DISC_ID } }`.
5. **Track field order**: `// Track N` → `// TRACK_TITLE_UNICODE:` (if needed) → `TRACK AUDIO` → `COPY`|`NO COPY` → `PRE_EMPHASIS`|`NO PRE_EMPHASIS` → `TWO_CHANNEL_AUDIO` → `ISRC` (if present) → `CD_TEXT { LANGUAGE 0 { TITLE PERFORMER } }` → `FILE` → `START` (if present) → `INDEX` lines (if present).
6. **FILE start position**: always formatted as `MM:SS:FF` (e.g. `00:00:00` for track 1); never the bare integer `0`.

#### 6.1.10 Track CONTROL flags and INDEX points

Each audio track's Q-channel CONTROL flags may be recorded per track:

- `COPY` / `NO COPY` — the digital-copy-permitted flag (CONTROL bit value `0x2`).
- `PRE_EMPHASIS` / `NO PRE_EMPHASIS` — the 50/15 µs pre-emphasis flag (CONTROL bit
  value `0x1`). Readers aggregating a disc-level pre-emphasis signal (R14) **MUST**
  treat any track-level `PRE_EMPHASIS` as disc-level `True`.

Sources that capture the program-area Q stream (`c2read --sub raw` single-pass rips)
write the flags actually observed (majority-voted across the track's Q frames);
sources that cannot capture CONTROL (file-based `create`, foreign imports without
flag data) write the defaults `NO COPY` / `NO PRE_EMPHASIS`.

Additional index points (Q-channel INDEX ≥ 02) are recorded as `INDEX MM:SS:FF`
lines following the `START` line (cdrdao grammar): each timestamp is the offset of
the index transition **relative to the track's audio start** (the INDEX 01 point,
i.e. after any pre-gap). Index numbers are implicit and sequential: the first
`INDEX` line is INDEX 02, the second INDEX 03, and so on. Writers **MUST** emit
INDEX lines in ascending offset order; readers encountering INDEX lines **MUST**
preserve them across a parse → regenerate round-trip.

---

### 6.2 PCM Block (`b"PCM "`)

The PCM block contains raw interleaved audio samples with no file wrapper.

| Parameter     | Value                  | IEC 60908 reference       |
|---------------|------------------------|---------------------------|
| Format        | Raw interleaved PCM    | §7 Recording parameters   |
| Sample width  | 16-bit signed (s16le)  | §13 Channel bit rate      |
| Channels      | 2 (stereo, L then R)   | §3.1.1                    |
| Sample rate   | 44100 Hz               | §13                       |
| Byte order    | Little-endian          | —                         |

The PCM block contains only sample data — no RIFF header or chunk structure. Audio parameters needed to reconstruct a WAV file on extraction are stored in the fixed header. This ensures the block checksum is a pure integrity check over audio data.

#### 6.2.1 Geometry invariant

The PCM block length **MUST** equal the total geometry declared by the TOC block:

```
PCM.length == Σ (pregap_frames + duration_frames) × 2352
```

The block therefore always holds a whole number of CD frames. This binds the two
representations of one disc: every consumer — extraction, burning, ReplayGain
slicing, offset correction — addresses audio as `frame × 2352` from byte 0 of the
block (§6.1.3), so a PCM block that is longer than the TOC declares has a tail no
reader can reach, and one that is shorter sends every reader past its end.

**Track boundaries are frame-quantised, and this is a property of the disc, not of
the container.** IEC 60908 addresses tracks in MM:SS:FF, so an index point cannot
fall between frames. A writer assembling a disc from sample-exact sources (the
`create` path) **MUST** therefore resolve each boundary to a frame, and **MUST**
derive every boundary from the *absolute* cumulative sample position rather than
from a running sum of already-rounded durations — the latter accumulates its
rounding error along the disc, displacing later tracks without bound.

Two resolutions are conforming, and they differ in what they preserve:

| Strategy | Stored audio | Boundary error | Gapless |
|----------|--------------|----------------|---------|
| **Snap** — round each boundary to the nearest frame, pad only the final frame | Bit-identical to the source concatenation | ≤ ½ frame (6.7 ms), non-accumulating | Preserved |
| **Pad** — extend each track with silence to a frame boundary | Silence inserted at every join | Exact | Broken |

A writer **SHOULD** snap. Padding every track is conforming but displaces the disc
timeline by the inserted silence, which is audible across a continuous mix; it is
appropriate only when exact per-track content matters more than continuity.

---

### 6.3 PROV Block (`b"PROV"`)

The PROV block stores provenance and extended metadata that has no natural home in the standard cdrdao TOC format. It is UTF-8 encoded plain text: one `key=value` pair per line, terminated by `\n` (U+000A). Readers **MUST** split the block into lines on U+000A **only** — not on other Unicode line separators (U+000D, U+000B, U+000C, U+0085, U+2028, U+2029, …) — and **MUST** apply the value encoding of §6.3.4 before interpreting any pair. Lines beginning with `#` are comments and **MUST** be ignored by readers. A reader **MUST** ignore any key it does not recognise.

#### 6.3.1 Key reference

| Key                    | Description |
|------------------------|-------------|
| `creator`              | Tool and version that created the file, e.g. `cdda2img v0.2.0` |
| `created`              | Creation timestamp (ISO 8601), e.g. `2026-05-14T16:30:00Z` |
| `mode`                 | Workflow that produced the container: `r` (rip) \| `c` (create from files) \| `i` (import foreign image) |
| `source`               | Human-readable origin path, device node, or source description |
| `ripper`               | Extraction engine and the recovery exits that fired. `accudisc` for a rip, with `+ctdb` appended when a CTDB parity repair was committed, `+reslice` when the displacement check re-sliced a track from the capture, and `+c2rec` when the speed-ladder re-read spliced a track; any combination may appear. `file` (create), `ddp`, `toc`, `nrg`, `ccd` for the import sources. `cdrdao` and `cdparanoia` were valid values in containers written before 2026-07; readers **SHOULD** accept them and **MUST NOT** write them. |
| `drive_name`           | Human-readable drive name, e.g. `Plextor PX-716A` |
| `drive_read_offset`    | Read offset applied during rip, as a signed integer string, e.g. `+30` or `-6` |
| `drive_write_offset`   | Write offset for this drive (informational), e.g. `-30` |
| `low_dynamic_range`        | `YES` \| `NO`. Derived from EBU R128 album LRA against the user's configured threshold (default 5.0 LU). Absent when no loudness analysis was performed (`--loudness none`) |
| `original_release_found`   | `YES` when MB release-group lookup (primary) or title-fuzz fallback yielded a usable answer. Absent / unwritten = lookup found nothing — *not* a guarantee that no earlier release exists |
| `original_release_title`   | Title of the earliest known release of the same logical album. Present only when `original_release_found = YES`. Surfaced via the canonical rendering described in §6.3.2 |
| `original_release_year`    | Year of that earliest release as a 4-digit integer string; present only when `original_release_found = YES` |
| `release_date`             | Release date of this specific release (YYYY, YYYY-MM, or YYYY-MM-DD) |
| `catalog_number`           | The record label's own catalogue number for this release, e.g. `CID U2 6`. Distinct from `CATALOG` (the MCN/EAN-13 barcode in the TOC); this is the alphanumeric sleeve/spine catalogue number. Sourced from MusicBrainz (Discogs corroborates barcode only). |
| `label`                    | Record label / imprint name for this release, e.g. `Island Records`. From MusicBrainz. |
| `country`                  | Release country as an ISO-3166 alpha-2 code, or MusicBrainz pseudo-codes `XE` (Europe) / `XW` (Worldwide), or MusicBrainz's codes for countries that no longer exist (`XC`, `XG`, `SU`, `YU`, `CS`). From MusicBrainz, or from Discogs converted by `countries.discogs_country_code` (2026-11-21; Discogs gives names, "UK" for `GB`): a European multi-country region becomes `XE`, any other region or unknown name is left blank. Containers written before then may hold a Discogs name. |
| `barcode`                  | The service UPC/EAN barcode (normalised GTIN-13) sourced from MusicBrainz / Discogs — the **disambiguation key**. PROV-only: never written to the TOC or physical layer. Distinct from the on-disc MCN (`CATALOG`): a different identifier in a different namespace. The on-disc MCN never seeds a lookup; the barcode does (identifier_trust_model.md §1a). Absent when no service barcode was resolved. |
| `mcn_source`               | `disc` \| `barcode_derived`. Provenance of the TOC `CATALOG` (MCN): `disc` = read from the disc's Q-channel; `barcode_derived` = synthesised from `barcode` at finalisation (the MCN field is *defined* to hold the UPC/EAN, so a normalised barcode is its canonical content). Lets a reader tell a genuine MCN reading from a reconstruction. Absent when the disc carries no MCN and no barcode to derive one from. |
| `release_selected_via`     | When MB disc-ID returned >1 album-consistent pressing and the lexicographic release-selection rung picked one, the key that broke the tie: `barcode_plurality` \| `preferred_country` \| `date` \| `discogs_link` \| `mbid`. `discogs_link` (2026-11-21) prefers, among pressings still tied after `date`, one MusicBrainz links to a Discogs release, so the automatic pick is reproducible and can be archived (a rip needs one Discogs release) instead of resting on the alphabetical `mbid` sort: on ABBA *Gold* it picks 159d12a8 (Discogs 2758131) over 09cc6e86 (no link). It narrows the automatic pick only; the pressing menu still offers every tied candidate. Absent when no rung selection ran (single match, or an earlier ISRC disambiguator already won). The on-disc MCN is never a selection key (§1a). |
| `preferred_country_applied` | The `preferred_country` config value (comma-joined) that influenced `release_selected_via=preferred_country`. Records the user preference that shaped a config-dependent choice, for R10 reproducibility. Absent unless that key actually broke the tie. |
| `release_tied_after`       | `<rung>:<n>` — the last rung that actually narrowed the candidate set, and how many candidates were still tied when the terminal `mbid` sort arbitrated between them. `<rung>` is `none` when nothing above the terminal key narrowed anything. **Read this, not `release_selected_via`, to judge how the pressing was chosen**: `via` names the first rung on which the candidates *vary*, which is not the rung that *decides*, and reads identically whether the ladder determined the winner or merely narrowed to a tie. `:1` is a determined result; anything higher means the pressing is the alphabetically-first MBID among *n* indistinguishable candidates. Emitted alongside `via` rather than replacing it (containers written before 2026-08-06 carry only `via`). |
| `release_selection`        | `unique` \| `auto_tiebreak` \| `manual` \| `rejected`. How the pinned pressing came to be pinned. `unique` — one candidate, so nothing was chosen and nothing can be wrong. `auto_tiebreak` — several, and the terminal `mbid` sort picked; **the only value that is a guess**. `manual` — several, and the user chose from the alternatives menu with the disc in hand. `rejected` — several were shown and the user answered *none of these*, so the automatic pick stands but is explicitly unconfirmed. Four values because two cannot separate "no choice existed" from "an arbitrary choice was made", which are opposite provenance claims. Written at the point of selection, not inferred from `--auto` (the §10.4 gate can demote a run mid-disc). Absent when no release was pinned at all, and on the `create` pipeline, which has no disc-ID cascade. |
| `release_disambiguation`   | MusicBrainz's own free-text description of the selected pressing, verbatim (e.g. `WE 835, newer 'e above E' Elektra logo on disc`). Records *which physical object* the container claims to be, rather than only an MBID that a later MusicBrainz edit could redefine. Absent when MB describes the release with no disambiguation string. NB this is MB's lossy one-line summary; the fuller `annotation` (matrix / SID codes / plant) is what the alternatives menu displays, but it is not stored. |
| `corroborated_release`     | An MB release UUID. Emitted only when the user changed the pressing in the alternatives menu **and** a corroboration key (`acoustid_corroborates`, `discogs_corroborates`) is present: those checks run before the menu, against the release the ladder pinned, so after a manual change they describe a release the container no longer claims to be. Names the release they were actually taken against. Absent when the selection did not change or no corroboration ran. |
| `acoustid_gate`            | `failed`. Emitted only when the post-selection AcoustID gate ran and the disc audio did **not** corroborate the selected release's album (catches wrong disc-ID / TOC-collision / mispress). Absent on pass or when the gate did not run. A `failed` gate suppresses `--auto` auto-commit. |
| `discogs_corroborates`     | `YES` or `NO`. Whether the selected release's MB→Discogs url-relation resolved a Discogs release whose barcode **agreed** with MB's. Named to mirror `acoustid_corroborates`, so a reader comparing corroboration across sources meets one vocabulary rather than several (kgr, 2026-08-08). **Read the scope carefully**: this validates *the relation*, not the pressing — an MB editor supplied both the barcode and the link, so the two sides are not independent sources, unlike AcoustID's fingerprints. It detects a mis-linked relation; it is not cross-source confirmation of which pressing the disc is. Absent when the check did not run (no disc-ID match, no Discogs link/token, or no barcode on either side) — absence and `NO` are different answers. PROV-only; does not feed selection or confidence. Named `discogs_barcode_corroborates` before v6.0.1, then `discogs_link_barcode_agrees` until 2026-08-08. |
| `discogs_barcode_conflict` | `mb:<barcode>\|discogs:<barcode>`. The two barcodes behind a `discogs_corroborates=NO` — positive evidence that the relation points at the wrong release. Absent on agreement or when the check did not run. Named `discogs_link_barcode_conflict` until 2026-08-08. |
| `discogs_barcode_matches`  | Integer. How many releases the Discogs **barcode search** returned. A separate query from the url-relation above, and a separate question from either: 25 rows means Discogs answered richly and could not be narrowed to one. |
| `discogs_barcode_outcome`  | `applied` \| `ambiguous_<n>` \| `no_match` \| `album_mismatch` \| `no_barcode_to_search`. Why that search did or did not supply catalogue metadata. This is the **disambiguation** fact that `lookup_status_discogs` used to be standing in for. |
| `mb_release_id`            | MusicBrainz release UUID, e.g. `9d8f7a02-3851-4c49-9dc4-b08e7cb0ad7c` |
| `mb_release_group_id`      | MusicBrainz release-group UUID (used to re-run the original-release lookup from an existing RBI without redoing the disc-ID query) |
| `discogs_release_id`       | Discogs release ID (integer as decimal string) |
| `art_source`               | Origin of the embedded ART-block image, as `<source>:<scope>:<id>`. Values: `caa:release-group:<uuid>`, `caa:release:<uuid>`, `discogs:<id>`, or `file:embedded` (cover lifted from a source file's tags during `create`). Emitted only when an ART block is present. Records the **confirmed** (post-menu) identifier the embedded art was fetched against, so a reader can detect art-vs-metadata drift. |
| `duration_match_release`   | MusicBrainz release UUID, or `?` if the matched release carried no id. Emitted only when the stage-7 last-resort duration matcher fired — i.e. no higher source (CD-Text / MB disc-ID / Discogs / AcoustID / CDDB) identified the release in MB, and a text-search candidate's total duration matched the physical disc within tolerance. The lowest-trust identifier in the container; treat as a best guess pending user confirmation. |
| `multi_match_isrc_disambiguated` | `YES`. Present when MB disc-ID returned >1 match and the in-memory ISRC tally (R1) picked a strictly-winning candidate. Absent when N=1 (no disambiguation needed) or when N>1 and the tally was a tie / sub-threshold. |
| `arip_transport`           | `https` \| `http`. Emitted whenever at least one AccurateRip fetch attempt reached the server (any 2xx/4xx). A 5xx does **not** count as reaching it (the code recorded one until 2026-09-10, contradicting this row). `http` indicates the HTTPS attempt failed and the fetcher fell back to plaintext — readers SHOULD treat the confidence values with reduced trust. |
| `arip_dbar_b3sum`          | 64 lowercase hex chars. BLAKE3 of the raw dBAR response body (pre-parse). Emitted only when a body was actually received. Lets later re-fetches detect AR-side changes or mirror tampering without re-running verification. (`arip_dbar_sha256` was the name used in v4.x; the value is semantically equivalent but computed with BLAKE3.) |
| `recovery_passes`          | Integer (decimal string). Emitted only when re-read recovery ran (a track failed AR and recovery was enabled). **Since 2026-11-25 it is the setting `recovery_passes`** (`--recovery-passes`, else config `recovery_passes`, default 1; kgr merged `recovery_attempts` into it that day): how many times each recovery stage ran its profile's `passes` sweeps, inside one call and ended early by the first match, and on the `--ad-ladder` path how many times that ladder was walked. A sweep tries every target at every speed the profile reads at (for the speed ladder, one walk of the ladder; for a sector strategy, targets × rungs reads; for the span rung, one pass per rung; for `whole-disc`, one read per rung), and every shipped profile has `passes = 1`. It counts repetitions of whole sweeps, not reads: the reads that ran are in `recovery_attempts_track_<n>` (speed ladder) and `strategy_reads_track_<n>` (strategies). **Earlier meanings, for auditing older containers and failure reports:** from 2026-11-24 to 2026-11-25 it was the named (or built-in) profile's own `passes`, sweeps per attempt (1 for every shipped profile), and the repeat count was the separate key `recovery_attempts`. Before 2026-11-24 it was the configured number of full ladder sweeps attempted per failed track (total attempts per track = `recovery_passes` × ladder steps); **when `recovery_strategy` is present** (2026-11-21 to 2026-11-24) no ladder was walked and it was the strategy's maximum number of reads per track (for `whole-disc`, of whole-disc reads, shared by every failed track), and for the span rung its number of passes. |
| `recovery_attempts`        | Integer (decimal string). **Historical: written only from 2026-11-24 to 2026-11-25**, when the recovery chain ran: how many times each stage ran its profile's `passes` sweeps (`--recovery-attempts`, else config `recovery_attempts`, default 1). Merged into `recovery_passes` on 2026-11-25 (kgr), which carries this meaning since; neither the option nor the config key exists any more. It counts repetitions of whole sweeps, not reads, and is not to be confused with `recovery_attempts_track_<n>`, the speed ladder's per-read record, which keeps its name. |
| `recovery_ladder`          | Comma-separated drive read speeds, in the order each recovery pass walks them, e.g. `40X,32X,24X,8X,4X`: fastest first for a profile's ladder, as written for `--ad-ladder`. Emitted alongside `recovery_passes`. **Containers written before 2026-10-22 walked this list in reverse**, because the loop reversed whatever it was given: the ascending lists written until 2026-07-25 (`4X,8X,…`) were walked fastest first, and the descending lists written after it were walked slowest first. For such a container, read the attempt order from the reversed list. **When `recovery_strategy` is present** (2026-11-21 on) this list was not walked: it is the speeds the strategy could use. `max-variation` drew one at random for each read; every other strategy read at the first and only speed. The speed of each read is in `strategy_reads_track_<n>`. |
| `recovery_ad_flags`        | The `--ad-*` recovery flags the user supplied, as sorted, comma-separated `<flag>=<value>` pairs, e.g. `ladder=32,16,8`. Emitted alongside `recovery_passes` when those flags chose the recovery (`recovery_source=ad-flags`). `--ad-speed` is never listed: it sets the capture pass's speed and composes with any profile. **Audit note (2026-11-25):** a container written before 2026-11-25 can also name `retries`, `c2`, `verify`, `overlap` or `recovery`. Those flags were accepted and recorded here, but **no read ever received them**: the reads ran with the engine's defaults. They were deleted on 2026-11-25 (kgr; under the re-read ownership ruling those settings are AccuDisc's). |
| `recovery_track_<n>`       | Per failed track `<n>`, which recovery exit produced the audio in the container. `ctdb_repaired@<entry_id>` = a CTDB parity repair was committed after passing both the CTDB per-track CRC and the AccurateRip gate, with **zero extra reads**. `matched@<speed>X` = the speed-ladder re-read at that rung produced an AccurateRip-matching track, spliced in sample-exactly; `<speed>` is the speed **requested**, and the speed the drive ran at is in `recovery_attempts_track_<n>`. `strategy_matched@a<k>` = a sector or whole-disc recovery strategy (`recovery_strategy`) re-read part of the disc, spliced the read into a working copy of the track in memory, and the track matched AccurateRip after its read `<k>` (the `<k>`-th entry of `strategy_reads_track_<n>` names the sectors and the speed); written only after that match. `span_matched@p<k>` = the flagged-span rung (profile `granularity = "span"`, opt-in) re-read only the sectors the capture pass flagged, overlaid its accepted copies onto the track in memory, and the result matched AccurateRip after pass `<k>`; written only after that match. `displaced@<±N>` = the displacement check found the track's audio intact in the capture but `<N>` samples from its place, and re-sliced it with **zero extra reads**; `+N` means `N` samples were lost from the stream before this track's audio (it arrived early), `-N` that `N` were repeated (it arrived late). `matched@<speed>X,displaced@<±N>` = a speed-ladder re-read at that rung was itself displaced by `<N>` samples, and the track was cut from the re-read at that displacement; it records that the displacement survived a fresh seek. `unrecovered` = no exit succeeded and the **original audio was kept unaltered** — an unverified splice is never written. **A rip written on or after 2026-11-21 never carries `unrecovered`**: a track that does not verify aborts the rip and nothing is written (kgr: "any outcome other than 100% AR verified tracks should not result in an RBI being written"); the record goes to the failure report instead. Emitted only for tracks that failed the initial AR check. |
| `recovery_attempts_track_<n>` | Per track `<n>` the speed ladder re-read, every attempt in the order it ran, comma-separated. Each attempt is `<requested>><honoured>:<cache_defeat>`, e.g. `40>40:done,32>24:failed,4>4:done`. `<requested>` = the rung's speed, as a multiplier. `<honoured>` = the speed the drive reported adopting for that read (AccuDisc `ReadStats.speed_honoured_x`), or `?` when the engine cannot report it or the drive did not answer; it differs from `<requested>` when the drive quantized the request. One value per attempt is exact rather than a summary: a ladder re-read sets one speed and AccuDisc runs no speed ladder of its own inside it. `<cache_defeat>` = `done` (AccuDisc read one sector 5000 sectors away before the first transfer, so the attempt was not answered from the drive's cache), `near` (AccuDisc 0.51.0 and later: the far read ran, but closer than 5000 sectors, because the read's session left no room that far away; any read that reaches the disc empties the PX-716A's and the LITE-ON's cache, so this is a defeat, recorded so it can be told apart), `failed` (asked for and did not run: the drive refused the far read, or the session held no sector outside the span, and the read went ahead as if never asked, so it may have been answered from the cache), or `off` (the engine has no such option: AccuDisc before 0.49.0), or `declined` (since 2026-11-25: AccuDisc's read policy chose no defeat, because no far target fits the session, `recovery_policy` reason `defeat_no_target`). Since 2026-11-22 every far read, the one asked for and the engine's own before its retries and verify passes, stays inside the session being read (AccuDisc's `readable` range); before then a far read could land between the sessions of an Enhanced CD. `<requested>:error` = the read raised and returned no audio; it still used one of the `recovery_passes` × ladder-steps attempts. The last attempt of a recovered track is the one that matched, and its `recovery_track_<n>` names the requested speed; an `unrecovered` track lists every attempt. Absent = the speed ladder did not re-read that track: it passed AccurateRip, an earlier exit recovered it, recovery did not run, or a recovery strategy ran instead (`recovery_strategy`; its reads are in `strategy_reads_track_<n>`). Containers written before 2026-10-25 never carry it, and nothing in them records which attempts ran (read `recovery_ladder` for the order, reversed before 2026-10-22). |
| `recovery_chain_track_<n>` | Since 2026-11-21, when no profile was named (`recovery_source=builtin`): the automatic recovery chain's stages for track `<n>`, in the order they ran, comma-separated, each `<stage>:hit` or `<stage>:miss`, e.g. `span-flagged:miss,max-variation:hit`. The stages are the shipped profiles. The order comes from `recovery_damage_track_<n>`: none flagged = `track-ladder,track-constant`; 1 to 5 flagged = `span-flagged,max-variation,track-ladder,track-constant,sector-runup,span-fixed,sector-hammer`; more than 5 = `track-ladder,track-constant,span-flagged,max-variation,sector-runup,span-fixed,sector-hammer`; then one `whole-disc` stage shared by every track still failing. The chain stops at the first hit. `recovery_track_<n>` names how that stage matched. Every read is recorded under the stage's own keys, suffixed `__<stage>` with hyphens as underscores (e.g. `strategy_reads_track_3__max_variation`, `recovery_attempts_track_3__track_ladder`). |
| `recovery_drive_work`      | `reads:<n>,seeks:<n>,speed_changes:<n>`. Rip only, since 2026-11-25, whenever re-read recovery ran: the drive work cdda2img asked for across the whole recovery, counted **before** each read is sent (`drive_work.py`). `reads` = recovery read calls (a ladder attempt, a strategy read, a span re-read); `seeks` = the far reads it asked AccuDisc for (`cache_defeat`); `speed_changes` = reads whose requested speed differed from the previous recovery read's (the first counts). It does **not** include the engine's own re-reads inside a call (error retries, Q rescue, verify passes and their far reads). Those are in `recovery_engine_work` (AccuDisc 0.57.0 and later). |
| `recovery_engine_work`     | `cmds:<n>,speed_changes:<n>,max_read_cmds:<n>,reported_reads:<n>`. Rip only, since 2026-11-25, whenever re-read recovery ran on AccuDisc 0.57.0 or later: the engine's **own** recovery work inside cdda2img's recovery reads, which `recovery_drive_work` does not include. Summed over every recovery read that returned, from AccuDisc's `ReadStats.budget_spent_cmds` and `budget_spent_changes`. `cmds` = commands the engine issued to recover (error fallback, Q rescue, C2 rescue, consensus, anchoring, verify witnesses, and every far read except the head far read of `cache_defeat`, which `recovery_drive_work` counts as a seek); `speed_changes` = its spindle speed changes inside those reads, except the head speed set (counted in `recovery_drive_work`); `max_read_cmds` = the most any single read spent; `reported_reads` = how many reads returned these counts. A read that raised returns none, so where `reported_reads` is below `recovery_drive_work`'s `reads` the totals are a lower bound. The capture is never included (its own counts are `capture_*`). Absent = the engine predates 0.57.0 (never written as 0) or no recovery read returned. **Enforced since 2026-11-25** (kgr: every AccuDisc 0.53-0.59 feature on): each recovery read's engine gets what is left of config `recovery_max_engine_cmds` (default 10000) and of `recovery_max_speed_changes`, which the engine's speed changes share with ours (one spindle, one limit), as AccuDisc's per-read budget; a spent limit stops recovery (`recovery_budget_hit`). |
| `recovery_budget_hit`      | `reads:<limit>` \| `speed_changes:<limit>` \| `engine_cmds:<limit>` (since 2026-11-25: config `recovery_max_engine_cmds`, the engine's own recovery commands; `speed_changes` then counts the engine's changes as well as ours). Since 2026-11-25: the per-rip drive-work budget (config `recovery_max_reads`, default 5000; `recovery_max_speed_changes`, default 1000; 0 = no limit) stopped recovery before the next read was sent (kgr: protect the drive from a command storm). Every later stage and track was skipped (`<stage>:budget` / `budget` in `recovery_chain_track_<n>`). Since a track left unverified aborts the rip, this normally appears only in a failure report. |
| `recovery_policy`          | Since 2026-11-25 (kgr: "I would rather do my tests against 0.59.0 with all new features enabled"), whenever re-read recovery ran: what AccuDisc's read policy (`accudisc_read_policy`, 0.58.0) chose for the recovery reads, one entry per distinct choice, comma-separated, each `<purpose>:r<revision>:<reasons>`, e.g. `gated_reread:r2:engine_recovery_pending_ab,span_rescue:r2:shift_search_pending_ab`. `<purpose>` = `gated_reread` (the speed-ladder and strategy re-reads, AccurateRip-gated) or `span_rescue` (the flagged-span rung); `<reasons>` = the policy's `R_*` reason bits, lower case, `+`-joined (`none` when it set none). The policy owns every setting that decides what a read does (C2, subchannel, cache defeat, verify passes, overlap, the rescue ladder, `rescue_by_rung`, `shift_search`, the per-read budget); cdda2img adds only its sink, maps and cancel flag, and the overrides in `recovery_policy_overrides`. A reason bit describes the table's choice, so read it together with the overrides: `shift_search_pending_ab` beside `span_rescue:shift_search=on` means the table left it off and cdda2img turned it on. **Auditing older containers:** before 2026-11-25 no recovery read went through the policy. |
| `recovery_policy_overrides` | `<purpose>:<field>=<value>`, comma-separated, e.g. `span_rescue:shift_search=on`. Since 2026-11-25: where cdda2img overrode AccuDisc's read-policy table for this rip's recovery reads, written only when a read of that purpose ran. Revision 2 of the table leaves `shift_search` off for `SPAN_RESCUE` pending a hardware A/B; kgr turned every new feature on for his tests. |
| `recovery_damage_track_<n>` | `<flagged>:<class>`, e.g. `3:low`. How many sectors of track `<n>` the capture pass flagged (its damage lane, the disc's final sector excluded), and the class that picked the chain's order: `none` (0), `low` (1 to 5, the bench's threshold, kgr 2026-11-21) or `high` (more than 5). Written with `recovery_chain_track_<n>`. |
| `recovery_speed`           | `<requested>X><used>X`, e.g. `32X>32X`. The speed the chain's fixed-speed stages read at: those whose profile says `ladder = "single"` (shipped: `track-constant`, `span-fixed`), at config or `--recovery-speed`, snapped to the nearest rung the drive admitted on this disc. A stage whose profile says `ladder = "full"` reads at a different rung on each attempt, stepping through the admitted ladder. Written when the chain ran, which since 2026-11-21 is every rip with recovery: no profile named runs every stage, and `--profile X` runs stage `X` alone. With `recovery_speed = "auto"` it is `auto` and each track's speed is in `recovery_speed_track_<n>`. |
| `recovery_speed_track_<n>` | `<speed>X:<reason>`, e.g. `24X:c2=3` or `4X:no_c2_data`. Written only with `recovery_speed = "auto"` (2026-11-21): the speed track `<n>`'s fixed-speed stages held. `c2=<k>` = the rung whose speed-ladder re-read of this track left the fewest C2-flagged sectors, `<k>` of them (ties to the slower rung); `no_c2_data` = no such re-read had run (a restricted chain, or a drive without C2), so the slowest admitted rung was used. |
| `recovery_c2_track_<n>`    | `<speed>X:<flagged>` per speed-ladder re-read of track `<n>`, comma-separated, e.g. `40X:12,32X:3,24X:5`: how many of its sectors each re-read flagged with a C2 error. Written only when `recovery_speed = "auto"` asked the ladder for C2 (2026-11-21); in the chain it carries the `__track_ladder` suffix. |
| `recovery_strategy`        | `sector-hammer` \| `sector-runup` \| `span-fixed` \| `max-variation` \| `whole-disc`. The recovery strategy that ran **instead of** the speed ladder, derived from the profile's fields (`granularity`, `variation`, `run_up`, `span`) rather than its name, so a user profile that shadows a shipped one is described by what it did. Each is one of the recovery bench's read plans (`cdda2img.strategy_reads`, shared with `tools/strategy_bench.py`): `sector-hammer` re-reads a flagged sector alone; `sector-runup` starts `run_up` sectors before it; `span-fixed` starts `span` sectors before it; `max-variation` picks a random flagged sector, a random run-up (0, 8, 32, 128 or 512 sectors) and a random tail (2, 16 or 64) on every read; `whole-disc` re-reads `[0, lead-out)` once per pass and tests every failed track against that one read. A strategy stands alone: a track it does not recover is not handed to the speed ladder. Every read asks AccuDisc for a cache defeat except `whole-disc`'s, whose far target would lie past the lead-out (its reads record `off`; before 2026-11-21 evening they asked, and recorded `failed`). Emitted only when a strategy ran. Containers written before 2026-11-21 never carry it: before then these profiles ran as the speed ladder while `recovery_profile` named them. **Since the chain (2026-11-21) every strategy's reads step through the admitted ladder, one rung per read**, except `span-fixed`, which reads at `recovery_speed`: a speed that is itself the cause of a failure would otherwise be repeated on every read (kgr). **Since 2026-11-24 `sector-hammer` and `sector-runup` (and `span-fixed` under a profile with `ladder = "full"`), which take a track's flagged sectors in turn, step once per round of those sectors** (kgr), so every target is read at every rung and the targets of a round share its speed. Between 2026-11-21 and 2026-11-24 they stepped on every read while the target advanced too, so with n targets on an L-rung ladder target j only met rungs j, j + n, ... (L/gcd(n, L) of them; five targets on five rungs: one speed each). Both periods are auditable from `strategy_reads_track_<n>`, which records every read's sector and speed. |
| `recovery_seed`            | Integer (decimal string). The seed of the random choices `max-variation` made, so its reads can be regenerated. Emitted only with `recovery_strategy=max-variation`. |
| `strategy_targets_track_<n>` | Integer. How many sectors the capture pass's damage lane flagged inside track `<n>`, the sectors a sector strategy aims at. `0` means it had none to aim at (no damage map, or nothing flagged) and every read was placed at the track's first sector, as on the bench. Emitted for every track a sector strategy tried; not for `whole-disc`. |
| `strategy_reads_track_<n>` | Every read a recovery strategy made for track `<n>`, in the order it ran, comma-separated: `<start>+<count>@<requested>><honoured>:<cache_defeat>`, e.g. `15624+531@32>32:done`. `<start>` and `<count>` are raw LBAs and sectors (before the read-offset correction); `<requested>`, `<honoured>` and `<cache_defeat>` are as in `recovery_attempts_track_<n>`. `<start>+<count>@<requested>:error` = the read raised and returned no audio. For `whole-disc` one read serves every failed track and appears in each track's list. The last read of a recovered track is the one that matched. |
| `displacement_check`       | `found` \| `none`. Emitted only when the displacement check ran, i.e. on a partial AccurateRip mismatch of a rip. The check costs no reads: it looks for a shift of up to 2939 samples at which a failed track's audio verifies, tests only against the dBAR blocks that already matched a verified track of this disc at shift 0 (so another pressing's offset cannot pass as a displacement), and writes a track only on a full-checksum match there. `found` = at least one track was re-sliced (see `recovery_track_<n>`). `none` = no failed track verified at any shift, which is what real damage looks like; the absence of the key means the check did not run. A drive that drops or repeats a few samples mid-stream returns every later byte intact and in the wrong place: C2 raises no flag, every later track fails AccurateRip, and CTDB sees damage beyond its capacity (measured 2026-10-10: 12 samples, six tracks). |
| `zero_read_repairs`        | `skipped`: the rip ran with `--profile-only` (a temporary test switch, 2026-11-22), so neither zero-read repair ran: no CTDB parity repair (no `ctdb_*` keys) and no displacement check (no `displacement_check`), and every track that failed AccurateRip went to the recovery profile. Every track still verified, or nothing would have been written. Absent = both repairs were available as usual. |
| `span_target_lbas_track_<n>` | The raw LBAs the flagged-span rung targeted on track `<n>`, as comma-separated ascending ranges, `<lba>` or `<first>-<last>`, e.g. `112502,113058` or `113040-113047,113052`. Since 2026-11-25 (kgr), so two runs can be shown to have met the same damage. Written with `span_targets_track_<n>` whenever there were targets. |
| `span_reads_track_<n>`     | Every span read on track `<n>`, in the order it ran, comma-separated: `<start>+<count>@<speed>X:<stages>:slips=<s>[/<off>]`, e.g. `113054+9@40X:far=1/witness=1:slips=0`, or `<start>+<count>@<speed>X:error` when the read raised. `<speed>` is the speed asked for, `0` = the drive's own (no ladder). `<stages>` = AccuDisc's own operations inside that read, per stage, non-zero only, `/`-separated (`ReadStats.cmds`, 0.53.0; `none` when every count is zero, `?` on an older engine): `far`, `witness`, `consensus`, `anchor`, `qpos`, `fallback`, `c2_rescue`, `speed`. `slips` = displaced copies the engine detected (`ReadStats.slips`), `/<off>` how many of them were not a whole CIRC frame (`slips_off_frame`, 0.56.0). Since 2026-11-25 (AccuDisc 2026-11-25ae): the slip test, and so `shift_search`, acts only where a witness copy disagrees, and every such sector goes to consensus, so **`consensus` > 0 marks a read in which `shift_search` was exercised**; a read with only `far` and `witness` had nothing for it to judge. |
| `span_targets_track_<n>`   | `<sectors>/<spans>`. The flagged-span rung ran on track `<n>`: how many sectors the capture pass's damage lane flagged inside the track's raw window, and how many padded spans they clustered into. `0/0` = nothing was flagged, so the rung had nothing to re-read and the track fell through to the speed ladder (a slip the capture pass cannot see leaves no flag). |
| `span_unresolved_track_<n>` | Integer. The flagged-span rung gave up on track `<n>`, and this many target sectors never received a copy that passed the acceptance rule (state `OK`/`RECOVERED`, own C2 block all zero, not position-suspect, read with `verify_passes >= 2`). |
| `span_accepted_unverified_track_<n>` | Integer. The rung's falsifier: **every** target sector on track `<n>` received at least one accepted copy and the assembled track **still** failed AccurateRip, so the acceptance rule accepted at least one wrong copy. The value is the target count. Nothing was written; the track fell through to the speed ladder. Target sectors are never retired before the AccurateRip gate passes, so "the target set emptied" is not a reachable state and this key is defined on accepted copies instead. |
| `span_declined`            | Why the flagged-span rung did not run although the profile asked for it: `no_damage_map` (the capture pass produced no damage lane), `no_engine_support` (the binding lacks `caller_map_buffers`), `no_ar_responses`, `recovery_disabled` (`recovery_passes = 0`). The speed ladder still ran as usual. |
| `ctdb_declined`            | Reason a CTDB repair was attempted and rejected, e.g. `disc not in CTDB`, `no CTDB entry reconciles with this rip`, `ctanalyse failed`. Present only when the attempt was made and did not commit; its absence means either no attempt or a success. A declined repair used to leave no trace at all, which made "we never tried" and "we tried and it failed" indistinguishable after the fact. |
| `ctdb_entry`               | The CTDB entry id the repair was reconciled against. Emitted on success and on a decline that got as far as selecting an entry. |
| `ctdb_offset`              | Signed sample offset (e.g. `-639`) at which the rip reconciled with that CTDB entry. |
| `ctdb_erasures`            | `c2` when the C2 bitmap was supplied to the decoder as erasures (roughly doubling reconstruction capacity). Absent when the repair ran error-only. |
| `acoustid_corroborates`    | `YES` \| `NO`. Emitted only when the pre-menu AcoustID helper (R6) ran (i.e. `acoustid_lookup.is_available()` was true and at least one per-track fingerprint produced a chained MB recording). `YES` = AcoustID's consistent-across-tracks winner agrees with the disc's existing MB release MBID; `NO` = disagrees. |
| `pre_emphasis`             | `YES` \| `NO`. Aggregate disc-level pre-emphasis flag. `YES` if any track has CONTROL bit 0 set, `NO` otherwise. Absent when not captured by the source parser; populated by the subchannel assembly on a rip and by the TOC parser on an import. |
| `disagreement_cddb_mb`     | Comma-separated list of fields where CDDB and MB returned different answers, after NFC + casefold + reissue-suffix allow-list normalisation. Possible values: `album`, `artist`, or `album,artist`. Absent when both agree, when one side is blank, or when the pre-MB artist was the literal `Unknown Artist` default. |
| `original_release_corroborated` | `discogs,mb`. Emitted when both the Discogs master `main_release.year` and the MB RG `first-release-date` were resolvable and agreed on the same 4-digit year. |
| `original_release_disagreement` | `discogs:YYYY\|mb:YYYY`. Emitted when both years resolved and disagreed. The disc's stored `original_release_year` reflects the *earlier* of the two. |
| `lookup_status_cddb`       | `OK` \| `empty` \| `down` \| `disabled`. `OK` = response had data; `empty` = service reached but returned nothing; `down` = network or parse error; `disabled` = service not attempted (R10 offline mode). **A rip written on or after 2026-11-21 always records `OK`**: `empty`, `down` and `disabled` abort the rip and write nothing (kgr: a service that answers "nothing found" has failed to provide data the archive needs; the exact query and answer go to the failure report). |
| `lookup_status_mb`         | As `lookup_status_cddb`, for the MusicBrainz disc-ID lookup. `empty` means MusicBrainz answered 404 (it does not know the disc ID). `down` means any other outcome that is not a result, with the cause in `lookup_error_mb`; the ISRC-tally and duration-match fallbacks are then skipped, since they query the same service. |
| `mb_identified_via`        | `disc_id` \| `isrc_tally` \| `duration_match`. Which MusicBrainz path identified the release: the disc-ID lookup, the ISRC-tally fallback (R4) after a disc-ID miss, or the stage-7 duration match after both. Absent = MusicBrainz identified nothing. `lookup_status_mb` describes the disc-ID lookup alone, so a disc-ID miss that a fallback then resolved reads `empty` there and is identified here. Added 2026-11-21. **A rip written on or after that date always carries it**: a rip whose MusicBrainz identified nothing aborts and writes no container. |
| `lookup_error_mb`          | Present only with `lookup_status_mb=down`: the HTTP status code MusicBrainz answered with (e.g. `503`, `400`), or `network` when no HTTP response arrived (DNS failure, refused connection, timeout). A 5xx is a service problem; a 4xx other than 404 is a defect in the request, and the code keeps the two apart. |
| `lookup_status_discogs`    | As `lookup_status_cddb`, for Discogs: **did Discogs reply**, nothing more. `OK` when either Discogs query returned anything — the barcode search or the MB→Discogs link follow. `disabled` covers the absence of a `DISCOGS_TOKEN`. Corrected 2026-08-08: it previously reported whether a Discogs hit was *merged into the disc*, which under fill-blank merge semantics reads `empty` precisely when the rest of the metadata is good — so the better the record, the more likely the status lied. A container could carry `lookup_status_discogs=empty` beside `discogs_corroborates=YES`, the latter having fetched a Discogs release in the same run. The merge/disambiguation outcome now has its own keys (`discogs_barcode_matches`, `discogs_barcode_outcome`). **A rip written on or after 2026-11-21 always records `OK`**: `empty`, `down` and `disabled` abort the rip and write nothing (kgr: a service that answers "nothing found" has failed to provide data the archive needs; the exact query and answer go to the failure report). |
| `discogs_second_look`      | The MusicBrainz release ID (or `barcode`) Discogs was asked about a second time, after the ISRC tally or the duration match identified the release (2026-11-21). Discogs is first asked straight after the disc-ID lookup, by barcode; when that lookup found nothing there is no barcode yet, and without the second look a release a fallback identified would abort the rip for want of Discogs data. Found by replaying real discs (`tools/archival_replay.py`): ABBA *Gold* via the duration match, Toca under `--digital-media` via the ISRC tally. Absent = the first look sufficed. |
| `lookup_status_acoustid`   | As `lookup_status_cddb`, for AcoustID. `disabled` covers R10 offline mode, the absence of an `ACOUSTID_API_KEY`, and missing pyacoustid / libchromaprint. **A rip written on or after 2026-11-21 always records `OK`**: `empty`, `down` and `disabled` abort the rip and write nothing (kgr: a service that answers "nothing found" has failed to provide data the archive needs; the exact query and answer go to the failure report). |
| `lookup_status_art`        | As `lookup_status_cddb`, for the album-art fetch. `OK` = an image was retrieved and embedded; `empty` = no source carried cover art; `down` = network/decode error; `disabled` = R10 offline mode (no live fetch attempted — a cover already embedded from source-file tags may still be present). **A rip written on or after 2026-11-21 always records `OK`**: `empty`, `down` and `disabled` abort the rip and write nothing (kgr: a service that answers "nothing found" has failed to provide data the archive needs; the exact query and answer go to the failure report). |
| `lookup_status_accuraterip` | `OK` \| `empty` \| `down`. Rip only. `OK` = the dBAR was fetched and describes this disc; `empty` = AccurateRip answered but the disc is not in its database (or no block matched the disc's IDs); `down` = **no usable answer** — a network failure on both transports, or only server errors (5xx). `down` means *not verified*, and this key is the only record of that state: the ARIP block is omitted (§6.5), because status `0` (`NOT_IN_DB`) is an answer AccurateRip gave, never a stand-in for one it did not. Added 2026-09-10 — before it, an unreachable AccurateRip was stored as `NOT_IN_DB`, printed as "disc not found in database", and CTDB repair and re-read recovery were skipped without a word. **A rip written on or after 2026-11-21 always records `OK`**: `empty` and `down` abort the rip and nothing is written (`empty` is investigated as our own fault first; every earlier case was). Under `media_override=digital_media` it is `disabled`: no disc-ID database can hold such a disc, and `digital_media_consensus` replaces it. |
| `subq_q_valid` | `<valid>/<sectors>`. Rip only, and written even for an empty capture (`0/0`, which counts as collapsed). The count of sectors whose subchannel Q frame passed its CRC-16, over every sector the read captured. A value near zero is a **structural** failure, not a disc property: Q is CRC-checked per sector and cannot pass by accident, so a collapse means the subchannel was sliced from the wrong bytes. That happens with a record stride the reader did not expect or a non-standard field order, and the audio sliced by the same arithmetic may be misframed with it. Writers treating it as a verification failure use a threshold of 7.5%. cdda2img wrote the container and exited 4 below it until 2026-11-21; since then such a rip, or one whose Q stream could not be anchored (`subq_layout=unanchored`), aborts and writes nothing, so a rip container written after that date always has a usable Q count. A misframed read measures 0% or 1/n for an n-sector transfer chunk. A value above zero but low is not proof of bad audio. The lowest yield measured with good audio is 13.43%, on a read while another process was using the same drive. Without contention, specific read-speed requests crater whole-pass Q to about 33–39% (RECOVERY.md), and ordinary reads measure 98–99.9%. This threshold answers a different question from RECOVERY.md's ~90% gate, which discards a disturbed **pass** for re-reading; this one flags a read whose subchannel was sliced from the wrong bytes. Added 2026-09-14. |
| `capture_speed_honoured` | `<n>` (a multiplier, decimal string). Rip only, since 2026-11-25: the speed the drive reported adopting for the capture pass (AccuDisc `ReadStats.speed_honoured_x`), which can differ from the speed asked for when the drive quantizes the request. Absent when the engine cannot report it or the drive did not answer (never written as 0). `--digital-media` uses it as the reference its verifying re-reads must avoid (`digital_media_consensus`). |
| `capture_cmds`             | `<stage>:<n>`, comma-separated, stages with a non-zero count, e.g. `stream:1484,fallback:6,qpos:24,far:30,speed:2`; `none` when every count is zero. The engine operations the capture pass issued, per stage (`ReadStats.cmds`). Rip only, since 2026-11-25 (kgr: record which mechanism did the capture's work), with AccuDisc 0.53.0 or later (feature `effort_stats`); absent on an older engine, never written as zeros. The capture asks for none of the engine's opt-in recovery, so on a normal rip only these stages can appear: `stream` (the transfers themselves), `fallback` (single-sector reads after a failed transfer, `retries` each), `qpos` (the always-on Q-position rescue), `far` (the far reads before each re-attempt) and `speed` (the speed set and read-backs). Whether the rescue catches a damaged sector varies from pass to pass on the same disc (AccuDisc 2026-11-25ad), so compare runs on these, not only on whether a track later passed AccurateRip. |
| `capture_cmds_failed`      | As `capture_cmds`, for the operations that failed (`ReadStats.cmds_failed`), e.g. `fallback:4,qpos:16`; `none` when none failed. |
| `capture_ms`               | As `capture_cmds`, for the time each stage took, in milliseconds rounded down (`ReadStats.us`), e.g. `stream:451200,qpos:3810`; `none` when every stage took under 1 ms. |
| `capture_won_by`           | `<origin>:<n>`, comma-separated, origins with a non-zero count, e.g. `first:347180,fallback:2,qpos:26`; `none` when every count is zero. Whose bytes the capture delivered for each sector (`ReadStats.won_by`): `first` = the first transfer, `fallback` / `qpos` = that rescue's re-read. Says whose copy was kept, never whether it is right; AccurateRip decides that. |
| `capture_speed_changes`    | Integer. Spindle speed changes during the capture pass (`ReadStats.speed_changes`), the head speed set included when it changed the speed. |
| `capture_sectors_recovered` | Integer (decimal string). Rip only, since 2026-11-25. AccuDisc's own count, from the capture pass, of sectors where it saw a problem and a re-read copy won (`ReadStats.sectors_recovered`). The capture asks for the raw subchannel, so the engine's **always-on Q-position rescue** runs on it, with no setting to turn it off: a sector whose CRC-valid Q names another LBA, widened to ±4 sectors within its chunk, is re-read up to 4 times, each after a far read (AccuDisc 2026-11-25a, A.1). **Relative evidence only**: the engine settled the sector against its own re-reads; AccurateRip has not judged it. Absent when the engine does not report the counter, and from every container written before 2026-11-25. |
| `capture_sectors_suspect` | Integer (decimal string). Rip only, since 2026-11-25. Sectors of the capture pass the engine could not settle and delivered best-effort (`ReadStats.sectors_suspect`), e.g. a Q rescue whose re-reads never named the LBA asked for. One input of the engine's "completed with caveats" verdict. Absent as for `capture_sectors_recovered`. |
| `capture_subq_misposition` | Integer (decimal string). Rip only, since 2026-11-25. Sectors of the capture pass whose CRC-valid ADR-1 Q frame named an LBA other than the one read (`ReadStats.subq_misposition`): the triggers of the Q rescue, not counting the ±4-sector margin it widens to. Not the same quantity as `subq_frames`'s `slip`, which counts Q frames cdda2img's own layout pass discarded from the **delivered** stream, after any rescue. Absent as for `capture_sectors_recovered`. |
| `ar_total_miss` | `offset_mismatch` \| `no_offset_verifies` \| `offset_probe_failed`. **Historical: no container written since 2026-11-21 carries it**, because cdda2img now aborts such a rip and writes no container at all (an archive with no verified provenance is pointless; kgr). The diagnosis goes to stderr and the failure report instead; the exit status was 5 on 2026-11-21 and is 3 since the exit codes were consolidated the same day. The rest of this row describes containers written before then. Rip only, and present only when the disc **is** in AccurateRip and no track verified. `offset_mismatch` is written only when at least one offset is **confirmed** by a whole-track checksum, and is then accompanied by `ar_offset_candidates` (comma-separated read offsets that verify, best first, at most four) and `ar_offset_suggests` (the first candidate minus the offset used). A confirmed candidate does not prove the offset wrong: a disc AccurateRip holds in several pressings verifies at several offsets. Before 2026-09-14 `offset_mismatch` could be written on an unconfirmed probe hit, so older containers carrying it are not evidence of any offset. |
| `network_preflight` | `ok` \| `degraded:<service>[,<service>…]`. Written by `create`, `rip` and `import`: the result of the network check run **before** any audio is read, copied or transcoded. Each `<service>` is one of `musicbrainz`, `accuraterip`, `ctdb`, `coverart`, `acoustid`, `discogs`, `cddb`, and the list names every probed service that did not answer. A container naming a **required** service here (rip: `accuraterip`, `musicbrainz`; create and import: `musicbrainz`) was written only because the user chose to continue at the prompt or passed `--allow-offline`. It describes reachability at the start of the run only — a failure after that point is recorded by the per-service `lookup_status_*` keys. Added 2026-09-10. Since 2026-11-21 `rip` requires every contributing service (all but CTDB), and a required one that is down stops the run before the disc is read, so a rip container's value lists at most `ctdb`. |
| `media_type`               | `pressed` \| `recordable` \| `unknown`. Rip only, since 2026-11-21: what the disc is, from AccuDisc's two signals. `recordable` = the drive's current profile is CD-R (0x09) or CD-RW (0x0A), **or** the disc carries ATIP (pre-groove identity, physically present on recordable media and kept through finalising). `pressed` = profile CD-ROM (0x08) **and** no ATIP. `unknown` = anything else: no profile, or the ATIP read raised. A rip refuses `recordable` and `unknown` unless the user says what the disc is (`media_override`). |
| `media_evidence`           | The two signals behind `media_type`, e.g. `profile=0x0009,atip=present` or `profile=0x0008,atip=absent`; `profile=none` when the drive reported none, `atip=error:<text>` when the read raised. |
| `media_override`           | `cdr` \| `digital_media`. The user's word for a disc whose `media_type` is not `pressed`. `cdr` = a recordable copy of a pressed CD (or a disc whose type could not be determined): the normal pipeline runs, and AccurateRip must still verify every track. `digital_media` = a disc made from a digital-only release, which no disc-ID database can hold: AccurateRip, CTDB, CDDB and the MusicBrainz disc-ID lookup are skipped, the audio is proved by `digital_media_consensus`, the release is identified by `digital_release` and `discogs_release`, and every track by `acoustid_tracks_matched`. Absent = a pressed disc. |
| `digital_media_policy`     | `r<revision>:<reasons>` per verifying re-read, comma-separated, e.g. `r2:engine_recovery_pending_ab`. Since 2026-11-25, with `digital_media_consensus`: what AccuDisc's read policy chose for each re-read (`REFERENCE_VERIFY`: the rung, C2, no subchannel, one cache defeat, no engine recovery), in the vocabulary of `recovery_policy`. Each re-read is AccuDisc's `compare_read` (0.59.0), compared sector by sector with the capture. |
| `digital_media_consensus`  | `<n>@<speeds>:<c2>`, e.g. `2@capture,32X:clean` (`capture` = the whole-disc pass at its own speed). Under `media_override=digital_media`, the proof the audio was read right, in place of AccurateRip: `<n>` full reads at the listed admitted rungs, all byte-identical. `<c2>` = `clean` (no read flagged a C2 error, so two reads suffice) or `unavailable` (the drive returned no C2 map, so a third read at a third rung was required). A disagreement aborts the rip. Since 2026-11-25 each listed speed is the one the drive **reported** using (`capture_speed_honoured` for the capture), and no two reads share one: each re-read takes the nearest admitted rung no earlier read used (below, else above; AccuDisc's `REFERENCE_VERIFY` rule), and a drive that cannot give enough distinct speeds, or that reads a re-read at a speed already used, aborts the rip. **Auditing older containers:** before then a drive with one admitted rung re-read at the capture's own speed, a third read reused the second's rung, and the listed speeds are the ones requested. |
| `acoustid_tracks_matched`  | `<k>/<n>`. Under `media_override=digital_media`, how many tracks' AcoustID fingerprints matched a recording on the identified release (its release group). Always `<n>/<n>` in a written container: any unmatched track aborts the rip. |
| `digital_release`          | Under `media_override=digital_media`, the MusicBrainz release this disc was made from, or why there is none: `none` (neither the ISRC tally nor the duration match identified a release), `conflict:<a>\|<b>` (they named different releases), `not_digital:<id>` (MusicBrainz does not call every medium of it `Digital Media`: the disc is a copy of a download, so a CD release of the album is the wrong record), `down:<id>` (MusicBrainz did not answer; `digital_release_error` holds the reason). Always an MBID in a written container: anything else aborts the rip (2026-11-21). The same id is the container's `mb_release_id`; its Discogs release is `discogs_release`. A fallback's id is stripped on a pressed disc, because it cannot tell one pressing from another; a digital release has no pressings and can never have a disc ID, so it is kept. |
| `digital_release_format`   | The medium formats MusicBrainz records for that release, joined with `+` (`?` where it records none). |
| `digital_release_error`    | Why MusicBrainz gave no answer about the release (`MBLookupError.reason`: an HTTP status or `network`). Present only with `digital_release=down:<id>`. |
| `discogs_release`          | The one Discogs release this disc is, or why there is none (kgr, 2026-11-21: a rip requires a single Discogs release). Set by the barcode search when it found exactly one release naming the album, else through the MusicBrainz release's Discogs link: from the release matched by **disc ID** on a pressed disc (a fallback's release is not proven to be the pressing, C2), or from the digital release under `media_override=digital_media`. A barcode is shared by many pressings (Tracy Chapman 72, Eliminator 23, ABBA *Gold* 121 on Discogs), so the link is what usually decides. Reasons: `<discogs_barcode_outcome>+no_link`, `unfetched:<id>` (Discogs did not return it), `album_mismatch:<id>` (it names another album; punctuation is ignored), `disabled`. Re-resolved after the menu when the user picks another pressing. Always an id in a written rip container; it is the container's `discogs_release_id`. |
| `discogs_release_via`      | `barcode` \| `mb_link` \| `mb_link_after_menu` (the user picked another pressing and its link was followed) \| `menu` (the user set the Discogs release). |
| `discogs_release_barcode`  | The barcode the single Discogs hit was found by, when `discogs_release_via=barcode`; a pressing chosen in the menu keeps that release only if it carries the same barcode. |

All keys are optional. A writer **SHOULD** emit at minimum `creator` and `created`. A reader **MUST NOT** fail on a missing key.

Leading and trailing whitespace in values is significant and **MUST** be preserved.

#### 6.3.4 Value encoding (escaping)

PROV is an integrity surface: a reader or auditor treats each `key=value` pair as an authentic provenance record. Free-text values (e.g. `source`, `original_release_title`) may originate from remote metadata and could otherwise contain a literal newline, which — written verbatim — would forge additional pairs (e.g. a fabricated `mb_release_id=`). To make pairs unforgeable, the line-structuring characters are backslash-escaped.

On **write**, in both the key and the value, the writer **MUST** apply these substitutions in order:

1. `\` (U+005C) → `\\`
2. newline (U+000A) → `\n` (backslash, `n`)
3. carriage return (U+000D) → `\r` (backslash, `r`)

The `\`-first ordering makes the transform unambiguous (a literal backslash in the input cannot collide with an introduced escape).

On **read**, after splitting on U+000A and partitioning each raw line on its first `=`, the reader **MUST** unescape the key and value by scanning left to right: `\\` → `\`, `\n` → U+000A, `\r` → U+000D. A backslash followed by any other character (or a trailing backslash) is preserved literally as the backslash plus that character. (Partitioning before unescaping is safe because no escape sequence produces a `=`.)

Because the only line terminator is U+000A and U+000A is always escaped inside an encoded pair, no value can introduce a spurious line break: the escaping is complete with respect to the line grammar. `=` is **not** escaped — values may contain `=` (the split takes the first `=` only), and keys are drawn from the controlled set in §6.3.1 and never contain `=`.

#### 6.3.2 Release intelligence

The pair `low_dynamic_range` and `original_release_found` replaces the v3-era `remastered` enum. Rationale: `remastered` conflated two unrelated questions (provenance and loudness), and both questions were being answered by heuristic guesses. The v4 fields each carry a single, factual signal:

- `low_dynamic_range` is a *measurement* — we computed the album LRA and compared it to a user-set threshold. A value of `YES` does not imply "loudness war remaster"; it states only that the source is heavily compressed, which can be an artistic choice on an original release (cf. ZZ Top *Eliminator*, 1983).
- `original_release_found` is a *lookup result* — when present and `YES`, MB's release-group endpoint identified at least one strictly earlier release of the same logical album. Absent means the lookup did not produce a usable answer, not that this disc is the original.
- `original_release_corroborated` / `original_release_disagreement` are the sub-goal-3 disagreement surfaces. When *both* are absent and `original_release_found=YES`, only one source (MB RG primary or title-fuzz fallback) was usable — treat with v4.0's existing confidence semantics.

**Canonical rendering.** Every surface that displays the original-release fields (the
interactive metadata menu, the `list`/`--info` dump, and the catalogue browser) renders a
single identical string produced by one core function (`rbi_format.format_original_fields`):

```
Original: <Yes|No|Unknown>, <this release|<earlier title>|unknown release> (<year>|unknown year)
```

- Field 1 answers "is THIS disc the original release?" and is **gated by the disc's own
  year** (parsed from `release_date`). Without a disc year nothing can be claimed to predate
  it, so the answer is `Unknown` and the earlier-release fields collapse to `unknown` too —
  a paradoxical "Unknown, <title> (<year>)" is never emitted.
- Comparison is at **year granularity**: a 1983 pressing is the original even when the
  release-group's first-release date is `1983-03-23`. A same-year pressing renders
  `Yes, this release (<year>)` regardless of any title mismatch (e.g. a reissue suffix).
- The `list` view has no separate album line, so when `original_release_found` is absent
  but `release_date` is present it falls back to a bare `Released:  <date>` — the only place
  a `list` dump surfaces the disc's own release date. The menu and catalogue show that year
  elsewhere and emit nothing extra in that case.

**Canonical disc-metadata rendering (v6.0).** The v6.0 catalogue fields (`catalog_number`,
`label`, `country`) and the already-stored `release_date` are surfaced through a single core
formatter (`rbi_format.format_disc_metadata`, which subsumes `format_original_fields` for the
original-release line). Three invariants bind the three display surfaces — the interactive
metadata menu, the `list`/`--info` dump, and the catalogue browser:

1. **stored ⟺ displayed** — the rendered field set is exactly the set persisted to PROV /
   catalogue, in both directions: no field is shown that is not stored (closing the prior
   "shown-then-discarded" gap for `catalog_number`), and no stored field is hidden (surfacing
   `release_date`, previously persisted but shown only on `list`).
2. **same set in all three** — all three surfaces call `format_disc_metadata`; divergence is
   not possible without editing the one function.
3. **identical format / spacing / order** — defined once in the formatter.

These invariants govern the **disc-level header block** only. The per-track table (#, Title,
Duration, ISRC) is out of scope: only the menu and `list` render it; the catalogue summary does
not, and that asymmetry is intentional. Canonical field order: Album (+ this-release year from
`release_date`) · Artist · Label · Country · Catalogue no. (`catalog_number`) · MCN (`CATALOG`) ·
Original release · Tracks.

#### 6.3.3 Lookup status and conflict surfaces

The §6.3.1 keys split into two semantic groups:

- **Identifier keys** (`mb_release_id`, `mb_release_group_id`, `discogs_release_id`, `release_date`, `low_dynamic_range`, `original_release_*`, …): each names a single, factual fact about the disc.
- **Observation keys** (`lookup_status_*`, `disagreement_*`, `*_corroborated`, `*_disagreement`, `acoustid_corroborates`, `multi_match_isrc_disambiguated`, `arip_transport`, `arip_dbar_b3sum`): each records *what we asked and what the answer was*.

For observation keys, **presence implies the question was asked**. This lets a verifier distinguish "blank-because-no-data" from "blank-because-service-offline". A blank `mb_release_id` paired with `lookup_status_mb=down` is "we asked MB and the network was down"; the same blank paired with `lookup_status_mb=empty` is "we asked MB and MB had nothing".

Two value-grammar notes for parsers:

- `original_release_disagreement` uses a compound value with a pipe separator and `source:YYYY` segments — the only PROV value that requires parsing beyond plain string. Format: `discogs:YYYY|mb:YYYY`.
- `disagreement_cddb_mb` is a comma list of field names; consumers should split on `,` and treat the result as a set.

---

### 6.4 RGDB Block (`b"RGDB"`)

The RGDB block stores EBU R128 / ReplayGain 2.0 loudness metadata. All float32 values are IEEE 754 single-precision, little-endian.

#### 6.4.1 Binary layout

| Offset       | Size (bytes) | Type      | Field             | Description |
|--------------|-------------|-----------|-------------------|-------------|
| 0            | 1           | uint8     | `rgdb_version`    | RGDB block format version; current value: `1` |
| 1            | 4           | float32 LE| `rg_reference`    | Reference loudness in LUFS; ReplayGain 2.0 standard: `−18.0` |
| 5            | 4           | float32 LE| `album_gain`      | Album gain in dB |
| 9            | 4           | float32 LE| `album_peak`      | Album true peak, linear scale |
| 13           | 4           | float32 LE| `album_range`     | Album loudness range (LRA) in LU |
| 17           | 4×N         | float32[] | `track_gain[N]`   | Per-track gain in dB; N = `track_count` |
| 17 + 4N      | 4×N         | float32[] | `track_peak[N]`   | Per-track true peak, linear scale |
| 17 + 8N      | 4×N         | float32[] | `track_range[N]`  | Per-track LRA in LU |

**Total block size:** `17 + 12 × N` bytes, where N = `track_count` from the fixed header.

Track arrays are 0-indexed; `track_gain[0]` corresponds to track 1 in the TOC.

#### 6.4.2 Loudness measurement

RG values are computed using the EBU R128 / ITU-R BS.1770-3 integrated loudness algorithm with true peak detection. `rg_reference` is nominally −18.0 LUFS (ReplayGain 2.0). Gain values represent the adjustment required to reach the reference: `gain = rg_reference − integrated_loudness`.

---

### 6.5 ARIP Block (`b"ARIP"`)

The ARIP block stores AccurateRip verification results for the disc.

The block is **omitted** when AccurateRip could not be reached (`lookup_status_accuraterip=down`, §6.3.1). An absent block therefore means *not verified* (or a container that was never ripped); a present block whose tracks carry status `0` means AccurateRip **answered** that the disc is not in its database. Writers **MUST NOT** use status `0` to record a query that went unanswered — the two are indistinguishable once sealed, and they call for opposite actions (re-rip later versus nothing to verify against).

#### 6.5.1 Binary layout

**Block header (13 bytes):**

| Offset | Size | Type      | Field          | Description |
|--------|------|-----------|----------------|-------------|
| 0      | 1    | uint8     | `arip_version` | ARIP block format version; current value: `1` |
| 1      | 4    | uint32 LE | `disc_id1`     | AccurateRip disc ID 1 |
| 5      | 4    | uint32 LE | `disc_id2`     | AccurateRip disc ID 2 |
| 9      | 4    | uint32 LE | `cddb_id`      | CDDB disc ID (used in AccurateRip URL) |

**Per-track entry (15 bytes × N, N = `track_count`):**

| Offset | Size | Type      | Field             | Description |
|--------|------|-----------|-------------------|-------------|
| 0      | 4    | uint32 LE | `v1_crc`          | Computed AccurateRip v1 CRC; `0` if not in DB |
| 4      | 4    | uint32 LE | `v2_crc`          | Computed AccurateRip v2 CRC; `0` if not in DB |
| 8      | 2    | uint16 LE | `v1_confidence`   | Count of AR submissions matching v1 CRC; `0` if no match |
| 10     | 2    | uint16 LE | `v2_confidence`   | Count of AR submissions matching v2 CRC; `0` if no match |
| 12     | 2    | uint16 LE | `db_total`        | Total AR submissions for this track; `0` if not in DB |
| 14     | 1    | uint8     | `status`          | Verification status (see §6.5.2) |

**Total block size:** `13 + 15 × N` bytes.

#### 6.5.2 Status codes

| Value | Name         | Meaning |
|-------|--------------|---------|
| `0`   | `NOT_IN_DB`  | Disc not found in AccurateRip database |
| `1`   | `MISMATCH`   | Disc found in database but computed CRC matches no entry |
| `2`   | `OK`         | Computed v2 CRC (or v1 if v2 unavailable) matches a database entry |

---

### 6.6 RLOG Block (`b"RLOG"`)

The RLOG block stores the complete structured rip log as UTF-8 text. The format follows the whipper log convention: human-readable, one logical section per topic, machine-parseable by line prefix. The final line is a BLAKE3 self-seal (see §6.6.2; v4.x used SHA-256).

#### 6.6.1 Log structure

```
Log created by: cdda2img <version>
Log creation date: <ISO 8601 datetime>

Ripping phase information:
  Drive: <drive name>
  Extraction engine: accudisc <version>
  Read offset correction: <N>
  Gap detection: <method>

CD metadata:
  Artist: <artist>
  Title: '<album title>'
  CDDB Disc ID: <hex>
  MusicBrainz Disc ID: <base64url>

TOC:
  <track number>:
    Start: <MM:SS:FF>
    Length: <MM:SS:FF>
    Start sector: <N>
    End sector: <N>
  ...

Tracks:
  <track number>:
    Peak level: <float>
    Extraction quality: <float> %
    Test CRC: <8 hex chars>
    Copy CRC: <8 hex chars>
    AccurateRip v1:
      Result: <Found, exact match | Found, no match | Disc not present in database | Not verified (AccurateRip unreachable)>
      Confidence: <N>
      Local CRC: <8 hex chars>
      Remote CRC: <8 hex chars>
    AccurateRip v2:
      Result: <...>
      Confidence: <N>
      Local CRC: <8 hex chars>
      Remote CRC: <8 hex chars>
    Status: <Copy OK | ...>
  ...

Conclusive status report:
  AccurateRip summary: <All tracks accurately ripped | N/M tracks accurately ripped | Disc not present in AccurateRip database | Not verified (AccurateRip unreachable)>
  [Health status: <No errors occurred | N errors occurred>]
  EOF: End of status report

BLAKE3: <64 lowercase hex chars>
```

Sections are present only when the relevant data is available (e.g. AccurateRip sections are absent when the disc is not in the database).

The `Health status:` line is **optional** and **MUST** be present only when the writer has a measured read-error count to report. A writer with no such count **MUST** omit the line rather than assert `No errors occurred`. cdda2img currently carries no read-error count into the log, so it omits the line. (Until 2026-09-14 it wrote `No errors occurred` unconditionally; logs sealed before then carry that line with nothing measured behind it, and a reader **MUST NOT** treat it as evidence of a clean read.)

#### 6.6.2 BLAKE3 self-seal

The final line **MUST** be exactly:
```
BLAKE3: <64 lowercase hex chars>
```
The hash is computed over all preceding bytes of the RLOG block (everything before this final line, including the preceding `\n`). A reader verifying the log integrity strips the last line, computes BLAKE3 of the remainder, and compares against the stored hex string.

The self-seal is independent of the block-level checksum in the directory entry. The directory checksum covers the entire RLOG block (including the BLAKE3 line) and protects against accidental corruption. The in-log BLAKE3 hash protects against deliberate modification after insertion. (Historical: v4.x used a `SHA-256:` self-seal label. The v6.0 reader accepts only `BLAKE3:` — earlier containers are rejected at the version gate, §1, so no dual-label handling is needed.)

---

### 6.7 CTDB Block (`b"CTDB"`) — Reserved

The `CTDB` type identifier is registered to store CUETools Database (CTDB) verification results. The binary layout is not yet defined. Implementations **MUST** set `BLOCK_FLAG_SKIP` on any `CTDB` block they write. Implementations **MUST NOT** write a `CTDB` block until the format is defined in a future revision of this specification.

---

### 6.8 ART Block (`b"ART "`)

The ART block stores a single embedded front-cover image for the disc. Added in
format v4.1; a v4.0 reader treats it as an unrecognised block and skips it
(`BLOCK_FLAG_SKIP` is mandatory). At most one ART block may be present.

The stored image is **always JPEG** and is kept at the **source resolution** of
the fetched cover (the largest derivative available) — no downscaling is applied
on the way in. Consumers that need a smaller image (terminal preview, a per-track
FLAC `PICTURE` block) downscale a working copy at use time; the block itself is
the full-quality master.

#### 6.8.1 Binary layout

**Block header (10 bytes):**

| Offset | Size | Type      | Field          | Description |
|--------|------|-----------|----------------|-------------|
| 0      | 1    | uint8     | `art_version`  | ART block format version; current value: `1` |
| 1      | 1    | uint8     | `image_format` | Image codec: `1` = JPEG (the only value defined in v4.1) |
| 2      | 2    | uint16 LE | `width`        | Image width in pixels; `0` = unknown / not recorded |
| 4      | 2    | uint16 LE | `height`       | Image height in pixels; `0` = unknown / not recorded |
| 6      | 4    | uint32 LE | `image_length` | Length of `image_data` in bytes |

**Image payload:**

| Offset | Size           | Type  | Field        | Description |
|--------|----------------|-------|--------------|-------------|
| 10     | `image_length` | bytes | `image_data` | Encoded image bytes (JPEG when `image_format == 1`) |

**Total block size:** `10 + image_length` bytes.

`image_length` is redundant with the directory entry's `length` field
(`length == 10 + image_length`); it is stored explicitly so the payload is
self-describing and a reader can validate the two against each other.

#### 6.8.2 Format and provenance

A conforming writer **MUST** store the image as JPEG (`image_format == 1`),
transcoding from any other source format on ingest, and **MUST** set
`BLOCK_FLAG_SKIP`. `width` / `height` are best-effort: a writer that cannot
cheaply determine the dimensions **MAY** write `0` for both.

The image's origin — the **confirmed** (post-metadata-menu) identifier it was
fetched against — is recorded in the PROV block's `art_source` key (§6.3.1), not
in the ART block. The embedded image always reflects the user-confirmed release;
the cosmetic pre-rip preview may have shown a different best-guess cover, which is
never persisted.

---

## 7. Validation Rules

A conforming reader **MUST** enforce (33 rules):

1. `magic == b'RBIMAGE\x00'`
2. `version_major == 6` (reject if not equal — v6.0 is a clean break, §1)
3. `version_minor` known to this revision is `0`; a reader **MUST** warn (not reject) when `version_minor` exceeds the highest minor it understands, and **MAY** attempt to read, since minor increments are intended to be backwards-compatible
4. `flags & 0xAAAAAAAA == 0` — no odd-position (must-understand) flag bit is set, since this revision defines none; reject otherwise. An unknown *even*-position bit is not grounds for rejection (§4.2)
5. `reserved == b'\x00' × 7`
6. `1 <= track_count <= 99`
7. `1 <= disc_number <= disc_total`
8. `pcm_sample_rate == 44100 and pcm_channels == 2 and pcm_bit_depth == 16`
9. `dir_count >= 2` (at minimum `TOC ` and `PCM ` must be present)
10. `dir_count <= 256` (implementation-defined upper bound; reject if exceeded)
11. `dir_offset >= 40` (directory does not overlap the fixed header)
12. `dir_offset + dir_count × 54 == file_size`
13. Exactly one `TOC ` entry in the directory
14. Exactly one `PCM ` entry in the directory
15. No duplicate `type_id` values in the directory for required blocks
16. For every directory entry: `offset + length <= dir_offset` (blocks do not overlap the directory)
17. For every directory entry: `offset >= 40` (blocks do not overlap the fixed header)
18. No two directory entries have overlapping byte ranges
19. Number of `TRACK AUDIO` entries in the TOC block **MUST** equal `track_count` in the fixed header
20. `blake3(block_content) == directory_entry.checksum` for every block; readers **SHOULD** warn on mismatch rather than hard-failing by default, unless policy requires strict verification
21. TOC block decodes as valid UTF-8
22. PROV block (if present) decodes as valid UTF-8
23. RLOG block (if present) decodes as valid UTF-8
24. RGDB block (if present): `length == 17 + 12 × track_count`
25. ARIP block (if present): `length == 13 + 15 × track_count`
26. ARIP block (if present): all `status` values are in the range `0`–`2`
27. RLOG block (if present): last line matches `BLAKE3: [0-9a-f]{64}` (optional integrity check; warn on mismatch)
28. ART block (if present): `length >= 10` (room for the fixed ART header)
29. ART block (if present): `image_length == length − 10`
30. ART block (if present): `image_format` is a recognised value (`1` = JPEG); a reader **SHOULD** warn and skip the block on an unrecognised value rather than reject the file (the block carries `BLOCK_FLAG_SKIP`)
31. `PCM.length == Σ (pregap_frames + duration_frames) × 2352` over the TOC block's tracks (§6.2.1) — the PCM block holds exactly the audio the TOC describes, and a whole number of CD frames
32. Every directory entry whose `type_id` this revision does not define has `BLOCK_FLAG_SKIP` set (§3, §5.3); reject otherwise — an unknown block its writer did not mark skippable is one this reader cannot safely ignore
33. For every directory entry whose `type_id` this revision defines: the reserved `block_flags` bits (`0xFFFE`) are `0`, `TOC ` and `PCM ` do not set `BLOCK_FLAG_SKIP`, and every optional block does (§5.3, §5.4)

---

## 8. Python Reference Definition

See `src/cdda2img/rbi_format.py` for the canonical Python struct definitions, constants, and dataclasses that implement this specification.

---

## 9. Normative References

- IEC 60908:1999 — Audio recording — Compact disc digital audio system
- ITU-R BS.1770-3 — Algorithms to measure audio programme loudness and true-peak audio level
- EBU R128 — Loudness normalisation and permitted maximum level of audio signals
- cdrdao TOC format — cdrdao(1) man page, `toc-file` section
- RFC 4634 — SHA-2 specification
- AccurateRip — http://www.accuraterip.com/ (checksum algorithm and database protocol)
- CUETools Database — http://db.cuetools.net/ (whole-disc integrity verification)
