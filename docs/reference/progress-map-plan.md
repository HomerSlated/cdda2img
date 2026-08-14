# The Q + C2 progress map — design and wiring plan

**Status:** planning, 2026-08-07. No production code written yet. The bench is
`tools/progress_lab.py`; the TODO item is N2.

The rip progress bar is to *become* the Q and C2 map — one widget, not a bar with
a map bolted on. This plan covers where it draws from, where it is wired, what
AccuDisc has to supply, and what degrades when colour is unavailable.

Settled by kgr 2026-08-07 after comparing the bench renderings: **`glyph` style,
`cb` palette, `ramp` aggregation**, no track-boundary ruler.

---

## 0. The headline finding — almost nothing needs to be built

The original framing was "the map needs Q and C2 during the read, and the read
callback carries only `(done, total)`". That is true of the *callback* and false
of the *library*. Two things already exist:

**`Device.read(..., status_map=True)` allocates a `count`-byte per-sector map and
documents live reading from another thread as "the intended progress-tracking
pattern"** (binding `__init__.py:1196-1229`, `1841`). Its states are
`PENDING / OK / C2 / HARD / RECOVERED / SUSPECT`, plus a severity nibble. That
covers, with no change on either side:

| what the map needs | where it already comes from |
|---|---|
| the read frontier | `MapState.PENDING` — exactly our `UNREAD` |
| the C2 lane | `MapState.C2` / `HARD` / `RECOVERED` / `SUSPECT` |
| per-sector C2 severity | the severity nibble (~log2 of fired C2 bits) |

### RESOLVED 2026-08-08 — AccuDisc shipped caller-owned buffers; we adopted them

The correction below stood for about six hours. AccuDisc confirmed the defect,
fixed the false docstring as its own change, and shipped `status_map=` /
`subq_map=` accepting **a writable buffer of exactly `count` bytes** alongside
the existing `True`/`False`. We pass our own `bytearray` and project it onto the
damage lane in the sink (`accudisc_reader._prepare_damage_lane` /
`_update_damage`); `_census_c2` survives as the pre-0.5.0 fallback, which is a
live path because our binding resolves through a symlink into their build tree.

Three things from that exchange worth keeping:

- **They measured the thing that decides it, before designing.** Does cffi release
  the GIL during `accudisc_read_cdda`? A spinner thread advanced 114,777 ticks
  across a 6.08 s read with `sink=None`. It does. Neither side had checked, and
  both designs were unusable if it did not.
- **Feature-detect, never version-check.** The C library is untouched, so
  `library_version()` stays `(0, 5, 0)` across the change. A version guard would
  be permanently false while looking exactly right.
- **Their buffer beats our callback, and the argument is not performance.** A
  callback's contract is a *timing* property, and timing properties fail
  silently: move the call after the read and it still fires once, with a
  correctly-sized buffer of plausible bytes, and every test still passes. We had
  already written an all-zero assertion to detect exactly that — a guard invented
  because the design permits a silent failure. Passing the buffer in has no
  handover to order, so it needs no guard.

### CORRECTION 2026-08-08 — "no change on either side" is false for the LIVE case

Found while wiring, and it is the load-bearing detail the table above misses:
**through the Python binding, the status map is unreachable until the read has
finished.** `Device.read` allocates the buffer itself
(`map_buf = ffi.new("uint8_t[]", count)`, `__init__.py:1874`) and surfaces it
only via `ReadResult.status_map` — and `ReadResult` is constructed *after*
`accudisc_read_cdda` returns. So the docstring's "read it live from another
thread through `ReadResult.status_map`" describes something no caller can do: the
object holding the map does not exist while there is anything live to read.

The C API has no such problem — `accudisc_read_req.status_map` is a
**caller-supplied** `uint8_t *` (`accudisc.h:1444`), which is exactly the shape
that works. This is a *binding* gap, not an engine one, and the same gap will
apply to `subq_map` the moment it lands. See §3's amended ask.

The table is still right about the *post-read* map, which is what §5's static
map wants. It is only the live claim that was wrong, and it was wrong because it
was read off the docstring rather than off the allocation.

**`status_map` composes with everything we already pass.** Verified by reading
`Device.read`'s body (`1849-1880`): `c2`, `sub`, `sink`, `copy` and `status_map`
are independent fields on one `accudisc_read_req`. Our seam
(`accudisc_reader.py:835-839`) simply does not pass `status_map`. That is a
one-argument change, not a negotiation.

**`Device.read_span(**kwargs)` forwards to `read`**, so `c2=`, `sub=` and
`status_map=` already reach it (`1906-1928`). The earlier plan item "request that
`read_span` return C2" was **withdrawn before it was sent** — it asks for
something that is already there. What our `_read_span_binding`
(`accudisc_reader.py:927-990`) passes is PCM-only, by our own choice.

### What is genuinely missing: the Q lane

The status map has **no Q state**. Every state it carries is audio-side. But
`ReadStats.subq_total` / `subq_ok` (binding `964-974`) prove AccuDisc *already
runs the per-sector subchannel CRC* — they keep the count and discard the
position. That is the one ask, and it is small. See §3.

---

## 1. Where the data comes from (per lane)

| lane | source | status |
|---|---|---|
| C2 | `ReadResult.status_map`, polled live | available today |
| frontier | same map, `PENDING` | available today |
| Q | **needs `subq_map`** (§3), or computed here from raw P-W | see §3 |

**Polling, not pushing.** The map is read from `TerminalUI`'s existing 10 fps
render thread rather than from the read sink. This is better than widening the
sink callback for three reasons: the sink runs on the read hot path and must not
grow work; `chunk.data` is a `memoryview` over library memory that raises
`RetainedBufferError` if it escapes the call, so anything the sink computes must
be computed *there*, synchronously; and the render thread already runs at exactly
the rate the display needs. The binding explicitly blesses the cross-thread read
(`__init__.py:1229`).

**Aggregation stays ours.** `map_severity` is documented as **not comparable
across states** — the unit differs per state (log2 C2 bits, log2 disagreeing
bytes, raw reread count). Our `ramp` bands are a *different quantity*: the
fraction of a cell's sectors in an error state, banded by decade. That is
computed from states alone and is safe to compare. We are **not** asking AccuDisc
to make severity comparable; if we ever use it, it must be within one state.

---

## 2. Wiring targets — one now, one later, one never

Surveyed all 36 `set_status` call sites in `cdda2img.py` and `disc_writer.py`.

**`_rip_disc_stage` (`cdda2img.py:2544`) — the target.** The single-pass
`read_disc_c2`. The only site in the tree where both lanes exist, because it is
the only read that captures C2 *and* raw subchannel.

**`_recover_failed_tracks` (`cdda2img.py:2686`) — later, and it needs no AccuDisc
change.** Re-reads a track window across passes x speeds via `read_span`, which
we call PCM-only. Passing `c2=`/`sub=`/`status_map=` gets both lanes. The design
question is display, not data: the right rendering is the **whole-disc map kept on
screen with only the failed track's region live**, so the user sees a repair in
context rather than a bar that restarts per attempt. Needs a fourth cell state,
`REREADING`, and a decision about what a *later* attempt does to a cell an earlier
one already flagged (proposal: last-write-wins, since the committed audio is
whichever attempt matched).

**CTDB repair (`cdda2img.py:3023`) — never as a progress bar; see §5.**

**Everything else has no sector frontier**: AR verify/re-verify (network + CRC),
import/convert, metadata lookups, album art, loudness, container build. Track-1
preview is `read_span` for playback — cosmetic, best-effort, no C2. The burn path
has a frontier but nothing to read back.

### Pre-existing defect found while surveying

The TUI is constructed under `sys.stdin.isatty()` (`cdda2img.py:991`, `2951`) —
**stdin**, not stdout. So `cdda2img rip … > log.txt` from a terminal draws cursor
motion into the log. Harmless-ish today; the map makes it worse by adding colour.
Fix when wiring: gate on stdout, or gate the TUI on stdin and the *escape output*
on stdout.

---

## 3. The one ask of AccuDisc — a parallel `subq_map`

Proposed shape: a second `count`-byte array, same allocation, lifetime and
live-read semantics as `status_map`, requested by `subq_map=True`.

**Why parallel and not folded into the existing map.** A sector can be both
C2-flagged and Q-bad — those are independent failures with independent remedies —
and one enum nibble cannot hold two states. Widening the byte is worse: the
status map's layout is a documented contract, byte-identical to the CLI's
`--map-file` so that one decoder serves both (`__init__.py:1204-1207`). A
parallel array costs one more byte per sector, reuses the decode pattern, and
leaves that contract untouched.

**Open questions for them** (in §148 outbound):
1. Is the `subq_ok` check per-sector, or per some other unit?
2. What states are meaningful — is `absent` (no subchannel requested) distinct
   from `CRC failed`, and is there a `PENDING` equivalent?
3. **Does Q lag the audio?** `accudisc_probe_c2_lag` exists for C2. If the Q for
   sector N arrives alongside sector N+k, a map indexed by LBA is misaligned by
   k, and every cell boundary is wrong by that much.

### ANSWERED — AccuDisc §2026-08-07a, replied in §149

**1. Per-sector, exactly once.** One classifier iteration per LBA, never
revisited; every retry mechanism mutates the winning copy *inside* the chunk. An
LBA-indexed map is safe. The Q that gets checked comes from the sector actually
delivered, after rescue and consensus — so the Q verdict and the audio in one map
cell come from the same transfer.

**2. "Absent" is four states, and there is a fifth we did not know to ask for.**
Proposed enum: `SUBQ_PENDING` / `OK` / `BAD` / `NO_POSITION` / `NO_AUDIO`.

`NO_POSITION` is CRC-good with ADR≠1 — a legitimately interleaved MCN or ISRC
frame. Measured **1,590 of 162,892 = 0.98%**. That is not a footnote at our
aggregation: one cell stands for ~7,000 sectors, so **68 non-position frames per
cell**, and treating them as errors would flag **100% of cells on a clean disc**.
The bench had already produced this failure from the other direction (`sparse`
under worst-wins), which is why the ramp exists — and we would still have walked
into it, not knowing the 0.98% was there to look for.

**Our rendering:** `NO_POSITION` counts as **healthy** in the map — the
subchannel is working. It stays a distinct state because `subq_toc` wants the
opposite reading: "no position available here" is signal when deriving pregaps
and INDEX points. Two consumers, opposite polarity, one state.

**3. Q lag: zero on this drive — and index by slot regardless.** Measured over a
whole-disc capture: 157,871 CRC-good position frames at delta **exactly 0**
(99.973%). But the structural half decides it, not the measurement:
`accudisc_q_parse` deliberately leaves the position fields zero on CRC failure,
so a **CRC-bad frame has no address of its own** and must be placed by transfer
slot — and those are precisely the frames the Q lane exists to draw. There is no
arrangement in which a self-locating frame lets us dodge the question. Plus 43
frames that are CRC-good and positionally wrong, in six runs at exact multiples
of 512 sectors (an observable; they attach no mechanism to it).

**The trap that decides where the work belongs — reproduced on our side.** Hard
sectors arrive zero-filled, and a zero frame *fails* CRC. Checked against our own
CRC-16/GSM rather than taken on trust: `stored=0x0000 computed=0xffff`. So a DIY
Q lane would paint fabricated subchannel damage on exactly the sectors whose
audio is already gone — sitting beside the real failure, reading as corroboration
rather than as a bug. The engine avoids it by `continue`ing before the check, and
the only way to know that is to read their engine. **This, not the cost, is the
argument for the ask.**

**Cost in the engine: already paid.** The CRC runs unconditionally for every
`SUB_RAW` read whether or not a map was requested; the only new work is one byte
store per sector through the same relaxed atomic `status_map` already uses. ABI
impact: none — `accudisc_read_req` carries its own `.size` and the field is
additive.

**Two design calls, both theirs, both agreed** (§149.4): refuse `subq_map`
without `SUB_RAW` (`ACCUDISC_ERR_INVAL`) rather than return a uniform map that a
renderer would draw as a lane; and the severity nibble stays zero, because Q
integrity is one CRC-16 and anything else would be a proxy.

**Superseded:** our DIY cost measurement (~15 µs/sector, ~5.4 s per disc) is no
longer the relevant number. It was an upper bound on a bad implementation, and
the correctness trap makes the cost argument moot either way.

**Accepted:** their ~40-line device-free lag instrument. It needs a saved raw-sub
capture and nothing else, and our disc shelf is larger than theirs — including at
least one disc where Q health collapses while audio stays clean, the arm most
likely to produce a nonzero lag if one exists anywhere.

### Q lag: CLOSED on our corpus 2026-08-07 — 42 captures, all NO LAG

AccuDisc delivered the instrument (`tools/qlag.c` in their tree, public header
only, builds against the installed library). We reproduced their reference
capture exactly, built our own shifted arm to confirm the tool can report
nonzero (`LAG +3`, with the minority delta tracking the origin `-2048 -> -2045`),
then swept `private/bench/runs/run{6,7,8}` — three discs, 4x/8x/24x/32x/40x,
three passes each. Full table: `private/bench/qlag-sweep-2026-08-07.txt`.

**Every capture NO LAG**, and the question that mattered is answered: a Q
collapse does *not* disturb slot alignment. Two independent events —

| event | CRC-good | in-slot |
|---|---|---|
| run6 32x (the vendor speed cliff), all 3 passes | 99.18% -> **47.79%** | **99.988%** |
| run8 8x_p2, one degraded pass among identical siblings | 98.03% -> **38.73%** | **99.978%** |

A slot-indexed Q lane stays correct through exactly the failure it exists to
draw. **Design decision: index by slot, unconditionally.**

**Two things the sweep found that neither side predicted.**

1. **The raw `non-position` column was misleading across speeds — AccuDisc fixed
   the tool rather than accept our excuse.** It halves from 1.01% to 0.49%
   between 24x and 32x, but it is a percentage of *all* frames and CRC-good
   halved underneath it. We called that "not a defect, the denominator is right
   for its stated purpose" and in the same breath admitted we nearly concluded
   something false; their §2026-08-07d ruled that a number correct for its purpose
   and misleading in the obvious cross-capture comparison **is** a defect, because
   nobody reads one capture in isolation. `qlag` now prints both denominators.

   The interleave rate is **a per-disc constant, not a global one** — measured
   from the regenerated sweep, run6 sits at 1.021–1.024% and run8 at
   0.994–1.000%. Within each disc it does not move when the yield collapses:
   run6's 32x collapse reads 1.022–1.023% and run8's degraded 8x pass 0.995%,
   both inside their own healthy bands. So the MCN/ISRC interleave is a property
   of the *pressing*, not of read quality.

   The two bands are **disjoint** — run6's floor 1.021% sits above run8's ceiling
   1.000%, a 0.021 pp gap — so each collapse lands inside its own disc's band and
   outside the other's entirely. With run7 at exactly 0.000%, that is three
   pressings with three separated rates. Suggestive, and deliberately load-bearing
   for nothing: one drive, three discs.

   *(An earlier revision carried 1.018–1.026%, normalised by hand from rounded
   percentages, and quoted run6's band as though it covered both discs —
   superseded, outbound §154.)*

   **The methodological lesson, which is the durable part.** One of those wrong
   figures was not a typo: `0.39 ÷ 38.73 = 1.007%` is exactly what we typed, i.e.
   NP/GOOD re-derived from the two *rounded* display values instead of read off
   the three-decimal column (AccuDisc §2026-08-07f, reproduced here). That is a
   **method**, so it reproduces — and the rounding error is amplified as the
   denominator shrinks, 2.6x at 38.73% CRC-good. On healthy captures the same
   derivation is right to three digits. **A method that is accurate everywhere
   except where it matters is worse than one that is wrong everywhere, because
   nothing prompts you to check it** — and here "where it matters" is precisely
   the collapse rows the design rests on.
2. **`NO_POSITION` is 0.00% on an entire disc.** Every run7 capture — all five
   speeds, fifteen passes — a pressing with neither MCN nor ISRCs. So the rate is
   **0% to ~1% by disc**, which sharpens the case for the fifth state rather than
   softening it: a state whose necessity varies by disc is *worse* than one always
   needed, because testing on the wrong disc proves it unnecessary. Validating the
   Q lane on run7's disc alone would have concluded the state was optional.

### SHIPPED — AccuDisc 0.5.0, 2026-08-08 (kgr ruled yes), and the one thing still blocking

`accudisc_read_req` gained `uint8_t *subq_map` appended last (struct 56 → 64,
additive, no soname bump — verified here: our fatal-on-ABI-mismatch read path
still runs). States are as agreed: `PENDING` / `OK` / `BAD` / `NO_POSITION` /
`NO_AUDIO`, severity nibble always 0, `ERR_INVAL` without `SUB_RAW`. Python:
`read(sub=Sub.RAW, subq_map=True)` → `ReadResult.subq_map`, plus `SubQState` /
`subq_state()` / `subq_state_counts()`.

**Do not decode a Q byte with the status-map decoder.** The numberings are
parallel and the vocabularies disjoint, so `map_state()` on a Q byte returns a
well-formed name for a state that never happened — `NO_AUDIO` reads back as
`RECOVERED`, `BAD` as `C2`. Nothing raises, and *`RECOVERED` is a reassuring word
to see on a sector that was never read*. They pinned both collisions in a test.

**Their §3 is the zero-fill trap a third time.** `accudisc_q_parse` fills `adr`
from the frame header byte whether or not the CRC verified — it must, there is
nowhere else to read it from — so a corrupt frame routinely presents ADR=2 or 3.
Classify on `adr` before `crc_ok` and damage is painted `NO_POSITION`, this
lane's *healthy* state, on exactly the frames the lane exists to draw. They
consult `crc_ok` first and `BAD` outranks `NO_POSITION`. Three instances now of
one shape: **a caller-side derivation that is exactly wrong on the frames the
feature exists for, and right everywhere else.**

**Still blocking the Q lane: the binding liveness gap (§0 correction).**
`subq_map` inherits it exactly — `subq_buf = ffi.new(...)` inside `read()`
(`__init__.py:1955-1958`), surfaced only on the returned `ReadResult`. Verified
in their source, not read off the message. Asked as §158: let both map
parameters take a caller-allocated buffer, or hand it out at allocation. The C
API already has that shape.

### The correspondence is a dataset, and it contains retracted rows

Found 2026-08-07 by AccuDisc, confirmed here, and it outlasts this thread.

Both sides follow a no-in-place-edit rule: a correction is a **new section**, so
nothing anyone may already have read is silently rewritten. That rule is right.
Its unpriced cost is that the retracted number stays in the old section, in the
same format, **indistinguishable to a parser**. Since both projects have now
agreed the correspondence is the shared artefact that public files lean on, it is
a dataset — and it currently carries retracted rows with nothing marking them.

Demonstrated rather than hypothesised: AccuDisc's first extraction of our §153
picked up **46 rows** — the 42 generated ones plus our own four-row hand-typed
excerpt, which §154 had already retracted — and produced a wrong band from the
retracted 1.007%. We had counted the same 46 ourselves and read it as arithmetic
agreeing with expectations; they read it as a hazard. Same number, better reading.

Proposed to them in §156 (forward-only, so it never edits an existing section):
every correcting section carries a fenced ```retracted block naming the section
and the exact values it withdraws, one per row. A parser collects those first and
subtracts before aggregating.

**Both sides adopted it 2026-08-07**, with one addition from AccuDisc that our
own §153 proved necessary: **retraction is only half the problem — duplication is
the other half.** Of the four hand-typed rows in §153, two were *wrong* (retracted
by §154) and two were **correct copies** of rows already in the generated table.
Retraction does not touch those, so a parser still over-counts. Measured here:

```
run6 unique   n=12  mean=1.02233  band 1.021-1.024
run6 +dupes   n=14  mean=1.02243  band 1.021-1.024   <- band IDENTICAL
```

The band is unchanged because **min/max are insensitive to duplication** — and
bands are the only aggregate either project quoted all evening, so the defect
could not have surfaced through anything we were computing. A count or a mean is
wrong and looks fine.

**Final convention: a row's identity is `(section-of-origin, run, capture)`,
de-duplicated before aggregation.** Retraction handles rows that are wrong;
de-duplication handles rows that are repeated. Excerpts and emphasis blocks stay
free — a rule against quoting your own data in prose is a rule that fights how
people write, and gets dropped.

*(superseded note: adopted only if both sides take it — a convention one side follows is worse
than none, because the dataset looks curated while half of it is not.)*

**STATUS: scope is kgr's call.** AccuDisc has put the new `accudisc_read_req`
field to him rather than implementing it quietly. Nothing waits on the answer
(§6 step 3). If it comes back "do it on your side", the five states plus the
`HARD`-before-CRC rule are a complete specification with no further round trip.

---

## 4. Colour degradation — done, 2026-08-07

Implemented in the bench (`colour_enabled` / `resolve_palette`); to be lifted
into the widget when it moves to `src/`.

`NO_COLOR` is an **application-side** convention — no terminal strips SGR for us,
so an application that does not check the variable simply ignores it. The rule is
**presence, not truthiness**: any non-empty value disables colour, so `NO_COLOR=0`
means *no colour*. Testing the value would invert the case a user is most likely
to try. A non-TTY stdout degrades too.

**What degradation costs, measured:**

| style | mono rendering |
|---|---|
| `glyph` | **lossless for lane identity** — `cb`+`glyph` with SGR stripped is byte-identical to `mono`+`glyph` |
| `dual` / `stacked` / `single` | **total loss** — each collapses to one repeated character (measured: 1 distinct glyph each) |

**In every style, `ramp` severity is lost under mono**: it is carried in colour
alone, because `glyph` already spends its shape on the lane split. A mono map
answers *where* and *which lane*, never *how much*. That is the price of the
`glyph`+`ramp` default and it is documented rather than hidden.

**`timg` cannot help.** Checked 2026-08-07: no monochrome or greyscale mode (zero
matches in its help), and no `NO_COLOR` string in the binary. It also renders
*images*, while the map is generated text — there is nothing to hand it. Its
half-block pixelation (`-p h`) is the same technique we use, which is
confirmation of the approach, not a dependency.

---

## 5. A static map after CTDB repair — sketch, not a promise

A *progress* bar is wrong here: CTDB repair does **zero extra reads** (it is
computation over PCM already in hand) and finishes in ~0.8 s on the Tracy
fixture. Animating it would be theatre.

A **result** map is genuinely informative, and it is a different map: the lanes
would be `undamaged / repaired / unrepairable`, drawn once, beneath the rip
summary — "here is what the parity fixed and what it could not".

**What blocks it:** `CtdbRepairResult` (`ctdb_repair.py:88-107`) carries
`damaged_tracks`, `corrections`, `erasure_columns`, `unverified_columns` — counts
and track numbers, **no per-column or per-sector positions**. So the finest map we
could draw today is per *track*, which is a table, not a map.

Two ways forward, in preference order:
1. **Derive positions here.** The repair writes back to the PCM file; a diff of
   the pre- and post-repair buffers gives exact repaired sector positions with no
   API change at all. Costs one buffer copy of the damaged region.
2. **Ask AccuDisc for the positions.** `accudisc_ctdb_repair` knows precisely
   which columns it touched, and AccuDisc's §2026-08-03a already discusses an
   `affected_sectors`-shaped quantity — but we explicitly told them in §146 that
   we were *not* asking for it. Reopening that needs a better reason than a
   display, especially as (1) gets the same answer locally.

**Do (1).** It needs nothing from anyone, and it measures the repair's *effect*
rather than trusting its self-report — which is the same reason the CRC and AR
gates exist.

---

## 6. Order of work

1. ~~Colour degradation in the bench~~ — done 2026-08-07.
2. **§148 to AccuDisc**: the `subq_map` ask and the three questions. Blocking for
   the Q lane and nothing else. *(kgr took this over 2026-08-08; see the amended
   ask under the §0 correction — the binding gap now rides with it.)*
3. ~~**Wire the C2 lane and the frontier into `_rip_disc_stage`**~~ — **done
   2026-08-08.** `src/cdda2img/disc_map.py` (bucketing, bands, palette, NO_COLOR),
   `TerminalUI.set_map` / `_build_map` (the bar *becomes* the map, all widths
   pinned), `accudisc_reader._census_c2` + `read_disc_c2(map_cb=…)`. Computed in
   the **sink**, not from `status_map`, for the reason in the §0 correction —
   and nothing is lost by it, because `RECOVERED`/`SUSPECT` are the only states
   the sink cannot see and this path leaves every reread knob at zero, so they
   are structurally unreachable. Raw C2 bits are finer than `MapState.C2` anyway.
4. ~~Q lane~~ — **done 2026-08-08**, on AccuDisc 0.5.0's `subq_map`. Never DIY:
   the zero-fill trap (§3) means a Q lane computed here paints fabricated damage
   on exactly the sectors whose audio is already gone. Decoded with
   `subq_state()`, never `map_state()`. `NO_POSITION` counts as **healthy**
   (~1% interleave, ~100 frames per cell — as error it flags 100% of cells on a
   clean disc); `NO_AUDIO` counts as **damage**, deliberately, because the lane
   answers "is the subchannel intact here" and where no frame was delivered the
   answer is no. A binding without the capability still draws one lane and says
   so, rather than drawing Q as healthy.

   Shipped with it: the **track number** on the status line, from the TOC
   `_read_disc_binding` already fetches — so it costs no extra command and no
   second spin-up — and a **"Ripping disc at Nx…"** spin-up phase that holds
   until the TOC has been read, because until then there is no track list and
   the drive really is seeking the lead-in. The speed clause is omitted rather
   than printed as `0x` when the drive did not report.

   The width trap came back and was caught in review before it shipped:
   `_build_map` pinned the status column from *whichever text was showing at the
   first frame*, which is fine for one constant string and wrong the moment the
   text cycles — every longer phase would then be truncated for the rest of the
   rip. `set_map(status_width=…)` now takes the widest text the read can ever
   show, computed rather than measured. The bench had this (`status_width()`);
   production did not.
5. ~~Recovery-ladder rendering (§2), which is our own work.~~ — **DONE
   2026-08-13**, on the damage-map retention that step 6 needed anyway.

   `disc_map.REREADING` is the fourth cell state, with **its own hue and its own
   glyph** rather than a rung of the error ramp: it is a change of kind, not of
   degree — not a worse error, work in progress. `cells_from_damage(active=…)`
   marks the cells a `[lo, hi)` sector range **intersects** (not contains: a
   track window can be narrower than one cell, and a repair the map declines to
   draw because it did not fill a bucket is the wrong way round).

   **The frontier was the real problem, and it is why this could not just be
   wired up.** `_build_map` derives `frontier = round(prog * len(damage))`, and
   during recovery `prog` measures progress through one *track*. Left alone it
   collapses the whole-disc map to a sliver and redraws it from the left on every
   attempt — precisely the "bar that restarts per attempt" §2 set out to replace.
   `set_map(active=…)` therefore forces `frontier = len(damage)`: an active
   repair region means the first pass is long finished.

   **§2's last-write-wins is adopted, with a stronger justification than "last".**
   `_RecoveryMap.clear` zeroes a track's damage only on an **AccurateRip match** —
   positive evidence the audio is now correct, whatever C2 said on the first
   pass. So the clear is a defensible claim rather than "assume the reread was
   clean", and a track that never matches keeps its damage, because the ladder
   exhausted every pass x speed and the original audio was kept.

   `_RecoveryMap` holds a **copy**: this is the one place something other than
   the reader writes to a map, and the CTDB report is drawn later from the rip's
   own record of what the drive reported. It is inert without a TUI or without a
   captured map, so the loop needs no branches — which also kept
   `_recover_failed_tracks` under the C901 limit it was already sitting on.
6. ~~CTDB result map via the pre/post diff (§5).~~ — **DONE 2026-08-13.**
   `ctdb_repair._repaired_sector_map` diffs the pre/post buffers at the commit
   point (where both are already in hand, so no extra I/O), carried on
   `CtdbRepairResult.repaired_sectors`; `cdda2img._print_ctdb_repair_map` draws
   it once, above the AR re-verify report and inside the same `ui.pause()`.

   **The three-lane sketch collapsed to one lane on inspection, and the reason is
   structural.** §5 proposed `undamaged / repaired / unrepairable`. The third
   lane is empty on the only path where a map can be drawn at all: a map requires
   a write-back, the write-back requires the CTDB per-track CRC gate (and, when
   asked, the AR gate) to pass, and the CRC gate covers `[bounds[0], bounds[-1])`
   — every word a repair can touch. So a drawable map is by construction a map of
   a *fully successful* repair. Recorded rather than quietly dropped, because
   "we chose not to build it" and "it cannot be non-empty" are different claims.

   Consequence worth keeping: on the failure paths `repaired_sectors` is `None`,
   **not** an all-zero map. Nothing is written there, so a diff would be all
   zeros — indistinguishable from a successful repair that changed nothing.
   "Repaired nothing" and "did not repair" must not render alike, and a test
   states that rather than the docstring alone.

   The diff is chunked at 4096 sectors: a whole-disc `a != b` allocates a boolean
   array the size of the PCM (816 MB) on top of the two buffers already resident,
   and this machine's `/tmp` lesson makes that the wrong default to reach for.

   **Amended the same day — paired with the read's C2 damage map, as two ROWS.**
   kgr asked for the pairing (the repair map alone is not legible: a coloured
   cell is *good* news, so "all blue" reads as success when it means nothing was
   repaired). The row count is the part that changed on inspection. `_GLYPH`
   defines the two-lane vocabulary as **filled = healthy**, which the repair lane
   inverts — so sharing one row states the opposite of the truth in mono, under
   `NO_COLOR`, and in a piped log, where the glyph is the only channel there is.
   The one-row constraint belongs to the **live** map, which shares a line with
   the progress bar; a static end-of-rip report has vertical space for free.

   ```
     CTDB parity repair:
       Read damage     ███████████▒▒███████████████▒███████████████  700 sector(s)
       Parity repairs  ███████████▒▒█████████████████████████▒█████  340 sector(s)
   ```

   Bucketed to the same width from per-sector maps of the same disc, so the rows
   are column-aligned and readable against each other: marked above and clear
   below is damage parity did not rewrite; clear above and marked below is a
   repair the drive never flagged. The damage row is **omitted** when no map was
   captured rather than drawn clean — the same rule that kept a DIY Q lane out of
   the live map.

   **Superseded 2026-08-14 on the first live run — before/after, not
   damage/repairs.** The pairing above answers "where did the drive struggle and
   what did parity touch?". The question the user actually has is *"is the disc
   fixed?"*, and a damage row that still shows the original damage **after a
   successful repair** answers "no" in the only vocabulary the row has. kgr had
   already said what he expected, on 2026-08-13: *"Either the final map still
   shows errors, in which case we move on to a CTDB parity repair, or it's all
   blue (or white, if mono), and we're done."* Two rows were the right call for
   the glyph-inversion reason recorded above; the two *quantities* were wrong.

   ```
      CTDB parity repair:
        Before  ████▒███████████████████████████████████████████████  1 flagged
        After   ████████████████████████████████████████████████████  clean
   ```

   The map the user watched during the read **is** the "before", so sourcing that
   row from the C2 damage map keeps it continuous with what they just saw;
   without a C2 capture it falls back to what parity rewrote, and the suffix
   names which quantity is being counted rather than swapping one for the other
   under an unchanged label.

   "After: clean" is **earned, not assumed**: `repair_whole_disc` writes the PCM
   back only after `verify_ctdb` *and* `verify_ar` both pass, so on the only path
   that reaches the renderer every track CTDB called damaged verifies against
   both references. The gate does not cover everything, though — AR and CTDB are
   different reference populations, so a track can still fail AR while passing
   the per-track CRC that admitted the repair. `resolved=` carries the post-repair
   AR verdict; when it is false the "after" row draws the residual
   (`damage & ~repaired`) and the recovery ladder runs next. Deriving that
   residual in the *resolved* branch would be wrong in the opposite direction:
   C2 over-flags, so unrewritten flags are refuted evidence once AR verifies, and
   drawing them would paint phantom damage directly above a report saying every
   track is OK.

   **The width budget was also wrong, and this is the reusable part.** The first
   version sized the bar as `terminal_width - 4` while the line carried a
   20-column label prefix and a 13-column suffix — emitting a **153-column line**
   under a `min(…, 120)` cap that read like a guard and was cosmetic. It wrapped
   on every terminal narrower than 153, including the ones the cap was supposedly
   protecting. The live map never had this bug because it subtracts the whole
   line's furniture and then **clips** (`visible = min(map_cols, avail)`); a
   one-shot report has no pinning requirement and so no clip, which means it has
   to get the budget right up front. Note the floor is the same trap wearing a
   different constant: `max(16, …)` forces a bar wider than fits on a narrow
   terminal, so below the minimum the report prints counts and no bars.

   Worth recording why the wrap was cosmetic rather than corrupting: `pause()`
   calls `_clear_region()`, which zeroes `_prev_height`, so the TUI has
   disclaimed the region before any of this is printed. Inside a *live* frame the
   same wrap would be the N8 stray-write mechanism — `_prev_height` counts
   **logical** lines and the rewind would land short.

   **This required retaining the damage map past the read** (`_rip_disc_stage`
   now returns it; `map_cb` became unconditional, so a `--no-tui` rip captures it
   too — the rendering was the reason for the gate, never the capture).
   `ui.set_map(None)` still fires: it releases the *renderer's* reference so it
   stops polling a finished read, which is not the same as the data ceasing to be
   interesting. That retention is also **step 5's prerequisite**, which is why
   the two jobs are cheaper together than apart.

Step 3 was deliberately ahead of step 2 in dependency terms, and that held: the
useful half needed no one's permission.

### What shipped, and the two things the bench could not have caught

Both were found by writing production code against a real terminal, which is the
honest limit of a synthetic bench — worth recording, because the bench was
otherwise right about everything it was asked.

- **A resize re-buckets the map.** The bench pinned width by construction; the
  real `_build` recomputes `shutil.get_terminal_size()` every frame. Resolved by
  pinning cell size for the life of the read and **clipping cells off the right**
  when the terminal narrows. Clipping loses least: the content is left-weighted
  and the frontier is also reported numerically.
- **`count(1, …)` is not `count non-zero`.** The first census counted bytes equal
  to 1, so any other marker byte — a severity value, a bitmask — would have read
  as a clean disc. Caught by the test written for it. The map now counts *zeros*
  and subtracts, so an unexpected value falls to the safe side.

One more, from the first render rather than from a test: `per = total // width`
leaves up to `width - 1` sectors at the **end** of the disc in no cell at all —
the outer edge, where damage concentrates. The last cell now absorbs the
remainder.
