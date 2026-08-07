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
   the Q lane and nothing else.
3. **Wire the C2 lane and the frontier into `_rip_disc_stage`** — needs no reply,
   because `status_map` is already there. Ship the map with one lane live and the
   Q lane dark rather than waiting.
4. Q lane, once §148 is answered (or the DIY path if they decline).
5. Recovery-ladder rendering (§2), which is our own work.
6. CTDB result map via the pre/post diff (§5).

Step 3 is deliberately ahead of step 2 in dependency terms: the useful half needs
no one's permission.
