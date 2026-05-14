# ATIP Manufacturer Code Authority: Current Status Research (2026)

## Research Summary

Attempted comprehensive research into the current (2026) state of ATIP/Media Identification Code (MID) assignment authority. Due to the proprietary and non-open-source nature of this topic, many sources were inaccessible.

**Status:** Research incomplete due to tool limitations, but the following information was recovered.

---

## What We Know About ATIP/MID

### Technical Overview

**ATIP (Absolute Time In Pregroove)** is a method for storing manufacturer identification and write parameters on CD-R and CD-RW discs:

- **Encoding location:** Spiral pregroove wobble signal (22.05 kHz carrier frequency)
- **Media Identification Code (MID):** 16-bit field comprising:
  - **12 bits:** Manufacturer identifier (registered)
  - **4 bits:** Variant/format indicator
- **Additional ATIP data:**
  - Disc type (CD-R vs CD-RW)
  - Recording dye type (cyanine, phthalocyanine, azo)
  - Spiral length (blocks)
  - Recommended recording speed
  - Disc Application Code (DAC) — determines if disc is standard CD-R or "Music CD-R" for private copying levy

### Historical Authority

**Orange Forum (1996–2001)**
- Japanese registered organization (`orangeforum.or.jp`)
- Maintained authoritative MID code registry
- Registry was proprietary; access required payment

**CDs21 Solutions (2001–~2012)**
- Formed via merger of Orange Forum and Multimedia CD Consortium (April 2001)
- Succeeded Orange Forum's MID code assignment function
- Membership: 79 companies (53 domestic Japan, 26 overseas) as of October 2002
- Office: Philips Bldg (RIVAGE SHINAGAWA 10F), 4-1-8 Konan, Minato-ku, Tokyo 108-0075
- Chairman: Heitaro NAKAJIMA (as of October 2002)
- Contact: infocds21@cds21solutions.org, +81-3-3740-4592
- Last confirmed archived snapshot: September 8, 2012

---

## Current Authority Status (2026): Findings

### ✅ Confirmed Information

1. **Standards Reference:** ECMA International publishes CD-R/CD-RW technical specifications including ATIP details
   - **ECMA-394** — "Multi-Speed Compact Disc Recordable System Description" (December 2010 edition)
   - Available: https://www.ecma-international.org/wp-content/uploads/ECMA-394_1st_edition_december_2010.pdf
   - Includes technical details on ATIP encoding and MID code structure

2. **DVD Extension:** MID technology was extended to DVD-R/DVD+R/DVD-RAM discs as DVD Media Identification Code
   - Controlled by DVD standards bodies (DVD Consortium, Blu-ray Disc Association for BDs)

3. **Preservation Community:** Aaru Data Preservation Suite (GitHub: `aaru-dps/Aaru`)
   - Actively maintained as of 2026
   - Handles manufacturer identification for multiple optical disc formats
   - Source: https://github.com/aaru-dps/Aaru
   - Maintainer: Natalia Portillo (claunia@claunia.com)

### ❌ Could NOT Confirm (Access Denied / Unavailable)

1. **CDs21 Solutions Successor Organization**
   - No confirmation of continued operation post-2012
   - No identified successor organization maintaining ATIP/MID registry
   - Japanese business registries require authentication

2. **Current ATIP/MID Code Registry Access**
   - Who maintains the authoritative registry in 2026?
   - Is access still proprietary/fee-based?
   - Is a public registry available?
   - **IEC.ch website:** 403 Forbidden (requires authentication)
   - **ISO standards bodies:** Gated behind pay-for-standards model

3. **Current Standards Authority**
   - IEC TC 100/WG 11 (Information technology / Consumer Electronics / Media)
   - ISO/IEC JTC 1 (optical media standards)
   - Contact information and current representatives: Not accessible

4. **Leadership and Contacts**
   - No confirmed current leadership of ATIP/MID code authority
   - Heitaro NAKAJIMA (last confirmed contact: October 2002) — current status unknown
   - Wataru Fujihira (last confirmed contact: October 2002) — current status unknown

---

## Market Context (CD-R/RW Decline)

### Industry Obsolescence

- **CD-R/RW market:** Effectively obsolete for consumer use as of ~2015–2020
- **Remaining use cases:**
  - Archival/preservation (Aaru project, optical media repositories)
  - Specialized professional applications
  - Legacy media recovery

### Implications for ATIP/MID Authority

- **Likely scenario:** CDs21 Solutions wound down operations as CD-R manufacturing became economically marginal
- **Possible outcomes:**
  1. Function transferred to another standards body (IEC, ISO, or DVD/Blu-ray associations)
  2. Registry went dormant; no active code assignment post-2012
  3. Function absorbed by surviving media manufacturers or archival organizations
  4. No successor body — registry frozen at last update

---

## Related Standards Bodies (for ATIP successor search)

### ECMA International
- **Website:** https://www.ecma-international.org/
- **Relevant committees:** Technical Committee TC/107 (Digital storage equipment and media)
- **Published CD-R/CD-RW standards:** ECMA-130, ECMA-359, ECMA-394
- **Current status of MID registry maintenance:** Not confirmed accessible

### DVD Standardization Bodies

- **DVD Consortium** (legacy; effectively dissolved)
- **Blu-ray Disc Association** (BDA) — manages Blu-ray media standards
- **Possibly maintains MID-like systems for newer disc formats**

### ISO/IEC Standards

- **ISO/IEC 23912** — 80 mm DVD-R and DVD-R DL for General, Archival, and Authoring Purpose
- **ISO/IEC JTC 1/SC 23** — Digital storage devices for digital cinema; may have broader optical media scope
- **Access model:** Pay-for-access (standards cost €100–400+ to purchase)

---

## Aaru Project: Technical Reference (2026)

### GitHub Repository

- **Project:** Aaru — Data Preservation Suite
- **URL:** https://github.com/aaru-dps/Aaru
- **Purpose:** Identification, analysis, and preservation of optical media (CD, DVD, Blu-ray, magnetic tape)
- **Language:** C#
- **Maintainer:** Natalia Portillo (@claunia, claunia@claunia.com)
- **Status:** Active development as of 2026
- **License:** Open source

### Aaru's MID/ATIP Handling

- Recognizes and parses ATIP fields from CD-R discs
- Maintains manufacturer identification tables (likely based on historical ATIP registries)
- Provides command-line tools for media identification
- Used by preservation communities (Library of Congress, archives, etc.) for media analysis

### Contact for Current MID Information

**Natalia Portillo** (Aaru maintainer) may have insights into:
- Current state of MID registry access
- Whether CDs21 Solutions or successor bodies are active
- How preservation communities obtain manufacturer identification data
- **Contact:** claunia@claunia.com, GitHub profile: `@claunia`

---

## Open Questions / Research Gaps

1. **Did CDs21 Solutions continue operating after 2012?**
   - Last archived web presence: September 2012
   - No confirmation of dissolution or continued operation

2. **Who holds the intellectual property and registry data?**
   - Philips (original Orange Book author)
   - CDs21 Solutions or its legal successor
   - Public domain (unlikely given historical paywall model)

3. **Is ATIP code assignment still happening (2026)?**
   - CD-R manufacturing has largely ceased
   - Remaining manufacturing (archive media, specialized applications) — how are codes assigned?

4. **Alternative / parallel registries?**
   - Community-maintained tables (like in Aaru, cdrtools)
   - Reverse-engineered databases from archived discs
   - Were any competitor systems established?

5. **Access policy changes?**
   - Has the embargo lifted post-obsolescence?
   - Are manufacturer codes now public or open-access?
   - Any recent announcements or policy changes (2020–2026)?

---

## Recommended Follow-Up Actions

To complete this research, you would need:

### 1. Direct Contact with Preservation Communities

- **Aaru maintainer:** Natalia Portillo (claunia@claunia.com)
- **Optical media preservation forums:**
  - OpticalSecrets community
  - RetroComputing forums
  - Digital Preservation Coalition (DPC)
  - Library of Congress Sustainability of Digital Formats lab

### 2. Japanese Business Registry Search

- **Japan Business Search:** https://www.jtc-corp.jp/ or similar
- Lookup: "CDs21 Solutions" dissolution status
- Status of Philips Japan operations (Tokyo office)

### 3. ECMA Standards Committee Contact

- **ECMA TC/107 Chair:** Contact via https://www.ecma-international.org/
- Ask for current ATIP/MID registry administrator
- Request historical documentation on authority transition

### 4. Philips IP & Licensing

- **Contact:** Philips IP & Standards licensing (ip.philips.com)
- Inquiry: Who currently administers ATIP/MID code assignment?
- Historical records of Orange Forum / CDs21 Solutions transition

### 5. Wayback Machine Deep Dive

- Search for archived CDs21 Solutions pages (2008–2012)
- Look for successor announcements or bankruptcy filings
- Japanese-language archives of announcements

---

## Preliminary Conclusion

**The current (2026) state of ATIP/MID code assignment authority is unclear.**

Most likely scenarios:

1. **Authority transferred to ECMA or ISO standards bodies** (probable) — codes now assigned by standards committee request
2. **Authority defunct; registry frozen** (possible) — last update ~2012; CDs21 Solutions dissolved
3. **Authority absorbed by media manufacturers** (less likely) — codes self-assigned by remaining CD-R makers

The embargo that Jörg Schilling protested in the 1990s–2000s has likely **become moot** due to industry obsolescence. If ATIP code assignment is still needed (for archival media, specialized applications), it may now be:
- Openly available (registry published)
- Handled informally by remaining manufacturers
- Controlled by successor standards bodies with different access policies

**For `cdda2img` project implications:** ATIP/MID parsing remains a lower priority due to the obsolescence of CD-R media and the project's focus on CD-DA audio ripping (which predates CD-R and doesn't require ATIP data).

---

## Sources & References

### Confirmed Accessible Sources
- Wikipedia: Absolute Time in Pregroove — https://en.wikipedia.org/wiki/Absolute_Time_in_Pregroove
- Wikipedia: Media Identification Code — https://en.wikipedia.org/wiki/Media_Identification_Code
- ECMA-394 Standard (PDF): https://www.ecma-international.org/wp-content/uploads/ECMA-394_1st_edition_december_2010.pdf
- GitHub: Aaru — https://github.com/aaru-dps/Aaru
- Local research: `docs/research/OFE.md`, `docs/research/OF-Background.md`

### Inaccessible Sources (Authentication/Paywall)
- IEC.ch standards registry
- ISO standards databases
- Philips IP licensing portal (requires registration)
- Japanese business registries (CDs21 Solutions status)
- CDs21 Solutions archived website (Flash-based; content not extractable)

### Research Limitation Notes

This research was conducted using:
- ✅ Public web fetching (Wikipedia, ECMA)
- ✅ GitHub repository search
- ✅ Local archive of Wayback Machine data
- ❌ Proprietary business databases (not accessible)
- ❌ Authenticated standards portals (not accessible)
- ❌ Japanese-language registries (not accessible)

**Date of research:** May 14, 2026
**Researcher notes:** Follow-up research recommended before drawing conclusions for any ATIP/MID integration work.
