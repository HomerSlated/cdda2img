# The "Orange Forum Embargo"

Research notes on the phrase coined by Jörg Schilling in the `cdrtools` source code.

## TL;DR

- **"Orange Forum"** is not the formal name of any standards body. It is Schilling's own
  sardonic term for the Philips apparatus (specifically **Philips System Standards &
  Licensing** / Philips IP&S) that administers the **Orange Book** (the Philips–Sony
  Recordable CD standard) and controls registration and publication of the Media
  Identification Code (MID) that encodes the disc manufacturer in the ATIP pregroove.
- **"Embargo"** refers to the fact that (a) the Orange Book specifications are only
  distributed under a paid CD Information Agreement or CD-R/RW license, and (b) the
  authoritative list of assigned MID codes is likewise not freely available. An open-
  source tool cannot legally obtain a current MID registry, so its manufacturer table
  is necessarily incomplete and ages out as new codes are assigned.
- The phrase survives almost entirely inside `cdrecord/diskid.c` and is surfaced to
  users via the `cdrecord -atip` output when the MID cannot be resolved (or is resolved
  only speculatively).

## The exact Schilling strings

From `cdrecord/diskid.c` (verified in the Distrotech mirror of cdrtools):

```
Manufacturer is guessed because of the orange forum embargo.
The orange forum likes to get money for recent information.
The information for this media may not be correct.
```

```
Manufacturer is unknown because of the orange forum embargo.
As the orange forum likes to get money for recent information,
it may be that this media does not use illegal manufacturer coding.
```

These are the only places the phrase appears in the codebase. There is no README,
man-page, or comment explaining the term further; Schilling evidently expected the
context to be self-explanatory to anyone familiar with the licensing history of the
Orange Book.

## 1. What is (was) the "Orange Forum"?

There is no organisation with this formal name. The term is Schilling's own
nickname — almost certainly a sarcastic echo of the **DVD Forum** (a real industry
consortium founded 1995/1997 by Hitachi, Matsushita, Mitsubishi, Pioneer, Philips,
Sony, Thomson, Time Warner, Toshiba, and JVC). The DVD Forum operates as an open
consortium that maintains the DVD specifications; in contrast, the CD specifications
("Rainbow Books", including the Orange Book for CD-R/RW) were never transferred to
an open industry body. They remained proprietary documents published and controlled
jointly by **Philips and Sony**, with the licensing apparatus operated by Philips.

The practical referent of "Orange Forum" in Schilling's text is:

- **Philips IP&S / Philips System Standards & Licensing** — the Philips division
  that sells the Orange Book specifications (and the rest of the Rainbow Books) via
  `licensing.philips.com`, administers the **CD Information Agreement**, and
  registers the 12-bit Manufacturer ID portion of the MID code.
- Secondarily, the **CD-R/CD-RW joint-licensing pool** originally comprising Philips,
  Sony, and Taiyo Yuden (Ricoh was added later), which jointly licenses the
  Orange Book patents under a royalty of 3 % of net sales price / ¥10 minimum
  per disc (as detailed in Princo v. Philips and Liu's analysis of the Taiwanese
  CD-R cartel cases).

In short: Schilling is lampooning the fact that what *ought* to be a neutral standards
forum is in practice a cartel licensing body.

## 2. Who were the "members"?

There is no membership roster because the "forum" itself is an informal coinage, but
the relevant parties in the licensing apparatus Schilling is describing are:

- **Philips** — author of the Orange Book; operator of the licensing programme;
  assigner of MID manufacturer codes.
- **Sony** — co-author of the Orange Book and co-licensor of the patent pool.
- **Taiyo Yuden** — joined the CD-R joint licensing agreement in 1992.
- **Ricoh** — later added to the pool.
- **CD-R/RW manufacturers** (Ritek, CMC Magnetics, Mitsubishi Chemical / Verbatim,
  Prodisc, Moser Baer, etc.) — licensees who execute a CD Information Agreement
  or CD-R/CD-RW license to obtain specifications and an MID block assignment. The
  1992 Joint Licensing Agreement (JLA) set the royalty at 3 % of net sales / ¥10
  minimum per disc.

## 3. What is the "embargo"?

Two interlocking restrictions:

1. **Specifications paywall.** The Orange Book itself is only distributed to
   parties who have signed either a **CD Information Agreement** or a relevant
   CD-R/CD-RW licence agreement with Philips. It is not a publicly available
   standard (unlike, for example, ECMA-130 for CD-ROM or the later ECMA-394).
   The same applies to the other Rainbow Books.

2. **MID registry paywall.** The 16-bit Media Identification Code encoded in the
   ATIP pregroove comprises a 12-bit manufacturer code (registered with Philips)
   plus a 4-bit product/dye variant. The authoritative, current list of assigned
   12-bit manufacturer codes is held by Philips and is not published openly; it
   is made available to licensees. Independent open-source tools therefore rely
   on codes harvested piecemeal from disc samples, leaked documents, and
   reverse-engineered datasheets, and their tables inevitably lag the
   authoritative registry and contain errors.

Consequences Schilling calls out in the `diskid.c` strings:

- When the MID reads a code not in cdrecord's baked-in table, the tool emits
  *"Manufacturer is unknown"* and hedges that the code might simply be one
  cdrecord does not know about, or that a disc manufacturer might be using a code
  without legitimate registration (hence *"illegal manufacturer coding"* — a
  real practice among grey-market pressing plants).
- When cdrecord's table has an entry but Schilling's confidence in it is low,
  the tool emits *"Manufacturer is guessed"* and warns that the data may be
  wrong.
- The underlying cause in both cases is the paywall: Schilling cannot legally
  obtain the current registry, so he maintains his table by hand and apologises
  for its limitations in-band.

## 4. Other reliable sources

The phrase itself is near-unique to Schilling. Discussion of the underlying
licensing regime is widely documented:

### Primary — Schilling's own text
- **`cdrecord/diskid.c`** (cdrtools source, Distrotech mirror on GitHub): the
  string literals quoted above.
  <https://github.com/Distrotech/cdrtools/blob/master/cdrecord/diskid.c>
- **cdrecord output in bug trackers** that captures the message verbatim, e.g.
  Ubuntu Launchpad bug 66710 and bug 251712.

### Standards and licensing
- **Rainbow Books, Wikipedia** — overview of the colour-book family and the
  fact that Orange Book access is paywalled for manufacturers.
- **Media Identification Code, Wikipedia / Grokipedia** — structure of the MID
  (12-bit manufacturer + 4-bit variant) and its origin in the Orange Book.
- **Philips IP&S licensing portal** (`licensing.philips.com` → `ip.philips.com`) —
  the current landing page for the CD Information Agreement and CD-R/CD-RW
  patent licences.

### Legal / economic analysis of the licensing regime
- **Princo Corp. v. U.S. Philips Corp.**, 424 F.3d 1179 (Fed. Cir. 2005); later
  en banc 616 F.3d 1318 (Fed. Cir. 2010). Describes the Orange Book patent pool
  and the "essential" vs "non-essential" patent packaging.
- **Kung-Chung Liu, "The Taiwanese 'Philips' CD-R Cases: Abuses of a
  Monopolistic Position, Cartel and Compulsory Patent Licensing"** (SSRN
  1831275). Details the JLA, the 3 % royalty / ¥10 floor, and the antitrust
  findings against the pool in Taiwan.

### Community discussion
- **HardwareBanter thread "What is the 'orange forum embargo'?"** — a direct
  user-facing discussion of the phrase (currently intermittently unavailable;
  indexed by Google and retrievable from the Wayback Machine).
- **Cdrtools-support mailing list** archive on mail-archive.com — the message
  quoted in search results attributes the explanation directly to Schilling.
- **Forensic Focus CD-R Manufacturer Code thread** — practitioner-level
  discussion of MID identification and its unreliability, referencing the
  same paywall issue.

### Related open-source attempts to work around the embargo
- **CDR Identifier** (Glueckert, Wolf, Machelett & Partner) and **CDR Media Code
  Identifier** — Windows utilities that maintain independent MID tables with the
  same limitations as cdrecord's.
- **DVD Identifier** — comparable tool on the DVD side; its "Manufacturer
  Database" last updated 2008-10-12 with 872 entries, illustrating the slow
  decay of community-maintained tables after DVD recording peaked.

## Assessment of the original hypothesis

The user's guess was correct in substance:

> "Obtaining an ATIP [spec] was prohibitively expensive and subject to unreasonably
> strict conditions, and … small, independent, and in particular Open Source
> developers, such as Schilling, had little hope of ever being granted access to
> that data, and so he struggled to essentially reverse engineer it, guess at its
> validity, and was forced to deal with compatibility issues as a result."

Two refinements:

- It is not the ATIP *format* that was unavailable (the ATIP bitstream layout
  has been widely documented in practice, e.g. MMC command sets, and in
  derivative texts). What is paywalled is (a) the Orange Book spec itself and
  (b) the authoritative **MID registry** — the mapping from 12-bit manufacturer
  code to named company.
- The "forum" is not a literal body Schilling was excluded from; it is his
  rhetorical device for the Philips-Sony licensing apparatus, likely modelled
  on the name of the (genuinely) open DVD Forum.

## Relevance to this project

For `cdda2img`:

- If we ever add ATIP/MID parsing (relevant to the **Physical Media** roadmap
  item and the **Foreign Image Format Support** item for CCD/IMG/SUB, which
  carries subchannel data), we will inherit the same embargo problem. Our
  manufacturer table will be incomplete by construction.
- The pragmatic workaround the OSS world has settled on is to ship a best-effort
  table, cite sources per entry where possible, and surface uncertainty to the
  user in the same spirit as Schilling's messages. We should not represent
  guessed identifications as authoritative.
- For accuracy verification (the **C2 / AccurateRip** roadmap item), the
  AccurateRip protocol is documented well enough externally that the embargo
  does not affect that work.
