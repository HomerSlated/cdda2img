# RBI Format Specification
## Red Book Image — CD-DA Archive Container
### Version 1.2 · Format version `major=1, minor=2`

---

## 1. Introduction

RBI (Red Book Image) is an open, single-file container format for archiving and mastering Red Book standard (IEC 60908:1999) CD-DA audio discs. It stores a human-readable TOC and a raw PCM audio payload in a single binary file, with SHA-256 integrity verification for both sections.

RBI is deliberately CD-DA-only. It does not attempt to represent raw physical sectors, subchannel data, copy-protection artefacts, or data tracks. This makes it unsuitable as a bit-for-bit clone format, but well-suited as a high-fidelity audio archive and mastering source.

---

## 2. Comparison with Existing Formats

| Property              | CUE/BIN        | CCD/IMG/SUB     | MDS/MDF (Alcohol 120%) | NRG (Nero)      | **RBI**                     |
|-----------------------|---------------|-----------------|------------------------|-----------------|-----------------------------|
| File count            | 2             | 3               | 2                      | 1               | **1**                       |
| Container type        | Text + binary | Text + binary×2 | Binary + binary        | Binary (chunks) | **Binary**                  |
| Magic / signature     | None          | None            | `MEDIA DESCRIPTOR`     | `NERO`/`NER5`   | **`RBIMAGE\x00`**           |
| Endianness            | N/A           | N/A             | Little-endian          | Big-endian      | **Little-endian**           |
| Specification status  | De facto      | Proprietary     | Proprietary            | Proprietary     | **Open**                    |
| Integrity checking    | None          | None            | None                   | None            | **SHA-256 (TOC+PCM)**       |
| TOC format            | Plain text    | INI text        | Binary structs         | Binary chunks   | **Plain text (cdrdao-compatible)** |
| Audio storage         | Raw sectors   | Raw sectors     | Raw sectors            | Raw sectors     | **Raw PCM (s16le)**         |
| Subchannel data       | Optional      | Yes (.SUB)      | Optional               | No              | **No**                      |
| Multi-session         | Limited       | Yes             | Yes                    | Yes             | **No (CD-DA only)**         |
| CD-TEXT               | Yes           | Yes             | No                     | Yes (CDTX chunk)| **Yes (in TOC)**            |
| ISRC / MCN            | Yes           | Yes             | Yes                    | Yes             | **In TOC**                  |
| Max file size (index) | —             | —               | 4 GB (uint32 offsets)  | 8 GB (v2 uint64)| **Unlimited (uint64)**      |

### Notes on comparable formats

**CUE/BIN** — The most widely supported format. The `.cue` sheet is a plain-text file loosely based on the cdrecord/cdrdao TOC syntax. No formal specification exists. No integrity checking. The `.bin` stores raw 2352-byte sectors (including sync, header, EDC/ECC bytes that are meaningless for CD-DA but present for compatibility).

**CCD/IMG/SUB (CloneCD)** — Three files: an INI-style `.ccd` containing raw TOC entries (session/entry/track sections with LBA addresses, ADR/Control bytes, ISRC, CD-TEXT packs), a `.img` of raw 2352-byte sectors, and a `.sub` of 96-byte interleaved P-W subchannel blocks per sector. Proprietary but substantially reverse-engineered.

**MDS/MDF (Alcohol 120%)** — Binary `.mds` descriptor with magic `MEDIA DESCRIPTOR\x01` (17 bytes), followed by a fixed header with little-endian offsets to session blocks, track blocks, and a per-track footer that contains the filename of the paired `.mdf` data file. Track blocks encode raw TOC point data (ADR/CTL, PMIN/PSEC/PFRAME, LBA, sector size). No checksums.

**NRG (Nero Burning ROM)** — Single file. The actual data occupies the start; the footer (either 12-byte v1 or 16-byte v2) is appended at the end with an offset pointing back to a chain of big-endian length-prefixed chunks: `CUES`/`CUEX` (cue data), `DAOI`/`DAOX` (DAO track info with ISRC, index0/1/end offsets), `SINF` (session info), `CDTX` (CD-TEXT), `MTYP` (media type), `ETN2`/`ETNF` (track entries), `END!`/`END1`. Proprietary; no checksums.

---

## 3. File Structure Overview

```
┌─────────────────────────────────────────────────────────┐
│  FIXED HEADER (121 bytes)                               │
│    Magic (8) · Version (2) · Flags (4)                  │
│    Track count (1) · Disc number (1) · Disc total (1)   │
│    PCM sample rate (4) · Channels (1) · Bit depth (1)   │
│    Offsets (32) · TOC checksum (32) · PCM checksum (32) │
│    Metadata length (2)                                  │
├─────────────────────────────────────────────────────────┤
│  VARIABLE HEADER (metadata_len bytes, max 1024)         │
│    Creation metadata string (UTF-8)                     │
├─────────────────────────────────────────────────────────┤
│  TOC BLOCK (variable)                                   │
│    cdrdao-compatible plain-text TOC (UTF-8)             │
├─────────────────────────────────────────────────────────┤
│  [OPTIONAL FUTURE SECTIONS]                             │
│    Reserved gap between toc_end and pcm_start           │
├─────────────────────────────────────────────────────────┤
│  PCM BLOCK (variable)                                   │
│    Raw interleaved PCM audio (s16le, stereo, 44100 Hz)  │
└─────────────────────────────────────────────────────────┘
```

The TOC block begins at `toc_start`. The PCM block begins at `pcm_start`. These offsets are independent — a gap between `toc_end` and `pcm_start` is reserved for future optional sections. A reader that does not understand a section in this gap **MUST** skip from `toc_end` to `pcm_start` to locate the audio.

---

## 4. Binary Header Layout

All multi-byte integer fields are **little-endian** unless otherwise noted.

| Offset | Size (bytes) | Type      | Field             | Description |
|--------|-------------|-----------|-------------------|-------------|
| 0      | 8           | bytes     | `magic`           | `RBIMAGE\x00` (0x52 0x42 0x49 0x4D 0x41 0x47 0x45 0x00) |
| 8      | 1           | uint8     | `version_major`   | Format major version; current value: `1` |
| 9      | 1           | uint8     | `version_minor`   | Format minor version; current value: `2` |
| 10     | 4           | uint32 LE | `flags`           | Feature bitmask (see §5.3); currently `0x00000000` |
| 14     | 1           | uint8     | `track_count`     | Number of audio tracks (1–99) |
| 15     | 1           | uint8     | `disc_number`     | This disc's position in a set (1-based; `1` for single discs) |
| 16     | 1           | uint8     | `disc_total`      | Total discs in set (`1` for single discs) |
| 17     | 4           | uint32 LE | `pcm_sample_rate` | Audio sample rate in Hz; Red Book standard: `44100` |
| 21     | 1           | uint8     | `pcm_channels`    | Number of audio channels; Red Book standard: `2` |
| 22     | 1           | uint8     | `pcm_bit_depth`   | Bits per sample; Red Book standard: `16` |
| 23     | 8           | uint64 LE | `toc_start`       | Byte offset from file start to beginning of TOC block |
| 31     | 8           | uint64 LE | `toc_end`         | Byte offset from file start to end of TOC block |
| 39     | 8           | uint64 LE | `pcm_start`       | Byte offset from file start to beginning of PCM block |
| 47     | 8           | uint64 LE | `pcm_end`         | Byte offset from file start to end of PCM block |
| 55     | 32          | bytes     | `toc_checksum`    | SHA-256 digest of TOC block bytes |
| 87     | 32          | bytes     | `pcm_checksum`    | SHA-256 digest of PCM block bytes |
| 119    | 2           | uint16 LE | `metadata_len`    | Byte length of the following metadata string |
| 121    | variable    | UTF-8     | `metadata`        | Creation metadata string; length = `metadata_len` |

**Fixed header size:** 121 bytes
**Total header size:** `121 + metadata_len` bytes
**TOC block begins at:** `toc_start` (== `121 + metadata_len` in a v1.2 file with no preceding sections)
**PCM block begins at:** `pcm_start` (>= `toc_end`)

The four offset fields at bytes 23–54 are written as `0x0000000000000000` placeholders initially, then patched once the header size and TOC size are known.

---

## 5. Field Specifications

### 5.1 `magic`
- Fixed value: `b'RBIMAGE\x00'` (8 bytes)
- The null byte prevents false matches in plain-text files and is consistent with the convention used by PNG (`\x89PNG\r\n\x1a\n`) and others.
- A file not beginning with exactly these 8 bytes is not an RBI file.

### 5.2 `version_major` and `version_minor`
- Two independent `uint8` fields encoding the format version.
- Current values: `version_major = 1`, `version_minor = 2`.
- The code version (`cdda2img` release) is separate. Format changes increment these fields; tool releases do not.
- A reader encountering `version_major > 1` **MUST** reject the file. A reader encountering an unknown `version_minor` **SHOULD** attempt to read the file and warn, as minor increments are intended to be backwards-compatible.

### 5.3 `flags`
- Unsigned 32-bit little-endian bitmask.
- All bits are currently reserved and **MUST** be `0` in v1.2 files.
- Future bits will designate optional sections present in the gap between `toc_end` and `pcm_start`. Even-numbered bits will indicate "safe to ignore"; odd-numbered bits will indicate "must understand to read correctly."
- A reader encountering an unknown flag bit on an odd position **MUST** reject the file.

### 5.4 `track_count`
- Number of audio tracks on this disc (1–99, per Red Book §3.1.2).
- Allows fast inspection without parsing the TOC block.
- **MUST** match the number of `TRACK AUDIO` entries in the TOC block.

### 5.5 `disc_number` and `disc_total`
- Support for multi-disc sets. Both are `1` for single-disc releases.
- `disc_number` is 1-based. `disc_number > disc_total` is invalid.
- Multi-disc sets are stored as separate RBI files; these fields identify each file's place in the set.

### 5.6 `pcm_sample_rate`, `pcm_channels`, `pcm_bit_depth`
- Explicitly encode the audio parameters of the PCM block.
- Red Book standard values: `44100`, `2`, `16`.
- A v1.2 reader **SHOULD** reject files where these differ from Red Book values, as no other values are currently defined.
- These fields exist to enable future format variants (e.g. 24-bit archival quality) without a major version bump.

### 5.7 Offset fields (`toc_start`, `toc_end`, `pcm_start`, `pcm_end`)
- All are unsigned 64-bit little-endian integers giving byte offsets from the start of the file.
- `toc_start >= 121 + metadata_len` (TOC begins after the full header).
- `pcm_start >= toc_end` (PCM begins at or after the end of the TOC; a gap is permitted).
- `pcm_end == file_size` (PCM block extends to end of file).
- There is no requirement that `toc_end == pcm_start`; the gap is reserved for future optional sections.

### 5.8 `toc_checksum` and `pcm_checksum`
- SHA-256 digests (32 bytes each), computed over the raw bytes of each block.
- `pcm_checksum` covers the raw PCM bytes only — no RIFF/WAV wrapper.
- Stored unconditionally. Readers **MUST** verify both on extraction and **SHOULD** warn (not hard-fail by default) on mismatch.

### 5.9 `metadata_len`
- Unsigned 16-bit little-endian integer.
- Maximum valid value: 1024. A reader encountering a larger value **MUST** reject the file.
- A value of 0 is valid (no metadata string follows).

### 5.10 `metadata`
- UTF-8 encoded string, `metadata_len` bytes long. Not null-terminated.
- Canonical format: `Created by cdda2img vX.Y.Z (format 1.2) on ISO8601_DATETIME`
- Example: `Created by cdda2img v0.1.4 (format 1.2) on 2025-10-26T19:26:00`
- A reader that cannot decode this field as valid UTF-8 **MUST** reject the file.

---

## 6. TOC Block

The TOC block is a UTF-8 encoded plain-text file in cdrdao TOC format. It is self-contained and human-readable.

### 6.1 Structure

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
  }
}

// Track 1
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
FILE "<album>.pcm" MM:SS:FF MM:SS:FF

// Track 2
...
```

The `CATALOG` line is optional; it is included only when an MCN is available from the source material (physical rip or existing subchannel data).

### 6.2 Timestamp format

CD-DA frame timestamps use the format `MM:SS:FF` where:
- `MM` = minutes (00–79)
- `SS` = seconds (00–59)
- `FF` = frames (00–74); 1 frame = 1/75 second

The first `MM:SS:FF` in each `FILE` line is the start position within the PCM blob; the second is the duration of the track.

Conversion: `total_frames = MM × 75 × 60 + SS × 75 + FF`

### 6.3 FILE reference

The filename in each `FILE` line uses the extension `.pcm` to reflect that the payload is raw PCM, not a WAV file. The stem is the sanitised album title. When extracting, a WAV file is reconstructed from the raw PCM using the audio parameters in the fixed header.

### 6.4 Character sanitisation

Track and album titles are sanitised before embedding:
- Curly quotes (`'`, `'`, `"`, `"`) → straight equivalents
- Em/en dashes (`—`, `–`) → hyphen-minus (`-`)
- Ellipsis (`…`) → three periods (`...`)
- Leading two-digit track number prefix (`01 `) stripped
- Remaining non-ASCII characters removed

---

## 7. PCM Block

The PCM block contains raw interleaved audio samples with no file wrapper.

### 7.1 Audio parameters (Red Book compliant)

| Parameter     | Value                  | IEC 60908 reference       |
|---------------|------------------------|---------------------------|
| Format        | Raw interleaved PCM    | §7 Recording parameters   |
| Sample width  | 16-bit signed (s16le)  | §13 Channel bit rate      |
| Channels      | 2 (stereo, L then R)   | §3.1.1                    |
| Sample rate   | 44100 Hz               | §13                       |
| Byte order    | Little-endian          | —                         |

### 7.2 No WAV wrapper

The PCM block contains only sample data — no RIFF header or chunk structure. The audio parameters needed to reconstruct a WAV file on extraction are stored in the fixed header (`pcm_sample_rate`, `pcm_channels`, `pcm_bit_depth`). This avoids redundancy and ensures `pcm_checksum` is a pure integrity check over audio data.

---

## 8. Validation Rules

A conforming reader **MUST** enforce:

1. `magic == b'RBIMAGE\x00'`
2. `version_major == 1` (reject if greater; warn if lesser)
3. `flags == 0x00000000` for any unrecognised bit at an odd position
4. `1 <= track_count <= 99`
5. `1 <= disc_number <= disc_total`
6. `pcm_sample_rate == 44100 and pcm_channels == 2 and pcm_bit_depth == 16`
7. `metadata_len <= 1024`
8. `metadata` decodes as valid UTF-8
9. `toc_start == 121 + metadata_len`
10. `pcm_start >= toc_end`
11. `pcm_end == file_size`
12. `sha256(toc_bytes) == toc_checksum`
13. `sha256(pcm_bytes) == pcm_checksum`

Rules 12 and 13 are integrity checks: a conforming reader **SHOULD** warn on mismatch rather than silently proceeding, but the severity policy is implementation-defined.

---

## 9. Python Reference Definition

See `src/cdda2img/rbi_format.py` for the canonical Python struct definitions, constants, and dataclasses that implement this specification.

---

## 10. Normative References

- IEC 60908:1999 — Audio recording — Compact disc digital audio system
- cdrdao TOC format — cdrdao(1) man page, `toc-file` section
- RFC 4634 — SHA-2 specification
