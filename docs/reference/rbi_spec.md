# RBI Format Specification
## Red Book Image — CD-DA Archive Container
### Version 4.0 · Format version `major=4, minor=0`

---

## 1. Introduction

RBI (Red Book Image) is an open, single-file container format for archiving and mastering Red Book standard (IEC 60908:1999) CD-DA audio discs. It stores a human-readable TOC, raw PCM audio, and optional metadata blocks in a single binary file, with SHA-256 integrity verification for every block.

RBI is deliberately CD-DA-only. It does not attempt to represent raw physical sectors, subchannel data, copy-protection artefacts, or data tracks. This makes it unsuitable as a bit-for-bit clone format, but well-suited as a high-fidelity audio archive and mastering source.

### Backwards compatibility

**RBI v4.0 is not backwards-compatible with v3.0.** The fixed header is redesigned (40 bytes vs. 169 bytes) around an extensible block-directory model; v3.0 files use hard-coded per-block offsets in the header. A v4.0 reader **MUST** reject any file with `version_major != 4`.

**Version history:**

| Version | Change summary |
|---------|---------------|
| v1.x    | Initial format; 121-byte fixed header |
| v2.0    | Header grew to 169 bytes; added RG block in TOC/PCM gap |
| v3.0    | Added per-track pre-gap storage and ISRC in TOC; breaking change from v2 |
| v4.0    | Redesigned around extensible block directory; provenance moved out of TOC; new PROV, ARIP, RLOG block types |

---

## 2. Comparison with Existing Formats

| Property              | CUE/BIN        | CCD/IMG/SUB     | MDS/MDF (Alcohol 120%) | NRG (Nero)      | **RBI**                     |
|-----------------------|---------------|-----------------|------------------------|-----------------|-----------------------------|
| File count            | 2             | 3               | 2                      | 1               | **1**                       |
| Container type        | Text + binary | Text + binary×2 | Binary + binary        | Binary (chunks) | **Binary**                  |
| Magic / signature     | None          | None            | `MEDIA DESCRIPTOR`     | `NERO`/`NER5`   | **`RBIMAGE\x00`**           |
| Endianness            | N/A           | N/A             | Little-endian          | Big-endian      | **Little-endian**           |
| Specification status  | De facto      | Proprietary     | Proprietary            | Proprietary     | **Open**                    |
| Integrity checking    | None          | None            | None                   | None            | **SHA-256 (per block)**     |
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
│    CTDB block  — CUETools DB results (optional, reserved) │
├─────────────────────────────────────────────────────────┤
│  BLOCK DIRECTORY (dir_count × 54 bytes)                 │
│    One entry per block: type_id, flags, offset, length, │
│    SHA-256 checksum                                     │
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
| 8      | 1           | uint8     | `version_major`   | Format major version; current value: `4` |
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

All other bits are currently reserved and **MUST** be `0` in v4.0 files. Even-numbered bits indicate "safe to ignore if not understood"; odd-numbered bits indicate "must understand to read correctly." A reader encountering an unknown odd-position flag **MUST** reject the file.

---

## 5. Block Directory

### 5.1 Location and structure

The block directory begins at `dir_offset` bytes from the start of the file. It consists of exactly `dir_count` consecutive entries, each 54 bytes. The directory is always written last, after all blocks; `dir_offset` is patched into the fixed header once all block offsets are known.

`dir_offset + dir_count × 54 == file_size` in all well-formed v4.0 files.

### 5.2 Directory entry layout

| Offset | Size (bytes) | Type      | Field          | Description |
|--------|-------------|-----------|----------------|-------------|
| 0      | 4           | bytes     | `type_id`      | 4-byte ASCII block type identifier (see §5.4) |
| 4      | 2           | uint16 LE | `block_flags`  | Block-level flags (see §5.3) |
| 6      | 8           | uint64 LE | `offset`       | Byte offset from file start to first byte of block |
| 14     | 8           | uint64 LE | `length`       | Block length in bytes |
| 22     | 32          | bytes     | `checksum`     | SHA-256 digest of block content (`length` bytes at `offset`) |

**Directory entry size:** 54 bytes

### 5.3 Block flags

| Bit | Mask     | Name                 | Description |
|-----|----------|----------------------|-------------|
| 0   | `0x0001` | `BLOCK_FLAG_SKIP`    | A reader that does not recognise `type_id` **MAY** skip this block and proceed. |

All other bits are reserved and **MUST** be `0`. The required blocks (`TOC ` and `PCM `) **MUST NOT** set `BLOCK_FLAG_SKIP`. All optional blocks **MUST** set `BLOCK_FLAG_SKIP`.

### 5.4 Block type identifiers

| `type_id`    | Name             | Required | `BLOCK_FLAG_SKIP` | Description |
|--------------|------------------|----------|-------------------|-------------|
| `b"TOC "`    | TOC block        | Yes      | No                | cdrdao-compatible plain-text TOC (UTF-8) |
| `b"PCM "`    | PCM block        | Yes      | No                | Raw interleaved s16le audio |
| `b"PROV"`    | Provenance block | No       | Yes               | Rip provenance and extended metadata (key=value UTF-8) |
| `b"RGDB"`    | ReplayGain block | No       | Yes               | EBU R128 / ReplayGain 2.0 data (binary) |
| `b"ARIP"`    | AccurateRip block| No       | Yes               | AccurateRip verification results (binary) |
| `b"RLOG"`    | Rip log block    | No       | Yes               | Structured rip log text (UTF-8) |
| `b"CTDB"`    | CUETools DB      | No       | Yes               | CUETools database results (RESERVED — format not yet defined) |

A conforming v4.0 writer **MUST NOT** write more than one entry of a given `type_id`. A conforming reader **MUST** reject a file containing duplicate `type_id` entries for any required block (`TOC ` or `PCM `); for optional blocks, it **SHOULD** use the first entry and warn.

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

Tracks on a CD-DA disc may have a pre-gap: a period of silence (or, rarely, audio) preceding the track's INDEX 01 point. RBI v4.0 stores pre-gap audio contiguously in the PCM block as part of the following track's slot.

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

The `DISC_ID` field in the disc-level `LANGUAGE 0` block corresponds to CD-Text PTI `0x86` — a label catalogue reference string. It is optional; omitted when no `disc_id` is available. Double-quote characters within the value are replaced with single quotes before embedding.

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
5. **Track field order**: `// Track N` → `// TRACK_TITLE_UNICODE:` (if needed) → `TRACK AUDIO` → `NO COPY` → `NO PRE_EMPHASIS` → `TWO_CHANNEL_AUDIO` → `ISRC` (if present) → `CD_TEXT { LANGUAGE 0 { TITLE PERFORMER } }` → `FILE` → `START` (if present).
6. **FILE start position**: always formatted as `MM:SS:FF` (e.g. `00:00:00` for track 1); never the bare integer `0`.

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
| `ripper`               | Extraction engine: `cdrdao` \| `cdparanoia` \| `file` \| `ddp` \| `toc` |
| `drive_name`           | Human-readable drive name, e.g. `Plextor PX-716A` |
| `drive_read_offset`    | Read offset applied during rip, as a signed integer string, e.g. `+30` or `-6` |
| `drive_write_offset`   | Write offset for this drive (informational), e.g. `-30` |
| `low_dynamic_range`        | `YES` \| `NO`. Derived from EBU R128 album LRA against the user's configured threshold (default 5.0 LU). Absent when no loudness analysis was performed (`--loudness none`) |
| `original_release_found`   | `YES` when MB release-group lookup (primary) or title-fuzz fallback yielded a usable answer. Absent / unwritten = lookup found nothing — *not* a guarantee that no earlier release exists |
| `original_release_title`   | Title of the earliest known release of the same logical album. Present only when `original_release_found = YES`. Surfaced via the canonical rendering described in §6.3.2 |
| `original_release_year`    | Year of that earliest release as a 4-digit integer string; present only when `original_release_found = YES` |
| `release_date`             | Release date of this specific release (YYYY, YYYY-MM, or YYYY-MM-DD) |
| `mb_release_id`            | MusicBrainz release UUID, e.g. `9d8f7a02-3851-4c49-9dc4-b08e7cb0ad7c` |
| `mb_release_group_id`      | MusicBrainz release-group UUID (used to re-run the original-release lookup from an existing RBI without redoing the disc-ID query) |
| `discogs_release_id`       | Discogs release ID (integer as decimal string) |
| `multi_match_isrc_disambiguated` | `YES`. Present when MB disc-ID returned >1 match and the in-memory ISRC tally (R1) picked a strictly-winning candidate. Absent when N=1 (no disambiguation needed) or when N>1 and the tally was a tie / sub-threshold. |
| `arip_transport`           | `https` \| `http`. Emitted whenever at least one AccurateRip fetch attempt reached the server (any 2xx/4xx). `http` indicates the HTTPS attempt failed and the fetcher fell back to plaintext — readers SHOULD treat the confidence values with reduced trust. |
| `arip_dbar_sha256`         | 64 lowercase hex chars. SHA-256 of the raw dBAR response body (pre-parse). Emitted only when a body was actually received. Lets later re-fetches detect AR-side changes or mirror tampering without re-running verification. |
| `acoustid_corroborates`    | `YES` \| `NO`. Emitted only when the pre-menu AcoustID helper (R6) ran (i.e. `acoustid_lookup.is_available()` was true and at least one per-track fingerprint produced a chained MB recording). `YES` = AcoustID's consistent-across-tracks winner agrees with the disc's existing MB release MBID; `NO` = disagrees. |
| `pre_emphasis`             | `YES` \| `NO`. Aggregate disc-level pre-emphasis flag. `YES` if any track has CONTROL bit 0 set, `NO` otherwise. Absent when not captured by the source parser (today only the cdrdao TOC path populates it). |
| `disagreement_cddb_mb`     | Comma-separated list of fields where CDDB and MB returned different answers, after NFC + casefold + reissue-suffix allow-list normalisation. Possible values: `album`, `artist`, or `album,artist`. Absent when both agree, when one side is blank, or when the pre-MB artist was the literal `Unknown Artist` default. |
| `original_release_corroborated` | `discogs,mb`. Emitted when both the Discogs master `main_release.year` and the MB RG `first-release-date` were resolvable and agreed on the same 4-digit year. |
| `original_release_disagreement` | `discogs:YYYY\|mb:YYYY`. Emitted when both years resolved and disagreed. The disc's stored `original_release_year` reflects the *earlier* of the two. |
| `lookup_status_cddb`       | `OK` \| `empty` \| `down` \| `disabled`. `OK` = response had data; `empty` = service reached but returned nothing; `down` = network or parse error; `disabled` = service not attempted (R10 offline mode). |
| `lookup_status_mb`         | As `lookup_status_cddb`, for MusicBrainz disc-ID. |
| `lookup_status_discogs`    | As `lookup_status_cddb`, for Discogs. `disabled` covers both R10 offline mode and the absence of a `DISCOGS_TOKEN`. |
| `lookup_status_acoustid`   | As `lookup_status_cddb`, for AcoustID. `disabled` covers R10 offline mode, the absence of an `ACOUSTID_API_KEY`, and missing pyacoustid / libchromaprint. |

All keys are optional. A v4.0 writer **SHOULD** emit at minimum `creator` and `created`. A reader **MUST NOT** fail on a missing key.

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

#### 6.3.3 Lookup status and conflict surfaces

The §6.3.1 keys split into two semantic groups:

- **Identifier keys** (`mb_release_id`, `mb_release_group_id`, `discogs_release_id`, `release_date`, `low_dynamic_range`, `original_release_*`, …): each names a single, factual fact about the disc.
- **Observation keys** (`lookup_status_*`, `disagreement_*`, `*_corroborated`, `*_disagreement`, `acoustid_corroborates`, `multi_match_isrc_disambiguated`, `arip_transport`, `arip_dbar_sha256`): each records *what we asked and what the answer was*.

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

The RLOG block stores the complete structured rip log as UTF-8 text. The format follows the whipper log convention: human-readable, one logical section per topic, machine-parseable by line prefix. The final line is a SHA-256 self-seal (see §6.6.2).

#### 6.6.1 Log structure

```
Log created by: cdda2img <version>
Log creation date: <ISO 8601 datetime>

Ripping phase information:
  Drive: <drive name>
  Extraction engine: <cdrdao|cdparanoia> <version>
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
      Result: <Found, exact match | Found, no match | Disc not present in database>
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
  AccurateRip summary: <All tracks accurately ripped | N/M tracks accurately ripped | Disc not present in AccurateRip database>
  Health status: <No errors occurred | N errors occurred>
  EOF: End of status report

SHA-256: <64 lowercase hex chars>
```

Sections are present only when the relevant data is available (e.g. AccurateRip sections are absent when the disc is not in the database).

#### 6.6.2 SHA-256 self-seal

The final line **MUST** be exactly:
```
SHA-256: <64 lowercase hex chars>
```
The hash is computed over all preceding bytes of the RLOG block (everything before this final line, including the preceding `\n`). A reader verifying the log integrity strips the last line, computes SHA-256 of the remainder, and compares against the stored hex string.

The self-seal is independent of the block-level checksum in the directory entry. The directory checksum covers the entire RLOG block (including the SHA-256 line) and protects against accidental corruption. The in-log SHA-256 protects against deliberate modification after insertion and mirrors the convention established by Exact Audio Copy and whipper.

---

### 6.7 CTDB Block (`b"CTDB"`) — Reserved

The `CTDB` type identifier is registered to store CUETools Database (CTDB) verification results. The binary layout is not yet defined. Implementations **MUST** set `BLOCK_FLAG_SKIP` on any `CTDB` block they write. Implementations **MUST NOT** write a `CTDB` block until the format is defined in a future revision of this specification.

---

## 7. Validation Rules

A conforming v4.0 reader **MUST** enforce (27 rules):

1. `magic == b'RBIMAGE\x00'`
2. `version_major == 4` (reject if not equal)
3. `version_minor == 0` (warn if greater; MAY attempt to read as minor increments are intended to be backwards-compatible)
4. `flags & ~0x00000004 == 0` (all bits except `FLAG_MASTER_MODE` reserved; reject if any unknown odd-position flag bit is set)
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
20. `sha256(block_content) == directory_entry.checksum` for every block (integrity check; readers **SHOULD** warn on mismatch rather than hard-failing by default, unless policy requires strict verification)
21. TOC block decodes as valid UTF-8
22. PROV block (if present) decodes as valid UTF-8
23. RLOG block (if present) decodes as valid UTF-8
24. RGDB block (if present): `length == 17 + 12 × track_count`
25. ARIP block (if present): `length == 13 + 15 × track_count`
26. ARIP block (if present): all `status` values are in the range `0`–`2`
27. RLOG block (if present): last line matches `SHA-256: [0-9a-f]{64}` (optional integrity check; warn on mismatch)

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
