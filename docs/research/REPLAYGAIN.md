# ReplayGain

*Research notes for the cdda2img project.*

---

## Table of Contents

1. [Origins and Purpose](#1-origins-and-purpose)
2. [ReplayGain 1.0: The Algorithm](#2-replaygain-10-the-algorithm)
3. [ReplayGain 2.0: EBU R128 Basis](#3-replaygain-20-ebu-r128-basis)
4. [Track Gain vs. Album Gain](#4-track-gain-vs-album-gain)
5. [Metadata: Tag Names and Formats](#5-metadata-tag-names-and-formats)
6. [Hardware CD Players](#6-hardware-cd-players)
7. [Software CD Playback](#7-software-cd-playback)
8. [Audio File Playback Software](#8-audio-file-playback-software)
9. [Ripping and Transcoding Software](#9-ripping-and-transcoding-software)
10. [Normalization vs. ReplayGain: The Critical Distinction](#10-normalization-vs-replaygain-the-critical-distinction)

---

## 1. Origins and Purpose

### 1.1 The Problem

Before ReplayGain, listeners shuffling between tracks from different albums faced a constant battle with volume. A quiet classical recording might sit at −20 dBFS average; a heavily compressed pop CD might sit at −6 dBFS. The difference was audible and disruptive. The instinctive industry answer was to master louder — which led to the "Loudness War" documented in `ABHOOD.md`. The user's answer was the volume knob, or simply skipping tracks.

### 1.2 The Solution

**ReplayGain** is a proposed technical standard published in 2001 by **David Robinson**, then completing his PhD at the University of Essex (funded by the EPSRC). Robinson was motivated by the practical problem of inconsistent volume in his own digital music collection. The solution he devised is elegant: rather than modifying audio data, **annotate** the file with metadata describing how much the player should adjust gain at playback time.

This single design decision — loudness information as metadata, not as a modification to the bitstream — is the defining characteristic of ReplayGain and the source of most of the tricky interactions with other processing pipelines.

Robinson credited Glen Sawyer and Jim Casaburi for software contributions, and Bob Katz (mastering engineer and author of *Mastering Audio*) and Matt Ashland for ideas. The specification was developed with input from the Hydrogenaudio community.

### 1.3 Scope

ReplayGain is designed for **consumer music listening** — it targets a comfortable reference loudness at which most music is pleasant to hear without adjustment. It is **not** a broadcast standard (that is EBU R128 / ATSC A/85), **not** a mastering standard, and **not** a loudness standard for theatrical or game audio. It was designed for use with 44.1 kHz stereo audio (CD quality) though implementations exist for other sample rates.

---

## 2. ReplayGain 1.0: The Algorithm

### 2.1 Overview

The algorithm models human perception of loudness and produces a single gain value in decibels for each track (and optionally for an album). The process:

1. Apply an equal-loudness weighting filter to the audio
2. Compute RMS energy in 50 ms blocks
3. Take the 95th percentile of those RMS values as the loudness estimate
4. Compute the gain required to bring that loudness to the reference level
5. Store gain and peak values as metadata tags

### 2.2 Equal-Loudness Weighting Filter

Human hearing is not flat across frequency. The ear is most sensitive in the 2–5 kHz range and considerably less sensitive at low and very high frequencies (as described by the Fletcher–Munson / ISO 226 equal-loudness contours).

The ReplayGain 1.0 filter approximates an inversion of these contours:

- A **10th-order IIR filter** designed using MATLAB's Yule-Walker method, which provides the broad spectral shaping to boost the perceptually important mid frequencies relative to bass and treble.
- A **2nd-order Butterworth high-pass filter** at 150 Hz, which removes the sub-bass energy that contributes to RMS level but is largely inaudible on typical consumer equipment.

Filter coefficients are specified for 44,100 Hz and 48,000 Hz; implementations for other sample rates must either resample first or recompute the coefficients.

For stereo audio, both channels are filtered and then their squared samples are **averaged across channels** for each 50 ms block (not summed). This means a mono signal and a full stereo signal at the same perceived loudness get the same RG value.

### 2.3 RMS Energy per Block

For each 50 ms block of filtered, channel-averaged audio:

```
L_block = sqrt(mean(x_i²))          # RMS
L_dB = 20 × log₁₀(2 × L_block / L_peak_to_peak)
```

At 44,100 Hz, a 50 ms block is 2,205 samples.

### 2.4 Statistical Analysis: The 95th Percentile

Rather than using the mean or peak RMS across the whole track, ReplayGain uses the **95th percentile** of all 50 ms block loudnesses. This was chosen because it matches human perception better than the mean: listeners judge a track's loudness by its sustained loud passages, not its average (which is dragged down by quiet passages and silence between phrases). It also avoids the pathological case of a single transient dominating the measurement.

The 95th percentile is computed by sorting all block loudness values and taking the value at the 95% position.

### 2.5 Reference Level and Gain Calculation

The reference loudness in ReplayGain 1.0 is defined as the loudness of a **stereo pink noise signal** at **89 dB SPL** on an SMPTE-calibrated monitoring system. This translates to approximately **−14 dBFS** (relative to digital full scale) when measured through the equal-loudness filter.

The important caveat: the 89 dB SPL figure is only meaningful in the context of a specific monitoring calibration (SMPTE RP 200 / EBU R68). It is not an absolute sound pressure level that must be reproduced in the listener's room — it is a reference point for the algorithm. What matters in practice is the −14 dBFS numerical target.

```
RG_gain = L_reference − L_track     (in dB)
```

Where `L_reference = −14 dBFS` (by the 1.0 definition) and `L_track` is the 95th-percentile filtered RMS of the track.

A quiet track might yield `RG_gain = +4.0 dB` (needs boosting). A loud track might yield `RG_gain = −3.5 dB` (needs attenuating). Gains in excess of ±20 dB are considered pathological.

### 2.6 Peak Level

In addition to gain, ReplayGain stores the **peak sample amplitude** of the (unfiltered) audio as a linear value, where 1.0 represents digital full scale.

Peak values above 1.0 are possible for certain lossy codecs (MP3 especially), where the decoder may produce inter-sample peaks exceeding 0 dBFS. For lossless formats (FLAC, WAV), the peak is always ≤ 1.0.

The peak is used by players to prevent clipping when applying gain:

```
scale_factor = min(10^((RG_gain + preamp) / 20), 1.0 / peak_amplitude)
```

Without peak-limited clipping prevention, applying a positive ReplayGain value to a track whose peak is already near 1.0 will clip.

---

## 3. ReplayGain 2.0: EBU R128 Basis

### 3.1 Motivation

ReplayGain 1.0's equal-loudness filter was hand-crafted and not rooted in a formal international standard. Meanwhile, broadcast audio standardisation had converged on **ITU-R BS.1770** (2006, updated 2012) — a robust, internationally agreed loudness measurement algorithm using **K-weighting** (a two-stage filter: a shelf to model the acoustic effect of the head, followed by a high-pass). EBU R128 (2010) adopted BS.1770 with a reference level of **−23 LUFS** (Loudness Units relative to Full Scale) for broadcast.

The Hydrogenaudio community developed **ReplayGain 2.0** to bring ReplayGain into alignment with BS.1770/R128 while maintaining backwards compatibility.

### 3.2 Key Differences from 1.0

| Aspect | RG 1.0 | RG 2.0 |
|--------|--------|--------|
| Loudness measurement | Yule-Walker + Butterworth equal-loudness filter | ITU-R BS.1770-3 K-weighting |
| RMS gating | 95th percentile of 50 ms blocks | BS.1770 absolute + relative gating |
| Reference level | −14 dBFS / 89 dB SPL | −18 LUFS |
| True peak | Not measured | Measured (oversampled) |
| Algorithm tag | (none) | `REPLAYGAIN_ALGORITHM = ITU-R BS.1770` |

### 3.3 The −18 LUFS Reference

The choice of −18 LUFS rather than −23 LUFS (EBU R128 broadcast) or −16 LUFS (streaming platforms) was empirical: when the RG 2.0 reference was applied to a large corpus of popular music, −18 LUFS produced *perceived loudness* closest to the original RG 1.0 results. The two numbers (−14 dBFS and −18 LUFS) are not algebraically equivalent; they agree approximately because the K-weighting and BS.1770 gating treat the music corpus similarly to the Yule-Walker filter + 95th percentile.

The practical consequence: files tagged with RG 1.0 tools and files tagged with RG 2.0 tools should, for most music, play at similar perceived loudness when the player uses the same gain value — but the gain values in the tags may differ by a few dB, and a player should use `REPLAYGAIN_REFERENCE_LOUDNESS` to interpret them correctly.

### 3.4 True Peak

RG 2.0 measures **true peak** (inter-sample peak, computed by oversampling at 4× or more) rather than sample peak. This is important because MP3 and certain other lossy codecs can reconstruct inter-sample peaks above 0 dBFS even when no sample in the encoded file exceeds 0 dBFS. Using true peak for clipping prevention catches these cases; using sample peak does not.

### 3.5 Extended Tags

RG 2.0 defines optional additional tags:

| Tag | Purpose |
|-----|---------|
| `REPLAYGAIN_REFERENCE_LOUDNESS` | Target level used (e.g., `-18.00 LUFS`) |
| `REPLAYGAIN_TRACK_RANGE` | Dynamic range of track in LU |
| `REPLAYGAIN_ALBUM_RANGE` | Dynamic range of album in LU |
| `REPLAYGAIN_ALGORITHM` | Algorithm used (e.g., `ITU-R BS.1770-3`) |

`REPLAYGAIN_REFERENCE_LOUDNESS` is the most practically important: it tells a player or tool what target was used, so the gain value can be correctly applied regardless of whether the file was tagged with a 1.0 or 2.0 tool.

---

## 4. Track Gain vs. Album Gain

This distinction is fundamental to understanding ReplayGain's design intent.

### 4.1 Track Gain

`REPLAYGAIN_TRACK_GAIN` is computed independently for each track. Applying track gain at playback makes **every track equally loud** — useful for shuffle play across a diverse library.

The trade-off: within an album, tracks deliberately mastered at different relative levels (a quiet, introspective track followed by a loud, energetic one) will all be levelled out. The dynamics and flow the mastering engineer intended between tracks are destroyed.

### 4.2 Album Gain

`REPLAYGAIN_ALBUM_GAIN` is a single value computed by treating the entire album as one continuous audio stream (all tracks concatenated). The gain required to bring this album-level loudness to the reference is applied uniformly to all tracks on the album.

Applying album gain at playback makes **every album equally loud** while preserving the **inter-track dynamics** within each album. Track 5 may still be quieter than Track 8 — as intended — but the whole album will be at the same overall level as other albums.

Both track gain and album gain values (and their corresponding peaks) are typically stored on every track in the album. The player chooses which to apply based on the user's preference:

- **Track mode**: apply `REPLAYGAIN_TRACK_GAIN` → consistent level across all tracks in shuffle
- **Album mode**: apply `REPLAYGAIN_ALBUM_GAIN` → consistent level across albums, preserved intra-album dynamics
- **No mode**: apply no gain → source levels as mastered

### 4.3 The Right Default

For most listening contexts — especially for music that was carefully mastered as an album — **album mode is the more faithful choice**. It respects the mastering engineer's intent while still controlling inter-album volume variation.

Track mode is appropriate for shuffle libraries, DJ preparation, or broadcast contexts where every track must be independently normalised.

---

## 5. Metadata: Tag Names and Formats

### 5.1 Canonical Tag Names

All formats use the same logical names (case-insensitive in practice, uppercase by convention):

| Tag | Meaning | Example Value |
|-----|---------|---------------|
| `REPLAYGAIN_TRACK_GAIN` | Track gain | `+2.35 dB` |
| `REPLAYGAIN_TRACK_PEAK` | Track peak amplitude | `0.987654` |
| `REPLAYGAIN_ALBUM_GAIN` | Album gain | `-1.20 dB` |
| `REPLAYGAIN_ALBUM_PEAK` | Album peak amplitude | `0.992341` |
| `REPLAYGAIN_REFERENCE_LOUDNESS` | Reference level used | `-18.00 LUFS` |
| `REPLAYGAIN_TRACK_RANGE` | Track dynamic range (optional) | `7.23 LU` |
| `REPLAYGAIN_ALBUM_RANGE` | Album dynamic range (optional) | `5.11 LU` |

Gain values are stored as strings in the form `[+/-]N.NN dB`. Peak values are dimensionless linear floats where `1.0 = 0 dBFS`.

### 5.2 Format-Specific Storage

| Container | Tag system | Notes |
|-----------|-----------|-------|
| **FLAC** | Vorbis comment | Standard; `REPLAYGAIN_*` uppercase preferred |
| **Ogg Vorbis** | Vorbis comment | Same as FLAC |
| **MP3 (ID3v2)** | TXXX free-form frames | RG 2.0 preferred; field name is the key, value is the dB string |
| **MP3 (legacy)** | `RGAD` frame | Older proprietary ID3v2 frame; avoid in new files |
| **MP3 (legacy)** | `RVA2` frame | Standard ID3v2 relative-volume frame; less widely supported |
| **APEv2** | APEv2 tag items | Used by Musepack, WavPack, some MP3 taggers (e.g. foobar2000) |
| **MP4/M4A** | iTunes metadata atoms (freeform `----:com.apple.iTunes:REPLAYGAIN_*`) | Non-standard but widely implemented |
| **Opus** | Opus comment (R128 variant: `R128_TRACK_GAIN`, `R128_ALBUM_GAIN`) | Opus uses a different scaling and always stores the value in Q7.8 fixed-point, not dB strings |
| **WAV** | ID3v2 chunk or INFO chunk | Inconsistent; avoid WAV for ReplayGain storage |

**Opus is a special case**: the Opus RFC defines its own volume normalization scheme (`output gain` in the stream header, and `R128_TRACK_GAIN` / `R128_ALBUM_GAIN` comment tags) using a fixed-point Q7.8 format with a reference of −23 LUFS (the broadcast EBU R128 level, not the RG 2.0 −18 LUFS). Do not confuse `R128_TRACK_GAIN` with `REPLAYGAIN_TRACK_GAIN` — they use different references and different value formats.

---

## 6. Hardware CD Players

**Standard CD-DA discs carry no ReplayGain metadata.** The Red Book (IEC 60908) defines no mechanism for embedding playback-gain information. The disc's Q subcode channel carries MCN, ISRC, and timing; the R–W channels carry CD TEXT, CD+G graphics, and MIDI. None of these mechanisms is suitable for, or has been used for, ReplayGain.

CD-Text (IEC 60908:1999, clause 26) could theoretically carry arbitrary text data in the USER mode or extended fields, but no standard defines a ReplayGain field for CD TEXT, and no consumer CD player implements one.

Therefore: **standalone hardware CD players have never supported ReplayGain in any form**. The concept is entirely native to the digital file domain. A listener using a standard CD player to play back a CDDA disc has no mechanism available for automatic loudness normalisation beyond the player's own analogue output level controls.

The only exception worth noting is open-source firmware for DAP (Digital Audio Player) hardware — notably **Rockbox**, which supports ReplayGain from file tags when playing digital files, but which, even on hardware that can play physical CDs, cannot apply ReplayGain to CD-DA streams.

---

## 7. Software CD Playback

Software CD players (applications that play an inserted CD disc from a computer drive) face the same constraint: the disc carries no ReplayGain data, so there is nothing to read and apply.

Some software CD players address this by:
- Looking up the disc in an online database (CDDB, MusicBrainz) and loading pre-computed ReplayGain values for the release
- Performing on-the-fly ReplayGain analysis during playback (computationally expensive)
- Storing per-disc or per-track ReplayGain values in a local cache keyed by disc ID

These are all non-standard workarounds, not part of any CDDA specification. They are fragile (depend on third-party databases or persistent local state) and not portable across playback applications.

---

## 8. Audio File Playback Software

The following is a representative survey of ReplayGain support in playback software.

| Application | Platform | RG support | Notes |
|-------------|----------|-----------|-------|
| **foobar2000** | Windows | Full (1.0 + 2.0) | Reference implementation; track/album/smart modes; preamp control; clipping prevention; also computes RG |
| **Quod Libet** | Cross-platform | Full | Detailed RG mode settings; reads REPLAYGAIN_REFERENCE_LOUDNESS |
| **MusicBee** | Windows | Full | RG 2.0 support |
| **VLC** | Cross-platform | Partial | Reads REPLAYGAIN_TRACK_GAIN; limited album mode |
| **Strawberry** | Cross-platform | Full | Reads and applies RG tags; also computes |
| **Clementine** | Cross-platform | Partial | Reads RG tags |
| **Winamp** | Windows | Track + album | Legacy support |
| **Rockbox** | DAP firmware | Full | Reference implementation for embedded hardware |
| **Poweramp** | Android | Full | One of few mobile players with comprehensive RG support |

The key player behaviours:

1. **Read the gain value** from the appropriate tag (`REPLAYGAIN_TRACK_GAIN` or `REPLAYGAIN_ALBUM_GAIN`)
2. **Convert to linear scale factor**: `10^(gain/20)`
3. **Apply preamp offset** if configured by the user: `10^((gain + preamp)/20)`
4. **Clamp to peak**: `min(scale_factor, 1.0 / peak_amplitude)` to prevent clipping
5. **Apply during playback** — the audio data on disk is never modified

A well-implemented player also:
- Reads `REPLAYGAIN_REFERENCE_LOUDNESS` to handle files tagged at different targets
- Provides a "smart" mode that uses album gain when tracks in an album are played consecutively and track gain during shuffle
- Allows the user to set a preamp offset (e.g., +6 dB) to compensate for consistently quiet or loud libraries

---

## 9. Ripping and Transcoding Software

### 9.1 Computing ReplayGain During Ripping

Several rippers calculate and embed ReplayGain values as part of the ripping workflow:

| Tool | Behaviour |
|------|-----------|
| **dBpoweramp** | Calculates track and album RG during ripping; embeds in output tags |
| **EAC** | Can call external RG tools post-rip |
| **Whipper** | Does not compute RG natively; relies on post-rip tools |
| **abcde** | Can invoke `mp3gain`, `metaflac`, or `loudgain` post-rip |
| **XLD (macOS)** | Calculates and embeds RG |

### 9.2 Standalone ReplayGain Tools

| Tool | Notes |
|------|-------|
| **mp3gain** | Original MP3-specific tool; **bakes the gain into the MP3 bitstream** (losslessly via the MP3 header gain field) rather than tagging; also writes APEv2 tags. Controversial because it modifies the file. |
| **aacgain** | mp3gain variant for AAC |
| **metaflac** | FLAC utility; writes `REPLAYGAIN_*` Vorbis comment tags; does NOT modify audio |
| **r128gain** | EBU R128 / BS.1770 based; writes RG 2.0 tags |
| **loudgain** | EBU R128 / BS.1770 based; writes RG 2.0 tags; comprehensive format support |
| **rsgain** | Modern C++ tool; RG 2.0; fast; recommended for new projects |
| **beets replaygain plugin** | Automatic RG tagging in library management |

### 9.3 ReplayGain and Transcoding

Transcoding — converting from one audio format to another — introduces a critical decision: **what to do with existing ReplayGain tags?**

The options are:

**Option A — Copy tags unchanged.** The gain value, measured against the original audio data, remains valid as long as the transcoding is lossless or perceptually transparent. For FLAC → FLAC or FLAC → WAV, this is fine. For lossy transcoding (FLAC → MP3), the gain value will be *approximately* correct but may differ slightly because the lossy codec changes the waveform slightly.

**Option B — Recalculate tags.** After transcoding, run ReplayGain analysis on the output file and embed fresh values. This is the most correct approach for lossy output but adds processing time.

**Option C — Drop tags.** Produce output without ReplayGain tags. The player will apply no gain correction.

**Option D — Bake in the gain.** Apply the ReplayGain gain value as a volume adjustment to the audio signal, then drop the tags. The output audio is permanently louder or quieter, and no tags are needed.

Option D is almost always wrong for archival or lossless output — see section 10.

---

## 10. Normalization vs. ReplayGain: The Critical Distinction

This section addresses the most important practical question: **what is the correct behaviour when a normalization pipeline (such as ffmpeg-normalize / EBU R128) encounters audio that already has ReplayGain tags?**

### 10.1 The Fundamental Difference

| | ReplayGain | Audio Normalization |
|-|-----------|-------------------|
| **What it modifies** | Metadata only (tags) | The audio signal itself |
| **Reversibility** | Fully reversible (delete the tag) | Irreversible (original signal is gone) |
| **Player dependency** | Requires a ReplayGain-aware player | Applied universally, even on dumb players |
| **Scope** | Track and/or album | Usually per-file or per-session |
| **Standard** | Hydrogenaudio / informal | EBU R128, ITU BS.1770, or peak normalization |

ReplayGain is a **metadata annotation** that instructs a player how to adjust gain at playback time. The audio data on disk is untouched.

Normalization is a **destructive operation** that modifies the audio waveform. The resulting file has permanently different levels from the source.

### 10.2 The Conflict: Normalized Audio + Pre-existing ReplayGain Tags

If a file has `REPLAYGAIN_TRACK_GAIN = +2.35 dB` (meaning the file was analysed as quiet and needs boosting), and you then run EBU R128 normalization on it:

1. The normalization tool measures the file's current loudness and boosts it (let's say by +4 dB, targeting −18 LUFS / −23 LUFS / whatever target).
2. The audio signal is now 4 dB louder than before.
3. The `REPLAYGAIN_TRACK_GAIN` tag still says `+2.35 dB` — referring to the **original, unnormalized** audio.
4. A player that reads this tag will apply an additional +2.35 dB boost to audio that was already normalized. The result is too loud, potentially clipped.

The ReplayGain tags are **now wrong** because they describe the original file, not the normalized file. This is a silent corruption: the tags are syntactically valid but semantically incorrect.

The converse is also true: if you normalize a file that had `REPLAYGAIN_TRACK_GAIN = −3.5 dB` (the file was loud, needs attenuating), the normalized audio is already quieter, but the tag still tells the player to attenuate further — the file ends up too quiet.

### 10.3 The Correct Handling

There are three correct outcomes, depending on intent:

---

**Case 1: Normalizing for archival / output to a device without ReplayGain support**

If the destination does not support ReplayGain (e.g., raw PCM for a CD image, MP3 for a portable player without RG support), and you want consistent loudness in the output:

→ **Apply the normalization. Strip or do not write ReplayGain tags.**

The audio levels are baked in. No tags are needed. Do not propagate stale ReplayGain tags from the source.

---

**Case 2: Normalizing while preserving the ReplayGain system**

If the output format supports ReplayGain (e.g., FLAC) and the destination player will use RG tags:

→ **Do not normalize the audio signal. Instead, compute fresh ReplayGain tags on the output files.**

The player applies the gain at runtime. This is always preferable to baking in the gain for lossless archival, because:
- The original dynamic range is preserved
- The gain can be changed without re-processing the audio
- Album gain relationships are preserved

If the source has pre-existing RG tags, you may either copy them (if the audio is unchanged) or recalculate them (if transcoding has altered the waveform).

---

**Case 3: Source has ReplayGain tags and normalization is requested — the conflict case**

If the source already has RG tags and the user explicitly requests normalization:

→ **Normalize the audio. Then recalculate ReplayGain on the normalized output. Embed fresh tags.**

The pre-existing tags from the source should be discarded. Fresh tags on the normalized output will reflect the new levels correctly.

A simpler alternative: if the user's goal is *consistent loudness* and the source already has RG album gain tags, **apply the album gain values as a pre-processing step** and treat that as the "normalization" — skip the EBU R128 pass entirely. This is a valid interpretation of "normalize using ReplayGain" and avoids the destructive EBU R128 step.

---

### 10.4 Applying vs. Baking: Lossy vs. Lossless

For **lossless formats** (FLAC, WAV, AIFF): baking in gain via normalization is fully lossless in terms of bit depth (volume scaling a 24-bit or 32-bit float is exact for amplitudes that don't exceed full scale). However, it is still irreversible in the sense that the relationship to the original mastered levels is lost.

For **lossy formats** (MP3, AAC, Opus): applying any volume gain that then re-encodes the audio introduces a further generation of lossy compression artefacts. This is almost always unacceptable for archival purposes.

**For archival purposes: never bake gain into a lossy format. Always use metadata tags.**

### 10.5 The mp3gain Exception

`mp3gain` deserves special mention because it takes a third path: it modifies the **header gain field** in the MP3 bitstream, which is applied by the decoder before any sample is returned to the application. This is described as "lossless" because the underlying compressed data is not re-encoded — only a scaling value in the header changes.

This approach has significant drawbacks:
- The modification is not visible as a standard ReplayGain tag to most software
- Reverting requires knowing the original gain value, which mp3gain stores in APEv2 tags but which may be lost
- It conflates the normalization step with the file format
- It provides no album gain pathway

For new workflows, prefer tagging tools (rsgain, loudgain, metaflac) that write REPLAYGAIN_* tags without touching the audio data.

### 10.6 Summary Decision Table

| Source has RG tags? | Normalization requested? | Destination supports RG? | Correct action |
|--------------------|------------------------|-------------------------|---------------|
| No | No | — | Pass audio through unchanged |
| No | Yes | No | Normalize; no tags needed |
| No | Yes | Yes | Compute RG on output; embed tags |
| Yes | No | Yes | Copy or recalculate RG tags; do not touch audio |
| Yes | No | No | Strip RG tags; do not touch audio |
| Yes | Yes | No | Normalize (stale tags are wrong); strip tags from output |
| Yes | Yes | Yes | Normalize; recalculate RG on output; embed fresh tags |

---

## References

- David Robinson, *ReplayGain 1.0 specification* (2001), Hydrogenaudio Knowledgebase:
  <https://wiki.hydrogenaudio.org/index.php?title=ReplayGain_1.0_specification>
- *ReplayGain 2.0 specification*, Hydrogenaudio Knowledgebase:
  <https://wiki.hydrogenaudio.org/index.php?title=ReplayGain_2.0_specification>
- *ReplayGain legacy metadata formats*, Hydrogenaudio Knowledgebase:
  <https://wiki.hydrogenaudio.org/index.php?title=ReplayGain_legacy_metadata_formats>
- EBU R128 (2010, rev. 2014) — *Loudness Normalisation and Permitted Maximum Level of Audio Signals*, European Broadcasting Union.
- ITU-R BS.1770 (2006, updated 2012) — *Algorithms to Measure Audio Programme Loudness and True-Peak Audio Level*, International Telecommunication Union.
- rsgain: <https://github.com/complexlogic/rsgain>
- loudgain: <https://github.com/Moonbase59/loudgain>
- ffmpeg-normalize: <https://github.com/slhck/ffmpeg-normalize>
- Bob Katz, *Mastering Audio: The Art and the Science* — reference for professional loudness practice and the context in which the 89 dB SPL reference was chosen.
