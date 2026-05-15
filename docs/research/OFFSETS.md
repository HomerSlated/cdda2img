# CD Drive Offsets — Read, Write, and Combined

This document explains the two independent hardware offset parameters that affect
the accuracy of CD-DA rips and burns: **read offset** and **write offset**.
Both arise from mechanical tolerance variation between drives; neither is a bug.

---

## 1. Read Offset

### What it is

Every CD drive has a fixed **read offset**: a small number of samples by which the
drive's read head is physically displaced from the exact position it believes it is
reading. The result is that the decoded PCM stream is shifted relative to the disc's
actual audio content.

Sign convention (AccurateRip / cdda2img):

| Sign | Meaning |
|------|---------|
| **Positive** (+N) | Drive reads **early** — PCM stream contains N extra samples at the start (shifted right on the disc) |
| **Negative** (−N) | Drive reads **late** — PCM stream is missing N samples at the start (shifted left on the disc) |

The Plextor PX-716A has a read offset of **+30 samples**, meaning its decoded PCM
begins 30 samples (120 bytes, about 0.68 ms) ahead of the actual disc content.

### Why it matters

Without correction, a rip from a +30 drive will have:
- 30 extra samples of the preceding track (or silence) prepended to each track
- 30 samples of the following track (or silence) missing from the end

For AccurateRip verification, the drive offset must be known and applied when computing
per-track checksums — the database was built from submissions where each contributor's
offset was accounted for.

For archival in cdda2img: the offset is informational. The raw ripped PCM is stored
as-is (no shift applied); the offset value is recorded in the container TOC as
`PROVENANCE_DRIVE_OFFSET` and applied at verification time only.

### Where to find it

The AccurateRip drive offset catalog at `http://www.accuraterip.com/driveoffsets.htm`
lists read offsets for thousands of drives. Offsets are crowd-sourced from EAC
submissions (users ripping test discs and submitting their results). The entry for
the Plextor PX-716A shows offset +30 with ~2781 submissions.

cdda2img queries this catalog automatically before each rip (`drive_info.py:
ensure_drive_offsets`) and saves confirmed offsets to `[[drives]]` in
`cdda2img.toml` so subsequent rips skip the lookup.

### How cdda2img applies it

**At rip time (cdrdao primary path):** no correction is applied to the PCM.
cdrdao has no sample-accurate offset correction flag, so the raw disc bytes are
captured as-is.

**At rip time (cd-paranoia fallback):** correction can be applied via `-O N`. The
libcdio-paranoia implementation is `disc_reader.py`.

**At verification time:** `accuraterip.py:verify_rip()` shifts the byte window for
each track by `drive_offset * 4` bytes before computing the checksum. Zero-padding
is applied when the window extends past the file boundary (critical for the last track
with a positive offset; see the zero-padding invariant in `CLAUDE.md`).

---

## 2. Write Offset

### What it is

Every CD burner also has a **write offset**: a small number of samples by which the
laser write head burns the audio slightly early or late relative to the requested
position. It is independent of the read offset and is specific to each drive unit.

Sign convention (cdda2img `tools/measure_write_offset.py`):

```
write_offset W = (found pulse position) − (expected pulse position)
```

| Sign | Meaning |
|------|---------|
| **Positive** (+W) | Drive burns **late** — audio lands W samples after the intended position |
| **Negative** (−W) | Drive burns **early** — audio lands \|W\| samples before the intended position |

The Plextor PX-716A has a write offset of **−30 samples** (confirmed via three
burn-and-read-back cycles in `rips/write_offset_results.toml`).

### Why it matters for archival

If you burn an RBI to disc for long-term storage or distribution, and your burn drive
has a non-zero write offset, the resulting disc will have audio that is shifted by W
samples from the original content. Another drive reading that disc will then see
content displaced by `write_offset + read_offset_of_reader`.

For true archival fidelity — where a burned disc should be bit-for-bit reproducible
from the original source — the write offset must be compensated before burning.

### The AccurateRip driveoffsets.htm page covers read offsets only

The AccurateRip catalog contains **read offsets only**. It has no write offset column.
Write offsets were historically collected inside EAC (Exact Audio Copy) through a
separate calibration step. An archived copy of the EAC write offset database exists on
archive.org; it is old and covers primarily legacy drives, but is a useful starting
reference for common hardware. No actively maintained open write offset database exists.

### How to measure it

The only reliable method is empirical: **burn a known test signal, read it back, and
measure the displacement**.

`tools/measure_write_offset.py` implements this:

1. Generates a 75-second synthetic test signal: silence with deterministic noise
   bursts at exactly 1.0 s and 60.0 s (two pulses for internal consistency checking).
2. Burns the signal via `cdrdao write`.
3. Reads it back via `cdrdao read-cd`, applies the read offset correction.
4. Locates the two pulse positions by RMS peak detection within a ±8820-sample
   search window around each expected position.
5. Computes `W = found_position − expected_position` for each pulse.
6. Flags inconsistency between the two pulses (indicates a defective disc).
7. Accumulates results across cycles in `rips/write_offset_results.toml`.

Multiple cycles reduce noise from pressing defects (even blank CD-Rs have minor
write-position jitter at the ±1-sample level). Three consistent cycles are sufficient
for high confidence.

### Burn correction

To compensate for write offset W when burning a disc from an RBI:

| W | Correction applied to the disc stream |
|---|---------------------------------------|
| W > 0 (burns late) | Trim W samples from the **start** of the full stream |
| W < 0 (burns early) | Prepend \|W\| samples of silence to the **start** of the full stream |

Equivalently: correction = `−W` samples.

This correction is applied to the **full concatenated disc stream**, not per-track.
The offset spans track boundaries: the N corrected samples at a track boundary come
from the adjacent track's content.

---

## 3. Combined Offset

When the same drive is used for both ripping and burning, the effects may cancel:

```
combined = read_offset + write_offset
```

For the Plextor PX-716A:

```
combined = (+30) + (−30) = 0
```

This means a disc ripped and burned on the same PX-716A unit will be a perfect
round-trip: the burned disc, when re-ripped on the same drive, will produce
bit-identical PCM.

However, this is **coincidental**, not guaranteed. A different PX-716A unit may have
a different write offset (units vary within manufacturing tolerance). The same
drive model does not have the same offset on every unit.

More importantly: a disc burned with write offset compensation intended for the
PX-716A (+30 read, −30 write) and then read by a different drive (e.g. +667 read,
+12 write) will have a combined displacement. The purpose of burn correction is to
produce a disc whose audio content lands at the correct absolute position, independent
of which drive reads it next.

---

## 4. cdda2img Strategy

### Ripping

| Path | Offset applied? | Notes |
|------|----------------|-------|
| cdrdao (primary) | No | No correction flag available; raw disc bytes captured |
| cd-paranoia (fallback) | Yes, via `-O N` | `drive_offset` passed to `-O` |

For both paths, the resolved `drive_offset` is passed to `verify_rip()` for
AccurateRip verification and stored as `PROVENANCE_DRIVE_OFFSET` in the RBI TOC.

### Burning (planned — `b` subcommand)

The planned `b` subcommand will:
1. Read the write offset for the burn drive from `[[drives]]` config
   (a new `write_offset` field alongside the existing `read_offset`).
2. Apply burn correction (`−write_offset` samples) to the full disc stream before
   passing it to `cdrdao write`.
3. Embed the write offset and correction applied as provenance comments in the
   burned disc's TOC (for auditing, not for standard players).

Write offset measurement will be integrated into a `drive` subcommand that manages
both read offset (from AccurateRip catalog) and write offset (from
`tools/measure_write_offset.py` cycles) in one place.

### Config format (planned)

```toml
[[drives]]
name = "PLEXTOR DVDR PX-716A"
offset = 30          # read offset (samples) — from AccurateRip catalog
write_offset = -30   # write offset (samples) — from measure_write_offset.py
```

---

## 5. Key Facts for the Development Drive (PX-716A)

| Property | Value | Source |
|----------|-------|--------|
| Read offset | +30 samples | AccurateRip catalog, conf ≈ 2781 |
| Write offset | −30 samples | 3 burn-read cycles, 100% confidence |
| Combined | 0 | Self-correcting in same-drive round-trip |
| Burn correction | Prepend 30 samples of silence before burning | |

---

## 6. Reference

- `src/cdda2img/accuraterip.py` — read offset application in `verify_rip()`
- `src/cdda2img/drive_info.py` — AccurateRip catalog lookup (`ensure_drive_offsets`, `find_drive_offset`)
- `src/cdda2img/config.py` — per-drive offset storage (`DriveConfig`, `save_drive`)
- `tools/measure_write_offset.py` — standalone write offset measurement tool
- `rips/write_offset_results.toml` — PX-716A measurement results (3 cycles)
- AccurateRip driveoffsets.htm — read offsets only; no write offset data
- `docs/research/ABHOOD.md §5.4` — drive requirements for accurate dumping
- `docs/research/NONSPEC.md` — write offset in the context of disc mastering
