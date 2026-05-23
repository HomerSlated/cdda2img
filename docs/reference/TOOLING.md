# Tooling Reference

---

## Foreign Disc Format Software

| Format            | Read   | Write  | Primary Tool(s)                     |
| ----------------- | ------ | ------ | ----------------------------------- |
| **B6T / B6I**     | ✅     | ✅     | BlindWrite 5/6                      |
| **C2D**           | ✅     | ✅     | WinOnCD 6                           |
| **CCD / IMG/SUB** | ✅     | ✅     | CloneCD                             |
| **CDI**           | ✅     | ✅     | DiscJuggler                         |
| **CIF / GI**      | ✅     | ✅     | Easy CD Creator / Roxio Creator 10  |
| **CUE / BIN**     | ✅     | ✅     | ImgBurn, EAC, CDRWIN                |
| **ISO**           | ✅     | ✅     | ImgBurn, Nero, Alcohol              |
| **MDS / MDF**     | ✅     | ✅     | Alcohol 120%                        |
| **MDX**           | ✅     | ✅     | Alcohol 120% (v6+)                  |
| **NRG**           | ✅     | ✅     | Nero Burning ROM                    |
| **READCD**        | ✅     | ⚠️      | readcd (dump only)                  |
| **TOC / BIN**     | ✅     | ✅     | cdrdao                              |
| **XCDRoast**      | (n/a)* | (n/a)* | (project format; trivial if needed) |
| **Harddisk**      | ⚠️      | ❌     | (not optical; out of scope)         |

*XCDRoast and Harddisk are out of scope: XCDRoast is a trivial project format; Harddisk is not optical.*

### CUE sheet + audio variants (widely supported)

ImgBurn, DAEMON Tools, and UltraISO all accept CUE sheets paired with audio files in
formats other than raw BIN: APE+CUE, FLAC+CUE, WAV+CUE are the common combinations.
cdda2img's `c` command should handle these transparently — PyAV decodes APE/FLAC/WAV
equally, so the only additional work is CUE sheet parsing to derive track boundaries.

---

## Disc Image Software — Feature Notes

*Drawn from the Wikipedia "Comparison of disc image software" article.*

### Tools with features relevant to cdda2img

**ImgBurn**: Accepts 15+ input formats including APE+CUE, FLAC+CUE, BIN+CUE, CDI, MDF+MDS,
NRG, WV. The broadest consumer-grade input format support of any single free tool. Strong
CD-DA handling. The authoritative tool for generating CUE/BIN sample images.

**CDRWIN**: One of the oldest tools with native audio-CD awareness; explicitly handles
Audio File Types + CUE for both input and output. The CUE sheet format originates here.

**IsoBuster**: Supports ~100+ formats and variable sector sizes (512–2448 bytes). The
broadest format support of any single tool, including raw audio modes. Useful as a
reference for exotic sector size handling.

**CloneCD**: Gold standard for copy-protection-aware cloning. Full subchannel support
including EFM encoding and raw sector writing. Produces `.sub` files alongside disc images
containing raw subcode data — the only consumer tool that routinely preserves P–W subchannels
in a separate sidecar. cdda2img's subchannel optional block (TODO: FLAG_SUBCHANNEL_PRESENT)
should be compatible with the CloneCD .sub layout.

**Alcohol 120%**: Supports CCD/IMG/SUB, CUE/BIN, ISO, BWI/BWT/BWS (BlindWrite). Can bypass
SafeDisc, SecuROM, and DPM (Data Position Measurement) copy protection on burns. Generally
regarded as producing better images than CloneCD for protected media. The authoritative tool
for MDS/MDF and MDX samples.

**BlindWrite**: Native format (BWI/BWT/BWS → later B5T/B5I, B6T/B6I) distinguishes itself
through subchannel data support; the sidecar contains subcode data most other tools discard.

**UltraISO**: Handles BIN+CUE, BWI+BWT, B5T+B5I, B6T+B6I, and can modify image contents.

### CD Architect (Sonic Foundry → Sony Creative Software → MAGIX)

Professional CD mastering and burning application — not a ripping or imaging tool. Notable for:

- **Purpose**: DDP creation, disc authoring, track assembly for replication mastering
- **Formats**: WAV, MP3, WMA, OGG, AIFF, FLAC input; writes Red Book–compliant CDs with CD-TEXT
- **Project file format**: CDP extension, RIFF container with "SFPJ" format chunk (v4) or
  lowercase "riff" header (v5). Last release: v5.2.240 (2009-08-30). No development since.
- **Ownership chain**: Sonic Foundry → Sony Creative Software (2003) → MAGIX Software (2016)
- **Availability**: Not actively sold or supported. Available only as legacy copies; some
  versions archived at archive.org. MAGIX inherited the catalog but does not distribute it.
- **Significance**: Used in mastering studios for Red Book authoring, CD-TEXT embedding, and
  DDP preparation. The CDP project file format is of archival interest (RIFF-based).

---

## Professional Disc Authoring

### GEAR Pro (GEAR Software — gearsoftware.com)

Professional-grade disc authoring for enterprise and broadcast environments. Primary
differentiator: multi-platform availability (Windows, Linux, Unix) and DDP mastering output
for professional disc replication.

**Editions**:
- **GEAR PRO Professional Edition** (Windows)
- **GEAR PRO Mastering Edition** (Windows) — adds DDP 1.0 and 2.1 output for glass mastering
- **GEAR PRO Linux** — supports major Linux kernel releases and distributions
- **GEAR PRO Unix** — supports Solaris 8/9/10 (x86/SPARC), IBM AIX 5.x, HP-UX 11.x

**Current version**: GEAR PRO Mastering Edition 8.10. The Linux/Unix pages at gearsoftware.com
are present but appear stale; the software appears to be in maintenance-only mode. *[Uncertain
whether Linux support is actively maintained or legacy-only as of 2026.]*

**Version history highlights**:
- v4.12 (Unix): UDF + ISO 9660, DVD±RW/CD-RW/DLT/DAT
- v6.05 → v7.0 (2005) → v8.x (current): CD/DVD mastering with DDP
- Pricing (last known ~2005): ~$399 for Mastering Edition; current pricing requires contacting
  sales@gearsoftware.com

**CD-DA / Red Book features**:
- All CD formats: CD-DA, CD-ROM, CD-XA, Enhanced CD
- All recording methods: track-at-once, disc-at-once, session-at-once, raw recording, fixed
  and variable packet writing
- Audio track extraction
- DDP 1.0 and 2.1 image output (Mastering Edition) — the professional standard for sending
  masters to pressing plants; DLT/Exabyte/DDS/DAT tape device support for DDP masters
- CD-TEXT (CDTEXT.BIN, block-0 packs) and ISRC (PQDESCR records) both confirmed present
  in GEAR Pro DDP 2.0 output; cdda2img parses both via `ddp_reader.py`.

**DDP (Disc Description Protocol)**: The professional mastering standard for delivering CD
masters to pressing plants. Contains the audio data, PQ subcode (TOC, ISRC, index points),
CD-TEXT, and metadata in a tape or file format. **cdda2img imports DDP 2.0 packages via
`cdda2img i <dir>`** — the only open-source DDP 2.0 reader for Linux. GEAR Pro writes s16le
to TRACK\*.DAT (verified empirically 2026-05-05); CD-TEXT and ISRC confirmed present.
DDP export (RBI → DDP 2.0) is planned; see `TODO.md`.

**Open-source equivalents**: cdrdao (TOC-based burning with subchannel), cdrecord/wodim
(track/disc-at-once). No direct open-source DDP mastering equivalent exists on Linux.

---

## Physical Media Acquisition

### Hardware

| Status | Item | Purpose | Notes |
|--------|------|---------|-------|
| ✅ | Plextor PX-716A (IDE) | Primary ripping drive | Best lead-in access; PLEXTOR negative-LBA SCSI extension; ISRC, accurate pre-gaps |
| ❌ | ASUS BW-16D1HT (SATA) | Lead-out coverage | With RibShark OmniDrive firmware (v3.02 mod, released 2026-02-18): unlocks full lead-out reading without cache. Complements the Plextor for complete preservation dumps. |

**Why ASUS BW-16D1HT**: The Plextor PX-716A is the gold standard for lead-in depth. For
lead-out, the ASUS BW-16D1HT with RibShark/OmniDrive custom firmware is the current
community recommendation (accepted for both new Redump dumps and verifications). The two
drives together cover every accessible area of the disc.

**RibShark OmniDrive firmware** (based on stock 3.02, released 2026-02-18): Fully unlocks
lead-out reading without using the drive cache. Flashing procedure documented at:
http://wiki.redump.org/index.php?title=Flashing_Asus_BW-16D1HT_firmware

---

### CD-TEXT and MCN Samples

No public database of confirmed CD-TEXT + MCN titles with UPC codes exists; confirmation
requires direct EAC/redumper output. The following guidance applies:

**Best hunting ground**: Japanese pressings from 1997–2010. CD-TEXT was co-developed by
Sony and Philips and heavily promoted in Japan. Sony Music Japan, Universal Japan, and EMI
Music Japan pressings are most likely to have CD-TEXT embedded. Western pressings from the
same era are inconsistent.

**Search strategy**:
1. Search the Redump database (redump.org/search) for audio disc submissions that include
   "CD-TEXT" in the notes. Redump entries include MCN/ISRC data where present.
2. Search Steve Hoffman Music Forums for EAC logs with "UPC: [number]" — this is the MCN
   read from the disc's Q subchannel, not the printed barcode.
3. On eBay: search for Japanese pressings (region JP, labels SICP-/TOCP-/MHCL-/WPCS-
   catalog number prefixes) of well-known Western albums.

**Technical note**: CD-TEXT is embedded at the mastering/authoring stage (must be in the
DDP or CD master). MCN/UPC is embedded by the mastering engineer if the label provides the
UPC. Pressing plants (Sony DADC, Universal) can add MCN during disc preparation but this is
label-dependent.

| Status | Type | Description | Notes |
|--------|------|-------------|-------|
| ❌ | CD-TEXT + MCN | Any Japanese pressing with confirmed subchannel CD-TEXT | Search Redump + SHF forums; target SICP-/TOCP-/WPCS- catalog prefixes |

---

### Copy Protection Samples

#### Cactus Data Shield / Copy Control (Macrovision)

**Mechanism**: Two-part. (1) Multi-session disc with intentionally corrupted/structured TOC
that confuses CD-ROM drives. (2) Near-silence audio samples replaced with erroneous data
during mastering (per US Patent 6,425,098); CD players error-correct tolerable C2 errors,
rippers receive corrupt audio. **Not Red Book compliant.**

**Versions**: CDS-100 (no PC playback), CDS-200 (adds Windows Media player + restricted copy
in data session), CDS-300 (adds controlled 3-copy burn at 320 kbit/s WMA).

**Defeat**: EAC burst mode sometimes extracts correctly; felt-tip marker on outer disc edge
disables the data session; modern rippers can ignore the second session.

**Timeline**: Deployed November 2001; EMI abandoned December 2006.

| Status | Title | Artist | Label | Region | Notes |
|--------|-------|--------|-------|--------|-------|
| ❌ | *White Lilies Island* | Natalie Imbruglia | BMG | Europe, 2001 | First major CDS-200/CactusPJ deployment |
| ❌ | *Greatest Hits* | Red Hot Chili Peppers | Warner | Europe | Copy Control (CDS-200) |
| ❌ | *Music* | Madonna | Warner | Europe | Copy Control (CDS-200) |

#### Extended Copy Protection / XCP (Sony BMG Rootkit)

**Mechanism**: Auto-installs via Windows AutoRun. Installs CD-ROM filter driver (`aries.sys`)
returning white noise to non-approved players. Rootkit cloaks all `$sys$`-prefixed files.
Polls all running processes every 1.5 seconds to detect ripping software. **Became exploitable
by malware (November 2005). Recalled November 2005.**

**Defeat**: Felt-tip marker on outer disc edge; holding Shift during insertion disables AutoRun.

**52 affected albums confirmed.** Notable titles with UPC:

| Status | Title | Artist | UPC |
|--------|-------|--------|-----|
| ❌ | *Nothing Is Sound* | Switchfoot | 827969653425 |
| ❌ | *Unwritten* | Natasha Bedingfield | 827969398821 |
| ❌ | *Touch* | Amerie | 827969076323 |
| ❌ | *The Body Acoustic* | Cyndi Lauper | 827969456927 |
| ❌ | *12 Songs* | Neil Diamond | 827969477625 |
| ❌ | *Healthy in Paranoid Times* | Our Lady Peace | 827969477724 |
| ❌ | *Suspicious Activity?* | The Bad Plus | 827969474020 |
| ❌ | *Shine* | Trey Anastasio | 827969642825 |
| ❌ | *To Love Again: The Duets* | Chris Botti | 827969482322 |
| ❌ | *Contraband* | Velvet Revolver | *(MediaMax, not XCP — see below)* |

*Full list of 52 XCP titles:* https://en.wikipedia.org/wiki/List_of_compact_discs_sold_with_Extended_Copy_Protection

#### Key2Audio (Sony DADC)

**Mechanism**: Bogus data tracks applied during glass master manufacturing. TOC contains
three sessions (two small data + one audio). Standard CD players ignore non-audio sessions
and play normally; computer drives fail to access audio. **Not Red Book compliant.**
Supports full ISRC, UPC, and CD-TEXT in the audio session.

**Versions**: Key2Audio (original, no PC playback; max 77 min) and Key2AudioXS (PC playback
via WMA DRM, controlled burns; max 75 min).

**Defeat**: Felt-tip marker on disc outer edge; CloneCD; EAC burst mode.

**Note**: No specific confirmed album titles with UPC found in public sources. Used
extensively by Sony Music labels on European pressings c. 2001–2004. Search Discogs notes
field for "key2audio" or "Key2Audio" to find confirmed titles.

| Status | Type | Notes |
|--------|------|-------|
| ❌ | Key2Audio European pressing | Search Discogs for "key2audio" in release notes; Sony Music / Columbia / Epic European pressings 2001–2004 |

#### MediaMax CD-3 (SunnComm Technologies)

**Mechanism**: Standard audio tracks (no TOC corruption). DRM software in data track installs
via Windows AutoRun; installs `sbcphid.sys` driver blocking direct audio track reading.
Entirely defeated by holding Shift during insertion (disables AutoRun on Windows).
Watermark in audio defeated by any lossy roundtrip.

| Status | Title | Artist | Label | Notes |
|--------|-------|--------|-------|-------|
| ❌ | *Comin' From Where I'm From* | Anthony Hamilton | RCA/BMG | First major US MediaMax release |
| ❌ | *Contraband* | Velvet Revolver | RCA | First US No. 1 album with MediaMax |
| ❌ | *Z* | My Morning Jacket | ATO/RCA, 2005 | Confirmed MediaMax |

**Note**: SafeDisc and SecuROM were **not** deployed on commercial audio CDs — PC games only.

---

### CD+G and Red Book Extensions

#### CD+G (Compact Disc + Graphics)

**Technical encoding**:
- Uses subcode channels R–W (6 channels × 1 bit/frame = 6 bits/frame at 75 frames/second
  = 28.8 kbit/s raw bandwidth)
- Data in 24-byte packets: 2-byte instruction + 16-byte data + 4 bytes CRC/padding
- Graphics: 16-color (4-bit) raster, 288×192 pixels display, 6×12 pixel tiles
- Color system: 16-entry manipulable color table

**Ripping**: cdrdao with `--read-subchan` in raw mode extracts R–W subchannels; cdgtools /
cdgrip (Linux, open source) decodes to MP3+G. cdda2img would need to capture the raw P–W
subchannel block and store in the subchannel optional block (FLAG_SUBCHANNEL_PRESENT).

**Playback hardware**: Dedicated karaoke machines; NEC TurboGrafx-CD, Turbo Duo, PC-FX;
Philips CD-i; Sega CD/Saturn; 3DO; Amiga CD32; many DVD players (post-2003).

**Commercially released CD+G titles are rare collectibles.** Confirmed artists include:
Alphaville, Anita Baker, Crosby Stills & Nash, Fleetwood Mac, Simply Red, Talking Heads.
*[No specific UPCs found in available sources — search eBay for "CD+G" explicitly.]*

| Status | Type | Description | Notes |
|--------|------|-------------|-------|
| ❌ | CD+G disc | Any commercially pressed CD+G | Search eBay for "CD+G karaoke"; artist titles above are confirmed. Ensure it is a standard pressed CD, not a CD-R. |

#### CD+EG / CD+XG (Extended Graphics)

Same subchannel R–W encoding as CD+G; enhanced to 288×192 pixels at up to **256 colours**
(vs. 16 for CD+G). Very limited commercial adoption — "very few, if any, CD+EG discs have
been published." IEC 60908 does not contain full CD+EG specifications; annexes available
from Philips at additional cost. *Low acquisition priority.*

#### CD-MIDI / CD+MIDI

CD-MIDI stores MIDI performance data rather than PCM; CD+MIDI combines CD+G with MIDI data.
Both defined as Red Book extensions. *[No confirmed commercial releases found. Low priority.]*

#### HTOA (Hidden Track One Audio)

Not a formal extension — an authoring practice. Audio stored in the pregap before Track 1
(index 0, negative LBA). Accessible by manually seeking backward from Track 1 on most
players. Requires reading negative-LBA sectors from the drive; the Plextor PX-716A handles
this. No special acquisition needed — the Technotronic disc may already have pre-gap data
worth inspecting.

---

### Subchannel / Unorthodox Usage

#### Standard channel roles (P–W)

- **P**: Pause flag — 2+ consecutive seconds of all-1s marks new track start
- **Q**: Primary control (TOC/position Mode 1; MCN Mode 2; ISRC Mode 3); 16-bit CRC
- **R–W**: "Unused" per Red Book; used by CD+G, CD-TEXT, and copy protection schemes

#### LibCrypt (PlayStation 1 — game protection, subchannel Q corruption)

Not a CD-DA protection, but the most thoroughly documented example of subchannel Q
exploitation. Stored a 16-bit decryption key as intentionally corrupted Q subchannel data
at specific MSF locations (Minute=03, sectors 5 apart; backup at Minute=09 on some discs).
Game code checks subchannel at random gameplay points; wrong key = lockup. Used by ~100 PAL
PS1 games. Ripping requires SBI (subchannel information) files or raw subchannel preservation;
DiscImageCreator and redumper both capture this.

*Reference*: https://red-j.github.io/Libcrypt-PS1-Protection-bible/index.htm

#### Copy protection via Q-channel timestamp corruption (general)

Standard game protection approach: modify sector timestamps in Q subchannel of data track
sectors to intentionally wrong values. Game code reads back the subchannel of specific
sectors; if timestamps are correct (as a CD-R would produce), the game detects a copy and
locks up. Most CD burners write correct timestamps, not the "nonsense" values of the
original — this asymmetry is the detection vector.

#### Hidden data in R–W subchannels (commercial audio CDs)

No documented case of hidden payload data in R–W subchannels on commercial audio CDs beyond
overt CD+G and CD-TEXT. Community speculation exists but no confirmed examples.

---

## Lead-in / Lead-out Technical Reference

### Physical size of the lead-in

**Authoritative source: IEC 60908:1999 (Second Edition), §7.2 and §17.5.1** — verified
from the licensed copy at `private/IEC_60908-1999.pdf`.

The standard specifies **physical bounds only** — no fixed sector count is given for the
lead-in. The relevant dimensions are:

| Parameter | Value | Section |
|-----------|-------|---------|
| Starting diameter of lead-in area | ≤ 46 mm (≤ 23 mm radius) | §7.2.2 |
| Starting diameter of program area | 50 mm (25 mm radius, −0/+0.4 mm) | §7.2.3 |
| Radial width of lead-in band | ~2 mm | derived |
| Theoretical sector capacity | ~11,750 sectors (~157 s) | calculated |

**Calculation**: at track pitch 1.6 µm and average radius 24 mm, the 2 mm lead-in band
holds ~1,250 revolutions × ~9.4 sectors/revolution ≈ **11,750 sectors** of CLV track
capacity. The standard does not mandate that all of this is filled with TOC data; it
requires only that the TOC be "continuously repeated in the lead-in area" (§17.5.1).

**The commonly cited 4,500-sector (60 second) figure is empirical**, not a specification
requirement. It reflects what is observed on typical pressed discs. Some discs may have
shorter or longer lead-in data. The 6,750-sector figure cited by some community sources is
also unconfirmed by the specification.

The standard 2-second pre-gap before Track 1 adds **150 sectors** (§17.4, §17.5.1). The
pre-gap is part of the program area structure, not the lead-in area proper.

The program area inner boundary is at radius 25 mm; the lead-in occupies the spiral track
from ≤ 23 mm to 25 mm. The hub clamping area (0–13 mm radius) is physically inaccessible
to the laser.

### Why the lead-in area is so large

The TOC is repeated continuously across the entire lead-in band so that a player can acquire
the disc's track structure quickly on spin-up even if it misses some sectors (the laser takes
time to stabilise on track). The redundancy is the Red Book's answer to the mechanical
uncertainty of the early tracking servo. For a 12-track disc, the actual TOC data is tiny —
the rest is repetition and silence in the audio channels. The standard (§17.5.1) mandates
this continuous repetition explicitly; it does not mandate how many sectors the repetition
spans.

The spec also reserves the structural area for:
- Copy protection attack surface (Key2Audio, Cactus Data Shield used the lead-in structure)
- CD-TEXT packs (stored in R–W channels of the lead-in; ~5 KB available in lead-in,
  extending into program area if needed)
- Repeated P/Q/R–W subcode across all lead-in sectors

### Why reading backward into the lead-in matters for preservation

1. **HTOA (Hidden Track One Audio)**: Audio before Track 1 (index 0, negative LBA). Any
   audio in the 2-second standard pre-gap requires reading negative-LBA sectors.
2. **Disc write offset**: Physical discs are not pressed with perfect sector alignment. The
   combined disc write offset (drive read offset + disc write offset) shifts audio data.
   For a disc with a large **negative** combined offset, Track 1 audio begins before LBA 0
   — the samples are in the pre-gap and must be read there for a complete dump.
3. **Lead-in subcode data**: Very rarely, non-zero data exists in early lead-in sectors
   (the TOC area, beyond the pre-gap). Only PLEXTOR drives can access this via the
   negative-LBA SCSI extension.
4. **Copy protection forensics**: Key2Audio and Cactus Data Shield structured the lead-in
   to confuse drives; reading the lead-in raw reveals the attack mechanism.

### What limits access to the lead-in

**Mechanical limit**: The laser pickup assembly has a finite inward travel range. At some
point the pickup physically cannot track further toward the hub. This is the absolute
hardware limit — no firmware or software can overcome it.

**Firmware limit**: Most drive firmwares clamp negative-LBA addressing at LBA -75 (the last
1 second of pre-gap) for audio CDs using the 0xD8 CDDA read command. Data CDs permit access
to approximately LBA -142 to -143. The Plextor firmware exposes a proprietary SCSI extension
allowing negative-LBA addressing into the full pre-gap and part of the TOC area.

**SCSI command layer**:
- **READ TOC (0x43)**: Standard MMC; returns firmware-parsed TOC data, not raw subcode.
  No raw lead-in access.
- **READ CD (0xBE)**: Reads sectors as raw audio data; used by redumper for main area.
- **0xD8 (CDDA Read)**: Legacy/proprietary CDDA read; audio CDs capped at LBA -75.
- **PLEXTOR negative-LBA extension**: Proprietary SCSI extension allowing sector addressing
  below LBA 0; enables reading into the TOC area of the lead-in.

**OS/driver layer**: Raw SCSI passthrough (SG_IO on Linux) requires root privileges.
cdrdao's normal operation uses standard MMC and does not need root. redumper's `dump::extra`
requires `doas`/`sudo` for raw SCSI lead-in access.

### Drive capability comparison (Redump community data)

| Drive family | Lead-in pre-gap access | TOC area access | Lead-out access | Notes |
|--------------|----------------------|-----------------|-----------------|-------|
| PLEXTOR (PX-716A etc.) | Full 150 sectors (LBA -150 to -1) | Yes — via negative-LBA SCSI extension | ~100 sectors | Only family confirmed to read TOC area; preferred for archival |
| LG / ASUS / LITE-ON (standard firmware) | ~135 of 150 pre-gap sectors | No | Cache-based only | Sufficient for 99% of discs |
| ASUS BW-16D1HT + RibShark OmniDrive (2026-02-18) | ~135 of 150 sectors | No | **Full lead-out, no cache** | Best lead-out access; accepted for Redump dumps and verifications |
| Generic drives | Variable, often unreliable | No | Minimal | Not recommended for preservation |

### Variation in lead-in depth: what is actually accessible vs. inaccessible

The physically accessible lead-in (for the best drives) runs from approximately LBA -150
(the start of the standard 2-second pre-gap) inward to wherever the laser pickup runs out
of travel. The pre-gap itself (LBA -150 to -1) is the main target — this is where HTOA
lives and where negative-disc-write-offset correction is needed.

The TOC area (beyond LBA -150, extending as far as the disc's lead-in was recorded — often
cited as ~LBA -4500 empirically) is accessible only to PLEXTOR drives via the proprietary
extension. The community observes that most discs contain only repeated Q-subcode TOC data
and silence in the audio channels in this region — the spec-mandated redundancy. A small
fraction of discs have been found to contain non-zero data in these sectors. This is not a
routinely present feature; its significance is disc-specific.

The innermost portion of the lead-in (closest to the clamping area) is inaccessible to all
known drives due to the physical travel limit of the pickup. No known firmware or software
hack extends access past this mechanical limit. There are no known cases of meaningful audio
or payload data being confirmed in this physically inaccessible region.

### Potential value of extending lead-in access

For practical preservation purposes: reading the full 150-sector pre-gap (LBA -150 to -1)
is the meaningful target and the Plextor already achieves this. The additional TOC-area
sectors readable by the Plextor extension are valuable for completeness but rarely contain
anything beyond spec-required repeated TOC data. No application beyond Redump-grade forensic
preservation currently exploits this deeper access for general advantage.

---

## cdda2img — Implementation Ideas from Research

Feature gaps identified by comparison with the software landscape above:

| Feature | Status | Source of insight |
|---------|--------|-------------------|
| CUE+audio input (APE+CUE, FLAC+CUE) | ❌ TODO | ImgBurn, DAEMON Tools, UltraISO all support this; trivial since PyAV decodes APE/FLAC |
| CD-TEXT read from subchannel (physical disc) | ❌ TODO | Plextor driver reads it via R–W subchannel; already in TODO |
| CD-TEXT in generated TOC | ✅ Done | generate_toc() writes CD_TEXT block |
| ISRC per track in rip | ❌ TODO | Plextor cdrdao driver extracts it; needs to flow into RBI container |
| MCN / CATALOG from rip | ❌ TODO | cdrdao logs "Found disk catalogue number"; parse into RBI TOC |
| Subchannel P–W optional block | ❌ TODO (flag reserved) | CloneCD .sub; CD+G preservation |
| DDP 2.0 import | ✅ Done | `ddp_reader.py`; GEAR Pro writes s16le to TRACK*.DAT (verified empirically); `cdda2img import <dir>` — first open-source DDP 2.0 reader for Linux |
| DDP 2.0 export | ❌ Planned | ddpLib (GPL-3.0 Java) is the only open-source reference; no Python/Rust implementation; RBI PCM block is already s16le so no conversion needed |
| AccurateRip v1/v2 | ❌ TODO | Standard verification step |
| HTOA detection and capture | ❌ TODO | Requires reading negative-LBA pre-gap via Plextor driver |
| Enhanced CD (CD Extra) rip | ❌ TODO | Two-session; cdrdao `--session 1` extracts audio; foreign importer must skip non-AUDIO tracks |
| Variable sector size input | ❌ Out of scope | IsoBuster — cdda2img is CDDA-only (2352 bytes per sector) |

---

## DDP (Disc Description Protocol) — Technical Reference

### Ownership and spec availability

DDP is proprietary — spec owned by **Singulus Mastering** (successor to Doug Carson &
Associates). License required from DCA/Singulus to implement from spec. The license page
is at `http://www.dcainc.com/products/ddplicense/index.html`. The spec is not publicly
indexed; any open-source implementation must be reverse-engineered from binary files or
from GPL-licensed code that already parsed them.

The only open-source DDP reader is **ddpLib** (Java, GPL-3.0):
- https://github.com/suntriprecords/ddpLib (origin, 2011)
- https://github.com/fabmars/ddpLib (fork)
- https://github.com/NonStaticEu/ddplib (fork)

The only free DDP writer is **cue2ddp** by Andreas Ruge — binary-only (no source):
- http://ddp.andreasruge.de/

**No Python or Rust DDP implementation exists.** ddpLib's GPL-3.0 Java source is the
primary reverse-engineering reference for binary field layouts.

### DDP versions

| Version | Scope | Status |
|---------|-------|--------|
| 1.00 / 1.01 | CD-DA, CD-ROM; no CD-TEXT | Obsolescent; no reason to generate |
| **2.00** | CD-DA, CD-ROM + CD-TEXT | **Universal standard for CD pressing** |
| 2.10 | Adds DVD types | Required for DVD; irrelevant for CD-only |
| 3.00 | HD DVD, Blu-ray | Not relevant |

**Target: DDP 2.0.** Every pressing plant accepts it; it adds CD-TEXT over 1.x.

### Package structure

A DDP image is a **directory of named files**, not a single container. Filenames for the
audio and subcode streams are not fixed — they are specified within DDPMS.

| File | Role | Format |
|------|------|--------|
| `DDPID` | Entry point / version identifier | Binary, ~128 bytes; contains DDP version string and stream count |
| `DDPMS` | Map Stream — disc layout directory | Stream of **128-byte records**; maps track segments to audio/subcode streams |
| `*.DAT` (name from DDPMS) | Audio data | Raw **s16le**, 44100 Hz, stereo — no header; all tracks concatenated |
| `PQDESCR` / `SD` / `DDPPQ` (name from DDPMS) | PQ Subcode Stream | Q-channel data per disc frame; BCD-encoded timing, track/index, ISRC, MCN |
| `CDTEXT.BIN` | CD-TEXT (optional, DDP 2.0+) | MMC-3 Pack binary format with checksums |

**DDPMS 128-byte record** (confirmed structure from multiple independent sources):
Each record maps one disc segment — track boundary, pregap, postgap — to its audio stream
offset, subcode stream offset, and stream type codes. The DDPMS is the bridge between the
logical disc layout and the physical file data.

**PQDESCR / PQ Subcode Stream** — Q channel encoding:
One packet per sector. Q channel structure (98 frames of 96 bits each):
- 4 bits control (pre-emphasis, copy-permit, audio type, two-channel flag)
- 4 bits ADR/mode (1 = timing/position, 2 = MCN, 3 = ISRC)
- 72 bits data (mode-dependent; see below)
- 16-bit CRC

Mode 1 (timing) data fields (BCD-encoded):
- TNO (track number), X (index), MM:SS:FF (relative time), MM:SS:FF (absolute time)

Mode 2 (MCN/UPC) data fields:
- 13 BCD digits (EAN-13 / UPC-A), zero-padded to 12 + leading 0

Mode 3 (ISRC) data fields:
- 12-character ISRC code (country + owner + year + serial)

All time and track number fields are **BCD-encoded** — each decimal digit packed as 4
bits in a byte. This is the same encoding used in subchannel Q on the physical disc.

**Audio data (.DAT file)**:
- Raw **s16le** 16-bit signed PCM, 44100 Hz, stereo interleaved (L, R, L, R...)
- No header — pure byte stream; all tracks concatenated with pre/post-gap silence included
- This is the **same byte order as the RBI PCM block** — no conversion needed for DDP export

### Relationship to cdda2img

The RBI container already holds everything needed to generate DDP 2.0:

| DDP requirement | RBI / cdda2img source |
|-----------------|----------------------|
| Audio stream (s16le PCM) | RBI PCM block (already s16le) |
| Track boundaries and timing | RBI TOC (start_frame, duration_frames per track) |
| ISRC per track | Subchannel Q from Plextor rip (TODO: propagate into RBI) |
| MCN / Catalog | cdrdao TOC `CATALOG` field (TODO: propagate into RBI) |
| CD-TEXT | RBI TOC TITLE / PERFORMER fields |
| Pre-gap / post-gap timing | Plextor driver pre-gap detection (in cdrdao TOC `START` offsets) |

**Implementation path** (without spec license):
1. Read ddpLib GPL-3.0 Java source to extract binary field layouts for DDPID, DDPMS,
   PQDESCR. The parser code directly implies the byte-level format.
2. Implement `ddp.py` (or `ddp.rs` for Rust): serialisers for each file in the package.
3. Expose as `cdda2img ddp album.rbi` → output directory with complete DDP 2.0 package.
4. Validate against cue2ddp-generated reference packages (produce same DDP from same
   source, compare binary output).
5. Submit to a pressing plant for acceptance testing.

**Note on the s16be/s16le distinction**: Physical CD sectors contain s16be audio (as raw
cdrdao output confirms). DDP package audio files use s16le — **verified empirically
(2026-05-05)** by byte-level comparison of GEAR Pro TRACK01.DAT against a cdrdao s16be BIN
of the same pressing (Technotronic *Pump Up the Jam*): byte-swapping the DAT produces an
exact match; the raw DAT does not match. The RBI pipeline byte-swaps on cdrdao ingest
(s16be → s16le); DDP import requires no byte-swap. DDP export requires no conversion —
the RBI PCM block feeds the DDP .DAT file directly.

---

## CD-XA (CD-ROM Extended Architecture) — Technical Reference

CD-XA is a Yellow Book extension (Sony/Philips/Microsoft, 1991) that adds real-time
interleaved audio, video, and data to CD-ROM Mode 2. It is **not CD-DA** — its audio
uses ADPCM compression, not Red Book PCM. CD-XA is the foundation for Photo CD, Video CD,
CD-i, and PlayStation 1 game audio (STR/XA streams).

### Sector layout

All CD sectors are 2352 bytes. CD-XA adds an 8-byte subheader (written twice as 4+4
for resilience) to Mode 2:

| Type | Sync | Header | Subheader | User data | EDC | ECC | Total |
|------|------|--------|-----------|-----------|-----|-----|-------|
| Mode 1 (Yellow Book) | 12 | 4 | — | 2048 | 4 | 276 | 2352 |
| Mode 2 Form 1 (XA) | 12 | 4 | 8 | 2048 | 4 | 276 | 2352 |
| Mode 2 Form 2 (XA) | 12 | 4 | 8 | 2324 | 4 | — | 2352 |

**Form 1** (bit 5 of subheader Submode byte = 0): full EDC+ECC, for data requiring
integrity. **Form 2** (bit 5 = 1): EDC only, no ECC — for real-time A/V where a dropped
frame is preferable to stalling the decoder.

### ADPCM audio parameters

- Sample rates: 37.8 kHz or 18.9 kHz (never 44.1 kHz — that is CD-DA)
- Stereo or mono
- 4-bit or 8-bit samples per ADPCM byte
- Compression ratio: 4:1 to 16:1 vs. uncompressed PCM

### Relevance to cdda2img

CD-XA audio tracks are out of scope — cdda2img handles Red Book CD-DA (44.1 kHz, s16le
PCM) only. Intersection points:

- **Mixed-mode discs**: data track (Mode 1 or Mode 2) in Track 1 of a single session,
  followed by CD-DA audio tracks. cdrdao TOC marks these as `MODE1`, `MODE2_FORM1`, etc.
  The foreign TOC importer must skip non-AUDIO tracks rather than erroring.
- **Enhanced CD** (see below): Session 2 uses Mode 2 Form 1; Session 1 is CD-DA only.
  No XA decoding needed — Session 2 is ignored entirely.
- **PS1 games**: XA ADPCM audio interleaved in data tracks. Entirely out of scope.

---

## Enhanced CD (CD Extra / Blue Book — IEC 61310-3) — Technical Reference

Enhanced CD is a **two-session format** that achieves backward compatibility by placing
audio in Session 1 and data in Session 2:

- **Session 1**: Standard Red Book CD-DA audio tracks. Any audio player reads only this
  session — it sees a normal audio disc.
- **Session 2**: Exactly one CD-ROM XA Mode 2 Form 1 data track with ISO 9660 filesystem
  (optionally + HFS for Mac). Contains PC multimedia content. Required directories per
  Blue Book spec: `CDPLUS/` and `PICTURES/`. May include `autorun.inf`.

### Contrast with Mixed-Mode CD

| Feature | Mixed-Mode CD | CD Extra (Blue Book) |
|---------|---------------|----------------------|
| Sessions | 1 | 2 |
| Track order | Data (Track 1), then audio | Audio (Session 1), then data (Session 2) |
| Audio player behaviour | May play Track 1 as noise | Sees only Session 1; fully backward-compatible |

### Critical sector offset issue

Session 2's ISO 9660 Volume Descriptor encodes sector offsets as **absolute to LBA 0
of the physical disc**, not relative to Session 2's start. A raw ISO image extracted from
Session 2 cannot be directly mounted — filesystem pointers are wrong relative to the
extracted image's base. Tools such as IsoBuster compensate; naive tools fail.

### Ripping

**cdrdao**: `--session 1` restricts read to the audio session. Correct approach for
cdda2img's `r` subcommand when an Enhanced CD is detected.

**whipper**: Known freeze issue on some Enhanced CDs during initial disc probing
(GitHub issue #256, open). Drive-specific; workaround not documented.

**EAC (Windows)**: Most reliable for Enhanced CD audio extraction in practice.

### Relevance to cdda2img

- **`rip` subcommand**: detect multi-session disc (cdrdao reports session count in TOC);
  automatically restrict to `--session 1` for audio. Log that Session 2 data was ignored.
- **Foreign TOC importer**: skip any non-AUDIO track type in the parsed TOC; do not error
  on MODE1/MODE2_FORM1 tracks.
- **`create` subcommand (creating Enhanced CD)**: out of scope for now — requires two burn
  passes with `--multi`; not a standard archival use case for RBI.

### Confirmed Enhanced CD albums

- Prince — *1999* (1999 reissue) — widely cited in preservation discussions
- Many major-label mid-1990s / early-2000s releases had Enhanced CD editions; check
  the Redump "Enhanced CD" category for confirmed entries with TOC data.
