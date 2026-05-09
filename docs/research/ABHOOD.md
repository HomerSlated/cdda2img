# A Brief History of Optical Discs

*Research notes for the cdda2img project. CDDA-focused, but covers the full optical disc family.*

---

## Table of Contents

1. [From Inception to the Present Day](#1-from-inception-to-the-present-day)
2. [The Rise and Fall of Physical Media](#2-the-rise-and-fall-of-physical-media)
3. [DRM: A History of Locks](#3-drm-a-history-of-locks)
4. [The Rippers: Circumventing the Locks](#4-the-rippers-circumventing-the-locks)
5. [Tools of the Trade](#5-tools-of-the-trade) (inc. §5.4 CD Drive Technical Requirements)
6. [From CDDA to ISO 9660: Why Red Book Is Not a Filesystem](#6-from-cdda-to-iso-9660-why-red-book-is-not-a-filesystem)
7. [Formats, Variants, and Deliberate Violations](#7-formats-variants-and-deliberate-violations)
8. [The Case for a Formal Optical Disc Archival Tool](#8-the-case-for-a-formal-optical-disc-archival-tool)

---

## 1. From Inception to the Present Day

### 1.1 The Pre-History: LaserDisc and the Optical Principle (1963–1978)

The conceptual groundwork for optical disc storage was laid in 1963 by David Paul Gregg and James Russell, who developed a transparent disc-based optical recording system. Russell's patents, later acquired by MCA, described the fundamental principle that would drive all subsequent formats: encoding information as a series of microscopic pits and lands that could be read by a focused laser beam without physical contact.

The first commercial realisation was **LaserDisc** (LD), marketed initially in the United States in 1978 under the name *DiscoVision* by MCA, with Pioneer and Philips as hardware partners. LaserDisc was an analogue video format — the audio was either analogue FM-modulated or, in later "digital audio" variants, PCM-encoded. It never achieved mass-market success outside Japan, but it served as the proof-of-concept for the optical disc industry and, critically, as the technology seedbed from which the CD emerged. Pioneer produced LaserDisc players until 2009.

### 1.2 The Compact Disc (1979–1982)

In 1979, Philips and Sony jointly developed the Compact Disc system. The two companies had competing designs — Philips favoured 115 mm diameter and 14-bit audio; Sony favoured 120 mm and 16-bit audio. The 120 mm format won, partly because it could hold Beethoven's Ninth Symphony (74 minutes and 33 seconds in Karajan's 1951 EMI recording) in its entirety — a requirement that, according to a popular industry legend, was mandated by Sony's executive Norio Ohga.

The joint Philips-Sony specification was documented in what became known informally as the **Red Book**, and was first published in 1980. It was formally standardised by the International Electrotechnical Commission (IEC) as **IEC 60908:1987** (first edition), prepared by IEC subcommittee 100B (Audio, video and multimedia information storage systems). A second edition, **IEC 60908:1999**, superseded the first and incorporated Amendment 1 (1992) and its corrigendum.

The **Red Book** standard defines a prerecorded optical reflective digital audio disc system. Key parameters (paraphrased from IEC 60908:1999):

- **Disc dimensions**: 120 mm diameter standard disc; 80 mm diameter miniature disc. Central hole 15 mm, thickness 1.2 mm.
- **Audio specification**: Two-channel (stereo), 16-bit linear PCM, 44,100 Hz sampling rate — the parameters derived from the maximum digital bandwidth of a modified PAL or NTSC video recorder, the then-standard professional mastering medium.
- **Modulation**: Eight-to-Fourteen Modulation (**EFM**). Each 8-bit data byte is mapped to a 14-channel-bit pattern chosen to minimise transitions, with 3 merging bits added between symbols. This limits the minimum and maximum pit/land run lengths to maintain tracking and clock recovery.
- **Error correction**: **CIRC** (Cross-Interleaved Reed-Solomon Coding). Data is interleaved across multiple frames and protected by two layers of Reed-Solomon code (C1 and C2). This provides resilience to both random errors and burst errors (scratches), and allows interpolation or muting when errors cannot be corrected.
- **Frame structure**: Each frame contains 24 bytes of audio data, 8 bytes of error-correction parity, and 1 byte of subcode. At 75 frames per second and 588 stereo samples per frame, one second of audio occupies 75 × 2,352 = 176,400 bytes.
- **Track geometry**: Constant Linear Velocity (CLV), 1.2–1.4 m/s. Track pitch 1.6 μm. Minimum pit length 0.833 μm. The disc spins faster at the inner edge and slower at the outer, unlike a vinyl record (which uses constant angular velocity).
- **Disc layout**: Lead-in area (containing the Table of Contents in the Q subcode channel), programme area (up to 99 tracks), and lead-out area. The design intentionally provides "landing areas" of silence around each track, because early laser tracking was imprecise.

The first commercial CD was the ABBA album *The Visitors*, pressed in August 1982. The first dedicated CD player for consumers, the Sony CDP-101, launched in Japan on 1 October 1982.

### 1.3 The Subcode System and CD+G, MIDI, CD TEXT

The **subcode** system is one of the most underappreciated aspects of the Red Book design. Each CD frame carries one byte of subcode data, divided into 8 single-bit channels designated **P through W**.

- **Channel P**: A binary flag indicating track boundaries and pauses (largely superseded by Q).
- **Channel Q**: Carries the Table of Contents (in the lead-in area), absolute and relative track time, ISRC codes (per ISO 3901:1986), and the Media Catalogue Number (MCN, the disc-level EAN barcode). This is the primary metadata channel used by all players.
- **Channels R through W**: Six bits per frame, available for ancillary data. These channels are mostly unused on standard CDDA discs; on specialised discs they carry graphics, text, or MIDI data.

The R–W channels are the mechanism for several important CDDA extensions, all formally incorporated into IEC 60908:1999 (clauses 20–26):

- **ZERO mode** (MODE = 0): All R–W bits zero; the default for standard CDDA discs.
- **LINE GRAPHICS mode** (MODE = 1, ITEM = 0): **CD+G** (Compact Disc + Graphics). Defined by Philips and Sony as an extension of the Red Book, CD+G encodes low-resolution graphics (288 × 192 pixels, 16 colours from a palette of 4096) in the R–W channels. The first commercially released CD+G disc was *Eat or Be Eaten* by Firesign Theatre (1985). CD+G became the dominant karaoke format in Japan and subsequently worldwide, with dedicated CD+G karaoke players becoming standard household appliances in East Asia during the 1990s. The IEC standard defines two further CD+G variants: **TV-GRAPHICS mode** (MODE = 1, ITEM = 1) and **EXTENDED TV-GRAPHICS mode** (MODE = 1, ITEM = 1 & 2), the latter providing a larger colour table and more display instructions.
- **MIDI mode** (MODE = 3, ITEM = 0): Embeds MIDI event data synchronised to the audio. Never widely deployed.
- **USER mode** (MODE = 7, ITEM = 0): Reserved for application-specific data.
- **CD TEXT mode** (MODE = 2 and MODE = 4): Standardised in the MMC-3 specification (backed by Sony, September 1996) and incorporated into IEC 60908:1999 as clause 26. Stores album title, disc title, track titles, performer names, songwriter and composer information, and disc identifiers in the lead-in area and optionally in the programme area. CD TEXT is the only mechanism on a standard CDDA disc for embedding textual metadata directly on the disc. Its support in consumer CD players was always uneven; many drives cannot read it, and many ripping tools do not extract it.

### 1.4 CD-ROM and the Yellow Book (1984–1985)

In 1984, Philips and Sony extended the CD system to store arbitrary data, publishing the **Yellow Book** specification. CD-ROM (Mode 1) adapts the 2,352-byte Red Book audio frame by replacing 2,048 bytes with user data and using the remaining bytes for an additional layer of error correction (EDC/ECC) — essential for data applications where a single bit error is catastrophic, unlike audio where interpolation is acceptable. Mode 2 drops the extra ECC in favour of more user data, and was later refined into Mode 2 Form 1 and Form 2 in the **Green Book** (CD-i).

### 1.5 CD-ROM/XA, Mixed Mode, and CD Extra (1988–1995)

The **Yellow Book** evolved through several extensions:

- **CD-ROM/XA** (eXtended Architecture, 1988): Combined audio and data in interleaved sectors (Mode 2 Forms 1 and 2), enabling synchronised audio-visual playback without buffering. Developed by Philips, Sony, and Microsoft.
- **Mixed Mode CD** (also called Yellow+Red): A disc with a data track (Track 1) followed by audio tracks. Used widely for PlayStation games (data session, then audio) and early multimedia CD-ROMs.
- **CD Extra** (also called CD Plus or Enhanced CD, 1995): A multisession format with audio tracks in Session 1 and a data session in Session 2. This allowed standard CD players to play the audio while computers could access the data session. The specification (*CD EXTRA, Enhanced music CD specification, Version 1.0, December 1995, Sony/Philips*) is normatively referenced within IEC 60908:1999 — meaning CD Extra is part of the Red Book family, not merely an ad-hoc industry extension.
- **Photo CD** (1992): Kodak's proprietary format for storing digitised photographs on CD.

### 1.6 CD-R and CD-RW: The Orange Book (1988–1997)

The **Orange Book** (Philips and Sony, 1988–1990) defined recordable and rewritable CD media:

- **Part I**: CD-MO (Magneto-Optical) — never achieved mass-market adoption.
- **Part II**: **CD-R** (Compact Disc Recordable / CD-WO, Write Once). A pre-grooved spiral (the ATIP wobble) guides the laser; cyanine, phthalocyanine, or azo organic dyes darken under write-laser heat, simulating pits. The ATIP (Absolute Time In Pre-groove) encodes the disc manufacturer's 16-bit MID code (12-bit manufacturer identifier registered with Philips, 4-bit variant), disc type, recommended recording speed, and lead-in/out timing. The first standalone CD-R drive was the Philips CDD521 (1990), priced at approximately US$10,000.
- **Part III**: **CD-RW** (Compact Disc ReWritable). Uses a phase-change alloy (typically AgInSbTe) that can be switched between crystalline and amorphous states repeatedly. CD-RW discs have lower reflectivity than CD-R and require player compatibility.

As detailed in `private/OFE.md`, the Orange Book specifications are distributed only to parties with a paid CD Information Agreement, and the authoritative MID registry is likewise paywalled — a constraint that continues to affect open-source tools.

**High Definition Compatible Digital (HDCD)**: Pacific Microsonics' proprietary CD-compatible audio format (1995). HDCD encodes extended dynamic range and subtle dither information in the LSB of the audio stream, transparent on standard players. Microsoft acquired the technology in 2000; it has since been discontinued.

### 1.7 DVD: The Silver Book Family (1996–2006)

By 1994, the limitations of CD capacity for video were apparent. Two competing formats emerged: Sony/Philips' MMCD and Toshiba/Matsushita's Super Density (SD) disc. Under pressure from Hollywood studios (particularly Warren Lieberfarb of Warner Bros.) to avoid a format war, the two camps converged on a joint specification in 1995, published as the **DVD** (Digital Versatile Disc) standard.

The **DVD Forum**, founded formally in 1997, became the first genuinely open industry consortium for optical disc standards — a deliberate contrast with the bilateral Philips–Sony CD regime.

Key DVD formats:

- **DVD-ROM / DVD-Video** (1996): Single-layer 4.7 GB, double-layer 8.5 GB. 120 mm disc, 0.6 mm substrate (compared to 1.2 mm for CD), track pitch 0.74 μm, 650 nm laser. Video encoded as MPEG-2.
- **DVD-Audio** (1999–2000): High-resolution audio, up to 24-bit/192 kHz stereo or 24-bit/96 kHz 5.1 surround. Never achieved significant market penetration, partly due to format war with SACD.
- **SACD** (Super Audio CD, 1999): Developed by Sony and Philips. Uses Direct Stream Digital (DSD), a 1-bit delta-sigma modulated format at 2.8224 MHz. Hybrid SACD discs contain both a standard CD layer and one or two SACD layers. Like DVD-Audio, SACD failed to achieve mainstream adoption, though it retains a dedicated audiophile following. Both formats were eclipsed by the "loudness war" of compressed MP3 distribution.
- **DVD-R, DVD+R, DVD-RW, DVD+RAM**: Two competing recordable DVD formats, distinguished primarily by different industry consortia and slight technical differences in the lead-in/linking sectors. The + format (DVD+RW Alliance) offered better random-access writing; the – format (DVD Forum) had broader player compatibility early on.

### 1.8 High-Definition: The Blue-Violet Laser Generation (2000–2010)

The move to blue-violet laser (405 nm) allowed track pitches of 0.32 μm (Blu-ray) or 0.34 μm (HD DVD) — approximately half that of DVD — enabling much higher data density.

- **HD DVD** (Toshiba et al., 2006): 15 GB single-layer, 30 GB double-layer. Adopted by Microsoft (Xbox 360 add-on) and several studios. Lower production cost due to similarity to existing DVD manufacturing lines.
- **Blu-ray Disc** (Sony et al., 2006): 25 GB single-layer, 50 GB double-layer. Higher capacity but initially more expensive to manufacture. Backed by Sony, Panasonic, Samsung, LG, and crucially, by Sony Pictures and Fox. The PlayStation 3 (2006) was a Trojan horse for Blu-ray adoption.

The format war ended on 19 February 2008 when Toshiba announced it would cease HD DVD production, conceding to Blu-ray after Warner Bros. and several major retailers switched exclusivity.

Further Blu-ray developments:

- **BDXL** (2010): Triple-layer (100 GB) and quad-layer (128 GB) discs, primarily for professional and broadcast archival use.
- **Ultra HD Blu-ray (4K UHD)** (2015): 66 GB single-layer, 100 GB double-layer (same physical format as standard Blu-ray but with higher data rate). Video encoded as HEVC (H.265) at up to 3840 × 2160 resolution with HDR (HDR10, Dolby Vision). Governed by AACS version 2.

### 1.9 M-Disc: Archival Permanence (2009–present)

**M-DISC** (Millennial Disc), introduced by Millenniata in 2009, is a write-once optical disc using a patented inorganic recording layer (described as "glassy carbon" or a stone/rock-like composite) rather than organic dye. The write laser physically engraves pits into this layer rather than chemically altering it, making the recording effectively immune to oxidation.

M-DISC is available in DVD+R (4.7 GB) and Blu-ray BD-R (25 GB, 50 GB, 100 GB BDXL) form factors, readable in any standard DVD or Blu-ray drive. It was tested by the US Department of Defense and the French National Laboratory of Metrology in 2009 and 2012; the NIST Interagency Report NIST IR 8387 (2022) lists it as an acceptable archival format rated for 100+ years. The Library of Congress has evaluated it for archival use.

As of 2025, no 4K UHD M-DISC has been released.

### 1.10 The Loudness War and the 16-bit Question

As noted in the Spoon's Audio Guide, the CD format provides 16-bit / 44.1 kHz audio — a resolution that, in ideal conditions, is generally considered to exceed the auditory threshold of human perception. However, the industry practice of dynamic range compression ("the loudness war"), which peaked around 2000–2008, effectively reduced the dynamic range of commercial releases well below the theoretical 96 dB ceiling of 16-bit audio. A 1980s CD would sound quiet by comparison to a 2005 release mastered to near-maximum loudness throughout.

The technical ceiling of 16-bit audio was not the constraint; the constraint was mastering choices made for commercial reasons.

---

## 2. The Rise and Fall of Physical Media

### 2.1 The Peak

The CD was the most successful consumer physical media format in history. Global CD sales peaked around 2000, when approximately 2.4 billion discs were sold worldwide. Music CDs displaced vinyl records and cassette tapes within roughly a decade of their launch — a transition that had no precedent in recorded music history for speed and completeness.

DVD video followed a similar trajectory, peaking around 2005–2006.

### 2.2 The Fall

The decline of physical media has been driven by three interlocking forces:

1. **Broadband internet** made downloading practical from around 2000 (Napster, LimeWire, BitTorrent).
2. **Legal download stores** (iTunes Music Store, 2003) offered per-track purchasing at lower friction and cost than physical media.
3. **Streaming services** (Spotify, 2008; Apple Music, 2015; Tidal, 2014) eliminated the purchase transaction altogether, replacing ownership with access.

By 2017, digital streaming revenue had overtaken physical and download sales for the first time in the US music industry. The structural decline of retail: Best Buy ceased selling CDs in 2018 (and DVDs in 2023); Target stopped selling DVDs in 2023; Netflix ceased its disc-by-mail service in 2023; Redbox closed in 2024.

### 2.3 "You Will Own Nothing"

The transition from physical media to streaming represents a fundamental legal and philosophical shift in how consumers relate to cultural content. On a physical CD or DVD, the buyer owns a tangible object. Copyright law grants the buyer the right of first sale: the right to lend, resell, or give away the physical artefact, and the right to make a personal archival copy under fair-use doctrines in many jurisdictions.

In the streaming and digital-purchase model, the consumer owns nothing. They hold a **revocable licence** to access content under the platform's Terms of Service. This licence:

- Can be revoked if the platform loses distribution rights (as happened to customers of Microsoft Movies, Google Play Movies, and Vudu/Fandango when content was removed from libraries after purchase).
- Expires if the platform ceases operations or discontinues the service.
- Cannot be lent, resold, or gifted.
- Is not protected by first-sale doctrine in most jurisdictions (courts have consistently ruled that digital purchases are licences, not sales).

The phrase "you will own nothing" — originating in a World Economic Forum context — has become shorthand in physical-media preservation communities for this broader trajectory. The practical consequence for cultural heritage is that streaming-era releases may become inaccessible when business models change, platform companies fail, or licensing agreements expire. Physical media, including properly archived CDDA, has a well-understood longevity and does not require a third party to remain operational.

### 2.4 The Vinyl Revival and Physical Media's Second Wind

Paradoxically, vinyl record sales have grown for 16 consecutive years (as of 2024), outselling CDs in the US for the first time since 1987 with approximately 41 million units versus 33 million. Physical media continues to command a dedicated collector market, particularly for music. Blu-ray sales remain commercially viable for premium film releases. The streaming model's ownership limitations are creating renewed interest in physical ownership among consumers aware of the risks.

---

## 3. DRM: A History of Locks

### 3.1 SCMS — Serial Copy Management System (1987)

The first digital audio copy-protection mechanism deployed on consumer formats. SCMS embeds flags in the audio stream (in the DAT subcode and in the IEC 60958 consumer digital audio interface) that allow a first-generation copy to be made from an original but prevent the copy from being further copied. SCMS was required by the US Audio Home Recording Act (1992).

SCMS was trivially circumvented: it applies only to consumer DAT and MiniDisc recorders, not to computer CD drives, which were not subject to the Act's requirements.

### 3.2 Audio CD Copy Protection (2000–2007)

The major record labels engaged in a series of increasingly desperate and ultimately counterproductive attempts to prevent CD ripping:

1. **Fake second session / corrupted TOC**: A second data session with an invalid TOC was added. Standard CD players read only the first session; computers were expected to use the last session and be confused. Better rippers could force first-session reading; drives with "over-read" capability could read across session boundaries. The *black marker pen* workaround became legendary: drawing on the outer edge of the disc with a felt-tip pen obscured the second session.

2. **Intentional audio errors** (*MediaMax*, *Key2Audio*, *Cactus Data Shield*): Deliberate uncorrectable errors were inserted into the audio stream. CD players would interpolate (silence) these; computer drives might not. The result was audio artefacts in ripped files — but also potentially in standard players with poor error correction. This was genuinely damaging to the product.

3. **Trojan software** (*XCP*, *MediaMax by SunnComm*): Enhanced CD autorun content secretly installed DRM software on Windows PCs. The most notorious case was Sony BMG's XCP rootkit (2005), developed by First 4 Internet. The rootkit hid files from the operating system, opened a significant security vulnerability (exploited by malware within weeks of its discovery), was installed without user consent on approximately 22 million CDs, and triggered a class-action lawsuit, government investigations, and a product recall. Sony BMG reached settlement agreements in multiple countries. The incident was a watershed event that effectively ended the audio CD copy-protection experiment.

None of these mechanisms complied with the Red Book standard, making affected discs technically non-standard. The Spoon's Audio Guide summarises: *"It is safe now to say that the experiment with audio CD protections has failed."*

### 3.3 CSS — Content Scramble System (DVD, 1996)

CSS was the copy protection scheme deployed on commercial DVD-Video discs. It used a proprietary 40-bit stream cipher to encrypt the disc contents, with title keys stored in the lead-in area and media keys distributed to licensed DVD player manufacturers. The 40-bit key length was a direct consequence of 1990s US export restrictions on cryptography, which forbade strong encryption in consumer devices.

The effective security was further weakened by structural flaws in the algorithm that reduced the practical key space to approximately 16 bits — brute-forceable in under a minute on a 450 MHz processor.

CSS licences required DVD player software to keep keys confidential. The scheme provided no security against authorised player software that exposed its keys.

### 3.4 AACS — Advanced Access Content System (HD DVD / Blu-ray, 2005)

AACS was designed by Intel, IBM, Microsoft, Matsushita, Sony, Toshiba, Warner, and Disney to address CSS's weaknesses. It uses 128-bit AES encryption with a key revocation mechanism: if a player's keys are compromised, AACS-LA can issue new disc pressings that revoke those keys, preventing the compromised player from playing new releases.

Deployed in 2006 on both HD DVD and Blu-ray, AACS was cracked by December of that year ("Muslix64"), less than six months after commercial release. The underlying vulnerability was not the cryptographic strength but the necessity of the decryption keys being present in memory on the playing device — keys that could be extracted with a debugger.

**BD+** was an additional layer of protection used on a subset of Blu-ray releases (notably Fox) from October 2007. BD+ is a virtual machine embedded in the player's firmware that executes disc-resident code; the disc can inspect the player environment and refuse playback if it detects circumvention tools. BD+ was cracked in 2008.

**AACS version 2** (AACSv2) was deployed alongside 4K UHD Blu-ray in 2015 with enhanced key management. It was publicly cracked at the 37th Chaos Communication Congress (December 2023, presentation "Full AACSess"), confirming that the fundamental vulnerability — the necessity of keys in accessible player memory — remains unresolvable in software-based players.

### 3.5 CPRM / CPPM (Recordable/Pre-recorded DVD, 1997)

Content Protection for Recordable Media (CPRM) and Content Protection for Pre-recorded Media (CPPM) applied to recordable DVD formats and DVD-Audio. Based on the Cryptomeria cipher (C2). Less relevant to archival work since they primarily affect DVD-Audio and encrypted DVD-R recordings.

### 3.6 Region Coding

DVD region codes (1–6, plus 8 for aircraft, 0 for region-free) are not a cryptographic mechanism but rather a hardware enforcement of geographical market segmentation baked into the player firmware and disc stamper. Blu-ray uses regions A, B, and C. Region codes are enforced at the player level, not the disc data level, and represent a form of market control rather than copy protection.

---

## 4. The Rippers: Circumventing the Locks

### 4.1 Digital Audio Extraction: The Foundation

Unlike data CDs, which return sector-perfect data through standard read commands, CD-DA audio drives initially provided no mechanism for computers to extract digital audio samples — audio was only accessible through the analogue output. "Ripping" required the development of custom SCSI command sequences that would request raw audio sectors.

The first widely available implementation on Linux/Unix was **cdda2wav** (Heiko Eißfeldt, early 1990s), a command-line tool that used direct sector-read commands to extract CDDA audio. cdda2wav was later incorporated into the **cdrtools** package by Jörg Schilling.

### 4.2 cdparanoia: The Accuracy Problem

Even once direct sector reading was possible, audio extraction was unreliable. Unlike data sectors, audio sectors have no checksum or error-correction metadata visible to the host — the CIRC correction happens inside the drive, and the host receives only the post-correction (or post-interpolation) audio bytes. A drive with poor tracking, a scratched disc, or a cache that served stale data would return incorrect audio without any error indication.

**cdparanoia** (Monty Montgomery / Xiph.Org, 1998) addressed this by reading each sector multiple times, comparing the results, and implementing jitter correction to handle the inconsistent sector offsets returned by drives. cdparanoia became the de facto standard for accurate audio extraction on Linux and is the backend used by many Linux ripping tools.

### 4.3 DeCSS and the DVD Cracking Era (1999)

On 6 October 1999, Jon Lech Johansen (age 15) and two anonymous collaborators posted **DeCSS** to the LiViD mailing list. DeCSS decrypted CSS-protected DVD content. The CSS decryption key had been reverse-engineered from the Xing DVD player software, where keys were stored with minimal obfuscation.

The legal fallout was extensive. Johansen was raided by Norwegian police in 2000 and prosecuted under Norwegian Criminal Code section 145; he was acquitted of all charges in 2003 and again on appeal. In the United States, the MPAA pursued multiple civil cases under the DMCA, targeting websites that hosted DeCSS. The 2600 magazine case (*Universal City Studios v. Reimerdes*, 2000) resulted in an injunction against DeCSS distribution — but also demonstrated that such content, once published, could not effectively be suppressed on the internet.

The CSS crack enabled the creation of a generation of DVD ripping and playback tools: **DVD Decrypter**, **DVDShrink**, **Handbrake**, **VLC** (which implemented libdvdcss independently), and many others.

### 4.4 Audio CD DRM Circumvention

The audio CD DRM experiments of 2000–2007 were trivially defeated:

- **Fake second session**: Defeated by first-session-only ripping mode in EAC and cdrecord; defeated physically by the pen trick.
- **Corrupted audio errors**: Better drives with stronger error correction simply corrected them. Where they couldn't be corrected, they were interpolated or identified by secure rippers. The protection created compatibility problems with standard players before rippers.
- **XCP rootkit**: Removed by tools released within days of the rootkit's public discovery. The autorun mechanism (the vector for installation) was widely disabled following the Sony BMG scandal.

### 4.5 AACS and Blu-ray Circumvention (2006–present)

The AACS crack timeline:
- **December 2006**: Muslix64 posts processing key extraction tool for HD DVD.
- **January 2007**: Blu-ray AACS also cracked.
- **2007**: AACS-LA begins key revocation on new pressings, but the community maintains an up-to-date database of keys (the "key exchange" model).
- **2008**: BD+ cracked.
- **2013**: **MakeMKV** becomes the standard consumer tool for Blu-ray ripping (using AACS keys maintained in a regularly-updated internal database).
- **2023**: AACSv2 (4K UHD) publicly cracked. **MakeMKV** adds 4K UHD support.

The underlying lesson — that any DRM system requiring key presence in accessible memory is fundamentally unresolvable — has been demonstrated repeatedly and is now widely accepted among security researchers.

### 4.6 AccurateRip: Quality Assurance for Rippers (2001)

The problem with secure ripping was not just whether the bits were extracted but whether they were *correct* in the sample-accurate sense. **AccurateRip** (created by the dBpoweramp developer, ca. 2001) addressed this by building a crowd-sourced CRC database: when users rip a disc, a checksum of each track (computed after applying drive offset correction) is submitted to a central server and compared against all previous submissions. A match with high confidence (many submissions from different drives and disc copies) indicates a correct rip. A mismatch indicates either a damaged disc or a drive problem.

AccurateRip fundamentally changed the standard for what constituted a "good" rip, and its database (now containing hundreds of millions of track checksums) is the closest thing the community has to a ground-truth verification system for CDDA rips.

---

## 5. Tools of the Trade

### 5.1 Digital Audio Extraction (Ripping) Tools

| Tool | Platform | Notes |
|------|----------|-------|
| **cdda2wav** | Linux/Unix | First widely-used DAE tool; part of cdrtools (Heiko Eißfeldt) |
| **cdparanoia** | Linux | Accurate ripping with jitter correction; backend for many tools (Monty Montgomery, 1998) |
| **Exact Audio Copy (EAC)** | Windows | Andre Wiethoff, 1998; audiophile standard for 20+ years; secure ripping with AccurateRip integration |
| **dBpoweramp CD Ripper** | Windows/Mac | Commercial; integrated AccurateRip; used by the Library of Congress |
| **CDex** | Windows | Free/open-source; uses cdparanoia algorithm; straightforward UI |
| **Audiograbber** | Windows | Simpler interface than EAC; discontinued |
| **Whipper** | Linux | Modern Python-based ripper; integrated AccurateRip and drive offset database |
| **Max** | macOS | GUI frontend for cdparanoia and FLAC/MP3 encoding |
| **X Lossless Decoder (XLD)** | macOS | Accurate ripping with AccurateRip; preferred macOS tool |
| **abcde** | Linux/Unix | Script-based; pipelines cdparanoia + encoder + CDDB/MusicBrainz |
| **fre:ac** | Cross-platform | Free audio converter with CD ripping; open source |

### 5.2 Disc Burning and Authoring Tools

| Tool | Platform | Notes |
|------|----------|-------|
| **cdrecord / cdrtools** | Linux/Unix/Solaris | Jörg Schilling; the canonical Unix CD burning engine; cdrkit (fork) in Debian |
| **cdrdao** | Linux/Unix | TOC-based CD burning; produces `.toc` + `.bin` format; better for audio than cdrecord |
| **Nero Burning ROM** | Windows | The dominant Windows CD/DVD authoring tool, 1997–2015; commercial |
| **Roxio Easy CD Creator** (later **Toast**) | Windows/Mac | Consumer-oriented; acquired Adaptec's software; Toast for macOS |
| **ImgBurn** | Windows | Free; excellent disc-image writing; widely used for archival accuracy |
| **k3b** | Linux (KDE) | GUI frontend for cdrecord/cdrdao; long the best Linux GUI burner |
| **Brasero** | Linux (GNOME) | Simpler GNOME-native burner |
| **Alcohol 120%** | Windows | Disc image mounting and writing; produces MDS/MDF format |
| **CloneCD** | Windows | Produces CCD/IMG/SUB format; pioneered subchannel-accurate copying |
| **DVD Shrink** | Windows | DVD compression and region-stripping; uses DeCSS |
| **HandBrake** | Cross-platform | Open-source video transcoder; handles CSS-encrypted DVDs via libdvdcss |

### 5.3 Disc Image Tools

| Tool | Platform | Notes |
|------|----------|-------|
| **dd** | Unix | Low-level block copy; no disc-specific intelligence |
| **ddrescue** | Linux | Robust copy with error recovery; suitable for deteriorating discs |
| **DiscImageCreator** | Windows/Linux | High-fidelity image creation including subchannel; supported by Redump project |
| **MakeMKV** | Cross-platform | Blu-ray and DVD decryption and extraction; integrated AACS key support |
| **DVD Decrypter** | Windows | Classic tool (discontinued 2005 after legal pressure); CSS/RCE/Macrovision removal |
| **IsoBuster** | Windows | Commercial; comprehensive image format support; recovery from damaged discs |
| **cdda2img** | Linux/Unix | This project; creates and extracts RBI (Red Book Image) archive format |

### 5.4 CD Drive Technical Requirements for Accurate Dumping

The Redump preservation project has formalised the technical criteria a CD drive must satisfy to produce a verifiably accurate disc image. These requirements are relevant to any archival tool that aspires to the same accuracy standard.

**Scrambled-mode dumping.** The drive must be capable of returning raw, pre-descrambled sector data. Standard consumer CD drives apply a hardware XOR descrambler to the EFM-decoded bitstream before delivering data to the host; drives used for accurate preservation must bypass or expose this stage. Scrambled mode is a prerequisite for correctly capturing the relationship between main-channel data, subchannel data, and C2 error vectors at the frame level.

**Subchannel support.** The drive must be capable of reading and returning all subcode channels: the full P–W set (8 channels per frame). In practice this means returning subchannel data in RAW multiplexed form (96 bits per sector — one bit per channel per frame × 98 frames). Redump requires, at minimum:
- **Channel Q** for TOC, ISRC, MCN, and timing data — the primary metadata channel.
- **Channels R–W** for CD+G, CD TEXT, and other R–W encoded extensions.

Subchannel Q is corrected in memory by the dumping tool (since Q carries a CRC); raw Q is never written to disc uncorrected. The MMC sector ordering spec defines `DATA_C2_SUB` (data bytes, then C2 error vector, then subcode), but some drives return `DATA_SUB_C2` — a practical incompatibility that dumping tools must detect and handle.

**C2 error pointers.** The drive must support and reliably report C2 error pointer vectors — one bit per byte of main-channel data indicating that the CIRC corrector failed to recover the byte. A drive that silently interpolates uncorrectable bytes gives the host no signal of data uncertainty; a drive with C2 support lets the host distinguish confident reads from guesses. Reliable C2 support is a hard requirement for Redump acceptance; drives where C2 pointers are unreliable (firing on good data, or not firing on bad data) are disqualified. Note that C2 error pointers are defined in the SCSI MMC specification, not in the Red Book itself.

**Lead-in sector depth.** The drive must be capable of reading at least 75 sectors of lead-in (one second at 75 frames/second — the Red Book minimum lead-in duration). In the edge case of Audio CDs with large positive write offsets, genuine audio data may be present in the pre-gap ahead of the nominal Track 1 start; such discs may require access to up to all 150 sectors of the lead-in pre-gap to capture the full audio content.

**Lead-out sector depth.** Similarly, the drive must read at least 75 sectors of lead-out (the silent region after the last track). Large negative write offsets on Audio CDs can push audio data into the lead-out, requiring more than 75 sectors of lead-out access.

**Preferred tool and drive posture.** Redump's current preference is **redumper** (by superg) as the primary dumping tool. DiscImageCreator (DIC) is not acceptable for Audio CDs in general; for data discs, only verified-good Plextor drives are accepted when using DIC. This reflects both the C2 reliability issue (many drives pass C2 but with systematic false-positive or false-negative behaviour) and the Audio CD write offset problem, where the drive's ability to read deep into the lead-in and lead-out is critical.

**Write offset detection.** The cumulative disc-level write offset (the number of samples by which all data on the disc is shifted relative to the nominal frame boundaries) is calculated by the dumping tool using one or more of: the addressing difference between data sector MSF and subchannel Q MSF on a data track; the data/audio track intersection (using the "BE" read method); silence-based "Perfect Audio Offset" detection; and CDi-Ready data in index 0 detection. The drive offset of the specific drive model (a fixed per-model correction for the mechanical read head position) is subtracted from the disc write offset to obtain the net correction. Drive offset values are maintained in community databases such as AccurateRip's drive offset registry.

### 5.5 Playback Software

| Tool | Platform | Notes |
|------|----------|-------|
| **WinAmp** | Windows | The MP3 revolution's primary beneficiary; 1997 onwards |
| **VLC** | Cross-platform | Free and open source; handles encrypted DVDs via libdvdcss |
| **foobar2000** | Windows | Audiophile playback; gapless; WASAPI output |
| **Clementine / Strawberry** | Cross-platform | Open-source; CD playback and library management |

---

## 6. From CDDA to ISO 9660: Why Red Book Is Not a Filesystem

### 6.1 The Nature of CD-DA

A Red Book audio CD is not a filesystem in any computer-science sense of the term. It is a **stream of audio data** with a minimal TOC structure. The disc's Table of Contents, encoded in the Q subcode channel of the lead-in area, contains:

- The number of tracks
- The starting time (in MM:SS:FF absolute disc time) of each track
- The lead-out starting time

That is all. There are no filenames, no directories, no file sizes, no access permissions, no file metadata. Audio data is not associated with named files — it is simply a continuous stream of PCM audio divided into segments by the TOC. The track "files" that appear on your desktop when you insert a CD (usually named `Track 01.cda`, `Track 02.cda`, etc.) are **virtual constructs** created by the operating system, not data present on the disc. The `.cda` shortcut files are 44-byte stubs containing only the track index and start/end addresses.

This design was deliberate: the engineers optimised for streaming reliability and backward compatibility with consumer audio hardware, not for random-access data storage. The disc rotates at Constant Linear Velocity; there is no concept of addressing a specific "file", only a specific point in time on the disc.

### 6.2 ISO 9660: The Data Filesystem for Optical Discs

When CD-ROM was developed in 1984 (Yellow Book), the question of how to organise files on a disc accessible to multiple computer architectures became pressing. The **High Sierra** working group (1985) produced a proposed standard that was adopted by ECMA as **ECMA-119** (1986) and by ISO as **ISO 9660:1988**.

ISO 9660 defines a hierarchical directory structure stored in the programme area of a CD-ROM Mode 1 disc. Like any filesystem, it provides:
- Named files and directories
- File sizes and creation timestamps
- A volume descriptor block at sector 16 (the first 16 sectors are reserved for the system area)
- Optional path tables for faster directory traversal

ISO 9660 Level 1 was extremely restricted (8.3 filenames, uppercase only, 8 directory levels max) — a lowest-common-denominator design for cross-platform compatibility. Extensions were later standardised:
- **Rock Ridge** (1993): POSIX-compliant extensions for Unix systems (long filenames, symbolic links, device files, ownership/permissions)
- **Joliet** (Microsoft, 1995): Unicode long filenames for Windows
- **El Torito** (1995): Bootable CD specification
- **ISO 9660:1999** (Level 3): Long filenames without requiring extensions

### 6.3 UDF: The Universal Disc Format

For DVD and subsequent formats, ISO 9660 was supplanted by **UDF** (Universal Disc Format, OSTA, 1995 / ECMA-167). UDF supports larger files, unicode filenames, incremental (packet) writing, and the structured authoring required for DVD-Video. UDF 2.5 and 2.6 are used for Blu-ray.

### 6.4 The Boundary Between Audio and Data Layers

The fundamental insight is that the Red Book and Yellow Book share the same **physical encoding layer** — EFM modulation, CIRC error correction, CLV rotation, the same disc geometry — but differ in what they put in the 2,352-byte payload area of each sector:

- **Red Book audio sector**: 2,352 bytes of raw PCM audio (588 stereo samples). No EDC/ECC. Error correction must succeed within the drive or the result is interpolated/muted.
- **Yellow Book Mode 1 data sector**: 12 bytes of sync, 4 bytes of header, 2,048 bytes of user data, 4 bytes of EDC, 8 bytes of zero-padding, and 276 bytes of ECC. Total: 2,352 bytes.

This means a mixed-mode CD can contain both audio sectors and data sectors on the same physical disc — they are distinguished only by reading the appropriate sector type. A data session on a mixed-mode disc is effectively a CD-ROM mounted within a disc that also contains Red Book audio. This architectural decision, made in 1984, is why mixed-mode handling is complex: tools must read both sector types correctly, and the indexing for the TOC and the ISO 9660 volume descriptor are entirely separate systems that happen to share a physical medium.

---

## 7. Formats, Variants, and Deliberate Violations

### 7.1 Standard CDDA Variants

| Format | Description |
|--------|-------------|
| Standard CD-DA | Red Book; up to 99 tracks, up to ~80 min (74 min by spec, 80+ with overburning) |
| Gapless (live mix) | No 2-second inter-track silence; tracks share a single audio stream, indices only |
| Hidden Track One Audio (HTOA) | Audio in the pre-gap of Track 1 (before absolute time 00:00:00); accessible by seeking back from Track 1 |
| Hidden last track | Long silence inserted at the end of the last advertised track; or many 2-second silence tracks preceding it |
| CD+G | Subcode R–W channels carry graphics; MODE=1, ITEM=0 per IEC 60908 §21 |
| CD+MIDI | Subcode R–W channels carry MIDI; MODE=3, ITEM=0 per IEC 60908 §24 |
| CD TEXT | Subcode R–W channels carry title/artist text; MODE=2/4 per IEC 60908 §26 |
| DualDisc | One side CD-DA; opposite side DVD-Video; rejected by RIAA as non-standard; thinness caused compatibility issues |
| CD Extra (CD Plus) | Multisession: Session 1 = audio; Session 2 = data. Defined in Sony/Philips CD EXTRA spec v1.0 (1995), normatively referenced in IEC 60908:1999 |
| HDCD | Sub-LSB encoding for extended dynamic range; backward compatible; now discontinued |

### 7.2 CD-ROM and Data CD Variants

| Format | Description |
|--------|-------------|
| CD-ROM Mode 1 | Yellow Book; 2,048 bytes user data + EDC/ECC; standard data disc |
| CD-ROM Mode 2 | Yellow Book; 2,336 bytes user data; no ECC; used in some older games |
| CD-ROM/XA Form 1 | Mode 2 with EDC/ECC; 2,048 bytes user data |
| CD-ROM/XA Form 2 | Mode 2 without ECC; 2,324 bytes; used for audio/video sectors in CD-i and Photo CD |
| Video CD (VCD) | MPEG-1 video + audio; CD-ROM/XA Form 2; popular in Asia pre-DVD |
| Super VCD (SVCD) | MPEG-2 video; better quality than VCD |
| CD-i (Green Book) | Philips Interactive; CD-ROM/XA with CD-i OS; commercial failure |
| Photo CD | Kodak; XA Form 2; image storage; multisession |
| Game CD (PlayStation) | Data track (Track 1) + audio tracks; Mode 2 data with anti-copying measures |

### 7.3 Recordable and Rewritable Formats

| Format | Standard | Notes |
|--------|----------|-------|
| CD-R | Orange Book Part II | Write-once; organic dye; 74–80 min (standard) to 100 min (overburn) |
| CD-RW | Orange Book Part III | Phase-change; ≈1,000 erase/rewrite cycles; lower reflectivity |
| DVD-R / DVD+R | DVD Forum / DVD+RW Alliance | 4.7 GB; competing formats; mostly interoperable |
| DVD-RW / DVD+RW | As above | Rewritable |
| DVD-RAM | DVD Forum | Random-access rewritable; cartridge form; professional use |
| BD-R | Blu-ray Disc Association | 25/50/100 GB; two organic dye types (HTL/LTL) |
| BD-RE | Blu-ray Disc Association | Rewritable; phase-change |
| M-DISC | Millenniata/Verbatim | Write-once; inorganic layer; archival-rated |

### 7.4 Non-Standard Formats: Deliberate Spec Violations

Several copy-protection systems deliberately violated the Red Book or Yellow Book standards to create discs that standard equipment could read for playback but that ripping tools or disc duplicators would fail to copy correctly.

**Audio CD copy protection (music)**:

- **Key2Audio** (Sony DADC): Corrupted TOC in the lead-in, causing computers to fail to enumerate tracks. CD players tolerate TOC errors better than drives following a strict spec.
- **MediaMax** (SunnComm): Hidden audio data in the pre-gap area; install a Windows driver to handle it. The first version was defeated by holding the Shift key (preventing autorun).
- **Cactus Data Shield** (Midbar Tech): Intentional errors in the audio sectors that CD player interpolation would mask but computer drives would return as errors.
- **XCP** (First 4 Internet / Sony BMG): Autorun rootkit on Enhanced CD; installed without consent; triggered class-action lawsuits and government investigation.

**PC game copy protection (data)**:

- **SafeDisc** (Macrovision): Embeds a digital signature in "weak sectors" — sectors written with deliberately marginal signal quality. The weakness is that standard burners cannot reproduce the weak-sector signal signature. SafeDisc 1 used an executable signature on the first track; later versions (2.9+) added increasingly sophisticated sector-level anomalies. Defeated by image-mount tools (Daemon Tools, Alcohol 120%) and eventually by subchannel-aware rippers (DiscImageCreator). Windows Vista+ broke SafeDisc compatibility, and Microsoft declined to fix it.
- **SecuROM** (Sony DADC): Used Data Position Measurement (DPM) — measuring the physical variation in pit position density on the disc — as a fingerprint that could not be reproduced by a standard burner. Evolved through many versions, each adding new detection methods. Version 7+ added online activation, making physical copy protection nearly irrelevant.
- **StarForce**: Russian copy protection; installed a low-level kernel driver; notorious for causing system instability; circumvented by image mounting.
- **LaserLock**: Hidden directory containing corrupted data; causes errors during duplication.
- **Tages**: French protection; used "twin sectors" — pairs of sectors at the same address returning different data; exploited deliberate anomalies in the optical pickup's behaviour.

These formats represent a paradox for preservation: they are, by definition, discs that standard tools are designed not to copy. Faithfully archiving them requires either raw sector capture with subchannel data (as implemented in DiscImageCreator, used by the Redump preservation project) or the use of drives that can tolerate and capture the anomalous signals.

### 7.5 DVD and Blu-ray Variants

- **DVD-Video**: MPEG-2, up to 8 audio tracks, 32 subtitle streams, menu system, CSS encryption, region codes, Macrovision analogue output protection.
- **DVD-R DL / DVD+R DL**: Dual-layer; 8.5 GB.
- **DVD-ROM**: Data-only; no CSS by default.
- **BD-VIDEO**: MPEG-4 AVC or VC-1 video; AACS encrypted; BD-J (Java) menus; region coded A/B/C.
- **Ultra HD Blu-ray**: HEVC video; HDR10/Dolby Vision; 7.1 lossless audio (Dolby Atmos / DTS:X); AACSv2; no 4K region coding (all 4K UHD discs are region-free by specification).
- **BD-ROM Mark** (ROM Mark): A forensic watermark physically embedded in Blu-ray disc substrate during mastering; cannot be copied by consumer recorders. One of the few DRM elements that addresses the disc layer, not the data layer.

---

## 8. The Case for a Formal Optical Disc Archival Tool

### 8.1 The Preservation Problem

The current state of optical disc archival is fragmented, platform-specific, and inadequately standardised. The primary tools are:

- **EAC** with AccurateRip for Windows-oriented accurate audio ripping
- **Whipper** for Linux with AccurateRip and proper drive offset handling
- **cdparanoia** as the underlying extraction engine on Unix/Linux
- **DiscImageCreator** and the **Redump** project for game disc preservation, including subchannel and non-standard formats
- **MakeMKV** for Blu-ray decryption

None of these tools produces a self-contained archival format that:
1. Preserves all CDDA metadata (CD TEXT, MCN, ISRC) in a structured, machine-readable form
2. Stores the raw audio in a well-documented lossless format alongside its TOC
3. Is format-version-stamped and cryptographically verified
4. Handles multi-disc releases as a single logical unit
5. Is readable and writable by open-source tools on all major platforms
6. Separates concerns cleanly: extraction, encoding, metadata, and container are independent stages

### 8.2 The Licensing Obstacle

As documented in `private/OFE.md`, the Orange Book specification and MID registry are paywalled. Any tool that attempts accurate manufacturer identification from ATIP data must maintain its own best-effort table. The table will be necessarily incomplete and will contain guesses — a condition Jörg Schilling embedded directly in cdrecord's output messages.

The broader licensing apparatus for the Rainbow Books creates a hostile environment for open-source archival tools. The Red Book (IEC 60908) is technically a public IEC standard, but costs money to purchase; the Orange Book, used for any CD-R/RW media identification, is not publicly accessible. A formal archival tool cannot claim authoritative media identification.

### 8.3 The Accuracy Problem

AccurateRip is the only widely-used ground-truth verification system for CDDA rips, but:
- It is a proprietary database owned by Illustrate (the dBpoweramp developer)
- It requires an internet connection at rip time
- It provides no coverage for newly ripped discs not yet in the database
- Its algorithm (track CRC with drive offset correction) is published but the database itself is not open
- It does not cover exotic disc types (CD+G, HTOA, discs with intentional errors)

A formal archival tool should ideally compute and store AccurateRip checksums (for compatibility with the existing ecosystem) but also implement an independent verification mechanism not dependent on a third-party proprietary service.

### 8.4 The "You Will Own Nothing" Argument for Archival Tools

The transition to streaming and digital licences makes formal physical-media archival tools more urgent, not less. As streaming services remove titles from their catalogues, as labels withdraw releases, and as digital storefronts close, the physical disc becomes the only permanent record of a release. An individual who owns a CD owns, in law, a permanent artefact; an individual who streams the same music owns nothing.

A well-designed archival tool serves the same function for audio as **Redump** serves for game disc preservation: it provides a standardised, verified, reproducible way to create an archive copy of a physical medium that is legally owned by the individual. Unlike Redump (which archives disc images in proprietary or ad-hoc formats), a purpose-designed tool with an open, well-specified container format (such as RBI) enables the long-term preservation community to build on a stable foundation.

### 8.5 Requirements for a Standard Archival Format

Drawing from the analysis above, a formal CDDA archival format should:

1. **Container format**: Self-describing, version-stamped, integrity-verified (cryptographic checksums). Binary container with a well-specified header.
2. **Audio**: Raw s16le PCM (the canonical Red Book format), parameters stored in header. No lossy encoding.
3. **TOC**: Full cdrdao-format or equivalent TOC, including MCN, ISRC per track, pre-gaps and indices, and all track types.
4. **Metadata**: CD TEXT fields, embedded tags from audio files, and provenance information (rip date, drive model, drive offset, AccurateRip confidence scores).
5. **Multi-disc support**: Logical grouping of multiple discs within the same release.
6. **Verification**: AccurateRip checksums per track (for ecosystem compatibility), plus independent internal checksums.
7. **Extensibility**: Reserved flags and optional blocks for future additions (subchannel data, watermarks, non-standard sector data) without breaking backward compatibility.
8. **Openness**: Fully specified, no licensing requirements for implementation, all supporting tools open source.

The **RBI (Red Book Image)** format under development in this project represents one approach to satisfying these requirements. Its evolution should be informed by the lessons of every previous format, standard, and proprietary scheme catalogued above.

---

## References and Further Reading

### Standards
- **IEC 60908:1999** — *Audio recording — Compact disc digital audio system* (second edition). International Electrotechnical Commission. *(Licensed copy held privately.)*
- **ECMA-119** — *Volume and File Structure of CDROM for Information Interchange* (ISO 9660 equivalent).
- **ECMA-394** — *12 cm Recordable Compact Disc (CD-R)*. Freely available from ECMA.

### Specifications (paywalled)
- **Orange Book Parts I–III** — Philips IP&S (CD-R, CD-RW). Requires CD Information Agreement.
- **Yellow Book** — Philips IP&S (CD-ROM).

### Legal and economic analysis
- Kung-Chung Liu, *"The Taiwanese 'Philips' CD-R Cases: Abuses of a Monopolistic Position, Cartel and Compulsory Patent Licensing"*, SSRN 1831275.
- *Princo Corp. v. U.S. Philips Corp.*, 616 F.3d 1318 (Fed. Cir. 2010, en banc).
- *Universal City Studios v. Reimerdes*, 111 F. Supp. 2d 294 (S.D.N.Y. 2000) — the DeCSS DMCA case.

### Project-internal research
- `private/OFE.md` — The Orange Forum Embargo: what it is, who operates it, and its implications for open-source tools.
- `private/IEC_60908-1999.pdf` — Full text of IEC 60908:1999 (licensed copy).
- `private/NONSPEC.md` — Lead-in and Lead-out: What They Contain, What They're Forced to Contain, and Where the Spec Breaks. Covers write offsets, copy protection attacks on the lead-in, HTOA, and pre-mastering edge cases.
- `private/DRIVES.md` — Per-drive technical notes for drives used or evaluated. Currently: Lite-On LH-20A1S (AccurateRip offset +6, 969 submissions; optical quality assessment; Redump compatibility checklist).
- `private/spoons-audio-guide-cd-ripping.txt` — dBpoweramp's Spoon's Audio Guide: practical guide to secure ripping, drive features, copy protection.
- `private/libmirage/` — libmirage image format parser source: authoritative reference implementations for CUE/BIN, CCD/IMG/SUB, MDS/MDF, NRG, TOC formats.

### Community projects
- **Redump** (redump.org) — Authoritative game disc preservation database and checksums. The wiki at wiki.redump.org documents drive compatibility requirements in detail (see §5.4 above).
- **AccurateRip** (accuraterip.com) — CDDA rip verification database.
- **Hydrogenaudio Knowledgebase** — Community wiki on audio formats, ripping, and encoding.
- **NIST IR 8387** (2022) — *Optical Disc Long-Term Storage*. US government evaluation of archival disc media including M-DISC.
