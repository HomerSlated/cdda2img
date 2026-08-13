# The rip progress map — legend

The `rip` progress bar is not a bar with a map beside it: **it is the map.** Each
cell stands for a fixed span of sectors and reports what the read found there.
The span is `total_sectors ÷ map_width`, so it depends on both the disc and the
terminal: about **6,000–7,700** sectors per cell on a full 80-minute disc, or
**2,800–3,500** on a 36-minute one like *Tracy Chapman*, at 47–59 map columns.

That matters for reading it: a cell is a *summary of thousands of sectors*, not a
sector. A single bad sector and a thousand bad sectors can both light a cell —
which is what the severity bands below exist to separate.

Two independent lanes share one text row, drawn with `▀` (U+2580 UPPER HALF
BLOCK), whose top half takes the foreground colour and bottom half the
background:

| half | lane | what it measures |
|---|---|---|
| **top** | **Q** | subchannel CRC — the disc's *structure*: pre-gaps, INDEX points, MCN/ISRC |
| **bottom** | **C2** | the drive's own error pointers — the *audio* |

They fail independently, which is the whole reason for two lanes. A disc can lose
its subchannel entirely while every audio checksum passes, and that rip will
satisfy AccurateRip and still have lost the disc's structure.

---

## Colours

Both lanes use the same palette. Colour says **how bad**; which half it is on
says **which lane**.

| swatch | xterm-256 | hex | meaning |
|---|---|---|---|
| <img src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI0NCIgaGVpZ2h0PSIxOCI+PHJlY3Qgd2lkdGg9IjQ0IiBoZWlnaHQ9IjE4IiBmaWxsPSIjMzAzMDMwIiBzdHJva2U9IiM4ODgiIHN0cm9rZS13aWR0aD0iMSIvPjwvc3ZnPg==" alt="dark grey" /> | 236 | `#303030` | **not read yet** — this is *not* "clean" |
| <img src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI0NCIgaGVpZ2h0PSIxOCI+PHJlY3Qgd2lkdGg9IjQ0IiBoZWlnaHQ9IjE4IiBmaWxsPSIjMDA4N0ZGIiBzdHJva2U9IiM4ODgiIHN0cm9rZS13aWR0aD0iMSIvPjwvc3ZnPg==" alt="blue" /> | 33 | `#0087FF` | **intact** — nothing wrong found in this lane |
| <img src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI0NCIgaGVpZ2h0PSIxOCI+PHJlY3Qgd2lkdGg9IjQ0IiBoZWlnaHQ9IjE4IiBmaWxsPSIjODc1RjAwIiBzdHJva2U9IiM4ODgiIHN0cm9rZS13aWR0aD0iMSIvPjwvc3ZnPg==" alt="dark brown" /> | 94 | `#875F00` | damage, band 0 — faintest |
| <img src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI0NCIgaGVpZ2h0PSIxOCI+PHJlY3Qgd2lkdGg9IjQ0IiBoZWlnaHQ9IjE4IiBmaWxsPSIjQUY4NzAwIiBzdHJva2U9IiM4ODgiIHN0cm9rZS13aWR0aD0iMSIvPjwvc3ZnPg==" alt="brown" /> | 136 | `#AF8700` | damage, band 1 |
| <img src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI0NCIgaGVpZ2h0PSIxOCI+PHJlY3Qgd2lkdGg9IjQ0IiBoZWlnaHQ9IjE4IiBmaWxsPSIjRDc4NzAwIiBzdHJva2U9IiM4ODgiIHN0cm9rZS13aWR0aD0iMSIvPjwvc3ZnPg==" alt="orange" /> | 172 | `#D78700` | damage, band 2 |
| <img src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI0NCIgaGVpZ2h0PSIxOCI+PHJlY3Qgd2lkdGg9IjQ0IiBoZWlnaHQ9IjE4IiBmaWxsPSIjRkZBRjAwIiBzdHJva2U9IiM4ODgiIHN0cm9rZS13aWR0aD0iMSIvPjwvc3ZnPg==" alt="bright orange" /> | 214 | `#FFAF00` | damage, band 3 — worst |
| <img src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI0NCIgaGVpZ2h0PSIxOCI+PHJlY3Qgd2lkdGg9IjQ0IiBoZWlnaHQ9IjE4IiBmaWxsPSIjRkZGRjAwIiBzdHJva2U9IiM4ODgiIHN0cm9rZS13aWR0aD0iMSIvPjwvc3ZnPg==" alt="yellow" /> | 226 | `#FFFF00` | **being re-read** — the recovery ladder is working on this region |

Blue/orange rather than green/red: red–green dichromacy affects roughly 8% of
men, and this pair also stays distinct in greyscale because the two hues differ
in luminance as well as in hue. The four damage shades stay within **one hue** on
purpose — the ramp encodes degree, and a ramp that drifted across hues would read
as a change of *kind*.

**Dark grey is the one to read carefully.** An unread cell and a clean cell are
both "no damage found so far", and the whole point of giving unread its own
colour is that they must never look alike. Grey to the right of the frontier is
simply where the read has not reached.

---

## What each band means — and the two lanes are calibrated differently

A cell's band comes from the *fraction* of its sectors flagged in that lane. The
bands are decade-wide because error density spans four orders of magnitude; a
linear ramp would show "nothing" and "everything" with nothing in between.

**The two lanes cannot share one table**, because their healthy baselines are not
the same number.

### C2 (bottom half) — healthy is exactly zero

A clean disc flags no sectors at all, so *any* non-zero density is worth showing.

| band | flagged sectors in the cell | colour |
|---|---|---|
| 0 | > 0 and < 0.1% | `#875F00` |
| 1 | 0.1% – 1% | `#AF8700` |
| 2 | 1% – 10% | `#D78700` |
| 3 | ≥ 10% | `#FFAF00` |

### Q (top half) — healthy is a few per cent

CRC-bad Q frames are **ordinary**. Measured across two discs at five speeds each
(`RECOVERY.md` §12.2.2, §12.3, plus the 42-capture `qlag` sweep):

| disc | Q-bad rate | verdict |
|---|---|---|
| ZZ Top | 0.081% – 0.204% | healthy, every speed |
| Tracy Chapman | ~2% | healthy, flat at every speed |
| ABBA *Gold* @ 4× | 3.5% | healthy for that pressing |
| *(three measured collapses)* | 38.7%, 47.8%, 52% | the failure this lane exists for |

So the bands sit in the order-of-magnitude gap between them:

| band | CRC-bad frames in the cell | colour |
|---|---|---|
| 0 | < 5% | `#875F00` |
| 1 | 5% – 15% | `#AF8700` |
| 2 | 15% – 35% | `#D78700` |
| 3 | ≥ 35% | `#FFAF00` |

> **The 5% floor is a claim about discs, not about any one drive.** A pressing
> whose ordinary Q rate exceeded it would be drawn as damaged. None of the three
> measured does — ABBA's 3.5% is the worst — but this floor was set from three
> discs and should *move* if a fourth contradicts it, rather than be defended.
>
> This calibration was got wrong once, in the obvious way: the Q lane briefly used
> C2's table, which put Tracy's perfectly healthy 2% into band 2 and painted every
> cell orange on a clean read. Worse, 2% and 20% saturated the same band, so the
> map could not have told them apart even if asked.

---

## Reading it

| what you see | what it means |
|---|---|
| solid blue, both halves | clean read |
| blue top, orange bottom | **audio** damage; subchannel fine |
| orange top, blue bottom | **subchannel** damage; audio fine — pre-gaps and INDEX points at risk while every audio gate still passes |
| orange both halves | nothing in this span survived intact |
| dark grey | not read yet |

A **uniform faint brown** top lane across the whole disc is normal — that is band
0, the ordinary interleave of MCN/ISRC frames and occasional CRC misses. A top
lane that jumps to bright orange over a long stretch is a real subchannel
collapse, and on this hardware it correlates with read speed.

---

## Without colour

Under `NO_COLOR`, or when stdout is not a terminal (piping the output into a
log), the map degrades to **shape**, so it still reads in a text file:

| glyph | meaning |
|---|---|
| `█` | both lanes intact |
| `▀` | Q intact, C2 damaged — only the top half survives |
| `▄` | C2 intact, Q damaged |
| `▒` | neither lane intact |
| `░` | not read yet |
| `▓` | **being re-read** — the recovery ladder is working here |

Shape carries *where* and *which lane*, but never *how much*: severity lives in
colour alone, so the four bands collapse into one glyph. `░` is deliberately not
blank — "no damage found" must never look like "not looked at".

`▓` covers **both** lanes at once: it is not a lane verdict but a statement that
this region is under active repair, so it replaces whatever the lanes said until
the attempt finishes. The three shades order `░` < `▒` < `▓` < `█` — unread,
damaged, being-worked-on, intact — and none of them is blank. Its colour is a
third hue rather than a rung of the damage ramp, because it is a change of *kind*
and not of degree: not a worse error, work in progress.

When the engine cannot supply a Q verdict at all (a binding older than AccuDisc
0.5.0), the map draws **C2 alone** — `█` / `▒` / `░` — rather than drawing Q as
healthy. Q health cannot be computed on this side: a hard-unreadable sector
arrives zero-filled and a zero Q frame *fails* CRC, so a locally-derived lane
would paint fabricated subchannel damage onto exactly the sectors whose audio is
already gone, sitting beside the real failure and reading as corroboration.

---

## The rest of the line

```
⠸  Ripping track 11…    ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀  95.1%   (154905/162892)
│  │                    │                              │       │
│  │                    │                              │       └─ sectors read / total
│  │                    │                              └───────── percentage
│  │                    └──────────────────────────────────────── the map
│  └───────────────────────────────────────────────────────────── phase
└──────────────────────────────────────────────────────────────── spinner
```

The phase reads `Ripping disc at Nx…` until the TOC has been read — at that point
the drive is genuinely spinning up and seeking the lead-in, and no sector of
track 1 has been delivered, so naming a track would be a guess dressed as a
measurement. The speed clause is omitted entirely when the drive does not report
one, rather than printed as `0x`.

**Every width on this line is pinned for the life of the read.** A cell's sector
span is derived from the column count, so a single column of drift re-buckets
every cell and already-drawn damage appears to move. If the terminal is narrowed
mid-rip the map **clips cells off the right** rather than re-bucketing — clipping
loses the least, since the content is left-weighted and the frontier is also
reported numerically.

---

*Source: `src/cdda2img/disc_map.py`. Every value in this document is generated
from that module; if the two disagree, the module is right.*
