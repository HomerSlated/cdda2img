# Lead-in and Lead-out: What They Contain, What They're Forced to Contain, and Where the Spec Breaks

*Research notes for the cdda2img project.*

---

## The Spec-Conformant View

**Lead-in** occupies the innermost 4,500 sectors (60 seconds at 75 frames/second) of the disc, from the physical beginning of the programme area inwards. Red Book requires a minimum of 4,500 sectors of lead-in. Its contents:

- **Q-channel subcode in the lead-in** carries the Table of Contents. Each Q frame in the lead-in is in "Mode 1" format, encoding one of three things: the starting MSF address of a programme track, the starting address of the lead-out, or the MCN if present. The player reads the TOC from the lead-in before doing anything else; this is why a player that is physically touched mid-lead-in fails to start — it hasn't finished reading the TOC yet.
- The **P channel** in the lead-in is always `0x00` — the "pause" flag is asserted continuously.
- The main channel (the 2,352-byte audio payload) in the lead-in is **all zeros** on a spec-conformant disc. There is no programme audio in the lead-in; it is silence.
- **R–W subcode** in the lead-in carries **CD TEXT** if present (Mode 2/4 per IEC 60908 §26). This is a specific exception to the usual R–W usage pattern.

**Lead-out** occupies the outermost portion of the disc, from the last programme track onward to the physical edge. Red Book requires a minimum of 6,750 sectors (90 seconds) of lead-out. Its contents:
- The main channel is **all zeros** — silent PCM audio frames.
- Q-channel Mode 1 in the lead-out reports the lead-out start address continuously.
- P channel is `0xFF` throughout — the "lead-out" flag.

Neither lead-in nor lead-out is accessible by normal consumer playback. A player reads the lead-in at startup, confirms the lead-out P-flag to know the disc has ended, and never exposes either region to the user.

---

## The Pre-Gap: The Ambiguous Zone

Between the end of the lead-in and the start of Track 1 is the **pre-gap of Track 1** — also called Index 0 of Track 1, or the "pause before Track 1." On a spec-conformant disc this is 2 seconds (150 sectors) of silence. The player uses it as a "landing zone" after the lead-in read.

The pre-gap is where things get interesting:

**Hidden Track One Audio (HTOA)** is real programme audio placed in the pre-gap of Track 1 — before the nominal track 1 start at absolute disc time 00:02:00 (150 sectors from the lead-in boundary). The TOC does not list this content; no track number is assigned to it. Access requires the user (or software) to seek backward past the track 1 index point into negative relative time. The spec technically permits silent data in the pre-gap; it does not sanction programme audio there, but it doesn't explicitly forbid it either. The HTOA trick is a deliberate exploit of the gap.

---

## Where the Spec Breaks: Write Offsets

A **disc write offset** is a manufacturing imprecision: the actual physical location of sample zero on the disc differs from the nominal location by some number of samples. This is a property of the CD mastering and pressing process, not a deliberate choice.

The Red Book doesn't define write offsets at all — it assumes perfect placement. In practice, every CD manufacturing plant introduces a small systematic offset. Offsets of ±500 samples are common; offsets of ±3,000 samples (±68 ms) are known.

The consequence:
- With a **positive write offset** (+N samples), the first N samples of Track 1 are written *before* the nominal Track 1 start — i.e., into the pre-gap, or in extreme cases into the last sectors of the lead-in. A drive that cannot read into the lead-in will miss those samples.
- With a **negative write offset** (−N samples), the last N samples of the last track are written *after* the nominal lead-out start — i.e., into the first sectors of the lead-out. A drive that stops reading at the lead-out boundary will clip the end of the disc.

This is why Redump requires drives capable of reading up to 150 pre-gap sectors (the full nominal pre-gap) and more than 75 lead-out sectors — to handle discs where the write offset has pushed real audio data into those regions. For the majority of discs the 75-sector minimum is sufficient; the edge cases are uncommon but real.

**Drive offset** is a distinct concept: the systematic error introduced by a specific drive model's read head position. AccurateRip's drive offset database lists these per model (e.g., "LG GH22NS90: +667 samples"). The net correction applied to a rip is `disc_write_offset − drive_read_offset`. Getting this wrong by even a single sample produces a checksummed-but-incorrect rip that AccurateRip will flag.

---

## Where the Spec Breaks: Copy Protection in the Lead-in

The most aggressive copy protection schemes targeted the lead-in specifically, because the TOC is the first thing a computer reads and because the spec's tolerance for lead-in format variations gave attackers a surface to exploit.

**Key2Audio** (Sony DADC) wrote a second, corrupted TOC into what should be the silent main-channel data of the lead-in. CD players — which are hardware state machines that read Q-channel subcode, not the main channel data, for the TOC — ignored it completely. Computer drives, which often do additional scanning of main-channel sectors during startup to cross-check the TOC, sometimes locked up or failed to mount the disc. The attack exploited an implementation detail (the computer drive's defensive TOC cross-check) rather than the spec itself.

**Fake second session** protection worked by using the multisession format: the lead-in of the first session was valid; a second session lead-in (visible to computer drives, which default to the last session) was invalid or pointed to a corrupted data track. Standard CD audio players only read the first session; they were unaffected. This is an example of the multisession mechanism (specified in the Orange Book for CD-R but also valid on pressed discs) being repurposed as a weapon.

**SafeDisc** (data discs) embedded "weak sectors" — sectors whose signal quality was deliberately degraded at mastering time so that the drive's CIRC corrector could not reliably decode them. CIRC is expected to have C2 errors on these sectors. The copy protection software running on the computer would interrogate the drive for C2 error patterns on those sectors and refuse to run if the pattern was absent (i.e., if the disc was a copy, since standard burners cannot reproduce the deliberate weakness). From a disc physics standpoint, these sectors break the Red Book's implicit requirement that all programme sectors be recoverable.

---

## The Lead-Out and Pre-mastering: An Edge Case Worth Knowing

When a disc is replicated, the lead-out data (silence frames in the main channel) is written by the pressing plant from the submitted DDP (Disc Description Protocol) master. The pressing plant appends the lead-out. What goes into the lead-out main channel is genuinely irrelevant to the standard — it's supposed to be silence, it's not supposed to be read.

However, a small number of discs (notably some early CD-Rs mastered by non-professional tools) have been found with non-zero data in the lead-out main channel — artefacts of the authoring software that wrote silence incorrectly, or cases where the last audio frame was not properly padded. These are not copy protection; they are mastering errors. A ripper that reads into the lead-out will return these bytes, and a tool that naively compares rips will see a mismatch. This is one reason the Redump community stores checksums of the programme area only, not the lead-out content.

---

## Summary

The core tension in CDDA archival is that the spec assumes a benign, well-behaved mastering process, while the real world contains write offsets, copy protection attacks, mastering errors, and drive-specific implementation variations. A rigorous archival tool must model all of these — or at minimum, record enough raw data (lead-in depth, lead-out depth, subchannel, C2 flags) that later analysis can reconstruct what was actually on the physical disc.

---

## References

- IEC 60908:1999 — *Audio recording — Compact disc digital audio system*. Clauses 5–9 (disc geometry and lead-in/lead-out layout), Clause 26 (CD TEXT in R–W subcode).
- [Optical Disc Drives: CD Compatibility Technical Details — Redump Wiki](http://wiki.redump.org/index.php?title=Optical_Disc_Drives:_CD_Compatibility_Technical_Details)
- [Redumper — Redump Wiki](http://wiki.redump.org/index.php?title=Redumper)
- [superg/redumper — GitHub](https://github.com/superg/redumper)
- `private/ABHOOD.md` §5.4 — CD Drive Technical Requirements for Accurate Dumping (summarises Redump drive criteria)
- `private/spoons-audio-guide-cd-ripping.txt` — practical notes on drive offset correction and secure ripping
