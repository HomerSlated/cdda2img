# RBI CD-Audio Container Format Specification (v1.1)

## Header Layout

| Offset Range | Size (bytes) | Field Name        | Description |
|--------------|--------------|-------------------|-------------|
| `0–2`        | 3            | `MAGIC`           | Format identifier: ASCII `"RBI"` |
| `3–5`        | 3            | `FORMAT_VERSION`  | ASCII format version, e.g. `"1.1"` |
| `6–21`       | 16           | `TOC/PCM Offsets` | Four unsigned 32-bit integers: `toc_start`, `toc_end`, `pcm_start`, `pcm_end` |
| `22–53`      | 32           | `TOC Checksum`    | SHA-256 digest of TOC data |
| `54–85`      | 32           | `PCM Checksum`    | SHA-256 digest of PCM data |
| `86–87`      | 2            | `Metadata Length` | Unsigned 16-bit integer: length of metadata string in bytes |
| `88+`        | variable     | `Metadata String` | UTF-8 encoded string, e.g. `"Created by cdda2img v0.1.3 (format 1.1) on 2025-10-26T19:26:00"` |

---

## Field Details

### `MAGIC`
- ASCII identifier for RBI format
- Must be exactly 3 bytes: `b'RBI'`

### `FORMAT_VERSION`
- ASCII version string, fixed width: 3 bytes
- Current version: `b'1.1'`

### `TOC/PCM Offsets`
- Packed as `<IIII>` (little-endian)
- Each field is a byte offset from start of file:
  - `toc_start`: start of TOC block
  - `toc_end`: end of TOC block
  - `pcm_start`: start of PCM block
  - `pcm_end`: end of PCM block

### `TOC Checksum` and `PCM Checksum`
- SHA-256 digests (32 bytes each)
- Used for integrity verification

### `Metadata Length`
- Unsigned 16-bit integer
- Indicates length of following metadata string

### `Metadata String`
- UTF-8 encoded
- Typically includes tool version, format version, and timestamp
- Example: `"Created by cdda2img v0.1.3 (format 1.1) on 2025-10-26T19:26:00"`

---

## Header Size

- **Fixed header size**: 88 bytes
- **Full header size**: `88 + metadata_len`

---

## Validation Rules

- `MAGIC` must be `b'RBI'`
- `FORMAT_VERSION` must be exactly 3 ASCII bytes
- `metadata_len` must be ≤ 1024
- `created_by` must decode as valid UTF-8
- Checksums must match actual TOC and PCM data
